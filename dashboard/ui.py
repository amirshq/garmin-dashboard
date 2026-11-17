"""Design system for the dashboard: tokens, global CSS, HTML components, Plotly theme.

Dark, dense "ops console" look: sidebar nav, mono metadata, alert banner, KPI cards with
sparklines, template-style tables and side-rail cards. All components are plain HTML strings
rendered with st.markdown(..., unsafe_allow_html=True); every dynamic value is escaped.
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass

import pandas as pd
import plotly.graph_objects as go

# ---------------------------------------------------------------- tokens
BG, PANEL, CARD, RAISED = "#0d0f12", "#0f1115", "#14171c", "#191d23"
LINE, LINE_SOFT, GRID = "#23272e", "#1b1f25", "#1d2127"
TEXT, TEXT_2, MUTED, FAINT = "#e7e9ec", "#b9bec6", "#8a919b", "#5c636d"
ACCENT, ACCENT_BG = "#4cc3d9", "#0f2a33"
GOOD, WARN, BAD, INFO = "#3ecf8e", "#f2c94c", "#f0605d", "#5aa9f0"
FONT = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
MONO = "'JetBrains Mono', 'SF Mono', Menlo, monospace"

# validated (dataviz validate_palette.js --mode dark --surface #14171c): all checks pass
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9"]
SERIES_SOFT = "rgba(57,135,229,0.30)"
# sleep stages, dark: passes; one adjacent pair in the CVD 6-8 band -> secondary encoding (segment gaps + legend)
STAGES = (("deep_s", "Deep", "#3062c4"), ("light_s", "Light", "#2f9fd8"),
          ("rem_s", "REM", "#9466e8"), ("awake_s", "Awake", "#d65a8a"))

SEV = {  # status palette: always shown with icon + label, never color alone
    "critical": (BAD, "rgba(240,96,93,0.10)", "rgba(240,96,93,0.35)", "▲"),
    "warning": (WARN, "rgba(242,201,76,0.08)", "rgba(242,201,76,0.30)", "●"),
    "info": (INFO, "rgba(90,169,240,0.08)", "rgba(90,169,240,0.28)", "◆"),
    "good": (GOOD, "rgba(62,207,142,0.08)", "rgba(62,207,142,0.28)", "✓"),
}


def esc(v) -> str:
    return _html.escape("" if v is None else str(v))


CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');
:root {{ --mono: {MONO}; }}
html, body, .stApp {{ background: {BG}; }}
.stApp, .stApp p, .stApp span, .stApp div {{ font-family: {FONT}; }}
header[data-testid="stHeader"] {{ background: transparent; height: 0; }}
[data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"], footer {{ display: none !important; }}
@media (min-width: 761px) {{
  [data-testid="stSidebarCollapseButton"], [data-testid="collapsedControl"], [data-testid="stExpandSidebarButton"] {{ display: none !important; }}
}}
@media (max-width: 760px) {{
  header[data-testid="stHeader"] {{ height: auto; }}
  [data-testid="stExpandSidebarButton"], [data-testid="collapsedControl"] {{ color: {TEXT}; }}
  .side-foot {{ position: static; margin-top: 16px; }}
}}
.block-container, [data-testid="stMainBlockContainer"] {{ max-width: 1480px; padding: 1.1rem 1.6rem 4rem; }}
[data-testid="stVerticalBlock"] {{ gap: .75rem; }}
.mono {{ font-family: {MONO} !important; }}

/* ---------- sidebar ---------- */
[data-testid="stSidebar"] {{ background: {PANEL}; border-right: 1px solid {LINE}; min-width: 236px !important;
    max-width: 236px !important; }}
[data-testid="stSidebar"] > div {{ padding-top: 0; }}
[data-testid="stSidebarContent"] {{ padding: 0 .55rem; }}
[data-testid="stSidebarUserContent"] {{ padding: 0 !important; }}
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{ gap: 2px; }}
.brand {{ display: flex; gap: 10px; align-items: center; padding: 18px 8px 16px; border-bottom: 1px solid {LINE};
          margin: 0 -.55rem 10px; padding-left: calc(8px + .55rem); }}
.brand .logo {{ width: 32px; height: 32px; border-radius: 8px; display: grid; place-items: center; font-weight: 700;
                font-size: 12px; color: #fff; background: linear-gradient(135deg, #3987e5, #9085e9); }}
.brand .n {{ font-weight: 600; font-size: 14px; color: {TEXT}; line-height: 1.2; }}
.brand .s {{ font-family: {MONO}; font-size: 10px; color: {MUTED}; letter-spacing: .06em; text-transform: uppercase; }}
.nav-h {{ font-size: 10.5px; font-weight: 600; letter-spacing: .1em; color: {FAINT}; text-transform: uppercase;
          padding: 16px 10px 8px; line-height: 1.2; }}
[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(.nav-h) {{ margin-bottom: 4px; }}
[data-testid="stSidebar"] .stButton button {{ width: 100%; justify-content: flex-start; border: 0; background: transparent;
    color: {TEXT_2}; padding: 7px 10px; min-height: 0; border-radius: 7px; font-size: 13.5px; font-weight: 450; }}
[data-testid="stSidebar"] .stButton button:hover {{ background: {RAISED}; color: {TEXT}; }}
[data-testid="stSidebar"] .stButton button p {{ font-size: 13.5px; }}
[data-testid="stSidebar"] .stButton button > div {{ width: 100%; justify-content: flex-start; }}
.side-foot {{ position: fixed; bottom: 0; width: 236px; margin-left: -.55rem; border-top: 1px solid {LINE};
              padding: 12px 16px; font-size: 11.5px; color: {MUTED}; display: flex; justify-content: space-between;
              background: {PANEL}; font-family: {MONO}; }}
.dot {{ display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 6px; vertical-align: 1px; }}

/* ---------- page header ---------- */
.ph {{ display: flex; align-items: baseline; gap: 14px; padding: 2px 0 0; }}
.ph h1 {{ font-size: 19px !important; font-weight: 600 !important; margin: 0 !important; padding: 0 !important; color: {TEXT}; letter-spacing: -.01em; }}
.ph .meta {{ font-family: {MONO}; font-size: 11.5px; color: {MUTED}; }}
.hr {{ border-top: 1px solid {LINE}; margin: 10px -1.6rem 6px; }}
div[data-testid="stSegmentedControl"] button, div[data-testid="stButtonGroup"] button {{
    background: transparent; border-color: {LINE}; color: {TEXT_2}; font-family: {MONO}; font-size: 12px; min-height: 32px; }}
div[data-testid="stButtonGroup"] button[kind$="Active"], div[data-testid="stButtonGroup"] button[aria-checked="true"] {{
    background: {ACCENT_BG}; color: {ACCENT}; border-color: {ACCENT}; }}
.stMainBlockContainer .stButton button[kind="primary"] {{ background: {ACCENT_BG}; color: {ACCENT}; border: 1px solid {ACCENT};
    font-size: 12.5px; min-height: 32px; }}
.stMainBlockContainer .stButton button[kind="primary"]:hover {{ background: #133844; }}

/* ---------- notes / alerts ---------- */
.note {{ border: 1px dashed #3a3f6b; border-radius: 10px; padding: 10px 14px; font-size: 12.5px; color: #b8b2f0;
         background: rgba(124,108,240,.05); line-height: 1.55; }}
.note b {{ color: #cdc7ff; }} .note code {{ font-family: {MONO}; font-size: 11.5px; color: #d9d5ff; background: none; }}
.alerts {{ border-radius: 10px; padding: 12px 16px 12px 16px; line-height: 1.6; margin-bottom: 6px; }}
.note {{ margin-bottom: 6px; }} .kpis {{ margin-bottom: 6px; }}
.alerts .h {{ display: flex; justify-content: space-between; align-items: center; font-family: {MONO};
              font-size: 12px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; margin-bottom: 4px; }}
.alerts .h small {{ font-family: {FONT}; font-weight: 400; letter-spacing: 0; text-transform: none; color: {MUTED}; font-size: 11.5px; }}
.alerts .row {{ font-size: 13px; color: {TEXT_2}; display: flex; gap: 8px; }}
.alerts .row b {{ font-weight: 600; }}
.alerts .row .ic {{ width: 14px; flex: none; text-align: center; font-size: 10px; padding-top: 3px; }}
.alerts .row .m {{ font-family: {MONO}; font-size: 12px; color: {TEXT}; }}

/* ---------- KPI cards ---------- */
.kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(165px, 1fr)); gap: 12px; }}
.kpi {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 10px; padding: 14px 16px 12px; position: relative;
        min-width: 0; overflow: hidden; }}
.kpi.hl-bad {{ border-color: rgba(240,96,93,.45); }} .kpi.hl-warn {{ border-color: rgba(242,201,76,.35); }}
.kpi .l {{ font-family: {MONO}; font-size: 10.5px; font-weight: 500; letter-spacing: .1em; text-transform: uppercase; color: {MUTED}; }}
.kpi .v {{ font-size: 27px; font-weight: 600; color: {TEXT}; margin: 6px 0 2px; letter-spacing: -.01em; white-space: nowrap; }}
.kpi .v small {{ font-size: 13px; color: {MUTED}; font-weight: 500; margin-left: 3px; }}
.kpi .vr {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; }}
.kpi .f {{ font-size: 12px; color: {MUTED}; line-height: 1.4; }}
.kpi .f .t {{ display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
.kpi svg {{ flex: none; }}
.c-good {{ color: {GOOD} !important; }} .c-bad {{ color: {BAD} !important; }} .c-warn {{ color: {WARN} !important; }}
.c-muted {{ color: {MUTED} !important; }} .c-accent {{ color: {ACCENT} !important; }}

/* ---------- panels (bordered containers) ---------- */
[data-testid="stVerticalBlockBorderWrapper"] {{ background: {CARD}; border-color: {LINE} !important; border-radius: 10px !important; }}
.panel-h {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: -2px 0 -4px; }}
.panel-h .t {{ font-size: 14px; font-weight: 600; color: {TEXT}; }}
.panel-h .t span {{ font-weight: 400; color: {MUTED}; font-size: 12px; margin-left: 6px; }}
.panel-h .r {{ font-family: {MONO}; font-size: 11.5px; color: {MUTED}; white-space: nowrap; }}
.panel-h .r b {{ color: {TEXT}; font-weight: 600; }}
.cap {{ font-size: 11.5px; color: {FAINT}; margin-top: -6px; }}

/* ---------- tables ---------- */
.tbl {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.tbl th {{ font-family: {MONO}; font-size: 10.5px; font-weight: 500; letter-spacing: .08em; text-transform: uppercase;
           color: {MUTED}; text-align: left; padding: 10px 12px; border-bottom: 1px solid {LINE}; white-space: nowrap; }}
.tbl td {{ padding: 11px 12px; border-bottom: 1px solid {LINE_SOFT}; color: {TEXT}; vertical-align: top; }}
.tbl tr:last-child td {{ border-bottom: 0; }}
.tbl tr.flag td:first-child {{ box-shadow: inset 3px 0 0 {BAD}; }}
.tbl tr.flag-w td:first-child {{ box-shadow: inset 3px 0 0 {WARN}; }}
.tbl tr:hover td {{ background: rgba(255,255,255,.015); }}
.tbl .sub {{ font-family: {MONO}; font-size: 11px; color: {FAINT}; margin-top: 3px; line-height: 1.5; }}
.tbl .num {{ text-align: right; font-family: {MONO}; white-space: nowrap; }}
.tbl a {{ color: {TEXT}; text-decoration: none; font-weight: 500; }}
.tbl a:hover {{ color: {ACCENT}; }}
.pill {{ display: inline-flex; align-items: center; gap: 6px; padding: 2px 9px; border-radius: 999px; font-size: 11.5px;
         white-space: nowrap; border: 1px solid; }}
.pill::before {{ content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }}
.tag {{ font-family: {MONO}; font-size: 10.5px; letter-spacing: .06em; text-transform: uppercase; padding: 3px 7px;
        border: 1px solid {LINE}; border-radius: 4px; color: {MUTED}; white-space: nowrap; }}
.tag.y {{ color: {WARN}; border-color: rgba(242,201,76,.5); background: rgba(242,201,76,.08); }}
.tag.g {{ color: {GOOD}; border-color: rgba(62,207,142,.45); background: rgba(62,207,142,.07); }}
.bar {{ height: 4px; border-radius: 2px; background: {LINE}; position: relative; min-width: 80px; margin-top: 6px; }}
.bar > i {{ position: absolute; left: 0; top: 0; bottom: 0; border-radius: 2px; }}
.tl {{ font-family: {MONO}; font-size: 11px; color: {FAINT}; display: flex; justify-content: space-between; gap: 8px; }}

/* ---------- side-rail list ---------- */
.list .it {{ display: flex; align-items: baseline; gap: 12px; padding: 9px 2px; border-bottom: 1px solid {LINE_SOFT}; font-size: 13px; }}
.list .it:last-child {{ border-bottom: 0; }}
.list .it .q {{ font-family: {MONO}; color: {ACCENT}; min-width: 58px; font-weight: 500; }}
.list .it .n {{ color: {TEXT}; font-weight: 500; flex: 1; min-width: 0; }}
.list .it .n small {{ font-family: {MONO}; color: {FAINT}; font-weight: 400; margin-left: 6px; font-size: 10.5px; }}
.list .it .r {{ color: {MUTED}; font-size: 12px; white-space: nowrap; }}
.prof .t {{ font-size: 15px; font-weight: 600; color: {TEXT}; }}
.prof .s {{ font-size: 12px; color: {MUTED}; }}
.prof {{ padding-bottom: 4px; }}
.prof .kv {{ display: flex; justify-content: space-between; font-size: 12.5px; color: {TEXT_2}; margin-top: 8px; }}
.prof .kv b {{ font-family: {MONO}; font-weight: 600; }}
.prof p {{ font-size: 12.5px; color: {TEXT_2}; line-height: 1.55; margin: 8px 0 0; }}
.zones {{ display: flex; height: 8px; border-radius: 4px; overflow: hidden; gap: 2px; margin-top: 8px; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 11.5px; color: {MUTED}; margin-top: 8px; }}
.legend i {{ display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 5px; vertical-align: 0; }}
.empty {{ height: 180px; display: grid; place-items: center; color: {FAINT}; font-size: 12.5px; }}

/* widgets */
[data-testid="stPills"] button, div[data-testid="stButtonGroup"] [data-testid="stBaseButton-pills"],
[data-testid="stBaseButton-pillsActive"] {{ font-size: 12px; }}
[data-testid="stExpander"] details {{ background: {CARD}; border-color: {LINE}; }}
[data-testid="stDataFrame"] {{ border: 1px solid {LINE}; border-radius: 10px; overflow: hidden; }}
@media (max-width: 760px) {{
  .block-container, [data-testid="stMainBlockContainer"] {{ padding: .8rem .9rem 3rem; }}
  .kpis {{ grid-template-columns: 1fr 1fr; gap: 8px; }}
  .kpi .v {{ font-size: 22px; }} .kpi svg {{ display: none; }}
  .tbl .hide-sm {{ display: none; }}
  .hr {{ margin: 10px -.9rem 6px; }}
  .ph {{ flex-direction: column; gap: 2px; }} .ph h1 {{ white-space: nowrap; }}
  [data-testid="stMarkdownContainer"]:has(> .tbl), [data-testid="stMarkdownContainer"]:has(.tbl) {{ overflow-x: auto; }}
  .tbl td, .tbl th {{ padding: 9px 8px; }}
}}
</style>
"""


