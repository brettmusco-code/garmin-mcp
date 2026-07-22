"""MCP JSON-RPC 2.0 server over HTTP for Garmin Connect.

Includes a minimal OAuth 2.0 stub so claude.ai's Custom Connector can connect.
Since this is a personal, single-tenant server, the OAuth flow is a rubber
stamp: any client can register, authorize returns instantly, and the token
endpoint always hands back the configured MCP_BEARER_TOKEN.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

import pathlib

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from . import cache, garmin, tokens

_keeper_logger = logging.getLogger("garmin.token_keeper")


def _token_keeper_loop() -> None:
    """Background daemon: keep the Garmin OAuth2 token fresh in R2.

    Runs every 5 minutes. When the token has less than KEEPER_MARGIN_SEC
    (default 30 min) remaining, it proactively exchanges for a new one and
    writes the result to R2. Cron jobs then always load a valid token and
    skip the exchange entirely, eliminating the most common source of 429s.
    """
    import time
    time.sleep(60)  # let startup settle before first check
    while True:
        try:
            status = tokens.proactive_refresh_if_needed()
            if not status.startswith("ok"):
                _keeper_logger.info("token keeper: %s", status)
        except Exception as ex:  # noqa: BLE001
            _keeper_logger.warning("token keeper error: %s", ex)
        time.sleep(300)  # check every 5 minutes


@asynccontextmanager
async def lifespan(app_: FastAPI):
    t = threading.Thread(target=_token_keeper_loop, daemon=True, name="garmin-token-keeper")
    t.start()
    _keeper_logger.info("background token keeper started (check interval: 5 min)")
    yield


app = FastAPI(lifespan=lifespan)

BEARER = os.environ.get("MCP_BEARER_TOKEN")
if not BEARER:
    raise RuntimeError("MCP_BEARER_TOKEN is required")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DAILY_METRIC_KEYS = sorted(garmin.DAILY_METHODS.keys())

TOOLS = [
    {
        "name": "get_activities",
        "description": (
            "List Garmin activities (runs, rides, walks, etc.). Two modes:\n"
            "- Recent mode: pass `start` (offset) and/or `limit` (default 10, max 50). "
            "Returns newest-first.\n"
            "- Date range mode: pass `startdate` + `enddate` (max 366 days, inclusive). "
            "Optionally filter by `activity_type` (e.g., 'running', 'cycling').\n"
            "Either mode returns start time, type, distance, duration, calories. "
            "Range-mode results are cached in S3; pass `force_refresh=true` to bypass."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {"type": "number", "description": "offset for recent mode, default 0"},
                "limit": {"type": "number", "description": "for recent mode: default 10, max 50"},
                "startdate": {"type": "string", "description": "YYYY-MM-DD (range mode)"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD (range mode)"},
                "activity_type": {"type": "string", "description": "range mode only: optional filter"},
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
        },
    },
    {
        "name": "get_activity_details",
        "description": (
            "Get full details for a single activity: summary, splits, HR zones, "
            "weather, gear. Use the activityId from get_activities. Cached in S3; "
            "pass `force_refresh=true` to bypass."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "activity_id": {"type": "string"},
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
            "required": ["activity_id"],
        },
    },
    {
        "name": "get_daily_summaries",
        "description": (
            "Bulk-fetch one or more per-day metrics across a date range (max 366 days). "
            "Large ranges fan out slowly (2 concurrent requests) to avoid Garmin rate limits — "
            "a full year across 5 metrics takes ~15-25 min on a cold cache. "
            "Per (metric, date) is cached in S3, so re-calls for overlapping ranges are near-instant. "
            "Pass `force_refresh=true` to re-fetch from Garmin. "
            "Returns { metric: { date: data } }. Supported metrics: "
            + ", ".join(DAILY_METRIC_KEYS)
            + "."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "startdate": {"type": "string", "description": "YYYY-MM-DD"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD"},
                "metrics": {
                    "type": "array",
                    "items": {"type": "string", "enum": DAILY_METRIC_KEYS},
                    "description": "list of metrics to fetch",
                },
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
            "required": ["startdate", "enddate", "metrics"],
        },
    },
    {
        "name": "get_body_composition",
        "description": "Weight, body fat, BMI, muscle mass. Single date or range (max 366 days). Cached 24h.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "startdate": {"type": "string", "description": "YYYY-MM-DD"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD (optional)"},
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
            "required": ["startdate"],
        },
    },
    {
        "name": "get_training_score",
        "description": (
            "Hill or endurance training score. Single date or range (max 366 days). "
            "metric: 'hill' or 'endurance'. Cached 24h."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": ["hill", "endurance"]},
                "startdate": {"type": "string", "description": "YYYY-MM-DD"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD (optional)"},
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
            "required": ["metric", "startdate"],
        },
    },
    {
        "name": "get_lactate_threshold",
        "description": (
            "Lactate threshold heart rate / pace. Call with no args for latest; or "
            "provide startdate+enddate (max 366 days) for history. aggregation = 'daily' (default) or 'weekly'. Cached 24h."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "startdate": {"type": "string", "description": "YYYY-MM-DD (optional)"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD (optional)"},
                "aggregation": {"type": "string", "enum": ["daily", "weekly"]},
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
        },
    },
    {
        "name": "get_progress_summary",
        "description": (
            "Aggregated training progress between two dates (max 366 days). "
            "metric: one of distance, duration, elevationGain, calories (default: distance). Cached 24h."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "startdate": {"type": "string", "description": "YYYY-MM-DD"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD"},
                "metric": {"type": "string", "description": "distance/duration/elevationGain/calories"},
                "group_by_activities": {"type": "boolean"},
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
            "required": ["startdate", "enddate"],
        },
    },
    {
        "name": "get_weekly_summaries",
        "description": (
            "Weekly aggregates (steps, stress, intensity_minutes) ending on a given date. "
            "Up to 104 weeks back. Returns { metric: [...weeks] }. Cached 24h per metric."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "enddate": {"type": "string", "description": "YYYY-MM-DD"},
                "weeks": {"type": "number", "description": "1–104, default 52"},
                "metrics": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["steps", "stress", "intensity_minutes"]},
                },
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
            "required": ["enddate"],
        },
    },
    {
        "name": "get_devices",
        "description": "List registered Garmin devices.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_workouts",
        "description": "List saved/custom workouts in the user's library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {"type": "number", "description": "offset, default 0"},
                "limit": {"type": "number", "description": "1-100, default 100"},
            },
        },
    },
    {
        "name": "get_workout_by_id",
        "description": "Full step-by-step definition of one saved workout. Cached per workout_id (30-day TTL) — workouts are immutable once created.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workout_id": {"type": "string"},
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
            "required": ["workout_id"],
        },
    },
    {
        "name": "get_training_plans",
        "description": "Active and available training plans (Garmin Coach + custom).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_training_plan_by_id",
        "description": (
            "Details of one training plan. Set adaptive=true for Garmin Coach "
            "adaptive plans (uses a different Garmin endpoint)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "adaptive": {"type": "boolean", "description": "default false"},
            },
            "required": ["plan_id"],
        },
    },
    {
        "name": "get_scheduled_workouts",
        "description": (
            "Scheduled/planned workouts (not completed activities) from the "
            "Garmin Connect calendar between two dates (inclusive). Walks the "
            "calendar month-by-month and caches per (year, month). Useful for "
            "'what's on my plan today/this week' queries."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "startdate": {"type": "string", "description": "YYYY-MM-DD"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD"},
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
            "required": ["startdate", "enddate"],
        },
    },
    {
        "name": "analyze_training_period",
        "description": (
            "Summarize activities in a date range (max 366 days). Returns totals, "
            "per-activity-type breakdown, and weekly timeline — pre-computed so "
            "you don't have to crunch raw activity JSON."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "startdate": {"type": "string", "description": "YYYY-MM-DD"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["startdate", "enddate"],
        },
    },
    {
        "name": "compare_activities",
        "description": (
            "Side-by-side comparison of 2-10 activities. Returns normalized rows "
            "and deltas-vs-baseline (first id) for distance, pace, HR, calories, "
            "training effect."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "activity_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-10 activity IDs (as strings)",
                },
            },
            "required": ["activity_ids"],
        },
    },
    {
        "name": "analyze_sleep_trend",
        "description": (
            "Sleep summary over the last N days (1-180) ending on enddate. "
            "Returns averages (duration/score/stages), simple first-half vs "
            "second-half trend, and per-day series."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "enddate": {"type": "string", "description": "YYYY-MM-DD"},
                "days": {"type": "number", "description": "1-180, default 30"},
            },
            "required": ["enddate"],
        },
    },
    {
        "name": "get_personal_records",
        "description": "Personal records across all activity types (fastest mile, longest ride, etc.). Cached 24h.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
        },
    },
    {
        "name": "get_race_predictions",
        "description": (
            "Predicted race times (5K/10K/half/full). Optional date range for history; "
            "otherwise returns latest. Cached 24h."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "startdate": {"type": "string", "description": "YYYY-MM-DD (optional)"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD (optional)"},
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
        },
    },
    {
        "name": "get_cycling_ftp",
        "description": (
            "User's cycling FTP as set in Garmin Connect (from the "
            "latestFunctionalThresholdPower/CYCLING endpoint). The "
            "authoritative bike FTP — matches what the user sees in their "
            "app and drives their zones. Separate from run FTP which lives "
            "on lactate_threshold. Cached 24h."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
        },
    },
    {
        "name": "get_athlete_baseline",
        "description": (
            "Current multi-sport physiology snapshot derived from freshest "
            "Garmin data + activity-derived multi-method cross-validation. "
            "Returns: VO2max (run/bike), LT HR, run FTP, bike FTP (measured "
            "or inferred), swim CSS, endurance/hill scores, weight, race "
            "predictions, VDOT, sport-specific 60-day fitness trends, and "
            "`multi_method`: a dict with run_vo2max, run_lt_hr, run_ftp, "
            "bike_ftp, bike_vo2max, swim_css — each with multiple independent "
            "estimates (Garmin + Daniels VDOT + Coggan 20min + Karvonen + "
            "Ginn CSS etc.), a consensus median, spread, and a flag when "
            "Garmin disagrees significantly with observed efforts. Skills "
            "should call this at the start of their data-gather step."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
        },
    },
    {
        "name": "save_weekly_snapshot",
        "description": (
            "Persist a weekly summary snapshot to R2. Called at the end of "
            "the /weekly skill to auto-save the week's key metrics (FTP, "
            "VDOT, CSS, weekly miles, CTL/ATL/TSB, race predictions, HRV "
            "avg, nutrition totals). Next week's /weekly retrieves this "
            "via get_weekly_snapshots for automatic WHAT CHANGED deltas — "
            "no manual copy-paste to project instructions needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot": {
                    "type": "object",
                    "description": "The JSON snapshot object. Must include a 'date' field (YYYY-MM-DD) used as the R2 key.",
                },
            },
            "required": ["snapshot"],
        },
    },
    {
        "name": "get_weekly_snapshots",
        "description": (
            "Retrieve recent weekly snapshots saved by save_weekly_snapshot. "
            "Returns a list of snapshots newest-first. weeks_back=1 for the "
            "previous week's snapshot (typical WHAT CHANGED diff); larger "
            "values give multi-week trajectory history."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "weeks_back": {
                    "type": "number",
                    "description": "How many past snapshots to return. Default 1.",
                },
            },
        },
    },
    {
        "name": "nutrition_plan_vs_actual",
        "description": (
            "Compare /weekly's nutrition plan (stored in the most recent "
            "weekly snapshot's nutrition_plan dict) against actual food "
            "logged in Garmin Connect. Returns per-day rows with "
            "target/actual/delta for kcal + P/C/F, foods_logged count, "
            "Garmin's own adjusted calorie goal, daily expenditure (BMR + "
            "active), and net balance. Used by /morning for a one-line "
            "yesterday-recap, and by /nutrition for the week-to-date view."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days_back": {
                    "type": "number",
                    "description": "Days of history to return, 1-14. Default 7.",
                },
            },
        },
    },
    {
        "name": "nutrition_trend",
        "description": (
            "Multi-week trend of nutrition adherence + weight. Returns per-"
            "week rows (avg daily intake, expenditure, delta, days logged, "
            "protein-hit days, avg weight) plus overall weight trajectory "
            "(start vs end, delta) and summary (logging consistency %, "
            "intake rising/falling/stable, weight trend). Uses cached "
            "weekly snapshots where present, synthesizes from raw daily "
            "data otherwise."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "weeks": {
                    "type": "number",
                    "description": "Weeks of history to return, 1-26. Default 4.",
                },
            },
        },
    },
    {
        "name": "set_fueling_goal",
        "description": (
            "Set/replace the athlete's fueling goal (target weight + timeline) "
            "used by generate_fueling_plan and the /fuel skill. goal_type is "
            "'lose', 'gain', or 'maintain'. For lose/gain, pass target_weight_kg "
            "and target_date (YYYY-MM-DD). Optionally pass sex ('male'/'female'), "
            "height_cm and age for an accurate Mifflin-St Jeor BMR, and "
            "protein_g_per_kg to override the default. Persisted to R2 as the "
            "single active goal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal_type": {"type": "string", "enum": ["lose", "gain", "maintain"]},
                "target_weight_kg": {"type": "number"},
                "target_date": {"type": "string", "description": "YYYY-MM-DD"},
                "start_weight_kg": {"type": "number", "description": "optional; defaults to current weight"},
                "sex": {"type": "string", "enum": ["male", "female"]},
                "height_cm": {"type": "number"},
                "age": {"type": "number"},
                "protein_g_per_kg": {"type": "number"},
                "max_deficit_kcal": {"type": "number", "description": "daily deficit cap; default 500, pass 0 to remove the cap"},
                "ea_floor": {"type": "number", "description": "energy-availability warning threshold (kcal/kg FFM); default 30, 0 disables"},
                "ea_min": {"type": "number", "description": "ENFORCED EA minimum: daily target floored at ea_min x FFM + day's burn. Unset = off"},
                "min_kcal": {"type": "number", "description": "absolute daily calorie floor. Unset = none"},
                "bmr_floor_mult": {"type": "number", "description": "daily-target floor as BMR multiple; default 1.2, 0 drops the floor"},
                "periodize_deficit": {"type": "boolean", "description": "bank the deficit on rest/easy days (default true for lose goals)"},
                "front_load": {"type": "number", "description": "0..0.9: steeper deficit early, tapering as weight nears target. 0 = flat linear pace"},
                "max_loss_lb_per_week": {"type": "number", "description": "cap the loss rate in lb/week (friendlier than max_deficit_kcal; 1 lb ~ 3500 kcal)"},
                "use_adaptive_tdee": {"type": "boolean", "description": "use measured maintenance (intake vs weight change) instead of BMR x1.3 once data is sufficient"},
                "home_lat": {"type": "number", "description": "home latitude, for heat-aware hydration on outdoor sessions"},
                "home_lon": {"type": "number", "description": "home longitude"},
                "skip_breakfast_weekdays": {"type": "boolean", "description": "time-restricted eating: drop the breakfast meal on weekdays (Mon-Fri) and shift its calories to the later eating window; weekends keep breakfast"},
                "notes": {"type": "string"},
            },
            "required": ["goal_type"],
        },
    },
    {
        "name": "get_fueling_goal",
        "description": (
            "Return the active fueling goal plus live progress: current weight "
            "vs target, weeks remaining, required daily kcal change, goal age, "
            "and review flags (stale goal, target passed, weight not logged). "
            "Returns goal=null if none set."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "generate_fueling_plan",
        "description": (
            "Build a forward fueling plan for the next N days from the stored "
            "goal + Garmin body composition + scheduled workouts: per-day "
            "calorie target, macros (protein by bodyweight, carbs periodized by "
            "session type, fat as balancer) and a per-workout fuel card "
            "(pre/during/post carbs + protein, hydration, sodium, caffeine) for "
            "sessions >=75 min or >=Z3. Session burn is calibrated from the "
            "athlete's own 90-day history. Set save=true to merge the plan into "
            "the weekly snapshot so nutrition_plan_vs_actual can track it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD; default today"},
                "days": {"type": "number", "description": "Horizon 1-28. Default 7."},
                "save": {"type": "boolean", "description": "merge into weekly snapshot, default false"},
                "carb_load": {"type": "boolean", "description": "race-week mode: no deficit, carbs ~9 g/kg. Default false"},
                "max_deficit_kcal": {"type": "number", "description": "override the daily deficit cap (default 500 or goal value); 0 removes it"},
                "ea_floor": {"type": "number", "description": "override the energy-availability warning threshold (default 30 or goal value); 0 disables"},
                "fuel_min_minutes": {"type": "number", "description": "min session length to get a fuel card. Default 90."},
                "bmr_floor_mult": {"type": "number", "description": "override the daily-target floor as BMR multiple (default 1.2 or goal value); 0 drops it"},
                "periodize_deficit": {"type": "boolean", "description": "override deficit periodization (default true for lose goals; false = flat)"},
                "ea_min": {"type": "number", "description": "override the enforced EA minimum (kcal/kg FFM); 0 disables"},
                "min_kcal": {"type": "number", "description": "override the absolute daily calorie floor; 0 disables"},
                "rebalance": {"type": ["boolean", "number"], "description": "self-correct from recent logged days: true = week-to-date, N = last N days. Spreads the accumulated intake error (vs expenditure-adjusted targets) across this window. Default false"},
                "front_load": {"type": "number", "description": "override front-loading 0..0.9 (default from goal): steeper deficit early, tapering as weight nears target"},
                "max_loss_lb_per_week": {"type": "number", "description": "override the loss-rate cap in lb/week"},
                "use_adaptive_tdee": {"type": "boolean", "description": "override use of measured maintenance instead of the BMR formula"},
                "heat_c": {"type": "number", "description": "override forecast: assume this day's high (deg C) for heat-aware hydration on outdoor sessions"},
                "skip_breakfast_weekdays": {"type": "boolean", "description": "override the goal's weekday breakfast-skip: drop the weekday breakfast meal and shift its calories later"},
            },
        },
    },
    {
        "name": "get_race_fueling",
        "description": (
            "Race-day fueling calculator for a target event: pre-race meal, "
            "carb-loading protocol (for long events), and hour-by-hour carbs, "
            "fluid, sodium, and caffeine. Pass sport, expected duration_hours, "
            "optional intensity, weight_kg (defaults to baseline), and hot=true "
            "for heat."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "cycling | running | triathlon | swimming | ..."},
                "duration_hours": {"type": "number"},
                "intensity": {"type": "string"},
                "weight_kg": {"type": "number"},
                "hot": {"type": "boolean", "description": "hot conditions, default false"},
            },
            "required": ["sport", "duration_hours"],
        },
    },
    {
        "name": "get_adaptive_tdee",
        "description": (
            "Estimate the athlete's TRUE maintenance from logged intake vs "
            "actual weight change over recent weeks (more accurate than BMR x "
            "1.3 once there's data). Returns total maintenance, the "
            "non-exercise base, mean daily exercise burn, the formula base for "
            "comparison, and a confidence rating. generate_fueling_plan uses "
            "this as its energy base when use_adaptive_tdee is on and "
            "confidence is sufficient."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "weeks": {"type": "number", "description": "window in weeks, 2-12. Default 6."},
            },
        },
    },
    {
        "name": "push_nutrition_targets_to_garmin",
        "description": (
            "EXPERIMENTAL: write the fueling plan's daily calorie/macro targets "
            "into Garmin Connect's nutrition goals (so the Connect app shows the "
            "plan's target, and Garmin's own activity adjustment then auto-raises "
            "it with actual burn). Reads targets from the weekly snapshot's "
            "nutrition_plan — save one first via generate_fueling_plan(save=true). "
            "Only works in live (non-readonly) mode, i.e. from the cron env. "
            "Returns per-endpoint diagnostics."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_date": {"type": "string", "description": "YYYY-MM-DD; default today"},
                "days": {"type": "number", "description": "how many days ahead to push, 1-7. Default 1."},
            },
        },
    },
]


def _require(args: dict, key: str) -> str:
    v = args.get(key)
    if not isinstance(v, str) or not DATE_RE.match(v):
        raise ValueError(f"`{key}` must be YYYY-MM-DD")
    return v


def _call_tool(name: str, args: dict) -> Any:
    if name == "get_activities":
        sd, ed = args.get("startdate"), args.get("enddate")
        if sd or ed:
            return garmin.get_activities_in_range(
                _require(args, "startdate"),
                _require(args, "enddate"),
                args.get("activity_type"),
                force_refresh=bool(args.get("force_refresh", False)),
            )
        start = int(args.get("start", 0))
        limit = min(int(args.get("limit", 10)), 50)
        return garmin.get_activities(start, limit)
    if name == "get_activity_details":
        aid = args.get("activity_id")
        if not aid:
            raise ValueError("`activity_id` is required")
        return garmin.get_activity_details(aid, force_refresh=bool(args.get("force_refresh", False)))
    if name == "get_daily_summaries":
        metrics = args.get("metrics")
        if not isinstance(metrics, list) or not metrics:
            raise ValueError("`metrics` must be a non-empty array")
        return garmin.get_daily_summaries(
            _require(args, "startdate"),
            _require(args, "enddate"),
            metrics,
            force_refresh=bool(args.get("force_refresh", False)),
        )
    if name == "get_body_composition":
        s = _require(args, "startdate")
        e = args.get("enddate")
        if e and not DATE_RE.match(e):
            raise ValueError("`enddate` must be YYYY-MM-DD")
        return garmin.get_body_composition(s, e, force_refresh=bool(args.get("force_refresh", False)))
    if name == "get_training_score":
        metric = args.get("metric")
        if metric not in ("hill", "endurance"):
            raise ValueError("`metric` must be 'hill' or 'endurance'")
        s = _require(args, "startdate")
        e = args.get("enddate")
        if e and not DATE_RE.match(e):
            raise ValueError("`enddate` must be YYYY-MM-DD")
        return garmin.get_training_score(metric, s, e, force_refresh=bool(args.get("force_refresh", False)))
    if name == "get_lactate_threshold":
        s = args.get("startdate")
        e = args.get("enddate")
        if (s and not DATE_RE.match(s)) or (e and not DATE_RE.match(e)):
            raise ValueError("dates must be YYYY-MM-DD")
        agg = args.get("aggregation", "daily")
        if agg not in ("daily", "weekly"):
            raise ValueError("`aggregation` must be 'daily' or 'weekly'")
        return garmin.get_lactate_threshold(s, e, agg, force_refresh=bool(args.get("force_refresh", False)))
    if name == "get_progress_summary":
        return garmin.get_progress_summary(
            _require(args, "startdate"),
            _require(args, "enddate"),
            args.get("metric", "distance"),
            bool(args.get("group_by_activities", True)),
            force_refresh=bool(args.get("force_refresh", False)),
        )
    if name == "get_weekly_summaries":
        weeks = int(args.get("weeks", 52))
        metrics = args.get("metrics")
        if metrics is not None and not isinstance(metrics, list):
            raise ValueError("`metrics` must be an array")
        return garmin.get_weekly_summaries(
            _require(args, "enddate"), weeks, metrics,
            force_refresh=bool(args.get("force_refresh", False)),
        )
    if name == "get_devices":
        return garmin.get_devices()
    if name == "get_workouts":
        return garmin.get_workouts(int(args.get("start", 0)), int(args.get("limit", 100)))
    if name == "get_workout_by_id":
        wid = args.get("workout_id")
        if not wid:
            raise ValueError("`workout_id` is required")
        return garmin.get_workout_by_id(wid, force_refresh=bool(args.get("force_refresh", False)))
    if name == "get_training_plans":
        return garmin.get_training_plans()
    if name == "get_training_plan_by_id":
        pid = args.get("plan_id")
        if not pid:
            raise ValueError("`plan_id` is required")
        return garmin.get_training_plan_by_id(pid, bool(args.get("adaptive", False)))
    if name == "get_scheduled_workouts":
        return garmin.get_scheduled_workouts(
            _require(args, "startdate"),
            _require(args, "enddate"),
            force_refresh=bool(args.get("force_refresh", False)),
        )
    if name == "analyze_training_period":
        return garmin.analyze_training_period(
            _require(args, "startdate"), _require(args, "enddate")
        )
    if name == "compare_activities":
        ids = args.get("activity_ids")
        if not isinstance(ids, list) or not (2 <= len(ids) <= 10):
            raise ValueError("`activity_ids` must be an array of 2-10 items")
        return garmin.compare_activities(ids)
    if name == "analyze_sleep_trend":
        days = int(args.get("days", 30))
        return garmin.analyze_sleep_trend(_require(args, "enddate"), days)
    if name == "get_personal_records":
        return garmin.get_personal_records(force_refresh=bool(args.get("force_refresh", False)))
    if name == "get_race_predictions":
        s = args.get("startdate")
        e = args.get("enddate")
        if (s and not DATE_RE.match(s)) or (e and not DATE_RE.match(e)):
            raise ValueError("dates must be YYYY-MM-DD")
        return garmin.get_race_predictions(s, e, force_refresh=bool(args.get("force_refresh", False)))
    if name == "get_athlete_baseline":
        return garmin.get_athlete_baseline(force_refresh=bool(args.get("force_refresh", False)))
    if name == "get_cycling_ftp":
        return garmin.get_cycling_ftp(force_refresh=bool(args.get("force_refresh", False)))
    if name == "save_weekly_snapshot":
        snap = args.get("snapshot")
        if not isinstance(snap, dict):
            raise ValueError("`snapshot` must be an object")
        return garmin.save_weekly_snapshot(snap)
    if name == "get_weekly_snapshots":
        return garmin.get_weekly_snapshots(
            weeks_back=int(args.get("weeks_back", 1))
        )
    if name == "nutrition_plan_vs_actual":
        return garmin.nutrition_plan_vs_actual(
            days_back=int(args.get("days_back", 7))
        )
    if name == "nutrition_trend":
        return garmin.nutrition_trend(
            weeks=int(args.get("weeks", 4))
        )
    if name == "set_fueling_goal":
        gt = args.get("goal_type")
        if gt not in ("lose", "gain", "maintain"):
            raise ValueError("`goal_type` must be 'lose', 'gain', or 'maintain'")
        td = args.get("target_date")
        if td and not DATE_RE.match(td):
            raise ValueError("`target_date` must be YYYY-MM-DD")
        return garmin.set_fueling_goal(
            goal_type=gt,
            target_weight_kg=args.get("target_weight_kg"),
            target_date=td,
            start_weight_kg=args.get("start_weight_kg"),
            sex=args.get("sex"),
            height_cm=args.get("height_cm"),
            age=args.get("age"),
            protein_g_per_kg=args.get("protein_g_per_kg"),
            max_deficit_kcal=args.get("max_deficit_kcal"),
            ea_floor=args.get("ea_floor"),
            ea_min=args.get("ea_min"),
            min_kcal=args.get("min_kcal"),
            bmr_floor_mult=args.get("bmr_floor_mult"),
            periodize_deficit=args.get("periodize_deficit"),
            front_load=args.get("front_load"),
            max_loss_lb_per_week=args.get("max_loss_lb_per_week"),
            use_adaptive_tdee=args.get("use_adaptive_tdee"),
            home_lat=args.get("home_lat"),
            home_lon=args.get("home_lon"),
            skip_breakfast_weekdays=args.get("skip_breakfast_weekdays"),
            notes=args.get("notes"),
        )
    if name == "get_fueling_goal":
        return garmin.get_fueling_goal()
    if name == "generate_fueling_plan":
        sd = args.get("start_date")
        if sd and not DATE_RE.match(sd):
            raise ValueError("`start_date` must be YYYY-MM-DD")
        return garmin.generate_fueling_plan(
            start_date=sd,
            days=int(args.get("days", 7)),
            save=bool(args.get("save", False)),
            carb_load=bool(args.get("carb_load", False)),
            max_deficit_kcal=args.get("max_deficit_kcal"),
            ea_floor=args.get("ea_floor"),
            fuel_min_minutes=int(args.get("fuel_min_minutes", 90)),
            bmr_floor_mult=args.get("bmr_floor_mult"),
            periodize_deficit=args.get("periodize_deficit"),
            ea_min=args.get("ea_min"),
            min_kcal=args.get("min_kcal"),
            rebalance=args.get("rebalance", False),
            front_load=args.get("front_load"),
            use_adaptive_tdee=args.get("use_adaptive_tdee"),
            max_loss_lb_per_week=args.get("max_loss_lb_per_week"),
            heat_c=args.get("heat_c"),
            skip_breakfast_weekdays=args.get("skip_breakfast_weekdays"),
        )
    if name == "get_race_fueling":
        sport = args.get("sport")
        dur = args.get("duration_hours")
        if not sport or dur is None:
            raise ValueError("`sport` and `duration_hours` are required")
        return garmin.get_race_fueling(
            sport=sport, duration_hours=float(dur),
            intensity=args.get("intensity", "race"),
            weight_kg=args.get("weight_kg"), hot=bool(args.get("hot", False)),
        )
    if name == "get_adaptive_tdee":
        return garmin.get_adaptive_tdee(weeks=int(args.get("weeks", 6)))
    if name == "push_nutrition_targets_to_garmin":
        td = args.get("target_date")
        if td and not DATE_RE.match(td):
            raise ValueError("`target_date` must be YYYY-MM-DD")
        return garmin.push_nutrition_targets_to_garmin(
            target_date=td,
            days=int(args.get("days", 1)),
        )
    raise ValueError(f"Unknown tool: {name}")


RESOURCES = [
    {
        "uri": "garmin://athlete/profile",
        "name": "Athlete Profile",
        "description": "Authenticated Garmin user's profile, settings, unit system, full name.",
        "mimeType": "application/json",
    },
    {
        "uri": "garmin://today/summary",
        "name": "Today's Summary",
        "description": "Today's stats, steps, sleep, HR, body battery — live context for the current day.",
        "mimeType": "application/json",
    },
    {
        "uri": "garmin://training/readiness",
        "name": "Training Readiness",
        "description": "Today's training readiness, status, HRV, and morning readiness score.",
        "mimeType": "application/json",
    },
]

RESOURCE_READERS = {
    "garmin://athlete/profile": lambda: garmin.resource_athlete_profile(),
    "garmin://today/summary": lambda: garmin.resource_today_summary(),
    "garmin://training/readiness": lambda: garmin.resource_training_readiness(),
}


def _ok(rpc_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _err(rpc_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _exception_to_rpc_error(rpc_id: Any, ex: Exception) -> dict:
    if isinstance(ex, garmin.GarminAuthError):
        return _err(rpc_id, -32001, f"Garmin auth failed: {ex}")
    if isinstance(ex, garmin.GarminRateLimitError):
        return _err(rpc_id, -32002, f"Garmin rate limited (retry later): {ex}")
    if isinstance(ex, garmin.GarminNotFoundError):
        return _err(rpc_id, -32003, f"Garmin not found: {ex}")
    return _err(rpc_id, -32000, str(ex))


def _handle(req: dict) -> dict:
    rpc_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    try:
        if method == "initialize":
            return _ok(
                rpc_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": {"name": "garmin-mcp", "version": "0.3.0"},
                },
            )
        if method == "tools/list":
            return _ok(rpc_id, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            data = _call_tool(name, args)
            return _ok(
                rpc_id,
                {"content": [{"type": "text", "text": json.dumps(data, default=str, indent=2)}]},
            )
        if method == "resources/list":
            return _ok(rpc_id, {"resources": RESOURCES})
        if method == "resources/read":
            uri = params.get("uri")
            reader = RESOURCE_READERS.get(uri)
            if reader is None:
                raise ValueError(f"Unknown resource: {uri}")
            data = reader()
            return _ok(
                rpc_id,
                {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(data, default=str, indent=2),
                        }
                    ]
                },
            )
        if method == "ping":
            return _ok(rpc_id, {})
        return _err(rpc_id, -32601, f"Method not found: {method}")
    except Exception as e:  # noqa: BLE001
        return _exception_to_rpc_error(rpc_id, e)


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
def health() -> PlainTextResponse:
    # GET and HEAD both return 200 so uptime pingers (which default to HEAD)
    # get a clean check instead of a 405 — and keep the free-tier web service
    # from spinning down.
    mode = "readonly" if garmin.READONLY_MODE else "live"
    return PlainTextResponse(f"garmin-mcp ok ({mode})")


# --- Mobile-friendly dashboard ----------------------------------------------
# The Claude Artifact URL (claude.ai/code/artifact/...) is a universal link, so
# on a phone it hands off to the Claude app instead of rendering in the browser.
# This route serves the same self-contained dashboard as a plain web page that
# opens natively anywhere, rebuilding it from the live plan on each load.
_DASH_TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "web" / "fuel-dashboard.html"
_DASH_PLAN_RE = re.compile(r"const PLAN = \{.*?\n\};", re.DOTALL)
_DASH_TTL = 120.0  # seconds; keeps repeated loads off the cache/R2 hot path
_dash_cache: dict[str, Any] = {"html": None, "ts": 0.0}


@app.api_route("/dashboard", methods=["GET", "HEAD"])
def dashboard(request: Request) -> HTMLResponse:
    """Fueling dashboard as a plain, mobile-friendly web page. Reads the live
    plan from cache on each load (2-min micro-cache). If DASHBOARD_TOKEN is set
    in the environment, a matching ?k=<token> is required; otherwise it's open
    (obscure but unauthenticated — set the token to lock it down)."""
    import time as _time

    gate = os.environ.get("DASHBOARD_TOKEN")
    if gate:
        supplied = request.query_params.get("k") or ""
        if not secrets.compare_digest(supplied, gate):
            raise HTTPException(status_code=404, detail="not found")

    now = _time.time()
    cached = _dash_cache["html"]
    if cached and now - _dash_cache["ts"] < _DASH_TTL:
        return HTMLResponse(cached)

    try:
        plan = garmin.generate_fueling_plan(days=7, rebalance=True)
    except Exception as ex:  # noqa: BLE001
        return HTMLResponse(
            f"<h1>Fueling dashboard</h1><p>Could not build the plan: "
            f"{type(ex).__name__}: {ex}</p>",
            status_code=500,
        )
    if plan.get("no_goal_available"):
        return HTMLResponse(
            "<h1>Fueling dashboard</h1><p>No fueling goal is set yet — run "
            "<code>/fuel</code> in Claude to create one.</p>"
        )
    if plan.get("error"):
        return HTMLResponse(
            f"<h1>Fueling dashboard</h1><p>{plan['error']}</p>"
        )

    try:
        html = _DASH_PLAN_RE.sub(
            lambda _m: "const PLAN = " + json.dumps(plan) + ";",
            _DASH_TEMPLATE.read_text(),
            count=1,
        )
    except Exception as ex:  # noqa: BLE001
        return HTMLResponse(
            f"<h1>Fueling dashboard</h1><p>Template error: "
            f"{type(ex).__name__}: {ex}</p>",
            status_code=500,
        )
    _dash_cache.update(html=html, ts=now)
    return HTMLResponse(html)


@app.get("/cache/list")
def cache_list(tool: str | None = None, limit: int = 100) -> JSONResponse:
    """List cached keys under the configured prefix (or under a tool subprefix)."""
    try:
        keys = cache.list_keys(tool, limit)
        return JSONResponse({"count": len(keys), "keys": keys})
    except Exception as ex:  # noqa: BLE001
        return JSONResponse(
            {"error": f"{type(ex).__name__}: {ex}"}, status_code=500
        )


@app.get("/cache/count")
def cache_count(tool: str | None = None) -> JSONResponse:
    """Total count of cached keys (paginates beyond 1000)."""
    try:
        return JSONResponse({"count": cache.count_keys(tool)})
    except Exception as ex:  # noqa: BLE001
        return JSONResponse(
            {"error": f"{type(ex).__name__}: {ex}"}, status_code=500
        )


@app.post("/cache/delete")
def cache_delete(
    tool: str,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Delete all cached objects for a given tool prefix. Auth required."""
    if authorization != f"Bearer {BEARER}":
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        deleted = cache.delete_prefix(tool)
        return JSONResponse({"tool": tool, "deleted": deleted})
    except Exception as ex:  # noqa: BLE001
        return JSONResponse(
            {"error": f"{type(ex).__name__}: {ex}"}, status_code=500
        )


