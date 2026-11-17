"""Read-only MCP server over the local Garmin SQLite database (stdio transport).

Never calls the Garmin API; it only reads what `make sync` stored.
Built on the official `mcp` Python SDK (v2: FastMCP is now `MCPServer`).
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from garmin import config  # noqa: E402
from garmin import insights  # noqa: E402

MAX_ROWS = 1000

mcp = MCPServer(
    name="garmin-health",
    instructions=(
        "Read-only access to the user's personal Garmin data, synced daily into SQLite. "
        "Dates are the user's local calendar dates (YYYY-MM-DD). Units: durations in seconds "
        "unless a field says _min/_h, distances in meters, heart rate in bpm, HRV in milliseconds "
        "(RMSSD), stress and Body Battery on Garmin's 0-100 scales, SpO2 in percent, respiration "
        "in breaths/min. Missing values are null (device not worn / metric unsupported). "
        "Data may lag up to a day; check get_data_coverage if a recent date is missing."
    ),
)


# ---------------------------------------------------------------- helpers
def _conn() -> sqlite3.Connection:
    if not config.DB_PATH.exists():
        raise RuntimeError(f"Database not found at {config.DB_PATH}. Run `make sync` first.")
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _rows(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _d(s: str) -> str:
    return date.fromisoformat(s).isoformat()  # validates YYYY-MM-DD


def _range(start: str, end: str) -> tuple[str, str]:
    a, b = _d(start), _d(end)
    if a > b:
        raise ValueError(f"start {a} is after end {b}")
    return a, b


# ---------------------------------------------------------------- tools
@mcp.tool()
def get_daily_summary(start: str, end: str) -> list[dict]:
    """Daily wellness summaries for each date in [start, end] (inclusive, YYYY-MM-DD).

    Fields per day:
      steps (count), steps_goal (count), distance_m (meters walked/run, all-day),
      floors_up (floors climbed), rhr_bpm (resting heart rate, beats/min),
      hr_min_bpm / hr_max_bpm (min/max 2-minute avg HR, beats/min),
      stress_avg / stress_max (Garmin stress score 0-100; 0-25 rest, 26-50 low, 51-75 medium, 76-100 high),
      stress_qualifier (text), bb_min / bb_max (Body Battery 0-100),
      bb_charged / bb_drained (Body Battery points gained/lost that day),
      im_moderate_min / im_vigorous_min (intensity minutes; vigorous counts double toward the weekly goal),
      cal_total_kcal / cal_active_kcal / cal_resting_kcal (kilocalories),
      spo2_avg_pct (pulse-ox %, null if not enabled), resp_avg_brpm (breaths/min).
    """
    a, b = _range(start, end)
    return _rows(
        "SELECT calendar_date, steps, steps_goal, distance_m, floors_up, rhr_bpm, hr_min_bpm, hr_max_bpm, "
        "stress_avg, stress_max, stress_qualifier, bb_min, bb_max, bb_charged, bb_drained, "
        "im_moderate_min, im_vigorous_min, cal_total_kcal, cal_active_kcal, cal_resting_kcal, "
        "spo2_avg_pct, resp_avg_brpm FROM daily_summary WHERE calendar_date BETWEEN ? AND ? "
        "ORDER BY calendar_date", (a, b))


@mcp.tool()
def get_sleep(start: str, end: str) -> list[dict]:
    """Nightly sleep for each date in [start, end] (inclusive). calendar_date is the morning the sleep ended.

    Fields: sleep_h (total sleep, hours, excluding naps), deep_h / light_h / rem_h / awake_h (hours in each stage),
    nap_min (minutes), sleep_score (Garmin 0-100; 90+ excellent, 80-89 good, 60-79 fair, <60 poor),
    start_gmt / end_gmt (UTC timestamps of the sleep window), avg_hr_bpm (beats/min during sleep),
    avg_resp_brpm (breaths/min), avg_spo2_pct (%), avg_sleep_stress (0-100), awake_count (times woken).
    """
    a, b = _range(start, end)
    return _rows(
        "SELECT calendar_date, round(sleep_s/3600.0,2) AS sleep_h, round(deep_s/3600.0,2) AS deep_h, "
        "round(light_s/3600.0,2) AS light_h, round(rem_s/3600.0,2) AS rem_h, round(awake_s/3600.0,2) AS awake_h, "
        "round(nap_s/60.0) AS nap_min, sleep_score, start_gmt, end_gmt, avg_hr_bpm, avg_resp_brpm, "
        "avg_spo2_pct, avg_sleep_stress, awake_count FROM sleep WHERE calendar_date BETWEEN ? AND ? "
        "ORDER BY calendar_date", (a, b))


@mcp.tool()
def get_hrv_trend(days: int = 30) -> dict:
    """Overnight heart-rate variability for the last `days` days (default 30, max 3650).

    HRV values are RMSSD in milliseconds; higher relative to YOUR baseline is generally better recovery.
    Per day: last_night_avg_ms (average overnight HRV), last_night_5min_high_ms (highest 5-min average),
    weekly_avg_ms (7-day rolling average computed by Garmin), baseline_balanced_low_ms / baseline_balanced_upper_ms
    (the user's personal 'balanced' range), baseline_low_upper_ms (below this = LOW), status (BALANCED / UNBALANCED / LOW / POOR).
    Also returns a summary with mean, min, max and the latest status over the period.
    """
    days = max(1, min(int(days), 3650))
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = _rows(
        "SELECT calendar_date, last_night_avg_ms, last_night_5min_high_ms, weekly_avg_ms, "
        "baseline_balanced_low_ms, baseline_balanced_upper_ms, baseline_low_upper_ms, status "
        "FROM hrv WHERE calendar_date >= ? ORDER BY calendar_date", (since,))
    vals = [r["last_night_avg_ms"] for r in rows if r["last_night_avg_ms"] is not None]
    summary = {
        "nights_with_data": len(vals),
        "mean_last_night_avg_ms": round(sum(vals) / len(vals), 1) if vals else None,
        "min_ms": min(vals) if vals else None,
        "max_ms": max(vals) if vals else None,
        "latest": rows[-1] if rows else None,
    }
    return {"summary": summary, "days": rows}


@mcp.tool()
def list_activities(start: str, end: str, type: str | None = None) -> list[dict]:
    """Recorded workouts that started between [start, end] (inclusive local dates), newest first.

    Optional `type` filters by Garmin activity type key (e.g. running, trail_running, cycling,
    indoor_cycling, strength_training, walking, hiking, lap_swimming, yoga). Partial match allowed.
    Fields: activity_id, name, type_key, start_local, duration_min (minutes, timer time),
    distance_km (km), elev_gain_m (meters), avg_pace_min_per_km (for foot sports; minutes per km),
    avg_speed_kmh (km/h), avg_hr_bpm / max_hr_bpm (beats/min), calories_kcal,
    avg_power_w (watts), training_load (Garmin EPOC-based load; ~50-150 typical easy-moderate, 200+ hard),
    aerobic_te / anaerobic_te (Training Effect 0-5: 1 minor, 2 maintaining, 3 improving, 4 highly improving, 5 overreaching),
    vo2max (ml/kg/min estimate, if computed).
    """
    a, b = _range(start, end)
    sql = (
        "SELECT activity_id, name, type_key, start_local, round(duration_s/60.0,1) AS duration_min, "
        "round(distance_m/1000.0,2) AS distance_km, elev_gain_m, "
        "CASE WHEN avg_speed_mps>0 AND (type_key LIKE '%run%' OR type_key LIKE '%walk%' OR type_key LIKE '%hik%') "
        "  THEN round(1000.0/avg_speed_mps/60.0,2) END AS avg_pace_min_per_km, "
        "round(avg_speed_mps*3.6,1) AS avg_speed_kmh, avg_hr_bpm, max_hr_bpm, calories_kcal, avg_power_w, "
        "round(training_load,1) AS training_load, aerobic_te, anaerobic_te, vo2max "
        "FROM activity WHERE calendar_date BETWEEN ? AND ?")
    params: list[Any] = [a, b]
    if type:
        sql += " AND type_key LIKE ?"
        params.append(f"%{type.strip().lower()}%")
    return _rows(sql + " ORDER BY start_local DESC", tuple(params))


@mcp.tool()
def get_activity_detail(activity_id: int) -> dict:
    """Full detail for one activity: the summary row (units as in list_activities, raw SI units:
    duration_s seconds, distance_m meters, avg_speed_mps m/s), the parsed .FIT session (fit: sport,
    total_timer_s seconds, total_distance_m meters, avg/max HR bpm, avg_cadence steps-or-rpm per minute,
    total_ascent_m meters, n_records = number of per-second samples), the lap/split summaries from
    Garmin (splits), and the Garmin summaryDTO with all computed metrics (summary)."""
    rows = _rows("SELECT * FROM activity WHERE activity_id=?", (int(activity_id),))
    if not rows:
        return {"error": f"activity {activity_id} not found"}
    row = rows[0]
    detail = json.loads(row.pop("detail_json") or "{}")
    row.pop("raw_json", None)
    fit = _rows("SELECT * FROM activity_fit WHERE activity_id=?", (int(activity_id),))
    return {
        "activity": row,
        "fit": fit[0] if fit else None,
        "summary": detail.get("summaryDTO"),
        "splits": detail.get("splitSummaries"),
    }


# metric -> (table, SQL expression, aggregation, unit)
METRICS: dict[str, tuple[str, str, str, str]] = {
    "steps": ("daily_summary", "steps", "avg", "steps/day"),
    "resting_hr": ("daily_summary", "rhr_bpm", "avg", "bpm"),
    "stress": ("daily_summary", "stress_avg", "avg", "Garmin stress 0-100"),
    "body_battery_max": ("daily_summary", "bb_max", "avg", "0-100"),
    "body_battery_min": ("daily_summary", "bb_min", "avg", "0-100"),
    "intensity_minutes": ("daily_summary", "coalesce(im_moderate_min,0)+2*coalesce(im_vigorous_min,0)", "avg",
                          "weighted min/day (vigorous x2)"),
    "active_calories": ("daily_summary", "cal_active_kcal", "avg", "kcal/day"),
    "sleep_hours": ("sleep", "sleep_s/3600.0", "avg", "hours/night"),
    "sleep_score": ("sleep", "sleep_score", "avg", "0-100"),
    "deep_sleep_hours": ("sleep", "deep_s/3600.0", "avg", "hours/night"),
    "rem_sleep_hours": ("sleep", "rem_s/3600.0", "avg", "hours/night"),
    "hrv": ("hrv", "last_night_avg_ms", "avg", "ms (RMSSD)"),
    "training_load": ("activity", "training_load", "sum", "total Garmin load"),
    "activity_count": ("activity", "1", "sum", "activities"),
    "activity_minutes": ("activity", "duration_s/60.0", "sum", "minutes"),
    "activity_distance_km": ("activity", "distance_m/1000.0", "sum", "km"),
}


def _period(p: str) -> tuple[str, str]:
    m = re.fullmatch(r"\s*(\d{4}-\d{2}-\d{2})\s*(?:\.\.|/|,| to )\s*(\d{4}-\d{2}-\d{2})\s*", p)
    if not m:
        raise ValueError(f"period must look like 'YYYY-MM-DD..YYYY-MM-DD', got {p!r}")
    return _range(m.group(1), m.group(2))


@mcp.tool()
def compare_periods(metric: str, period_a: str, period_b: str) -> dict:
    """Compare one metric between two date ranges. Periods are 'YYYY-MM-DD..YYYY-MM-DD' (inclusive).

    Metrics (unit / aggregation):
      steps (avg steps/day), resting_hr (avg bpm), stress (avg 0-100), body_battery_max / body_battery_min (avg 0-100),
      intensity_minutes (avg weighted min/day, vigorous x2), active_calories (avg kcal/day),
      sleep_hours / deep_sleep_hours / rem_sleep_hours (avg hours/night), sleep_score (avg 0-100),
      hrv (avg overnight RMSSD ms), training_load (SUM of activity loads), activity_count (SUM),
      activity_minutes (SUM minutes), activity_distance_km (SUM km).
    Returns value for each period, days_with_data, absolute and percent change (b vs a).
    For resting_hr and stress lower is usually better; for hrv, sleep and body battery higher is usually better.
    """
    if metric not in METRICS:
        return {"error": f"unknown metric {metric!r}", "available": sorted(METRICS)}
    table, expr, agg, unit = METRICS[metric]
    out: dict[str, Any] = {"metric": metric, "unit": unit, "aggregation": agg}
    for label, p in (("a", period_a), ("b", period_b)):
        s, e = _period(p)
        r = _rows(
            f"SELECT {agg}({expr}) AS v, count({expr}) AS n, count(DISTINCT calendar_date) AS d "
            f"FROM {table} WHERE calendar_date BETWEEN ? AND ?", (s, e))[0]
        out[f"period_{label}"] = {"range": f"{s}..{e}",
                                  "value": round(r["v"], 2) if r["v"] is not None else None,
                                  "days_with_data": r["d"]}
    va, vb = out["period_a"]["value"], out["period_b"]["value"]
    if va is not None and vb is not None:
        out["change"] = round(vb - va, 2)
        out["change_pct"] = round((vb - va) / va * 100, 1) if va else None
    return out


# ---------------------------------------------------------------- SQL
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|replace|drop|alter|create|attach|detach|pragma|vacuum|reindex|analyze|"
    r"begin|commit|rollback|savepoint|release|load_extension)\b", re.I)


def _strip_sql(q: str) -> str:
    q = re.sub(r"--[^\n]*", " ", q)
    q = re.sub(r"/\*.*?\*/", " ", q, flags=re.S)
    return q.strip().rstrip(";").strip()


def _authorizer(action, arg1, arg2, dbname, source):
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
               getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
    if action in allowed:
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in {"load_extension", "writefile", "readfile"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def validate_select(query: str) -> str:
    q = _strip_sql(query)
    if not q:
        raise ValueError("empty query")
    # Check structure/keywords outside string literals and quoted identifiers
    bare = re.sub(r"'(?:[^']|'')*'", "''", q)
    bare = re.sub(r'"(?:[^"]|"")*"', '""', bare)
    if ";" in bare:
        raise ValueError("only a single statement is allowed")
    if not re.match(r"^(select|with)\b", q, re.I):
        raise ValueError("only SELECT (or WITH ... SELECT) queries are allowed")
    if _FORBIDDEN.search(bare):
        raise ValueError("query contains a forbidden keyword; only read-only SELECT is allowed")
    return q


@mcp.tool()
def run_readonly_sql(query: str) -> dict:
    """Run a single read-only SELECT against the Garmin SQLite DB (max 1000 rows returned).

    Tables (dates are local 'YYYY-MM-DD' text):
      daily_summary(calendar_date PK, steps, steps_goal, distance_m, floors_up, rhr_bpm, hr_min_bpm, hr_max_bpm,
                    stress_avg, stress_max, stress_qualifier, bb_min, bb_max, bb_charged, bb_drained,
                    im_moderate_min, im_vigorous_min, cal_total_kcal, cal_active_kcal, cal_resting_kcal,
                    spo2_avg_pct, resp_avg_brpm, raw_json)
      sleep(calendar_date PK, sleep_s, deep_s, light_s, rem_s, awake_s, nap_s [all seconds], sleep_score 0-100,
            start_gmt, end_gmt, avg_hr_bpm, avg_resp_brpm, avg_spo2_pct, avg_sleep_stress, awake_count, raw_json)
      hrv(calendar_date PK, last_night_avg_ms, last_night_5min_high_ms, weekly_avg_ms, baseline_low_upper_ms,
          baseline_balanced_low_ms, baseline_balanced_upper_ms, status, raw_json)
      activity(activity_id PK, name, type_key, start_local, start_gmt, calendar_date, duration_s, moving_s,
               distance_m, elev_gain_m, avg_speed_mps [m/s], avg_hr_bpm, max_hr_bpm, calories_kcal, avg_power_w,
               training_load, aerobic_te, anaerobic_te, vo2max, raw_json, detail_json)
      activity_fit(activity_id PK, path, status, sport, sub_sport, total_timer_s, total_distance_m,
                   avg_hr_bpm, max_hr_bpm, avg_cadence, total_ascent_m, n_records)
      activity_ext = activity + hr_z1_s..hr_z5_s (seconds in HR zones 1-5), te_label, im_moderate_min,
                     im_vigorous_min, steps, location   [view]
      weight(sample_pk PK, calendar_date, timestamp_gmt, weight_kg, bmi, body_fat_pct, body_water_pct,
             muscle_mass_kg, bone_mass_kg, source_type)
      body_battery_day(calendar_date PK, charged, drained, level_start, level_end,
                       values_json [[epoch_ms, level 0-100]...], events_json [{type, start_gmt, duration_min, impact}])
      hr_intraday(calendar_date PK, resting_bpm, min_bpm, max_bpm, rhr_7d_avg_bpm, n_samples, values_json [[epoch_ms, bpm]...])
      spo2_day(calendar_date PK, avg_pct, lowest_pct, avg_sleep_pct, latest_pct, avg_7d_pct, hourly_json)
      fitness_age(as_of_date PK, chronological_age, fitness_age, achievable_fitness_age, previous_fitness_age)
      profile_snapshot(kind PK: 'hr_zones' | 'user_settings', raw_json)
      sync_state(data_type PK, last_synced_date, oldest_synced_date, backfill_complete)
    raw_json/detail_json hold the full Garmin API JSON; use json_extract(raw_json, '$.path') for fields not in columns.
    Writes, PRAGMA, ATTACH and multiple statements are rejected.
    """
    try:
        q = validate_select(query)
    except ValueError as e:
        return {"error": str(e)}
    with _conn() as conn:
        conn.set_authorizer(_authorizer)
        try:
            cur = conn.execute(q)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(MAX_ROWS + 1)
        except sqlite3.DatabaseError as e:
            return {"error": f"SQL error: {e}"}
    truncated = len(rows) > MAX_ROWS
    return {"columns": cols, "rows": [list(r) for r in rows[:MAX_ROWS]],
            "row_count": min(len(rows), MAX_ROWS), "truncated": truncated}


@mcp.tool()
def get_data_coverage() -> list[dict]:
    """Which date range is synced for each data type (daily_summary, sleep, hrv, activity),
    row counts, and when each was last synced (UTC). Use this to check whether data exists before analysing."""
    out = []
    for t, col in (("daily_summary", "calendar_date"), ("sleep", "calendar_date"),
                   ("hrv", "calendar_date"), ("activity", "calendar_date"), ("weight", "calendar_date"),
                   ("body_battery_day", "calendar_date"), ("hr_intraday", "calendar_date"),
                   ("spo2_day", "calendar_date"), ("fitness_age", "as_of_date")):
        r = _rows(f"SELECT count(*) AS rows, min({col}) AS first_date, max({col}) AS last_date, "
                  f"max(synced_at) AS last_synced_utc FROM {t}")[0]
        out.append({"data_type": t, **r})
    return out


@mcp.tool()
def get_alerts() -> list[dict]:
    """Current health alerts computed from the synced data, most severe first.

    severity: critical | warning | info | good. Rules (transparent, not medical advice):
    HRV 7-day avg below the user's Garmin balanced baseline; resting HR 3-day avg >= +3 bpm (warning) or
    +5 bpm (critical) over the 30-day median; weekly sleep debt vs Garmin's nightly sleep need (minutes);
    last-night sleep score < 60; acute:chronic training-load ratio (7-day load / 4-week weekly average)
    >= 1.5 spike, >= 1.3 rising, < 0.6 dropping; no workouts for 5+ days; 3-day stress avg >= 50;
    Body Battery peak < 50; data older than 2 days. Each alert's `detail` contains the triggering numbers.
    """
    with _conn() as conn:
        return [a.as_dict() for a in insights.compute_alerts(conn)]


@mcp.tool()
def get_training_load(days: int = 90) -> list[dict]:
    """Daily training load for the last `days` days (max 730), with acute_7d (sum of the last 7 days of
    Garmin activityTrainingLoad), chronic_weekly (28-day sum / 4, i.e. average weekly load) and
    acwr = acute_7d / chronic_weekly (0.8-1.3 is the commonly cited 'sweet spot'; > 1.5 = spike).
    Derived locally: the Personal API has no training-status endpoint."""
    days = max(1, min(int(days), 730))
    with _conn() as conn:
        end = conn.execute("SELECT max(calendar_date) FROM daily_summary").fetchone()[0]
        return insights.training_load(conn, date.fromisoformat(end) if end else date.today(), days)


def _downsample(values: list, minutes: int) -> list[dict]:
    from datetime import datetime as _dt, timezone as _tz
    buckets: dict[int, list[float]] = {}
    for ts, v in values:
        if v is None:
            continue
        buckets.setdefault(int(ts // (minutes * 60000)), []).append(float(v))
    return [{"time_utc": _dt.fromtimestamp(k * minutes * 60, _tz.utc).strftime("%Y-%m-%dT%H:%MZ"),
             "avg": round(sum(v) / len(v), 1), "min": min(v), "max": max(v)} for k, v in sorted(buckets.items())]


@mcp.tool()
def get_intraday(day: str, resolution_minutes: int = 30) -> dict:
    """Within-day curves for one date (YYYY-MM-DD), bucketed to `resolution_minutes` (5-240, default 30).

    heart_rate: bpm per bucket (avg/min/max) plus the day's resting/min/max and 7-day resting average.
    body_battery: level 0-100 per bucket, plus charged/drained totals and events (SLEEP, ACTIVITY, ...
    with duration_min and impact = Body Battery points gained/lost).
    spo2: hourly blood-oxygen % averages, daily avg / lowest / avg during sleep.
    Times are UTC. Intraday HR and SpO2 are kept for the most recent ~90 days.
    """
    d = _d(day)
    res = max(5, min(int(resolution_minutes), 240))
    out: dict[str, Any] = {"date": d}
    hr = _rows("SELECT resting_bpm, min_bpm, max_bpm, rhr_7d_avg_bpm, values_json FROM hr_intraday WHERE calendar_date=?", (d,))
    if hr:
        h = hr[0]
        out["heart_rate"] = {"resting_bpm": h["resting_bpm"], "min_bpm": h["min_bpm"], "max_bpm": h["max_bpm"],
                             "resting_7d_avg_bpm": h["rhr_7d_avg_bpm"],
                             "series": _downsample(json.loads(h["values_json"] or "[]"), res)}
    bb = _rows("SELECT charged, drained, level_start, level_end, values_json, events_json FROM body_battery_day "
               "WHERE calendar_date=?", (d,))
    if bb:
        b = bb[0]
        out["body_battery"] = {"charged": b["charged"], "drained": b["drained"], "start": b["level_start"],
                               "end": b["level_end"], "events": json.loads(b["events_json"] or "[]"),
                               "series": _downsample(json.loads(b["values_json"] or "[]"), max(res, 30))}
    sp = _rows("SELECT avg_pct, lowest_pct, avg_sleep_pct, hourly_json FROM spo2_day WHERE calendar_date=?", (d,))
    if sp:
        out["spo2"] = {"avg_pct": sp[0]["avg_pct"], "lowest_pct": sp[0]["lowest_pct"],
                       "avg_sleep_pct": sp[0]["avg_sleep_pct"],
                       "hourly": _downsample(json.loads(sp[0]["hourly_json"] or "[]"), 60)}
    if len(out) == 1:
        out["note"] = "no intraday data stored for this date"
    return out


@mcp.tool()
def get_body_metrics(start: str, end: str) -> dict:
    """Body composition and fitness age in [start, end].

    weigh_ins: weight_kg (kilograms), bmi, body_fat_pct (%), muscle_mass_kg, source_type.
    fitness_age: Garmin Fitness Age (years) as of each sampled date, vs chronological_age and the
    achievable_fitness_age; latest_components shows what drives it (resting HR bpm, vigorous days/week,
    vigorous minutes/week, BMI) with targets.
    hr_zones: the user's configured heart-rate zone floors (bpm) and max/resting HR used.
    """
    a, b = _range(start, end)
    w = _rows("SELECT calendar_date, timestamp_gmt, weight_kg, bmi, body_fat_pct, muscle_mass_kg, source_type "
              "FROM weight WHERE calendar_date BETWEEN ? AND ? ORDER BY timestamp_gmt", (a, b))
    fa = _rows("SELECT as_of_date, chronological_age, fitness_age, achievable_fitness_age FROM fitness_age "
               "WHERE as_of_date BETWEEN ? AND ? ORDER BY as_of_date", (a, b))
    latest = _rows("SELECT raw_json FROM fitness_age ORDER BY as_of_date DESC LIMIT 1")
    zones = _rows("SELECT raw_json FROM profile_snapshot WHERE kind='hr_zones'")
    return {"weigh_ins": w, "fitness_age": fa,
            "latest_components": json.loads(latest[0]["raw_json"]).get("components") if latest else None,
            "hr_zones": json.loads(zones[0]["raw_json"]) if zones else None}


def main() -> None:
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