# ---------------------------------------------------------------- formatting
def hm(hours) -> str:
    if hours is None or pd.isna(hours):
        return "—"
    h, m = divmod(round(float(hours) * 60), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def num(v, fmt=",.0f", dash="—") -> str:
    return dash if v is None or pd.isna(v) else format(v, fmt)


def sparkline(values, color: str = ACCENT, w: int = 72, h: int = 26) -> str:
    v = [float(x) for x in values if x is not None and not pd.isna(x)]
    if len(v) < 2:
        return ""
    lo, hi = min(v), max(v)
    r = (hi - lo) or 1.0
    pts = " ".join(f"{i / (len(v) - 1) * w:.1f},{h - 2 - (x - lo) / r * (h - 4):.1f}" for i, x in enumerate(v))
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true">'
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.4" stroke-linejoin="round" '
            f'stroke-linecap="round" opacity=".9"/></svg>')


# ---------------------------------------------------------------- components
def page_header(title: str, meta: str) -> str:
    return f'<div class="ph"><h1>{esc(title)}</h1><span class="meta">{esc(meta)}</span></div>'


def note(html_body: str) -> str:
    return f'<div class="note">{html_body}</div>'


def alert_banner(alerts: list, title: str = "Alerts", limit: int = 6) -> str:
    if not alerts:
        return ""
    top = alerts[0].severity
    color, bg, border, _ = SEV[top]
    n_bad = sum(a.severity in ("critical", "warning") for a in alerts)
    rows = []
    for a in alerts[:limit]:
        c, _, _, ic = SEV[a.severity]
        rows.append(f'<div class="row"><span class="ic" style="color:{c}">{ic}</span><div>'
                    f'<b style="color:{c}">{esc(a.title)}</b> — {esc(a.detail)}</div></div>')
    label = f"{esc(title)} · {n_bad}" if n_bad else esc(title)
    return (f'<div class="alerts" style="background:{bg};border:1px solid {border}">'
            f'<div class="h" style="color:{color}"><span>{label}</span><small>{len(alerts)} signals · rules in '
            f'garmin/insights.py</small></div>{"".join(rows)}</div>')


