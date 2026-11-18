"""Static, password-encrypted snapshot of the dashboard for GitHub Pages.

    uv run python -m dashboard.snapshot              # build data/pages/index.html only
    uv run python -m dashboard.snapshot --publish    # build + force-push it to the gh-pages branch

GitHub Pages can't run Streamlit, so this renders a self-contained HTML page (KPIs, alerts,
Plotly charts, tables) from the local SQLite DB, then encrypts it:

    html --gzip--> AES-256-GCM, key = PBKDF2-SHA256(SNAPSHOT_PASSWORD, salt, 600k iterations)

The published index.html holds only the ciphertext plus a small unlock form that decrypts in
the browser with WebCrypto. The password lives in .env (SNAPSHOT_PASSWORD) and never leaves the Mac.
The gh-pages branch is force-pushed as a single commit, so old snapshots don't pile up in history.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from plotly.offline import get_plotlyjs_version

from dashboard import ui
from dashboard.snapshot_hero import hero_data
from garmin import config, insights

DAYS = 365
DEFAULT_RANGE = 90
ITERATIONS = 600_000
MIN_PASSWORD_LEN = 16
OUT_DIR = config.PROJECT_ROOT / "data" / "pages"
SALT_FILE = config.PROJECT_ROOT / "data" / ".snapshot_salt"
PLOTLY_JS = f"https://cdn.jsdelivr.net/npm/plotly.js-dist-min@{get_plotlyjs_version()}/plotly.min.js"
ZONE_COLORS = ["#9cc8f2", "#5ea3e6", "#3987e5", "#2a64b3", "#1d4580"]  # sequential: zone 1 -> 5


# ---------------------------------------------------------------- data
def _df(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def _pretty(t: str | None) -> str:
    return (t or "other").replace("_", " ").title()


def _last(s: pd.Series):
    s = s.dropna()
    return None if s.empty else s.iloc[-1]


def _avg(s: pd.Series, n: int, offset: int = 0):
    s = s.dropna()
    s = s.iloc[len(s) - n - offset: len(s) - offset] if len(s) > offset else s.iloc[0:0]
    return None if s.empty else float(s.mean())


def _delta(cur, prev, unit: str = "", better: str = "down", fmt: str = ".0f") -> tuple[str, str]:
    """('+3 bpm vs prev 7d', css class); colour only when the direction is meaningful."""
    if cur is None or prev is None:
        return "", "c-muted"
    d = cur - prev
    if abs(d) < 0.5:
        return "flat vs prev 7d", "c-muted"
    good = (d < 0) == (better == "down")
    return f"{'+' if d > 0 else '−'}{format(abs(d), fmt)}{unit} vs prev 7d", "c-good" if good else "c-bad"


# ---------------------------------------------------------------- figures
def _x(df: pd.DataFrame, col: str = "calendar_date") -> list[str]:
    return df[col].tolist()


def fig_hrv(hv: pd.DataFrame) -> go.Figure:
    f = ui.fig(240, legend=True)
    b = hv.dropna(subset=["baseline_balanced_low_ms", "baseline_balanced_upper_ms"])
    f.add_trace(go.Scatter(x=_x(b), y=b["baseline_balanced_upper_ms"], mode="lines", line=dict(width=0),
                           hoverinfo="skip", showlegend=False))
    f.add_trace(go.Scatter(x=_x(b), y=b["baseline_balanced_low_ms"], mode="lines", line=dict(width=0),
                           fill="tonexty", fillcolor="rgba(62,207,142,0.10)", name="Balanced range",
                           hovertemplate="%{y} ms floor<extra></extra>"))
    f.add_trace(go.Scatter(x=_x(hv), y=hv["last_night_avg_ms"], mode="markers", name="Nightly",
                           marker=dict(size=5, color=ui.SERIES[0], opacity=.55),
                           hovertemplate="%{y} ms<extra>nightly</extra>"))
    f.add_trace(go.Scatter(x=_x(hv), y=hv["weekly_avg_ms"], mode="lines", name="7-day avg",
                           line=dict(width=2, color=ui.SERIES[0]), hovertemplate="%{y} ms<extra>7-day avg</extra>"))
    f.update_yaxes(ticksuffix=" ms")
    return f


def fig_line_avg(df: pd.DataFrame, col: str, color: str, unit: str, name: str) -> go.Figure:
    f = ui.fig(220, legend=True)
    d = df.dropna(subset=[col])
    f.add_trace(go.Scatter(x=_x(d), y=d[col], mode="markers", name="Daily", marker=dict(size=4, color=color, opacity=.5),
                           hovertemplate=f"%{{y:.0f}} {unit}<extra>daily</extra>"))
    f.add_trace(go.Scatter(x=_x(d), y=d[col].rolling(7, min_periods=3).mean().round(1), mode="lines", name="7-day avg",
                           line=dict(width=2, color=color), hovertemplate=f"%{{y:.1f}} {unit}<extra>7-day avg</extra>"))
    f.update_yaxes(ticksuffix=f" {unit}" if unit else "")
    return f


def fig_sleep(sl: pd.DataFrame) -> go.Figure:
    f = ui.fig(240, legend=True)
    for col, name, color in ui.STAGES:
        f.add_trace(go.Bar(x=_x(sl), y=(sl[col] / 3600).round(2), name=name, marker=dict(color=color, line_width=0),
                           hovertemplate=f"%{{y:.1f}} h<extra>{name}</extra>"))
    f.update_layout(barmode="stack", bargap=0.18)
    f.update_yaxes(ticksuffix=" h")
    return f


def fig_score(sl: pd.DataFrame) -> go.Figure:
    f = ui.fig(220)
    d = sl.dropna(subset=["sleep_score"])
    f.add_trace(go.Bar(x=_x(d), y=d["sleep_score"], marker=dict(color=ui.SERIES[5], line_width=0), name="Score",
                       hovertemplate="score %{y}<extra></extra>"))
    f.add_trace(go.Scatter(x=_x(d), y=d["sleep_score"].rolling(7, min_periods=3).mean().round(0), mode="lines",
                           line=dict(width=2, color=ui.TEXT_2), name="7-day avg", hovertemplate="%{y} avg<extra></extra>"))
    f.update_yaxes(range=[0, 100])
    return f


def fig_battery(ds: pd.DataFrame) -> go.Figure:
    f = ui.fig(220)
    d = ds.dropna(subset=["bb_min", "bb_max"])
    f.add_trace(go.Bar(x=_x(d), y=d["bb_max"] - d["bb_min"], base=d["bb_min"], name="Low → peak",
                       marker=dict(color=ui.SERIES[2], line_width=0),
                       customdata=d[["bb_min", "bb_max"]].values,
                       hovertemplate="%{customdata[0]} → %{customdata[1]}<extra>Body Battery</extra>"))
    f.update_yaxes(range=[0, 100])
    return f


def fig_steps(ds: pd.DataFrame) -> go.Figure:
    f = ui.fig(220)
    d = ds.dropna(subset=["steps"])
    f.add_trace(go.Bar(x=_x(d), y=d["steps"], marker=dict(color=ui.SERIES[0], line_width=0), name="Steps",
                       hovertemplate="%{y:,} steps<extra></extra>"))
    f.add_trace(go.Scatter(x=_x(d), y=d["steps"].rolling(7, min_periods=3).mean().round(0), mode="lines",
                           line=dict(width=2, color=ui.TEXT_2), name="7-day avg",
                           hovertemplate="%{y:,} avg<extra></extra>"))
    return f


def fig_load(load: pd.DataFrame) -> go.Figure:
    f = ui.fig(240, legend=True)
    d = load.dropna(subset=["chronic_weekly"])
    f.add_trace(go.Scatter(x=_x(d, "date"), y=(d["chronic_weekly"] * 1.3).round(0), mode="lines", line=dict(width=0),
                           hoverinfo="skip", showlegend=False))
    f.add_trace(go.Scatter(x=_x(d, "date"), y=(d["chronic_weekly"] * 0.8).round(0), mode="lines", line=dict(width=0),
                           fill="tonexty", fillcolor="rgba(62,207,142,0.09)", name="0.8–1.3× chronic",
                           hoverinfo="skip"))
    f.add_trace(go.Scatter(x=_x(d, "date"), y=d["chronic_weekly"], mode="lines", name="Chronic (weekly avg)",
                           line=dict(width=2, color=ui.MUTED, dash="dot"), hovertemplate="%{y:.0f}<extra>chronic</extra>"))
    f.add_trace(go.Scatter(x=_x(d, "date"), y=d["acute_7d"], mode="lines", name="Acute (7-day)",
                           line=dict(width=2, color=ui.SERIES[1]), customdata=d["acwr"],
                           hovertemplate="%{y:.0f} · ratio %{customdata:.2f}<extra>acute</extra>"))
    return f


def fig_volume(acts: pd.DataFrame, types: list[str], colors: dict[str, str]) -> go.Figure:
    f = ui.fig(240, legend=True)
    if acts.empty:
        return f
    a = acts.assign(week=pd.to_datetime(acts["calendar_date"]).dt.to_period("W-SUN").dt.start_time.dt.strftime("%Y-%m-%d"),
                    grp=acts["type_key"].where(acts["type_key"].isin(types), "other"))
    piv = a.pivot_table(index="week", columns="grp", values="duration_s", aggfunc="sum", fill_value=0) / 3600
    for t in types + ["other"]:
        if t in piv:
            f.add_trace(go.Bar(x=piv.index.tolist(), y=piv[t].round(2), name=_pretty(t),
                               marker=dict(color=colors.get(t, ui.FAINT), line_width=0),
                               hovertemplate=f"%{{y:.1f}} h<extra>{_pretty(t)}</extra>"))
    f.update_layout(barmode="stack", bargap=0.2)
    f.update_xaxes(hoverformat="week of %-d %b %Y")
    f.update_yaxes(ticksuffix=" h")
    return f


def fig_weight(w: pd.DataFrame) -> go.Figure:
    f = ui.fig(220)
    f.add_trace(go.Scatter(x=_x(w), y=w["weight_kg"].round(1), mode="lines+markers", name="Weight",
                           line=dict(width=2, color=ui.SERIES[3]), marker=dict(size=8),
                           hovertemplate="%{y:.1f} kg<extra></extra>"))
    f.update_yaxes(ticksuffix=" kg")
    return f


def fig_fitness_age(fa: pd.DataFrame) -> go.Figure:
    f = ui.fig(220, legend=True)
    f.add_trace(go.Scatter(x=_x(fa, "as_of_date"), y=fa["fitness_age"].round(1), mode="lines+markers", name="Fitness Age",
                           line=dict(width=2, color=ui.SERIES[4]), marker=dict(size=8),
                           hovertemplate="%{y:.1f} y<extra>fitness age</extra>"))
    f.add_trace(go.Scatter(x=_x(fa, "as_of_date"), y=fa["chronological_age"], mode="lines", name="Actual age",
                           line=dict(width=2, color=ui.MUTED, dash="dot"), hovertemplate="%{y} y<extra>actual</extra>"))
    return f


# ---------------------------------------------------------------- tables
def workouts_table(acts: pd.DataFrame, colors: dict[str, str], n: int = 20) -> str:
    rows = []
    for r in acts.sort_values(["calendar_date", "start_local"], ascending=False).head(n).itertuples():
        dist = f"{r.distance_m / 1000:.2f} km" if r.distance_m and r.distance_m > 0 else "—"
        te = ui.esc(r.te_label.replace("_", " ").lower()) if isinstance(r.te_label, str) else ""
        rows.append(
            f"<tr><td><b>{ui.esc(r.name)}</b><div class='sub'>{ui.esc((r.start_local or r.calendar_date or '')[:16])}</div></td>"
            f"<td>{ui.pill(_pretty(r.type_key), colors.get(r.type_key, ui.FAINT))}</td>"
            f"<td class='num'>{ui.hm((r.duration_s or 0) / 3600)}</td><td class='num hide-sm'>{dist}</td>"
            f"<td class='num'>{ui.num(r.avg_hr_bpm)}</td><td class='num'>{ui.num(r.training_load)}</td>"
            f"<td class='hide-sm'>{te}</td></tr>")
    return ("<table class='tbl'><thead><tr><th>Workout</th><th>Type</th><th class='num'>Time</th>"
            "<th class='num hide-sm'>Distance</th><th class='num'>Avg HR</th><th class='num'>Load</th>"
            f"<th class='hide-sm'>Effect</th></tr></thead><tbody>{''.join(rows)}</tbody></table>")


def nights_table(sl: pd.DataFrame, hv: pd.DataFrame, ds: pd.DataFrame, n: int = 14) -> str:
    m = (sl.merge(hv[["calendar_date", "last_night_avg_ms"]], on="calendar_date", how="left")
           .merge(ds[["calendar_date", "rhr_bpm"]], on="calendar_date", how="left"))
    rows = []
    for r in m.sort_values("calendar_date", ascending=False).head(n).itertuples():
        score = r.sleep_score
        cls = " class='flag'" if score is not None and not pd.isna(score) and score < 60 else ""
        rows.append(
            f"<tr{cls}><td class='mono'>{ui.esc(r.calendar_date)}</td><td class='num'>{ui.hm((r.sleep_s or 0) / 3600)}</td>"
            f"<td class='num hide-sm'>{ui.hm((r.deep_s or 0) / 3600)}</td><td class='num hide-sm'>{ui.hm((r.rem_s or 0) / 3600)}</td>"
            f"<td class='num'>{ui.num(score)}</td><td class='num'>{ui.num(r.last_night_avg_ms)}</td>"
            f"<td class='num'>{ui.num(r.rhr_bpm)}</td></tr>")
    return ("<table class='tbl'><thead><tr><th>Night</th><th class='num'>Asleep</th><th class='num hide-sm'>Deep</th>"
            "<th class='num hide-sm'>REM</th><th class='num'>Score</th><th class='num'>HRV ms</th>"
            f"<th class='num'>RHR</th></tr></thead><tbody>{''.join(rows)}</tbody></table>")


# ---------------------------------------------------------------- page
ASSETS = Path(__file__).resolve().parent / "snapshot_assets"
FONTS = ("https://fonts.googleapis.com/css2?family=Geist:wght@300;400;500;600"
         "&family=Geist+Mono:wght@400;500&display=swap")
SEV_ICON = {"critical": "▲", "warning": "●", "info": "◆", "good": "✓"}

I_BOLT = '<svg width="18" height="18" viewBox="0 0 24 24" fill="#ff8f4a" aria-hidden="true"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg>'
I_MOON = ('<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#a99bff" stroke-width="2" stroke-linecap="round" '
          'stroke-linejoin="round" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>')
I_TARGET = ('<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#5fd6a8" stroke-width="2" aria-hidden="true">'
            '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.2" fill="#5fd6a8"/></svg>')
I_HEART_LINE = ('<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#a99bff" stroke-width="2.2" stroke-linecap="round" '
                'stroke-linejoin="round" aria-hidden="true"><path d="M3 12h4l2-5 4 10 2-5h6"/></svg>')
I_HEART = ('<svg width="20" height="20" viewBox="0 0 24 24" fill="#111" aria-hidden="true"><path d="M12 21s-7.5-4.6-9.6-9.2C.9 8.4 3 4.5 6.8 4.5c2.1 0 '
           '3.6 1.1 4.4 2.6.8-1.5 2.3-2.6 4.4-2.6 3.8 0 5.9 3.9 4.4 7.3C19.5 16.4 12 21 12 21z"/></svg>')
I_BELL = ('<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#e6e6ea" stroke-width="1.8" stroke-linecap="round" '
          'stroke-linejoin="round" aria-hidden="true"><path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>')
I_EXPAND = ('<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#9a9aa1" stroke-width="2" stroke-linecap="round" '
            'stroke-linejoin="round" aria-hidden="true"><path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/></svg>')
I_CLOSE = ('<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#e6e6ea" stroke-width="2.2" stroke-linecap="round" '
           'aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>')
I_SPARK = ('<svg width="18" height="18" viewBox="0 0 24 24" fill="#c7bbff" aria-hidden="true"><path d="M12 2c.6 4.8 2.7 8.4 10 10-7.3 '
           '1.6-9.4 5.2-10 10-.6-4.8-2.7-8.4-10-10 7.3-1.6 9.4-5.2 10-10z"/></svg>')
I_AVATAR = ('<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#c7bbff" stroke-width="1.8" stroke-linecap="round" '
            'stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="6.5"/><path d="M9 3h6M9 21h6M12 9v3l2 1.5"/></svg>')


def _alerts_html(alerts: list) -> tuple[str, str, int]:
    """(strip, menu items, count of warnings+criticals)."""
    esc = ui.esc
    items = "".join(
        f"<div class='mi tone-{a.severity}'><span class='ic'>{SEV_ICON[a.severity]}</span>"
        f"<div><b>{esc(a.title)}</b><span class='d'>{esc(a.detail)}</span></div></div>" for a in alerts) \
        or "<div class='mi'><div><b>No signals</b><span class='d'>Nothing stands out.</span></div></div>"
    n_bad = sum(a.severity in ("critical", "warning") for a in alerts)
    strip = ""
    if alerts:
        a = alerts[0]
        more = f"<span class='more'>+{len(alerts) - 1} more</span>" if len(alerts) > 1 else ""
        strip = (f"<button class='strip tone-{a.severity}' id='strip' aria-label='Show all signals'>"
                 f"<span class='ic'>{SEV_ICON[a.severity]}</span><span class='t'><b>{esc(a.title)}</b> — {esc(a.detail)}</span>{more}</button>")
    return strip, items, n_bad


def _hero_html(hero: dict) -> str:
    odo_cols = "".join("<div class='dcol'><div class='s'></div></div>" for _ in range(3))
    matrix = []
    for r in range(4):  # rows top -> bottom; a column lights up from the bottom to its level
        for lv in hero["sleep"].get("matrix", [0] * 13):
            lit = (3 - r) < lv
            top = (3 - r) == lv - 1
            matrix.append(f"<i class='{'lit' if lit else ''}{' top' if top else ''}' style='animation-delay:{(len(matrix) * .37) % 3:.2f}s'></i>")
    stage_cols = [("Deep", "var(--deep)"), ("Light", "var(--light)"), ("REM", "var(--rem)"), ("Awake", "var(--awake)")]
    stats = "".join(f"<div class='stat' id='st{i}'><div class='k'><i style='background:{c}'></i>{n}</div><div class='v'></div></div>"
                    for i, (n, c) in enumerate(stage_cols))
    stagebar = "".join(f"<i id='sb{i}' style='background:{c};width:0'></i>" for i, (_, c) in enumerate(stage_cols))
    chip = hero["heart"]["chip"]
    chip_cls = "on" if chip["tone"] == "good" else "amber" if chip["tone"] == "warn" else ""
    insight = ui.esc(hero["sleep"].get("insight", ""))
    return f"""
