# garmin-mcp

Remote MCP server that wraps Garmin Connect with MFA support, plus a fueling engine built on top of it. Deploy to Render free tier, add as a Custom Connector in claude.ai, and query your Garmin data — or run a periodized nutrition plan — from mobile.

Two halves:

- **Garmin read layer** (§ Tools) — activities, daily metrics, planned training, analysis.
- **Fueling engine** (§ Fueling) — a stored weight goal + race calendar drives per-day calorie and macro targets, per-workout fuel cards, and a mobile dashboard. This is the part that owns state; everything else is a read-through cache over Garmin.

## Tools (41)

### Activities
- `get_activities` — recent (offset/limit) OR date range (startdate/enddate, max 366 days, optional `activity_type`)
- `get_activity_details` — full summary + splits + HR zones + weather + gear for one activity

### Daily & range data
- `get_daily_summaries` — bulk fan-out of per-day metrics across up to 366 days. 21 supported metrics: steps, sleep, stress, body_battery_events, hrv, rhr, respiration, training_readiness, training_status, stats, stats_and_body, user_summary, max_metrics, floors, intensity_minutes, heart_rates, morning_readiness, fitness_age, hydration, spo2, all_day_events. Fan-out uses 2 workers + 429 backoff. Year-scale pulls take 15–25 min.
- `get_weekly_summaries` — weekly aggregates (steps / stress / intensity_minutes) up to 104 weeks back
- `get_body_composition` — weight, body fat, BMI (date or range)
- `get_training_score` — `metric: "hill" | "endurance"` (date or range)
- `get_lactate_threshold` — latest, or history with `daily`/`weekly` aggregation
- `get_progress_summary` — aggregated training progress between two dates (distance/duration/elevation/calories)

### Planned training
- `get_workouts` / `get_workout_by_id` — library of saved workouts
- `get_training_plans` / `get_training_plan_by_id` — plans (Coach + custom; `adaptive: true` for Garmin Coach plans)
- `get_scheduled_workouts` — the training calendar over a date range (what the fueling plan builds each day's burn from)
- `skip_scheduled_session` / `get_skipped_sessions` — drop a calendar session the fueling plan shouldn't count

### Analysis (pre-computed, LLM-friendly)
- `analyze_training_period` — totals, by-activity-type breakdown, weekly timeline
- `compare_activities` — side-by-side of 2–10 activities with deltas vs baseline
- `analyze_sleep_trend` — averages + first-half vs second-half trend over N days (1–180)
- `get_athlete_baseline` — one call for the numbers every other analysis needs: thresholds, FTP, CSS, VDOT, weekly volume, CTL/ATL/TSB, race predictions, weight
- `get_cycling_ftp` — measured (20-min best × 0.95) or inferred from run FTP

### Misc
- `get_devices` — registered Garmin devices
- `get_personal_records` — PRs across all activity types
- `get_race_predictions` — 5K/10K/half/full (latest or history)

## Fueling

State this app owns (persisted in the same object store as the cache): one active weight goal, a race calendar, manual weigh-ins, weekly snapshots, and per-day exclusions.

### Goal & plan
- `set_fueling_goal` — target weight + date, BMR inputs Garmin doesn't expose (sex/height/age), and the safety knobs: deficit cap, enforced energy-availability floor, BMR-multiple floor, deficit periodization, front-loading
- `update_fueling_goal` — merge a partial edit without wiping the rest of the goal
- `get_fueling_goal` — the active goal plus live progress and review flags
- `generate_fueling_plan` — the engine. Per-day calorie + macro targets and per-workout fuel cards for the next 1–28 days, from the goal, body composition, the Garmin calendar and the race calendar. Session burn is calibrated from your own 90-day history. `save=true` merges it into the weekly snapshot.
- `get_adaptive_tdee` — measured maintenance from logged intake vs actual weight change, with a confidence rating; replaces the BMR formula once there's enough data
- `reset_fueling_history` — clear this app's own history for a fresh block (never touches Garmin-derived caches)

### Races
Store a race and the plan periodizes itself around it — no manual carb-load flag.

- `set_race` — date, sport, distance (raw or a preset: `marathon`, `half`, `10k`, `50k`, `70.3`, `ironman`, `olympic`, `sprint`, `century`, `gran fondo`, …), A/B/C priority, optional goal time and heat flag
- `get_races` / `delete_race`
- `get_race_fueling` — pre-race meal, hour-by-hour carbs/fluid/sodium/caffeine, gel count, loading protocol. Takes a `race_id` or an ad-hoc sport + duration.

Everything scales off **estimated duration**, not distance — from Garmin's race predictions for running, your own recent pace otherwise, or your stated goal time:

| Phase | Deficit | Carbs |
|---|---|---|
| Taper (A: 7d, B: 4d, C: none) | 50%, then 25% inside 3 days | 5 g/kg floor near the end |
| Carb load (0–3 days by duration) | **off** | ramps 8 → 10 g/kg |
| Race day | **off** | set by the race fuel card |
| Recovery (day after) | **off** | 6 g/kg, +0.3 g/kg protein |
| Post-race | 50% | 5 g/kg |

Loading tops out at 3 days / 10 g/kg because glycogen supercompensation does — an Ironman and a marathon load identically. What keeps scaling with the event is recovery (1/2/3 days, then 5 past 5 h and 7 past 8 h) and race-day intake. Loading days deliberately run **above maintenance**: 10 g/kg is ~2,960 kcal of carbohydrate for a 74 kg athlete, so the day is floored at what its own macros cost rather than trimming the carbs to fit.

### Tracking
- `nutrition_plan_vs_actual` — logged intake vs the expenditure-adjusted target, per day
- `nutrition_trend` — weekly intake/weight trajectory
- `ignore_food_day` / `unignore_food_day` / `get_ignored_food_days` — exclude a day you know you logged badly from the rebalance, adaptive TDEE and coaching maths
- `save_weekly_snapshot` / `get_weekly_snapshots`
- `push_nutrition_targets_to_garmin` — EXPERIMENTAL: write the plan's targets into Garmin Connect's nutrition goals so the app shows them. Requires the live (non-readonly) env; the nightly cron can do it with `FUEL_PUSH_GARMIN=true`.

### Dashboard

`GET /dashboard` serves a self-contained mobile page: today's card, the week strip with race-phase badges, energy-availability panel, weight-to-target projection, per-workout fuel timeline, and a settings tab for logging a weigh-in, editing the goal, managing races, and ignoring a day. Set `DASHBOARD_TOKEN` to gate it behind `?k=<token>`.

## Resources (3)

Exposed via MCP `resources/list` + `resources/read` so Claude has live context without explicit tool calls:
- `garmin://athlete/profile` — profile, settings, unit system, name
- `garmin://today/summary` — today's stats, steps, sleep, HR, body battery
- `garmin://training/readiness` — today's training readiness, status, HRV, morning readiness

## How auth works

Garmin requires MFA (email code) on most accounts. You can't complete that from a server. Instead:

1. **Once, locally:** run `scripts/bootstrap.py`. You log in, paste the emailed code, and the script prints a base64 blob containing long-lived OAuth tokens.
2. **Deploy:** paste that blob into Render as `GARTH_TOKENS_B64`. The server uses the tokens (no password ever leaves your machine). Tokens auto-refresh for ~1 year.
3. If re-auth is needed later (password change, token expiry), re-run bootstrap.

## One-time local setup

```bash
cd ~/garmin-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/bootstrap.py
```

> Run bootstrap directly in your own terminal — it prompts interactively for email, password, and MFA code, so it won't work through non-interactive tools (like Claude Code's Bash).

