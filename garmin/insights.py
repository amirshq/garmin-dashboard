"""Rule-based health alerts computed from the local SQLite DB.

Used by the dashboard's alert banner and the MCP `get_alerts` tool. Rules are deliberately
simple and transparent; each alert says which numbers triggered it. Not medical advice.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from statistics import median

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2, "good": 3}
DEFAULT_SLEEP_NEED_MIN = 7 * 60


@dataclass
class Alert:
    severity: str  # critical | warning | info | good
    key: str
    title: str
    detail: str
    page: str = "overview"  # dashboard page that explains it

    def as_dict(self) -> dict:
        return asdict(self)


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    return conn.execute(sql, params).fetchall()


def _hm(minutes: float) -> str:
    h, m = divmod(round(minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def training_load(conn: sqlite3.Connection, end: date, days: int = 120) -> list[dict]:
    """Daily load with acute (7d sum), chronic (28d sum / 4 = weekly avg) and ACWR."""
    start = end - timedelta(days=days + 28)
    by_day = dict(_rows(conn, "SELECT calendar_date, sum(coalesce(training_load,0)) FROM activity "
                              "WHERE calendar_date BETWEEN ? AND ? GROUP BY 1", (start.isoformat(), end.isoformat())))
    series, window = [], []
    d = start
    while d <= end:
        window.append(float(by_day.get(d.isoformat(), 0.0)))
        if len(window) > 28:
            window.pop(0)
        acute = sum(window[-7:])
        chronic = sum(window) / 4 if len(window) == 28 else None
        if d > end - timedelta(days=days):
            series.append({"date": d.isoformat(), "load": round(window[-1], 1), "acute_7d": round(acute, 1),
                           "chronic_weekly": round(chronic, 1) if chronic is not None else None,
                           "acwr": round(acute / chronic, 2) if chronic else None})
        d += timedelta(days=1)
    return series


def compute_alerts(conn: sqlite3.Connection, today: date | None = None) -> list[Alert]:
    today = today or date.today()
    alerts: list[Alert] = []

    # -- data freshness
    last = _rows(conn, "SELECT max(calendar_date) FROM daily_summary")[0][0]
    if not last:
        return [Alert("critical", "no_data", "No data synced yet", "Run `make sync`.", "data")]
    age = (today - date.fromisoformat(last)).days
    if age >= 2:
        alerts.append(Alert("critical", "stale", f"Data is {age} days old",
                            f"Latest day in the database is {last}. Check logs/sync.log and that your API key "
                            "hasn't expired.", "data"))
    ref = min(today, date.fromisoformat(last))

    # -- HRV vs personal baseline
    h = _rows(conn, "SELECT calendar_date, last_night_avg_ms, baseline_balanced_low_ms, baseline_balanced_upper_ms, "
                    "status, weekly_avg_ms FROM hrv WHERE last_night_avg_ms IS NOT NULL "
                    "ORDER BY calendar_date DESC LIMIT 1")
    hrv_ok = None
    if h and (ref - date.fromisoformat(h[0][0])).days <= 2:
        d, v, lo, hi, status, wk = h[0]
        status = (status or "").upper()
        if status in ("LOW", "POOR") or (lo and wk and wk < lo):
            alerts.append(Alert("warning", "hrv_low", "HRV below your baseline",
                                f"7-day avg {wk} ms vs balanced range {lo}–{hi} ms (status {status.lower() or 'n/a'}). "
                                "Often a sign of fatigue, illness or stress.", "recovery"))
            hrv_ok = False
        elif status == "UNBALANCED":
            alerts.append(Alert("info", "hrv_unbalanced", "HRV unbalanced",
                                f"Last night {v} ms; 7-day avg {wk} ms is outside {lo}–{hi} ms.", "recovery"))
            hrv_ok = False
        elif lo and v < lo:
            alerts.append(Alert("info", "hrv_dip", "HRV dipped last night",
                                f"{v} ms vs balanced floor {lo} ms; weekly average still balanced ({wk} ms).",
                                "recovery"))
            hrv_ok = True
        else:
            hrv_ok = True

    # -- resting HR elevated vs 30-day median
    r = _rows(conn, "SELECT calendar_date, rhr_bpm FROM daily_summary WHERE rhr_bpm IS NOT NULL AND calendar_date "
                    "BETWEEN ? AND ? ORDER BY calendar_date", ((ref - timedelta(days=33)).isoformat(), ref.isoformat()))
    rhr_ok = None
    if len(r) >= 14:
        recent = [x[1] for x in r[-3:]]
        base = median([x[1] for x in r[:-3]])
        diff = sum(recent) / len(recent) - base
        rhr_ok = diff < 3
        if diff >= 5:
            alerts.append(Alert("critical", "rhr_high", "Resting HR well above normal",
                                f"3-day avg {sum(recent)/len(recent):.0f} bpm vs 30-day median {base:.0f} bpm "
                                f"(+{diff:.0f}).", "recovery"))
        elif diff >= 3:
            alerts.append(Alert("warning", "rhr_up", "Resting HR elevated",
                                f"3-day avg {sum(recent)/len(recent):.0f} bpm vs 30-day median {base:.0f} bpm "
                                f"(+{diff:.0f}).", "recovery"))

    # -- sleep debt vs Garmin sleep need
    s = _rows(conn, "SELECT calendar_date, sleep_s, sleep_score, json_extract(raw_json,'$.sleepNeed.actual') "
                    "FROM sleep WHERE sleep_s IS NOT NULL AND calendar_date BETWEEN ? AND ? ORDER BY calendar_date",
              ((ref - timedelta(days=6)).isoformat(), ref.isoformat()))
    if len(s) >= 4:
        need = [x[3] or DEFAULT_SLEEP_NEED_MIN for x in s]
        got = [x[1] / 60 for x in s]
        debt = sum(n - g for n, g in zip(need, got))
        avg_got = sum(got) / len(got)
        if debt >= 5 * 60:
            alerts.append(Alert("warning", "sleep_debt", f"Sleep debt {_hm(debt)} this week",
                                f"Averaging {_hm(avg_got)} over {len(s)} nights vs Garmin sleep need "
                                f"~{_hm(sum(need)/len(need))}.", "sleep"))
        elif debt >= 2 * 60:
            alerts.append(Alert("info", "sleep_short", f"Slightly short on sleep ({_hm(debt)})",
                                f"Averaging {_hm(avg_got)} over {len(s)} nights.", "sleep"))
        last_s = s[-1]
        if last_s[2] is not None and last_s[2] < 60 and (ref - date.fromisoformat(last_s[0])).days <= 1:
            alerts.append(Alert("warning", "sleep_poor", f"Poor sleep last night (score {last_s[2]})",
                                f"{_hm(last_s[1] / 60)} asleep.", "sleep"))

    # -- training load spike / drop (acute:chronic workload ratio)
    tl = training_load(conn, ref, days=1)
    if tl:
        t = tl[-1]
        if t["chronic_weekly"] and t["chronic_weekly"] >= 50:
            if t["acwr"] >= 1.5:
                alerts.append(Alert("warning", "load_spike", f"Training load spike (ratio {t['acwr']:.2f})",
                                    f"Last 7 days {t['acute_7d']:.0f} vs 4-week weekly avg {t['chronic_weekly']:.0f}. "
                                    "Ratios above 1.5 are associated with higher injury risk.", "training"))
            elif t["acwr"] >= 1.3:
                alerts.append(Alert("info", "load_rising", f"Training load rising (ratio {t['acwr']:.2f})",
                                    f"Last 7 days {t['acute_7d']:.0f} vs weekly avg {t['chronic_weekly']:.0f}.",
                                    "training"))
            elif t["acwr"] < 0.6:
                alerts.append(Alert("info", "load_drop", f"Training load dropping (ratio {t['acwr']:.2f})",
                                    f"Last 7 days {t['acute_7d']:.0f} vs weekly avg {t['chronic_weekly']:.0f}.",
                                    "training"))

    # -- inactivity
    la = _rows(conn, "SELECT max(calendar_date) FROM activity")[0][0]
    if la:
        gap = (ref - date.fromisoformat(la)).days
        if gap >= 5:
            alerts.append(Alert("info", "inactive", f"No workouts for {gap} days",
                                f"Last recorded workout on {la}.", "training"))

    # -- stress & Body Battery
    st = _rows(conn, "SELECT avg(stress_avg), min(bb_max) FROM (SELECT stress_avg, bb_max FROM daily_summary "
                     "WHERE calendar_date BETWEEN ? AND ? )", ((ref - timedelta(days=2)).isoformat(), ref.isoformat()))
    if st and st[0][0] is not None and st[0][0] >= 50:
        alerts.append(Alert("warning", "stress_high", f"High stress ({st[0][0]:.0f} avg, 3 days)",
                            "Garmin stress above 50 means medium-to-high stress through the day.", "recovery"))
    bb = _rows(conn, "SELECT calendar_date, bb_max FROM daily_summary WHERE bb_max IS NOT NULL "
                     "ORDER BY calendar_date DESC LIMIT 1")
    if bb and bb[0][1] < 50 and (ref - date.fromisoformat(bb[0][0])).days <= 1:
        alerts.append(Alert("warning", "bb_low", f"Low recharge (Body Battery peak {bb[0][1]})",
                            "Your battery didn't recharge above 50 overnight.", "recovery"))

    if not any(a.severity in ("critical", "warning") for a in alerts) and hrv_ok and rhr_ok is not False:
        alerts.append(Alert("good", "recovered", "Recovery on track",
                            "HRV is within your balanced range and resting HR is at baseline.", "recovery"))

    alerts.sort(key=lambda a: SEVERITY_ORDER[a.severity])
    return alerts


def alerts_json(conn: sqlite3.Connection, today: date | None = None) -> str:
    return json.dumps([a.as_dict() for a in compute_alerts(conn, today)])


if __name__ == "__main__":  # quick look: uv run python -m garmin.insights
    from db.db import connect

    from . import config

    for a in compute_alerts(connect(config.DB_PATH, readonly=True)):
        print(f"[{a.severity:8}] {a.title} — {a.detail}")
    print(datetime.now().isoformat(timespec="seconds"))