<section class="hero" id="hero">
 <div class="row r1">
  <section class="card energy" aria-label="Body Battery">
   <div class="tint" style="background:radial-gradient(420px 260px at 85% 90%,rgba(255,120,50,.10),transparent 70%)"></div>
   <div class="chead"><div class="ico">{I_BOLT}</div>Body Battery</div>
   <div class="ebody">
    <div><div class="lbl" id="eLbl"></div>
     <div class="odo" id="odo" aria-live="polite">{odo_cols}<span class="dash">—</span></div>
     <div class="delta" id="eDelta"></div></div>
    <div class="bars" id="bars"><div class="tip" id="tip"></div></div>
   </div>
  </section>
  <section class="card sleep" id="sleep" aria-label="Sleep">
   <div class="tint" style="background:radial-gradient(380px 300px at 80% 100%,rgba(143,124,247,.12),transparent 70%)"></div>
   <div class="chead"><div class="ico">{I_MOON}</div>Sleep</div>
   <button class="rbtn only-closed" id="sOpen" aria-expanded="false" aria-label="Expand sleep details">{I_EXPAND}</button>
   <button class="rbtn only-open" id="sClose" aria-label="Close sleep details">{I_CLOSE}</button>
   <div class="total" id="sTotal"></div>
   <div class="lbl only-closed" id="sLine" style="margin-top:10px"></div>
   <div class="lbl only-open" id="sWindow" style="margin-top:10px"></div>
   <div class="only-closed"><div class="matrix" aria-hidden="true">{''.join(matrix)}</div>
     <div class="mcap">LAST 13 NIGHTS · SCORE</div></div>
   <div class="stats only-open">{stats}</div>
   <div class="only-open"><div class="lbl" id="sLabel" style="margin-top:22px"></div>
     <div class="stagebar" aria-hidden="true">{stagebar}</div>
     <div class="trend"><div class="cap" style="margin-top:22px">Sleep score · last 30 nights</div>
       <svg id="trendSvg" height="200" role="img" aria-label="Sleep score over the last 30 nights"></svg>
       <div class="tt" id="trendTip"></div></div></div>
   <div class="insight">{I_SPARK}<span>{insight}</span></div>
  </section>
 </div>
 <div class="row r2">
  <section class="card activity" aria-label="Activity">
   <div class="tint" style="background:radial-gradient(360px 260px at 20% 80%,rgba(95,214,168,.10),transparent 70%)"></div>
   <div class="chead"><div class="ico">{I_TARGET}</div>Activity</div>
   <div class="abody">
    <div class="ringbox">
     <svg width="176" height="176" viewBox="0 0 176 176" aria-hidden="true">
      <defs><filter id="rglow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="4"/></filter></defs>
      <circle cx="88" cy="88" r="76" fill="none" stroke="rgba(255,255,255,.07)" stroke-width="10"/>
      <circle class="ring-g" cx="88" cy="88" r="76" fill="none" stroke="#5fd6a8" stroke-width="10" stroke-linecap="round" transform="rotate(-90 88 88)" filter="url(#rglow)" opacity=".45" stroke-dasharray="0 478"/>
      <circle class="ring" cx="88" cy="88" r="76" fill="none" stroke="#5fd6a8" stroke-width="10" stroke-linecap="round" transform="rotate(-90 88 88)" stroke-dasharray="0 478"/>
      <circle class="rdot" r="12" fill="#9ff0cf" filter="url(#rglow)" cx="88" cy="12" opacity="0"/>
      <circle class="rdot" r="5.5" fill="#fff" cx="88" cy="12" opacity="0"/>
     </svg>
     <div class="rtxt" id="aVal"></div><div class="rlbl" id="aOf"></div>
    </div>
    <div style="min-width:0">
     <div class="chip" id="aChip"><span class="dot"></span><span id="aChipT"></span></div>
     <div class="atitle" id="aTitle"></div><div class="asub" id="aSub"></div>
     <button class="pillbtn" id="aGo">Workouts <span aria-hidden="true">↓</span></button>
    </div>
   </div>
  </section>
  <section class="card heart" aria-label="Heart">
   <div class="tint" style="background:radial-gradient(380px 220px at 60% 45%,rgba(143,124,247,.10),transparent 70%)"></div>
   <div class="chead"><div class="ico">{I_HEART_LINE}</div>Heart</div>
   <div class="status"><span class="chip {chip_cls}"><span class="dot"></span>{ui.esc(chip['text'])}</span></div>
   <div class="wave" id="wave" aria-hidden="true">{'<i class="wb"></i>' * 36}</div>
   <div class="wcap" id="wCap"></div>
   <div class="hrow"><div class="hbtn">{I_HEART}</div>
    <div style="min-width:0"><div class="htitle" id="hTitle"></div><div class="hsub" id="hSub"></div></div>
    <div class="hright" id="hRight"></div></div>
  </section>
 </div>