Copy the printed `GARTH_TOKENS_B64` value — you'll need it below.

## Local dev

Fill in `.env`:

```
MCP_BEARER_TOKEN=some-long-random-string
GARTH_TOKENS_B64=<paste-from-bootstrap>
```

Run:

```bash
source .venv/bin/activate
set -a && source .env && set +a
uvicorn app.main:app --reload --port 8787
```

Test:

```bash
TOKEN=$(grep ^MCP_BEARER_TOKEN= .env | cut -d= -f2-)
curl -s -X POST http://localhost:8787/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "content-type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"resources/read","params":{"uri":"garmin://athlete/profile"}}' | python3 -m json.tool
```

## Tests

Both suites stub the Garmin fetchers and swap the object-store cache for an in-memory dict, so the maths runs with no credentials and no network:

```bash
python scripts/test_fueling.py   # goal store, energy model, macros, meals, tracking
python scripts/test_races.py     # distance presets, duration estimates, race phases
```

Each prints a PASS/FAIL line per check and exits non-zero on failure. Anything touching the fueling engine should keep both green — the numbers they pin down (floors, energy availability, carb periodization) are the ones that decide what you actually eat.

## Scheduled refresh

One Render cron (`scripts/cron_dispatcher.py`, hourly at :30) fans out to three jobs, because Render bills a $1/mo minimum per service and three separate crons cost three times as much as one:

- `daily_refresh` at 03:30 UTC — the full nightly anchor: pre-warms the caches the read-only web service serves from, and rebuilds the fueling plan
- `today_refresh` at 00:30 / 06:30 / 12:30 / 18:30 — today's stats, food log and weight
- a workout check every tick — picks up newly-synced activities so today's burn and targets track reality

The web service runs `GARMIN_READONLY=true` and never calls Garmin itself; it serves whatever the cron last wrote. That's why a cold cache shows stale numbers rather than fetching, and why `push_nutrition_targets_to_garmin` only works from the cron env.

## Deploy to Render