@dataclass
class Kpi:
    label: str
    value: str
    unit: str = ""
    foot: str = ""
    foot_cls: str = "c-muted"
    spark: list | None = None
    spark_color: str = ACCENT
    highlight: str = ""  # "", "bad", "warn"
    value_cls: str = ""


def kpi_grid(kpis: list[Kpi]) -> str:
    cards = []
    for k in kpis:
        sp = sparkline(k.spark, k.spark_color) if k.spark is not None else ""
        hl = f" hl-{k.highlight}" if k.highlight else ""
        cards.append(f'<div class="kpi{hl}"><div class="l">{esc(k.label)}</div>'
                     f'<div class="vr"><div class="v {k.value_cls}">{esc(k.value)}<small>{esc(k.unit)}</small></div>{sp}</div>'
                     f'<div class="f"><span class="t {k.foot_cls}">{k.foot}</span></div></div>')
    return f'<div class="kpis">{"".join(cards)}</div>'


def panel_header(title: str, sub: str = "", right: str = "") -> str:
    return (f'<div class="panel-h"><div class="t">{esc(title)}<span>{esc(sub)}</span></div>'
            f'<div class="r">{right}</div></div>')


def pill(text: str, color: str) -> str:
    return (f'<span class="pill" style="color:{color};border-color:{color}55;background:{color}14">'
            f'{esc(text)}</span>')


