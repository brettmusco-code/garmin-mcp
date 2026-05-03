"""Lightweight mid-day refresh — re-pulls TODAY's activities and their
details, plus today's daily summary metrics. Everything else (historical
days, scheduled workouts, derived metrics) stays as the 3am nightly set it.

Runs every 4 hours via GitHub Actions so that a session finished at, say,
3pm is cached by ~6pm instead of waiting until tomorrow's 3am nightly.

Scope kept minimal on purpose — 1 container/4 hours × tiny call volume is
well under any rate limit.

Required env (same as daily_refresh.py):
  GARTH_TOKENS_B64, S3_CACHE_BUCKET, S3_ENDPOINT_URL,
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import cache, garmin  # noqa: E402

METRICS_TODAY = [
    "steps", "sleep", "stress", "rhr", "hrv",
    "training_readiness", "training_status", "stats_and_body",
    "body_battery_events", "morning_readiness",
    "nutrition_food_log", "nutrition_meals",
]


def main() -> int:
    if garmin.READONLY_MODE:
        print("ERROR: GARMIN_READONLY set. Unset it for refresh jobs.",
              file=sys.stderr)
        return 1
    if not cache.enabled():
        print("ERROR: R2 cache not configured.", file=sys.stderr)
        return 1

    today = date.today()
    today_iso = today.isoformat()
    print(f"=== today_refresh {today_iso} ===")

    # 1. Today's daily summary metrics (force refresh).
    print(f"[1/3] daily summaries for today ({len(METRICS_TODAY)} metrics)")
    try:
        garmin.get_daily_summaries(
            startdate=today_iso, enddate=today_iso,
            metrics=METRICS_TODAY, force_refresh=True,
        )
        print("  ok")
    except Exception as ex:  # noqa: BLE001
        print(f"  ERROR: {str(ex)[:200]}", file=sys.stderr)

    # 2. Current month's activities (force refresh to pick up new ones).
    print(f"[2/3] current month activities ({today.year}-{today.month:02d})")
    try:
        garmin._fetch_activities_month(today.year, today.month, force_refresh=True)
        print("  ok")
    except Exception as ex:  # noqa: BLE001
        print(f"  ERROR: {str(ex)[:200]}", file=sys.stderr)

    # 3. Activity details for today AND yesterday.
    # GitHub Actions runs in UTC. A session completed at 6pm ET on May 1
    # is "yesterday" from a May 2 00:00 UTC container's perspective, so
    # we widen the window by a day to catch evening-local sessions.
    from datetime import timedelta
    yesterday = today - timedelta(days=1)
    yesterday_iso = yesterday.isoformat()
    print("[3/3] activity details for today + yesterday (UTC-tolerant)")
    try:
        acts = garmin.get_activities_in_range(
            startdate=yesterday_iso, enddate=today_iso
        ) or []
        print(f"  found {len(acts)} activit(ies) in {yesterday_iso}..{today_iso}")
        for act in acts:
            aid = act.get("activityId")
            if not aid:
                continue
            try:
                # force_refresh=True for today's; yesterday's is likely already
                # cached from the nightly — still safe to refresh once since
                # we only run every 4h.
                garmin.get_activity_details(str(aid), force_refresh=True)
                print(f"    activity {aid}: refreshed")
            except Exception as ex:  # noqa: BLE001
                print(f"    activity {aid}: ERROR {str(ex)[:120]}", file=sys.stderr)
    except Exception as ex:  # noqa: BLE001
        print(f"  ERROR: {str(ex)[:200]}", file=sys.stderr)

    # Refresh athlete baseline — new activities in the last hour may
    # shift thresholds (e.g. a hard interval session boosts the recency-
    # weighted 20-min power candidate). Cheap because most underlying
    # data is already cached from steps 1-3 above.
    print("[4/4] athlete_baseline — recompute with fresh activity data")
    try:
        garmin.get_athlete_baseline(force_refresh=True)
        print("  ok")
    except Exception as ex:  # noqa: BLE001
        print(f"  ERROR: {str(ex)[:200]}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
