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

    # 3. Activity details for any activity from today.
    print("[3/3] activity details for today's activities")
    try:
        todays_acts = garmin.get_activities_in_range(
            startdate=today_iso, enddate=today_iso
        ) or []
        print(f"  found {len(todays_acts)} activit(ies) today")
        for act in todays_acts:
            aid = act.get("activityId")
            if not aid:
                continue
            try:
                # force_refresh=True — activities can change briefly after
                # upload (gear edits, name fixes) so refresh once mid-day.
                garmin.get_activity_details(str(aid), force_refresh=True)
                print(f"    activity {aid}: refreshed")
            except Exception as ex:  # noqa: BLE001
                print(f"    activity {aid}: ERROR {str(ex)[:120]}", file=sys.stderr)
    except Exception as ex:  # noqa: BLE001
        print(f"  ERROR: {str(ex)[:200]}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
