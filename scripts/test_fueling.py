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


cache.get = _fake_get          # type: ignore[assignment]
cache.put = _fake_put          # type: ignore[assignment]
cache.enabled = lambda: True   # type: ignore[assignment]

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

    print("get_fueling_goal:")
    gi = g.get_fueling_goal()
    check("goal returned", gi["goal"]["goal_type"] == "lose")
    check("weeks remaining ~6", gi["progress"]["weeks_remaining"] == 6)
    check("kg to target = 2.0", gi["progress"]["kg_to_target"] == 2.0)
    check("required daily change negative", gi["progress"]["required_daily_kcal_change"] < 0)

    print("generate_fueling_plan (Mifflin BMR):")
    plan = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)
    check("no error", "error" not in plan and not plan.get("no_goal_available"))
    check("bmr from Mifflin-St Jeor", plan["bmr"]["source"] == "mifflin_st_jeor")
    bmr = plan["bmr"]["value"]
    floor = round(bmr * 1.2)
    check("7 day rows", len(plan["days"]) == 7)
    check("all targets >= BMR*1.2 floor", all(d["target_kcal"] >= floor for d in plan["days"]))
    check("deficit capped at <=500", plan["daily_kcal_adjustment"] >= -500)

    day0 = plan["days"][0]   # threshold (75min)
    day1 = plan["days"][1]   # recovery
    day4 = plan["days"][4]   # long ride
    day2 = plan["days"][2]   # rest

    print("protein periodization (no longer static):")
    check("rest-day protein = base 1.9 g/kg", day2["protein_g"] == round(74.1 * 1.9))
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

    print("macro reconciliation:")
    for d in plan["days"]:
        kcal = d["protein_g"] * 4 + d["carbs_g"] * 4 + d["fat_g"] * 9
        check(f"  {d['date']} P/C/F sums within 8% of target",
              abs(kcal - d["target_kcal"]) <= 0.08 * d["target_kcal"] + 60)

    print("deficit periodization (default for lose goals):")
    check("config says periodized", plan["config"]["periodize_deficit"] is True)
    check("rest day clamps at the floor", day2["target_kcal"] == round(bmr * 1.2))
    check("hard day never cut deeper than flat",
          abs(day0["kcal_adjustment"]) <= abs(plan["daily_kcal_adjustment"]) + 1)
    check("shortfall note fires when floors bind",
          any("could not absorb" in n for n in plan["notes"]))
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
    check("shortfall note projects a landing date",
          any("lands ~" in n for n in plan_eta["notes"]))
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

    print("weight_x22 fallback when sex/height/age missing:")
    g.set_fueling_goal(goal_type="maintain")
    plan3 = g.generate_fueling_plan(days=3)
    check("bmr fallback source", plan3["bmr"]["source"] == "weight_x22_fallback")
    check("maintain -> no deficit", plan3["daily_kcal_adjustment"] == 0)

    print("no goal -> no_goal_available:")
    _STORE.pop(_key("fueling_goal", ["current"]), None)
    plan4 = g.generate_fueling_plan(days=3)
    check("no_goal_available true", plan4.get("no_goal_available") is True)

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
