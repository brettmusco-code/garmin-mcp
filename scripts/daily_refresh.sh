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
METRICS='["steps","sleep","stress","rhr","hrv","respiration","training_readiness","training_status","max_metrics","intensity_minutes","stats_and_body"]'
DAILY_LOOKBACK_DAYS=3
FORCE_REFRESH_DAYS=2       # re-fetch the last N days to catch late Garmin syncs
# Render free-tier proxy cuts requests off around ~100s. Keep per-call scope
# small so each HTTP request stays under that budget.
REQ_TIMEOUT_SEC=90

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
    return 1
  fi
}

echo "=== daily_refresh $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "Target: $MCP_URL"
echo

# Per-day fan-out: one HTTP call per (date). Keeps each request small
# enough for Render's free-tier proxy timeout. Cache hits return fast.
echo "[1/2] get_daily_summaries — $DAILY_LOOKBACK_DAYS days ending $today"
failed_days=0
for i in $(seq 0 $DAILY_LOOKBACK_DAYS); do
  d=$(python3 -c "from datetime import date, timedelta; print((date.today() - timedelta(days=$i)).isoformat())")
  force="false"
  if [ "$i" -lt "$FORCE_REFRESH_DAYS" ]; then force="true"; fi
  echo "  day $d (force_refresh=$force)"
  if ! call_mcp "daily_${d}" get_daily_summaries \
    "{\"startdate\":\"$d\",\"enddate\":\"$d\",\"metrics\":$METRICS,\"force_refresh\":$force}"; then
    failed_days=$((failed_days + 1))
  fi
done
if [ "$failed_days" -gt 0 ]; then
  echo "  WARNING: $failed_days day(s) failed — run again to retry" >&2
fi

echo
echo "[2/2] get_activities — force_refresh month(s): $months_to_refresh"
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
  call_mcp "activities_${ym}" get_activities \
    "{\"startdate\":\"$start\",\"enddate\":\"$end\",\"force_refresh\":true}"
done

echo
echo "Cache totals:"
curl -s --max-time 30 "$MCP_URL/cache/count?tool=daily_summary" \
  | python3 -c "import json,sys; print('  daily_summary:', json.load(sys.stdin).get('count'))" || true
curl -s --max-time 30 "$MCP_URL/cache/count?tool=activities_month" \
  | python3 -c "import json,sys; print('  activities_month:', json.load(sys.stdin).get('count'))" || true
curl -s --max-time 30 "$MCP_URL/cache/count?tool=activities_in_range" \
  | python3 -c "import json,sys; c=json.load(sys.stdin).get('count'); print('  activities_in_range (legacy):', c) if c else None" || true

echo
echo "Done."
