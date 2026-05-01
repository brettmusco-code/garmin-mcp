"""Gap-filling backfill that talks to Garmin directly (no MCP hop).

Imports app.garmin and calls its cached functions with force_refresh=False.
The cache layer in app.garmin checks R2 first and only hits Garmin for
missing (metric, date) pairs — exactly what we want.

Because this bypasses the MCP entirely, it doesn't care about
GARMIN_READONLY on the deployed service. The Render web MCP stays in
readonly mode; this script runs in a fresh GitHub container with its
own env (live mode) and writes results to the shared R2 bucket.

Required env (all from GitHub Action secrets):
  GARTH_TOKENS_B64        Fresh garth OAuth tokens
  S3_CACHE_BUCKET         R2 bucket name
  S3_ENDPOINT_URL         R2 endpoint URL
  AWS_ACCESS_KEY_ID       R2 access key
  AWS_SECRET_ACCESS_KEY   R2 secret
  S3_CACHE_PREFIX         Optional, default "garmin-mcp/"

Inputs (env):
  DAYS_BACK               How many days back (default 180)

Behavior:
  - For each (metric, date) in the window, check R2. If cached, skip.
  - If missing, call Garmin via app.garmin. Write to R2 on success.
  - Abort after 2 consecutive Garmin 429s to protect the rate-limit window.
  - Pace with 2s sleep between per-day calls (prevents per-second throttling).
  - Report totals at the end.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, timedelta

# Ensure we can import app.* when run from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import cache, garmin  # noqa: E402

DAYS_BACK = int(os.environ.get("DAYS_BACK", "180"))
SLEEP_BETWEEN_DAYS = float(os.environ.get("SLEEP_BETWEEN_DAYS", "2"))
MAX_CONSECUTIVE_429 = int(os.environ.get("MAX_CONSECUTIVE_429", "2"))

METRICS = [
    "steps", "sleep", "stress", "rhr", "hrv", "respiration",
    "training_readiness", "training_status", "max_metrics",
    "intensity_minutes", "stats_and_body", "body_battery_events",
    "morning_readiness", "nutrition_food_log", "nutrition_meals",
]


def is_rate_limit(ex: BaseException) -> bool:
    msg = str(ex).lower()
    return "429" in msg or "too many requests" in msg or "rate limit" in msg


def main() -> int:
    if garmin.READONLY_MODE:
        print("ERROR: GARMIN_READONLY=true is set in the environment. "
              "Unset it for backfill — this script must be able to reach Garmin.",
              file=sys.stderr)
        return 1

    if not cache.enabled():
        print("ERROR: R2 cache is not configured (missing S3_CACHE_BUCKET).",
              file=sys.stderr)
        return 1

    end = date.today()
    start = end - timedelta(days=DAYS_BACK)
    all_dates = [(start + timedelta(days=i)).isoformat()
                 for i in range((end - start).days + 1)]

    print(f"=== backfill_direct ===")
    print(f"Window: {start.isoformat()} → {end.isoformat()} ({len(all_dates)} days)")
    print(f"Metrics: {len(METRICS)}")
    print()

    consec_429 = 0
    total_fetched = 0
    total_failed = 0
    aborted = False

    for metric in METRICS:
        # Build the set of dates already cached for this metric.
        # Cache key path: {PREFIX}daily_summary/{metric}/{date}.json
        prefix_for_metric = f"daily_summary/{metric}"
        cached_keys = cache.list_keys(tool_prefix=prefix_for_metric, limit=10000)
        cached_dates: set[str] = set()
        for k in cached_keys:
            base = k.rsplit("/", 1)[-1]  # e.g. "2025-06-15.json"
            if base.endswith(".json"):
                cached_dates.add(base[:-5])

        missing = [d for d in all_dates if d not in cached_dates]
        print(f"--- {metric} ---")
        print(f"  cached: {len(cached_dates)}  |  missing: {len(missing)}")

        if not missing:
            continue

        fetched = 0
        failed = 0
        for d in missing:
            if consec_429 >= MAX_CONSECUTIVE_429:
                print(f"  ABORT: {MAX_CONSECUTIVE_429} consecutive 429s. "
                      f"Stopping to avoid extending the Garmin throttle window.",
                      file=sys.stderr)
                aborted = True
                break
            try:
                result = garmin.get_daily_summaries(
                    startdate=d, enddate=d, metrics=[metric]
                )
                payload = result.get(metric, {}).get(d)
                if isinstance(payload, dict) and "error" in payload:
                    # Garmin returned an error for this metric-date.
                    if is_rate_limit_err := (
                        "429" in payload["error"].lower()
                        or "rate limit" in payload["error"].lower()
                    ):
                        consec_429 += 1
                        failed += 1
                    else:
                        # e.g. "no data for this date" — normal, count as fetched
                        consec_429 = 0
                        fetched += 1
                else:
                    consec_429 = 0
                    fetched += 1
                    total_fetched += 1
            except Exception as ex:  # noqa: BLE001
                if is_rate_limit(ex):
                    consec_429 += 1
                else:
                    consec_429 = 0
                failed += 1
                total_failed += 1
            time.sleep(SLEEP_BETWEEN_DAYS)

        print(f"  fetched: {fetched}, failed: {failed}")
        if aborted:
            break

    print()
    print("=== Done ===")
    print(f"Total new metric-days fetched: {total_fetched}")
    print(f"Total failed: {total_failed}")
    print()
    print("Cache counts per metric:")
    for m in METRICS:
        count = cache.count_keys(tool_prefix=f"daily_summary/{m}")
        print(f"  {m}: {count}")

    return 2 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())
