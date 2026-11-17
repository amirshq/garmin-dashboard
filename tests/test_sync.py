"""Sync idempotency + incremental/backfill behaviour, using a fake client."""

import json
from datetime import date, timedelta

import pytest

from db.db import connect
from garmin import sync
from garmin.models import Activity, DailySummary, HrvSummary, Sleep
from mcp_server import server as mcp_srv


class FakeClient:
    """Serves synthetic data for dates >= history_start; records calls."""

    def __init__(self, history_start: date, steps_bump: int = 0):
        self.history_start = history_start
        self.bump = steps_bump
        self.calls: list[tuple[str, date, date]] = []

    def _days(self, a, b):
        d = max(a, self.history_start)
        while d <= b:
            yield d
            d += timedelta(days=1)

    def daily_summaries(self, a, b):
        self.calls.append(("daily", a, b))
        out = []
        for d in self._days(a, b):
            raw = {"calendarDate": d.isoformat(), "movement": {"steps": {"value": 5000 + d.day + self.bump}},
                   "heartRate": {"restingValue": 50}}
            out.append((DailySummary.model_validate(raw), raw))
        return out

    def sleep_summaries(self, a, b):
        self.calls.append(("sleep", a, b))
        out = []
        for d in self._days(a, b):
            raw = {"calendarDate": d.isoformat(), "sleepTimeSeconds": 27000,
                   "sleepScores": {"overall": {"value": 80}}}
            out.append((Sleep.model_validate(raw), raw))
        return out

    def hrv_daily(self, a, b):
        self.calls.append(("hrv", a, b))
        out = []
        for d in self._days(a, b):
            raw = {"calendarDate": d.isoformat(), "lastNightAvg": 45, "status": "BALANCED"}
            out.append((HrvSummary.model_validate(raw), raw))
        return out

    def activities(self, a, b):
        self.calls.append(("activity", a, b))
        out = []
        for d in self._days(a, b):
            if d.day % 3 == 0:
                raw = {"activityId": int(d.strftime("%Y%m%d")), "activityName": "Run",
                       "activityType": {"typeKey": "running"}, "startTimeLocal": f"{d} 07:00:00",
                       "duration": 1800.0, "distance": 5000.0, "activityTrainingLoad": 80.0}
                out.append((Activity.model_validate(raw), raw))
        return out

    def activity_detail(self, aid):
        return {"activityId": aid, "summaryDTO": {"distance": 5000}}

    def download_activity_file(self, aid):
        raise RuntimeError("no files in tests")

    # v2 endpoints
    def weight_range(self, a, b):
        self.calls.append(("weight", a, b))
        return [{"samplePk": int(d.strftime("%Y%m%d")), "calendarDate": d.isoformat(), "weight": 80500.0,
                 "bmi": 24.1} for d in self._days(a, b) if d.day == 1]

    def body_battery_reports(self, a, b, chunks=48):
        self.calls.append(("bb", a, b))
        return [{"date": d.isoformat(), "charged": 60, "drained": 55,
                 "bodyBatteryValueDescriptorDTOList": [
                     {"bodyBatteryValueDescriptorIndex": 0, "bodyBatteryValueDescriptorKey": "timestamp"},
                     {"bodyBatteryValueDescriptorIndex": 1, "bodyBatteryValueDescriptorKey": "bodyBatteryLevel"}],
                 "bodyBatteryValuesArray": [[1, 30], [2, None], [3, 85], [4, 40]],
                 "bodyBatteryActivityEvent": [{"eventType": "SLEEP", "durationInMilliseconds": 28800000,
                                               "bodyBatteryImpact": 55, "shortFeedback": "GOOD"}]}
                for d in self._days(a, b)]

    def daily_heart_rate(self, d):
        self.calls.append(("hr", d, d))
        if d < self.history_start:
            return {"calendarDate": d.isoformat(), "heartRateValues": None}
        return {"calendarDate": d.isoformat(), "restingHeartRate": 55, "minHeartRate": 50, "maxHeartRate": 140,
                "heartRateValueDescriptors": [{"index": 0, "key": "timestamp"}, {"index": 1, "key": "heartrate"}],
                "heartRateValues": [[1, None], [2, 60], [3, 72]]}

    def spo2_acclimation(self, d):
        self.calls.append(("spo2", d, d))
        if d < self.history_start:
            return None
        return {"calendarDate": d.isoformat(), "averageSpO2": 96.0,
                "spO2HourlyAveragesDescriptorList": [{"spo2ValueDescIndex": 0, "spo2ValueDescKey": "timestamp"},
                                                     {"spo2ValueDescIndex": 1, "spo2ValueDescKey": "spo2Level"}],
                "spO2HourlyAverages": [[1, 95], [2, 97]]}

    def fitness_age(self, d):
        return {"chronologicalAge": 35, "fitnessAge": 31.5, "achievableFitnessAge": 29.0}

    def heart_rate_zones(self):
        return [{"sport": "DEFAULT", "zone1Floor": 95}]

    def user_settings(self):
        return {"measurementSystem": "metric"}


