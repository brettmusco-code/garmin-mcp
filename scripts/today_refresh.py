"""Two-mode mid-day refresh, driven by TODAY_REFRESH_MODE env var.

  live    (default) — force-refresh live intraday metrics every 6 hours.
                      4 API calls: steps, stress, body_battery_events,
                      stats_and_body. These change continuously during the
                      day; everything else is handled by the nightly run.

  workout           — check for a new activity every hour and, if one
                      synced, force-refresh the post-sync metrics (incl.
                      stats_and_body's calorie totals) and activity details.
                      ~2 API calls normally (nutrition + stats_and_body);
                      ~11 when a workout appears.

Two separate GitHub Actions workflows call this script on different cron
schedules, passing the appropriate mode via the env var.

Required env: GARTH_TOKENS_B64, S3_CACHE_BUCKET, S3_ENDPOINT_URL,
              AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta

logging.getLogger("garminconnect").setLevel(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import cache, garmin, tokens  # noqa: E402

MODE = os.environ.get("TODAY_REFRESH_MODE", "live").lower()

# Changes continuously — always worth a 6-hour refresh.
LIVE_METRICS = ["steps", "stress", "body_battery_events", "stats_and_body"]

# Only meaningful after an activity syncs or food is logged. stats_and_body
# carries the day's aggregate calorie totals (bmrKilocalories/
# activeKilocalories/totalKilocalories) — the "everyday" + all-day burn the
# fueling plan needs, not just the individual workout's own calorie figure.
# It has to be here (and force-refreshed) so those totals update the moment
# a workout syncs, not just on the next 6-hour live tick.
POST_SYNC_METRICS = [
    "training_readiness",
    "training_status",
    "nutrition_food_log",
    "nutrition_meals",
    "stats_and_body",
]


def _is_rate_limit(ex_or_msg) -> bool:
    s = str(ex_or_msg).lower()
    return (
        "429" in s
        or "too many requests" in s
        or "rate limit" in s
        # Soft-throttle (CDN empty-body) — same root cause, different surface
        or "soft throttle" in s
        or "expecting value" in s
    )


def _cached_activity_count(year: int, month: int) -> int:
    data = cache.get(
        "activities_month",
        {"year": year, "month": month},
        key_parts=[f"{year:04d}-{month:02d}"],
        ttl_seconds=24 * 3600,
    )
    return len(data) if isinstance(data, list) else 0


def _oauth_preflight() -> bool:
    """Return True if OAuth is ready (or transiently soft-throttled), False
    on a hard failure. Empty-body responses from Garmin's CDN classify as
    soft rate-limit; let the run continue since downstream calls have
    their own per-endpoint error handling and may succeed."""
    try:
        garmin.ensure_oauth_ready()
        print("  OAuth ok")
        return True
    except garmin.GarminRateLimitError as ex:
        if getattr(ex, "soft", False):
            print(f"  OAuth soft-throttled (proceeding): {str(ex)[:150]}",
                  file=sys.stderr)
            return True
        print(f"  ERROR: OAuth preflight rate-limited: {str(ex)[:200]}",
              file=sys.stderr)
        return False
    except Exception as ex:  # noqa: BLE001
        print(f"  ERROR: OAuth preflight failed: {str(ex)[:200]}", file=sys.stderr)
        return False


def _check_cooldowns() -> tuple[bool, str | None]:
    """Return (active, reason_str) — active=True means caller should skip this run.

    Checks both the API-call 429 cooldown and the OAuth exchange cooldown.
    Returns a non-empty reason string when active so callers can print a single
    clear SKIPPED message (exit 0) instead of an ERROR (exit 1). Skipping is
    intentional protective behavior, not a code failure.
    """
    api_remaining, api_reason = tokens.load_api_429_cooldown_remaining()
    if api_remaining > 0:
        hrs, mins = api_remaining // 3600, (api_remaining % 3600) // 60
        return True, (
            f"Garmin API 429 cooldown active — {hrs}h {mins}m remaining "
            f"(last error: {api_reason or 'unknown'}). Skipping to let "
            "Garmin's throttle window reset."
        )
    oauth_remaining, oauth_reason = tokens.load_cooldown_remaining()
    if oauth_remaining > 0:
        hrs, mins = oauth_remaining // 3600, (oauth_remaining % 3600) // 60
        return True, (
            f"Garmin OAuth 429 cooldown active — {hrs}h {mins}m remaining "
            f"(last error: {oauth_reason or 'unknown'}). Skipping to let "
            "Garmin's throttle window reset."
        )
    return False, None


def run_live() -> int:
    """Refresh live intraday metrics (6-hour cadence)."""
    print(f"=== today_refresh [live] {date.today()} ===")

    active, reason = _check_cooldowns()
    if active:
        print(f"SKIPPED: {reason}")
        return 0

    print("[0/1] OAuth preflight")
    if not _oauth_preflight():
        return 1

    today_iso = date.today().isoformat()
    print(f"[1/1] Live metrics ({len(LIVE_METRICS)}): {', '.join(LIVE_METRICS)}")
    try:
        # force_refresh=False so we honor any no-data sentinels written
        # by an earlier run within the last NO_DATA_SOFT_THROTTLE_TTL_SEC
        # (4h). Today's data is still picked up via the 24h TTL on real
        # cached values; sentinels short-circuit the Garmin call when
        # we already know the endpoint is throttled.
        result = garmin.get_daily_summaries(
            startdate=today_iso, enddate=today_iso,
            metrics=LIVE_METRICS, force_refresh=False,
        )
        # Garmin endpoints return mixed shapes: dicts for most metrics,
        # lists for body_battery_events / training_status. Only dicts
        # carry our error-shape from get_daily_summaries; treat anything
        # else as a successful fetch.
        def _payload(m):
            return result.get(m, {}).get(today_iso)
        rate_limited = sum(
            1 for m in LIVE_METRICS
            if isinstance(_payload(m), dict)
            and _is_rate_limit(_payload(m).get("error", ""))
        )
        errors = sum(
            1 for m in LIVE_METRICS
            if isinstance(_payload(m), dict) and "error" in _payload(m)
        )
        ok = len(LIVE_METRICS) - errors
        status = f"  {ok}/{len(LIVE_METRICS)} refreshed"
        if errors:
            status += f", {errors} errors"
        if rate_limited:
            status += f" ({rate_limited} rate-limited — sentinel cached)"
        print(status)
    except Exception as ex:  # noqa: BLE001
        print(f"  ERROR: {str(ex)[:200]}", file=sys.stderr)
        return 1

    # Soft throttles are a normal Garmin-side state, not a workflow
    # failure — the sentinel cache prevents re-hitting the same endpoint
    # within its TTL, and the daily anchor run will retry. Always return
    # 0 unless something genuinely broke (handled above).
    return 0


def run_workout_check() -> int:
    """Check for a new activity and refresh post-sync data if found (1-hour cadence)."""
    today = date.today()
    today_iso = today.isoformat()
    yesterday_iso = (today - timedelta(days=1)).isoformat()
    print(f"=== today_refresh [workout] {today_iso} ===")

    active, reason = _check_cooldowns()
    if active:
        print(f"SKIPPED: {reason}")
        return 0

    print("[0/3] OAuth preflight")
    if not _oauth_preflight():
        return 1

    # [1/3] Activity detection — 1 API call.
    print(f"[1/3] Activity detection ({today.year}-{today.month:02d})")
    prev_count = _cached_activity_count(today.year, today.month)
    new_month = None
    activity_synced = False
    try:
        new_month = garmin._fetch_activities_month(
            today.year, today.month, force_refresh=True
        )
        new_count = len(new_month) if isinstance(new_month, list) else 0
        activity_synced = new_count > prev_count
        print(f"  {prev_count} → {new_count} "
              f"({'NEW +' + str(new_count - prev_count) if activity_synced else 'no change'})")
    except garmin.GarminRateLimitError as ex:
        # Hard 429: stop here. The circuit breaker has tripped and
        # post-sync calls would all fail too.
        if not getattr(ex, "soft", False):
            print(f"  rate-limited — skipping post-sync refresh: {str(ex)[:150]}",
                  file=sys.stderr)
            return 0
        # Soft throttle: log and fall through. Use the cached month list
        # to detect newly-synced activities; post-sync metrics often
        # succeed even when the activities-by-date endpoint is empty.
        print(f"  soft-throttle on activities — using cached list: {str(ex)[:150]}",
              file=sys.stderr)
        new_month = cache.get(
            "activities_month",
            {"year": today.year, "month": today.month},
            key_parts=[f"{today.year:04d}-{today.month:02d}"],
            ttl_seconds=24 * 3600,
        )
        new_count = len(new_month) if isinstance(new_month, list) else 0
        # Without a fresh count we can't tell if a workout just synced.
        # Run post-sync refresh anyway IF the most recent cached activity
        # is from today — minimal cost, high value when a sync is pending.
        if isinstance(new_month, list) and new_month:
            latest = new_month[0]
            start = str(latest.get("startTimeLocal") or latest.get("startTimeGMT") or "")
            if start.startswith(today_iso):
                activity_synced = True
                print(f"  cached list shows an activity from today — refreshing post-sync anyway")
    except Exception as ex:  # noqa: BLE001
        if _is_rate_limit(ex):
            print(f"  rate-limited — skipping post-sync refresh: {str(ex)[:150]}",
                  file=sys.stderr)
            return 0
        print(f"  ERROR: {str(ex)[:200]}", file=sys.stderr)
        return 0  # non-fatal: skip post-sync refresh this run

    if not activity_synced:
        # No new activity, but food logged through the day still needs to flow
        # through — refresh nutrition every run and regenerate the plan so the
        # dashboard reflects intraday logging within the hour.
        print("[2/3] Post-sync metrics — nutrition only (no new activity)")
        try:
            garmin.get_daily_summaries(
                startdate=today_iso, enddate=today_iso,
                metrics=["nutrition_food_log", "nutrition_meals", "stats_and_body"],
                force_refresh=True,
            )
        except Exception as ex:  # noqa: BLE001
            print(f"  nutrition refresh error: {str(ex)[:120]}", file=sys.stderr)
        _refresh_fueling_plan()
        print("[3/3] Activity details — skipped (no new activity)")
        return 0

    # [2/3] Post-sync metrics — only reached when a new activity appeared, so
    # force_refresh=True is bounded to real events (not every hourly tick):
    # this is exactly the moment stale cached values (esp. stats_and_body's
    # calorie totals, which predate the workout that just synced) need to be
    # replaced, not silently reused for up to a day.
    print(f"[2/3] Post-sync metrics ({len(POST_SYNC_METRICS)}): "
          f"{', '.join(POST_SYNC_METRICS)}")
    try:
        result = garmin.get_daily_summaries(
            startdate=today_iso, enddate=today_iso,
            metrics=POST_SYNC_METRICS, force_refresh=True,
        )
        def _payload(m):
            return result.get(m, {}).get(today_iso)
        rate_limited = sum(
            1 for m in POST_SYNC_METRICS
            if isinstance(_payload(m), dict)
            and _is_rate_limit(_payload(m).get("error", ""))
        )
        if rate_limited:
            print(f"  {rate_limited} metric(s) rate-limited — sentinel cached, "
                  f"will retry next anchor run", file=sys.stderr)
        errors = sum(
            1 for m in POST_SYNC_METRICS
            if isinstance(_payload(m), dict) and "error" in _payload(m)
        )
        ok = len(POST_SYNC_METRICS) - errors
        print(f"  {ok}/{len(POST_SYNC_METRICS)} refreshed" +
              (f", {errors} errors" if errors else ""))
    except Exception as ex:  # noqa: BLE001
        print(f"  ERROR: {str(ex)[:200]}", file=sys.stderr)

    # [3/3] Activity details for today + yesterday (UTC-tolerant).
    print("[3/3] Activity details")
    if not isinstance(new_month, list):
        print("  no activity list available — skipping")
        return 0

    target_acts = [
        a for a in new_month
        if str(a.get("startTimeLocal") or a.get("startTimeGMT") or "")
           .startswith((today_iso, yesterday_iso))
    ]
    print(f"  {len(target_acts)} activit(ies) in today/yesterday window")
    for act in target_acts:
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
                return 1

    _refresh_fueling_plan()
    return 0


def _refresh_fueling_plan() -> None:
    """If a fueling goal is set, regenerate the plan (self-correcting from
    today's actuals) and save it into the weekly snapshot, so the dashboard /
    /nutrition / /morning always reflect the latest intake and burn. No-op
    when no goal is set. Runs every intraday refresh (~every few hours)."""
    try:
        if not (garmin.get_fueling_goal() or {}).get("goal"):
            return
        print("[fuel] regenerating fueling plan from latest actuals")
        res = garmin.generate_fueling_plan(days=7, save=True, rebalance=True)
        note = "; ".join(res.get("notes", [])[:1]) or "ok"
        print(f"  saved -> {res.get('saved_to_weekly_snapshot')} ({note})")
    except Exception as ex:  # noqa: BLE001
        print(f"  fuel refresh skipped: {str(ex)[:160]}", file=sys.stderr)


def main() -> int:
    if garmin.READONLY_MODE:
        print("ERROR: GARMIN_READONLY set. Unset it for refresh jobs.", file=sys.stderr)
        return 1
    if not cache.enabled():
        print("ERROR: R2 cache not configured.", file=sys.stderr)
        return 1

    if MODE == "workout":
        return run_workout_check()
    return run_live()


if __name__ == "__main__":
    sys.exit(main())
