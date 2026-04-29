# garmin-mcp

Remote MCP server that wraps Garmin Connect with MFA support. Deploy to Render free tier, add as a Custom Connector in claude.ai, and query your Garmin data from mobile.

## Tools (18)

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

### Analysis (pre-computed, LLM-friendly)
- `analyze_training_period` — totals, by-activity-type breakdown, weekly timeline
- `compare_activities` — side-by-side of 2–10 activities with deltas vs baseline
- `analyze_sleep_trend` — averages + first-half vs second-half trend over N days (1–180)

### Misc
- `get_devices` — registered Garmin devices
- `get_personal_records` — PRs across all activity types
- `get_race_predictions` — 5K/10K/half/full (latest or history)

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

## Deploy to Render

1. Push this directory to a GitHub repo.
2. https://render.com → New → Web Service → connect your repo.
3. Render auto-detects `render.yaml`. Confirm.
4. Set env vars in Render dashboard:
   - `MCP_BEARER_TOKEN` — same random string from `.env`
   - `GARTH_TOKENS_B64` — the blob from bootstrap
5. Deploy. You'll get a URL like `https://garmin-mcp.onrender.com`.

Free tier sleeps after ~15 min idle. First request after sleep: ~30–60s cold start.

## Add to claude.ai

1. https://claude.ai → Settings → Connectors → Add custom connector
2. URL: `https://<your-service>.onrender.com/mcp`
3. Auth header: `Authorization: Bearer <your MCP_BEARER_TOKEN>`
4. Save. Available on web + iOS + Android.

## Object-store cache (optional but recommended)

Without a cache, a year-long `get_daily_summaries` takes 15-25 min every time and loses work on Render cold-starts. With a cache, per-(metric, date) responses are stored and repeated/overlapping pulls return in ~100-300ms per hit.

Works with any S3-compatible store. **Cloudflare R2** is the recommended default — 10 GB storage + generous free ops, no 12-month expiration.

**What's cached:** `get_daily_summaries` (per metric-day), `get_activities_in_range`, `get_activity_details`.
**Bypass:** pass `force_refresh: true` on any of those calls.
**No cache?** If `S3_CACHE_BUCKET` is unset, all tools still work — they just hit Garmin every time.

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
- **Security.** The bearer token is the only thing protecting `/mcp` — make it long and random (`openssl rand -hex 32`). Anyone with it can read your Garmin data.