TODAY = date(2026, 3, 15)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def counts(conn):
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("daily_summary", "sleep", "hrv", "activity", "activity_fit")}


def test_incremental_is_idempotent(conn):
    fc = FakeClient(date(2025, 1, 1))
    types = list(sync.DATA_TYPES)
    sync.run(types, False, None, fit=True, cl=fc, conn=conn, today=TODAY)
    first = counts(conn)
    snap = conn.execute("SELECT calendar_date, steps, raw_json FROM daily_summary ORDER BY 1").fetchall()
    sync.run(types, False, None, fit=True, cl=fc, conn=conn, today=TODAY)
    sync.run(types, False, None, fit=True, cl=fc, conn=conn, today=TODAY)
    assert counts(conn) == first
    assert conn.execute("SELECT calendar_date, steps, raw_json FROM daily_summary ORDER BY 1").fetchall() == snap
    assert first["daily_summary"] == sync.FIRST_SYNC_DAYS + 1
    # failed file downloads are recorded (one row per activity) and retried on later runs
    assert conn.execute("SELECT count(*) FROM activity_fit WHERE status='error'").fetchone()[0] == first["activity"]


def test_incremental_resumes_from_state_with_overlap(conn):
    fc = FakeClient(date(2025, 1, 1))
    sync.run(["daily_summary"], False, None, fit=False, cl=fc, conn=conn, today=TODAY)
    fc.calls.clear()
    sync.run(["daily_summary"], False, None, fit=False, cl=fc, conn=conn, today=TODAY + timedelta(days=2))
    assert fc.calls == [("daily", TODAY - timedelta(days=sync.OVERLAP_DAYS), TODAY + timedelta(days=2))]


def test_upsert_updates_revised_values(conn):
    sync.run(["daily_summary"], False, None, fit=False, cl=FakeClient(date(2025, 1, 1)), conn=conn, today=TODAY)
    sync.run(["daily_summary"], False, None, fit=False, cl=FakeClient(date(2025, 1, 1), steps_bump=100),
             conn=conn, today=TODAY)
    row = conn.execute("SELECT steps, raw_json FROM daily_summary WHERE calendar_date=?",
                       (TODAY.isoformat(),)).fetchone()
    assert row[0] == 5000 + TODAY.day + 100
    assert json.loads(row[1])["movement"]["steps"]["value"] == row[0]


def test_backfill_stops_when_history_runs_out_and_is_idempotent(conn):
    history_start = date(2025, 11, 20)
    fc = FakeClient(history_start)
    sync.run(["sleep", "hrv"], True, None, fit=False, cl=fc, conn=conn, today=TODAY)
    n_days = (TODAY - history_start).days + 1
    assert counts(conn)["sleep"] == n_days
    st = sync.get_state(conn, "sleep")
    assert st["backfill_complete"] == 1
    # stopped after N empty windows rather than walking to 2005
    assert date.fromisoformat(st["oldest_synced_date"]) > date(2024, 1, 1)
    fc.calls.clear()
    sync.run(["sleep", "hrv"], True, None, fit=False, cl=fc, conn=conn, today=TODAY)
    assert counts(conn)["sleep"] == n_days
    # second backfill is a no-op beyond the incremental window
    assert all(a >= TODAY - timedelta(days=sync.OVERLAP_DAYS) for _, a, _ in fc.calls)


