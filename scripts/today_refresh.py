"""Activity-aware mid-day refresh.

Runs every 2 hours. Three-tier refresh strategy to cut Garmin API calls
from ~168/day (old: force-refresh all 12 metrics every run) to ~60-72/day:

  LIVE_METRICS       always force-refreshed (change continuously during day)
  POST_SYNC_METRICS  force-refreshed only when a new activity is detected
  MORNING_ONCE_METRICS  cache-first only (finalized at dawn, never change intraday)

Activity detection: before any data refresh, we force-refresh the current
month's activity list (1 call) and compare the count against what was in R2
before the refresh. If the count increased, a new activity synced and we
trigger the post-sync metrics + activity details. Otherwise we skip them.

Required env: same as daily_refresh.py
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta

logging.getLogger("garminconnect").setLevel(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import cache, garmin, tokens  # noqa: E402

# Updates step-by-step throughout the day — always worth refreshing.
LIVE_METRICS = ["steps", "stress", "body_battery_events", "stats_and_body"]

# Only meaningful after an activity syncs or food is logged. Force-refresh
# only when the activity list shows a new entry.
POST_SYNC_METRICS = [
    "training_readiness",
    "training_status",
    "nutrition_food_log",
    "nutrition_meals",
]

# Finalized once in the morning (overnight HRV, sleep score, RHR).
# The nightly daily_refresh handles these; no point re-fetching intraday.
MORNING_ONCE_METRICS = ["sleep", "hrv", "rhr", "morning_readiness"]


def _is_refresh_blocked(ex: BaseException) -> bool:
    return "ALLOW_OAUTH_REFRESH" in str(ex)


def _is_rate_limit(ex_or_msg) -> bool:
    s = str(ex_or_msg).lower()
    return "429" in s or "too many requests" in s or "rate limit" in s


def _cached_activity_count(year: int, month: int) -> int:
    """Return the activity count from R2 cache WITHOUT hitting Garmin."""
    data = cache.get(
        "activities_month",
        {"year": year, "month": month},
        key_parts=[f"{year:04d}-{month:02d}"],
        ttl_seconds=24 * 3600,
    )
    return len(data) if isinstance(data, list) else 0


def main() -> int:
    if garmin.READONLY_MODE:
        print("ERROR: GARMIN_READONLY set. Unset it for refresh jobs.",
              file=sys.stderr)
        return 1
    if not cache.enabled():
        print("ERROR: R2 cache not configured.", file=sys.stderr)
        return 1

    # Abort before touching Garmin if an API 429 cooldown is active.
    api_remaining, api_reason = tokens.load_api_429_cooldown_remaining()
    if api_remaining > 0:
        hrs = api_remaining // 3600
        mins = (api_remaining % 3600) // 60
        print(
            f"ERROR: Garmin API 429 cooldown active — {hrs}h {mins}m remaining. "
            f"Last error: {api_reason or 'unknown'}. Skipping today_refresh.",
            file=sys.stderr,
        )
        return 1

    today = date.today()
    today_iso = today.isoformat()
    yesterday = today - timedelta(days=1)
    yesterday_iso = yesterday.isoformat()
    print(f"=== today_refresh {today_iso} ===")

    # [0/4] OAuth preflight — ensure token is valid before fan-out.
    print("[0/4] OAuth preflight")
    try:
        garmin.ensure_oauth_ready()
        print("  ok")
    except Exception as ex:  # noqa: BLE001
        if _is_refresh_blocked(ex):
            print(
                "  ERROR: OAuth token expired but ALLOW_OAUTH_REFRESH=false.",
                file=sys.stderr,
            )
        else:
            print(f"  ERROR: OAuth preflight failed: {str(ex)[:200]}",
                  file=sys.stderr)
        return 1

    # [1/4] Activity detection — 1 API call.
    # Read the cached count BEFORE the force-refresh so we can detect new
    # entries. A higher count means an activity synced since the last run.
    print(f"[1/4] Activity detection ({today.year}-{today.month:02d})")
    prev_count = _cached_activity_count(today.year, today.month)
    try:
        new_month = garmin._fetch_activities_month(
            today.year, today.month, force_refresh=True
        )
        new_count = len(new_month) if isinstance(new_month, list) else 0
        activity_synced = new_count > prev_count
        delta = new_count - prev_count
        status = (f"NEW activity detected (+{delta})" if activity_synced
                  else "no new activity")
        print(f"  {prev_count} → {new_count} ({status})")
    except Exception as ex:  # noqa: BLE001
        if _is_rate_limit(ex):
            print(f"  ERROR: rate limited — stopping: {str(ex)[:150]}",
                  file=sys.stderr)
            return 1
        print(f"  ERROR: {str(ex)[:200]}", file=sys.stderr)
        activity_synced = False
        new_month = []
        new_count = prev_count

    # [2/4] Live metrics — always force-refresh (steps/stress/body battery
    # change continuously; stats_and_body accumulates throughout the day).
    print(f"[2/4] Live metrics ({len(LIVE_METRICS)} metrics, force_refresh=True)")
    try:
        result = garmin.get_daily_summaries(
            startdate=today_iso, enddate=today_iso,
            metrics=LIVE_METRICS, force_refresh=True,
        )
        errored = sum(
            1 for m in LIVE_METRICS
            if isinstance(result.get(m, {}).get(today_iso), dict)
            and "error" in result[m][today_iso]
        )
        rate_limited = sum(
            1 for m in LIVE_METRICS
            if isinstance(result.get(m, {}).get(today_iso), dict)
            and _is_rate_limit(result[m][today_iso].get("error", ""))
        )
        if rate_limited:
            print(f"  ERROR: {rate_limited} metric(s) rate-limited — stopping",
                  file=sys.stderr)
            return 1
        ok = len(LIVE_METRICS) - errored
        print(f"  {ok}/{len(LIVE_METRICS)} refreshed" +
              (f", {errored} errors" if errored else ""))
    except Exception as ex:  # noqa: BLE001
        if _is_refresh_blocked(ex):
            print("  ERROR: token expired and not refresh-authorized",
                  file=sys.stderr)
            return 1
        print(f"  ERROR: {str(ex)[:200]}", file=sys.stderr)
        return 1

    # [3/4] Post-sync metrics — force-refresh only when a new activity
    # appeared; otherwise cache-first (avoids API calls when nothing changed).
    post_force = activity_synced
    print(f"[3/4] Post-sync metrics ({len(POST_SYNC_METRICS)} metrics, "
          f"force_refresh={post_force})")
    try:
        result = garmin.get_daily_summaries(
            startdate=today_iso, enddate=today_iso,
            metrics=POST_SYNC_METRICS, force_refresh=post_force,
        )
        errored = sum(
            1 for m in POST_SYNC_METRICS
            if isinstance(result.get(m, {}).get(today_iso), dict)
            and "error" in result[m][today_iso]
        )
        rate_limited = sum(
            1 for m in POST_SYNC_METRICS
            if isinstance(result.get(m, {}).get(today_iso), dict)
            and _is_rate_limit(result[m][today_iso].get("error", ""))
        )
        if rate_limited:
            print(f"  ERROR: {rate_limited} metric(s) rate-limited — stopping",
                  file=sys.stderr)
            return 1
        ok = len(POST_SYNC_METRICS) - errored
        print(f"  {ok}/{len(POST_SYNC_METRICS)} " +
              ("refreshed" if post_force else "served from cache/fetched if missing") +
              (f", {errored} errors" if errored else ""))
    except Exception as ex:  # noqa: BLE001
        print(f"  ERROR: {str(ex)[:200]}", file=sys.stderr)

    # [4/4] Activity details for new activities (only on activity sync).
    # Also handles yesterday cache-first for UTC-timezone tolerance.
    print("[4/4] Activity details")
    if activity_synced and isinstance(new_month, list):
        today_acts = [
            a for a in new_month
            if str(a.get("startTimeLocal") or a.get("startTimeGMT") or "")
               .startswith(today_iso)
        ]
        yesterday_acts = [
            a for a in new_month
            if str(a.get("startTimeLocal") or a.get("startTimeGMT") or "")
               .startswith(yesterday_iso)
        ]
        new_acts = today_acts + yesterday_acts
        print(f"  activity sync detected — prewarming {len(new_acts)} activit(ies)")
        for act in new_acts:
            aid = act.get("activityId")
            if not aid:
                continue
            is_today = str(
                act.get("startTimeLocal") or act.get("startTimeGMT") or ""
            ).startswith(today_iso)
            try:
                garmin.get_activity_details(str(aid), force_refresh=is_today)
                print(f"    {aid}: {'refreshed' if is_today else 'cached/fetched'}")
            except Exception as ex:  # noqa: BLE001
                print(f"    {aid}: ERROR {str(ex)[:120]}", file=sys.stderr)
                if _is_rate_limit(ex):
                    print("  rate limited on activity details — stopping",
                          file=sys.stderr)
                    return 1
    else:
        print("  no new activity — skipping (cache warmed by nightly run)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
