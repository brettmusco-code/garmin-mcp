#!/usr/bin/env bash
# Backfill the R2 cache with activity lists across a year+ range.
#
# Usage:
#   ./scripts/backfill_activities.sh [start_chunk]
#
# Fires one MCP call per yearly window. Results are large (several MB) so
# each call is cached as a single R2 object keyed by (startdate, enddate).
#
# No args: starts at chunk 1. Pass a chunk index to resume.
set -euo pipefail

MCP_URL="${MCP_URL:-https://garmin-mcp-rnwu.onrender.com}"
CHUNK_TIMEOUT_SEC=300   # activities calls are fast; 5 min is plenty
SLEEP_BETWEEN=30
START_CHUNK="${1:-1}"

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
  curl -s --max-time 30 "$MCP_URL/cache/count?tool=activities_in_range" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('count', 0))"
}

call_chunk() {
  local idx=$1 start=$2 end=$3
  echo
  echo "=== chunk $idx: activities $start -> $end ==="
  local before
  before=$(count_cached) || before="?"
  echo "cached before: $before"

  local payload
  payload=$(python3 -c "
import json
print(json.dumps({
  'jsonrpc':'2.0','id':$idx,'method':'tools/call',
  'params':{
    'name':'get_activities',
    'arguments':{'startdate':'$start','enddate':'$end'}
  }
}))")

  local http
  http=$(curl -s --max-time $CHUNK_TIMEOUT_SEC -o /tmp/mcp_activities_${idx}.json \
    -w "%{http_code}" -X POST "$MCP_URL/mcp" \
    -H "Authorization: Bearer $BEARER" \
    -H "Content-Type: application/json" \
    -d "$payload" || echo "000")
  echo "http: $http, payload_bytes: $(wc -c < /tmp/mcp_activities_${idx}.json 2>/dev/null || echo 0)"

  local after
  after=$(count_cached) || after="?"
  echo "cached after:  $after (delta: $((after - before)))"

  if [ "$idx" -lt 2 ]; then
    echo "sleeping ${SLEEP_BETWEEN}s..."
    sleep $SLEEP_BETWEEN
  fi
}

# Chunks covering 2025-01-01 → 2026-04-29 (two yearly windows).
chunks=(
  "2025-01-01 2025-12-31"
  "2026-01-01 2026-04-29"
)

echo "Starting at chunk $START_CHUNK of ${#chunks[@]}."
for i in "${!chunks[@]}"; do
  idx=$((i + 1))
  if [ "$idx" -lt "$START_CHUNK" ]; then continue; fi
  # shellcheck disable=SC2086
  call_chunk $idx ${chunks[$i]}
done

echo
echo "Done. Final activities_in_range cache count:"
count_cached