def test_backfill_since_floor(conn):
    fc = FakeClient(date(2020, 1, 1))
    sync.run(["daily_summary"], True, date(2025, 6, 1), fit=False, cl=fc, conn=conn, today=TODAY)
    lo = conn.execute("SELECT min(calendar_date) FROM daily_summary").fetchone()[0]
    assert lo == "2025-06-01"


def test_activity_details_fetched_once(conn):
    fc = FakeClient(date(2025, 1, 1))
    sync.run(["activity"], False, None, fit=False, cl=fc, conn=conn, today=TODAY)
    assert conn.execute("SELECT count(*) FROM activity WHERE detail_json IS NULL").fetchone()[0] == 0
    d, _ = sync.sync_activity_extras(fc, conn, fit=False)
    assert d == 0


# ---------------- MCP read-only SQL guard
@pytest.mark.parametrize("q", [
    "DELETE FROM sleep", "select 1; drop table sleep", "PRAGMA table_info(sleep)",
    "ATTACH DATABASE 'x' AS y", "WITH x AS (SELECT 1) DELETE FROM sleep", "update sleep set sleep_s=0",
    "-- comment\nINSERT INTO sleep(calendar_date, raw_json) VALUES ('x','{}')", "",
])
def test_sql_guard_rejects(q):
    with pytest.raises(ValueError):
        mcp_srv.validate_select(q)


def test_sql_guard_allows_selects_and_literals():
    assert mcp_srv.validate_select("SELECT * FROM sleep WHERE name = 'update; drop'  ;")
    assert mcp_srv.validate_select("with t as (select 1 as a) select a from t")


def test_mcp_tools_against_db(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    c = connect(db)
    sync.run(list(sync.DATA_TYPES), False, None, fit=False, cl=FakeClient(date(2025, 1, 1)), conn=c, today=TODAY)
    c.close()
    monkeypatch.setattr(mcp_srv.config, "DB_PATH", db)
    s = mcp_srv.get_sleep("2026-03-01", "2026-03-02")
    assert s[0]["sleep_h"] == 7.5 and s[0]["sleep_score"] == 80
    r = mcp_srv.run_readonly_sql("SELECT count(*) AS n FROM daily_summary")
    assert r["rows"][0][0] == sync.FIRST_SYNC_DAYS + 1
    assert "error" in mcp_srv.run_readonly_sql("DELETE FROM sleep")
    cmp_ = mcp_srv.compare_periods("steps", "2026-02-14..2026-02-28", "2026-03-01..2026-03-15")
    assert cmp_["period_a"]["value"] is not None and "change_pct" in cmp_
    acts = mcp_srv.list_activities("2026-02-01", "2026-03-31", "run")
    assert acts and acts[0]["distance_km"] == 5.0 and acts[0]["avg_pace_min_per_km"] is None  # no speed given
    det = mcp_srv.get_activity_detail(acts[0]["activity_id"])
    assert det["summary"] == {"distance": 5000}


# ---------------- v2 data types
def test_v2_mappers_parse_descriptor_arrays(conn):
    fc = FakeClient(date(2026, 1, 1))
    sync.run(["weight", "body_battery", "hr_intraday", "spo2"], False, None, fit=False, cl=fc, conn=conn, today=TODAY)
    w = conn.execute("SELECT weight_kg, bmi FROM weight").fetchone()
    assert w[0] == 80.5  # grams normalised to kg
    bb = conn.execute("SELECT level_start, level_end, values_json, events_json FROM body_battery_day "
                      "WHERE calendar_date=?", (TODAY.isoformat(),)).fetchone()
    assert (bb[0], bb[1]) == (30, 40) and json.loads(bb[2]) == [[1, 30], [3, 85], [4, 40]]
    assert json.loads(bb[3])[0] == {"type": "SLEEP", "start_gmt": None, "duration_min": 480, "impact": 55,
                                    "feedback": "GOOD"}
    hr = conn.execute("SELECT n_samples, values_json, resting_bpm FROM hr_intraday WHERE calendar_date=?",
                      (TODAY.isoformat(),)).fetchone()
    assert hr[0] == 2 and json.loads(hr[1]) == [[2, 60], [3, 72]] and hr[2] == 55
    sp = conn.execute("SELECT avg_pct, hourly_json FROM spo2_day").fetchone()
    assert sp[0] == 96.0 and json.loads(sp[1]) == [[1, 95], [2, 97]]
    # snapshots
    assert conn.execute("SELECT count(*) FROM profile_snapshot").fetchone()[0] == 2
    assert conn.execute("SELECT fitness_age FROM fitness_age").fetchone()[0] == 31.5


def test_per_day_backfill_respects_history_cap(conn):
    fc = FakeClient(date(2020, 1, 1))  # data "exists" for years
    sync.run(["hr_intraday"], True, None, fit=False, cl=fc, conn=conn, today=TODAY, snapshots=False)
    lo = conn.execute("SELECT min(calendar_date) FROM hr_intraday").fetchone()[0]
    assert lo == (TODAY - timedelta(days=sync.INTRADAY_DAYS - 1)).isoformat()
    assert len([c for c in fc.calls if c[0] == "hr"]) <= sync.INTRADAY_DAYS + 7


def test_sync_lock_prevents_concurrent_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "LOCK_PATH", tmp_path / ".lock")
    held = sync._acquire_lock()
    with pytest.raises(sync.SyncLocked):
        sync._acquire_lock()
    held.close()
    sync._acquire_lock().close()


