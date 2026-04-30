#!/usr/bin/env bash
# Backfill activity_details cache for the last N months of activities.
#
# Usage:
#   ./scripts/backfill_activity_details.sh [months]
#
# Defaults to 6 months. Reads activity IDs from the monthly activities cache
# (so backfill_activities.sh must have run first). Fires one MCP call per
# activity, ~5 Garmin requests each. Expect ~15-20 min for 6 months.
set -euo pipefail

MCP_URL="${MCP_URL:-https://garmin-mcp-rnwu.onrender.com}"
MONTHS="${1:-6}"
REQ_TIMEOUT_SEC=60
SLEEP_BETWEEN=2   # light throttle between activities

echo "Fetching bearer from $MCP_URL/token..."
BEARER=$(curl -s --max-time 30 -X POST "$MCP_URL/token" \
  -d 'grant_type=authorization_code&code=x' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
if [ -z "$BEARER" ]; then
  echo "ERROR: could not obtain bearer token" >&2
  exit 1
fi

# Compute start date: today minus MONTHS months.
start_date=$(MONTHS=$MONTHS python3 -c "
import os
from datetime import date
from dateutil.relativedelta import relativedelta
print((date.today() - relativedelta(months=int(os.environ['MONTHS']))).isoformat())
" 2>/dev/null || MONTHS=$MONTHS python3 -c "
import os
from datetime import date, timedelta
# Approximate if dateutil is missing: 30 days per month.
print((date.today() - timedelta(days=30 * int(os.environ['MONTHS']))).isoformat())
")
today=$(python3 -c "from datetime import date; print(date.today().isoformat())")

echo "Range: $start_date -> $today"
echo

# Fetch activity IDs via the MCP get_activities tool (reads from monthly cache).
echo "Fetching activity list..."
payload=$(START=$start_date END=$today python3 -c '
import json, os
print(json.dumps({
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {"name": "get_activities",
             "arguments": {"startdate": os.environ["START"],
                           "enddate": os.environ["END"]}}
}))')

curl -s --max-time 120 -o /tmp/mcp_activity_list.json \
  -X POST "$MCP_URL/mcp" \
  -H "Authorization: Bearer $BEARER" \
  -H "Content-Type: application/json" \
  -d "$payload"

# Extract activity IDs from the MCP envelope.
ids=$(python3 <<'PYEOF'
import json
with open("/tmp/mcp_activity_list.json") as fh:
    rpc = json.load(fh)
text = rpc["result"]["content"][0]["text"]
acts = json.loads(text)
for a in acts:
    aid = a.get("activityId")
    if aid is not None:
        print(aid)
PYEOF
)

if [ -z "$ids" ]; then
  echo "No activities found in range."
  exit 0
fi

total=$(echo "$ids" | wc -l | tr -d ' ')
echo "Found $total activities. Fetching details..."
echo

idx=0
failed=0
for aid in $ids; do
  idx=$((idx + 1))
  payload=$(AID=$aid python3 -c '
import json, os
print(json.dumps({
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {"name": "get_activity_details",
             "arguments": {"activity_id": os.environ["AID"]}}
}))')

  http=$(curl -s --max-time $REQ_TIMEOUT_SEC -o /tmp/mcp_act_details.json \
    -w "%{http_code}" -X POST "$MCP_URL/mcp" \
    -H "Authorization: Bearer $BEARER" \
    -H "Content-Type: application/json" \
    -d "$payload" || echo "000")

  if [ "$http" = "200" ]; then
    echo "  [$idx/$total] $aid — ok"
  else
    echo "  [$idx/$total] $aid — FAILED (http=$http)" >&2
    failed=$((failed + 1))
  fi

  # Throttle so Garmin OAuth doesn't throttle us.
  sleep $SLEEP_BETWEEN
done

echo
echo "Done. Final activity_details count:"
curl -s --max-time 30 "$MCP_URL/cache/count?tool=activity_details" \
  | python3 -c "import json,sys; print(' ', json.load(sys.stdin).get('count'))"

if [ "$failed" -gt 0 ]; then
  echo "WARNING: $failed activity/activities failed. Rerun to retry — cached ones will skip." >&2
  exit 1
fi
