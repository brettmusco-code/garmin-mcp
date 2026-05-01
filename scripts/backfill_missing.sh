#!/usr/bin/env bash
# Gap-filling backfill: fetch ONLY missing (metric, date) entries.
#
# Queries the R2 cache for each metric, computes the set of missing dates
# in the target window, and fetches just those from Garmin. Skips any date
# already cached. Paces requests with a short sleep between chunks to avoid
# rate limits.
#
# The deployed web MCP runs with GARMIN_READONLY=true so it can't be used
# for backfill. This script needs a "live" MCP endpoint — by default it
# points at garmin-mcp-rnwu.onrender.com but expects MCP to be configured
# live. In the GitHub Action we temporarily spin up a live MCP session by
# setting a different env.
#
# Required env:
#   MCP_URL       (default: https://garmin-mcp-rnwu.onrender.com)
#   MCP_BEARER    (optional — rubber-stamped via /token if unset)
#   DAYS_BACK     (default: 180)
#
# Usage:
#   DAYS_BACK=180 ./scripts/backfill_missing.sh
set -euo pipefail

MCP_URL="${MCP_URL:-https://garmin-mcp-rnwu.onrender.com}"
DAYS_BACK="${DAYS_BACK:-180}"
METRICS=(steps sleep stress rhr hrv respiration training_readiness training_status max_metrics intensity_minutes stats_and_body body_battery_events morning_readiness nutrition_food_log nutrition_meals)
REQ_TIMEOUT_SEC=90
SLEEP_BETWEEN_DAYS=2    # small pacing gap between per-day requests
MAX_CONSECUTIVE_429=2

end_date=$(python3 -c "from datetime import date; print(date.today().isoformat())")
start_date=$(python3 -c "from datetime import date, timedelta; print((date.today() - timedelta(days=$DAYS_BACK)).isoformat())")

echo "=== backfill_missing $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "Target: $MCP_URL"
echo "Window: $start_date -> $end_date ($DAYS_BACK days)"
echo "Metrics: ${METRICS[*]}"
echo

# Detect readonly mode — if the MCP won't call Garmin, abort early.
mode=$(curl -s --max-time 10 "$MCP_URL/health" | grep -oE "readonly|live" | head -1)
if [ "$mode" = "readonly" ]; then
  echo "ERROR: $MCP_URL is in readonly mode. Cannot backfill against it." >&2
  echo "Set GARMIN_READONLY=false (or unset it) on the MCP service, or point" >&2
  echo "this script at a live MCP instance." >&2
  exit 1
fi

if [ -z "${MCP_BEARER:-}" ]; then
  echo "Fetching bearer..."
  MCP_BEARER=$(curl -s --max-time 30 -X POST "$MCP_URL/token" \
    -d 'grant_type=authorization_code&code=x' \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
fi

# Build the full set of target dates.
dates=$(python3 -c "
from datetime import date, timedelta
s = date.fromisoformat('$start_date')
e = date.fromisoformat('$end_date')
cur = s
while cur <= e:
    print(cur.isoformat())
    cur += timedelta(days=1)
")

total_days=$(echo "$dates" | wc -l | tr -d ' ')
echo "Checking $total_days days × ${#METRICS[@]} metrics for gaps..."
echo

# For each metric, list what's cached, compute gaps, fetch them.
consec_429=0
total_fetched=0
total_skipped=0

for metric in "${METRICS[@]}"; do
  echo "--- $metric ---"
  # List cached keys for this metric. Key format: PREFIX/daily_summary/METRIC/DATE.json
  cached_dates=$(curl -s --max-time 30 "$MCP_URL/cache/list?tool=daily_summary/$metric&limit=1000" \
    | python3 -c "
import json, sys, re
try:
    d = json.load(sys.stdin)
    keys = d.get('keys', [])
    # Extract DATE from each key
    dates = set()
    for k in keys:
        m = re.search(r'/([0-9]{4}-[0-9]{2}-[0-9]{2})\.json$', k)
        if m:
            dates.add(m.group(1))
    for x in sorted(dates):
        print(x)
except Exception:
    pass
")
  cached_count=$(echo "$cached_dates" | grep -c . || true)
  echo "  already cached: $cached_count dates"

  # Compute missing dates: target dates - cached dates
  missing=$(comm -23 <(echo "$dates" | sort) <(echo "$cached_dates" | sort))
  missing_count=$(echo "$missing" | grep -c . || true)
  if [ "$missing_count" = "0" ]; then
    echo "  no gaps — skipping"
    total_skipped=$((total_skipped + cached_count))
    continue
  fi
  echo "  gaps to fetch: $missing_count"

  # Fetch each missing date via the MCP.
  fetched=0
  failed=0
  for d in $missing; do
    # Stop early if Garmin is rate-limiting us.
    if [ "$consec_429" -ge "$MAX_CONSECUTIVE_429" ]; then
      echo "  ABORT: $MAX_CONSECUTIVE_429 consecutive 429s — stopping" >&2
      break 2
    fi

    payload=$(METRIC="$metric" D="$d" python3 -c '
import json, os
print(json.dumps({
  "jsonrpc":"2.0","id":1,"method":"tools/call",
  "params":{"name":"get_daily_summaries","arguments":{
    "startdate": os.environ["D"],
    "enddate": os.environ["D"],
    "metrics": [os.environ["METRIC"]]
  }}
}))')
    outfile=/tmp/bf_${metric}_${d}.json
    http=$(curl -s --max-time $REQ_TIMEOUT_SEC -o "$outfile" -w "%{http_code}" \
      -X POST "$MCP_URL/mcp" \
      -H "Authorization: Bearer $MCP_BEARER" \
      -H "Content-Type: application/json" \
      -d "$payload" || echo "000")

    if [ "$http" != "200" ]; then
      failed=$((failed + 1))
      continue
    fi

    # Check for rate-limit error in MCP envelope.
    err=$(python3 -c "
import json
try:
    d = json.load(open('$outfile'))
    if 'error' in d:
        print(d['error'].get('message','')[:200])
    else:
        content = d.get('result',{}).get('content',[])
        for c in content:
            if c.get('type') == 'text':
                t = json.loads(c['text'])
                payload = t.get('$metric', {}).get('$d')
                if isinstance(payload, dict) and 'error' in payload:
                    print(payload['error'][:200])
except Exception: pass
")
    if echo "$err" | grep -qi "rate limit\|too many requests\|429"; then
      consec_429=$((consec_429 + 1))
      failed=$((failed + 1))
      echo "  $d: 429"
      continue
    fi
    consec_429=0
    fetched=$((fetched + 1))
    total_fetched=$((total_fetched + 1))
    rm -f "$outfile"

    # Pace: short sleep to avoid tripping Garmin's per-second limiter.
    sleep "$SLEEP_BETWEEN_DAYS"
  done
  echo "  fetched: $fetched, failed: $failed"
  echo
done

echo
echo "=== Done ==="
echo "Total fetched: $total_fetched"
echo
echo "Final cache counts (per metric):"
for m in "${METRICS[@]}"; do
  curl -s --max-time 15 "$MCP_URL/cache/count?tool=daily_summary/$m" \
    | python3 -c "import json,sys,os; print(f'  {os.environ[\"M\"]}:', json.load(sys.stdin).get('count'))" M="$m" || true
done