</section>"""


def build_html(conn: sqlite3.Connection, today: date | None = None) -> str:
    today = today or date.today()
    end_s = conn.execute("SELECT max(calendar_date) FROM daily_summary").fetchone()[0]
    if not end_s:
        raise SystemExit("No data in the database yet — run `make sync` first.")
    end = date.fromisoformat(end_s)
    start = end - timedelta(days=DAYS - 1)
    lead = (start - timedelta(days=13)).isoformat()  # extra days so 7-day averages are valid at the left edge
    S, E = start.isoformat(), end.isoformat()

    ds = _df(conn, "SELECT * FROM daily_summary WHERE calendar_date BETWEEN ? AND ? ORDER BY calendar_date", (lead, E))
    sl = _df(conn, "SELECT calendar_date, sleep_s, deep_s, light_s, rem_s, awake_s, sleep_score FROM sleep "
                   "WHERE calendar_date BETWEEN ? AND ? ORDER BY calendar_date", (lead, E))
    hv = _df(conn, "SELECT calendar_date, last_night_avg_ms, weekly_avg_ms, baseline_balanced_low_ms, "
                   "baseline_balanced_upper_ms, status FROM hrv WHERE calendar_date BETWEEN ? AND ? "
                   "ORDER BY calendar_date", (lead, E))
    acts = _df(conn, "SELECT activity_id, name, type_key, start_local, calendar_date, duration_s, distance_m, "
                     "avg_hr_bpm, training_load, te_label FROM activity_ext WHERE calendar_date BETWEEN ? AND ? "
                     "ORDER BY start_local", (S, E))
    w = _df(conn, "SELECT calendar_date, avg(weight_kg) AS weight_kg FROM weight WHERE weight_kg IS NOT NULL "
                  "GROUP BY calendar_date ORDER BY calendar_date")
    fa = _df(conn, "SELECT as_of_date, fitness_age, chronological_age FROM fitness_age "
                   "WHERE fitness_age IS NOT NULL ORDER BY as_of_date")
    load = pd.DataFrame(insights.training_load(conn, end, DAYS))
    alerts = insights.compute_alerts(conn, today)
    hero = hero_data(conn, end)

    all_types = [r[0] for r in conn.execute("SELECT type_key FROM activity GROUP BY 1 ORDER BY count(*) DESC LIMIT 5")]
    colors = {t: ui.SERIES[i] for i, t in enumerate(all_types)}  # fixed order over all history, never by range rank

    vis = lambda df, col="calendar_date": df[df[col] >= S]  # noqa: E731
    figs: dict[str, go.Figure] = {
        "hrv": fig_hrv(vis(hv)),
        "rhr": fig_line_avg(ds, "rhr_bpm", ui.SERIES[1], "bpm", "Resting HR"),
        "sleep": fig_sleep(vis(sl)),
        "score": fig_score(sl),
        "bb": fig_battery(vis(ds)),
        "stress": fig_line_avg(ds, "stress_avg", ui.SERIES[3], "", "Stress"),
        "steps": fig_steps(ds),
        "load": fig_load(load) if len(load) else ui.fig(),
        "volume": fig_volume(acts, all_types, colors),
    }
    panels = [
        ("hrv", "HRV", "overnight RMSSD vs your balanced range", ""),
        ("rhr", "Resting heart rate", "bpm", ""),
        ("sleep", "Sleep stages", "hours per night", ""),
        ("score", "Sleep score", "0–100", ""),
        ("bb", "Body Battery", "daily low → peak", ""),
        ("stress", "Stress", "daily average, 0–100", ""),
        ("steps", "Steps", "per day", ""),
        ("load", "Training load", "acute 7-day vs chronic weekly", ""),
        ("volume", "Weekly volume", "hours by activity type", "wide"),
    ]
    if len(w) >= 2:
        figs["weight"] = fig_weight(w)
        panels.append(("weight", "Weight", f"{len(w)} weigh-ins", ""))
    if len(fa) >= 2:
        figs["fa"] = fig_fitness_age(fa)
        panels.append(("fa", "Fitness Age", "vs actual age", ""))
    date_figs = [k for k in figs if k not in ("weight", "fa")]  # these keep their full history
    for k in date_figs:
        figs[k].update_xaxes(range=[(end - timedelta(days=DEFAULT_RANGE - 1)).isoformat(),
                                    (end + timedelta(hours=12)).isoformat()])

    def card(key, title, sub, cls):
        return (f"<section class='card {cls}'><div class='ch'>{ui.esc(title)}<span>{ui.esc(sub)}</span></div>"
                f"<div class='chart' id='c-{key}'></div></section>")

    fig_json = {k: json.loads(f.to_json()) for k, f in figs.items()}
    strip, menu_items, n_bad = _alerts_html(alerts)
    badge = "<span class='badge-ring'></span><span class='badge'></span>" if n_bad else ""
    built = datetime.now().strftime("%-d %b %H:%M")
    js_data = (f"const HERO = {json.dumps(hero, separators=(',', ':'))};\n"
               f"const FIGS = {json.dumps(fig_json, separators=(',', ':'))};\n"
               f"const DATE_FIGS = {json.dumps(date_figs)};\nconst END = {json.dumps(E)};\n")
    css = (ASSETS / "page.css").read_text()
    js = (ASSETS / "page.js").read_text()
    views = "".join(f"<button role='tab' data-v='{v}'>{v.title()}</button>" for v in ("today", "week", "month"))
    ranges = "".join(f"<button role='tab' data-v='{d}'>{lab}</button>" for d, lab in ((30, "30D"), (90, "90D"), (365, "1Y")))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#09090a">
<title>Health snapshot</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="{FONTS}" rel="stylesheet">
<style>{css}</style>
<script src="{PLOTLY_JS}"></script>
</head><body><div class="app">
<header class="hdr">
 <div class="who"><div class="avatar">{I_AVATAR}</div>
  <div><div class="hello" id="hello">Hello</div><div class="date">Data through {ui.esc(hero['end_label'])} · built {built}</div></div></div>
 <div class="hdr-r">
  <div class="switch" id="viewSwitch" role="tablist" aria-label="Period"><div class="ind"></div>{views}</div>
  <button class="bell" id="bell" aria-label="Signals ({len(alerts)})" aria-expanded="false" aria-controls="menu">{I_BELL}{badge}</button>
  <div class="menu" id="menu" role="dialog" aria-label="Signals">{menu_items}
   <button class="mi lock" id="lock">Lock — forget the password on this device</button></div>
 </div>
</header>
{strip}
{_hero_html(hero)}
<div class="sec"><h2>Trends<small>through {ui.esc(E)}</small></h2>
 <div class="switch sm" id="rangeSwitch" role="tablist" aria-label="Chart range"><div class="ind"></div>{ranges}</div></div>
<div class="grid">{''.join(card(*p) for p in panels)}</div>
<div class="sec" id="workouts"><h2>Recent workouts<small>{len(acts)} in the last year</small></h2></div>
<section class="card" style="padding:14px 18px"><div class="scroll">{workouts_table(acts, colors)}</div></section>
<div class="sec"><h2>Recent nights<small>sleep, HRV and resting HR</small></h2></div>
<section class="card" style="padding:14px 18px"><div class="scroll">{nights_table(vis(sl), hv, ds)}</div></section>
<div class="foot">Encrypted snapshot of your local Garmin dashboard · regenerated after each daily sync · not medical advice</div>
</div>
<script>{js_data}</script>
<script>{js}</script>
</body></html>"""


