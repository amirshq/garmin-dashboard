"""Dashboard pages. Each `render_*` takes the date-range context and draws one page."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from garmin import config

from . import data as D
from . import ui
from .ui import Kpi, esc, hm, num


def md(s: str) -> None:
    st.markdown(s, unsafe_allow_html=True)


def roll(s: pd.Series, start: str) -> pd.Series:
    r = s.rolling(7, min_periods=3).mean()
    return r[r.index >= pd.Timestamp(start)]


def vis(s: pd.Series, start: str) -> pd.Series:
    return s[s.index >= pd.Timestamp(start)]


def panel(title: str, sub: str = "", right: str = "", figure: go.Figure | None = None, body: str | None = None,
          caption: str = "", empty_msg: str = "No data in this range") -> None:
    with st.container(border=True):
        md(ui.panel_header(title, sub, right))
        if figure is not None:
            st.plotly_chart(figure, config=ui.PLOT_CFG, width="stretch")
        elif body is not None:
            md(body)
        else:
            md(ui.empty(empty_msg))
        if caption:
            md(f'<div class="cap">{esc(caption)}</div>')


def trend(ctx: D.Ctx, s: pd.Series, unit: str, fmt: str = ".0f", bars: bool = False, under: list | None = None,
          height: int = 210, color: str = ui.SERIES[0]) -> go.Figure | None:
    v = vis(s, ctx.S)
    if v.dropna().empty:
        return None
    r = roll(s, ctx.S)
    f = ui.fig(height, x_range=ctx.x_range)
    for tr in under or []:
        f.add_trace(tr)
    size = 5 if ctx.span <= 120 else 3.5 if ctx.span <= 400 else 2.5
    soft = ui.SERIES_SOFT if color == ui.SERIES[0] else color + "55"
    if bars:
        f.add_bar(x=v.index, y=v.values, name="Daily", marker=dict(color=soft, line=dict(width=0)),
                  hovertemplate=f"%{{y:{fmt}}} {unit}<extra>Daily</extra>")
    else:
        f.add_scatter(x=v.index, y=v.values, mode="markers", name="Daily", marker=dict(size=size, color=soft),
                      hovertemplate=f"%{{y:{fmt}}} {unit}<extra>Daily</extra>")
    f.add_scatter(x=r.index, y=r.values, mode="lines", name="7-day avg", connectgaps=True,
                  line=dict(width=2, color=color, shape="spline", smoothing=0.5),
                  hovertemplate=f"%{{y:{fmt}}} {unit}<extra>7-day avg</extra>")
    return f


def avg7(s: pd.Series, start: str, fmt: str, unit: str = "") -> str:
    r = roll(s, start).dropna()
    return f"7d avg <b>{format(r.iloc[-1], fmt)}</b> {esc(unit)}" if len(r) else ""


def delta_html(cur, prev, fmt: str, higher_is_better: bool | None, unit: str = "", flat: float = 0) -> tuple[str, str]:
    if cur is None or prev is None:
        return "no prior data", "c-muted"
    d = cur - prev
    if abs(d) <= flat:
        return f"→ flat vs prior", "c-muted"
    arrow = "↑" if d > 0 else "↓"
    cls = "c-muted" if higher_is_better is None else ("c-good" if (d > 0) == higher_is_better else "c-bad")
    return f"{arrow} {format(abs(d), fmt)}{unit} vs prior", cls


def local_tz() -> str:
    s = D.profile("user_settings") or {}
    return s.get("timeZone") or "UTC"


def to_local(ms_values: list, tz: str, max_gap_min: float | None = None) -> pd.DataFrame:
    """Epoch-ms pairs -> local-time frame. With max_gap_min, inserts a NaN row in gaps so lines break
    instead of drawing a straight segment across missing data (e.g. watch off the wrist)."""
    if not ms_values:
        return pd.DataFrame(columns=["t", "v"])
    df = pd.DataFrame(ms_values, columns=["t", "v"]).sort_values("t")
    if max_gap_min:
        gaps = df.t.diff() > max_gap_min * 60000
        if gaps.any():
            fill = pd.DataFrame({"t": df.t[gaps] - 1, "v": float("nan")})
            df = pd.concat([df, fill]).sort_values("t")
    df["t"] = pd.to_datetime(df.t, unit="ms", utc=True).dt.tz_convert(tz).dt.tz_localize(None)
    return df


@st.cache_data(ttl=600, show_spinner=False)
def type_colors() -> dict[str, str]:
    """Color follows the entity: top-5 activity types over ALL history get fixed slots."""
    top = D.q("SELECT type_key FROM activity GROUP BY 1 ORDER BY count(*) DESC LIMIT 5").type_key.tolist()
    return {t: ui.SERIES[i] for i, t in enumerate(top)}


def type_color(t: str) -> str:
    return type_colors().get(t, ui.FAINT)


# ======================================================================= OVERVIEW
def render_overview(ctx: D.Ctx) -> None:
    hi = ctx.data_hi
    alerts = D.alerts(date.today().isoformat())

    last_sleep = D.q("SELECT * FROM sleep WHERE sleep_s IS NOT NULL ORDER BY calendar_date DESC LIMIT 14")
    hrv = D.q("SELECT * FROM hrv ORDER BY calendar_date DESC LIMIT 14")
    daily = D.q("SELECT * FROM daily_summary ORDER BY calendar_date DESC LIMIT 35")
    ls = last_sleep.iloc[0] if len(last_sleep) else None
    lh = hrv.iloc[0] if len(hrv) else None
    ld = daily.iloc[0] if len(daily) else None
    today_acts = D.q("SELECT count(*) n, sum(duration_s) d FROM activity WHERE calendar_date=?", (hi.isoformat(),)).iloc[0]

    # daily brief (purple note, like the template's notes box)
    bits = []
    if ls is not None:
        bits.append(f"<b>Last night</b> {hm(ls.sleep_s / 3600)} sleep, score <code>{num(ls.sleep_score)}</code>")
    if lh is not None:
        bits.append(f"HRV <code>{num(lh.last_night_avg_ms)} ms</code> ({esc(str(lh.status or '').lower())})")
    if ld is not None:
        bits.append(f"resting HR <code>{num(ld.rhr_bpm)} bpm</code>, Body Battery peaked at <code>{num(ld.bb_max)}</code>")
    if today_acts.n:
        bits.append(f"{int(today_acts.n)} workout{'s' if today_acts.n > 1 else ''} today ({hm(today_acts.d / 3600)})")
    md(ui.note(" · ".join(bits) + ". Alerts below are recomputed from your data on every load; sources: "
               "<code>daily_summary · sleep · hrv · activity</code>."))
    md(ui.alert_banner(alerts, "Signals"))

    # KPIs
    ts = lambda df, c: df.sort_values("calendar_date")[c].tolist() if len(df) else []  # noqa: E731
    rhr30 = daily.rhr_bpm.dropna()
    rhr_base = float(rhr30.iloc[3:].median()) if len(rhr30) > 10 else None
    rhr3 = float(rhr30.iloc[:3].mean()) if len(rhr30) >= 3 else None
    load = D.load_series(hi.isoformat(), 60)
    lt = load.iloc[-1] if len(load) else None
    acwr = lt.acwr if lt is not None else None
    acwr_cls = "c-muted" if acwr is None or pd.isna(acwr) else ("c-bad" if acwr >= 1.5 else "c-warn" if acwr >= 1.3 or acwr < 0.6 else "c-good")
    hrv_status = str(lh.status or "").upper() if lh is not None else ""
    md(ui.kpi_grid([
        Kpi("Sleep · last night", hm(ls.sleep_s / 3600) if ls is not None else "—", "",
            f"score {num(ls.sleep_score) if ls is not None else '—'} · 14-night trend",
            spark=[v / 3600 for v in ts(last_sleep, "sleep_s")]),
        Kpi("HRV · 7d avg", num(lh.weekly_avg_ms) if lh is not None else "—", "ms",
            f"balanced {num(lh.baseline_balanced_low_ms)}–{num(lh.baseline_balanced_upper_ms)} · {hrv_status.lower()}"
            if lh is not None else "", "c-good" if hrv_status == "BALANCED" else "c-warn",
            spark=ts(hrv, "last_night_avg_ms"), highlight="" if hrv_status in ("BALANCED", "") else "warn"),
        Kpi("Resting HR · 3d", num(rhr3), "bpm",
            f"30d median {num(rhr_base)} · {'+' if (rhr3 or 0) >= (rhr_base or 0) else ''}{num((rhr3 or 0) - (rhr_base or 0), '.0f')} bpm",
            "c-bad" if rhr3 and rhr_base and rhr3 - rhr_base >= 3 else "c-muted",
            spark=ts(daily.head(30), "rhr_bpm"), spark_color=ui.WARN if rhr3 and rhr_base and rhr3 - rhr_base >= 3 else ui.ACCENT,
            highlight="warn" if rhr3 and rhr_base and rhr3 - rhr_base >= 3 else "",
            value_cls="c-warn" if rhr3 and rhr_base and rhr3 - rhr_base >= 3 else ""),
        Kpi("Body Battery · peak", num(ld.bb_max) if ld is not None else "—", "/100",
            f"+{num(ld.bb_charged)} charged · −{num(ld.bb_drained)} drained" if ld is not None else "",
            spark=ts(daily.head(14), "bb_max"), spark_color=ui.GOOD),
        Kpi("Training load · 7d", num(lt.acute_7d) if lt is not None else "—", "",
            f"ratio {num(acwr, '.2f')} vs 4-wk avg {num(lt.chronic_weekly) if lt is not None else '—'}/wk",
            acwr_cls, spark=load.acute_7d.tail(30).tolist() if len(load) else None),
    ]))

    left, right = st.columns([2.05, 1], gap="medium")
    with left:
        _today_panel()
        _workouts_panel(ctx, compact=True)
    with right:
        _last_night_card(ls)
        _zones_card(ctx)
        _body_card()


def _today_panel() -> None:
    days = D.q("SELECT calendar_date FROM hr_intraday ORDER BY calendar_date DESC LIMIT 90").calendar_date.tolist()
    if not days:
        panel("Today", "intraday", body=ui.empty("No intraday data yet — run a sync"))
        return
    tz = local_tz()
    with st.container(border=True):
        c1, c2 = st.columns([3, 1.3], vertical_alignment="center")
        with c2:
            day = st.selectbox("Day", days, index=0, label_visibility="collapsed", key="intraday_day",
                               format_func=lambda d: date.fromisoformat(d).strftime("%a %-d %b %Y"))
        hr = D.q("SELECT * FROM hr_intraday WHERE calendar_date=?", (day,))
        bb = D.q("SELECT * FROM body_battery_day WHERE calendar_date=?", (day,))
        acts = D.q("SELECT name, type_key, start_local, duration_s FROM activity WHERE calendar_date=?", (day,))
        h = hr.iloc[0] if len(hr) else None
        with c1:
            md(ui.panel_header("Day timeline", "heart rate · Body Battery",
                               f"rest <b>{num(h.resting_bpm) if h is not None else '—'}</b> · min {num(h.min_bpm) if h is not None else '—'}"
                               f" · max <b>{num(h.max_bpm) if h is not None else '—'}</b> bpm"))
        x0 = pd.Timestamp(day)
        rng = (x0.isoformat(), (x0 + pd.Timedelta(hours=24)).isoformat())
        if h is not None:
            df = to_local(json.loads(h.values_json or "[]"), tz, max_gap_min=15)
            f = ui.fig(170, x_range=rng, date_axis=False)
            f.update_xaxes(tickformat="%H:%M", hoverformat="%H:%M")
            for _, a in acts.iterrows():  # workout spans
                s0 = pd.Timestamp(a.start_local)
                f.add_vrect(x0=s0, x1=s0 + pd.Timedelta(seconds=a.duration_s or 0), fillcolor=ui.ACCENT,
                            opacity=0.10, line_width=0, annotation_text=esc(D.pretty_type(a.type_key)),
                            annotation_position="top left", annotation_font=dict(size=10, color=ui.ACCENT))
            f.add_scatter(x=df.t, y=df.v, mode="lines", line=dict(width=1.4, color=ui.BAD), name="HR",
                          hovertemplate="%{y:.0f} bpm<extra>Heart rate</extra>")
            if h.resting_bpm:
                f.add_hline(y=h.resting_bpm, line=dict(color=ui.FAINT, width=1, dash="dot"))
            st.plotly_chart(f, config=ui.PLOT_CFG, width="stretch")
        if len(bb):
            b = bb.iloc[0]
            df = to_local(json.loads(b.values_json or "[]"), tz)
            f = ui.fig(120, x_range=rng, date_axis=False)
            f.update_xaxes(tickformat="%H:%M", hoverformat="%H:%M")
            f.update_yaxes(range=[0, 100], nticks=3)
            f.add_scatter(x=df.t, y=df.v, mode="lines", line=dict(width=1.6, color=ui.GOOD, shape="spline", smoothing=0.4),
                          fill="tozeroy", fillcolor="rgba(62,207,142,0.08)", name="Body Battery",
                          hovertemplate="%{y:.0f}<extra>Body Battery</extra>")
            st.plotly_chart(f, config=ui.PLOT_CFG, width="stretch")
            ev = json.loads(b.events_json or "[]")
            chips = " · ".join(f"<span class='mono'>{esc((e.get('type') or '').lower())}</span> "
                               f"<b class='{'c-good' if (e.get('impact') or 0) > 0 else 'c-bad'}'>"
                               f"{'+' if (e.get('impact') or 0) > 0 else ''}{num(e.get('impact'))}</b>"
                               f" <span class='c-muted'>({hm((e.get('duration_min') or 0) / 60)})</span>"
                               for e in ev[:6])
            md(f'<div class="cap" style="margin-top:-4px">Body Battery +{num(b.charged)} / −{num(b.drained)}'
               f'{" · " + chips if chips else ""}</div>')


def txt(v) -> str:
    """NaN/None-safe text from a pandas row value."""
    return v if isinstance(v, str) else ""


def _te_tag(label: str | None) -> str:
    label = txt(label)
    if not label:
        return '<span class="tag">—</span>'
    short = {"AEROBIC_BASE": "base", "RECOVERY": "recovery", "TEMPO": "tempo", "LACTATE_THRESHOLD": "threshold",
             "VO2MAX": "vo2max", "ANAEROBIC_CAPACITY": "anaerobic", "SPEED": "speed"}.get(label, label.lower())
    cls = "y" if label in ("VO2MAX", "ANAEROBIC_CAPACITY", "LACTATE_THRESHOLD", "SPEED") else ""
    return f'<span class="tag {cls}">{esc(short)}</span>'


def workouts_table(df: pd.DataFrame, rng_key: str, max_rows: int = 200) -> str:
    if not len(df):
        return ui.empty("No workouts in this range")
    max_d = float(df.duration_s.max() or 1)
    p90 = float(df.training_load.quantile(0.9)) if df.training_load.notna().any() else 1e9
    rows = []
    for _, a in df.head(max_rows).iterrows():
        start = datetime.fromisoformat(a.start_local) if a.start_local else None
        end = start + timedelta(seconds=a.duration_s or 0) if start else None
        color = type_color(a.type_key)
        high = (a.training_load or 0) >= p90 and (a.training_load or 0) > 0
        sub = " · ".join(x for x in [esc(txt(a.get("location"))), f"#{a.activity_id}"] if x)
        dist = f"{a.distance_m / 1000:.2f} km" if a.distance_m and a.distance_m > 0 else "—"
        hr_txt = f"{a.avg_hr_bpm:.0f}" if pd.notna(a.avg_hr_bpm) else "—"
        zones = ""
        z = [a.get(f"hr_z{i}_s") or 0 for i in range(1, 6)]
        if sum(z) > 0:
            zones = '<div class="zones" style="height:4px;margin-top:6px">' + "".join(
                f'<i style="flex:{v};background:{ZONE_COLORS[i]}"></i>' for i, v in enumerate(z) if v) + "</div>"
        rows.append(
            f'<tr class="{"flag-w" if high else ""}">'
            f'<td><a href="?page=training&range={rng_key}&activity={a.activity_id}" target="_self">'
            f'{esc(a["name"] or "Workout")}</a><div class="sub">{sub}</div></td>'
            f'<td>{ui.pill(D.pretty_type(a.type_key), color)}</td>'
            f'<td class="hide-sm" style="min-width:150px"><div class="tl"><span>{start:%a %-d %b}</span><span>{hm((a.duration_s or 0) / 3600)}</span></div>'
            f'{ui.bar((a.duration_s or 0) / max_d, color)}<div class="sub">{start:%H:%M} → {end:%H:%M}</div></td>'
            f'<td class="num">{dist}</td><td class="num hide-sm">{hr_txt}<div class="sub">max {num(a.max_hr_bpm)}</div>{zones}</td>'
            f'<td class="num">{num(a.training_load)}</td>'
            f'<td class="hide-sm">{_te_tag(a.get("te_label"))}<div class="sub">TE {num(a.aerobic_te, ".1f")} · '
            f'{num(a.anaerobic_te, ".1f")}</div></td></tr>')
    return ('<table class="tbl"><thead><tr><th>Workout</th><th>Type</th><th class="hide-sm">Timeline</th>'
            '<th class="num">Dist</th><th class="num hide-sm">Avg HR</th><th class="num">Load</th>'
            f'<th class="hide-sm">Effect</th></tr></thead><tbody>{"".join(rows)}</tbody></table>')


ZONE_COLORS = ["#1f3b5c", "#22598f", "#2f78c4", "#4f9be6", "#8cc2f5"]  # ordinal: one hue, dim -> bright


def _workouts_panel(ctx: D.Ctx, compact: bool) -> None:
    df = D.q("SELECT * FROM activity_ext WHERE calendar_date BETWEEN ? AND ? ORDER BY start_local DESC",
             (ctx.S, ctx.E))
    with st.container(border=True):
        c1, c2, c3 = st.columns([0.9, 3.6, 1.3], vertical_alignment="center")
        with c1:
            md(ui.panel_header("Workouts"))
        types = df.type_key.value_counts().index.tolist()[:4]
        with c2:
            sel = st.pills("Filter", ["All"] + types, default="All", key=f"wf_{compact}",
                           format_func=lambda t: t if t == "All" else D.pretty_type(t), label_visibility="collapsed")
        if sel and sel != "All":
            df = df[df.type_key == sel]
        with c3:
            md(f'<div class="panel-h"><span></span><span class="r">{len(df)} workouts · '
               f'{df.duration_s.sum() / 3600:,.1f} h</span></div>')
        limit = 10 if compact else (len(df) if st.session_state.get("wf_all") else 25)
        md(workouts_table(df, st.session_state.get("range", "30d"), max_rows=limit))
        if not compact and len(df) > 25:
            st.toggle(f"Show all {len(df)} workouts", key="wf_all")
        if compact and len(df) > 10:
            md(f'<div class="cap" style="margin-top:4px">Showing 10 of {len(df)} · open '
               f'<a href="?page=training&range={st.session_state.get("range", "30d")}" target="_self" '
               f'style="color:{ui.ACCENT}">Training</a> for all</div>')


def _last_night_card(ls) -> None:
    if ls is None:
        return
    tz = local_tz()
    tot = sum((ls[c] or 0) for c, _, _ in ui.STAGES) or 1
    seg = "".join(f'<i style="flex:{ls[c] or 0};background:{col}"></i>' for c, _, col in ui.STAGES if ls[c])
    try:
        s0 = pd.Timestamp(ls.start_gmt, tz="UTC").tz_convert(tz)
        s1 = pd.Timestamp(ls.end_gmt, tz="UTC").tz_convert(tz)
        window = f"{s0:%H:%M} → {s1:%H:%M}"
    except Exception:  # noqa: BLE001
        window = ""
    stage_rows = "".join(
        f'<div class="kv"><span><i class="dot" style="background:{col}"></i>{n}</span>'
        f'<b>{hm((ls[c] or 0) / 3600)} <span class="c-muted" style="font-weight:400">'
        f'{(ls[c] or 0) / tot * 100:.0f}%</span></b></div>' for c, n, col in ui.STAGES)
    with st.container(border=True):
        md(ui.panel_header("Last night", "", f"{date.fromisoformat(ls.calendar_date):%a %-d %b}"))
        md(f'<div class="prof"><div style="display:flex;justify-content:space-between;align-items:baseline">'
           f'<span class="t" style="font-size:22px">{hm(ls.sleep_s / 3600)}</span>'
           f'<span class="tag g">score {num(ls.sleep_score)}</span></div>'
           f'<div class="s mono">{window} · {num(ls.awake_count)} awakenings · {num(ls.avg_hr_bpm)} bpm avg</div>'
           f'<div class="zones" style="height:10px;margin-top:12px">{seg}</div>{stage_rows}</div>')


def _zones_card(ctx: D.Ctx) -> None:
    zones = D.profile("hr_zones") or []
    z = zones[0] if zones else {}
    t = D.q("SELECT sum(hr_z1_s) z1, sum(hr_z2_s) z2, sum(hr_z3_s) z3, sum(hr_z4_s) z4, sum(hr_z5_s) z5 "
            "FROM activity_ext WHERE calendar_date BETWEEN ? AND ?", (ctx.S, ctx.E)).iloc[0]
    tot = float(sum((t[f"z{i}"] or 0) for i in range(1, 6))) or 0
    floors = [z.get(f"zone{i}Floor") for i in range(1, 6)]
    ceil = floors[1:] + [z.get("maxHeartRateUsed")]
    rows = "".join(
        f'<div class="it"><span class="q" style="color:{ui.TEXT_2}"><i class="dot" style="background:{ZONE_COLORS[i]};border-radius:2px"></i>Z{i + 1}</span>'
        f'<span class="n">{num(floors[i])}–{num(ceil[i])} <small>bpm</small></span>'
        f'<span class="r mono">{hm((t[f"z{i + 1}"] or 0) / 3600)} · {((t[f"z{i + 1}"] or 0) / tot * 100) if tot else 0:.0f}%</span></div>'
        for i in range(5))
    bar_ = "".join(f'<i style="flex:{t[f"z{i + 1}"] or 0};background:{ZONE_COLORS[i]}"></i>' for i in range(5)) if tot else ""
    with st.container(border=True):
        md(ui.panel_header("Heart-rate zones", "", f"{ctx.span}d · {hm(tot / 3600)}"))
        md(f'<div class="zones">{bar_}</div><div class="list">{rows}</div>'
           f'<div class="cap" style="margin-top:6px">Zones from Garmin ({esc((z.get("trainingMethod") or "").replace("_", " ").lower())}), '
           f'resting {num(z.get("restingHeartRateUsed"))} · max {num(z.get("maxHeartRateUsed"))} bpm</div>')


def _body_card() -> None:
    fa = D.q("SELECT * FROM fitness_age ORDER BY as_of_date DESC LIMIT 1")
    w = D.q("SELECT * FROM weight ORDER BY timestamp_gmt DESC LIMIT 2")
    spo2 = D.q("SELECT avg_pct, avg_sleep_pct FROM spo2_day ORDER BY calendar_date DESC LIMIT 7")
    items = []
    if len(fa):
        f = fa.iloc[0]
        items.append(("Fitness age", f"{f.fitness_age:.1f}", f"actual {num(f.chronological_age)} · achievable {num(f.achievable_fitness_age, '.1f')}"))
    if len(w):
        x = w.iloc[0]
        d = f" · {x.weight_kg - w.iloc[1].weight_kg:+.1f}" if len(w) > 1 else ""
        items.append(("Weight", f"{x.weight_kg:.1f}", f"kg · BMI {num(x.bmi, '.1f')}{d} · {x.calendar_date}"))
    if len(spo2):
        items.append(("SpO₂ 7d", f"{spo2.avg_pct.mean():.0f}%", f"sleep {spo2.avg_sleep_pct.mean():.0f}%"))
    s = D.profile("user_settings") or {}
    rows = "".join(f'<div class="it"><span class="q">{esc(v)}</span><span class="n">{esc(k)}</span>'
                   f'<span class="r">{esc(sub)}</span></div>' for k, v, sub in items)
    with st.container(border=True):
        md(ui.panel_header("Body", "", f'<a href="?page=body" target="_self" style="color:{ui.MUTED}">details →</a>'))
        md(f'<div class="list">{rows or ui.empty("No body data")}</div>'
           f'<div class="cap" style="margin-top:6px">Timezone {esc(s.get("timeZone") or "—")}</div>')


# ======================================================================= RECOVERY
def render_recovery(ctx: D.Ctx) -> None:
    daily = D.table("daily_summary", ctx.lead, ctx.E)
    hrv = D.table("hrv", ctx.lead, ctx.E)
    sp = D.table("spo2_day", ctx.lead, ctx.E)
    pa, pb = ctx.prev
    p_daily, p_hrv = D.table("daily_summary", pa, pb), D.table("hrv", pa, pb)
    S = lambda df, c: D.series(df, c, ctx.lead, ctx.E)  # noqa: E731

    k = []
    for lbl, df, pdf, col, unit, fmt, hib, fl in (
            ("HRV · avg", hrv, p_hrv, "last_night_avg_ms", " ms", ".1f", True, .5),
            ("Resting HR · avg", daily, p_daily, "rhr_bpm", " bpm", ".1f", False, .4),
            ("Stress · avg", daily, p_daily, "stress_avg", "", ".1f", False, .5),
            ("Body Battery · peak", daily, p_daily, "bb_max", "", ".1f", True, .5),
            ("Respiration · avg", daily, p_daily, "resp_avg_brpm", " brpm", ".1f", None, .1)):
        cur, prev = D.mean(df, col, ctx.S), D.mean(pdf, col)
        txt, cls = delta_html(cur, prev, fmt, hib, unit, fl)
        k.append(Kpi(lbl, num(cur, ".0f" if col != "resp_avg_brpm" else ".1f"), unit.strip(), txt, cls,
                     spark=roll(S(df, col), ctx.S).dropna().tolist()))
    k.append(Kpi("SpO₂ · avg", num(D.mean(sp, "avg_pct", ctx.S), ".1f"), "%",
                 f"sleep {num(D.mean(sp, 'avg_sleep_pct', ctx.S), '.1f')}% · last 90d only",
                 spark=roll(S(sp, "avg_pct"), ctx.S).dropna().tolist()))
    md(ui.kpi_grid(k))

    c1, c2 = st.columns(2, gap="medium")
    with c1:
        lo, hi_ = vis(S(hrv, "baseline_balanced_low_ms"), ctx.S), vis(S(hrv, "baseline_balanced_upper_ms"), ctx.S)
        band = [go.Scatter(x=hi_.index, y=hi_.values, mode="lines", line=dict(width=0, shape="hv"), hoverinfo="skip",
                           showlegend=False, connectgaps=True),
                go.Scatter(x=lo.index, y=lo.values, mode="lines", line=dict(width=0, shape="hv"), fill="tonexty",
                           fillcolor="rgba(62,207,142,0.08)", hoverinfo="skip", showlegend=False, connectgaps=True)]
        s = S(hrv, "last_night_avg_ms")
        panel("HRV", "overnight RMSSD · ms", avg7(s, ctx.S, ".0f", "ms"), trend(ctx, s, "ms", under=band),
              caption="Green band: your Garmin balanced baseline.")
    with c2:
        s = S(daily, "rhr_bpm")
        panel("Resting heart rate", "bpm", avg7(s, ctx.S, ".0f", "bpm"), trend(ctx, s, "bpm"))
    c1, c2 = st.columns(2, gap="medium")
    with c1:
        s = S(daily, "stress_avg")
        f = trend(ctx, s, "", color=ui.SERIES[3])
        if f is not None:
            for y0, y1, lab in ((0, 25, "rest"), (26, 50, "low"), (51, 75, "medium")):
                f.add_hrect(y0=y0, y1=y1, line_width=0, fillcolor="rgba(255,255,255,0.012)" if y0 % 2 else "rgba(0,0,0,0)")
        panel("Stress", "daily average · 0–100", avg7(s, ctx.S, ".0f"), f)
    with c2:
        bmin, bmax = vis(S(daily, "bb_min"), ctx.S), vis(S(daily, "bb_max"), ctx.S)
        f = None
        if bmax.notna().any():
            f = ui.fig(210, x_range=ctx.x_range)
            f.add_bar(x=bmax.index, y=(bmax - bmin).values, base=bmin.values, name="Range",
                      marker=dict(color="rgba(62,207,142,0.28)", line=dict(width=0)),
                      customdata=list(zip(bmin.values, bmax.values)),
                      hovertemplate="%{customdata[0]:.0f} → %{customdata[1]:.0f}<extra>Low → peak</extra>")
            f.add_scatter(x=bmax.index, y=bmax.values, mode="markers", name="Peak", hoverinfo="skip",
                          marker=dict(size=4 if ctx.span <= 120 else 2.5, color=ui.GOOD))
            f.update_yaxes(range=[0, 100])
        panel("Body Battery", "daily low → peak", avg7(S(daily, "bb_max"), ctx.S, ".0f", "peak"), f)
    c1, c2 = st.columns(2, gap="medium")
    with c1:
        s = S(sp, "avg_pct")
        f = trend(ctx, s, "%", ".1f", color=ui.SERIES[2])
        if f is not None:
            low = vis(S(sp, "lowest_pct"), ctx.S)
            f.add_scatter(x=low.index, y=low.values, mode="markers", name="Lowest",
                          marker=dict(size=4, color=ui.SERIES[4], symbol="line-ew-open", line=dict(width=1.5)),
                          hovertemplate="%{y:.0f}%<extra>Lowest</extra>")
        panel("Blood oxygen", "SpO₂ % · daily avg & lowest", avg7(s, ctx.S, ".1f", "%"), f,
              empty_msg="SpO₂ is kept for the last 90 days")
    with c2:
        s = S(daily, "resp_avg_brpm")
        panel("Respiration", "breaths / min", avg7(s, ctx.S, ".1f", "brpm"), trend(ctx, s, "brpm", ".1f", color=ui.SERIES[5]))

    # Body Battery drivers
    ev = D.q("SELECT calendar_date, events_json FROM body_battery_day WHERE calendar_date BETWEEN ? AND ?", (ctx.S, ctx.E))
    rows = []
    for _, r in ev.iterrows():
        for e in json.loads(r.events_json or "[]"):
            rows.append({"date": r.calendar_date, **e})
    if rows:
        e = pd.DataFrame(rows)
        g = e.groupby("type").agg(n=("impact", "size"), impact=("impact", "sum"), mins=("duration_min", "sum")).sort_values("impact")
        body = "".join(f'<div class="it"><span class="q {"c-good" if v.impact > 0 else "c-bad"}">{v.impact:+,.0f}</span>'
                       f'<span class="n">{esc(str(t).replace("_", " ").title())}<small>{v.n}×</small></span>'
                       f'<span class="r mono">{hm(v.mins / 60)}</span></div>' for t, v in g.iterrows())
        panel("Body Battery drivers", "events in range", "total impact", body=f'<div class="list">{body}</div>',
              caption="Garmin-detected events (sleep, activities, stress) and their Body Battery impact.")


# ======================================================================= SLEEP
def render_sleep(ctx: D.Ctx) -> None:
    sl = D.table("sleep", ctx.lead, ctx.E)
    raw = D.q("SELECT calendar_date, json_extract(raw_json,'$.sleepNeed.actual') need_min, "
              "json_extract(raw_json,'$.sleepScores.overall.qualifierKey') q FROM sleep WHERE calendar_date BETWEEN ? AND ?",
              (ctx.S, ctx.E))
    pa, pb = ctx.prev
    p = D.table("sleep", pa, pb)
    v = sl[sl.calendar_date >= ctx.S]
    tz = local_tz()
    S = lambda c, sc=1.0: D.series(sl, c, ctx.lead, ctx.E, sc)  # noqa: E731

    cur, prev = D.mean(sl, "sleep_s", ctx.S), D.mean(p, "sleep_s")
    t1, c1_ = delta_html(cur / 60 if cur else None, prev / 60 if prev else None, ".0f", True, " min", 2)
    sc, psc = D.mean(sl, "sleep_score", ctx.S), D.mean(p, "sleep_score")
    t2, c2_ = delta_html(sc, psc, ".1f", True, "", .5)
    tot = v[["deep_s", "light_s", "rem_s"]].sum().sum() or 1
    deep_pct, rem_pct = v.deep_s.sum() / tot * 100, v.rem_s.sum() / tot * 100
    need = raw.need_min.dropna()
    debt = float(need.sum() - v.sleep_s.sum() / 60) if len(v) and len(need) == len(v) else None
    starts = pd.to_datetime(v.start_gmt, utc=True, errors="coerce").dt.tz_convert(tz)
    bed_h = ((starts.dt.hour + starts.dt.minute / 60 - 12) % 24 + 12)  # hours after noon, wraps past midnight
    md(ui.kpi_grid([
        Kpi("Sleep · avg", hm(cur / 3600) if cur else "—", "", t1, c1_, spark=roll(S("sleep_s", 1 / 3600), ctx.S).dropna().tolist()),
        Kpi("Sleep score · avg", num(sc), "/100", t2, c2_, spark=roll(S("sleep_score"), ctx.S).dropna().tolist()),
        Kpi("Deep · REM", f"{deep_pct:.0f}% · {rem_pct:.0f}%", "", "of time asleep (excl. awake)"),
        Kpi("Bedtime · median", f"{int(bed_h.median()) % 24:02d}:{int(bed_h.median() % 1 * 60):02d}" if bed_h.notna().any() else "—", "",
            f"± {bed_h.std() * 60:.0f} min consistency" if bed_h.notna().sum() > 2 else ""),
        Kpi("vs sleep need", (("+" if debt < 0 else "−") + hm(abs(debt) / len(v) / 60)) if debt is not None else "—",
            "/night", f"Garmin need ~{hm(need.mean() / 60) if len(need) else '—'} · {len(v)} nights",
            "c-bad" if debt and debt / len(v) > 30 else "c-muted",
            value_cls="c-warn" if debt and debt / len(v) > 30 else ""),
    ]))

    c1, c2 = st.columns([3, 2], gap="medium")
    with c1:
        f = None
        if len(v) and v.sleep_s.notna().any():
            f = ui.fig(240, legend=True, x_range=ctx.x_range)
            x = pd.to_datetime(v.calendar_date)
            for col, name, color in ui.STAGES:
                f.add_bar(x=x, y=v[col] / 3600, name=name, marker=dict(color=color, line=dict(width=1, color=ui.CARD)),
                          hovertemplate=f"%{{y:.1f}} h<extra>{name}</extra>")
            f.update_layout(barmode="stack", bargap=0.15 if ctx.span <= 120 else 0.04, margin=dict(l=0, r=6, t=26, b=0))
            f.update_yaxes(ticksuffix="h")
        panel("Stages", "hours per night", f"avg <b>{hm(cur / 3600) if cur else '—'}</b>", f)
    with c2:
        s = S("sleep_score")
        panel("Sleep score", "0–100", avg7(s, ctx.S, ".0f"), trend(ctx, s, "", height=240, color=ui.SERIES[5]))

    c1, c2 = st.columns([3, 2], gap="medium")
    with c1:
        f = None
        if len(v):
            ends = pd.to_datetime(v.end_gmt, utc=True, errors="coerce").dt.tz_convert(tz)
            b0 = (starts.dt.hour + starts.dt.minute / 60 - 12) % 24  # hours after noon
            dur = (ends - starts).dt.total_seconds() / 3600
            f = ui.fig(240, x_range=ctx.x_range)
            f.add_bar(x=pd.to_datetime(v.calendar_date), y=dur, base=b0, name="In bed",
                      marker=dict(color="rgba(148,102,232,0.45)", line=dict(width=0)),
                      customdata=list(zip(starts.dt.strftime("%H:%M"), ends.dt.strftime("%H:%M"))),
                      hovertemplate="%{customdata[0]} → %{customdata[1]}<extra>Sleep window</extra>")
            lo_h = max(6, int(b0.min()) - 1) if b0.notna().any() else 9
            hi_h = min(24, int((b0 + dur).max()) + 1) if dur.notna().any() else 21
            ticks = list(range(lo_h + lo_h % 2, hi_h + 1, 2))
            f.update_yaxes(tickvals=ticks, ticktext=[f"{(t + 12) % 24:02d}:00" for t in ticks], range=[hi_h, lo_h])
        panel("Sleep window", "bedtime → wake, local time", f"{esc(tz)}", f)
    with c2:
        s = S("avg_hr_bpm")
        panel("Sleeping heart rate", "bpm", avg7(s, ctx.S, ".0f", "bpm"), trend(ctx, s, "bpm", height=240, color=ui.BAD))

    # nights table
    if len(v):
        rows = []
        q_map = dict(zip(raw.calendar_date, raw.q))
        best = v.sleep_score.max()
        n_show = len(v) if st.session_state.get("nights_all") else 21
        for _, n in v.sort_values("calendar_date", ascending=False).head(n_show).iterrows():
            t = sum((n[c] or 0) for c, _, _ in ui.STAGES) or 1
            seg = "".join(f'<i style="flex:{n[c] or 0};background:{col}"></i>' for c, _, col in ui.STAGES if n[c])
            qk = txt(q_map.get(n.calendar_date)).lower()
            cls = "flag" if (n.sleep_score or 100) < 60 else ""
            try:
                s0 = pd.Timestamp(n.start_gmt, tz="UTC").tz_convert(tz)
                s1 = pd.Timestamp(n.end_gmt, tz="UTC").tz_convert(tz)
                win = f"{s0:%H:%M} → {s1:%H:%M}"
            except Exception:  # noqa: BLE001
                win = "—"
            rows.append(f'<tr class="{cls}"><td>{date.fromisoformat(n.calendar_date):%a %-d %b}<div class="sub">{win}</div></td>'
                        f'<td class="num">{hm((n.sleep_s or 0) / 3600)}</td>'
                        f'<td class="num">{num(n.sleep_score)}{" ★" if n.sleep_score == best else ""}<div class="sub">{esc(qk)}</div></td>'
                        f'<td class="hide-sm" style="min-width:180px"><div class="zones" style="margin-top:6px">{seg}</div>'
                        f'<div class="sub">deep {hm((n.deep_s or 0) / 3600)} · rem {hm((n.rem_s or 0) / 3600)} · awake {hm((n.awake_s or 0) / 3600)}</div></td>'
                        f'<td class="num hide-sm">{num(n.avg_hr_bpm)}</td><td class="num hide-sm">{num(n.avg_resp_brpm, ".1f")}</td>'
                        f'<td class="num hide-sm">{num(n.avg_sleep_stress)}</td><td class="num">{num(n.awake_count)}</td></tr>')
        with st.container(border=True):
            md(ui.panel_header("Nights", f"{len(v)} in range", ui.legend([(nm, c) for _, nm, c in ui.STAGES]).replace('class="legend"', 'class="legend" style="margin:0"')))
            md('<table class="tbl"><thead><tr><th>Night</th><th class="num">Asleep</th><th class="num">Score</th>'
               '<th class="hide-sm">Stages</th><th class="num hide-sm">HR</th><th class="num hide-sm">Resp</th>'
               f'<th class="num hide-sm">Stress</th><th class="num">Wakes</th></tr></thead><tbody>{"".join(rows)}</tbody></table>')
            if len(v) > 21:
                st.toggle(f"Show all {len(v)} nights", key="nights_all")


# ======================================================================= TRAINING
def render_training(ctx: D.Ctx) -> None:
    aid = st.query_params.get("activity")
    if aid and str(aid).isdigit():
        _activity_detail(int(aid))

    acts = D.q("SELECT * FROM activity_ext WHERE calendar_date BETWEEN ? AND ? ORDER BY start_local DESC", (ctx.S, ctx.E))
    pa, pb = ctx.prev
    pacts = D.q("SELECT duration_s, distance_m, training_load FROM activity WHERE calendar_date BETWEEN ? AND ?", (pa, pb))
    load = D.load_series(ctx.E, ctx.span)
    lt = load.iloc[-1] if len(load) else None
    t_n, c_n = delta_html(len(acts), len(pacts), ".0f", None)
    t_h, c_h = delta_html(acts.duration_s.sum() / 3600, pacts.duration_s.sum() / 3600, ".1f", None, " h")
    wk = acts.groupby(pd.to_datetime(acts.calendar_date).dt.to_period("W")).duration_s.sum() / 3600 if len(acts) else pd.Series(dtype=float)
    md(ui.kpi_grid([
        Kpi("Workouts", f"{len(acts)}", "", t_n, c_n, spark=wk.tolist()),
        Kpi("Time", f"{acts.duration_s.sum() / 3600:,.1f}", "h", t_h, c_h),
        Kpi("Distance", f"{acts.distance_m.sum() / 1000:,.0f}", "km",
            f"{(acts.distance_m > 0).sum()} with distance"),
        Kpi("Load · 7d", num(lt.acute_7d) if lt is not None else "—", "",
            f"4-wk avg {num(lt.chronic_weekly) if lt is not None else '—'}/wk", spark=load.acute_7d.tolist() if len(load) else None),
        Kpi("Load ratio", num(lt.acwr, ".2f") if lt is not None else "—", "",
            "0.8–1.3 sweet spot · >1.5 spike",
            "c-bad" if lt is not None and lt.acwr and lt.acwr >= 1.5 else "c-muted",
            highlight="bad" if lt is not None and lt.acwr and lt.acwr >= 1.5 else ""),
    ]))

    c1, c2 = st.columns([1.35, 1], gap="medium")
    with c1:  # weekly volume by type
        f = None
        if len(acts):
            a = acts.copy()
            a["week"] = pd.to_datetime(a.calendar_date).dt.to_period("W-SUN").dt.start_time
            colors = type_colors()
            a["grp"] = a.type_key.where(a.type_key.isin(colors), "other")
            g = a.groupby(["week", "grp"]).duration_s.sum().unstack(fill_value=0) / 3600
            f = ui.fig(250, legend=True)
            order = [t for t in colors if t in g] + (["other"] if "other" in g else [])
            for t in order:
                f.add_bar(x=g.index, y=g[t], name=D.pretty_type(t) if t != "other" else "Other",
                          marker=dict(color=colors.get(t, ui.FAINT), line=dict(width=1, color=ui.CARD)),
                          hovertemplate="%{y:.1f} h<extra>" + esc(D.pretty_type(t)) + "</extra>")
            f.update_layout(barmode="stack", margin=dict(l=0, r=6, t=26, b=0))
            f.update_xaxes(tickformat="%-d %b", hoverformat="week of %-d %b")
            f.update_yaxes(ticksuffix="h")
        panel("Weekly volume", "hours by activity type", f"{acts.duration_s.sum() / 3600:,.1f} h", f)
    with c2:  # ACWR
        f = None
        if len(load):
            f = ui.fig(250, legend=True, x_range=ctx.x_range)
            lo_, hi_ = load.chronic_weekly * 0.8, load.chronic_weekly * 1.3
            f.add_scatter(x=load.date, y=hi_, mode="lines", line=dict(width=0), hoverinfo="skip", showlegend=False)
            f.add_scatter(x=load.date, y=lo_, mode="lines", line=dict(width=0), fill="tonexty",
                          fillcolor="rgba(62,207,142,0.07)", name="0.8–1.3× chronic", hoverinfo="skip")
            f.add_scatter(x=load.date, y=load.chronic_weekly, mode="lines", name="Chronic (4-wk avg/wk)",
                          line=dict(width=1.5, color=ui.MUTED, dash="dot"), hovertemplate="%{y:.0f}<extra>Chronic</extra>")
            f.add_scatter(x=load.date, y=load.acute_7d, mode="lines", name="Acute (7d)",
                          line=dict(width=2, color=ui.SERIES[0]), hovertemplate="%{y:.0f}<extra>Acute 7d</extra>")
            f.update_layout(margin=dict(l=0, r=6, t=26, b=0))
        panel("Training load", "acute vs chronic", f"ratio <b>{num(lt.acwr, '.2f') if lt is not None else '—'}</b>", f,
              caption="Derived from per-activity Garmin load; the Personal API has no training-status endpoint.")

    c1, c2 = st.columns([1.35, 1], gap="medium")
    with c1:  # calendar heatmap
        f = None
        if len(acts) or ctx.span:
            days = pd.date_range(ctx.start - timedelta(days=ctx.start.weekday()), ctx.end)
            mins = (acts.groupby("calendar_date").duration_s.sum() / 60) if len(acts) else pd.Series(dtype=float)
            mins.index = pd.to_datetime(mins.index)
            m = mins.reindex(days, fill_value=0)
            dfc = pd.DataFrame({"d": days, "m": m.values})
            dfc["wk"] = dfc.d - pd.to_timedelta(dfc.d.dt.weekday, unit="D")
            dfc["wd"] = dfc.d.dt.weekday
            z = dfc.pivot(index="wd", columns="wk", values="m")
            txt = dfc.pivot(index="wd", columns="wk", values="d").map(lambda d: d.strftime("%a %-d %b") if pd.notna(d) else "")
            f = ui.fig(190, date_axis=False)
            f.add_heatmap(z=z.values, x=z.columns, y=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], text=txt.values,
                          colorscale=[[0, ui.RAISED], [0.0001, "#163252"], [0.5, "#2a6fc0"], [1, "#8cc2f5"]],
                          xgap=3, ygap=3, showscale=False, hovertemplate="%{text}: %{z:.0f} min<extra></extra>")
            f.update_layout(hovermode="closest")
            f.update_xaxes(showspikes=False, tickformat="%-d %b")
            f.update_yaxes(autorange="reversed", showgrid=False, showspikes=False)
        panel("Activity calendar", "minutes per day", f"{(acts.groupby('calendar_date').size() > 0).sum() if len(acts) else 0} active days", f)
    with c2:  # zones by type
        z = acts.groupby("type_key")[[f"hr_z{i}_s" for i in range(1, 6)]].sum() if len(acts) else pd.DataFrame()
        z = z[z.sum(axis=1) > 0] if len(z) else z
        f = None
        if len(z):
            z = z.loc[z.sum(axis=1).sort_values(ascending=True).index].tail(6)
            f = ui.fig(190, legend=True, date_axis=False)
            for i in range(5):
                f.add_bar(y=[D.pretty_type(t) for t in z.index], x=z[f"hr_z{i + 1}_s"] / 3600, orientation="h",
                          name=f"Z{i + 1}", marker=dict(color=ZONE_COLORS[i], line=dict(width=1, color=ui.CARD)),
                          hovertemplate=f"Z{i + 1}: %{{x:.1f}} h<extra>%{{y}}</extra>")
            f.update_layout(barmode="stack", hovermode="closest", margin=dict(l=0, r=6, t=26, b=0))
            f.update_xaxes(ticksuffix="h", showspikes=False)
            f.update_yaxes(showgrid=False, showspikes=False)
        panel("Time in HR zones", "by activity type", "", f)

    _workouts_panel(ctx, compact=False)


def _activity_detail(aid: int) -> None:
    a = D.q("SELECT * FROM activity_ext WHERE activity_id=?", (aid,))
    if not len(a):
        st.warning(f"Activity {aid} not found.")
        return
    a = a.iloc[0]
    fit = D.q("SELECT * FROM activity_fit WHERE activity_id=?", (aid,))
    with st.container(border=True):
        c1, c2 = st.columns([5, 1], vertical_alignment="center")
        with c1:
            md(f'<div class="prof"><div class="t" style="font-size:18px">{esc(a["name"] or "Workout")}</div>'
               f'<div class="s mono">{esc(D.pretty_type(a.type_key))} · {esc(a.start_local)} · #{aid}'
               f'{" · " + esc(txt(a.get("location"))) if txt(a.get("location")) else ""}</div></div>')
        with c2:
            if st.button("Close", key="close_detail", width="stretch"):
                del st.query_params["activity"]
                st.rerun()
        md(ui.kpi_grid([
            Kpi("Duration", hm((a.duration_s or 0) / 3600)),
            Kpi("Distance", num((a.distance_m or 0) / 1000, ".2f") if a.distance_m else "—", "km"),
            Kpi("Avg · max HR", f"{num(a.avg_hr_bpm)} · {num(a.max_hr_bpm)}", "bpm"),
            Kpi("Load", num(a.training_load), "", esc(txt(a.te_label).replace("_", " ").lower())),
            Kpi("Training effect", f"{num(a.aerobic_te, '.1f')} · {num(a.anaerobic_te, '.1f')}", "", "aerobic · anaerobic"),
            Kpi("Calories", num(a.calories_kcal), "kcal", f"elev {num(a.elev_gain_m)} m"),
        ]))
        if len(fit) and fit.iloc[0].status == "ok" and fit.iloc[0].path:
            rec = _fit_records(str(config.PROJECT_ROOT / fit.iloc[0].path))
            if len(rec) and "timestamp" in rec:
                t = pd.to_datetime(rec.timestamp)
                rec["min"] = (t - t.iloc[0]).dt.total_seconds() / 60
                series = []
                if "heart_rate" in rec and rec.heart_rate.notna().any():
                    series.append(("heart_rate", "Heart rate", "bpm", 1.0, ui.BAD))
                spd = next((c for c in ("enhanced_speed", "speed") if c in rec and rec[c].fillna(0).gt(0).any()), None)
                if spd:
                    series.append((spd, "Speed", "km/h", 3.6, ui.SERIES[0]))
                alt = next((c for c in ("enhanced_altitude", "altitude") if c in rec and rec[c].notna().any()), None)
                if alt:
                    series.append((alt, "Elevation", "m", 1.0, ui.SERIES[2]))
                cols = st.columns(len(series) or 1, gap="medium")
                for col, (fld, title, unit, sc, color) in zip(cols, series):
                    y = (pd.to_numeric(rec[fld], errors="coerce") * sc).rolling(5, min_periods=1, center=True).mean()
                    f = ui.fig(170, date_axis=False)
                    f.add_scatter(x=rec["min"], y=y, mode="lines", line=dict(width=1.5, color=color),
                                  fill="tozeroy" if fld != "heart_rate" else None, fillcolor=color + "14",
                                  hovertemplate=f"%{{y:.1f}} {unit}<extra>%{{x:.0f}} min</extra>")
                    f.update_xaxes(ticksuffix=" min", hoverformat=".0f")
                    if fld == "heart_rate":
                        f.update_yaxes(range=[max(40, y.min() - 10), y.max() + 8])
                    elif fld == alt:
                        f.update_yaxes(range=[y.min() - 5, y.max() + 5])
                    with col:
                        md(ui.panel_header(title, unit))
                        st.plotly_chart(f, config=ui.PLOT_CFG, width="stretch")
        det = D.scalar("SELECT detail_json FROM activity WHERE activity_id=?", (aid,))
        with st.expander("Raw Garmin data"):
            st.json(json.loads(det) if det else {}, expanded=False)


@st.cache_data(show_spinner=False, max_entries=32)
def _fit_records(path: str) -> pd.DataFrame:
    import fitdecode
    recs = []
    try:
        with fitdecode.FitReader(path) as fr:
            for m in fr:
                if isinstance(m, fitdecode.FitDataMessage) and m.name == "record":
                    recs.append({f: m.get_value(f) for f in ("timestamp", "heart_rate", "enhanced_speed", "speed",
                                                             "enhanced_altitude", "altitude") if m.has_field(f)})
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    return pd.DataFrame(recs)


# ======================================================================= BODY
def render_body(ctx: D.Ctx) -> None:
    w = D.q("SELECT * FROM weight ORDER BY timestamp_gmt")
    fa = D.q("SELECT * FROM fitness_age ORDER BY as_of_date")
    latest_fa = json.loads(fa.iloc[-1].raw_json) if len(fa) else {}
    lw = w.iloc[-1] if len(w) else None
    first_in = w[w.calendar_date >= ctx.S]
    chg = (lw.weight_kg - first_in.iloc[0].weight_kg) if lw is not None and len(first_in) > 1 else None
    md(ui.kpi_grid([
        Kpi("Weight · latest", num(lw.weight_kg, ".1f") if lw is not None else "—", "kg",
            f"{lw.calendar_date} · {len(w)} weigh-ins total" if lw is not None else "no weigh-ins", spark=w.weight_kg.tail(20).tolist()),
        Kpi("Change in range", f"{chg:+.1f}" if chg is not None else "—", "kg", f"{len(first_in)} weigh-ins in range"),
        Kpi("BMI", num(lw.bmi, ".1f") if lw is not None else "—", ""),
        Kpi("Fitness age", num(latest_fa.get("fitnessAge"), ".1f"), "yrs",
            f"actual {num(latest_fa.get('chronologicalAge'))} · achievable {num(latest_fa.get('achievableFitnessAge'), '.1f')}",
            "c-good" if (latest_fa.get("fitnessAge") or 99) < (latest_fa.get("chronologicalAge") or 0) else "c-muted",
            spark=fa.fitness_age.tolist(), spark_color=ui.GOOD),
    ]))
    c1, c2 = st.columns(2, gap="medium")
    with c1:
        f = None
        if len(w):
            f = ui.fig(240)
            x = pd.to_datetime(w.timestamp_gmt, errors="coerce").fillna(pd.to_datetime(w.calendar_date))
            f.add_scatter(x=x, y=w.weight_kg, mode="lines+markers", name="Weight", line=dict(width=1.5, color=ui.SERIES[0]),
                          marker=dict(size=7, color=ui.SERIES[0], line=dict(width=2, color=ui.CARD)),
                          hovertemplate="%{y:.1f} kg<extra>Weight</extra>")
            f.update_yaxes(ticksuffix=" kg")
        panel("Weight", "all weigh-ins", f"{len(w)} entries", f, empty_msg="No weigh-ins (log weight in Garmin Connect)")
    with c2:
        f = None
        if len(fa):
            f = ui.fig(240, legend=True)
            x = pd.to_datetime(fa.as_of_date)
            f.add_scatter(x=x, y=fa.chronological_age, mode="lines", name="Actual age",
                          line=dict(width=1.5, color=ui.MUTED, dash="dot"), hovertemplate="%{y:.0f}<extra>Actual</extra>")
            f.add_scatter(x=x, y=fa.achievable_fitness_age, mode="lines", name="Achievable",
                          line=dict(width=1.5, color=ui.SERIES[2], dash="dash"), hovertemplate="%{y:.1f}<extra>Achievable</extra>")
            f.add_scatter(x=x, y=fa.fitness_age, mode="lines+markers", name="Fitness age",
                          line=dict(width=2, color=ui.GOOD), marker=dict(size=6, color=ui.GOOD),
                          hovertemplate="%{y:.1f}<extra>Fitness age</extra>")
            f.update_layout(margin=dict(l=0, r=6, t=26, b=0))
        panel("Fitness age", "monthly samples · years", "", f)
    comps = latest_fa.get("components") or {}
    if comps:
        names = {"rhr": ("Resting HR", "bpm"), "vigorousDaysAvg": ("Vigorous days", "/week"),
                 "vigorousMinutesAvg": ("Vigorous minutes", "/week"), "bmi": ("BMI", "")}
        rows = []
        for k, (label, unit) in names.items():
            c = comps.get(k) or {}
            if not c:
                continue
            tgt = c.get("targetValue")
            pot = c.get("potentialAge")
            rows.append(f'<tr><td>{esc(label)}<div class="sub">priority {num(c.get("priority"))}{" · stale" if c.get("stale") else ""}</div></td>'
                        f'<td class="num">{num(c.get("value"), ".1f")} <span class="c-muted">{esc(unit)}</span></td>'
                        f'<td class="num">{num(tgt, ".1f")}</td><td class="num">{num(pot, ".1f")}</td></tr>')
        with st.container(border=True):
            md(ui.panel_header("What drives your fitness age", "Garmin components", f"as of {esc(fa.iloc[-1].as_of_date)}"))
            md('<table class="tbl"><thead><tr><th>Component</th><th class="num">Current</th><th class="num">Target</th>'
               f'<th class="num">Fitness age if reached</th></tr></thead><tbody>{"".join(rows)}</tbody></table>')


# ======================================================================= DATA
def render_data(ctx: D.Ctx) -> None:
    tables = [("daily_summary", "calendar_date"), ("sleep", "calendar_date"), ("hrv", "calendar_date"),
              ("activity", "calendar_date"), ("activity_fit", "parsed_at"), ("weight", "calendar_date"),
              ("body_battery_day", "calendar_date"), ("hr_intraday", "calendar_date"), ("spo2_day", "calendar_date"),
              ("fitness_age", "as_of_date"), ("profile_snapshot", "synced_at")]
    rows = []
    for t, c in tables:
        r = D.q(f"SELECT count(*) n, min({c}) lo, max({c}) hi FROM {t}").iloc[0]
        rows.append(f'<tr><td class="mono">{t}</td><td class="num">{int(r.n):,}</td><td class="mono">{esc(str(r.lo or "—")[:10])}</td>'
                    f'<td class="mono">{esc(str(r.hi or "—")[:10])}</td></tr>')
    st_ = D.q("SELECT * FROM sync_state ORDER BY data_type")
    done, part = '<span class="tag g">complete</span>', '<span class="tag">partial</span>'
    srows = "".join(f'<tr><td class="mono">{esc(r.data_type)}</td><td class="mono">{esc(r.oldest_synced_date)}</td>'
                    f'<td class="mono">{esc(r.last_synced_date)}</td><td>{done if r.backfill_complete else part}</td>'
                    f'<td class="mono c-muted">{esc(r.updated_at)}</td></tr>' for _, r in st_.iterrows())
    c1, c2 = st.columns(2, gap="medium")
    with c1:
        with st.container(border=True):
            md(ui.panel_header("Tables", "data/garmin.db", f"{config.DB_PATH.stat().st_size / 1e6:.1f} MB"))
            md('<table class="tbl"><thead><tr><th>Table</th><th class="num">Rows</th><th>From</th><th>To</th></tr></thead>'
               f'<tbody>{"".join(rows)}</tbody></table>')
    with c2:
        with st.container(border=True):
            md(ui.panel_header("Sync state", "per data type", "UTC"))
            md('<table class="tbl"><thead><tr><th>Type</th><th>Oldest</th><th>Newest</th><th>Backfill</th><th>Updated</th>'
               f'</tr></thead><tbody>{srows}</tbody></table>')
        with st.container(border=True):
            md(ui.panel_header("Not available", "Personal API returned no data"))
            md('<div class="list">' + "".join(
                f'<div class="it"><span class="n">{n}</span><span class="r">{r}</span></div>' for n, r in (
                    ("Hydration", "no logged intake"), ("Nutrition", "no food logs"),
                    ("Wellness activities", "none recorded"), ("VO₂ max", "not computed for your activities"),
                    ("Training status / readiness", "no endpoint in the Personal API"))) + "</div>")
    log = config.LOG_DIR / "sync.log"
    if log.exists():
        with st.expander("logs/sync.log (last 40 lines)"):
            st.code("".join(log.read_text(errors="replace").splitlines(True)[-40:]) or "(empty)", language="log")
