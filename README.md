# Garmin Health Dashboard + MCP server

A local, personal Garmin data stack built on Garmin's official **Connect Personal API**
(free, pull-only). It syncs into SQLite, shows a Streamlit dashboard, and exposes a
read-only MCP server so Claude can answer questions about your data. Nothing leaves
your Mac apart from the API calls to Garmin.

```
Garmin Personal API ──► garmin/sync.py ──► data/garmin.db (SQLite) ──► dashboard/app.py (Streamlit)
        (daily 06:00 via launchd)            data/fit/*.fit          └─► mcp_server/server.py (Claude)
```

## 1. Setup

Requires macOS, [uv](https://docs.astral.sh/uv/) (`brew install uv`) and Node (only for `make mcp-test`).

```bash
make install            # uv installs Python 3.12 + deps into .venv
cp .env.example .env    # if .env doesn't exist yet
```

### Getting your API key
1. Sign in at [connect.garmin.com](https://connect.garmin.com) in a browser.
2. Open the **Personal API Key** page (linked from
   [developer.garmin.com → Personal API](https://developer.garmin.com/gc-developer-program/personal/personal-api)).
3. Create a key and choose an expiry (30 / 90 / 180 days or 1 year). **It's shown only once.** Copy it.
4. Put it in `.env`:
   ```
   GARMIN_API_KEY=your-key-here
   ```
   `.env` is gitignored and `chmod 600`.

How auth works (from the Personal API docs and FAQ): the key is exchanged for an access token via
`POST https://personal.connectapi.garmin.com/personalapitoken-service/personal-api/personal-api-key/token`
with `Authorization: ApiKey <key>`. The token is valid for **24 h** (or until the key expires) and there are
**no refresh tokens**, so the client simply requests a new one. Tokens are cached in `data/.token.json`
(mode 600, tied to a hash of your key). When the key expires, Garmin emails you about a week before.
Create a new key and update `.env`.

## 2. Sync

```bash
make sync                      # incremental: from last synced date - 3 days, to today
make backfill                  # walk back through all available history
make backfill SINCE=2023-01-01 # backfill only down to a date
uv run python -m garmin.sync --types sleep hrv --no-fit   # subsets / skip file downloads
```

- **First run** with no state syncs the last 30 days. Run `make backfill` once for history.
- **Incremental** sync re-fetches the last 3 days, because Garmin revises sleep, HRV and summaries after the fact.
- **Backfill** steps backwards in the largest documented window per endpoint. It stops after a run of
  empty windows (about 1 year without data), or at `SINCE`. Progress is stored in `sync_state`, so it
  resumes where it left off.
- **Upserts** are idempotent (`INSERT … ON CONFLICT DO UPDATE`). Every row keeps the full API JSON in
  `raw_json`, so nothing is lost.
- **Activities** additionally fetch detail JSON (splits, full `summaryDTO`). The original file is
  downloaded to `data/fit/<id>.fit` and session fields are parsed with `fitdecode`.
- **Retries:** 429 and 5xx responses are retried with exponential backoff, honouring `Retry-After`.
  A 401 triggers one token refresh.

### Schedule (daily 06:00)
```bash
make install-schedule     # installs ~/Library/LaunchAgents/com.garmin-dashboard.sync.plist
make run-schedule-now     # test it immediately
tail -f logs/sync.log
make uninstall-schedule
```
If the Mac is asleep at 06:00, launchd runs the job when it wakes.

## 3. Dashboard

```bash
make dashboard            # http://localhost:8501
```
A dark "ops console" layout with sidebar navigation, a range selector (7d / 30d / 90d / 1y / All) and a
**Sync now** button that runs an incremental sync in the background (a file lock prevents overlap
with the 06:00 job). Pages:

| Page | What's on it |
|---|---|
| **Overview** | daily brief; **signals** banner (rule-based alerts from `garmin/insights.py`); KPI cards with sparklines; day timeline (intraday HR with workout spans + Body Battery curve and its drivers); recent workouts; side rail: last night's sleep, HR-zone time, body metrics |
| **Recovery** | HRV vs baseline band, resting HR, stress, Body Battery low→peak, SpO₂ (avg + lowest), respiration, Body Battery drivers |
| **Sleep** | stages, score, sleep window (bed→wake), sleeping HR, average gap vs Garmin's nightly sleep need, nights table |
| **Training** | weekly volume by type, acute vs chronic load with the 0.8–1.3× band, activity calendar heatmap, HR-zone time by type, workouts table → click a workout for its FIT heart rate, speed and elevation |
| **Body** | weigh-ins, Fitness Age history vs actual/achievable, what drives Fitness Age |
| **Data & sync** | row counts and date ranges per table, sync state, data types the API has no data for, sync log |

Alerts the engine raises: HRV below baseline, resting HR up 3+ bpm (5+ critical) over the 30-day median,
weekly sleep debt vs Garmin sleep need, poor sleep score, training-load ratio spike or drop, no workouts
for 5+ days, high stress, low Body Battery recharge, stale data. Run `uv run python -m garmin.insights`
to print them in the terminal.

### Access from your phone and laptop (Tailscale)

The dashboard runs as an always-on launchd service bound to `127.0.0.1`, so it isn't reachable from
your local network. Tailscale makes it reachable from your own devices only, over HTTPS, from anywhere.
Nothing is exposed to the public internet.

```bash
make install-dashboard   # always-on service: starts at login, restarts on crash (logs/dashboard.log)
make install-schedule    # daily 06:00 sync, so the remote view stays fresh
# install Tailscale on the Mac, phone and laptop (same account), then:
make tailscale-serve     # prints https://<mac-name>.<tailnet>.ts.net
make remote-status       # service, health check and serve status in one place
```

- **Mac must be awake:** System Settings → Battery → Options → turn on "Prevent automatic sleeping on
  power adapter when the display is off". launchd also runs a missed 06:00 sync when the Mac wakes.
- **After code changes:** `make restart-dashboard`.
- **Stop remote access:** `make tailscale-unserve`. Remove the service with `make uninstall-dashboard`.
- **Who can see it:** Tailscale `serve` (not `funnel`) only reaches devices signed in to your tailnet.
  Never use `tailscale funnel` for this; it would publish your health data to the internet.

### Always-on link (encrypted GitHub Pages snapshot)

GitHub Pages can only serve static files, so it can't run the Streamlit app. Instead, after each
sync the Mac builds a static page (signals, KPIs, interactive charts, recent workouts and nights)
and publishes it **encrypted** to the `gh-pages` branch:

```
SQLite ──► dashboard/snapshot.py ──► HTML ──gzip──► AES-256-GCM (PBKDF2-SHA256, 600k) ──► gh-pages/index.html
```

The public file holds only ciphertext and a password form. The browser decrypts it with WebCrypto, and
"Remember on this device" stores the derived key (not the password) in that browser. **Lock** forgets it.

```bash
# 1. in .env (never committed): a long passphrase, 5+ random words
SNAPSHOT_PASSWORD=...
make snapshot         # local build: data/pages/index.html + unencrypted preview.html (never published)
make publish          # build + force-push to gh-pages (one commit, no history of old snapshots)
make install-schedule # 06:00 job now runs `make daily`: sync, then publish
```

Set GitHub → Settings → Pages → Source to **`gh-pages` / root** once.
The data is only as fresh as the Mac's last sync. The page shows "data through …".
To change the password, edit `.env` and run `make publish`. Devices that remembered the old key will ask again.

## 4. MCP server (Claude Desktop)

The server is read-only over the local SQLite file. It never calls Garmin. Tools:

| Tool | What it returns |
|---|---|
| `get_daily_summary(start, end)` | steps, resting HR, stress, Body Battery, intensity minutes, calories, SpO2, respiration |
| `get_sleep(start, end)` | hours by stage, sleep score, sleep HR, respiration, SpO2 |
| `get_hrv_trend(days)` | overnight HRV (ms), weekly avg, baseline range, status, and a summary |
| `list_activities(start, end, type?)` | workouts with duration, distance, pace, HR, load, training effect |
| `get_activity_detail(activity_id)` | activity + parsed FIT session + splits + Garmin summary |
| `compare_periods(metric, period_a, period_b)` | e.g. `hrv`, `"2026-09-01..2026-09-30"` vs `"2026-10-01..2026-10-31"` |
| `run_readonly_sql(query)` | a single SELECT/WITH only (validated, SQLite authorizer, `mode=ro`, `query_only`) |
| `get_data_coverage()` | synced date ranges per data type |
| `get_alerts()` | the same rule-based signals the dashboard shows, with the numbers that triggered them |
| `get_training_load(days)` | daily load, acute 7-day, chronic weekly, acute:chronic ratio |
| `get_intraday(day, resolution_minutes)` | heart rate, Body Battery (with events) and SpO₂ curves for one day |
| `get_body_metrics(start, end)` | weigh-ins, Fitness Age history and its drivers, HR zones |

Tool descriptions include units, so Claude reads e.g. HRV as ms RMSSD and stress on the 0–100 scale.

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json`, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "garmin-health": {
      "command": "/opt/homebrew/bin/uv",
      "args": [
        "--directory",
        "/Users/amirshahcheraghian/AI Projects/Garmin Dashboard",
        "run",
        "python",
        "mcp_server/server.py"
      ]
    }
  }
}
```
(Use `which uv` if uv lives elsewhere. JSON args handle the space in the path; don't add extra quotes.)

Test it interactively with the MCP Inspector: `make mcp-test`.

## 5. Tests

```bash
make test
```
- Client tests use mocked HTTP (`respx`): token caching, 401 refresh, 429 `Retry-After`, 5xx/network retries, pagination, 204 handling.
- Sync tests check idempotency (repeat runs give identical rows), incremental overlap, revised-value upserts, backfill stop/resume, and the SQL guard.

## What the Personal API does and doesn't give us

Endpoints come from the official Swagger spec (saved as `docs/openapi.json`):

| Data | Endpoint | Max range per call |
|---|---|---|
| Daily summary | `GET /usersummary-service/v2/dailysummaries` | 1 year |
| Sleep | `GET /sleep-service/sleep/dailySleepSummariesByDate` | 31 days |
| HRV | `GET /hrv-service/hrv/daily/{from}/{to}` | not documented (31 used) |
| Activities | `GET /activitylist-service/activities/search/activities` | paginated, 99 per page |
| Activity detail | `GET /activity-service/activity/{id}` | — |
| Activity file | `GET /download-service/files/activity/{id}` (zip of the original file) | — |
| Weight | `GET /weight-service/weight/range/{start}/{end}` | not stated (1 year used) |
| Body Battery curve + events | `GET /wellness-service/wellness/bodyBattery/reports/daily?chunks=48` | 31 days |
| Intraday heart rate | `GET /wellness-service/wellness/dailyHeartRate?date=` | 1 day per call |
| SpO₂ (hourly) | `GET /wellness-service/wellness/daily/spo2acclimation/{date}` | 1 day per call |
| Fitness Age | `GET /fitnessage-service/fitnessage/v2/{date}` (as of a date) | snapshot |
| HR zones / profile | `GET /biometric-service/heartRateZones/v2`, `/userprofile-service/userprofile/settings` | snapshot |

The one-call-per-day endpoints (intraday HR, SpO₂) are backfilled for the last **90 days** only
(`INTRADAY_DAYS` in `garmin/sync.py`), because of the rate limits. Fitness Age history is rebuilt from
monthly "as of" samples on `make backfill`. HR time-in-zone per activity is read from the stored
activity JSON (the `activity_ext` view), so it needs no extra API calls.

- **Training load:** the API has **no daily training status or acute/chronic load endpoint**. The
  dashboard's training load is *derived*: the sum of Garmin's per-activity `activityTrainingLoad`.
- **Not documented, verified against the live API:**
  - The token endpoint returns the JWT as `text/plain` (not JSON). Its `exp` claim is 24 h after `iat`.
  - Data calls use `Authorization: Bearer <token>`.
  - Garmin answers **HTTP 406** if `Accept` doesn't match the response type. The client sends `*/*` for
    the token and file-download calls, and `application/json` everywhere else.
  - There are rate limits: bursts of activity-detail calls get **429** with `Retry-After`
    (seen: 0–41 s). The client waits for the longer of `Retry-After` and its own exponential backoff.
    The first full backfill of a few hundred activities therefore takes a while. Later daily syncs
    only touch new activities.
  - `splitSummaries` is only present for some activity types (absent for walking and strength).
- **Not synced:**
  - Hydration, nutrition and wellness activities: the API returned no data for this account.
  - Menstrual cycle: not requested.
  - VO₂ max: not present in the activity data.
  - Training status and readiness: no endpoint exists.

## Layout
```
garmin/      client.py (API + auth + retries) · models.py (pydantic) · sync.py · fit.py · config.py
db/          schema.sql · db.py (connect, upsert)
dashboard/   app.py · views.py · ui.py · data.py · snapshot.py + snapshot_hero.py + snapshot_assets/ (encrypted GitHub Pages build)
mcp_server/  server.py
launchd/     sync.plist.template
tests/       test_client.py · test_sync.py
docs/        openapi.json (official spec)
data/        garmin.db, fit/, .token.json   (gitignored)
logs/        sync.log                       (gitignored)
```
