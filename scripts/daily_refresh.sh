#!/usr/bin/env bash
# Keep the R2 cache current with recent Garmin data.
#
# - Daily summaries: pulls the last DAILY_LOOKBACK_DAYS days for 11 metrics.
#   Per-(metric, date) cache means repeated days are free; only new days hit
#   Garmin. Most-recent FORCE_REFRESH_DAYS days bypass cache to catch late
#   Garmin syncs.
# - Activities: force-refreshes the current calendar month (and previous
#   month for the first 3 days of a new month). Activities cache as one R2
#   object per (year, month), so sliding-window queries don't pile up keys.
#
# Idempotent. Safe to run multiple times per day.
#
# Usage (local):
#   ./scripts/daily_refresh.sh
#
# Required env:
#   MCP_URL         e.g. https://garmin-mcp-rnwu.onrender.com
#   MCP_BEARER      optional — if unset, fetched via rubber-stamp /token
set -euo pipefail

MCP_URL="${MCP_URL:-https://garmin-mcp-rnwu.onrender.com}"
METRICS='["steps","sleep","stress","rhr","hrv","respiration","training_readiness","training_status","max_metrics","intensity_minutes","stats_and_body","body_battery_events","morning_readiness","nutrition_food_log","nutrition_meals"]'
DAILY_LOOKBACK_DAYS=3
FORCE_REFRESH_DAYS=1       # re-fetch only today (yesterday+ should already be cached)
SCHEDULED_LOOKAHEAD_DAYS=14  # prewarm scheduled workouts + their structures
# Render free-tier proxy cuts requests off around ~100s. Keep per-call scope
# small so each HTTP request stays under that budget.
REQ_TIMEOUT_SEC=90
# If Garmin OAuth starts 429ing, abort the run instead of hammering the
# endpoint — each additional call extends the throttle window.
MAX_CONSECUTIVE_429=2

today=$(python3 -c "from datetime import date; print(date.today().isoformat())")
daily_start=$(python3 -c "from datetime import date, timedelta; print((date.today() - timedelta(days=$DAILY_LOOKBACK_DAYS)).isoformat())")

