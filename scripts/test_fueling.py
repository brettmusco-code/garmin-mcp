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

    print("get_fueling_goal:")
    gi = g.get_fueling_goal()
    check("goal returned", gi["goal"]["goal_type"] == "lose")
    check("weeks remaining ~6", gi["progress"]["weeks_remaining"] == 6)
    check("kg to target = 2.0", gi["progress"]["kg_to_target"] == 2.0)
    check("required daily change negative", gi["progress"]["required_daily_kcal_change"] < 0)

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
    bmr = plan["bmr"]["value"]
    floor = round(bmr * 1.2)
    check("7 day rows", len(plan["days"]) == 7)
    check("all targets >= BMR*1.2 floor", all(d["target_kcal"] >= floor for d in plan["days"]))
    check("deficit capped at <=500", plan["daily_kcal_adjustment"] >= -500)
    check("energy base = RMR x NEAT (below old x1.3)",
          plan["energy_base"]["value"] == round(bmr * 1.15))
    check("net exercise is below gross on a training day",
          plan["days"][4]["net_exercise_kcal"] < plan["days"][4]["est_burn_kcal"])
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
    plan_flat = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7,
                                        periodize_deficit=False)
    check("flat override: every day same adjustment",
          len({d["kcal_adjustment"] for d in plan_flat["days"]}) == 1)
    check("flat override echoed", plan_flat["config"]["periodize_deficit"] is False)

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
    check("default plan raises the low-EA note",
          any("Low energy availability" in n for n in plan["notes"]))
    plan_ea = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, ea_floor=20)
    check("config echoes ea_floor=20", plan_ea["config"]["ea_floor_kcal_per_kg_ffm"] == 20)
    check("lower EA floor suppresses the note",
          not any("Low energy availability" in n for n in plan_ea["notes"]))

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
    near = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, front_load=0.5)
    flat_near = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7, front_load=0)
    check("near target, front-loaded deficit eases below linear",
          abs(near["daily_kcal_adjustment"]) < abs(flat_near["daily_kcal_adjustment"]))
    check("front-load reflected in config", front["config"]["front_load"] == 0.5)
    # restore stubs + default goal
    g.get_athlete_baseline = _fake_baseline
    g.get_body_composition = _fake_body_comp
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
    g.set_fueling_goal(goal_type="maintain")
    plan3 = g.generate_fueling_plan(days=3)
    g.get_body_composition = _bc
    check("bmr fallback source", plan3["bmr"]["source"] == "weight_x22_fallback")
    check("maintain -> no deficit", plan3["daily_kcal_adjustment"] == 0)

    print("no goal -> no_goal_available:")
    _STORE.pop(_key("fueling_goal", ["current"]), None)
    plan4 = g.generate_fueling_plan(days=3)
    check("no_goal_available true", plan4.get("no_goal_available") is True)

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