1. Push this directory to a GitHub repo.
2. https://render.com → New → Web Service → connect your repo.
3. Render auto-detects `render.yaml`. Confirm.
4. Set env vars in Render dashboard:
   - `MCP_BEARER_TOKEN` — same random string from `.env`
   - `GARTH_TOKENS_B64` — the blob from bootstrap
   - `DASHBOARD_TOKEN` — optional; gates `/dashboard` behind `?k=<token>`
5. Deploy. You'll get a URL like `https://garmin-mcp.onrender.com`.

The cron service needs **read-write** S3/R2 credentials. The web service can run read-only for the Garmin caches, but the fueling goal, race calendar, manual weigh-ins and ignored days are all written from the dashboard forms — with read-only keys those saves silently vanish. `set_fueling_goal` and `set_race` read back after writing and report `saved: false` with an actionable error when that happens, so it's visible rather than mysterious.

Free tier sleeps after ~15 min idle. First request after sleep: ~30–60s cold start.

## Add to claude.ai

1. https://claude.ai → Settings → Connectors → Add custom connector
2. URL: `https://<your-service>.onrender.com/mcp`
3. Auth header: `Authorization: Bearer <your MCP_BEARER_TOKEN>`
4. Save. Available on web + iOS + Android.

## Object store (cache + app state)

Without it, a year-long `get_daily_summaries` takes 15-25 min every time and loses work on Render cold-starts. With it, per-(metric, date) responses are stored and repeated/overlapping pulls return in ~100-300ms per hit.

Works with any S3-compatible store. **Cloudflare R2** is the recommended default — 10 GB storage + generous free ops, no 12-month expiration.

**What's cached:** `get_daily_summaries` (per metric-day), `get_activities_in_range`, `get_activity_details`.
**Bypass:** pass `force_refresh: true` on any of those calls.
**No cache?** If `S3_CACHE_BUCKET` is unset, the Garmin tools still work — they just hit Garmin every time.

The same bucket also holds everything the fueling engine owns, written under long-lived keys rather than cached with a TTL: the active goal (`fueling_goal/`), the race calendar (`fueling_races/`), manual weigh-ins, weekly snapshots, skipped sessions and ignored days. **This is real state, not a cache** — losing the bucket loses your goal and race calendar, and the fueling tools return `no_goal_available` without one. `reset_fueling_history` clears only this app's own state and deliberately can't touch the Garmin-derived caches, since the read-only web service could never re-fetch them.

### Setup — Cloudflare R2 (recommended)

1. Cloudflare dashboard → R2 → Create bucket (any name, e.g. `garmin-mcp-cache`).
2. R2 → Manage API tokens → Create API token → **Object Read & Write** permission → scope to that bucket. Copy the Access Key ID + Secret Access Key.
3. Note your R2 endpoint URL: `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` (shown on the bucket page).
4. Set Render env vars:

```
S3_CACHE_BUCKET=garmin-mcp-cache
S3_ENDPOINT_URL=https://<ACCOUNT_ID>.r2.cloudflarestorage.com
AWS_ACCESS_KEY_ID=<R2 access key id>
AWS_SECRET_ACCESS_KEY=<R2 secret>
S3_REGION=auto                       # R2 uses "auto"
S3_CACHE_PREFIX=garmin-mcp/          # optional, default "garmin-mcp/"
S3_CACHE_TTL_SECONDS=86400           # optional, default 24h
```

### Setup — AWS S3

Same env vars but skip `S3_ENDPOINT_URL` and set `S3_REGION` (or `AWS_DEFAULT_REGION`) to your bucket's region. Free tier is 5 GB + 20k GET + 2k PUT for the first 12 months only; after that ~$0.02–0.05/mo at this workload.

## Caveats

- **Unofficial Garmin auth.** `python-garminconnect` uses a reverse-engineered flow. Garmin occasionally changes it; check that project's issues if something breaks.
- **Token rotation.** Tokens auto-refresh but expire after ~1 year of non-use, or if you change your Garmin password. Re-run bootstrap to refresh.
- **Security.** The bearer token is the only thing protecting `/mcp` — make it long and random (`openssl rand -hex 32`). Anyone with it can read your Garmin data. `/dashboard` is separate: it's ungated unless you set `DASHBOARD_TOKEN`.
- **Not medical advice.** The fueling engine will happily plan a deep deficit if you ask it to. The guardrails (energy-availability floor, BMR-multiple floor, absolute calorie floor, loss-rate cap) are on by default and can all be disabled; the plan says so loudly when they are, but it won't stop you. Race-day and carb-loading numbers are population defaults, not a prescription — gut tolerance for 90 g/hr is trained, not assumed.
- **Estimates are labelled as such.** A race duration carries a `duration_source` (`race_predictions`, `history_pace`, `user`, `default_pace`) and session burn carries a `burn_source` (`history_similar`, `history_sport`, `generic_table`, `actual_today`). When those read `default_pace` or `generic_table`, the number is a guess from a table, not from you — set a goal time or log more sessions.
