#!/usr/bin/env python3
"""Offline smoke test for the race calendar and its effect on the fueling plan.

Same shape as scripts/test_fueling.py — stubs the Garmin fetchers and swaps the
S3 cache for an in-memory dict, so the phase model, the duration estimates and
the plan integration all run with no credentials and no network.

Run:  python scripts/test_races.py
"""
from __future__ import annotations

import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Stub garminconnect so app.garmin imports without the Garmin/curl_cffi stack.
if "garminconnect" not in sys.modules:
    _fake = types.ModuleType("garminconnect")
    _fake.Garmin = type("Garmin", (), {"__init__": lambda self, *a, **k: None})
    _fake_client = types.ModuleType("garminconnect.client")

    class _ClientMeta(type):
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

from app import cache, garmin as g, races as R  # noqa: E402

# --- in-memory cache ---------------------------------------------------------
_STORE: dict[str, object] = {}


def _key(tool, key_parts):
    return f"{tool}/{'/'.join(str(p) for p in (key_parts or []))}"


cache.get = lambda tool, args, ttl_seconds=None, raise_on_error=False, key_parts=None: \
    _STORE.get(_key(tool, key_parts))            # type: ignore[assignment]
cache.put = lambda tool, args, data, raise_on_error=False, key_parts=None: \
    _STORE.__setitem__(_key(tool, key_parts), data)   # type: ignore[assignment]
cache.enabled = lambda: True                     # type: ignore[assignment]

TODAY = date.today()
FAILURES: list[str] = []


