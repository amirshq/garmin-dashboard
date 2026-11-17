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
import html
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
PAGE_CSS = f"""
<style>
body {{ margin: 0; color: {ui.TEXT}; font-family: {ui.FONT}; -webkit-text-size-adjust: 100%; }}
.wrap {{ max-width: 1480px; margin: 0 auto; padding: 18px 24px 48px; }}
.top {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }}
.rng {{ display: flex; gap: 6px; }}
.rng button, .lock {{ background: transparent; border: 1px solid {ui.LINE}; color: {ui.TEXT_2}; font-family: {ui.MONO};
    font-size: 12px; padding: 6px 12px; border-radius: 7px; cursor: pointer; }}
.rng button[aria-pressed="true"] {{ background: {ui.ACCENT_BG}; color: {ui.ACCENT}; border-color: {ui.ACCENT}; }}
.grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 12px; }}
.card {{ background: {ui.CARD}; border: 1px solid {ui.LINE}; border-radius: 10px; padding: 14px 16px; min-width: 0; }}
.card .chart {{ margin-top: 10px; }}
.wide {{ grid-column: 1 / -1; }}
.scroll {{ overflow-x: auto; margin-top: 6px; }}
.foot {{ margin-top: 18px; font-family: {ui.MONO}; font-size: 11px; color: {ui.FAINT}; }}
@media (max-width: 760px) {{
  .wrap {{ padding: 12px 14px 40px; }}
  .grid {{ grid-template-columns: 1fr; }}
}}
</style>
"""


def build_html(conn: sqlite3.Connection, today: date | None = None) -> str:
    today = today or date.today()
    end_s = conn.execute("SELECT max(calendar_date) FROM daily_summary").fetchone()[0]
    if not end_s:
        raise SystemExit("No data in the database yet — run `make sync` first.")
    end = date.fromisoformat(end_s)
    start = end - timedelta(days=DAYS - 1)
    lead = (start - timedelta(days=13)).isoformat()  # extra days so 7-day averages and deltas are valid
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

    all_types = [r[0] for r in conn.execute("SELECT type_key FROM activity GROUP BY 1 ORDER BY count(*) DESC LIMIT 5")]
    colors = {t: ui.SERIES[i] for i, t in enumerate(all_types)}  # fixed order over all history, never by range rank

    # -- KPIs (latest value, 7-day avg vs the 7 days before)
    def kpi(label, s, unit, better, fmt=".0f", color=ui.ACCENT, value_fmt=None, spark_n=30):
        cur, prev = _avg(s, 7), _avg(s, 7, 7)
        foot, cls = _delta(cur, prev, f" {unit}".rstrip(), better, fmt)
        v = _last(s)
        value = value_fmt(v) if value_fmt and v is not None else ui.num(v, fmt)
        return ui.Kpi(label, value, unit if not value_fmt else "", foot, cls, s.dropna().tail(spark_n).tolist(), color)

    acwr = _last(load["acwr"]) if len(load) else None
    hrv_status = (_last(hv["status"]) or "").lower()
    kpis = [
        kpi("Resting HR", ds["rhr_bpm"], "bpm", "down", color=ui.SERIES[1]),
        kpi("HRV · last night", hv["last_night_avg_ms"], "ms", "up", color=ui.SERIES[0]),
        kpi("Sleep", sl["sleep_s"] / 3600, "", "up", fmt=".1f", color=ui.SERIES[5], value_fmt=ui.hm),
        kpi("Sleep score", sl["sleep_score"], "", "up", color=ui.SERIES[5]),
        kpi("Steps", ds["steps"], "", "up", fmt=",.0f", color=ui.SERIES[0]),
        kpi("Body Battery peak", ds["bb_max"], "", "up", color=ui.SERIES[2]),
        kpi("Stress avg", ds["stress_avg"], "", "down", color=ui.SERIES[3]),
        ui.Kpi("Load ratio", ui.num(acwr, ".2f"), "×", "acute 7d ÷ chronic weekly · 0.8–1.3 is steady",
               "c-muted", load["acwr"].dropna().tail(30).tolist() if len(load) else None, ui.SERIES[1],
               "warn" if acwr and acwr >= 1.5 else ""),
    ]
    if hrv_status:
        kpis[1].foot = f"status {ui.esc(hrv_status)} · " + kpis[1].foot

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
    body_figs = []
    if len(w) >= 2:
        figs["weight"] = fig_weight(w)
        body_figs.append(("weight", "Weight", f"{len(w)} weigh-ins", ""))
    if len(fa) >= 2:
        figs["fa"] = fig_fitness_age(fa)
        body_figs.append(("fa", "Fitness Age", "vs actual age", ""))
    date_figs = [k for k in figs if k not in ("weight", "fa")]  # these keep their full history

    for k in date_figs:
        figs[k].update_xaxes(range=[(end - timedelta(days=DEFAULT_RANGE - 1)).isoformat(),
                                    (end + timedelta(hours=12)).isoformat()])

    def card(key, title, sub, cls):
        return (f"<section class='card {cls}'>{ui.panel_header(title, sub)}"
                f"<div class='chart' id='c-{key}'></div></section>")

    fig_json = {k: json.loads(f.to_json()) for k, f in figs.items()}
    built = datetime.now().strftime("%Y-%m-%d %H:%M")
    ranges = "".join(f"<button data-days='{d}' aria-pressed='{str(d == DEFAULT_RANGE).lower()}'>{label}</button>"
                     for d, label in ((30, "30D"), (90, "90D"), (365, "1Y")))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Health snapshot</title>
{ui.CSS}{PAGE_CSS}
<script src="{PLOTLY_JS}"></script>
</head><body><div class="wrap">
<div class="top">
  {ui.page_header("Health snapshot", f"data through {E} · built {built}")}
  <div class="rng">{ranges}<button class="lock" id="lock" title="Forget the password on this device">Lock</button></div>