@app.get("/cache/health")
def cache_health() -> JSONResponse:
    """Diagnose cache config. Public — only exposes config values (no secrets)
    and a roundtrip probe result. Useful for debugging R2/S3 setup."""
    info: dict[str, Any] = {
        "enabled": cache.enabled(),
        "bucket": cache.BUCKET,
        "endpoint_url": cache.ENDPOINT_URL,
        "region": cache.REGION,
        "prefix": cache.PREFIX,
        "ttl_seconds": cache.DEFAULT_TTL_SECONDS,
    }
    if not cache.enabled():
        info["status"] = "disabled (S3_CACHE_BUCKET not set)"
        return JSONResponse(info)
    probe_args = {"probe": "__cache_health__"}
    try:
        cache.put("__cache_health__", probe_args, {"ok": True}, raise_on_error=True)
        info["probe_write"] = "ok"
    except Exception as ex:  # noqa: BLE001
        info["status"] = "write_failed"
        info["error"] = f"{type(ex).__name__}: {ex}"
        return JSONResponse(info, status_code=500)
    try:
        got = cache.get("__cache_health__", probe_args, raise_on_error=True)
        info["probe_read"] = "ok" if got and got.get("ok") is True else f"unexpected: {got}"
        info["status"] = "ok"
    except Exception as ex:  # noqa: BLE001
        info["status"] = "read_failed"
        info["error"] = f"{type(ex).__name__}: {ex}"
        return JSONResponse(info, status_code=500)
    return JSONResponse(info)