# ---------------------------------------------------------------- encryption
LOCK_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Health snapshot</title>
<style>
:root { color-scheme: dark; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0d0f12; color: #e7e9ec;
       font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif; padding: 16px; box-sizing: border-box; }
form { width: 100%; max-width: 340px; box-sizing: border-box; background: #14171c; border: 1px solid #23272e; border-radius: 12px; padding: 24px; }
h1 { font-size: 17px; font-weight: 600; margin: 0 0 4px; }
p { font-size: 13px; color: #8a919b; margin: 0 0 18px; line-height: 1.5; }
input[type=password] { width: 100%; box-sizing: border-box; background: #0d0f12; border: 1px solid #2d323a; color: #e7e9ec;
       border-radius: 8px; padding: 11px 12px; font-size: 16px; }
input[type=password]:focus { outline: 2px solid #4cc3d9; outline-offset: 1px; border-color: transparent; }
label.r { display: flex; gap: 8px; align-items: center; font-size: 13px; color: #b9bec6; margin: 14px 0 18px; }
button { width: 100%; background: #0f2a33; color: #4cc3d9; border: 1px solid #4cc3d9; border-radius: 8px; padding: 11px;
         font-size: 14px; font-weight: 600; cursor: pointer; }
button:disabled { opacity: .6; cursor: progress; }
.err { color: #f0605d; font-size: 13px; min-height: 18px; margin-top: 12px; }
</style></head><body>
<form id="f" autocomplete="on">
  <h1>Health snapshot</h1>
  <p>This page is encrypted. Enter the password to view it on this device.</p>
  <input type="text" name="username" value="health-snapshot" autocomplete="username" hidden>
  <input type="password" id="pw" autocomplete="current-password" aria-label="Password" required autofocus>
  <label class="r"><input type="checkbox" id="rm" checked> Remember on this device</label>
  <button id="go">Unlock</button>
  <div class="err" id="err" role="alert"></div>
</form>
<script>
const P = __PAYLOAD__;
const KEY_ID = "gd-key-" + P.salt.slice(0, 12);
const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
const store = { get: k => { try { return localStorage.getItem(k); } catch (e) { return null; } },
                set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} },
                del: k => { try { localStorage.removeItem(k); } catch (e) {} } };
async function derive(pw) {
  const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(pw), "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey({ name: "PBKDF2", salt: b64(P.salt), iterations: P.iter, hash: "SHA-256" },
                                 base, { name: "AES-GCM", length: 256 }, true, ["decrypt"]);
}
async function show(key) {
  const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(P.iv) }, key, b64(P.ct));
  const html = await new Response(new Blob([pt]).stream().pipeThrough(new DecompressionStream("gzip"))).text();
  document.open(); document.write(html); document.close();
}
(async () => {
  const saved = store.get(KEY_ID);
  if (saved) {
    try {
      const key = await crypto.subtle.importKey("raw", b64(saved), "AES-GCM", false, ["decrypt"]);
      return await show(key);
    } catch (e) { store.del(KEY_ID); }
  }
})();
document.getElementById("f").onsubmit = async ev => {
  ev.preventDefault();
  const btn = document.getElementById("go"), err = document.getElementById("err");
  btn.disabled = true; err.textContent = ""; btn.textContent = "Unlocking…";
  try {
    const key = await derive(document.getElementById("pw").value);
    if (document.getElementById("rm").checked) {
      const raw = new Uint8Array(await crypto.subtle.exportKey("raw", key));
      await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(P.iv) }, key, b64(P.ct));  // only remember a key that works
      store.set(KEY_ID, btoa(String.fromCharCode(...raw)));
    }
    await show(key);
  } catch (e) {
    err.textContent = "Wrong password."; btn.disabled = false; btn.textContent = "Unlock";
  }
};
</script></body></html>
"""


def _salt() -> bytes:
    """Stable per-install salt, so a key remembered on a device keeps working across daily builds."""
    if SALT_FILE.exists():
        return base64.b64decode(SALT_FILE.read_text().strip())
    salt = secrets.token_bytes(16)
    SALT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SALT_FILE.write_text(base64.b64encode(salt).decode())
    os.chmod(SALT_FILE, 0o600)
    return salt


def encrypt_page(page_html: str, password: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    salt = salt or _salt()
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(password.encode())
    iv = secrets.token_bytes(12)  # fresh nonce on every build
    ct = AESGCM(key).encrypt(iv, gzip.compress(page_html.encode(), 9), None)
    e = lambda b: base64.b64encode(b).decode()  # noqa: E731
    payload = json.dumps({"v": 1, "iter": iterations, "salt": e(salt), "iv": e(iv), "ct": e(ct)})
    return LOCK_PAGE.replace("__PAYLOAD__", payload)


def password() -> str:
    pw = os.getenv("SNAPSHOT_PASSWORD", "")
    if not pw:
        raise SystemExit("SNAPSHOT_PASSWORD is not set in .env — add a long passphrase (5+ random words).")
    if len(pw) < MIN_PASSWORD_LEN:
        raise SystemExit(f"SNAPSHOT_PASSWORD is too short ({len(pw)} chars); use at least {MIN_PASSWORD_LEN}. "
                         "The encrypted page is public, so a weak password can be brute-forced offline.")
    return pw


# ---------------------------------------------------------------- publish
def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def publish(out_dir: Path, branch: str = "gh-pages") -> str:
    remote = _git("remote", "get-url", "origin", cwd=config.PROJECT_ROOT)
    name = _git("config", "user.name", cwd=config.PROJECT_ROOT) or "snapshot"
    email = _git("config", "user.email", cwd=config.PROJECT_ROOT) or "snapshot@localhost"
    gh = shutil.which("gh") or "/opt/homebrew/bin/gh"
    cred = ["-c", "credential.helper=", "-c", f"credential.helper=!'{gh}' auth git-credential"] if Path(gh).exists() else []
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        for f in ("index.html", ".nojekyll", "robots.txt"):
            shutil.copy2(out_dir / f, t / f)
        _git("init", "-q", "-b", branch, cwd=t)
        _git("add", "-A", cwd=t)
        _git("-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-m",
             f"Encrypted snapshot {datetime.now():%Y-%m-%d %H:%M}", cwd=t)
        _git(*cred, "push", "-q", "--force", remote, f"HEAD:{branch}", cwd=t)
    return remote


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the encrypted static snapshot for GitHub Pages.")
    ap.add_argument("--publish", action="store_true", help="force-push the snapshot to the gh-pages branch")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--plain", action="store_true", help="also write an UNENCRYPTED preview.html (local only)")
    a = ap.parse_args(argv)

    pw = password()
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=30)
    page = build_html(conn)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "index.html").write_text(encrypt_page(page, pw))
    (a.out / ".nojekyll").write_text("")
    (a.out / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    if a.plain:
        (a.out / "preview.html").write_text(page)  # never published: publish() copies an explicit file list
    size = (a.out / "index.html").stat().st_size
    print(f"[snapshot] built {a.out / 'index.html'} ({size / 1024:.0f} KB, encrypted)")
    if a.publish:
        remote = publish(a.out)
        print(f"[snapshot] pushed to gh-pages on {remote}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
