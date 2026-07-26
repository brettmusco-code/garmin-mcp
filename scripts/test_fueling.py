#!/usr/bin/env python3
"""Offline smoke test for the fueling engine (goal store + generate_fueling_plan).

Stubs the Garmin fetchers and swaps the S3 cache for an in-memory dict, so the
math runs with no credentials or network. Exercises set/get_fueling_goal and
generate_fueling_plan, then asserts the key invariants.

Run:  python scripts/test_fueling.py
"""
from __future__ import annotations

import sys
import types
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Stub the garminconnect client so app.garmin imports without the heavy
# Garmin/curl_cffi stack — this test never makes real Garmin calls.
if "garminconnect" not in sys.modules:
    _fake = types.ModuleType("garminconnect")
    _fake.Garmin = type("Garmin", (), {"__init__": lambda self, *a, **k: None})
    _fake_client = types.ModuleType("garminconnect.client")

    class _ClientMeta(type):
        # Return a no-op callable for any class attribute tokens.py reads
        # (e.g. _refresh_di_token, _refresh_session) so its monkeypatching
        # succeeds without the real garminconnect package.
        def __getattr__(cls, name):
            return lambda *a, **k: None

    _fake_client.Client = _ClientMeta("Client", (), {"loads": lambda self, *a, **k: None})
    _fake_exc = types.ModuleType("garminconnect.exceptions")
    _fake_exc.GarminConnectAuthenticationError = type(
        "GarminConnectAuthenticationError", (Exception,), {})
    _fake_exc.GarminConnectTooManyRequestsError = type(
        "GarminConnectTooManyRequestsError", (Exception,), {})
    _fake.client = _fake_client
    _fake.exceptions = _fake_exc
    sys.modules["garminconnect"] = _fake
    sys.modules["garminconnect.client"] = _fake_client
    sys.modules["garminconnect.exceptions"] = _fake_exc

from app import cache, garmin as g  # noqa: E402

# --- in-memory cache (matches cache.get/put/enabled signatures) --------------
_STORE: dict[str, object] = {}


def _key(tool, key_parts):
    return f"{tool}/{'/'.join(str(p) for p in (key_parts or []))}"


def _fake_get(tool, args, ttl_seconds=None, raise_on_error=False, key_parts=None):
    return _STORE.get(_key(tool, key_parts))


def _fake_put(tool, args, data, raise_on_error=False, key_parts=None):
    _STORE[_key(tool, key_parts)] = data


def _fake_list_keys(tool_prefix=None, limit=100):
    p = (tool_prefix.rstrip("/") + "/") if tool_prefix else ""
    return [k for k in _STORE if k.startswith(p)]


cache.get = _fake_get                 # type: ignore[assignment]
cache.put = _fake_put                 # type: ignore[assignment]
cache.list_keys = _fake_list_keys     # type: ignore[assignment]
cache.enabled = lambda: True          # type: ignore[assignment]

# --- stub Garmin fetchers ----------------------------------------------------
TODAY = date.today()


def _fake_baseline(*a, **k):
    return {"weight_kg": 74.0, "staleness_days": {"weight": 2}}


def _fake_body_comp(startdate=None, enddate=None, **k):
    return {"dateWeightList": [
        {"date": (TODAY - timedelta(days=1)).isoformat(),
         "weight": 74100, "bodyFat": 14.0, "muscleMass": 34000},
    ]}


def _fake_scheduled(startdate, enddate, **k):
    base = date.fromisoformat(startdate)
    return [
        {"date": (base + timedelta(days=0)).isoformat(), "itemType": "workout",
         "title": "Bike Threshold 4x8min", "sportTypeKey": "cycling", "duration": 75 * 60},
        {"date": (base + timedelta(days=1)).isoformat(), "itemType": "workout",
         "title": "Recovery spin", "sportTypeKey": "cycling", "duration": 45 * 60},
        {"date": (base + timedelta(days=4)).isoformat(), "itemType": "workout",
         "title": "Long ride Z2 endurance", "sportTypeKey": "cycling",
         "duration": int(3.5 * 3600)},
        # day 2, 3, 5, 6 -> rest
    ]


def _fake_activities(startdate, enddate, *a, **k):
    # 5 rides averaging ~700 kcal/hr
    return [
        {"activityType": {"typeKey": "cycling"}, "activityName": "ride",
         "duration": 3600, "calories": 700}
        for _ in range(5)
    ]


g.get_athlete_baseline = _fake_baseline        # type: ignore[assignment]
g.get_body_composition = _fake_body_comp       # type: ignore[assignment]
g.get_scheduled_workouts = _fake_scheduled     # type: ignore[assignment]
g.get_activities_in_range = _fake_activities   # type: ignore[assignment]


