"""Nightly refresh — Python version (bypasses MCP readonly mode).

Runs in the GitHub Action's container. Imports app.garmin directly and
calls its cached functions with force_refresh=True for today's data and
the current month's activities. Writes everything to R2 so the readonly
web service can serve it.

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
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import cache, garmin  # noqa: E402

DAILY_LOOKBACK_DAYS = int(os.environ.get("DAILY_LOOKBACK_DAYS", "3"))
FORCE_REFRESH_DAYS = int(os.environ.get("FORCE_REFRESH_DAYS", "1"))
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

    # ---------- [2/5] activities (current month, force-refresh) ----------
    months = [(today.year, today.month)]
    if today.day <= 3:
        prev_y = today.year - 1 if today.month == 1 else today.year
        prev_m = 12 if today.month == 1 else today.month - 1
        months.insert(0, (prev_y, prev_m))

    print(f"[2/5] activities — force_refresh month(s): "
          f"{[f'{y}-{m:02d}' for y, m in months]}")
    for y, m in months:
        try:
            garmin._fetch_activities_month(y, m, force_refresh=True)
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
    ahead_end = (today + timedelta(days=SCHEDULED_LOOKAHEAD_DAYS)).isoformat()
    print(f"[3/5] scheduled_workouts — {today} → {ahead_end}")
    scheduled = []
    try:
        scheduled = garmin.get_scheduled_workouts(
            startdate=today.isoformat(), enddate=ahead_end, force_refresh=True
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
    print("[5/5] derived metrics (race predictions, LT, training scores, etc.)")
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
        ("race_predictions",   lambda: garmin.get_race_predictions(force_refresh=True)),
        ("lactate_threshold",  lambda: garmin.get_lactate_threshold(force_refresh=True)),
        ("hill_score",         lambda: garmin.get_training_score("hill", startdate=today_iso, force_refresh=True)),
        ("endurance_score",    lambda: garmin.get_training_score("endurance", startdate=today_iso, force_refresh=True)),
        ("personal_records",   lambda: garmin.get_personal_records(force_refresh=True)),
        ("body_composition",   lambda: garmin.get_body_composition(
            startdate=(today - timedelta(days=30)).isoformat(),
            enddate=today_iso, force_refresh=True,
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
                 "training_score", "personal_records", "body_composition"):
        count = cache.count_keys(tool_prefix=tool)
        print(f"  {tool}: {count}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
