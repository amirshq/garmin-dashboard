"""Garmin health console (Streamlit). Run: make dashboard"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard import data as D  # noqa: E402
from dashboard import ui, views  # noqa: E402
from garmin import config  # noqa: E402

st.set_page_config(page_title="Garmin Health", page_icon="◐", layout="wide", initial_sidebar_state="auto")
st.markdown(ui.CSS, unsafe_allow_html=True)

if not config.DB_PATH.exists():
    st.error(f"No database at `{config.DB_PATH}`. Run `make sync` first.")
    st.stop()

PAGES = {  # key: (label, icon, renderer, title)
    "overview": ("Overview", "◧", views.render_overview, "Overview"),
    "recovery": ("Recovery", "♡", views.render_recovery, "Recovery"),
    "sleep": ("Sleep", "☾", views.render_sleep, "Sleep"),
    "training": ("Training", "↗", views.render_training, "Training"),
    "body": ("Body", "◎", views.render_body, "Body"),
    "data": ("Data & sync", "≡", views.render_data, "Data & sync"),
}
RANGES = {"7d": 7, "30d": 30, "90d": 90, "1y": 365, "All": None}

# ---------------------------------------------------------------- state <-> URL
qp = st.query_params
if "page" not in st.session_state:
    st.session_state.page = qp.get("page", "overview") if qp.get("page") in PAGES else "overview"
if "range" not in st.session_state:
    st.session_state.range = qp.get("range", "30d") if qp.get("range") in RANGES else "30d"


def go(page: str, **params) -> None:
    st.session_state.page = page
    qp.clear()
    qp["page"] = page
    qp["range"] = st.session_state.range
    for k, v in params.items():
        qp[k] = v


page = st.session_state.page
bounds = D.q("SELECT min(calendar_date) lo, max(calendar_date) hi FROM daily_summary").iloc[0]
if not bounds.hi:
    st.info("Database is empty — run `make sync`.")
    st.stop()
data_lo, data_hi = date.fromisoformat(bounds.lo), date.fromisoformat(bounds.hi)


def sync_running() -> bool:
    import fcntl
    lock = ROOT / "data" / ".sync.lock"
    if not lock.exists():
        return False
    with open(lock) as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


# ---------------------------------------------------------------- sidebar
alerts = D.alerts(date.today().isoformat())
n_alerts = sum(a.severity in ("critical", "warning") for a in alerts)
week_n = int(D.scalar("SELECT count(*) FROM activity WHERE calendar_date >= ?",
                      ((data_hi - timedelta(days=6)).isoformat(),)) or 0)
badges = {"overview": (n_alerts, ui.BAD if any(a.severity == "critical" for a in alerts) else ui.WARN),
          "training": (week_n, ui.MUTED)}

css = []
for key, (label, icon, _, _) in PAGES.items():
    active = key == page
    if active:
        css.append(f".st-key-nav_{key} button {{ background:{ui.RAISED} !important; color:{ui.TEXT} !important; }}")
    css.append(f".st-key-nav_{key} button p::before {{ content: '{icon}'; display:inline-block; width:18px; margin-right:8px; "
               f"color:{ui.ACCENT if active else ui.FAINT}; font-size:13px; }}")
    n, color = badges.get(key, (0, ""))
    if n:
        css.append(f".st-key-nav_{key} button::after {{ content: '{n}'; margin-left:auto; font-family:{ui.MONO}; font-size:11px; "
                   f"padding:1px 7px; border-radius:6px; background:{color}26; color:{color}; }}")
types = D.q("SELECT type_key, count(*) n FROM activity WHERE calendar_date >= ? GROUP BY 1 ORDER BY n DESC LIMIT 8",
            ((data_hi - timedelta(days=89)).isoformat(),))
for _, t in types.iterrows():
    k = f"type_{t.type_key}"
    css.append(f".st-key-{k} button p::before {{ content:''; display:inline-block; width:7px; height:7px; border-radius:50%; "
               f"background:{views.type_color(t.type_key)}; margin:0 13px 1px 6px; }}")
    css.append(f".st-key-{k} button::after {{ content:'{t.n}'; margin-left:auto; font-family:{ui.MONO}; font-size:11px; color:{ui.FAINT}; }}")
st.markdown("<style>" + "\n".join(css) + "</style>", unsafe_allow_html=True)

with st.sidebar:
    st.markdown('<div class="brand"><div class="logo">GH</div><div><div class="n">Garmin Health</div>'
                '<div class="s">localhost:8501</div></div></div><div class="nav-h">Dashboards</div>',
                unsafe_allow_html=True)
    for key, (label, *_rest) in PAGES.items():
        if st.button(label, key=f"nav_{key}", width="stretch"):
            go(key)
            st.rerun()
    if len(types):
        st.markdown('<div class="nav-h">Activity types · 90d</div>', unsafe_allow_html=True)
        for _, t in types.iterrows():
            if st.button(D.pretty_type(t.type_key), key=f"type_{t.type_key}", width="stretch"):
                st.session_state["wf_False"] = t.type_key
                go("training")
                st.rerun()
    last = D.scalar("SELECT max(updated_at) FROM sync_state")
    running = sync_running()
    try:
        last_local = datetime.fromisoformat(last).replace(tzinfo=timezone.utc).astimezone().strftime("%-d %b %H:%M")
    except (TypeError, ValueError):
        last_local = "never"
    dot = ui.WARN if running else (ui.BAD if any(a.key == "stale" for a in alerts) else ui.GOOD)
    st.markdown(f'<div class="side-foot"><span><span class="dot" style="background:{dot}"></span>'
                f'{"syncing…" if running else "sync ok"}</span><span>{last_local}</span></div>', unsafe_allow_html=True)

# ---------------------------------------------------------------- header
label, _, render, title = PAGES[page]
n_act = int(D.scalar("SELECT count(*) FROM activity") or 0)
h1, h2, h3 = st.columns([3, 2.2, 0.9], vertical_alignment="center")
with h1:
    st.markdown(ui.page_header(title, f"{n_act:,} activities · data through {data_hi:%-d %b} · history from {data_lo:%b %Y}"),
                unsafe_allow_html=True)
with h2:
    with st.container(horizontal=True, horizontal_alignment="right"):
        rng = st.segmented_control("Range", list(RANGES), key="range", label_visibility="collapsed")
        if rng and qp.get("range") != rng:
            qp["range"] = rng
with h3:
    if st.button("Syncing…" if running else "Sync now", type="primary", width="stretch", disabled=running,
                 help="Runs `python -m garmin.sync` in the background (incremental). Output: logs/sync.log"):
        log = open(config.LOG_DIR / "sync.log", "a")
        subprocess.Popen([sys.executable, "-m", "garmin.sync"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)
        st.cache_data.clear()
        st.toast("Sync started — refresh in a minute. Output in logs/sync.log")
st.markdown('<div class="hr"></div>', unsafe_allow_html=True)

days = RANGES.get(st.session_state.range or "30d")
start = data_lo if days is None else max(data_lo, data_hi - timedelta(days=days - 1))
ctx = D.Ctx(start=start, end=data_hi, data_lo=data_lo, data_hi=data_hi)
render(ctx)