def check(label, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


# --- stub Garmin fetchers ----------------------------------------------------
# A 3:30 marathoner: the predictions below are what Garmin would serve, and the
# duration estimates are checked against them.
_PREDICTIONS = {
    "time5K": 22 * 60,
    "time10K": 46 * 60,
    "timeHalfMarathon": 101 * 60,
    "timeMarathon": 210 * 60,
}


def _fake_activities(startdate, enddate, *a, **k):
    return [
        {"activityType": {"typeKey": "cycling"}, "activityName": "ride",
         "duration": 3600, "movingDuration": 3600, "distance": 30000, "calories": 700}
        for _ in range(6)
    ]


def _fake_scheduled(startdate, enddate, **k):
    """A 90-min ride every day. The athlete needs real training on the books:
    with an untrained schedule every target lands on the BMR x 1.2 floor, so
    there is no deficit left for a taper to soften and the integration tests
    can't tell the race logic from the floor."""
    base = date.fromisoformat(startdate)
    return [
        {"date": (base + timedelta(days=i)).isoformat(), "itemType": "workout",
         "title": "Aerobic ride", "sportTypeKey": "cycling", "duration": 90 * 60}
        for i in range(21)
    ]


g.get_athlete_baseline = lambda *a, **k: {"weight_kg": 74.0}          # type: ignore[assignment]
g.get_body_composition = lambda startdate=None, enddate=None, **k: {  # type: ignore[assignment]
    "dateWeightList": [{"date": (TODAY - timedelta(days=1)).isoformat(),
                        "weight": 74000, "bodyFat": 14.0}]}
g.get_race_predictions = lambda *a, **k: dict(_PREDICTIONS)           # type: ignore[assignment]
g.get_activities_in_range = _fake_activities                          # type: ignore[assignment]
g.get_scheduled_workouts = _fake_scheduled                            # type: ignore[assignment]

# Pin "now" to local noon so _local_today() agrees with TODAY regardless of the
# UTC hour the suite runs at — otherwise days_until is off by one for part of
# the day, and an evening run trips the no-show cutoff on today's session.
g._local_now = lambda: datetime(TODAY.year, TODAY.month, TODAY.day, 12, 0,
                                tzinfo=ZoneInfo("America/New_York"))


def test_distances():
    print("distance presets and normalisation:")
    sp, km, label, legs = R.resolve_distance(distance_label="marathon")
    check("marathon preset -> running / 42.195 km", (sp, round(km, 3), label) == ("running", 42.195, "marathon"))
    check("marathon has no legs", legs is None)

    sp, km, label, legs = R.resolve_distance(distance_label="70.3")
    check("'70.3' slugs to the half-iron preset", (sp, label) == ("triathlon", "703"))
    check("70.3 total distance ~113 km", abs(km - 113.0) < 0.01)
    check("70.3 carries swim/bike/run legs", legs == (1.9, 90.0, 21.1))

    sp, km, _, _ = R.resolve_distance(sport="cycling", distance=100, unit="mi")
    check("100 mi -> 160.9 km", abs(km - 160.934) < 0.01)
    check("explicit sport preserved on a raw distance", sp == "cycling")

    _, km, label, _ = R.resolve_distance(sport="running", distance=42.2, unit="km")
    check("a raw 42.2 km run is named 'marathon'", label == "marathon")

    _, km, _, _ = R.resolve_distance(sport="swimming", distance=1500, unit="m")
    check("metres convert to km", abs(km - 1.5) < 1e-9)

    for bad in (
        lambda: R.resolve_distance(sport="running"),                       # no distance
        lambda: R.resolve_distance(sport="curling", distance=5),           # bad sport
        lambda: R.resolve_distance(sport="running", distance=5, unit="furlongs"),
        lambda: R.resolve_distance(sport="running", distance=-5),
    ):
        try:
            bad()
            check("invalid distance input rejected", False)
        except ValueError:
            check("invalid distance input rejected", True)


def test_duration_estimates():
    print("duration estimation:")
    hrs, src = R.estimate_duration_hours("running", 42.195, race_predictions=_PREDICTIONS)
    check("marathon reads its own prediction (3.5 h)", abs(hrs - 3.5) < 0.02)
    check("source names the prediction", src == "race_predictions")

    hrs, _ = R.estimate_duration_hours("running", 21.0975, race_predictions=_PREDICTIONS)
    check("half reads its own prediction (1.68 h)", abs(hrs - 101 / 60) < 0.02)

    # 30k has no anchor — Riegel-scaled off the nearest one (the half).
    hrs30, _ = R.estimate_duration_hours("running", 30.0, race_predictions=_PREDICTIONS)
    check("30k lands between the half and the marathon", 1.68 < hrs30 < 3.5)

    # 50 miles: past the marathon the steeper ultra exponent must apply, so the
    # estimate has to beat a naive linear-in-distance scaling of marathon pace.
    hrs50, _ = R.estimate_duration_hours("running", 80.467, race_predictions=_PREDICTIONS)
    check("ultra fades harder than marathon pace extended",
          hrs50 > 3.5 * (80.467 / 42.195))

    hrs_c, src_c = R.estimate_duration_hours("cycling", 160.934, speed_mps={"cycling": 8.0})
    check("century off measured speed (~5.6 h)", abs(hrs_c - 160934 / 8.0 / 3600) < 0.02)
    check("source names the history pace", src_c == "history_pace")

    hrs_d, src_d = R.estimate_duration_hours("cycling", 100.0)
    check("no data at all still returns an estimate", hrs_d > 0)
    check("...and admits it used a default", src_d == "default_pace")

    tri, src_t = R.estimate_duration_hours(
        "triathlon", 113.0, legs_km=(1.9, 90.0, 21.1),
        race_predictions=_PREDICTIONS, speed_mps={"cycling": 8.0, "swimming": 1.0},
    )
    check("70.3 estimate is plausible (4-8 h)", 4.0 < tri < 8.0)
    check("tri estimate credits the run prediction", src_t == "race_predictions")


def test_phases():
    print("phase model:")
    # Loading is short and flat by design: maximal glycogen is reached within
    # 24 h at 10 g/kg and holds, and 8 g/kg is indistinguishable from 6 — so a
    # 3-day 8/10/10 ramp spends two extra surplus days for no extra glycogen.
    check("sub-hour event earns no loading", R.load_days_for(0.75) == 0)
    check("nothing under ~90 min loads at all", R.load_days_for(1.4) == 0)
    check("90 min -> 1 loading day", R.load_days_for(1.5) == 1)
    check("3 h+ -> 2 loading days", R.load_days_for(3.5) == 2)

    # An A-priority parkrun still gets a taper — priority, not distance, is what
    # says "this one matters". What it must not get is a glycogen load, which a
    # 45-minute effort cannot use.
    short_a = R.phase_for(1, 0.75, "A")
    check("a 45-min A race tapers but does not load", short_a["phase"] == "taper")
    check("a 45-min C race is left entirely alone",
          R.phase_for(1, 0.75, "C") is None)

    p = R.phase_for(0, 3.5)
    check("race day is a phase", p["phase"] == "race_day")
    check("race day suspends the deficit", p["deficit_multiplier"] == 0.0)

    d1 = R.phase_for(1, 3.5)
    d2 = R.phase_for(2, 3.5)
    d3 = R.phase_for(3, 3.5)
    check("day before a marathon is a carb load", d1["phase"] == "carb_load")
    check("loading suspends the deficit", d1["deficit_multiplier"] == 0.0)
    check("both loading days sit at the 10 g/kg dose",
          d1["carb_g_per_kg"] == d2["carb_g_per_kg"] == 10.0)
    check("no ramp-in rung at 8 g/kg", d3["phase"] != "carb_load")
    check("loading days need no EA floor (deficit already off)",
          d1["ea_floor"] is None)

    check("a 2 h event loads for 1 day, not 2",
          R.phase_for(2, 2.0)["phase"] == "taper"
          and R.phase_for(1, 2.0)["phase"] == "carb_load")

    t = R.phase_for(5, 3.5, "A")
    check("A race tapers 5 days out", t["phase"] == "taper")
    check("taper softens rather than suspends", 0 < t["deficit_multiplier"] < 1)
    check("taper carries an EA floor", t["ea_floor"] == R.TAPER_EA_FLOOR)
    check("post-race carries one too", R.phase_for(-2, 3.5)["ea_floor"] == R.TAPER_EA_FLOOR)
    check("phases with the deficit already off carry none",
          R.phase_for(0, 3.5)["ea_floor"] is None
          and R.phase_for(-1, 3.5)["ea_floor"] is None)
    check("B race does not taper 5 days out", R.phase_for(5, 3.5, "B") is None)
    check("C race has no taper at all", R.phase_for(4, 3.5, "C") is None)
    check("C race still loads", R.phase_for(1, 3.5, "C")["phase"] == "carb_load")

    # Taper length is set by DURATION and merely capped by priority. Keying it
    # off priority alone gave a 78-minute sprint the same 7-day easing as a
    # 10-hour Ironman, which on an aggressive cut spends a week of deficit on a
    # race that needs a couple of easy days.
    check("a sprint tapers for 2 days even at priority A",
          R.taper_days_for(1.3, "A") == 2)
    check("a marathon still gets its full week at A",
          R.taper_days_for(3.5, "A") == 7)
    check("an ironman doesn't get more than a marathon",
          R.taper_days_for(10.6, "A") == 7)
    check("priority still caps a long race (B <= 4)",
          R.taper_days_for(3.5, "B") == 4 and R.taper_days_for(10.6, "B") == 4)
    check("priority can only shorten, never extend",
          all(R.taper_days_for(h, "B") <= R.taper_days_for(h, "A")
              for h in (0.4, 0.8, 1.3, 1.7, 3.5, 10.6)))
    check("C never tapers, whatever the duration",
          all(R.taper_days_for(h, "C") == 0 for h in (0.4, 1.3, 3.5, 10.6)))
    check("taper length rises monotonically with duration",
          [R.taper_days_for(h, "A") for h in (0.4, 0.8, 1.3, 1.7, 3.5, 10.6)]
          == sorted(R.taper_days_for(h, "A") for h in (0.4, 0.8, 1.3, 1.7, 3.5, 10.6)))
    check("4 days out from an A sprint is no longer a taper day",
          R.phase_for(4, 1.3, "A") is None)
    check("...while 4 days out from an A marathon still is",
          R.phase_for(4, 3.5, "A")["phase"] == "taper")
    check("the sprint's own taper day survives",
          R.phase_for(2, 1.3, "A")["phase"] == "taper")
    check("window_days reflects the shortened taper",
          R.window_days(1.3, "A")[0] == 2 and R.window_days(3.5, "A")[0] == 7)

    r1 = R.phase_for(-1, 3.5)
    check("day after is recovery", r1["phase"] == "recovery")
    check("recovery keeps the deficit off", r1["deficit_multiplier"] == 0.0)
    check("recovery bumps protein", r1["protein_bump"] > 0)
    check("marathon recovery runs 3 days", R.phase_for(-3, 3.5)["phase"] == "post_race")
    check("short race recovery is 1 day", R.phase_for(-2, 1.2) is None)
    check("nothing 30 days out", R.phase_for(30, 3.5) is None)

    # Loading tops out because glycogen supercompensation does — an Ironman and
    # a marathon load identically. Recovery is what has to keep scaling.
    check("loading tops out at 2 days for any duration",
          R.load_days_for(3.5) == R.load_days_for(12.0) == 2)
    check("an ironman recovers for a week, not 3 days",
          R.recovery_days_for(12.0) == 7 and R.recovery_days_for(3.5) == 3)
    check("a 6 h event sits between the two", R.recovery_days_for(6.0) == 5)
    check("ironman recovery still eased 5 days out",
          R.phase_for(-5, 12.0)["phase"] == "post_race")
    check("marathon recovery is over by then", R.phase_for(-5, 3.5) is None)


def test_store():
    print("race calendar store:")
    _STORE.clear()
    race_date = (TODAY + timedelta(days=30)).isoformat()
    saved = g.set_race(date=race_date, name="Chicago Marathon",
                       distance_label="marathon", priority="A")
    check("race saved", saved.get("saved") is True)
    r = saved["race"]
    check("sport inferred from the preset", r["sport"] == "running")
    check("duration estimated from Garmin predictions", abs(r["duration_hours"] - 3.5) < 0.05)
    check("estimate source recorded", r["duration_source"] == "race_predictions")
    check("loading days derived from duration", r["carb_load_days"] == 2)

    again = g.set_race(date=race_date, name="Chicago Marathon",
                       distance_label="marathon", target_time_hours=3.75)
    check("re-saving the same race updates rather than duplicates",
          len(again["races"]) == 1)
    check("explicit target time wins over the estimate",
          again["race"]["duration_hours"] == 3.75
          and again["race"]["duration_source"] == "user")

    # A is the setting that costs the most progress on a cut, so omitting the
    # field must not silently opt into it.
    dflt = g.set_race(date=(TODAY + timedelta(days=45)).isoformat(),
                      name="Unspecified", distance_label="10k")["race"]
    check("priority defaults to B, not A", dflt["priority"] == "B")
    g.delete_race(dflt["id"])

    g.set_race(date=(TODAY + timedelta(days=60)).isoformat(), name="Club 10k",
               distance_label="10k", priority="C")
    listed = g.get_races()
    check("both races listed", len(listed) == 2)
    check("listed in date order", listed[0]["date"] < listed[1]["date"])
    check("days_until computed", listed[0]["days_until"] == 30)

    check("get_race_fueling can read a stored race",
          g.get_race_fueling(race_id=r["id"], weight_kg=74.0)["duration_hours"] == 3.75)

    old = g.set_race(date=(TODAY - timedelta(days=200)).isoformat(),
                     name="Last year", distance_label="10k")
    check("a race past the retention window is pruned on the next read",
          all(x["id"] != old["race"]["id"] for x in g.get_races()))

    check("deleting a race removes it",
          g.delete_race(listed[1]["id"]).get("saved") is True
          and len(g.get_races()) == 1)
    check("deleting an unknown race is a clean refusal",
          g.delete_race("nope").get("saved") is False)

    try:
        g.set_race(date="not-a-date", distance_label="10k")
        check("bad date rejected", False)
    except ValueError:
        check("bad date rejected", True)
    try:
        g.set_race(date=race_date, distance_label="10k", priority="Z")
        check("bad priority rejected", False)
    except ValueError:
        check("bad priority rejected", True)


def _plan_days(days=14, start=None):
    return g.generate_fueling_plan(
        start_date=(start or TODAY).isoformat(), days=days)["days"]


def test_plan_integration():
    print("plan integration:")
    _STORE.clear()
    g.set_fueling_goal(goal_type="lose", target_weight_kg=70.0,
                       target_date=(TODAY + timedelta(weeks=12)).isoformat(),
                       sex="male", height_cm=178, age=40)

    baseline = _plan_days(14)
    check("no races -> no race field on any day",
          all(d.get("race") is None for d in baseline))
    check("no races -> a real deficit is being run",
          any(d["target_deficit_kcal"] > 100 for d in baseline))

    race_day = TODAY + timedelta(days=10)
    g.set_race(date=race_day.isoformat(), name="Autumn Marathon",
               distance_label="marathon", priority="A", target_time_hours=3.5)
    plan = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=14)
    by_date = {d["date"]: d for d in plan["days"]}
    rd = by_date[race_day.isoformat()]
    load1 = by_date[(race_day - timedelta(days=1)).isoformat()]
    load3 = by_date[(race_day - timedelta(days=3)).isoformat()]
    taper5 = by_date[(race_day - timedelta(days=5)).isoformat()]
    rec1 = by_date[(race_day + timedelta(days=1)).isoformat()]
    rec3 = by_date[(race_day + timedelta(days=3)).isoformat()]

    check("plan carries the race calendar", len(plan.get("races") or []) == 1)
    check("race day tagged", rd["race"]["phase"] == "race_day")
    check("loading days tagged", load1["race"]["phase"] == "carb_load")
    check("taper tagged", taper5["race"]["phase"] == "taper")
    check("recovery tagged", rec1["race"]["phase"] == "recovery")
    check("later recovery tagged", rec3["race"]["phase"] == "post_race")

    check("race injected as a session",
          any(s.get("race") for s in rd["sessions"]))
    check("race burn is substantial for a marathon",
          rd["est_burn_kcal"] > 1800)
    check("no deficit on race day", rd["target_deficit_kcal"] <= 0)
    check("no deficit while loading", load1["target_deficit_kcal"] <= 0)
    check("no deficit the day after", rec1["target_deficit_kcal"] <= 0)
    # The taper eases the cut; how far depends on which constraint binds. The
    # multiplier alone would leave a proportional deficit, but the EA floor
    # takes over on an aggressive cut and can remove it entirely (never
    # exceeding maintenance — a surplus is for loading days, not the taper).
    check("taper eases the cut",
          taper5["target_deficit_kcal"] < max(
              d["target_deficit_kcal"] for d in baseline))
    check("taper never forces a surplus",
          taper5["target_deficit_kcal"] >= 0)
    check("taper lifts energy availability",
          taper5["energy_availability_kcal_per_kg_ffm"]
          > baseline[0]["energy_availability_kcal_per_kg_ffm"])

    check("loading carbs hit 10 g/kg", load1["carb_g_per_kg"] >= 10.0)
    check("carbs ramp toward the race",
          load1["carb_g_per_kg"] > load3["carb_g_per_kg"])
    # The directive is worthless if _fill_macros trims it back to fit a
    # maintenance-sized target — which is exactly what happened before the
    # loading days got a floor sized to hold their own macros. The ramp has to
    # show up in GRAMS, not just in the g/kg label.
    check("the ramp moves actual grams, not just the label",
          load1["carbs_g"] > load3["carbs_g"])
    check("loading days deliver the prescribed carbs (within rounding)",
          abs(load1["carbs_g"] - load1["carb_g_per_kg"] * plan["bmr"]["weight_kg"]) <= 2)
    check("carbs are never flagged trimmed while loading",
          not load1["carbs_trimmed"] and not load3["carbs_trimmed"])
    check("a real load runs above maintenance",
          load1["target_kcal"] > load1["expected_expenditure_kcal"])
    check("...and the plan says so",
          any("ABOVE maintenance" in n for n in plan["notes"]))
    check("race-day carbs exceed a normal training day",
          rd["carb_g_per_kg"] > max(d["carb_g_per_kg"] for d in baseline))
    check("recovery carbs raised", rec1["carb_g_per_kg"] >= 6.0)
    check("recovery protein bumped",
          rec1["protein_g_per_kg"] > baseline[0]["protein_g_per_kg"])

    race_card = next((c for c in rd["fuel"] if c.get("race")), None)
    check("race day gets its own fuel card", race_card is not None)
    check("race card uses race carb rates (>=60 g/hr)",
          race_card["during_carbs_g_per_hr"] >= 60)
    check("race card beats the conservative training rates",
          race_card["during_carbs_g_per_hr"]
          > max(c["during_carbs_g_per_hr"] for c in baseline[0]["fuel"] or [{"during_carbs_g_per_hr": 0}]))
    check("race card carries caffeine", race_card["caffeine_mg"] > 0)
    check("race-day macros can afford the fuel card",
          not rd["carbs_trimmed"])
    check("race phases surfaced in the notes",
          any("Autumn Marathon" in n for n in plan["notes"]))

    print("race-day session is not double-counted:")
    g.get_scheduled_workouts = lambda startdate, enddate, **k: [  # type: ignore[assignment]
        {"date": race_day.isoformat(), "itemType": "workout",
         "title": "Autumn Marathon", "sportTypeKey": "running",
         "duration": int(3.5 * 3600)},
    ]
    rd2 = {d["date"]: d for d in _plan_days(14)}[race_day.isoformat()]
    check("an already-scheduled race is not injected twice",
          len([s for s in rd2["sessions"] if s["hours"] > 2]) == 1)

    # ...but an unrelated training session of the same sport must NOT suppress
    # the race. The first heuristic here ("same sport, at least half as long")
    # matched the athlete's routine 90-min ride against a sprint triathlon and
    # dropped the race entirely, leaving race day planned as a normal Tuesday.
    g.get_scheduled_workouts = _fake_scheduled   # back to the daily 90-min ride
    _STORE.pop("fueling_races/current", None)
    g.set_race(date=race_day.isoformat(), name="Cove Sprint Tri",
               distance_label="sprint", priority="A", target_time_hours=1.27)
    tri_days = {d["date"]: d for d in _plan_days(14)}
    tri = tri_days[race_day.isoformat()]
    ordinary = tri_days[(race_day - timedelta(days=6)).isoformat()]
    check("a training ride does not stand in for a sprint triathlon",
          any(s.get("race") for s in tri["sessions"]))
    check("the triathlon's burn lands on top of the ride's",
          tri["est_burn_kcal"] > ordinary["est_burn_kcal"])
    check("a short tri still gets on-course fuel",
          next(c for c in tri["fuel"] if c.get("race"))["during_carbs_g_per_hr"] > 0)

    _STORE.pop("fueling_races/current", None)
    g.set_race(date=race_day.isoformat(), name="Autumn Marathon",
               distance_label="marathon", priority="A", target_time_hours=3.5)

    print("breakfast-skip yields to loading:")
    g.update_fueling_goal(skip_breakfast_weekdays=True)
    days = {d["date"]: d for d in _plan_days(14)}
    check("never skip breakfast on race morning",
          days[race_day.isoformat()]["skip_breakfast"] is False)
    check("never skip breakfast while loading",
          days[(race_day - timedelta(days=1)).isoformat()]["skip_breakfast"] is False)
    g.update_fueling_goal(skip_breakfast_weekdays=False)

    print("a short race doesn't spend a week of deficit:")
    # The regression this guards: a 78-minute sprint at priority A used to be
    # given a marathon's 7-day taper, halving the deficit from a full week out.
    _STORE.pop("fueling_races/current", None)
    g.set_race(date=race_day.isoformat(), name="Cove Sprint Tri",
               distance_label="sprint", priority="A", target_time_hours=1.3)
    sp = {d["date"]: d for d in _plan_days(14)}
    base_by_date = {d["date"]: d for d in baseline}
    # Not "identical to baseline": injecting the race adds a session, and
    # periodization then redistributes the week's deficit around it, which
    # nudges every other day. What must hold is that no taper phase reaches
    # back this far and the cut is not being eased on those days.
    for n in (7, 6, 5, 4, 3):
        iso = (race_day - timedelta(days=n)).isoformat()
        check(f"{n}d out from an A sprint carries no race phase",
              sp[iso].get("race") is None)
        check(f"{n}d out from an A sprint is not eased",
              sp[iso]["target_deficit_kcal"] >= base_by_date[iso]["target_deficit_kcal"])
    check("the sprint still eases 2 days out",
          sp[(race_day - timedelta(days=2)).isoformat()]["race"]["phase"] == "taper")
    # A 78-minute event gets NO carb load: resting glycogen already covers
    # anything under ~90 min, so loading is a surplus bought for nothing.
    day_before = sp[(race_day - timedelta(days=1)).isoformat()]
    check("...but does not carb-load the day before a 78-min race",
          day_before["race"]["phase"] == "taper")
    check("...and so never swings into a big surplus",
          day_before["target_deficit_kcal"] >= 0)
    # Same date, same goal, same calendar — only the event changes. This is the
    # asymmetry the fix is for: the sprint leaves 4d out alone, the marathon
    # doesn't.
    _STORE.pop("fueling_races/current", None)
    g.set_race(date=race_day.isoformat(), name="Same-Slot Marathon",
               distance_label="marathon", priority="A", target_time_hours=3.5)
    mara = {d["date"]: d for d in _plan_days(14)}
    iso4 = (race_day - timedelta(days=4)).isoformat()
    check("a marathon in the same slot still eases 4 days out",
          mara[iso4]["race"]["phase"] == "taper"
          and mara[iso4]["target_deficit_kcal"] < base_by_date[iso4]["target_deficit_kcal"])

    print("a C race does not suspend the cut for a week:")
    _STORE.pop("fueling_races/current", None)
    g.set_race(date=race_day.isoformat(), name="Club 10k",
               distance_label="10k", priority="C", target_time_hours=0.77)
    days = {d["date"]: d for d in _plan_days(14)}
    check("a sub-hour C race earns no loading days",
          days[(race_day - timedelta(days=1)).isoformat()].get("race") is None)
    check("...but race day itself still drops the deficit",
          days[race_day.isoformat()]["target_deficit_kcal"] <= 0)

    print("overlapping races take the more protective phase:")
    _STORE.pop("fueling_races/current", None)
    g.set_race(date=race_day.isoformat(), name="Goal Marathon",
               distance_label="marathon", priority="A", target_time_hours=3.5)
    # A tune-up 10k lands inside the marathon's taper; its own "recovery day 2"
    # (deficit at 50%) must not override the marathon's suspended deficit.
    g.set_race(date=(race_day - timedelta(days=3)).isoformat(), name="Tune-up",
               distance_label="10k", priority="C", target_time_hours=0.77)
    days = {d["date"]: d for d in _plan_days(14)}
    check("marathon loading wins over the tune-up's recovery",
          days[(race_day - timedelta(days=1)).isoformat()]["target_deficit_kcal"] <= 0)

    print("a broken race store degrades to a plain plan:")
    _STORE["fueling_races/current"] = {"not": "a list"}
    plan_bad = g.generate_fueling_plan(start_date=TODAY.isoformat(), days=7)
    check("plan still builds", not plan_bad.get("error"))
    check("no race phases applied", all(d.get("race") is None for d in plan_bad["days"]))


def main():
    test_distances()
    test_duration_estimates()
    test_phases()
    test_store()
    test_plan_integration()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("all race tests passed")


if __name__ == "__main__":
    main()