</div>
{ui.alert_banner(alerts, "Signals")}
{ui.kpi_grid(kpis)}
<div class="grid">
{''.join(card(*p) for p in panels)}
{''.join(card(*p) for p in body_figs)}
<section class="card wide">{ui.panel_header("Recent workouts", f"{len(acts)} in the last year")}
  <div class="scroll">{workouts_table(acts, colors)}</div></section>
<section class="card wide">{ui.panel_header("Recent nights", "sleep, HRV and resting HR")}
  <div class="scroll">{nights_table(vis(sl), hv, ds)}</div></section>
</div>
<div class="foot">Static snapshot of the local Garmin dashboard · regenerated after each daily sync · not medical advice</div>
</div>
<script>
const FIGS = {json.dumps(fig_json, separators=(",", ":"))};
const DATE_FIGS = {json.dumps(date_figs)};
const END = "{E}";
const CFG = {{displayModeBar: false, responsive: true}};
for (const [k, f] of Object.entries(FIGS)) Plotly.newPlot("c-" + k, f.data, f.layout, CFG);
function setRange(days) {{
  const e = new Date(END + "T12:00:00"), s = new Date(e);
  s.setDate(s.getDate() - days + 1); s.setHours(0);
  const iso = d => d.toISOString().slice(0, 13).replace("T", " ") + ":00";
  for (const k of DATE_FIGS) Plotly.relayout("c-" + k, {{"xaxis.range": [iso(s), iso(e)]}});
  document.querySelectorAll(".rng button[data-days]").forEach(b =>
    b.setAttribute("aria-pressed", String(+b.dataset.days === days)));
}}
document.querySelectorAll(".rng button[data-days]").forEach(b => b.onclick = () => setRange(+b.dataset.days));
document.getElementById("lock").onclick = () => {{
  try {{ Object.keys(localStorage).filter(k => k.startsWith("gd-key")).forEach(k => localStorage.removeItem(k)); }} catch (e) {{}}
  location.reload();
}};
</script>
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