# Activity refresh: force-refresh the current month's cache (and previous
# month for the first 3 days of a new month, to catch late syncs). Activities
# are cached per-calendar-month in R2 so sliding windows don't duplicate keys.
months_to_refresh=$(python3 -c "
from datetime import date
t = date.today()
months = [(t.year, t.month)]
if t.day <= 3:
    prev_month = 12 if t.month == 1 else t.month - 1
    prev_year = t.year - 1 if t.month == 1 else t.year
    months.insert(0, (prev_year, prev_month))
print(' '.join(f'{y}-{m:02d}' for y, m in months))
")

if [ -z "${MCP_BEARER:-}" ]; then
  echo "Fetching bearer via /token..."
  MCP_BEARER=$(curl -s --max-time 30 -X POST "$MCP_URL/token" \
    -d 'grant_type=authorization_code&code=x' \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
fi
if [ -z "$MCP_BEARER" ]; then
  echo "ERROR: no bearer token" >&2
  exit 1
fi

# Returns exit codes: 0=ok, 1=non-429 error, 2=Garmin 429 rate-limit.
# Callers can distinguish 429s to abort early instead of hammering.
call_mcp() {
  local label="$1" name="$2" args="$3"
  local payload
  payload=$(NAME="$name" ARGS="$args" python3 -c '
import json, os
print(json.dumps({
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {"name": os.environ["NAME"], "arguments": json.loads(os.environ["ARGS"])}
}))')
  local outfile="/tmp/mcp_refresh_${label}.json"
  local http
  http=$(curl -s --max-time $REQ_TIMEOUT_SEC -o "$outfile" \
    -w "%{http_code}" -X POST "$MCP_URL/mcp" \
    -H "Authorization: Bearer $MCP_BEARER" \
    -H "Content-Type: application/json" \
    -d "$payload" || echo "000")
  local bytes
  bytes=$(wc -c < "$outfile" 2>/dev/null || echo 0)
  echo "  http=$http, bytes=$bytes"
  if [ "$http" != "200" ]; then
    echo "  ERROR: non-200 response" >&2
    return 1
  fi
  # Surface errors from the MCP envelope so CI logs catch them.
  local err
  err=$(python3 -c "
import json
try:
    d = json.load(open('$outfile'))
    if 'error' in d:
        print(d['error'].get('message','unknown error'))
except Exception as ex:
    print(f'(could not parse response: {ex})')
")
  if [ -n "$err" ]; then
    echo "  ERROR: $err" >&2
    # Distinguish Garmin OAuth 429 so the caller can abort the run.
    if echo "$err" | grep -qi "rate limit.*429\|429.*rate limit\|too many requests"; then
      return 2
    fi
    return 1
  fi
  return 0
}

echo "=== daily_refresh $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "Target: $MCP_URL"
echo

# Track consecutive Garmin 429s so we abort early. Each 429 re-triggers an
# OAuth-exchange rate-limit window; continuing to hammer only extends it.
consec_429=0

# Per-day fan-out: one HTTP call per (date). Keeps each request small
# enough for Render's free-tier proxy timeout. Cache hits return fast.
echo "[1/5] get_daily_summaries — $DAILY_LOOKBACK_DAYS days ending $today"
failed_days=0
aborted=false
for i in $(seq 0 $DAILY_LOOKBACK_DAYS); do
  d=$(python3 -c "from datetime import date, timedelta; print((date.today() - timedelta(days=$i)).isoformat())")
  force="false"
  if [ "$i" -lt "$FORCE_REFRESH_DAYS" ]; then force="true"; fi
  echo "  day $d (force_refresh=$force)"
  set +e
  call_mcp "daily_${d}" get_daily_summaries \
    "{\"startdate\":\"$d\",\"enddate\":\"$d\",\"metrics\":$METRICS,\"force_refresh\":$force}"
  rc=$?
  set -e
  if [ "$rc" = "2" ]; then
    consec_429=$((consec_429 + 1))
    failed_days=$((failed_days + 1))
    if [ "$consec_429" -ge "$MAX_CONSECUTIVE_429" ]; then
      echo "  ABORT: $MAX_CONSECUTIVE_429 consecutive Garmin 429s — skipping remaining steps to avoid extending the throttle window" >&2
      aborted=true
      break
    fi
  elif [ "$rc" != "0" ]; then
    consec_429=0
    failed_days=$((failed_days + 1))
  else
    consec_429=0
  fi
done
if [ "$failed_days" -gt 0 ]; then
  echo "  WARNING: $failed_days day(s) failed — run again to retry" >&2
fi

if [ "$aborted" = "true" ]; then
  echo
  echo "Aborted early due to Garmin rate-limit. Cache totals (what we have):"
  curl -s --max-time 30 "$MCP_URL/cache/count?tool=daily_summary" \
    | python3 -c "import json,sys; print('  daily_summary:', json.load(sys.stdin).get('count'))" || true
  exit 0   # exit 0 so the GitHub Action doesn't mark the run as failed every day
fi

echo
echo "[2/5] get_activities — force_refresh month(s): $months_to_refresh"
for ym in $months_to_refresh; do
  year=$(echo "$ym" | cut -d- -f1)
  month=$(echo "$ym" | cut -d- -f2)
  # Pass year/month as env vars so Python doesn't parse "04" as octal literal.
  start=$(YEAR=$year MONTH=$month python3 -c "
import os
print(f\"{os.environ['YEAR']}-{os.environ['MONTH']}-01\")
")
  end=$(YEAR=$year MONTH=$month python3 -c "
import os
from calendar import monthrange
from datetime import date
y = int(os.environ['YEAR']); m = int(os.environ['MONTH'])
last = monthrange(y, m)[1]
print(date(y, m, last).isoformat())
")
  # Don't ask Garmin for dates beyond today.
  if [ "$end" \> "$today" ]; then end="$today"; fi
  echo "  $ym ($start -> $end)"
  set +e
  call_mcp "activities_${ym}" get_activities \
    "{\"startdate\":\"$start\",\"enddate\":\"$end\",\"force_refresh\":true}"
  rc=$?
  set -e
  if [ "$rc" = "2" ]; then
    consec_429=$((consec_429 + 1))
    if [ "$consec_429" -ge "$MAX_CONSECUTIVE_429" ]; then
      echo "  ABORT: consecutive Garmin 429s — skipping remaining steps" >&2
      exit 0
    fi
  elif [ "$rc" = "0" ]; then
    consec_429=0
  fi
done

echo
echo "[3/5] get_scheduled_workouts — next $SCHEDULED_LOOKAHEAD_DAYS days (force_refresh month(s))"
ahead_end=$(python3 -c "from datetime import date, timedelta; print((date.today() + timedelta(days=$SCHEDULED_LOOKAHEAD_DAYS)).isoformat())")
echo "  window $today -> $ahead_end"
set +e
call_mcp "scheduled_workouts" get_scheduled_workouts \
  "{\"startdate\":\"$today\",\"enddate\":\"$ahead_end\",\"force_refresh\":true}"
rc=$?
set -e
if [ "$rc" = "2" ]; then
  consec_429=$((consec_429 + 1))
  if [ "$consec_429" -ge "$MAX_CONSECUTIVE_429" ]; then
    echo "  ABORT: consecutive Garmin 429s — skipping workout structure prewarm" >&2
    exit 0
  fi
fi

# Parse the response to extract workoutIds and prewarm each one.
echo
echo "[4/5] get_workout_by_id — prewarm structures for scheduled workouts"
workout_ids=$(python3 -c "
import json
try:
    d = json.load(open('/tmp/mcp_refresh_scheduled_workouts.json'))
    # Response is MCP envelope: {'result': {'content': [{'type':'text','text': '<json>'}]}}
    content = d.get('result', {}).get('content', [])
    items = []
    for c in content:
        if c.get('type') == 'text':
            items = json.loads(c.get('text', '[]'))
            break
    ids = sorted({str(it.get('workoutId')) for it in items if it.get('workoutId')})
    print(' '.join(ids))
except Exception as ex:
    print('', end='')
" 2>/dev/null || echo "")

if [ -z "$workout_ids" ]; then
  echo "  (no scheduled workouts found, or parse failed — skipping)"
else
  wcount=$(echo "$workout_ids" | wc -w | tr -d ' ')
  echo "  prewarming $wcount workout structure(s)"
  for wid in $workout_ids; do
    call_mcp "workout_${wid}" get_workout_by_id \
      "{\"workout_id\":\"$wid\"}" || true
  done
fi

echo
echo "[5/5] derived metrics — force-refresh the daily-updating ones so the"
echo "      web MCP (GARMIN_READONLY=true) can serve them from cache."
prewarm_one() {
  local label="$1" name="$2" args="$3"
  echo "  $name"
  set +e
  call_mcp "$label" "$name" "$args"
  rc=$?
  set -e
  if [ "$rc" = "2" ]; then
    consec_429=$((consec_429 + 1))
    if [ "$consec_429" -ge "$MAX_CONSECUTIVE_429" ]; then
      echo "  ABORT: consecutive Garmin 429s — stopping derived-metric prewarm" >&2
      return 99   # signal caller to exit
    fi
  elif [ "$rc" = "0" ]; then
    consec_429=0
  fi
  return 0
}

prewarm_one "race_predictions"      get_race_predictions     '{"force_refresh":true}' || [ $? -eq 99 ] && exit 0
prewarm_one "lactate_threshold"     get_lactate_threshold    '{"force_refresh":true}' || [ $? -eq 99 ] && exit 0
prewarm_one "training_score_hill"   get_training_score       "{\"metric\":\"hill\",\"startdate\":\"$today\",\"force_refresh\":true}" || [ $? -eq 99 ] && exit 0
prewarm_one "training_score_endur"  get_training_score       "{\"metric\":\"endurance\",\"startdate\":\"$today\",\"force_refresh\":true}" || [ $? -eq 99 ] && exit 0
prewarm_one "personal_records"      get_personal_records     '{"force_refresh":true}' || [ $? -eq 99 ] && exit 0

# Body composition: only if user logs weight. Pull a 30-day window.
body_start=$(python3 -c "from datetime import date, timedelta; print((date.today() - timedelta(days=30)).isoformat())")
prewarm_one "body_composition"      get_body_composition     "{\"startdate\":\"$body_start\",\"enddate\":\"$today\",\"force_refresh\":true}" || [ $? -eq 99 ] && exit 0

echo
echo "Cache totals:"
curl -s --max-time 30 "$MCP_URL/cache/count?tool=daily_summary" \
  | python3 -c "import json,sys; print('  daily_summary:', json.load(sys.stdin).get('count'))" || true
curl -s --max-time 30 "$MCP_URL/cache/count?tool=activities_month" \
  | python3 -c "import json,sys; print('  activities_month:', json.load(sys.stdin).get('count'))" || true
curl -s --max-time 30 "$MCP_URL/cache/count?tool=calendar_month" \
  | python3 -c "import json,sys; print('  calendar_month:', json.load(sys.stdin).get('count'))" || true
curl -s --max-time 30 "$MCP_URL/cache/count?tool=workout_by_id" \
  | python3 -c "import json,sys; print('  workout_by_id:', json.load(sys.stdin).get('count'))" || true
for tool in race_predictions lactate_threshold training_score personal_records body_composition; do
  curl -s --max-time 30 "$MCP_URL/cache/count?tool=$tool" \
    | python3 -c "import json,sys,os; print(f'  {os.environ[\"T\"]}:', json.load(sys.stdin).get('count'))" T=$tool || true
done
curl -s --max-time 30 "$MCP_URL/cache/count?tool=activities_in_range" \
  | python3 -c "import json,sys; c=json.load(sys.stdin).get('count'); print('  activities_in_range (legacy):', c) if c else None" || true

echo
echo "Done."