# ---------- OAuth 2.0 stub for claude.ai Custom Connectors ----------
# Claude expects RFC 9728 (protected resource metadata) + RFC 8414 (auth
# server metadata) + RFC 7591 (dynamic client registration) + RFC 7636 (PKCE).
# We fake all of it and always return the same bearer token.


def _base_url(request: Request) -> str:
    # Honor the proxy's forwarded scheme/host so URLs are https on Render.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    return f"{proto}://{host}"


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource_metadata(request: Request):
    base = _base_url(request)
    return JSONResponse(
        {
            "resource": f"{base}/mcp",
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
        }
    )


@app.get("/.well-known/oauth-authorization-server")
@app.get("/.well-known/oauth-authorization-server/mcp")
def auth_server_metadata(request: Request):
    base = _base_url(request)
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256", "plain"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        }
    )


@app.post("/register")
async def register(request: Request):
    # Accept any registration request; echo a fixed client_id back.
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return JSONResponse(
        {
            "client_id": "garmin-mcp-client",
            "client_id_issued_at": 0,
            "token_endpoint_auth_method": "none",
            "redirect_uris": body.get("redirect_uris", []),
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        },
        status_code=201,
    )


@app.get("/authorize")
def authorize(
    redirect_uri: str,
    state: str | None = None,
    response_type: str | None = None,
    client_id: str | None = None,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
    scope: str | None = None,
):
    # Rubber-stamp approval: mint a code and redirect straight back.
    code = secrets.token_urlsafe(24)
    params = {"code": code}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(url=f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


@app.post("/token")
async def token(
    grant_type: str = Form(...),
    code: str | None = Form(default=None),
    redirect_uri: str | None = Form(default=None),
    client_id: str | None = Form(default=None),
    code_verifier: str | None = Form(default=None),
    refresh_token: str | None = Form(default=None),
):
    if grant_type not in ("authorization_code", "refresh_token"):
        raise HTTPException(status_code=400, detail="unsupported_grant_type")
    return JSONResponse(
        {
            "access_token": BEARER,
            "token_type": "Bearer",
            "expires_in": 60 * 60 * 24 * 365,  # 1 year
            "refresh_token": BEARER,
            "scope": "mcp",
        }
    )


# ---------- MCP endpoint ----------


@app.post("/mcp")
async def mcp(request: Request, authorization: str | None = Header(default=None)):
    if authorization != f"Bearer {BEARER}":
        # Per RFC 9728, point clients at the protected resource metadata.
        base = _base_url(request)
        return JSONResponse(
            {"error": "invalid_token"},
            status_code=401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
                )
            },
        )
    body = await request.json()
    if isinstance(body, list):
        out = [_handle(b) for b in body]
    else:
        out = _handle(body)
    return JSONResponse(out)
