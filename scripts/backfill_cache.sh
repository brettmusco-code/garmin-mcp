#!/usr/bin/env bash
# Backfill the R2 cache with a year+ of daily summaries.
#
# Usage:
#   ./scripts/backfill_cache.sh [start_chunk]
#
# Runs 60-day chunks sequentially against the deployed MCP server. Cached
# (metric, date) pairs return fast; only gaps hit Garmin. Optionally pass a
# starting chunk index (1-based) to resume mid-way.
#
# No args: starts at chunk 1.
# ./scripts/backfill_cache.sh 3 -> skips chunks 1 and 2.
set -euo pipefail

MCP_URL="${MCP_URL:-https://garmin-mcp-rnwu.onrender.com}"
METRICS='["steps","sleep","stress","rhr","hrv","respiration","training_readiness","training_status","max_metrics","intensity_minutes","stats_and_body"]'
CHUNK_TIMEOUT_SEC=900   # 15 min — Render free tier can be slow
SLEEP_BETWEEN=60        # cool-down between chunks to avoid Garmin OAuth 429
START_CHUNK="${1:-1}"

# Fetch bearer from the rubber-stamp OAuth /token endpoint.
echo "Fetching bearer from $MCP_URL/token..."
BEARER=$(curl -s --max-time 30 -X POST "$MCP_URL/token" \
  -d 'grant_type=authorization_code&code=x' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
if [ -z "$BEARER" ]; then
  echo "ERROR: could not obtain bearer token" >&2
  exit 1
fi
echo "Bearer acquired."

count_cached() {
  # Print the count or "?" if the server is unreachable. Never fail the script.
  local body
  body=$(curl -s --max-time 30 "$MCP_URL/cache/count?tool=daily_summary" || echo "")
  python3 -c "
import json,sys
s = '''$body'''
try:
    print(json.loads(s).get('count', '?'))
except Exception:
    print('?')
"
}

call_chunk() {
  local idx=$1 start=$2 end=$3
  echo
  echo "=== chunk $idx: $start -> $end ==="
  local before
  before=$(count_cached) || before="?"
  echo "cached before: $before"

  local payload
  payload=$(python3 -c "
import json
print(json.dumps({
  'jsonrpc':'2.0','id':$idx,'method':'tools/call',
  'params':{
    'name':'get_daily_summaries',
    'arguments':{'startdate':'$start','enddate':'$end','metrics':$METRICS}
  }
}))")

  local http
  http=$(curl -s --max-time $CHUNK_TIMEOUT_SEC -o /tmp/mcp_chunk_${idx}.json \
    -w "%{http_code}" -X POST "$MCP_URL/mcp" \
    -H "Authorization: Bearer $BEARER" \
    -H "Content-Type: application/json" \
    -d "$payload" || echo "000")
  echo "http: $http"

  local after
  after=$(count_cached) || after="?"
  if [[ "$before" =~ ^[0-9]+$ ]] && [[ "$after" =~ ^[0-9]+$ ]]; then
    echo "cached after:  $after (delta: $((after - before)))"
  else
    echo "cached after:  $after (delta: n/a)"
  fi

  if [ "$idx" -lt 8 ]; then
    echo "sleeping ${SLEEP_BETWEEN}s..."
    sleep $SLEEP_BETWEEN
  fi
}

# Chunks covering 2025-01-01 → 2026-04-29 (485 days, 60-day windows).
chunks=(
  "2025-01-01 2025-03-01"
  "2025-03-02 2025-05-01"
  "2025-05-02 2025-06-30"
  "2025-07-01 2025-08-29"
  "2025-08-30 2025-10-28"
  "2025-10-29 2025-12-27"
  "2025-12-28 2026-02-25"
  "2026-02-26 2026-04-29"
)

echo "Starting at chunk $START_CHUNK of ${#chunks[@]}. Expected total when done: ~5,335"
for i in "${!chunks[@]}"; do
  idx=$((i + 1))
  if [ "$idx" -lt "$START_CHUNK" ]; then continue; fi
  # shellcheck disable=SC2086
  call_chunk $idx ${chunks[$i]}
done

echo
echo "Done. Final cached count:"
count_cached
