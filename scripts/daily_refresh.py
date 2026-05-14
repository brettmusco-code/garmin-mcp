"""Nightly refresh — Python version (bypasses MCP readonly mode).

Runs in the GitHub Action's container. Imports app.garmin directly and
warms the R2 cache so the readonly web service can serve Garmin data
without making live Garmin calls.

Required env (from GitHub Action secrets):
  GARTH_TOKENS_B64        Fresh garth OAuth tokens
  S3_CACHE_BUCKET         R2 bucket name
  S3_ENDPOINT_URL         R2 endpoint URL
  AWS_ACCESS_KEY_ID       R2 access key
  AWS_SECRET_ACCESS_KEY   R2 secret
  S3_CACHE_PREFIX         Optional, default "garmin-mcp/"

Config:
  DAILY_LOOKBACK_DAYS=3   Days to pre-warm daily summaries for
  FORCE_REFRESH_DAYS=1    How many of those bypass cache (today only)

What gets refreshed:
  1. Daily summaries — last 3 days × 15 metrics
  2. Activities — current month (and previous on the first 3 days)
  3. Scheduled workouts — today + 14 days
  4. Workout structures — each workout in the 14-day window
  5. Derived daily metrics — race predictions, LT, training scores, PRs,
     body composition
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, timedelta

# Silence garminconnect's logger.exception("Login failed") which dumps
# a full traceback to stderr even when downstream code catches the
# exception. We handle auth errors explicitly; no need for the noise.
logging.getLogger("garminconnect").setLevel(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import cache, garmin, tokens  # noqa: E402

DAILY_LOOKBACK_DAYS = int(os.environ.get("DAILY_LOOKBACK_DAYS", "3"))
# today_refresh runs every 2h and already force-refreshes today's daily
# summaries. The nightly doesn't need to re-hit Garmin for data that's
# still inside the cache TTL — let the cache TTL (24h) serve it instead.
# Set >0 only if you need to guarantee a per-night Garmin poll
# (e.g. today_refresh is disabled).
FORCE_REFRESH_DAYS = int(os.environ.get("FORCE_REFRESH_DAYS", "0"))
SCHEDULED_LOOKAHEAD_DAYS = int(os.environ.get("SCHEDULED_LOOKAHEAD_DAYS", "14"))
MAX_CONSECUTIVE_429 = int(os.environ.get("MAX_CONSECUTIVE_429", "2"))

METRICS = [
    "steps", "sleep", "stress", "rhr", "hrv", "respiration",
    "training_readiness", "training_status", "max_metrics",
    "intensity_minutes", "stats_and_body", "body_battery_events",
    "morning_readiness", "nutrition_food_log", "nutrition_meals",
]


def is_rate_limit(ex_or_msg) -> bool:
    s = str(ex_or_msg).lower()
    return "429" in s or "too many requests" in s or "rate limit" in s


def abort_if_rate_limited(consec_429: int, step: str) -> bool:
    if consec_429 >= MAX_CONSECUTIVE_429:
        print(f"  ABORT: {MAX_CONSECUTIVE_429} consecutive 429s — skipping {step}",
              file=sys.stderr)
        return True
    return False


def main() -> int:
    if garmin.READONLY_MODE:
        print("ERROR: GARMIN_READONLY is set. Unset it for the refresh job.",
              file=sys.stderr)
        return 1

    if not cache.enabled():
        print("ERROR: R2 cache not configured.", file=sys.stderr)
        return 1

    # Abort before touching Garmin if either cooldown is still active.
    # This prevents the nightly run from re-hammering Garmin immediately after
    # the previous run's 429s and extending the throttle window further.
    api_remaining, api_reason = tokens.load_api_429_cooldown_remaining()
    if api_remaining > 0:
        hrs = api_remaining // 3600
        mins = (api_remaining % 3600) // 60
        print(
            f"ERROR: Garmin API 429 cooldown active — {hrs}h {mins}m remaining. "
            f"Last error: {api_reason or 'unknown'}. "
            "Skipping refresh to let Garmin's throttle window reset.",
            file=sys.stderr,
        )
        return 1

    oauth_remaining, oauth_reason = tokens.load_cooldown_remaining()
    if oauth_remaining > 0:
        hrs = oauth_remaining // 3600
        mins = (oauth_remaining % 3600) // 60
        print(
            f"ERROR: Garmin OAuth 429 cooldown active — {hrs}h {mins}m remaining. "
            f"Last error: {oauth_reason or 'unknown'}. "
            "Skipping refresh to let Garmin's throttle window reset.",
            file=sys.stderr,
        )
        return 1

    today = date.today()
    print(f"=== daily_refresh_direct {today.isoformat()} ===")
    print()

    consec_429 = 0

    # ---------- [1/5] daily summaries ----------
    print(f"[1/5] get_daily_summaries — {DAILY_LOOKBACK_DAYS} days ending {today}")
    for i in range(DAILY_LOOKBACK_DAYS + 1):
        d = (today - timedelta(days=i)).isoformat()
        force = i < FORCE_REFRESH_DAYS
        print(f"  day {d} (force_refresh={force})")
        try:
            result = garmin.get_daily_summaries(
                startdate=d, enddate=d, metrics=METRICS, force_refresh=force
            )
            # Count per-metric outcomes
            errs = 0
            for m in METRICS:
                payload = result.get(m, {}).get(d)
                if isinstance(payload, dict) and "error" in payload:
                    if is_rate_limit(payload["error"]):
                        errs += 1
            if errs >= len(METRICS) // 2:
                consec_429 += 1
                if abort_if_rate_limited(consec_429, "remaining steps"):
                    return 0
            else:
                consec_429 = 0
        except Exception as ex:  # noqa: BLE001
            if is_rate_limit(ex):
                consec_429 += 1
                if abort_if_rate_limited(consec_429, "remaining steps"):
                    return 0
            print(f"  ERROR: {ex}", file=sys.stderr)
    print()

    # ---------- [1.5/5] activity details for last 7 days ----------
    # Pre-warm get_activity_details for every activity in the last week so
    # /session-review on claude.ai web is an instant cache hit rather than
    # a readonly-blocked error.
    week_start = (today - timedelta(days=7)).isoformat()
    print(f"[1.5/5] activity_details — last 7 days ({week_start} → {today})")
    try:
        recent = garmin.get_activities_in_range(
            startdate=week_start, enddate=today.isoformat()
        ) or []
        print(f"  found {len(recent)} activities")
        detail_count = 0
        for act in recent:
            aid = act.get("activityId")
            if not aid:
                continue
            try:
                garmin.get_activity_details(str(aid), force_refresh=False)
                detail_count += 1
                consec_429 = 0
            except Exception as ex:  # noqa: BLE001
                if is_rate_limit(ex):
                    consec_429 += 1
                    if abort_if_rate_limited(consec_429, "remaining steps"):
                        return 0
        print(f"  pre-warmed details for {detail_count}/{len(recent)} activities")
    except Exception as ex:  # noqa: BLE001
        print(f"  ERROR: {str(ex)[:150]}", file=sys.stderr)
    print()

    # ---------- [2/5] activities (current month, NON-force-refresh) ----------
    # today_refresh.py runs every 2 hours and force-refreshes the current
    # month. The nightly just warms anything uncached — it's wasteful to
    # have both jobs force-refresh. Each force_refresh=True is a Garmin
    # API call; avoiding them reduces pressure on the rate limiter.
    months = [(today.year, today.month)]
    if today.day <= 3:
        prev_y = today.year - 1 if today.month == 1 else today.year
        prev_m = 12 if today.month == 1 else today.month - 1
        months.insert(0, (prev_y, prev_m))

    print(f"[2/5] activities — cache-first (no force-refresh; today_refresh handles that): "
          f"{[f'{y}-{m:02d}' for y, m in months]}")
    for y, m in months:
        try:
            # force_refresh=False — if the month is cached, use it.
            # today_refresh is the one that keeps current-month fresh.
            garmin._fetch_activities_month(y, m, force_refresh=False)
            print(f"  {y}-{m:02d}: ok")
            consec_429 = 0
        except Exception as ex:  # noqa: BLE001
            print(f"  {y}-{m:02d}: ERROR {str(ex)[:150]}", file=sys.stderr)
            if is_rate_limit(ex):
                consec_429 += 1
                if abort_if_rate_limited(consec_429, "remaining steps"):
                    return 0
    print()

    # ---------- [3/5] scheduled workouts ----------
    # Cache-first: scheduled_workouts has a 24h TTL, and the nightly run
    # cadence matches that, so cache-miss path will refresh naturally.
    # Skipping force_refresh avoids a Garmin call on any manual re-trigger
    # fired within the same 24h window.
    ahead_end = (today + timedelta(days=SCHEDULED_LOOKAHEAD_DAYS)).isoformat()
    print(f"[3/5] scheduled_workouts — {today} → {ahead_end}")
    scheduled = []
    try:
        scheduled = garmin.get_scheduled_workouts(
            startdate=today.isoformat(), enddate=ahead_end, force_refresh=False
        )
        print(f"  fetched {len(scheduled)} scheduled workouts")
        consec_429 = 0
    except Exception as ex:  # noqa: BLE001
        print(f"  ERROR: {str(ex)[:150]}", file=sys.stderr)
        if is_rate_limit(ex):
            consec_429 += 1
            if abort_if_rate_limited(consec_429, "remaining steps"):
                return 0
    print()

    # ---------- [4/5] workout structures ----------
    print("[4/5] get_workout_by_id — prewarm structures")
    workout_ids = sorted({str(w.get("workoutId"))
                          for w in scheduled
                          if w.get("workoutId")})
    if not workout_ids:
        print("  (no scheduled workouts)")
    else:
        print(f"  prewarming {len(workout_ids)} workout(s)")
        for wid in workout_ids:
            try:
                garmin.get_workout_by_id(wid, force_refresh=False)  # 30-day TTL
                consec_429 = 0
            except Exception as ex:  # noqa: BLE001
                print(f"  {wid}: ERROR {str(ex)[:120]}", file=sys.stderr)
                if is_rate_limit(ex):
                    consec_429 += 1
                    if abort_if_rate_limited(consec_429, "remaining steps"):
                        return 0
    print()

    # ---------- [5/5] derived daily metrics ----------
    # ---------- [5/6] athlete baseline (FIRST among derived — low Garmin load) ----------
    # Baseline reads cached daily_summary / activities / training_score /
    # race_predictions / lactate_threshold. Almost all of those come from
    # R2 cache, NOT from Garmin. So baseline computes reliably even when
    # Garmin is throttling us on other endpoints. Run it BEFORE the
    # force-refresh operations below so the web MCP always gets a fresh
    # baseline schema even if later steps 429.
    print("[5/6] athlete_baseline — compute + cache consolidated baseline")
    try:
        baseline = garmin.get_athlete_baseline(force_refresh=True)
        mm = baseline.get("multi_method", {}) or {}
        flags = {k: v.get("flag") for k, v in mm.items() if v.get("flag")}
        if flags:
            print(f"  computed — {len(flags)} disagreement flags:")
            for metric, flag_text in flags.items():
                print(f"    {metric}: {flag_text[:120]}")
        else:
            print(f"  computed — no threshold disagreements flagged")
        ksc = baseline.get("key_session_counts", {})
        if ksc:
            print(f"  key sessions: run {ksc.get('run_key')}/{ksc.get('run_total')}, "
                  f"bike {ksc.get('bike_key')}/{ksc.get('bike_total')}, "
                  f"swim {ksc.get('swim_key')}/{ksc.get('swim_total')}")
    except Exception as ex:  # noqa: BLE001
        print(f"  ERROR: {str(ex)[:200]}", file=sys.stderr)
    print()

    # ---------- [6/6] derived metrics (cache-first) ----------
    # These all have 24h TTLs. Cache-first means cache-miss triggers a
    # real Garmin call naturally; a still-fresh cache entry (from a
    # manual re-trigger earlier in the day) is reused without burning
    # quota. The "latest X" endpoints are the most rate-sensitive in
    # the whole refresh — race_predictions / training_score /
    # lactate_threshold have been the historical 429 offenders.
    print("[6/6] derived metrics (race predictions, LT, training scores, etc.)")
    today_iso = today.isoformat()

    def try_one(label, fn):
        nonlocal consec_429
        print(f"  {label}")
        try:
            fn()
            consec_429 = 0
            return True
        except Exception as ex:  # noqa: BLE001
            print(f"    ERROR: {str(ex)[:150]}", file=sys.stderr)
            if is_rate_limit(ex):
                consec_429 += 1
                return not abort_if_rate_limited(consec_429, "remaining derived metrics")
            return True

    ops = [
        ("race_predictions",   lambda: garmin.get_race_predictions(force_refresh=False)),
        ("lactate_threshold",  lambda: garmin.get_lactate_threshold(force_refresh=False)),
        ("hill_score",         lambda: garmin.get_training_score("hill", startdate=today_iso, force_refresh=False)),
        ("endurance_score",    lambda: garmin.get_training_score("endurance", startdate=today_iso, force_refresh=False)),
        ("personal_records",   lambda: garmin.get_personal_records(force_refresh=False)),
        ("body_composition",   lambda: garmin.get_body_composition(
            startdate=(today - timedelta(days=30)).isoformat(),
            enddate=today_iso, force_refresh=False,
        )),
    ]
    for label, fn in ops:
        if not try_one(label, fn):
            break
    print()

    # ---------- summary ----------
    print("Cache totals:")
    for tool in ("daily_summary", "activities_month", "calendar_month",
                 "workout_by_id", "race_predictions", "lactate_threshold",
                 "training_score", "personal_records", "body_composition",
                 "athlete_baseline"):
        count = cache.count_keys(tool_prefix=tool)
        print(f"  {tool}: {count}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
