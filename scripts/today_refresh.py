"""Lightweight mid-day refresh — re-pulls TODAY's activities and their
details, plus today's daily summary metrics. Everything else (historical
days, scheduled workouts, derived metrics) stays as the nightly set it.

Runs every 2 hours via GitHub Actions so that a session finished at, say,
3pm is cached by ~5pm instead of waiting until the next nightly run.

Scope kept minimal on purpose — 1 container/2 hours × tiny call volume is
well under any rate limit.

Required env (same as daily_refresh.py):
  GARTH_TOKENS_B64, S3_CACHE_BUCKET, S3_ENDPOINT_URL,
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date

# garminconnect's login() calls logger.exception("Login failed") on any
# auth failure, which dumps a full traceback to stderr even when we
# catch the exception downstream. We handle auth explicitly below, so
# raise the garminconnect logger level to keep Actions output readable.
logging.getLogger("garminconnect").setLevel(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import cache, garmin, tokens  # noqa: E402

METRICS_TODAY = [
    "steps", "sleep", "stress", "rhr", "hrv",
    "training_readiness", "training_status", "stats_and_body",
    "body_battery_events", "morning_readiness",
    "nutrition_food_log", "nutrition_meals",
]


def _is_refresh_blocked(ex: BaseException) -> bool:
    """True if this run is configured as cache-only and cannot refresh OAuth."""
    return "ALLOW_OAUTH_REFRESH" in str(ex)


def _is_rate_limit(ex_or_msg) -> bool:
    s = str(ex_or_msg).lower()
    return "429" in s or "too many requests" in s or "rate limit" in s


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
    print(f"=== today_refresh {today_iso} ===")

    # 0. Refresh OAuth once up front if needed. This avoids discovering an
    # expired token inside the daily-summary fanout, where multiple worker
    # threads could otherwise try to authenticate at the same time.
    print("[0/3] OAuth preflight")
    try:
        garmin.ensure_oauth_ready()
        print("  ok")
    except Exception as ex:  # noqa: BLE001
        if _is_refresh_blocked(ex):
            print(
                "  ERROR: OAuth token expired but ALLOW_OAUTH_REFRESH=false. "
                "today-refresh must be refresh-authorized to guarantee "
                "2-hour cache updates.",
                file=sys.stderr,
            )
        else:
            print(f"  ERROR: OAuth preflight failed: {str(ex)[:200]}", file=sys.stderr)
        return 1

    # 1. Today's daily summary metrics (force refresh).
    print(f"[1/3] daily summaries for today ({len(METRICS_TODAY)} metrics)")
    try:
        result = garmin.get_daily_summaries(
            startdate=today_iso, enddate=today_iso,
            metrics=METRICS_TODAY, force_refresh=True,
        )
        # get_daily_summaries degrades gracefully — per-metric errors come
        # back as {"error": "..."} on individual metric-day entries. Count
        # them so an all-error no-op does not look like a fresh cache update.
        errored = 0
        blocked = 0
        rate_limited = 0
        for m in METRICS_TODAY:
            payload = result.get(m, {}).get(today_iso)
            if isinstance(payload, dict) and "error" in payload:
                errored += 1
                if _is_refresh_blocked(Exception(payload["error"])):
                    blocked += 1
                if _is_rate_limit(payload["error"]):
                    rate_limited += 1
        ok_count = len(METRICS_TODAY) - errored
        if errored == 0:
            print(f"  ok ({ok_count}/{len(METRICS_TODAY)} metrics refreshed)")
        elif rate_limited:
            print(
                f"  ERROR: {rate_limited} metric(s) hit rate limits — "
                "stopping refresh to avoid compounding the throttle window",
                file=sys.stderr,
            )
            return 1
        elif errored == len(METRICS_TODAY):
            if blocked == errored:
                print(
                    f"  ERROR: all {errored} metrics blocked — token expired "
                    "and this run is not refresh-authorized",
                    file=sys.stderr,
                )
            else:
                print(f"  ERROR: all {errored} metrics failed", file=sys.stderr)
            return 1
        else:
            print(f"  partial: {ok_count} refreshed, {errored} failed")
    except Exception as ex:  # noqa: BLE001
        if _is_refresh_blocked(ex):
            print(
                "  ERROR: token expired and this run is not refresh-authorized",
                file=sys.stderr,
            )
            return 1
        print(f"  ERROR: {str(ex)[:200]}", file=sys.stderr)
        return 1

    # 2. Current month's activities (force refresh to pick up new ones).
    print(f"[2/3] current month activities ({today.year}-{today.month:02d})")
    try:
        garmin._fetch_activities_month(today.year, today.month, force_refresh=True)
        print("  ok")
    except Exception as ex:  # noqa: BLE001
        if _is_refresh_blocked(ex):
            print(
                "  ERROR: token expired and this run is not refresh-authorized",
                file=sys.stderr,
            )
            return 1
        if _is_rate_limit(ex):
            print(
                "  ERROR: activities hit rate limits — stopping refresh to "
                "avoid compounding the throttle window",
                file=sys.stderr,
            )
            return 1
        print(f"  ERROR: {str(ex)[:200]}", file=sys.stderr)
        return 1

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
            start_ts = str(act.get("startTimeLocal") or act.get("startTimeGMT") or "")
            force_detail = start_ts.startswith(today_iso)
            try:
                # Force-refresh today's details because new uploads can keep
                # settling. For yesterday, use cache-first: it still fetches
                # when missing but avoids re-pulling the same immutable
                # details every 2 hours.
                garmin.get_activity_details(str(aid), force_refresh=force_detail)
                action = "refreshed" if force_detail else "cached/fetched"
                print(f"    activity {aid}: {action}")
            except Exception as ex:  # noqa: BLE001
                print(f"    activity {aid}: ERROR {str(ex)[:120]}", file=sys.stderr)
                if _is_rate_limit(ex):
                    return 1
    except Exception as ex:  # noqa: BLE001
        print(f"  ERROR: {str(ex)[:200]}", file=sys.stderr)
        if _is_rate_limit(ex):
            return 1

    # Baseline (physiology snapshot) is recomputed ONCE per day by the
    # nightly daily-refresh run. It's built from 90 days of data —
    # adding one mid-day activity shifts thresholds by <1%, not worth
    # the compute/rate-limit overhead on 12 runs/day.
    return 0


if __name__ == "__main__":
    sys.exit(main())