# ---------------- insights
def _seed(conn, days=40, rhr=55, rhr_recent=None, sleep_h=8.0, hrv=50, status="BALANCED", loads=None):
    for i in range(days):
        d = (TODAY - timedelta(days=days - 1 - i)).isoformat()
        r = rhr_recent if (rhr_recent and i >= days - 3) else rhr
        conn.execute("INSERT INTO daily_summary(calendar_date, rhr_bpm, stress_avg, bb_max, raw_json) "
                     "VALUES (?,?,?,?, '{}')", (d, r, 25, 80))
        conn.execute("INSERT INTO sleep(calendar_date, sleep_s, sleep_score, raw_json) VALUES (?,?,?,?)",
                     (d, int(sleep_h * 3600), 80, json.dumps({"sleepNeed": {"actual": 480}})))
        conn.execute("INSERT INTO hrv(calendar_date, last_night_avg_ms, weekly_avg_ms, baseline_balanced_low_ms, "
                     "baseline_balanced_upper_ms, status, raw_json) VALUES (?,?,?,?,?,?, '{}')",
                     (d, hrv, hrv, 45, 60, status))
    for i, load in enumerate(loads or []):
        d = (TODAY - timedelta(days=len(loads) - 1 - i)).isoformat()
        conn.execute("INSERT INTO activity(activity_id, calendar_date, training_load, raw_json) VALUES (?,?,?, '{}')",
                     (1000 + i, d, load))
    conn.commit()


def test_insights_healthy_baseline_is_good(conn):
    from garmin.insights import compute_alerts
    _seed(conn, loads=[50] * 30)
    keys = [a.key for a in compute_alerts(conn, TODAY)]
    assert keys == ["recovered"]


def test_insights_flags_rhr_hrv_sleep_and_load(conn):
    from garmin.insights import compute_alerts
    _seed(conn, rhr_recent=61, sleep_h=6.0, hrv=40, status="LOW", loads=[20] * 25 + [150] * 5)
    al = {a.key: a for a in compute_alerts(conn, TODAY)}
    assert al["rhr_high"].severity == "critical"
    assert al["hrv_low"].severity == "warning"
    assert al["sleep_debt"].severity == "warning"  # 7 nights x 2h short of 8h need
    assert "load_spike" in al and "recovered" not in al
    sev = [a.severity for a in compute_alerts(conn, TODAY)]
    assert sev == sorted(sev, key=["critical", "warning", "info", "good"].index)


def test_insights_stale_data(conn):
    from garmin.insights import compute_alerts
    _seed(conn, days=20)
    al = compute_alerts(conn, TODAY + timedelta(days=4))
    assert al[0].key == "stale" and al[0].severity == "critical"