def bar(frac: float, color: str = ACCENT) -> str:
    f = max(0.0, min(1.0, float(frac or 0)))
    return f'<div class="bar"><i style="width:{f * 100:.0f}%;background:{color}"></i></div>'


def legend(items: list[tuple[str, str]]) -> str:
    return '<div class="legend">' + "".join(f'<span><i style="background:{c}"></i>{esc(n)}</span>'
                                            for n, c in items) + "</div>"


def empty(msg: str = "No data in this range") -> str:
    return f'<div class="empty">{esc(msg)}</div>'


# ---------------------------------------------------------------- plotly
PLOT_CFG = {"displayModeBar": False, "responsive": True}


def fig(height: int = 220, legend: bool = False, x_range: tuple | None = None, date_axis: bool = True) -> go.Figure:
    f = go.Figure()
    f.update_layout(
        height=height, margin=dict(l=0, r=6, t=8, b=0), showlegend=legend,
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, size=11, color=MUTED), hovermode="x unified", hoverdistance=40,
        hoverlabel=dict(bgcolor=RAISED, bordercolor=LINE, font=dict(family=FONT, size=12, color=TEXT)),
        xaxis=dict(showgrid=False, showline=False, ticks="", fixedrange=True, nticks=7,
                   tickfont=dict(family=MONO, size=10, color=FAINT),
                   showspikes=True, spikemode="across", spikethickness=1, spikecolor="#3a404a", spikedash="solid"),
        yaxis=dict(gridcolor=GRID, zeroline=False, showline=False, ticks="", nticks=4, fixedrange=True,
                   tickfont=dict(family=MONO, size=10, color=FAINT)),
        legend=dict(orientation="h", y=1.02, yanchor="bottom", x=1, xanchor="right",
                    font=dict(size=11, color=TEXT_2), itemclick=False, itemdoubleclick=False, traceorder="normal"),
        bargap=0.25,
    )
    if date_axis:
        f.update_xaxes(tickformat="%-d %b", hoverformat="%a %-d %b %Y")
    if x_range:
        f.update_xaxes(range=list(x_range))
    return f
