"""Data for the snapshot's hero cards (Body Battery, Sleep, Activity, Heart) in Today / Week / Month views.

Everything is computed from the local SQLite DB and handed to snapshot_assets/page.js as JSON.
"Today" is the latest day with a daily summary (normally yesterday, since the sync runs at 06:00).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from zoneinfo import ZoneInfo

WEEKDAY = "MTWTFSS"
STAGE_KEYS = (("deep_s", "Deep"), ("light_s", "Light"), ("rem_s", "REM"), ("awake_s", "Awake"))
IM_WEEK_GOAL = 150  # WHO / Garmin default weekly intensity-minute goal


def _rows(conn, sql, params=()):
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.row_factory = None


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return mean(xs) if xs else None


def _hm(sec) -> str:
    if sec is None:
        return "—"
    h, m = divmod(round(sec / 60), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _pct_delta(cur, prev, label, higher_is_better=True):
    if cur is None or not prev:
        return {"text": "", "tone": "muted"}
    d = round((cur - prev) / prev * 100)
    if d == 0:
        return {"text": f"Same as {label}", "tone": "muted"}
    good = (d > 0) == higher_is_better
    return {"text": f"{'+' if d > 0 else '−'}{abs(d)}% vs {label}", "tone": "good" if good else "warn"}


def _tz(conn) -> ZoneInfo:
    r = conn.execute("SELECT json_extract(raw_json,'$.timeZone') FROM profile_snapshot WHERE kind='user_settings'").fetchone()
    try:
        return ZoneInfo(r[0]) if r and r[0] else ZoneInfo("UTC")
    except Exception:
        return ZoneInfo("UTC")


def _local(ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz)


def _gmt_to_local(s: str | None, tz: ZoneInfo) -> str:
    if not s:
        return ""
    return datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc).astimezone(tz).strftime("%H:%M")


def _days(end: date, n: int) -> list[str]:
    return [(end - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


# ---------------------------------------------------------------- cards
def energy(conn, end: date, tz: ZoneInfo, ds: dict) -> dict:
    E = end.isoformat()
    peak = lambda d: (ds.get(d) or {}).get("bb_max")  # noqa: E731
    out = {}

    # today: level at 6a, 9a, ... 12a from the 30-min Body Battery curve
    bb = _rows(conn, "SELECT values_json FROM body_battery_day WHERE calendar_date=?", (E,))
    pts = [(_local(t, tz), v) for t, v in json.loads((bb[0]["values_json"] if bb else None) or "[]") if v is not None]
    marks = [(6, "6a"), (9, "9a"), (12, "12p"), (15, "3p"), (18, "6p"), (21, "9p"), (24, "12a")]
    bars = []
    for h, _ in marks:
        cutoff = datetime.combine(end, datetime.min.time(), tz) + timedelta(hours=h)
        before = [v for t, v in pts if t <= cutoff]
        bars.append(before[-1] if before else None)
    yesterday = (end - timedelta(days=1)).isoformat()
    out["today"] = {"label": f"Peak · {end:%a %-d %b}", "value": peak(E), "unit": "",
                    "delta": _pct_delta(peak(E), peak(yesterday), "the day before"),
                    "bars": bars, "labels": [m[1] for m in marks], "caption": "level through the day"}

    def span(n: int, label: str, prev_label: str):
        days = _days(end, n)
        prev = _days(end - timedelta(days=n), n)
        cur_v, prev_v = _avg([peak(d) for d in days]), _avg([peak(d) for d in prev])
        if n == 7:
            bars = [peak(d) for d in days]
            labels = [WEEKDAY[date.fromisoformat(d).weekday()] for d in days]
            cap = "daily peak"
        else:  # 7 weekly averages ending today
            bars, labels = [], []
            for w in range(6, -1, -1):
                wk = _days(end - timedelta(days=7 * w), 7)
                a = _avg([peak(d) for d in wk])
                bars.append(round(a) if a is not None else None)
                labels.append(f"{date.fromisoformat(wk[0]):%-d/%-m}")
            cap = "weekly avg peak"
        return {"label": label, "value": round(cur_v) if cur_v is not None else None, "unit": "",
                "delta": _pct_delta(cur_v, prev_v, prev_label), "bars": bars, "labels": labels, "caption": cap}

    out["week"] = span(7, "Avg peak · 7 days", "prior week")
    out["month"] = span(30, "Avg peak · 30 days", "prior 30 days")
    return out


def sleep(conn, end: date, tz: ZoneInfo) -> dict:
    rows = _rows(conn, "SELECT calendar_date, sleep_s, deep_s, light_s, rem_s, awake_s, sleep_score, start_gmt, end_gmt, "
                       "json_extract(raw_json,'$.sleepScores.overall.qualifierKey') AS q, "
                       "json_extract(raw_json,'$.sleepNeed.actual') AS need_min "
                       "FROM sleep WHERE sleep_s IS NOT NULL AND calendar_date BETWEEN ? AND ? ORDER BY calendar_date",
                 ((end - timedelta(days=59)).isoformat(), end.isoformat()))
    if not rows:
        return {}
    by = {r["calendar_date"]: r for r in rows}
    last = rows[-1]

    def agg(n: int, label: str):
        sel = [by[d] for d in _days(end, n) if d in by]
        tot = _avg([r["sleep_s"] for r in sel])
        stages = {k: _avg([r[k] for r in sel]) for k, _ in STAGE_KEYS}
        sc = round(_avg([r["sleep_score"] for r in sel]) or 0)
        return {"label": label, "total": _hm(tot), "total_s": tot, "score": sc,
                "line": f"Avg score {sc} · {len(sel)} of {n} nights tracked",
                "window": f"Nightly average · {len(sel)} of {n} nights tracked · avg score {sc}",
                "stages": [{"name": n_, "s": stages[k], "txt": _hm(stages[k])} for k, n_ in STAGE_KEYS]}

    q = (last["q"] or "").replace("_", " ").title()
    today = {"label": f"Stage split · night of {date.fromisoformat(last['calendar_date']) - timedelta(days=1):%a %-d %b}",
             "total": _hm(last["sleep_s"]), "total_s": last["sleep_s"], "score": last["sleep_score"],
             "line": f"Score {last['sleep_score']}" + (f" · {q}" if q else ""),
             "window": f"{_gmt_to_local(last['start_gmt'], tz)} — {_gmt_to_local(last['end_gmt'], tz)} · Score {last['sleep_score']}",
             "stages": [{"name": n_, "s": last[k], "txt": _hm(last[k])} for k, n_ in STAGE_KEYS]}

    # insight: last night vs the 7 nights before it, and vs Garmin's sleep need
    prev7 = [by[d]["sleep_s"] for d in _days(end - timedelta(days=1), 7) if d in by]
    insight = ""
    if prev7:
        diff = round((last["sleep_s"] - mean(prev7)) / 60)
        if abs(diff) >= 5:
            insight = f"Last night you slept {abs(diff)} min {'longer' if diff > 0 else 'less'} than your 7-night average."
        else:
            insight = "Last night was right on your 7-night average."
        if last["need_min"]:
            gap = round(last["sleep_s"] / 60 - last["need_min"])
            insight += f" {'Above' if gap >= 0 else 'Short of'} Garmin's sleep need by {abs(gap)} min."

    nights13 = []
    for d in _days(end, 13):
        s = (by.get(d) or {}).get("sleep_score")
        nights13.append(0 if s is None else 1 if s < 60 else 2 if s < 75 else 3 if s < 85 else 4)
    trend = [{"d": d, "score": by[d]["sleep_score"], "h": round(by[d]["sleep_s"] / 3600, 2)}
             for d in _days(end, 30) if d in by and by[d]["sleep_score"] is not None]
    return {"today": today, "week": agg(7, "Stage split · average of the last 7 days"), "month": agg(30, "Stage split · average of the last 30 days"),
            "matrix": nights13, "trend": trend, "insight": insight}


def activity(conn, end: date, ds: dict) -> dict:
    E = end.isoformat()
    d = ds.get(E) or {}
    im = lambda r: (r.get("im_moderate_min") or 0) + 2 * (r.get("im_vigorous_min") or 0)  # noqa: E731  Garmin counts vigorous x2
    acts = _rows(conn, "SELECT name, type_key, calendar_date, duration_s, training_load FROM activity "
                       "WHERE calendar_date BETWEEN ? AND ? ORDER BY start_local DESC", ((end - timedelta(days=29)).isoformat(), E))
    lw = acts[0] if acts else None

    def last_txt(a):
        if not a:
            return {"title": "No workouts", "sub": "nothing recorded in 30 days"}
        dur = _hm(a["duration_s"])
        load = f" · load {a['training_load']:.0f}" if a["training_load"] else ""
        return {"title": a["name"] or a["type_key"], "sub": f"{date.fromisoformat(a['calendar_date']):%a %-d %b} · {dur}{load}"}

    def span(n: int):
        days = _days(end, n)
        sel = [a for a in acts if a["calendar_date"] >= days[0]]
        mins = sum(im(ds.get(x) or {}) for x in days)
        goal = round(IM_WEEK_GOAL * n / 7)
        hrs = sum((a["duration_s"] or 0) for a in sel)
        load = sum((a["training_load"] or 0) for a in sel)
        return {"value": f"{mins:,}", "of": f"of {goal} min goal", "pct": mins / goal if goal else 0,
                "title": f"{len(sel)} workout{'s' if len(sel) != 1 else ''}",
                "sub": f"{_hm(hrs)} total · load {load:.0f}"}

    steps, goal = d.get("steps"), d.get("steps_goal") or 0
    today = {"value": f"{steps:,}" if steps is not None else "—", "of": f"of {goal:,} steps" if goal else "steps",
             "pct": (steps or 0) / goal if goal else 0, **last_txt(lw)}
    return {"today": today, "week": span(7), "month": span(30)}


def heart(conn, end: date, tz: ZoneInfo, ds: dict) -> dict:
    E = end.isoformat()
    h = _rows(conn, "SELECT resting_bpm, min_bpm, max_bpm, values_json FROM hr_intraday WHERE calendar_date=?", (E,))
    vals = [v for _, v in json.loads((h[0]["values_json"] if h else None) or "[]") if v]
    n = 36
    wave = [round(mean(vals[i * len(vals) // n:(i + 1) * len(vals) // n])) if len(vals) >= n else None for i in range(n)]
    hv = _rows(conn, "SELECT calendar_date, last_night_avg_ms, weekly_avg_ms, status FROM hrv WHERE calendar_date<=? "
                     "ORDER BY calendar_date DESC LIMIT 30", (E,))
    status = (hv[0]["status"] if hv else "") or ""
    tone = "good" if status.upper() == "BALANCED" else "warn" if status else "muted"
    chip = {"text": f"HRV {status.lower()}" if status else "HRV n/a", "tone": tone}
    rhr = lambda d: (ds.get(d) or {}).get("rhr_bpm")  # noqa: E731

    def span(n: int, label: str):
        days = _days(end, 36)
        sel = [rhr(d) for d in _days(end, n)]
        hrv = _avg([r["last_night_avg_ms"] for r in hv[:n]])
        return {"wave": [rhr(d) for d in days], "wave_caption": "resting HR · 36 days",
                "title": f"Resting {round(_avg(sel)) if _avg(sel) else '—'} bpm", "label": label,
                "sub": f"HRV {round(hrv) if hrv else '—'} ms avg · {label}", "right": f"{min([x for x in sel if x] or [0])}–{max([x for x in sel if x] or [0])}"}

    today = {"wave": wave, "wave_caption": "heart rate through the day",
             "title": f"Resting {rhr(E) or '—'} bpm", "label": "today",
             "sub": f"HRV {hv[0]['last_night_avg_ms'] if hv else '—'} ms · 7-day {hv[0]['weekly_avg_ms'] if hv else '—'}",
             "right": f"max {h[0]['max_bpm']}" if h and h[0]["max_bpm"] else ""}
    return {"chip": chip, "today": today, "week": span(7, "7 days"), "month": span(30, "30 days")}


def hero_data(conn: sqlite3.Connection, end: date) -> dict:
    tz = _tz(conn)
    ds = {r["calendar_date"]: r for r in _rows(
        conn, "SELECT calendar_date, steps, steps_goal, rhr_bpm, bb_max, bb_min, im_moderate_min, im_vigorous_min "
              "FROM daily_summary WHERE calendar_date BETWEEN ? AND ?", ((end - timedelta(days=120)).isoformat(), end.isoformat()))}
    return {"end": end.isoformat(), "end_label": f"{end:%A, %-d %B}",
            "energy": energy(conn, end, tz, ds), "sleep": sleep(conn, end, tz),
            "activity": activity(conn, end, ds), "heart": heart(conn, end, tz, ds)}
