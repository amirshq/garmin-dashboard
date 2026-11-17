"""Incremental sync + backfill from the Personal API into SQLite.

    uv run python -m garmin.sync                 # incremental (default)
    uv run python -m garmin.sync --backfill      # walk back until history runs out
    uv run python -m garmin.sync --backfill --since 2022-01-01
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Callable

from db.db import connect, upsert

from . import client as c
from . import config
from .fit import parse_fit, save_activity_file

log = logging.getLogger("garmin.sync")

OVERLAP_DAYS = 3  # re-fetch recent days: Garmin revises sleep/HRV/summaries after the fact
FIRST_SYNC_DAYS = 30  # incremental sync with no state starts this far back
HARD_FLOOR = date(2005, 1, 1)
INTRADAY_DAYS = 90  # backfill depth for one-call-per-day endpoints (intraday HR, SpO2)
LOCK_PATH = config.PROJECT_ROOT / "data" / ".sync.lock"


def _j(d: dict) -> str:
    return json.dumps(d, separators=(",", ":"), default=str)


# ---------------------------------------------------------------- row mappers
def daily_row(m, raw) -> dict:
    mv, hr, st, bb = m.movement, m.heartRate, m.stress, m.bodyBattery
    im, cal, ox, rp = m.intensityMinutes, m.calories, m.pulseOx, m.respiration
    steps = mv.steps if mv else None
    return {
        "calendar_date": m.calendarDate.isoformat(),
        "steps": steps.value if steps else None,
        "steps_goal": steps.goal if steps else None,
        "distance_m": steps.distanceInMeters if steps else None,
        "floors_up": mv.floorsAscended.value if mv and mv.floorsAscended else None,
        "rhr_bpm": hr.restingValue if hr else None,
        "hr_min_bpm": hr.minValue if hr else None,
        "hr_max_bpm": hr.maxValue if hr else None,
        "stress_avg": st.avgLevel if st else None,
        "stress_max": st.maxLevel if st else None,
        "stress_qualifier": st.qualifier if st else None,
        "bb_min": bb.minValue if bb else None,
        "bb_max": bb.maxValue if bb else None,
        "bb_charged": bb.chargedValue if bb else None,
        "bb_drained": bb.drainedValue if bb else None,
        "im_moderate_min": im.moderate if im else None,
        "im_vigorous_min": im.vigorous if im else None,
        "cal_total_kcal": cal.burnedTotal if cal else None,
        "cal_active_kcal": cal.burnedActive if cal else None,
        "cal_resting_kcal": cal.burnedResting if cal else None,
        "spo2_avg_pct": ox.avgValue if ox else None,
        "resp_avg_brpm": rp.avgValue if rp else None,
        "raw_json": _j(raw),
    }


def _ts(v):
    if isinstance(v, (int, float)):  # epoch ms
        return datetime.fromtimestamp(v / 1000, UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    return v


def sleep_row(m, raw) -> dict:
    return {
        "calendar_date": m.calendarDate.isoformat(),
        "sleep_s": m.sleepTimeSeconds,
        "deep_s": m.deepSleepSeconds,
        "light_s": m.lightSleepSeconds,
        "rem_s": m.remSleepSeconds,
        "awake_s": m.awakeSleepSeconds,
        "nap_s": m.napTimeSeconds,
        "sleep_score": m.overall_score,
        "start_gmt": _ts(m.sleepStartTimestampGMT),
        "end_gmt": _ts(m.sleepEndTimestampGMT),
        "avg_hr_bpm": m.avgHeartRate,
        "avg_resp_brpm": m.averageRespirationValue,
        "avg_spo2_pct": m.averageSpO2Value,
        "avg_sleep_stress": m.avgSleepStress,
        "awake_count": m.awakeCount,
        "raw_json": _j(raw),
    }


def hrv_row(m, raw) -> dict:
    b = m.baseline
    return {
        "calendar_date": m.calendarDate.isoformat(),
        "last_night_avg_ms": m.lastNightAvg,
        "last_night_5min_high_ms": m.lastNight5MinHigh,
        "weekly_avg_ms": m.weeklyAvg,
        "baseline_low_upper_ms": b.lowUpper if b else None,
        "baseline_balanced_low_ms": b.balancedLow if b else None,
        "baseline_balanced_upper_ms": b.balancedUpper if b else None,
        "status": m.status,
        "raw_json": _j(raw),
    }


def activity_row(m, raw) -> dict:
    start_local = m.startTimeLocal
    return {
        "activity_id": m.activityId,
        "name": m.activityName,
        "type_key": m.activityType.typeKey if m.activityType else None,
        "start_local": start_local,
        "start_gmt": m.startTimeGMT,
        "calendar_date": start_local[:10] if start_local else None,
        "duration_s": m.duration,
        "moving_s": m.movingDuration,
        "distance_m": m.distance,
        "elev_gain_m": m.elevationGain,
        "avg_speed_mps": m.averageSpeed,
        "avg_hr_bpm": m.averageHR,
        "max_hr_bpm": m.maxHR,
        "calories_kcal": m.calories,
        "avg_power_w": m.avgPower,
        "training_load": m.activityTrainingLoad,
        "aerobic_te": m.aerobicTrainingEffect,
        "anaerobic_te": m.anaerobicTrainingEffect,
        "vo2max": m.vO2MaxValue,
        "raw_json": _j(raw),
    }


# ---------------------------------------------------------------- v2 mappers (raw dict in, row out)
def _epoch_date(ms) -> str | None:
    return datetime.fromtimestamp(ms / 1000, UTC).date().isoformat() if isinstance(ms, (int, float)) else None


def _kg(v):
    """Weight fields are documented as kg, but Garmin commonly returns grams; normalise."""
    if not isinstance(v, (int, float)):
        return None
    return round(v / 1000, 3) if v > 500 else float(v)


def weight_row(_m, raw) -> dict:
    return {
        "sample_pk": raw.get("samplePk") or int(raw.get("date") or 0),
        "calendar_date": raw.get("calendarDate") or _epoch_date(raw.get("date")),
        "timestamp_gmt": _ts(raw.get("timestampGMT")) if isinstance(raw.get("timestampGMT"), (int, float)) else raw.get("timestampGMT"),
        "weight_kg": _kg(raw.get("weight")),
        "bmi": raw.get("bmi"),
        "body_fat_pct": raw.get("bodyFat"),
        "body_water_pct": raw.get("bodyWater"),
        "muscle_mass_kg": _kg(raw.get("muscleMass")),
        "bone_mass_kg": _kg(raw.get("boneMass")),
        "source_type": raw.get("sourceType"),
        "raw_json": _j(raw),
    }


def _columns(descriptors: list[dict] | None, idx_key: str, name_key: str) -> dict[str, int]:
    return {d.get(name_key): d.get(idx_key) for d in descriptors or [] if d.get(name_key) is not None}


def body_battery_row(_m, raw) -> dict:
    cols = _columns(raw.get("bodyBatteryValueDescriptorDTOList"), "bodyBatteryValueDescriptorIndex",
                    "bodyBatteryValueDescriptorKey")
    ti, li = cols.get("timestamp", 0), cols.get("bodyBatteryLevel", 1)
    vals = [[r[ti], r[li]] for r in raw.get("bodyBatteryValuesArray") or []
            if len(r) > max(ti, li) and r[li] is not None]
    events = [{"type": e.get("eventType"), "start_gmt": e.get("eventStartTimeGmt"),
               "duration_min": round((e.get("durationInMilliseconds") or 0) / 60000),
               "impact": e.get("bodyBatteryImpact"), "feedback": e.get("shortFeedback")}
              for e in raw.get("bodyBatteryActivityEvent") or []]
    return {
        "calendar_date": raw.get("date"),
        "charged": raw.get("charged"),
        "drained": raw.get("drained"),
        "level_start": vals[0][1] if vals else None,
        "level_end": vals[-1][1] if vals else None,
        "values_json": _j(vals),
        "events_json": _j(events),
        "raw_json": _j(raw),
    }


def hr_intraday_row(_m, raw) -> dict:
    cols = _columns(raw.get("heartRateValueDescriptors"), "index", "key")
    ti, hi = cols.get("timestamp", 0), cols.get("heartrate", 1)
    vals = [[r[ti], r[hi]] for r in raw.get("heartRateValues") or [] if len(r) > max(ti, hi) and r[hi] is not None]
    return {
        "calendar_date": raw.get("calendarDate"),
        "resting_bpm": raw.get("restingHeartRate"),
        "min_bpm": raw.get("minHeartRate"),
        "max_bpm": raw.get("maxHeartRate"),
        "rhr_7d_avg_bpm": raw.get("lastSevenDaysAvgRestingHeartRate"),
        "n_samples": len(vals),
        "values_json": _j(vals),
        "raw_json": _j(raw),
    }


def spo2_row(_m, raw) -> dict:
    cols = _columns(raw.get("spO2HourlyAveragesDescriptorList"), "spo2ValueDescIndex", "spo2ValueDescKey")
    ti, vi = cols.get("timestamp", 0), cols.get("spo2Level", 1)
    hourly = [[r[ti], r[vi]] for r in raw.get("spO2HourlyAverages") or [] if len(r) > max(ti, vi) and r[vi] is not None]
    return {
        "calendar_date": raw.get("calendarDate"),
        "avg_pct": raw.get("averageSpO2"),
        "lowest_pct": raw.get("lowestSpO2"),
        "avg_sleep_pct": raw.get("avgSleepSpO2"),
        "latest_pct": raw.get("latestSpO2"),
        "avg_7d_pct": raw.get("lastSevenDaysAvgSpO2"),
        "hourly_json": _j(hourly),
        "raw_json": _j(raw),
    }


def fitness_age_row(as_of: date, raw: dict) -> dict:
    return {
        "as_of_date": as_of.isoformat(),
        "chronological_age": raw.get("chronologicalAge"),
        "fitness_age": raw.get("fitnessAge"),
        "achievable_fitness_age": raw.get("achievableFitnessAge"),
        "previous_fitness_age": raw.get("previousFitnessAge"),
        "last_updated": raw.get("lastUpdated"),
        "raw_json": _j(raw),
    }


def _per_day(fn: Callable[[c.GarminClient, date], dict | None]):
    """Adapt a one-call-per-day endpoint to the (client, start, end) window interface."""
    def fetch(cl, a: date, b: date):
        out, d = [], a
        while d <= b:
            raw = fn(cl, d)
            if raw and raw.get("calendarDate"):
                out.append((None, raw))
            d += timedelta(days=1)
        return out
    return fetch


def _raw_pairs(fn):
    return lambda cl, a, b: [(None, r) for r in fn(cl, a, b)]


def _daily_has_data(r: dict) -> bool:
    return any(r.get(k) is not None for k in ("steps", "rhr_bpm", "stress_avg", "bb_max"))


@dataclass(frozen=True)
class DataType:
    name: str
    table: str
    key: str
    chunk_days: int
    empty_windows_to_stop: int  # backfill stops after this many consecutive empty windows
    fetch: Callable[[c.GarminClient, date, date], list]
    to_row: Callable
    has_data: Callable[[dict], bool] = lambda r: True
    max_history_days: int | None = None  # cap backfill depth (per-day endpoints are rate-limit heavy)


DATA_TYPES: dict[str, DataType] = {
    "daily_summary": DataType("daily_summary", "daily_summary", "calendar_date",
                              c.MAX_DAYS_DAILY_SUMMARY, 2,
                              lambda cl, a, b: cl.daily_summaries(a, b), daily_row, _daily_has_data),
    "sleep": DataType("sleep", "sleep", "calendar_date", c.MAX_DAYS_SLEEP, 12,
                      lambda cl, a, b: cl.sleep_summaries(a, b), sleep_row,
                      lambda r: bool(r.get("sleep_s"))),
    "hrv": DataType("hrv", "hrv", "calendar_date", c.MAX_DAYS_HRV, 12,
                    lambda cl, a, b: cl.hrv_daily(a, b), hrv_row,
                    lambda r: r.get("last_night_avg_ms") is not None),
    "activity": DataType("activity", "activity", "activity_id", 366, 2,
                         lambda cl, a, b: cl.activities(a, b), activity_row),
    "weight": DataType("weight", "weight", "sample_pk", 366, 3,
                       _raw_pairs(lambda cl, a, b: cl.weight_range(a, b)), weight_row),
    "body_battery": DataType("body_battery", "body_battery_day", "calendar_date", 31, 12,
                             _raw_pairs(lambda cl, a, b: cl.body_battery_reports(a, b)), body_battery_row,
                             lambda r: r.get("level_end") is not None),
    "hr_intraday": DataType("hr_intraday", "hr_intraday", "calendar_date", 7, 4,
                            _per_day(lambda cl, d: cl.daily_heart_rate(d)), hr_intraday_row,
                            lambda r: (r.get("n_samples") or 0) > 0, max_history_days=INTRADAY_DAYS),
    "spo2": DataType("spo2", "spo2_day", "calendar_date", 7, 4,
                     _per_day(lambda cl, d: cl.spo2_acclimation(d)), spo2_row,
                     lambda r: r.get("avg_pct") is not None, max_history_days=INTRADAY_DAYS),
}


# ---------------------------------------------------------------- state
def get_state(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sync_state WHERE data_type=?", (name,)).fetchone()


def update_state(conn, name, *, newest: date | None = None, oldest: date | None = None,
                 backfill_complete: bool | None = None) -> None:
    conn.execute("INSERT OR IGNORE INTO sync_state(data_type) VALUES (?)", (name,))
    if newest:
        conn.execute(
            "UPDATE sync_state SET last_synced_date = max(coalesce(last_synced_date,''), ?) WHERE data_type=?",
            (newest.isoformat(), name))
    if oldest:
        conn.execute(
            "UPDATE sync_state SET oldest_synced_date = min(coalesce(oldest_synced_date,'9999'), ?) WHERE data_type=?",
            (oldest.isoformat(), name))
    if backfill_complete is not None:
        conn.execute("UPDATE sync_state SET backfill_complete=? WHERE data_type=?",
                     (int(backfill_complete), name))
    conn.execute("UPDATE sync_state SET updated_at=datetime('now') WHERE data_type=?", (name,))
    conn.commit()


# ---------------------------------------------------------------- core
def sync_window(cl, conn, dt: DataType, start: date, end: date) -> tuple[int, int]:
    """Fetch [start, end] and upsert. Returns (rows_upserted, rows_with_data)."""
    rows = [dt.to_row(m, raw) for m, raw in dt.fetch(cl, start, end)]
    n = upsert(conn, dt.table, dt.key, rows)
    conn.commit()
    return n, sum(1 for r in rows if dt.has_data(r))


def _windows_forward(start: date, end: date, size: int):
    cur = start
    while cur <= end:
        stop = min(end, cur + timedelta(days=size - 1))
        yield cur, stop
        cur = stop + timedelta(days=1)


def incremental(cl, conn, dt: DataType, today: date) -> int:
    st = get_state(conn, dt.name)
    if st and st["last_synced_date"]:
        start = date.fromisoformat(st["last_synced_date"]) - timedelta(days=OVERLAP_DAYS)
    else:
        start = today - timedelta(days=FIRST_SYNC_DAYS)
    total = 0
    for a, b in _windows_forward(start, today, dt.chunk_days):
        n, _ = sync_window(cl, conn, dt, a, b)
        total += n
        update_state(conn, dt.name, newest=b, oldest=a)
        log.info("%-13s %s..%s  %d rows", dt.name, a, b, n)
    return total


def backfill(cl, conn, dt: DataType, today: date, since: date | None) -> int:
    floor = max(since or HARD_FLOOR, HARD_FLOOR)
    if dt.max_history_days:
        floor = max(floor, today - timedelta(days=dt.max_history_days - 1))
    st = get_state(conn, dt.name)
    if st and st["backfill_complete"] and since is None:
        log.info("%-13s backfill already complete (oldest %s)", dt.name, st["oldest_synced_date"])
        return 0
    frontier = date.fromisoformat(st["oldest_synced_date"]) if st and st["oldest_synced_date"] else today + timedelta(days=1)
    if since and frontier <= since:
        log.info("%-13s already covered back to %s", dt.name, frontier)
        return 0
    total, empty = 0, 0
    end = frontier - timedelta(days=1)
    while end >= floor:
        start = max(floor, end - timedelta(days=dt.chunk_days - 1))
        try:
            n, with_data = sync_window(cl, conn, dt, start, end)
        except c.GarminAPIError as e:
            if e.status in (400, 404):
                log.warning("%-13s %s..%s rejected (%s); treating as empty", dt.name, start, end, e.status)
                n, with_data = 0, 0
            else:
                raise
        total += n
        update_state(conn, dt.name, oldest=start, newest=end)
        log.info("%-13s backfill %s..%s  %d rows (%d with data)", dt.name, start, end, n, with_data)
        empty = 0 if with_data else empty + 1
        if empty >= dt.empty_windows_to_stop:
            log.info("%-13s %d empty windows in a row; history exhausted", dt.name, empty)
            update_state(conn, dt.name, backfill_complete=since is None)
            return total
        end = start - timedelta(days=1)
    update_state(conn, dt.name, backfill_complete=since is None)
    return total


def sync_activity_extras(cl, conn, fit: bool = True, limit: int | None = None) -> tuple[int, int]:
    """Fetch detail JSON and original files for activities missing them."""
    q = "SELECT activity_id FROM activity WHERE detail_json IS NULL ORDER BY start_gmt DESC"
    ids = [r[0] for r in conn.execute(q + (f" LIMIT {int(limit)}" if limit else ""))]
    details = 0
    for aid in ids:
        try:
            d = cl.activity_detail(aid)
        except c.GarminAPIError as e:
            log.warning("detail %s failed: %s", aid, e)
            continue
        conn.execute("UPDATE activity SET detail_json=? WHERE activity_id=?", (_j(d or {}), aid))
        conn.commit()
        details += 1
    files = 0
    if fit:
        q = ("SELECT a.activity_id FROM activity a LEFT JOIN activity_fit f USING(activity_id) "
             "WHERE f.activity_id IS NULL OR f.status = 'error' ORDER BY a.start_gmt DESC")
        for (aid,) in conn.execute(q + (f" LIMIT {int(limit)}" if limit else "")).fetchall():
            row: dict = {"activity_id": aid}
            try:
                path = save_activity_file(aid, cl.download_activity_file(aid))
                row["path"] = str(path.relative_to(config.PROJECT_ROOT)) if path.is_relative_to(config.PROJECT_ROOT) else str(path)
                if path.suffix == ".fit":
                    row |= parse_fit(path) | {"status": "ok"}
                else:
                    row["status"] = "not_fit"
            except Exception as e:  # keep going; record the failure
                log.warning("file %s failed: %s", aid, e)
                row |= {"status": "error", "error": str(e)[:500]}
            upsert(conn, "activity_fit", "activity_id", [row])
            conn.commit()
            files += 1
    return details, files


def sync_snapshots(cl, conn, today: date, do_backfill: bool) -> int:
    """Point-in-time data: HR zones, user settings, Fitness Age (today; monthly history on backfill)."""
    n = 0
    for kind, fn in (("hr_zones", cl.heart_rate_zones), ("user_settings", cl.user_settings)):
        try:
            upsert(conn, "profile_snapshot", "kind", [{"kind": kind, "raw_json": _j(fn())}])
            n += 1
        except c.GarminAPIError as e:
            log.warning("snapshot %s failed: %s", kind, e)
    dates = [today]
    if do_backfill:
        lo = conn.execute("SELECT min(calendar_date) FROM daily_summary").fetchone()[0]
        have = {r[0] for r in conn.execute("SELECT as_of_date FROM fitness_age")}
        d = date.fromisoformat(lo).replace(day=1) if lo else today.replace(day=1)
        while d < today:
            if d.isoformat() not in have:
                dates.append(d)
            d = (d + timedelta(days=32)).replace(day=1)
    for d in dates:
        try:
            raw = cl.fitness_age(d)
        except c.GarminAPIError as e:
            log.warning("fitness age %s failed: %s", d, e)
            continue
        if raw and raw.get("fitnessAge") is not None:
            upsert(conn, "fitness_age", "as_of_date", [fitness_age_row(d, raw)])
            n += 1
    conn.commit()
    log.info("snapshots    %d rows (hr zones, settings, fitness age x%d)", n, len(dates))
    return n


class SyncLocked(RuntimeError):
    pass


def _acquire_lock():
    import fcntl
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise SyncLocked("another sync is already running")
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def run(types: list[str], do_backfill: bool, since: date | None, fit: bool,
        cl: c.GarminClient | None = None, conn: sqlite3.Connection | None = None,
        today: date | None = None, snapshots: bool = True) -> dict[str, int]:
    today = today or date.today()
    own_client = cl is None
    cl = cl or c.GarminClient()
    conn = conn or connect(config.DB_PATH)
    results: dict[str, int] = {}
    try:
        for name in types:
            dt = DATA_TYPES[name]
            results[name] = incremental(cl, conn, dt, today)
            if do_backfill:
                results[name] += backfill(cl, conn, dt, today, since)
        if "activity" in types:
            d, f = sync_activity_extras(cl, conn, fit=fit)
            results["activity_details"], results["activity_files"] = d, f
        if snapshots:
            results["snapshots"] = sync_snapshots(cl, conn, today, do_backfill)
    finally:
        if own_client:
            cl.close()
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backfill", action="store_true", help="also walk backwards through history")
    p.add_argument("--since", type=date.fromisoformat, help="backfill floor, YYYY-MM-DD")
    p.add_argument("--types", nargs="+", choices=list(DATA_TYPES), default=list(DATA_TYPES))
    p.add_argument("--no-fit", action="store_true", help="skip activity file downloads")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        lock = _acquire_lock()
    except SyncLocked as e:
        log.warning("%s; exiting", e)
        return 0
    try:
        res = run(a.types, a.backfill, a.since, not a.no_fit)
    except Exception:
        log.exception("sync failed")
        return 1
    finally:
        lock.close()
    log.info("done: %s", res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