def check(label, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        raise AssertionError(label)


def main():
    target_date = (TODAY + timedelta(weeks=6)).isoformat()

    # Pin "now" to local noon so plan tests are deterministic regardless of the
    # wall-clock time the suite runs at — otherwise, running after the 20:30
    # workout cutoff would drop today's scheduled session and break unrelated
    # periodization checks. The dedicated cutoff test overrides this locally.
    from datetime import datetime as _dt_pin
    from zoneinfo import ZoneInfo as _ZI_pin
    _ET_pin = _ZI_pin("America/New_York")
    g._local_now = lambda: _dt_pin(TODAY.year, TODAY.month, TODAY.day, 12, 0, tzinfo=_ET_pin)

    print("set_fueling_goal:")
    saved = g.set_fueling_goal(
        goal_type="lose", target_weight_kg=72.0, target_date=target_date,
        sex="male", height_cm=178, age=40,
    )
    check("goal saved", saved.get("saved") is True)
    check("start weight captured from baseline", saved["goal"]["start_weight_kg"] == 74.0)

    print("set_fueling_goal reports a failed (read-only) write instead of lying:")
    _save_put = cache.put
    cache.put = lambda *a, **k: None          # simulate a write-denied R2 (no-op)
    _STORE.pop("fueling_goal/current", None)   # nothing persisted
    ro = g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0,
                            target_date=target_date, sex="male", height_cm=178, age=40)
    cache.put = _save_put
    check("failed write -> saved is False", ro.get("saved") is False)
    check("failed write -> actionable error", "not persisted" in (ro.get("error") or "").lower())
    # restore a real goal for the rest of the suite
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40)

    print("manual weight override + display units:")
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40,
                       current_weight_kg=77.0, units="imperial")
    gov = g.get_fueling_goal()
    check("override wins over Garmin weight in progress",
          gov["progress"]["current_weight_kg"] == 77.0)
    check("units stored on goal", gov["goal"]["units"] == "imperial")
    pov = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=3)
    check("plan carries units for the dashboard", pov.get("units") == "imperial")
    check("plan uses the manual weight", pov["bmr"]["weight_kg"] == 77.0)
    check("manual-weight note surfaced",
          any("manual weight" in n.lower() for n in pov["notes"]))
    check("no readiness-easing note (removed)",
          not any("readiness" in n.lower() for n in pov["notes"]))
    # restore a plain goal for the rest of the suite
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40)

    print("update_fueling_goal merges partial edits without wiping the goal:")
    # Seed a full goal with fields the dashboard form does NOT expose (BMR
    # inputs, EA settings, manual weight override) so we can prove they survive.
    g.set_fueling_goal(
        goal_type="lose", target_weight_kg=72.0, target_date=target_date,
        sex="male", height_cm=178, age=40, ea_min=25.0, current_weight_kg=77.0,
        home_lat=40.1, home_lon=-75.2, protein_g_per_kg=1.8,
    )
    upd = g.update_fueling_goal(target_weight_kg=70.0, aggressive=True)
    check("partial edit saves", upd.get("saved") is True)
    check("edited field applied (target weight)", upd["goal"]["target_weight_kg"] == 70.0)
    check("edited toggle applied (aggressive)", upd["goal"]["aggressive"] is True)
    check("unshown BMR input preserved", upd["goal"]["height_cm"] == 178.0)
    check("unshown EA setting preserved", upd["goal"]["ea_min"] == 25.0)
    check("manual weight override preserved", upd["goal"]["current_weight_kg"] == 77.0)
    check("home coords preserved", upd["goal"]["home_lat"] == 40.1)
    check("untouched target_date preserved", upd["goal"]["target_date"] == target_date)
    # Omitting a field (the KEEP sentinel) leaves it; passing None clears it.
    upd2 = g.update_fueling_goal(protein_g_per_kg=None)
    check("omitted fields still preserved after 2nd edit",
          upd2["goal"]["target_weight_kg"] == 70.0 and upd2["goal"]["aggressive"] is True)
    check("explicit None clears the field", upd2["goal"]["protein_g_per_kg"] is None)
    # goal_type is validated on the merge path too.
    try:
        g.update_fueling_goal(goal_type="bulk")
        check("invalid goal_type rejected", False)
    except ValueError:
        check("invalid goal_type rejected", True)
    # Editing with no goal present is a clean refusal, not a crash.
    _STORE.pop("fueling_goal/current", None)
    none_edit = g.update_fueling_goal(target_weight_kg=68.0)
    check("edit with no goal -> saved False", none_edit.get("saved") is False)
    check("edit with no goal -> actionable error",
          "no fueling goal" in (none_edit.get("error") or "").lower())
    # restore a plain goal for the rest of the suite
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40)

    print("_today_actuals never falls back to an older logged day:")
    yesterday_iso = (TODAY - timedelta(days=1)).isoformat()
    def _fake_gds_stale_only(startdate, enddate, metrics=None, **k):
        # Old behavior looked back up to 6 days and would have picked up
        # yesterday's logged foods under a "today" label. The fixed version
        # only ever asks about `today`, so this data must never surface.
        return {
            "nutrition_food_log": {
                yesterday_iso: {
                    "dailyNutritionContent": {"calories": 3000},
                    "loggedFoodsWithServingSizes": [{
                        "foodMetaData": {"foodName": "stale food"},
                        "nutritionContents": [{"calories": 3000}],
                    }],
                    "dailyNutritionGoals": {},
                },
            },
            "stats_and_body": {},
        }
    _save_gds = g.get_daily_summaries
    g.get_daily_summaries = _fake_gds_stale_only
    ta = g._today_actuals()
    g.get_daily_summaries = _save_gds
    check("today_actuals reports today's date, not an older logged day",
          ta["date"] == TODAY.isoformat())
    check("today_actuals.is_today is always True", ta["is_today"] is True)
    check("no fallback: yesterday's logged foods are not surfaced as today's",
          ta["foods_logged"] == 0)

    print("_today_actuals prefers bmr+active over Garmin's totalKilocalories:")
    today_iso = TODAY.isoformat()
    def _fake_gds_inflated_total(startdate, enddate, metrics=None, **k):
        # Garmin's totalKilocalories bakes in the FULL day's BMR estimate from
        # hour zero, so mid-day it overstates what's actually been burned —
        # only activeKilocalories genuinely accumulates in real time. Here
        # bmr(1700) + active(400) = 2100 is the true "so far" figure, while
        # Garmin's own total (2600) is the inflated full-day-BMR projection.
        return {
            "nutrition_food_log": {},
            "stats_and_body": {
                today_iso: {"bmrKilocalories": 1700, "activeKilocalories": 400,
                            "totalKilocalories": 2600},
            },
        }
    g.get_daily_summaries = _fake_gds_inflated_total
    ta2 = g._today_actuals()
    g.get_daily_summaries = _save_gds
    check("expenditure uses bmr+active (2100), not Garmin's inflated total (2600)",
          ta2["expenditure_kcal"] == 2100)

    print("_today_actuals: non-workout burn excludes BMR (NEAT only, above BMR):")
    def _fake_gds_bmr_active(startdate, enddate, metrics=None, **k):
        return {
            "nutrition_food_log": {},
            "stats_and_body": {
                today_iso: {"bmrKilocalories": 1700, "activeKilocalories": 500,
                            "totalKilocalories": 2200},
            },
        }
    _save_air3 = g.get_activities_in_range
    g.get_daily_summaries = _fake_gds_bmr_active
    g.get_activities_in_range = lambda sd, ed, *a, **k: [
        {"activityType": {"typeKey": "strength_training"}, "activityName": "Strength",
         "duration": 2000, "calories": 300,
         "startTimeLocal": today_iso + " 07:00:00"}]
    ta3 = g._today_actuals()
    g.get_daily_summaries = _save_gds
    g.get_activities_in_range = _save_air3
    check("no restingCaloriesFromActivity -> workout_kcal falls back to gross (300)",
          ta3["workout_kcal"] == 300)
    check("non_workout_kcal = active - workout (500-300=200), BMR excluded",
          ta3["non_workout_kcal"] == 200)
    check("expenditure_kcal is still the full bmr+active total (2200)",
          ta3["expenditure_kcal"] == 2200)
    check("bmr_kcal exposed separately (1700)", ta3["bmr_kcal"] == 1700)

    print("_today_actuals: workout burn is Active Calories, not gross total:")
    def _fake_gds_resting_from_activity(startdate, enddate, metrics=None, **k):
        # Mirrors a real Garmin activity page: Total 285 / Resting 47 / Active
        # 238 for a single Strength session. restingCaloriesFromActivity (47)
        # is the day-level aggregate of that resting-equivalent component.
        return {
            "nutrition_food_log": {},
            "stats_and_body": {
                today_iso: {"bmrKilocalories": 751, "activeKilocalories": 287,
                            "totalKilocalories": 1038, "restingCaloriesFromActivity": 47},
            },
        }
    g.get_daily_summaries = _fake_gds_resting_from_activity
    g.get_activities_in_range = lambda sd, ed, *a, **k: [
        {"activityType": {"typeKey": "strength_training"}, "activityName": "Strength",
         "duration": 2000, "calories": 285,
         "startTimeLocal": today_iso + " 07:00:00"}]
    ta4 = g._today_actuals()
    g.get_daily_summaries = _save_gds
    g.get_activities_in_range = _save_air3
    check("workout_kcal is Active Calories (285-47=238), not the gross 285",
          ta4["workout_kcal"] == 238)
    check("the single listed workout's own kcal is also active-only (238)",
          ta4["workouts"][0]["kcal"] == 238)
    check("non_workout_kcal nets cleanly (287 day-active - 238 workout-active = 49)",
          ta4["non_workout_kcal"] == 49)

    print("_recent_days carries a real measured deficit per past day:")
    r2_iso, r1_iso = (TODAY - timedelta(days=2)).isoformat(), (TODAY - timedelta(days=1)).isoformat()
    def _fake_gds_recent(startdate, enddate, metrics=None, **k):
        def _food(cal):
            return {"dailyNutritionContent": {"calories": cal},
                    "loggedFoodsWithServingSizes": [{"foodMetaData": {"foodName": "x"},
                                                      "nutritionContents": [{"calories": cal}]}],
                    "dailyNutritionGoals": {}}
        return {
            "nutrition_food_log": {r2_iso: _food(1800), r1_iso: _food(2000)},
            "stats_and_body": {
                r2_iso: {"bmrKilocalories": 1700, "activeKilocalories": 400, "totalKilocalories": 2100},
                r1_iso: {"bmrKilocalories": 1700, "activeKilocalories": 800, "totalKilocalories": 2500},
            },
        }
    g.get_daily_summaries = _fake_gds_recent
    rd = g._recent_days(n=2)
    g.get_daily_summaries = _save_gds
    check("recent_days covers both days in order", [d["date"] for d in rd] == [r2_iso, r1_iso])
    check("day 1 deficit = 2100 - 1800 = 300", rd[0]["deficit_kcal"] == 300)
    check("day 2 deficit = 2500 - 2000 = 500", rd[1]["deficit_kcal"] == 500)

    print("get_fueling_goal:")
    gi = g.get_fueling_goal()
    check("goal returned", gi["goal"]["goal_type"] == "lose")
    check("weeks remaining ~6", gi["progress"]["weeks_remaining"] == 6)
    # current weight = the weigh-in snapshot (74.1), NOT the baseline stub
    # (74.0) — the "now" card must match the chart's latest actual, so both
    # read _latest_body_stats. kg_to_target = 74.1 - 72.0.
    check("kg to target = 2.1", gi["progress"]["kg_to_target"] == 2.1)
    check("current weight comes from the weigh-in snapshot, not the baseline",
          gi["progress"]["current_weight_kg"] == 74.1)
    check("required daily change negative", gi["progress"]["required_daily_kcal_change"] < 0)

    # Regression: the dashboard "now" card (goal_progress.current_weight_kg)
    # and the chart's latest actual (body.weight_kg) MUST agree — both resolve
    # from the shared weigh-in snapshot. The original bug had goal_progress
    # read the nightly baseline's stale LT weight while body read the fresh
    # weigh-in, so the card and chart disagreed (169.8lb vs 165.8lb).
    _plan_agree = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)
    check("now-card weight == chart weight (both from the weigh-in snapshot)",
          _plan_agree["goal_progress"]["current_weight_kg"]
          == _plan_agree["body"]["weight_kg"] == 74.1)

    print("_fmt_weight respects display units (notes match the dashboard):")
    check("metric -> kg", g._fmt_weight(68.0, "metric") == "68.0kg")
    check("imperial -> lb", g._fmt_weight(68.0, "imperial") == "149.9lb")
    check("None units defaults to kg", g._fmt_weight(68.0, None) == "68.0kg")
    check("None weight -> dash", g._fmt_weight(None, "imperial") == "—")

    print("_coerce_garmin_date parses ISO strings and epoch (s/ms):")
    check("ISO string", g._coerce_garmin_date("2026-07-24") == date(2026, 7, 24))
    check("ISO datetime", g._coerce_garmin_date("2026-07-24T06:00:00.0") == date(2026, 7, 24))
    check("epoch seconds (int)", g._coerce_garmin_date(1784796695) == date(2026, 7, 23))
    check("epoch seconds (str)", g._coerce_garmin_date("1784796695") == date(2026, 7, 23))
    check("epoch millis", g._coerce_garmin_date(1690000000000) == date(2023, 7, 22))
    check("None -> None", g._coerce_garmin_date(None) is None)
    check("garbage -> None", g._coerce_garmin_date("not-a-date") is None)

    print("weigh-in snapshot parses an epoch date into a real as_of (not the raw epoch):")
    _save_bc = g.get_body_composition
    g.get_body_composition = lambda startdate=None, enddate=None, **k: {"dateWeightList": [
        {"date": 1784796695, "weight": 74900, "bodyFat": 14.0}]}  # epoch seconds, not ISO
    g.refresh_weigh_in_snapshot()  # writer path: fetch + persist to stable key
    _bs = g._latest_body_stats()   # reader path: must not raise "'int' object is not subscriptable"
    g.get_body_composition = _save_bc
    check("epoch body-comp date -> weight still parsed, no crash", _bs["weight_kg"] == 74.9)
    check("epoch body-comp date -> as_of is a real ISO date (2026-07-23), not the raw epoch",
          _bs["as_of"] == "2026-07-23")
    check("epoch body-comp date -> staleness_days computed (an int)",
          isinstance(_bs["staleness_days"], int))

    # Regression: a NEW weigh-in must reach the readers via the STABLE snapshot
    # key, not a date-range key that rolls over each local midnight. The bug was
    # that the refresh warmed a date-keyed entry (30d window) while the readers
    # hit a different one (35d, or UTC-vs-local end date) — so on the readonly
    # web instance the fresh weigh-in never appeared. Here: the writer persists
    # today's weigh-in once; every reader then sees it with NO further fetch,
    # even though the date-range fetcher is disabled.
    print("weigh-in readers share one STABLE snapshot (survives date rollover):")
    _save_bc2 = g.get_body_composition
    g.get_body_composition = lambda startdate=None, enddate=None, **k: {"dateWeightList": [
        {"calendarDate": g._local_today().isoformat(), "weight": 73500}]}
    g.refresh_weigh_in_snapshot()   # cron writes the snapshot
    # Now make any further date-range fetch explode: readers must not need it.
    def _boom(*a, **k):
        raise AssertionError("readers must read the snapshot, not re-fetch by date range")
    g.get_body_composition = _boom
    _bs2 = g._latest_body_stats()
    _series = g._weight_series()
    g.get_body_composition = _save_bc2
    check("current-weight reader sees the fresh weigh-in from the snapshot",
          _bs2["weight_kg"] == 73.5 and _bs2["as_of"] == g._local_today().isoformat())
    check("history series sees the same fresh weigh-in (no separate fetch)",
          _series and _series[-1] == (g._local_today().isoformat(), 73.5))
    check("snapshot window is anchored to LOCAL today, not UTC",
          g.weigh_in_window()[1] == g._local_today().isoformat())
    # Restore the default weigh-in (TODAY-1, 74.1kg) into the snapshot so the
    # downstream generate_fueling_plan checks see the fixture they expect.
    g.refresh_weigh_in_snapshot()

    print("manual weigh-in logging (dashboard form -> log_weight):")
    # Seed a known Garmin series: TODAY-2 and TODAY-1.
    _save_bc3 = g.get_body_composition
    _d1 = (TODAY - timedelta(days=1)).isoformat()
    _d2 = (TODAY - timedelta(days=2)).isoformat()
    g.get_body_composition = lambda startdate=None, enddate=None, **k: {"dateWeightList": [
        {"calendarDate": _d2, "weight": 75000},
        {"calendarDate": _d1, "weight": 74600}]}
    g.refresh_weigh_in_snapshot()
    cache.put("manual_weigh_in", {}, {}, key_parts=["log"])   # start with no manual log
    # Log today (a gap Garmin doesn't have) — should append and be current.
    _r = g.log_weight(74.2, entry_date=TODAY.isoformat())
    check("log_weight reports saved", _r.get("saved") is True and _r.get("count") == 1)
    check("manual entry fills today's gap and becomes the current weight",
          g._weigh_in_entries()[-1] == {"date": TODAY.isoformat(), "weight_kg": 74.2, "source": "manual"})
    # Log a correction for a day Garmin already covers — manual must WIN.
    g.log_weight(73.9, entry_date=_d1)
    _by = {e["date"]: e["weight_kg"] for e in g._weigh_in_entries()}
    check("manual entry overrides Garmin's reading for the same day", _by[_d1] == 73.9)
    check("untouched Garmin day is preserved in the merge", _by[_d2] == 75.0)
    # Re-logging the same day overwrites rather than duplicating.
    g.log_weight(74.0, entry_date=_d1)
    _dates = [e["date"] for e in g._weigh_in_entries()]
    check("re-logging a day overwrites (no duplicate dates)",
          len(_dates) == len(set(_dates)) and {e["date"]: e["weight_kg"] for e in g._weigh_in_entries()}[_d1] == 74.0)
    # The engine validates kg sanity bounds (20–400) to catch garbage input.
    check("out-of-range weight is rejected",
          g.log_weight(450.0).get("saved") is False)
    check("non-numeric weight is rejected", g.log_weight("heavy").get("saved") is False)
    check("future-dated weigh-in is rejected",
          g.log_weight(74.0, entry_date=(TODAY + timedelta(days=1)).isoformat()).get("saved") is False)
    # Clean up so downstream tests see only Garmin data again.
    cache.put("manual_weigh_in", {}, {}, key_parts=["log"])
    g.get_body_composition = _save_bc3
    g.refresh_weigh_in_snapshot()

    print("generate_fueling_plan (Katch-McArdle BMR from body-fat):")
    plan = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)
    check("no error", "error" not in plan and not plan.get("no_goal_available"))
    check("plan carries a generated_at timestamp",
          isinstance(plan.get("generated_at"), str) and len(plan["generated_at"]) > 0)
    check("bmr from Katch-McArdle when body-fat is known",
          plan["bmr"]["source"] == "katch_mcardle")
    check("FFM measured from body-fat (not the 80% fallback)",
          abs(plan["fat_free_mass_kg"] - round(74.1 * (1 - 0.14), 1)) <= 0.2)
    check("Katch BMR = 370 + 21.6*FFM",
          plan["bmr"]["value"] == round(370 + 21.6 * plan["fat_free_mass_kg"]))
    check("weight_history carries the logged weigh-in for the chart",
          plan.get("weight_history") == [{"date": (TODAY - timedelta(days=1)).isoformat(),
                                          "weight_kg": 74.1}])
    bmr = plan["bmr"]["value"]
    floor = round(bmr * 1.2)
    check("7 day rows", len(plan["days"]) == 7)
    check("all targets >= BMR*1.2 floor", all(d["target_kcal"] >= floor for d in plan["days"]))
    check("deficit capped at <=500", plan["daily_kcal_adjustment"] >= -500)
    check("energy base = RMR x NEAT (below old x1.3)",
          plan["energy_base"]["value"] == round(bmr * 1.15))
    check("net exercise now equals total burn (both are Active Calories only)",
          plan["days"][4]["net_exercise_kcal"] == plan["days"][4]["est_burn_kcal"])
    check("TEF applied on the formula path (protein-weighted, >0)",
          plan["days"][0]["tef_kcal"] > 0)
    check("real deficit preserved: expenditure - target == pre-TEF deficit",
          all(d["target_deficit_kcal"] == d["expected_expenditure_kcal"] - d["target_kcal"]
              for d in plan["days"]))

    day0 = plan["days"][0]   # threshold (75min)
    day1 = plan["days"][1]   # recovery
    day4 = plan["days"][4]   # long ride
    day2 = plan["days"][2]   # rest

    print("protein periodization (anchored to goal weight):")
    check("protein anchored to goal weight (72kg), not scale weight",
          day2["protein_g"] == round(72.0 * day2["protein_g_per_kg"]))
    check("lose-goal protein base >= 2.2 g/kg", day2["protein_g_per_kg"] >= 2.2)
    check("threshold-day protein periodized up", day0["protein_g"] > day2["protein_g"])
    check("threshold-day protein_g_per_kg > rest", day0["protein_g_per_kg"] > day2["protein_g_per_kg"])

    print("90-min fuel rule + per-day deficit:")
    check("75-min threshold NOT fueled (default 90-min rule)", not day0["needs_fuel"])
    check("rest day has no fuel card", not day2["needs_fuel"])
    check("recovery short day: no fuel card", not day1["needs_fuel"])
    check("long ride (3.5h) fueled", day4["needs_fuel"])
    check("long-ride card has caffeine", day4["fuel"][0].get("caffeine_mg") == round(74.1 * 3))
    check("every day has target_deficit_kcal",
          all("target_deficit_kcal" in d for d in plan["days"]))
    check("deficit = expenditure - target",
          day0["target_deficit_kcal"] == day0["expected_expenditure_kcal"] - day0["target_kcal"])
    plan60 = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, fuel_min_minutes=60)
    check("75-min threshold IS fueled when min lowered to 60", plan60["days"][0]["needs_fuel"])

    print("similar-activity burn calibration:")
    check("threshold burn from similar-duration history",
          day0["sessions"][0]["burn_source"] in ("history_similar", "history_sport"))
    check("threshold carbs = 7.5 g/kg", day0["carb_g_per_kg"] == 7.5)
    check("long ride carbs (7 g/kg) > recovery day carbs", day4["carbs_g"] > day1["carbs_g"])
    check("long ride burn > recovery burn", day4["est_burn_kcal"] > day1["est_burn_kcal"])

    print("scheduled (not-yet-done) session estimates are netted to Active Calories:")
    rmr_per_hr_expected = bmr / 24.0
    s0 = day0["sessions"][0]
    gross_est0 = s0["kcal_per_hour"] * g._INTENSITY_MULT.get(s0["intensity"], 1.0) * s0["hours"]
    check("scheduled burn = gross calibrated estimate minus resting-during-exercise proxy",
          abs(s0["burn_kcal"] - round(gross_est0 - rmr_per_hr_expected * s0["hours"])) <= 1)
    check("scheduled burn is strictly less than the naive gross estimate",
          s0["burn_kcal"] < round(gross_est0))

    print("actual burn swaps the estimate on today's completed workouts:")
    _save_air2 = g.get_activities_in_range
    g.get_activities_in_range = lambda sd, ed, *a, **k: _fake_activities(sd, ed) + [
        {"activityType": {"typeKey": "cycling"}, "activityName": "Bike Threshold 4x8min",
         "duration": int(1.3 * 3600), "calories": 950,
         "startTimeLocal": TODAY.isoformat() + " 07:00:00"},
        # An off-plan walk today: no walking session is scheduled, so it should
        # fold in as an *unplanned* completed workout.
        {"activityType": {"typeKey": "walking"}, "activityName": "Evening Walk",
         "duration": int(0.75 * 3600), "calories": 180,
         "startTimeLocal": TODAY.isoformat() + " 18:00:00"}]
    plan_act = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)
    g.get_activities_in_range = _save_air2
    s_today = plan_act["days"][0]["sessions"][0]
    check("today's completed session tagged actual_today", s_today["burn_source"] == "actual_today")
    check("today's completed session marked done", s_today.get("done") is True)
    check("scheduled session is not flagged unplanned", s_today.get("unplanned") is False)
    check("burn = actual 950, not the estimate", s_today["burn_kcal"] == 950)
    _unpl = [s for s in plan_act["days"][0]["sessions"] if s.get("unplanned")]
    check("off-plan walk folded in as an unplanned workout", len(_unpl) == 1)
    check("unplanned workout carries its actual burn (180)",
          _unpl and _unpl[0]["burn_kcal"] == 180)
    check("est burn total picks up actual planned + unplanned",
          plan_act["days"][0]["est_burn_kcal"] == 950 + 180)
    check("future day still uses an estimate",
          plan_act["days"][4]["sessions"][0]["burn_source"] != "actual_today")

    print("burned vs projected split:")
    d0 = plan_act["days"][0]
    check("today exposes burned_kcal", "burned_kcal" in d0)
    check("today exposes projected_burn_kcal", "projected_burn_kcal" in d0)
    check("completed workouts count as burned (bike 950 + walk 180)",
          d0["burned_kcal"] == 950 + 180)
    check("no remaining session -> projected is 0", d0["projected_burn_kcal"] == 0)
    check("burned + projected == total burn",
          d0["burned_kcal"] + d0["projected_burn_kcal"] == d0["est_burn_kcal"])
    d4 = plan_act["days"][4]
    check("future day has nothing burned yet", d4["burned_kcal"] == 0)
    check("future day's whole burn is projected",
          d4["projected_burn_kcal"] == d4["est_burn_kcal"])

    print("_dedupe_actual_workouts collapses double-recorded sessions:")
    # Same activity_id recorded twice (e.g. watch + Strava re-import) -> one session,
    # and the higher-kcal record wins.
    _dd = g._dedupe_actual_workouts([
        {"sport": "cycling", "activity_id": 111, "start": "2026-07-24 07:00:00",
         "hours": 1.3, "kcal": 900},
        {"sport": "cycling", "activity_id": 111, "start": "2026-07-24 07:00:00",
         "hours": 1.3, "kcal": 950},
    ])
    check("same activity_id merges to one", len(_dd) == 1)
    check("merge keeps the higher-kcal record", _dd[0]["kcal"] == 950)

    # No activity_id, but starts within the 10-min tolerance (ROUVY + watch) -> merge.
    _dd = g._dedupe_actual_workouts([
        {"sport": "cycling", "activity_id": None, "start": "2026-07-24 07:00:00",
         "hours": 1.3, "kcal": 900},
        {"sport": "cycling", "activity_id": None, "start": "2026-07-24 07:04:00",
         "hours": 1.3, "kcal": 820},
    ])
    check("near-simultaneous same-sport starts merge", len(_dd) == 1)
    check("near-simultaneous merge keeps higher kcal", _dd[0]["kcal"] == 900)

    # Two genuine same-sport sessions on the same day, hours apart -> both kept.
    _dd = g._dedupe_actual_workouts([
        {"sport": "cycling", "activity_id": 201, "start": "2026-07-24 07:00:00",
         "hours": 1.0, "kcal": 700},
        {"sport": "cycling", "activity_id": 202, "start": "2026-07-24 17:00:00",
         "hours": 0.6, "kcal": 420},
    ])
    check("distinct same-sport sessions are both kept", len(_dd) == 2)

    # Different sports never merge, even at identical start times.
    _dd = g._dedupe_actual_workouts([
        {"sport": "cycling", "activity_id": 301, "start": "2026-07-24 07:00:00",
         "hours": 1.0, "kcal": 700},
        {"sport": "running", "activity_id": 302, "start": "2026-07-24 07:00:00",
         "hours": 0.8, "kcal": 600},
    ])
    check("different sports never merge", len(_dd) == 2)

    # No start times at all: fall back to near-equal duration for the same sport.
    _dd = g._dedupe_actual_workouts([
        {"sport": "running", "activity_id": None, "start": "", "hours": 0.80, "kcal": 600},
        {"sport": "running", "activity_id": None, "start": "", "hours": 0.85, "kcal": 640},
    ])
    check("no-start same-sport near-equal duration merges", len(_dd) == 1)
    check("duration-merge keeps higher kcal", _dd[0]["kcal"] == 640)

    print("generate_fueling_plan's today session also converts to Active Calories:")
    def _fake_gds_active_for_plan(startdate, enddate, metrics=None, **k):
        return {"stats_and_body": {TODAY.isoformat(): {"restingCaloriesFromActivity": 100}}}
    _save_air4, _save_gds3 = g.get_activities_in_range, g.get_daily_summaries
    g.get_daily_summaries = _fake_gds_active_for_plan
    g.get_activities_in_range = lambda sd, ed, *a, **k: _fake_activities(sd, ed) + [
        {"activityType": {"typeKey": "cycling"}, "activityName": "Bike Threshold 4x8min",
         "duration": int(1.3 * 3600), "calories": 950,
         "startTimeLocal": TODAY.isoformat() + " 07:00:00"}]
    plan_active = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=1)
    g.get_activities_in_range, g.get_daily_summaries = _save_air4, _save_gds3
    check("today's session burn is Active Calories (950-100=850), not the gross 950 "
          "— matches _today_actuals so the day view and 'today so far' card agree",
          plan_active["days"][0]["sessions"][0]["burn_kcal"] == 850)

    print("macro reconciliation:")
    for d in plan["days"]:
        kcal = d["protein_g"] * 4 + d["carbs_g"] * 4 + d["fat_g"] * 9
        check(f"  {d['date']} P/C/F sums within 8% of target",
              abs(kcal - d["target_kcal"]) <= 0.08 * d["target_kcal"] + 60)
    check("fat never exceeds ~30% of calories (carbs are the flex macro)",
          all(d["fat_g"] * 9 <= 0.30 * d["target_kcal"] + 20 for d in plan["days"]))
    check("high-burn day routes surplus energy to carbs, not fat",
          day4["carbs_g"] > day4["fat_g"])

    print("deficit periodization (default for lose goals):")
    check("config says periodized", plan["config"]["periodize_deficit"] is True)
    check("rest day clamps at the floor (+ its TEF)",
          day2["target_kcal"] == round(bmr * 1.2) + day2["tef_kcal"])
    check("hard day never cut deeper than flat",
          abs(day0["kcal_adjustment"]) <= abs(plan["daily_kcal_adjustment"]) + 1)
    check("projection reports a finish date when floors bind",
          plan["projection"].get("projected_finish_date") is not None)
    # The projection's sustainable deficit must not exceed what the per-day
    # targets actually deliver: both are bound by the SAME EA floor
    # (non_exercise_base − ea_min·FFM), so the projected rate can't outrun the
    # plan. Guards against the projection using bmr·1.3 as a NEAT proxy when
    # the real modeled base is lower (overstated finish date).
    _msd = plan["projection"].get("max_sustainable_deficit_kcal")
    _max_day_def = max(d["target_deficit_kcal"] for d in plan["days"])
    if _msd is not None:
        check("projected sustainable deficit doesn't exceed the deepest per-day deficit",
              _msd <= _max_day_def + 2)
    plan_flat = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7,
                                        periodize_deficit=False)
    check("flat override: every day same adjustment",
          len({d["kcal_adjustment"] for d in plan_flat["days"]}) == 1)
    check("flat override echoed", plan_flat["config"]["periodize_deficit"] is False)

    print("deficit allocation uses training load (TSS), not just the category label:")
    # Two "easy" days plus a rest day, floors permissive enough that weight
    # alone decides the split. Loads are TSS (hours x IF^2 x 100). A day with a
    # big two-session volume just below the protection threshold (~55 TSS, e.g.
    # ~1.3h easy) should bank noticeably less of the weekly deficit than a token
    # single easy session (~14 TSS, ~20min) — even though a category-only
    # system would have weighted them identically — and rest (load 0) should
    # still absorb the most.
    adjs, resid = g._allocate_deficit(
        [3000.0, 3000.0, 3000.0], ["easy", "easy", "rest"], [55.0, 14.0, 0.0],
        -1500.0, [500.0, 500.0, 500.0])
    check("big-volume easy day banks less deficit than a token easy day",
          adjs[0] > adjs[1])
    check("rest still banks more deficit than either easy day",
          adjs[2] < adjs[1])
    check("weekly deficit fully absorbed across the window",
          resid == 0 and abs(sum(adjs) - (-1500 * 3)) <= 2)  # rounding
    # A big-volume easy day should get the same "never cut deeper than flat"
    # protection as a categorically hard day, once its TSS crosses the
    # threshold (LOAD_PROTECT_TSS=75) — not just a smaller weight. ~2.9h easy
    # is ~124 TSS, well past it. Two tight-floored rest days max out fast and
    # try to dump their unmet share onto the easy day; without load-based
    # protection that would push it past the flat cut.
    adjs2, resid2 = g._allocate_deficit(
        [3000.0, 500.0, 500.0], ["easy", "rest", "rest"], [124.0, 0.0, 0.0],
        -1200.0, [0.0, 400.0, 400.0])
    check("a high-load 'easy' day is protected like a hard day (clamped at -1200, no deeper)",
          adjs2[0] == -1200)
    check("the unabsorbed residual is reported, not silently forced onto the protected day",
          resid2 == -2200)

    print("TSS load proxy (intensity counts non-linearly):")
    check("1h at threshold ~ 90 TSS", abs(g._session_tss("threshold", 1.0) - 90.25) < 0.5)
    check("2.17h easy clears the 75-TSS protection threshold",
          g._session_tss("easy", 2.17) >= 75)
    check("20min of the same easy work does NOT (volume matters)",
          g._session_tss("easy", 0.33) < 75)
    check("a threshold hour outranks a longer easy hour (intensity matters)",
          g._session_tss("threshold", 1.0) > g._session_tss("easy", 1.3))
    check("rest is zero load", g._session_tss("rest", 2.0) == 0.0)

    print("mean daily exercise over trailing 30 days (weekday vs weekend split):")
    # Build one ride on every day in the last 30, but make weekend rides bigger
    # (1000 kcal / 1.5h) than weekday rides (700 kcal / 1h). The split means
    # should reflect each day-type's own pattern, each divided by that type's
    # day count in the window — NOT one blended average.
    _hist_split = []
    for i in range(30):  # days TODAY .. TODAY-29 — exactly the window/denominator
        d = TODAY - timedelta(days=i)
        wknd = d.weekday() >= 5
        _hist_split.append({
            "duration": int((1.5 if wknd else 1.0) * 3600),
            "calories": 1000 if wknd else 700,
            "activityType": {"typeKey": "cycling"},
            "startTimeLocal": d.isoformat() + " 07:00:00"})
    _avg = g._mean_daily_exercise(_hist_split, rmr_per_hr=70.0, as_of=TODAY, window_days=30)
    check("returns a weekday/weekend dict", set(_avg) == {"weekday", "weekend"})
    # Every day in the window has a ride, so each bucket's per-day mean is just
    # that bucket's single-session net.
    check("weekday mean ~ weekday session net (700 - 70*1.0)",
          abs(_avg["weekday"][0] - (700 - 70)) < 5)
    check("weekend mean ~ weekend session net (1000 - 70*1.5)",
          abs(_avg["weekend"][0] - (1000 - 70 * 1.5)) < 5)
    check("weekend burn assumption exceeds weekday (long sessions land on weekends)",
          _avg["weekend"][0] > _avg["weekday"][0])
    check("weekend hours mean ~ 1.5", abs(_avg["weekend"][1] - 1.5) < 0.05)
    check("no history -> both buckets zero",
          g._mean_daily_exercise([], 70.0, TODAY) == {"weekday": (0.0, 0.0), "weekend": (0.0, 0.0)})
    # Garmin sometimes returns startTimeLocal as an epoch int, not an ISO
    # string. A stray int must be skipped, not crash the whole plan.
    _wd = (TODAY - timedelta(days=(TODAY.weekday() + 1) % 7 + 1))  # a recent weekday
    while _wd.weekday() >= 5:
        _wd -= timedelta(days=1)
    _mixed = g._mean_daily_exercise(
        [{"duration": 3600, "calories": 700, "activityType": {"typeKey": "cycling"},
          "startTimeLocal": 1690000000000},  # epoch int — must not blow up
         {"duration": 3600, "calories": 700, "activityType": {"typeKey": "cycling"},
          "startTimeLocal": _wd.isoformat() + " 07:00:00"}],
        rmr_per_hr=70.0, as_of=TODAY, window_days=30)
    check("int startTimeLocal is skipped, not fatal (one valid weekday counts)",
          _mixed["weekday"][0] > 0)
    check("activities outside the window are excluded",
          g._mean_daily_exercise(
              [{"duration": 3600, "calories": 700, "activityType": {"typeKey": "cycling"},
                "startTimeLocal": (TODAY - timedelta(days=45)).isoformat() + " 07:00:00"}],
              70.0, TODAY, window_days=30) == {"weekday": (0.0, 0.0), "weekend": (0.0, 0.0)})

    print("phantom-day fill: runs of >=2 unscheduled days assume typical training:")
    # One scheduled day, then a long open stretch. The open run should be
    # filled with an assumed typical training day (not banked as rest), and
    # protected from a deeper-than-flat cut like the training days they'll
    # likely become. Give history real dates so the 30-day mean is non-zero.
    _save_sched_ph, _save_air_ph = g.get_scheduled_workouts, g.get_activities_in_range
    g.get_scheduled_workouts = lambda s, e, **k: [
        {"date": s, "title": "Bike Threshold 4x8min", "sportTypeKey": "cycling",
         "duration": 75 * 60, "itemType": "workout"}]  # only day 0 scheduled
    g.get_activities_in_range = lambda sd, ed, *a, **k: [
        {"activityType": {"typeKey": "cycling"}, "activityName": "ride",
         "duration": 3600, "calories": 700,
         "startTimeLocal": (TODAY - timedelta(days=i)).isoformat() + " 07:00:00"}
        for i in range(1, 20)]
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40)
    plan_ph = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)
    g.get_scheduled_workouts, g.get_activities_in_range = _save_sched_ph, _save_air_ph
    _assumed = [d for d in plan_ph["days"]
                if any(s.get("assumed") for s in d["sessions"])]
    check("open days in a >=2 run are filled with an assumed session", len(_assumed) >= 2)
    check("day 0 (scheduled) is NOT a phantom day",
          not any(s.get("assumed") for s in plan_ph["days"][0]["sessions"]))
    check("assumed session is not counted as rest",
          all(d["primary_intensity"] != "rest" for d in _assumed))
    check("assumed session carries a positive burn",
          all(d["est_burn_kcal"] > 0 for d in _assumed))
    check("assumed burn is flagged as such for the dashboard",
          all(d["sessions"][0]["burn_source"] == "assumed_30d_avg" for d in _assumed))
    check("a note explains the phantom-day fill",
          any("scheduled yet" in n for n in plan_ph["notes"]))
    check("phantom days no longer take the deepest cut (protected like training)",
          all(abs(d["kcal_adjustment"]) <= abs(plan_ph["daily_kcal_adjustment"]) + 1
              for d in _assumed))
    # An isolated single open day (flanked by scheduled days) stays true rest.
    g.get_scheduled_workouts = lambda s, e, **k: [
        {"date": s, "title": "Bike Threshold", "sportTypeKey": "cycling",
         "duration": 75 * 60, "itemType": "workout"},
        {"date": (date.fromisoformat(s) + timedelta(days=2)).isoformat(),
         "title": "Recovery spin", "sportTypeKey": "cycling",
         "duration": 45 * 60, "itemType": "workout"}]  # day 0 and day 2, day 1 open
    g.get_activities_in_range = lambda sd, ed, *a, **k: [
        {"activityType": {"typeKey": "cycling"}, "activityName": "ride",
         "duration": 3600, "calories": 700,
         "startTimeLocal": (TODAY - timedelta(days=i)).isoformat() + " 07:00:00"}
        for i in range(1, 20)]
    plan_iso = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=3)
    g.get_scheduled_workouts, g.get_activities_in_range = _save_sched_ph, _save_air_ph
    check("an isolated single open day stays true rest (no phantom fill)",
          plan_iso["days"][1]["primary_intensity"] == "rest"
          and not any(s.get("assumed") for s in plan_iso["days"][1]["sessions"]))
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40)

    print("local-timezone 'today' + late-workout cutoff:")
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")
    check("_local_today returns a date", isinstance(g._local_today(), date))
    _save_now = g._local_now
    # 8:00pm ET: before the 20:30 cutoff -> nothing dropped
    g._local_now = lambda: _dt(TODAY.year, TODAY.month, TODAY.day, 20, 0, tzinfo=_ET)
    check("before cutoff: not flagged as past", g._past_workout_cutoff(TODAY) is False)
    # 9:00pm ET: past the cutoff for today, but not for a future day
    g._local_now = lambda: _dt(TODAY.year, TODAY.month, TODAY.day, 21, 0, tzinfo=_ET)
    check("past cutoff for today", g._past_workout_cutoff(TODAY) is True)
    check("future day never past cutoff", g._past_workout_cutoff(TODAY + timedelta(days=1)) is False)
    check("a fully-elapsed past day is past cutoff", g._past_workout_cutoff(TODAY - timedelta(days=1)) is True)
    # A not-yet-done scheduled session TODAY is dropped as a no-show past cutoff.
    _save_sched_co = g.get_scheduled_workouts
    g.get_scheduled_workouts = lambda s, e, **k: [
        {"date": s, "title": "Evening Threshold", "sportTypeKey": "cycling",
         "duration": 75 * 60, "itemType": "workout"}]
    plan_late = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=1)
    check("past cutoff: unlogged scheduled session dropped from today",
          not any(s["sport"] == "cycling" for s in plan_late["days"][0]["sessions"]))
    check("past cutoff: day falls back to rest once its only session is dropped",
          plan_late["days"][0]["primary_intensity"] == "rest")
    check("past cutoff: the drop is silent (no cutoff note surfaced)",
          not any("weren't logged" in n or "20:30" in n for n in plan_late["notes"]))
    # Before the cutoff, the same session is kept.
    g._local_now = lambda: _dt(TODAY.year, TODAY.month, TODAY.day, 12, 0, tzinfo=_ET)
    plan_early = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=1)
    check("before cutoff: scheduled session is kept",
          any(s["sport"] == "cycling" for s in plan_early["days"][0]["sessions"]))
    g.get_scheduled_workouts = _save_sched_co
    g._local_now = _save_now

    print("floors dropped (bmr_floor_mult=0, ea_floor=0):")
    plan_nf = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7,
                                      bmr_floor_mult=0, ea_floor=0)
    check("no floor in config", plan_nf["config"]["bmr_floor_mult"] is None)
    check("some day dips below BMRx1.2",
          any(d["target_kcal"] < round(bmr * 1.2) for d in plan_nf["days"]))
    check("rest day takes a bigger cut than threshold day",
          plan_nf["days"][2]["target_deficit_kcal"] > plan_nf["days"][0]["target_deficit_kcal"])
    check("weekly deficit fully absorbed (no shortfall note)",
          not any("could not absorb" in n for n in plan_nf["notes"]))
    check("ea_floor=0 silences the RED-S note",
          not any("Low energy availability" in n for n in plan_nf["notes"]))

    print("enforced EA minimum + absolute kcal floor:")
    plan_ea25 = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7,
                                        bmr_floor_mult=0, ea_floor=25, ea_min=25)
    check("config echoes ea_min", plan_ea25["config"]["ea_min_kcal_per_kg_ffm"] == 25)
    check("every day EA >= ea_min (within rounding)",
          all(d["energy_availability_kcal_per_kg_ffm"] >= 24.5 for d in plan_ea25["days"]))
    ffm = plan_ea25["fat_free_mass_kg"]
    check("hard-day target floored at ea_min*FFM + burn",
          all(d["target_kcal"] >= round(25 * ffm + d["est_burn_kcal"]) - 1
              for d in plan_ea25["days"]))
    plan_mk = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7,
                                      bmr_floor_mult=0, ea_floor=0, min_kcal=2100)
    check("config echoes min_kcal", plan_mk["config"]["min_kcal"] == 2100)
    check("no day below the absolute floor",
          all(d["target_kcal"] >= 2100 for d in plan_mk["days"]))
    # infeasible pace + ea_min -> shortfall note carries an ETA
    tight2 = (TODAY + timedelta(weeks=3)).isoformat()
    g.set_fueling_goal(goal_type="lose", target_weight_kg=69.0, target_date=tight2,
                       sex="male", height_cm=178, age=40, max_deficit_kcal=0,
                       ea_min=25, ea_floor=25, bmr_floor_mult=0)
    plan_eta = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)
    check("EA held at min even under an infeasible goal",
          all(d["energy_availability_kcal_per_kg_ffm"] >= 24.5 for d in plan_eta["days"]))
    check("projection gives a finish date under an infeasible goal",
          plan_eta["projection"].get("projected_finish_date") is not None)
    # restore the default lose goal for remaining checks
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40)

    print("fuel windows scale down to match a carb-trimmed day (never mismatch):")
    # An absurd goal (lose 22kg in 3 days) with every floor disabled forces a
    # day's carb budget far below what a 3.5h fueled ride needs (~258g),
    # reproducing the real-world case: a training day trimmed hard enough
    # that the 'Workout fuel' meal line and the per-workout fuel timeline
    # would otherwise disagree.
    tight3 = (TODAY + timedelta(days=3)).isoformat()
    g.set_fueling_goal(goal_type="lose", target_weight_kg=50.0, target_date=tight3,
                       sex="male", height_cm=178, age=40, max_deficit_kcal=0,
                       ea_min=0, ea_floor=0, bmr_floor_mult=0, min_kcal=0,
                       periodize_deficit=False)
    plan_fuel_trim = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)
    day4_ft = plan_fuel_trim["days"][4]  # the long-ride day (>=90min -> needs_fuel)
    check("long-ride day still needs fuel", day4_ft["needs_fuel"])
    wf = next((m for m in day4_ft["meals"] if m["meal"].startswith("Workout fuel")), None)
    fuel_total = sum(f["pre_carbs_g"] + f["during_carbs_g_total"] + f["post_carbs_g"]
                     for f in day4_ft["fuel"])
    check("this day's carb budget was actually trimmed below the fuel need",
          fuel_total < 258)  # untrimmed would be ~40+158+60=258g
    check("'Workout fuel' meal line exists", wf is not None)
    check("per-workout fuel timeline total matches the meal line exactly (no mismatch)",
          wf is not None and fuel_total == wf["carbs_g"])
    check("a note explains the fuel windows were scaled down",
          any("fuel windows were scaled" in n.lower() for n in plan_fuel_trim["notes"]))
    # restore the default lose goal for remaining checks
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40)

    print("title duration parsing:")
    check("'50min Aerobic Run' -> ~0.83h", abs(g._duration_from_title("50min Aerobic Run") - 0.83) < 0.02)
    check("\"Master's Swim - 90min\" -> 1.5h", g._duration_from_title("Master's Swim - 90min") == 1.5)
    check("'3x15min Sweet Spot' not read as 15min", g._duration_from_title("3x15min Sweet Spot Bike") is None)
    check("'4min Repeats' not read (interval, 1 digit)", g._duration_from_title("Punchy Threshold - 4min Repeats") is None)
    check("'Endurance 1.5h ride' -> 1.5h", g._duration_from_title("Endurance 1.5h ride") == 1.5)
    check("no-duration title -> None", g._duration_from_title("Short Run Off the Bike") is None)

    print("structured-workout step duration:")
    _wo = {"workoutSegments": [{"workoutSteps": [
        {"type": "ExecutableStepDTO", "endCondition": {"conditionTypeKey": "time"},
         "endConditionValue": 900},
        {"type": "RepeatGroupDTO", "numberOfIterations": 6, "workoutSteps": [
            {"type": "ExecutableStepDTO", "endCondition": {"conditionTypeKey": "time"},
             "endConditionValue": 1500},
            {"type": "ExecutableStepDTO", "endCondition": {"conditionTypeKey": "time"},
             "endConditionValue": 300}]},
        {"type": "ExecutableStepDTO", "endCondition": {"conditionTypeKey": "time"},
         "endConditionValue": 900}]}]}
    check("repeat-group steps sum to 3.5h (12600s)", g._workout_duration_secs(_wo) == 12600)
    check("distance-only steps -> None (no time to sum)",
          g._workout_duration_secs({"workoutSegments": [{"workoutSteps": [
              {"endCondition": {"conditionTypeKey": "distance"},
               "endConditionValue": 5000}]}]}) is None)
    _saved_gw = g.get_workout_by_id
    g.get_workout_by_id = lambda wid, **k: _wo   # type: ignore[assignment]
    hrs_sd, src_sd = g._planned_hours(
        {"workoutId": 123, "title": "Aerobic Ride with Sweet Spot Surges"}, "tempo")
    g.get_workout_by_id = _saved_gw              # type: ignore[assignment]
    check("_planned_hours reads 3.5h from workout steps, not the 1h default", hrs_sd == 3.5)
    check("_planned_hours source = workout_detail", src_sd == "workout_detail")

    print("energy-availability guard:")
    check("every day has an EA value",
          all(d.get("energy_availability_kcal_per_kg_ffm") is not None for d in plan["days"]))
    check("ffm surfaced (lean mass from body fat)", plan["fat_free_mass_kg"] > 0)

    print("carb-load (race week) mode:")
    plan_cl = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, carb_load=True)
    check("carb_load flag echoed", plan_cl["carb_load"] is True)
    check("no deficit during carb load", plan_cl["daily_kcal_adjustment"] == 0)
    check("all days at 9 g/kg carbs", all(d["carb_g_per_kg"] == 9.0 for d in plan_cl["days"]))
    check("carb-load carbs > normal-day carbs",
          plan_cl["days"][2]["carbs_g"] > plan["days"][2]["carbs_g"])

    print("configurable EA floor:")
    # The modest default goal (2.1kg/6wk) no longer dips any day below the
    # EA=30 threshold now that EA is computed from workout Active Calories
    # only (not gross burn) — that's the fix working as intended, so use a
    # genuinely aggressive cut to exercise the low-EA note itself.
    tight_ea = (TODAY + timedelta(weeks=2)).isoformat()
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=tight_ea,
                       sex="male", height_cm=178, age=40)
    plan_tight_ea = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)
    check("aggressive-cut plan raises the low-EA note",
          any("Low energy availability" in n for n in plan_tight_ea["notes"]))
    plan_ea = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, ea_floor=20)
    check("config echoes ea_floor=20", plan_ea["config"]["ea_floor_kcal_per_kg_ffm"] == 20)
    check("lower EA floor suppresses the note",
          not any("Low energy availability" in n for n in plan_ea["notes"]))
    # restore the default lose goal for remaining checks
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40)

    print("uncapped deficit (removed cap):")
    tight = (TODAY + timedelta(weeks=3)).isoformat()
    g.set_fueling_goal(goal_type="lose", target_weight_kg=69.0, target_date=tight,
                       sex="male", height_cm=178, age=40, max_deficit_kcal=0)
    planu = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)
    check("uncapped deficit exceeds 500", abs(planu["daily_kcal_adjustment"]) > 500)
    check("config echoes no cap", planu["config"]["deficit_cap_kcal"] is None)
    check("BMR*1.2 floor still applies",
          all(d["target_kcal"] >= round(planu["bmr"]["value"] * 1.2) for d in planu["days"]))
    # restore the default-capped lose goal for the remaining checks
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40)

    print("save=True merges into weekly snapshot:")
    plan2 = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, save=True)
    check("saved_to_weekly_snapshot present", "saved_to_weekly_snapshot" in plan2)
    monday = (TODAY - timedelta(days=TODAY.weekday())).isoformat()
    snap = _STORE.get(_key("weekly_snapshots", [monday]))
    check("snapshot persisted with nutrition_plan", bool(snap and snap.get("nutrition_plan")))
    check("nutrition_plan keyed by ISO date", TODAY.isoformat() in snap["nutrition_plan"])

    print("front-loading (steeper early, tapers to target):")
    # goal: lose 74.1 -> 70.0, start captured at 74.1 (== current) so frac ~ 1
    fl_goal_date = (TODAY + timedelta(weeks=10)).isoformat()
    g.set_fueling_goal(goal_type="lose", target_weight_kg=70.0, target_date=fl_goal_date,
                       sex="male", height_cm=178, age=40, start_weight_kg=74.1,
                       max_deficit_kcal=0)
    flat = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, front_load=0)
    front = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, front_load=0.5)
    check("config echoes front_load", front["config"]["front_load"] == 0.5)
    check("at start weight, front-loaded deficit is steeper",
          abs(front["daily_kcal_adjustment"]) > abs(flat["daily_kcal_adjustment"]))
    check("front-load ~1.5x at frac=1",
          abs(abs(front["daily_kcal_adjustment"]) - 1.5 * abs(flat["daily_kcal_adjustment"])) <= 2)
    # near target (current ~ target) the front-load should ease below linear
    g.get_athlete_baseline = lambda *a, **k: {"weight_kg": 70.3, "staleness_days": {"weight": 2}}
    g.get_body_composition = lambda startdate=None, enddate=None, **k: {"dateWeightList": [
        {"date": (TODAY - timedelta(days=1)).isoformat(), "weight": 70300,
         "bodyFat": 14.0, "muscleMass": 34000}]}
    g.refresh_weigh_in_snapshot()   # readers use the stable snapshot now
    near = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, front_load=0.5)
    flat_near = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, front_load=0)
    check("near target, front-loaded deficit eases below linear",
          abs(near["daily_kcal_adjustment"]) < abs(flat_near["daily_kcal_adjustment"]))
    check("front-load reflected in config", front["config"]["front_load"] == 0.5)
    # restore stubs + default goal
    g.get_athlete_baseline = _fake_baseline
    g.get_body_composition = _fake_body_comp
    g.refresh_weigh_in_snapshot()   # re-seed snapshot with the default fixture
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40)

    print("ahead-of-schedule ease (_schedule_lead):")
    _sl_set = (TODAY - timedelta(weeks=4)).isoformat()
    _sl_tgt = (TODAY + timedelta(weeks=16)).isoformat()
    _sl_goal = dict(goal_type="lose", target_weight_kg=68.0, start_weight_kg=80.0,
                    set_date=_sl_set, target_date=_sl_tgt)  # 0.6 kg/wk schedule
    check("on schedule -> no ease",
          g._schedule_lead(_sl_goal, 77.6, None)["ease_factor"] == 1.0)
    check("~2wk ahead is the threshold (still no ease yet)",
          g._schedule_lead(_sl_goal, 76.4, None)["ease_factor"] == 1.0)
    _far = g._schedule_lead(_sl_goal, 74.0, None)
    check("far ahead eases", _far["ease_factor"] < 1.0)
    check("ease is capped at 50% no matter how far ahead", _far["ease_factor"] >= 0.50)
    check("behind schedule -> no ease",
          g._schedule_lead(_sl_goal, 78.5, None)["ease_factor"] == 1.0)
    check("non-lose goal -> None", g._schedule_lead(dict(goal_type="maintain"), 75, None) is None)
    check("prefers trend fitted weight when trustworthy",
          g._schedule_lead(_sl_goal, 77.6, {"confidence": "high", "fitted_weight_kg": 74.0})["source"] == "trend")
    check("falls back to current weight without a trend",
          g._schedule_lead(_sl_goal, 74.0, None)["source"] == "current_weight")
    # In-plan: a goal where current weight is well ahead of the line eases the
    # deficit and surfaces a note. Use a manual weight override to drive it.
    g.set_fueling_goal(goal_type="lose", target_weight_kg=68.0, target_date=_sl_tgt,
                       sex="male", height_cm=178, age=40, start_weight_kg=80.0,
                       current_weight_kg=74.0, max_deficit_kcal=0)
    # set_date is stamped to today by set_fueling_goal, so the lead is measured
    # from a fresh schedule start; force a known set_date via the stored goal.
    _g = g.get_fueling_goal()["goal"]; _g["set_date"] = _sl_set
    cache.put("fueling_goal", {"key": "current"}, _g, key_parts=["current"])
    plan_ahead = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)
    check("ahead-of-schedule note surfaced",
          any("ahead of schedule" in n.lower() for n in plan_ahead["notes"]))

    print("aggressive mode: earlier finish when lighter; ease only when lean AND ahead:")
    # Same well-ahead goal, but aggressive=true: holds the max deficit, so the
    # projected finish must move EARLIER as current weight drops (the whole
    # point). The lean+ahead ease must NOT fire while still above 157 lb
    # (~71.2 kg), even when well ahead of schedule.
    _agg_tgt = (TODAY + timedelta(weeks=20)).isoformat()
    def _agg_finish(cur_w):
        # Floors mirroring a real aggressive goal (EA-enforced, no BMR-mult
        # floor) so the max sustainable deficit is meaningful — a 1.2x BMR
        # floor would otherwise crush max_daily to ~0.1xBMR and stall it.
        g.set_fueling_goal(goal_type="lose", target_weight_kg=68.0, target_date=_agg_tgt,
                           sex="male", height_cm=178, age=40, start_weight_kg=80.0,
                           current_weight_kg=cur_w, max_loss_lb_per_week=2.0,
                           ea_min=17.0, bmr_floor_mult=0, aggressive=True)
        return g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)
    _p_heavy = _agg_finish(78.0)
    _p_light = _agg_finish(73.0)   # 5kg lighter
    check("aggressive: plan surfaces the aggressive-mode note",
          any("aggressive mode" in n.lower() for n in _p_heavy["notes"]))
    check("aggressive: lean+ahead ease does NOT fire while still >157 lb",
          not any("lean + ahead" in n.lower() for n in _p_light["notes"]))
    _fh = _p_heavy["projection"].get("projected_finish_date")
    _fl = _p_light["projection"].get("projected_finish_date")
    check("aggressive: lighter current weight -> earlier finish date",
          _fh and _fl and _fl < _fh)
    check("aggressive: deficit holds the cap (2 lb/wk = 1000 kcal), not date-paced",
          _p_heavy["daily_kcal_adjustment"] == -1000)

    # Lean (<157 lb / 71.2 kg) AND >2 weeks ahead: the aggressive deficit now
    # eases. 70.0 kg is under the gate; on an 80->68 kg / 24-week schedule
    # started 4 weeks ago the expected weight is ~78 kg, so 70 is ~13 kg /
    # >2 weeks ahead. The eased goal must be a SMALLER deficit than the same
    # goal at the same lead but still-heavy (72.0 kg, above the gate).
    _agg_lean_tgt = (TODAY + timedelta(weeks=20)).isoformat()
    _agg_lean_set = (TODAY - timedelta(weeks=4)).isoformat()
    def _agg_lean_plan(cur_w):
        g.set_fueling_goal(goal_type="lose", target_weight_kg=68.0, target_date=_agg_lean_tgt,
                           sex="male", height_cm=178, age=40, start_weight_kg=80.0,
                           current_weight_kg=cur_w, max_loss_lb_per_week=2.0,
                           ea_min=17.0, bmr_floor_mult=0, aggressive=True)
        _gg = g.get_fueling_goal()["goal"]; _gg["set_date"] = _agg_lean_set
        cache.put("fueling_goal", {"key": "current"}, _gg, key_parts=["current"])
        return g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)
    _p_lean = _agg_lean_plan(70.0)   # under 71.2 kg gate, well ahead
    _p_gate = _agg_lean_plan(72.0)   # above the gate, same lead posture
    check("aggressive: lean+ahead surfaces the ease note",
          any("lean + ahead" in n.lower() for n in _p_lean["notes"]))
    check("aggressive: lean+ahead eases the deficit below full aggression",
          abs(_p_lean["daily_kcal_adjustment"]) < abs(_p_gate["daily_kcal_adjustment"]))
    check("aggressive: still-heavy (>157 lb) holds full aggression, no ease note",
          not any("lean + ahead" in n.lower() for n in _p_gate["notes"]))

    # restore the default goal
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40)

    print("max_loss_lb_per_week + trajectory projection:")
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40)
    plan_ml = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7,
                                      max_loss_lb_per_week=0.5)
    check("loss-rate cap -> ~250 kcal/day cap", abs(plan_ml["daily_kcal_adjustment"]) <= 251)
    check("config echoes max_loss_lb_per_week", plan_ml["config"]["max_loss_lb_per_week"] == 0.5)
    check("projection has weekly points", len(plan_ml["projection"]["points"]) >= 2)
    check("projection reaches target with a finish date",
          plan_ml["projection"]["projected_finish_date"] is not None)
    check("projection reports max sustainable weekly loss",
          plan_ml["projection"]["max_weekly_loss_kg"] > 0)

    print("swim fueling rule (no pre/during):")
    _save_sched = g.get_scheduled_workouts
    g.get_scheduled_workouts = lambda s, e, **k: [
        {"date": s, "title": "Long Swim Set", "sportTypeKey": "swimming",
         "duration": 2 * 3600, "itemType": "workout"}]
    psw = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=1)
    g.get_scheduled_workouts = _save_sched
    swim_card = psw["days"][0]["fuel"][0]
    check("swim gets a fuel card (>=90 min)", psw["days"][0]["needs_fuel"])
    check("swim: no pre-fuel", swim_card["pre_carbs_g"] == 0)
    check("swim: no during-fuel", swim_card["during_carbs_g_total"] == 0)
    check("swim: post-fuel intact", swim_card["post_carbs_g"] > 0)

    print("modeled metabolic adaptation (NEAT discount from kg lost):")
    check("no loss -> no discount",
          g._modeled_adaptation_factor({"goal_type": "lose", "start_weight_kg": 77.0}, 77.0) == 1.0)
    check("3kg lost -> ~3.6% NEAT trim",
          abs(g._modeled_adaptation_factor({"goal_type": "lose", "start_weight_kg": 77.0}, 74.0) - 0.964) < 0.001)
    check("10kg lost -> 12% NEAT trim (Leibel-supported deep-cut cap)",
          abs(g._modeled_adaptation_factor({"goal_type": "lose", "start_weight_kg": 80.0}, 70.0) - 0.88) < 1e-9)
    check("discount is capped at 12% (15kg lost -> still 12%, not 18%)",
          abs(g._modeled_adaptation_factor({"goal_type": "lose", "start_weight_kg": 85.0}, 70.0) - 0.88) < 1e-9)
    check("maintain goal -> no discount",
          g._modeled_adaptation_factor({"goal_type": "maintain", "start_weight_kg": 77.0}, 74.0) == 1.0)
    check("no start weight -> no discount",
          g._modeled_adaptation_factor({"goal_type": "lose"}, 74.0) == 1.0)
    # In-plan: with weight below start and no weigh-in trend to calibrate, the
    # modeled discount applies and is disclosed; EA detail is exposed per day.
    _tgt_adapt = (TODAY + timedelta(weeks=8)).isoformat()
    g.set_fueling_goal(goal_type="lose", target_weight_kg=70.0, target_date=_tgt_adapt,
                       sex="male", height_cm=178, age=40, start_weight_kg=80.0)
    plan_adapt = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=3)
    check("plan applies modeled adaptation to the energy base",
          "modeled_adaptation" in plan_adapt["energy_base"]["source"])
    check("modeled-adaptation note surfaced",
          any("metabolic adaptation" in n.lower() for n in plan_adapt["notes"]))
    check("EA detail exposed with intake/exercise/ffm components",
          all(set(("intake_kcal", "exercise_kcal", "ffm_kg")).issubset(
              (d.get("energy_availability_detail") or {})) for d in plan_adapt["days"]))
    check("EA detail reconciles: (intake - exercise)/ffm == reported EA",
          all(abs(round((d["energy_availability_detail"]["intake_kcal"]
                         - d["energy_availability_detail"]["exercise_kcal"])
                        / d["energy_availability_detail"]["ffm_kg"], 1)
                  - d["energy_availability_kcal_per_kg_ffm"]) <= 0.1
              for d in plan_adapt["days"]))
    # restore the default goal
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40)

    print("adaptive TDEE:")
    _save_nt, _save_air = g.nutrition_trend, g.get_activities_in_range
    g.nutrition_trend = lambda weeks=4: {
        "weeks": [{"avg_daily_kcal_intake": 2500, "days_logged": 6} for _ in range(6)],
        "weight_trajectory": {"delta_kg": -1.0, "readings_count": 10}}
    g.get_activities_in_range = lambda s, e, *a, **k: [{"calories": 700}] * 30
    at = g.get_adaptive_tdee(weeks=6)
    check("maintenance = intake - weight-change energy (~2683)",
          abs(at["total_maintenance_kcal"] - 2683) <= 3)
    check("non-exercise base strips mean exercise burn",
          at["non_exercise_base_kcal"] == at["total_maintenance_kcal"] - at["mean_daily_exercise_kcal"])
    check("confidence high with good logging", at["confidence"] == "high")
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40, use_adaptive_tdee=True)
    plan_at = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)
    check("plan adopts measured base", plan_at["energy_base"]["source"].startswith("adaptive_tdee"))
    check("TEF not double-counted on measured maintenance",
          all(d["tef_kcal"] == 0 for d in plan_at["days"]))
    g.nutrition_trend, g.get_activities_in_range = _save_nt, _save_air
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40)

    print("race fueling calculator:")
    rf = g.get_race_fueling("cycling", 5.0, weight_kg=77.0)
    check("long race -> 90 g carbs/hr", rf["during"]["carbs_g_per_hr"] == 90)
    check("during total scales with duration", rf["during"]["carbs_g_total"] == 450)
    check("gels equivalent computed", rf["during"]["gels_equiv"] > 0)
    check("carb-load protocol for long event", "carb_load" in rf)
    check("pre-race caffeine ~3 mg/kg", rf["caffeine"]["pre_mg"] == round(77 * 3))
    rf_short = g.get_race_fueling("running", 0.75, weight_kg=77.0)
    check("sub-hour race: no during carbs", rf_short["during"]["carbs_g_per_hr"] == 0)
    check("sub-90min: no carb load", "carb_load" not in rf_short)

    print("per-day meal split:")
    d0 = plan["days"][0]
    tot_p = sum(m["protein_g"] for m in d0["meals"])
    check("meals present", len(d0["meals"]) >= 4)
    check("meal protein sums ~ day protein", abs(tot_p - d0["protein_g"]) <= 3)
    d4m = plan["days"][4]  # long-ride day, carries a fuel card
    check("meal carbs sum ~ day carbs", abs(sum(m["carbs_g"] for m in d4m["meals"]) - d4m["carbs_g"]) <= 3)
    wfuel = next((m for m in d4m["meals"] if m["meal"].startswith("Workout fuel")), None)
    fuel_total_c = sum(c["pre_carbs_g"] + c["during_carbs_g_total"] + c["post_carbs_g"]
                       for c in d4m["fuel"])
    fuel_total_p = sum(c["post_protein_g"] for c in d4m["fuel"])
    check("workout-fuel meal present on a fueled day", wfuel is not None)
    check("workout-fuel carbs match the cards (pre+during+post)",
          wfuel["carbs_g"] == min(fuel_total_c, d4m["carbs_g"]))
    check("workout-fuel protein matches the cards' post protein",
          wfuel["protein_g"] == min(fuel_total_p, d4m["protein_g"]))
    check("meal kcal sums ~ day target",
          all(abs(sum(m["kcal"] for m in d["meals"]) - d["target_kcal"]) <= 30
              for d in plan["days"]))
    check("breakfast never balloons past its cap (~650 kcal)",
          all(next((m["kcal"] for m in d["meals"] if m["meal"] == "Breakfast"), 0) <= 680
              for d in plan["days"]))

    print("weekday breakfast-skip (time-restricted eating):")
    plan_sb = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7,
                                      skip_breakfast_weekdays=True)
    check("config echoes skip_breakfast_weekdays",
          plan_sb["config"]["skip_breakfast_weekdays"] is True)
    check("weekdays drop breakfast, weekends keep it",
          all((not any(m["meal"] == "Breakfast" for m in d["meals"]))
              == (date.fromisoformat(d["date"]).weekday() < 5)
              for d in plan_sb["days"]))
    check("skip-breakfast day still sums carbs to the day total",
          all(abs(sum(m["carbs_g"] for m in d["meals"]) - d["carbs_g"]) <= 3 for d in plan_sb["days"]))
    check("off by default: every day keeps breakfast",
          all(any(m["meal"] == "Breakfast" for m in d["meals"]) for d in plan["days"]))

    print("heat-aware hydration (outdoor sessions):")
    hot = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, heat_c=33)
    long_ride = hot["days"][4]["fuel"][0]  # outdoor 3.5h ride
    check("hot day flags heat on the card", long_ride.get("heat_c") == 33)
    check("hot day bumps sodium", long_ride["sodium_mg_per_hr"] > 600)
    cool = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, heat_c=18)
    check("cool day: no heat bump", cool["days"][4]["fuel"][0].get("heat_c") is None)

    print("rebalance from actuals:")
    def _fake_pva(days_back=7):
        return {"rows": [
            {"date": (TODAY - timedelta(days=2)).isoformat(), "foods_logged": 5,
             "actual_kcal": 3000, "adjusted_target_kcal": 2600},
            {"date": (TODAY - timedelta(days=1)).isoformat(), "foods_logged": 4,
             "actual_kcal": 2900, "adjusted_target_kcal": 2500},
            {"date": TODAY.isoformat(), "foods_logged": 1,
             "actual_kcal": 500, "adjusted_target_kcal": 2600},  # in-progress: ignored
            {"date": (TODAY - timedelta(days=3)).isoformat(), "foods_logged": 0,
             "actual_kcal": None, "adjusted_target_kcal": 2600},  # unlogged: ignored
        ]}
    g.nutrition_plan_vs_actual = _fake_pva
    base_adj = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)["daily_kcal_adjustment"]
    plan_rb = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, rebalance=3)
    # +800 over across 2 logged days -> ~114/day tighter over 7 days
    check("overeating tightens the window",
          abs(plan_rb["daily_kcal_adjustment"] - (base_adj - 800 / 7)) <= 1)
    check("rebalance note names 2 logged days",
          any("Rebalanced from the last 2 logged" in n for n in plan_rb["notes"]))
    check("rebalance off by default: adjustment unchanged",
          g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)["daily_kcal_adjustment"] == base_adj)
    # Adjustment breakdown surfaces the rebalance component in the target math.
    check("top-level breakdown splits base vs rebalance",
          plan_rb["adjustment_breakdown"]["base_deficit_kcal"] == base_adj
          and plan_rb["adjustment_breakdown"]["rebalance_kcal"] == round(-800 / 7))
    check("rebalance detail names the drift",
          plan_rb["rebalance"] and plan_rb["rebalance"]["logged_days"] == 2
          and plan_rb["rebalance"]["net_drift_kcal"] == 800)
    check("per-day rows carry the rebalance breakdown",
          all("rebalance_kcal" in d["adjustment_breakdown"] for d in plan_rb["days"]))

    print("deficit-only rebalance (never give calories back):")
    # Undereating: two-way rebalance would RAISE targets; deficit-only suppresses it.
    def _under_pva(days_back=7):
        return {"rows": [
            {"date": (TODAY - timedelta(days=2)).isoformat(), "foods_logged": 5,
             "actual_kcal": 2000, "adjusted_target_kcal": 2600},
            {"date": (TODAY - timedelta(days=1)).isoformat(), "foods_logged": 4,
             "actual_kcal": 2100, "adjusted_target_kcal": 2500},
        ]}
    g.nutrition_plan_vs_actual = _under_pva
    two_way = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, rebalance=3)
    check("two-way rebalance loosens after undereating",
          two_way["daily_kcal_adjustment"] > base_adj)
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178.0, age=40,
                       rebalance_deficit_only=True)
    d_only = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, rebalance=3)
    check("deficit-only holds the deficit when undereating",
          d_only["daily_kcal_adjustment"] == base_adj
          and d_only["adjustment_breakdown"]["rebalance_kcal"] == 0)
    check("deficit-only surfaces a 'deficit stands' note",
          any("deficit stands" in n for n in d_only["notes"]))
    # But deficit-only STILL tightens when overeating.
    g.nutrition_plan_vs_actual = _fake_pva
    d_over = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, rebalance=3)
    check("deficit-only still tightens on overeating",
          abs(d_over["daily_kcal_adjustment"] - (base_adj - 800 / 7)) <= 1)
    # Restore a two-way goal for later tests.
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178.0, age=40)

    print("logging suggestions from past days:")
    def _sugg_pva(days_back=7):
        # 4 logged days: consistently over the adjusted target and short on protein.
        return {"rows": [
            {"date": (TODAY - timedelta(days=i)).isoformat(), "foods_logged": 6,
             "actual_kcal": 2900, "adjusted_target_kcal": 2500,
             "delta_kcal_vs_adjusted": 400, "actual_p": 110}
            for i in range(4, 0, -1)
        ] + [
            {"date": TODAY.isoformat(), "foods_logged": 1, "actual_kcal": 300,
             "adjusted_target_kcal": 2500, "delta_kcal_vs_adjusted": -2200,
             "actual_p": 20},  # in-progress today: excluded
        ]}
    g.nutrition_plan_vs_actual = _sugg_pva
    sugg = g._logging_suggestions(protein_target_g=160)
    check("flags chronic overeating vs adjusted target",
          any("over your adjusted target" in s for s in sugg))
    check("flags protein routinely short",
          any("Protein has averaged" in s for s in sugg))
    check("today's in-progress day excluded from suggestions",
          not any("2200" in s for s in sugg))

    def _gap_pva(days_back=7):
        # Mostly unlogged week -> logging-gap flag.
        return {"rows": [
            {"date": (TODAY - timedelta(days=i)).isoformat(),
             "foods_logged": (6 if i == 1 else 0),
             "actual_kcal": (2400 if i == 1 else None),
             "adjusted_target_kcal": 2500,
             "delta_kcal_vs_adjusted": (-100 if i == 1 else None),
             "actual_p": (150 if i == 1 else None)}
            for i in range(5, 0, -1)
        ]}
    g.nutrition_plan_vs_actual = _gap_pva
    gap = g._logging_suggestions(protein_target_g=160)
    check("flags a logging gap", any("Logging gap" in s for s in gap))

    def _nolog_pva(days_back=7):
        return {"rows": [
            {"date": (TODAY - timedelta(days=i)).isoformat(), "foods_logged": 0,
             "actual_kcal": None, "adjusted_target_kcal": 2500}
            for i in range(4, 0, -1)
        ]}
    g.nutrition_plan_vs_actual = _nolog_pva
    nolog = g._logging_suggestions(protein_target_g=160)
    check("nothing logged -> single log-your-meals nudge",
          len(nolog) == 1 and "No food logged" in nolog[0])

    def _ontrack_pva(days_back=7):
        return {"rows": [
            {"date": (TODAY - timedelta(days=i)).isoformat(), "foods_logged": 6,
             "actual_kcal": 2480, "adjusted_target_kcal": 2500,
             "delta_kcal_vs_adjusted": -20, "actual_p": 165}
            for i in range(4, 0, -1)
        ]}
    g.nutrition_plan_vs_actual = _ontrack_pva
    check("on-track logging -> no nagging suggestions",
          g._logging_suggestions(protein_target_g=160) == [])
    g.nutrition_plan_vs_actual = _fake_pva  # restore for any later use

    print("garmin push (experimental, offline):")
    class _FailClient:
        def connectapi(self, *a, **k):
            raise RuntimeError("offline test")
    g.get_client = lambda: _FailClient()
    pushed = g.push_nutrition_targets_to_garmin(target_date=TODAY.isoformat(), days=1)
    r0 = pushed["results"][0]
    check("offline push fails with attempts logged",
          r0["status"] == "failed" and len(r0["attempts"]) == 3)
    check("payload carried the plan's target", r0["targets"]["calories"] > 0)
    far = g.push_nutrition_targets_to_garmin(
        target_date=(TODAY + timedelta(days=30)).isoformat(), days=1)
    check("no-plan date reported cleanly", far["results"][0]["status"] == "no_plan_for_date")

    print("weight_x22 fallback when sex/height/age AND body-fat missing:")
    _bc = g.get_body_composition
    g.get_body_composition = lambda startdate=None, enddate=None, **k: {"dateWeightList": [
        {"date": (TODAY - timedelta(days=1)).isoformat(), "weight": 74100}]}  # no bodyFat
    g.refresh_weigh_in_snapshot()   # readers use the stable snapshot
    g.set_fueling_goal(goal_type="maintain")
    plan3 = g.generate_fueling_plan(days=3)
    g.get_body_composition = _bc
    g.refresh_weigh_in_snapshot()   # restore default fixture for later tests
    check("bmr fallback source", plan3["bmr"]["source"] == "weight_x22_fallback")
    check("maintain -> no deficit", plan3["daily_kcal_adjustment"] == 0)

    print("skip_scheduled_session excludes a session from the plan:")
    g.set_fueling_goal(goal_type="lose", target_weight_kg=72.0, target_date=target_date,
                       sex="male", height_cm=178, age=40)
    before = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=1)
    check("baseline: today's Bike Threshold is scheduled",
          any(s["sport"] == "cycling" for s in before["days"][0]["sessions"]))
    skip_res = g.skip_scheduled_session(date=TODAY.isoformat(), sport="cycling")
    check("skip_scheduled_session reports saved", skip_res.get("saved") is True)
    check("get_skipped_sessions lists the active skip", len(g.get_skipped_sessions()) == 1)
    after = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=1)
    check("skipped sport no longer appears in today's sessions",
          not any(s["sport"] == "cycling" for s in after["days"][0]["sessions"]))
    check("day falls back to rest once its only session is skipped",
          after["days"][0]["primary_intensity"] == "rest")
    check("a note records the skip",
          any("skipped" in n.lower() for n in after["notes"]))
    # A skip dated in the past must not persist (auto-pruned on next read).
    g.skip_scheduled_session(date=(TODAY - timedelta(days=1)).isoformat(), sport="running")
    check("past-dated skips are pruned, not accumulated",
          all(s["date"] >= TODAY.isoformat() for s in g.get_skipped_sessions()))

    print("no goal -> no_goal_available:")
    _STORE.pop(_key("fueling_goal", ["current"]), None)
    plan4 = g.generate_fueling_plan(days=3)
    check("no_goal_available true", plan4.get("no_goal_available") is True)

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
