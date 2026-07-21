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
    check("protein = weight*1.9", all(d["protein_g"] == round(74.1 * 1.9) for d in plan["days"]))
    check("deficit capped at <=500", plan["daily_kcal_adjustment"] >= -500)

    day0 = plan["days"][0]   # threshold
    day1 = plan["days"][1]   # recovery
    day4 = plan["days"][4]   # long ride
    day2 = plan["days"][2]   # rest
    check("threshold day has a fuel card", day0["needs_fuel"] and len(day0["fuel"]) == 1)
    check("threshold carbs = 7.5 g/kg", day0["carb_g_per_kg"] == 7.5)
    check("rest day has no fuel card", not day2["needs_fuel"])
    check("recovery short day: no fuel card", not day1["needs_fuel"])
    check("long ride flagged for fuel", day4["needs_fuel"])
    check("long ride carbs (7 g/kg) > recovery day carbs", day4["carbs_g"] > day1["carbs_g"])
    check("long ride burn > recovery burn", day4["est_burn_kcal"] > day1["est_burn_kcal"])
    check("burn calibrated from history", day0["sessions"][0]["burn_source"] == "history")
    card = day0["fuel"][0]
    check("threshold card has caffeine", card.get("caffeine_mg") == round(74.1 * 3))

    print("macro reconciliation:")
    for d in plan["days"]:
        kcal = d["protein_g"] * 4 + d["carbs_g"] * 4 + d["fat_g"] * 9
        check(f"  {d['date']} P/C/F sums within 8% of target",
              abs(kcal - d["target_kcal"]) <= 0.08 * d["target_kcal"] + 60)

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
