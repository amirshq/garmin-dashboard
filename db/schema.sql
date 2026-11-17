-- Garmin personal data. Every table keeps the full API record in raw_json so nothing is lost.
-- Units: seconds (_s), meters (_m), beats per minute (bpm), milliseconds for HRV (_ms).
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS daily_summary (
    calendar_date      TEXT PRIMARY KEY,   -- YYYY-MM-DD (user local day)
    steps              INTEGER,
    steps_goal         INTEGER,
    distance_m         REAL,
    floors_up          INTEGER,
    rhr_bpm            INTEGER,            -- resting heart rate
    hr_min_bpm         INTEGER,
    hr_max_bpm         INTEGER,
    stress_avg         INTEGER,            -- Garmin stress 0-100
    stress_max         INTEGER,
    stress_qualifier   TEXT,
    bb_min             INTEGER,            -- Body Battery 0-100
    bb_max             INTEGER,
    bb_charged         INTEGER,
    bb_drained         INTEGER,
    im_moderate_min    INTEGER,            -- intensity minutes
    im_vigorous_min    INTEGER,
    cal_total_kcal     INTEGER,
    cal_active_kcal    INTEGER,
    cal_resting_kcal   INTEGER,
    spo2_avg_pct       REAL,
    resp_avg_brpm      REAL,               -- breaths per minute
    raw_json           TEXT NOT NULL,
    synced_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sleep (
    calendar_date      TEXT PRIMARY KEY,   -- the morning the sleep ended
    sleep_s            INTEGER,            -- total sleep excl. naps
    deep_s             INTEGER,
    light_s            INTEGER,
    rem_s              INTEGER,
    awake_s            INTEGER,
    nap_s              INTEGER,
    sleep_score        INTEGER,            -- 0-100
    start_gmt          TEXT,
    end_gmt            TEXT,
    avg_hr_bpm         REAL,
    avg_resp_brpm      REAL,
    avg_spo2_pct       REAL,
    avg_sleep_stress   REAL,
    awake_count        INTEGER,
    raw_json           TEXT NOT NULL,
    synced_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS hrv (
    calendar_date            TEXT PRIMARY KEY,
    last_night_avg_ms        INTEGER,
    last_night_5min_high_ms  INTEGER,
    weekly_avg_ms            INTEGER,
    baseline_low_upper_ms    INTEGER,
    baseline_balanced_low_ms INTEGER,
    baseline_balanced_upper_ms INTEGER,
    status                   TEXT,        -- e.g. BALANCED, UNBALANCED, LOW, POOR
    raw_json                 TEXT NOT NULL,
    synced_at                TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS activity (
    activity_id        INTEGER PRIMARY KEY,
    name               TEXT,
    type_key           TEXT,               -- e.g. running, cycling, strength_training
    start_local        TEXT,               -- YYYY-MM-DD HH:MM:SS local
    start_gmt          TEXT,
    calendar_date      TEXT,               -- date part of start_local
    duration_s         REAL,
    moving_s           REAL,
    distance_m         REAL,
    elev_gain_m        REAL,
    avg_speed_mps      REAL,
    avg_hr_bpm         REAL,
    max_hr_bpm         REAL,
    calories_kcal      REAL,
    avg_power_w        REAL,
    training_load      REAL,               -- Garmin activityTrainingLoad (EPOC-based)
    aerobic_te         REAL,               -- training effect 0-5
    anaerobic_te       REAL,
    vo2max             REAL,
    raw_json           TEXT NOT NULL,
    detail_json        TEXT,               -- /activity-service/activity/{id}
    synced_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_activity_date ON activity(calendar_date);
CREATE INDEX IF NOT EXISTS idx_activity_type ON activity(type_key);

CREATE TABLE IF NOT EXISTS activity_fit (
    activity_id        INTEGER PRIMARY KEY REFERENCES activity(activity_id) ON DELETE CASCADE,
    path               TEXT,               -- relative to project root
    status             TEXT NOT NULL,      -- ok | not_fit | error
    error              TEXT,
    sport              TEXT,
    sub_sport          TEXT,
    total_timer_s      REAL,
    total_distance_m   REAL,
    avg_hr_bpm         REAL,
    max_hr_bpm         REAL,
    avg_cadence        REAL,
    total_ascent_m     REAL,
    n_records          INTEGER,
    parsed_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_state (
    data_type          TEXT PRIMARY KEY,   -- daily_summary | sleep | hrv | activity
    last_synced_date   TEXT,               -- newest date fetched
    oldest_synced_date TEXT,               -- oldest date fetched (backfill frontier)
    backfill_complete  INTEGER NOT NULL DEFAULT 0,
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------- v2: richer data
CREATE TABLE IF NOT EXISTS weight (
    sample_pk          INTEGER PRIMARY KEY,
    calendar_date      TEXT NOT NULL,
    timestamp_gmt      TEXT,
    weight_kg          REAL,
    bmi                REAL,
    body_fat_pct       REAL,
    body_water_pct     REAL,
    muscle_mass_kg     REAL,
    bone_mass_kg       REAL,
    source_type        TEXT,
    raw_json           TEXT NOT NULL,
    synced_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_weight_date ON weight(calendar_date);

CREATE TABLE IF NOT EXISTS body_battery_day (
    calendar_date      TEXT PRIMARY KEY,
    charged            INTEGER,
    drained            INTEGER,
    level_start        INTEGER,            -- first reading of the day (0-100)
    level_end          INTEGER,            -- last reading of the day
    values_json        TEXT,               -- [[epoch_ms, level], ...] (30-min chunks)
    events_json        TEXT,               -- [{type, start_gmt, duration_min, impact, feedback}]
    raw_json           TEXT NOT NULL,
    synced_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS hr_intraday (
    calendar_date      TEXT PRIMARY KEY,
    resting_bpm        INTEGER,
    min_bpm            INTEGER,
    max_bpm            INTEGER,
    rhr_7d_avg_bpm     INTEGER,
    n_samples          INTEGER,
    values_json        TEXT,               -- [[epoch_ms, bpm], ...] non-null samples
    raw_json           TEXT NOT NULL,
    synced_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS spo2_day (
    calendar_date      TEXT PRIMARY KEY,
    avg_pct            REAL,
    lowest_pct         REAL,
    avg_sleep_pct      REAL,
    latest_pct         REAL,
    avg_7d_pct         REAL,
    hourly_json        TEXT,               -- [[epoch_ms, spo2_pct], ...]
    raw_json           TEXT NOT NULL,
    synced_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fitness_age (
    as_of_date             TEXT PRIMARY KEY,   -- date queried ("as of")
    chronological_age      INTEGER,
    fitness_age            REAL,
    achievable_fitness_age REAL,
    previous_fitness_age   REAL,
    last_updated           TEXT,
    raw_json               TEXT NOT NULL,
    synced_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- point-in-time profile objects (hr_zones, user_settings); latest wins
CREATE TABLE IF NOT EXISTS profile_snapshot (
    kind               TEXT PRIMARY KEY,
    raw_json           TEXT NOT NULL,
    synced_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- convenience view: per-activity metrics already present in the stored API JSON
CREATE VIEW IF NOT EXISTS activity_ext AS
SELECT a.*,
       json_extract(a.raw_json, '$.hrTimeInZone_1') AS hr_z1_s,
       json_extract(a.raw_json, '$.hrTimeInZone_2') AS hr_z2_s,
       json_extract(a.raw_json, '$.hrTimeInZone_3') AS hr_z3_s,
       json_extract(a.raw_json, '$.hrTimeInZone_4') AS hr_z4_s,
       json_extract(a.raw_json, '$.hrTimeInZone_5') AS hr_z5_s,
       json_extract(a.raw_json, '$.trainingEffectLabel') AS te_label,
       json_extract(a.raw_json, '$.moderateIntensityMinutes') AS im_moderate_min,
       json_extract(a.raw_json, '$.vigorousIntensityMinutes') AS im_vigorous_min,
       json_extract(a.raw_json, '$.steps') AS steps,
       json_extract(a.raw_json, '$.locationName') AS location
FROM activity a;
