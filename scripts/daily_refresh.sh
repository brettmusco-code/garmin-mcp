#!/usr/bin/env bash
# Keep the R2 cache current with recent Garmin data.
#
# - Daily summaries: pulls the last 7 days for 11 metrics. Per-(metric, date)
#   cache means repeated days are free; only new days hit Garmin.
# - Activities: pulls the last 2 days. These cache as one R2 object per range,
#   so the daily increment is 1 small entry.
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
METRICS='["steps","sleep","stress","rhr","hrv","respiration","training_readiness","training_status","max_metrics","intensity_minutes","stats_and_body"]'
DAILY_LOOKBACK_DAYS=7
ACTIVITIES_LOOKBACK_DAYS=2
REQ_TIMEOUT_SEC=600

today=$(python3 -c "from datetime import date; print(date.today().isoformat())")
daily_start=$(python3 -c "from datetime import date, timedelta; print((date.today() - timedelta(days=$DAILY_LOOKBACK_DAYS)).isoformat())")
acts_start=$(python3 -c "from datetime import date, timedelta; print((date.today() - timedelta(days=$ACTIVITIES_LOOKBACK_DAYS)).isoformat())")

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

call_mcp() {
  local name="$1" args="$2"
  local payload
  payload=$(python3 -c "
import json
print(json.dumps({
  'jsonrpc':'2.0','id':1,'method':'tools/call',
  'params':{'name':'$name','arguments':$args}
}))")
  local http
  http=$(curl -s --max-time $REQ_TIMEOUT_SEC -o /tmp/mcp_refresh_${name}.json \
    -w "%{http_code}" -X POST "$MCP_URL/mcp" \
    -H "Authorization: Bearer $MCP_BEARER" \
    -H "Content-Type: application/json" \
    -d "$payload" || echo "000")
  echo "  http=$http, bytes=$(wc -c < /tmp/mcp_refresh_${name}.json 2>/dev/null || echo 0)"
  # Surface errors from the MCP envelope so CI logs catch them.
  local err
  err=$(python3 -c "
import json
try:
    d = json.load(open('/tmp/mcp_refresh_${name}.json'))
    if 'error' in d:
        print(d['error'].get('message','unknown error'))
except Exception as ex:
    print(f'(could not parse response: {ex})')
")
  if [ -n "$err" ]; then
    echo "  ERROR: $err" >&2
    return 1
  fi
}

echo "=== daily_refresh $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "Target: $MCP_URL"
echo

echo "[1/2] get_daily_summaries $daily_start -> $today"
call_mcp get_daily_summaries "{\"startdate\":\"$daily_start\",\"enddate\":\"$today\",\"metrics\":$METRICS}"

echo
echo "[2/2] get_activities $acts_start -> $today (force_refresh to catch late edits)"
call_mcp get_activities "{\"startdate\":\"$acts_start\",\"enddate\":\"$today\",\"force_refresh\":true}"

echo
echo "Cache totals:"
curl -s --max-time 30 "$MCP_URL/cache/count?tool=daily_summary" \
  | python3 -c "import json,sys; print('  daily_summary:', json.load(sys.stdin).get('count'))" || true
curl -s --max-time 30 "$MCP_URL/cache/count?tool=activities_in_range" \
  | python3 -c "import json,sys; print('  activities_in_range:', json.load(sys.stdin).get('count'))" || true

echo
echo "Done."
