"""Race calendar logic: distance presets, duration estimation, and the
carb-load / taper / recovery phase model.

Pure functions only — no storage, no Garmin calls. app/garmin.py owns the R2
persistence and the plan integration; it calls in here to turn "there is a
marathon on 2026-10-11" into per-day carb and deficit directives that
generate_fueling_plan can apply.

Splitting it out keeps the phase model testable on its own: everything here is
a function of (sport, distance, duration, days-until-race), so the loading
protocol can be reasoned about without a cache, a goal, or a Garmin session.
"""
from __future__ import annotations

import re

SPORTS = ("running", "cycling", "triathlon", "swimming", "other")
# Race priority in the classic A/B/C sense: an A race gets a full taper and the
# longest load, a C race is trained through with only the load days.
PRIORITIES = ("A", "B", "C")

# Preset name -> (sport, total_km, legs_km). legs_km is (swim, bike, run) for
# triathlon and None otherwise. Keys are slugs (lowercased, non-alphanumerics
# stripped) so "Half Marathon", "half-marathon" and "half" all resolve here,
# as do "70.3" -> "703" and "140.6" -> "1406".
_PRESETS: dict[str, tuple[str, float, tuple[float, float, float] | None]] = {
    # running
    "5k": ("running", 5.0, None),
    "parkrun": ("running", 5.0, None),
    "8k": ("running", 8.0, None),
    "10k": ("running", 10.0, None),
    "12k": ("running", 12.0, None),
    "15k": ("running", 15.0, None),
    "10mile": ("running", 16.0934, None),
    "10miler": ("running", 16.0934, None),
    "20k": ("running", 20.0, None),
    "half": ("running", 21.0975, None),
    "halfmarathon": ("running", 21.0975, None),
    "25k": ("running", 25.0, None),
    "30k": ("running", 30.0, None),
    "marathon": ("running", 42.195, None),
    "fullmarathon": ("running", 42.195, None),
    "50k": ("running", 50.0, None),
    "50mile": ("running", 80.467, None),
    "50miler": ("running", 80.467, None),
    "100k": ("running", 100.0, None),
    "100mile": ("running", 160.934, None),
    "100miler": ("running", 160.934, None),
    # cycling
    "metriccentury": ("cycling", 100.0, None),
    "century": ("cycling", 160.934, None),
    "granfondo": ("cycling", 120.0, None),
    "mediofondo": ("cycling", 80.0, None),
    "doublecentury": ("cycling", 321.87, None),
    # triathlon (swim / bike / run)
    "supersprint": ("triathlon", 12.875, (0.375, 10.0, 2.5)),
    "sprint": ("triathlon", 25.75, (0.75, 20.0, 5.0)),
    "olympic": ("triathlon", 51.5, (1.5, 40.0, 10.0)),
    "standard": ("triathlon", 51.5, (1.5, 40.0, 10.0)),
    "703": ("triathlon", 113.0, (1.9, 90.0, 21.1)),
    "halfironman": ("triathlon", 113.0, (1.9, 90.0, 21.1)),
    "halfdistance": ("triathlon", 113.0, (1.9, 90.0, 21.1)),
    "1406": ("triathlon", 226.0, (3.8, 180.0, 42.2)),
    "ironman": ("triathlon", 226.0, (3.8, 180.0, 42.2)),
    "fulldistance": ("triathlon", 226.0, (3.8, 180.0, 42.2)),
    # swimming
    "1500mswim": ("swimming", 1.5, None),
    "5kswim": ("swimming", 5.0, None),
    "10kswim": ("swimming", 10.0, None),
}

# Unit -> metres, for the distance a caller types in.
_UNIT_M = {
    "km": 1000.0, "kms": 1000.0, "kilometer": 1000.0, "kilometers": 1000.0,
    "kilometre": 1000.0, "kilometres": 1000.0,
    "mi": 1609.34, "mile": 1609.34, "miles": 1609.34,
    "m": 1.0, "meter": 1.0, "meters": 1.0, "metre": 1.0, "metres": 1.0,
    "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
}

# Fallback race speeds (m/s) when there's neither a Garmin race prediction nor
# enough of the athlete's own history to calibrate against. Deliberately
# middle-of-the-pack — they only ever set the *shape* of the plan (how many
# load days a race earns), and any real data overrides them.
_DEFAULT_RACE_MPS = {
    "running": 3.1, "cycling": 8.3, "swimming": 1.05, "other": 3.0,
}

# Riegel endurance exponent: t2 = t1 x (d2/d1)^k. 1.06 is the classic fit for
# track-to-marathon extrapolation; past the marathon, fatigue, terrain and aid
# stations make the fade much steeper, so ultra segments use a bigger exponent.
_RIEGEL_K = 1.06
_RIEGEL_K_ULTRA = 1.15
_MARATHON_KM = 42.195


def _slug(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def preset_names() -> list[str]:
    """Every recognised distance preset, for tool descriptions and error text."""
    return sorted(_PRESETS)


def resolve_distance(
    sport: str | None = None,
    distance: float | None = None,
    unit: str = "km",
    distance_label: str | None = None,
) -> tuple[str, float, str, tuple[float, float, float] | None]:
    """Normalise a race's sport + distance into (sport, km, label, legs_km).

    Accepts either a named preset ("marathon", "70.3", "century") or a raw
    distance plus unit. A preset wins over an explicit sport, since "marathon"
    already says running — but a raw distance keeps whatever sport was given.
    When a raw distance lands within 1.5% of a known preset for that sport it
    inherits that preset's name, so a 42.2 km entry still reads "marathon".
    """
    key = _slug(distance_label)
    if key and key in _PRESETS:
        p_sport, km, legs = _PRESETS[key]
        return p_sport, round(km, 4), key, legs

    sp = (sport or "").strip().lower() or "running"
    if sp not in SPORTS:
        raise ValueError(f"sport must be one of {SPORTS}")

    if distance is None:
        if key:
            raise ValueError(
                f"unknown distance '{distance_label}' — pass an explicit "
                f"distance + unit, or one of: {', '.join(preset_names())}"
            )
        raise ValueError("provide a distance (+ unit) or a distance_label preset")

    u = (unit or "km").strip().lower()
    if u not in _UNIT_M:
        raise ValueError(f"unit must be one of {sorted(set(_UNIT_M))}")
    km = float(distance) * _UNIT_M[u] / 1000.0
    if km <= 0:
        raise ValueError("distance must be positive")

    # Name it from the nearest same-sport preset when it's essentially that
    # distance, so the label the plan shows matches what the athlete calls it.
    label = distance_label or None
    legs = None
    for name, (p_sport, p_km, p_legs) in _PRESETS.items():
        if p_sport == sp and abs(km - p_km) <= max(0.015 * p_km, 0.05):
            label, legs = name, p_legs
            break
    return sp, round(km, 4), label or f"{round(km, 2)}km", legs


# --- Duration estimation -----------------------------------------------------

def _predictions_seconds(race_predictions: dict | None) -> list[tuple[float, float]]:
    """(distance_km, seconds) anchors from a race-prediction dict. Accepts both
    the athlete_baseline shape ('5k_seconds', 'marathon_seconds', ...) and
    Garmin's raw shape ('time5K', 'timeMarathon', ...)."""
    if not isinstance(race_predictions, dict):
        return []
    pairs = (
        (5.0, ("5k_seconds", "time5K")),
        (10.0, ("10k_seconds", "time10K")),
        (21.0975, ("half_marathon_seconds", "timeHalfMarathon")),
        (_MARATHON_KM, ("marathon_seconds", "timeMarathon")),
    )
    out: list[tuple[float, float]] = []
    for km, keys in pairs:
        for k in keys:
            v = race_predictions.get(k)
            if v:
                try:
                    secs = float(v)
                except (TypeError, ValueError):
                    continue
                if secs > 0:
                    out.append((km, secs))
                break
    return out


def _riegel(t1_s: float, d1_km: float, d2_km: float) -> float:
    """Scale a known race time to another distance. Anything beyond the
    marathon is extrapolated in two segments — standard exponent up to 42.2 km,
    the steeper ultra exponent above it — so a 100 miler isn't predicted off a
    5K time as though the fade were linear in log-distance all the way out."""
    if d2_km <= _MARATHON_KM or d1_km >= _MARATHON_KM:
        k = _RIEGEL_K_ULTRA if d1_km >= _MARATHON_KM else _RIEGEL_K
        return t1_s * (d2_km / d1_km) ** k
    at_marathon = t1_s * (_MARATHON_KM / d1_km) ** _RIEGEL_K
    return at_marathon * (d2_km / _MARATHON_KM) ** _RIEGEL_K_ULTRA


def _leg_hours(sport: str, km: float, race_predictions=None, speed_mps=None) -> float:
    if sport == "running":
        anchors = _predictions_seconds(race_predictions)
        if anchors:
            # Extrapolate from the closest anchor in log-distance — the one
            # needing the least Riegel scaling, so the least distorted.
            import math
            d1, t1 = min(anchors, key=lambda a: abs(math.log(km / a[0])))
            return _riegel(t1, d1, km) / 3600.0
    mps = speed_mps or _DEFAULT_RACE_MPS.get(sport, _DEFAULT_RACE_MPS["other"])
    return (km * 1000.0) / mps / 3600.0


def _transition_minutes(total_km: float) -> float:
    """T1 + T2 combined. Scales with race size: a sprint's transitions are a
    couple of minutes, an Ironman's are a proper stop."""
    if total_km >= 200:
        return 18.0
    if total_km >= 100:
        return 11.0
    if total_km >= 40:
        return 7.0
    return 4.0


def estimate_duration_hours(
    sport: str,
    distance_km: float,
    legs_km: tuple[float, float, float] | None = None,
    race_predictions: dict | None = None,
    speed_mps: dict[str, float] | None = None,
) -> tuple[float, str]:
    """Best estimate of how long the race will take, and where it came from.

    speed_mps: optional {sport: median race-ish m/s} calibrated from the
    athlete's own history, used for anything Garmin doesn't predict (cycling,
    swimming, the non-run triathlon legs).

    Returns (hours, source) where source is one of 'race_predictions',
    'history_pace' or 'default_pace' — surfaced on the race so an estimate
    resting on nothing but a default table is obvious rather than implied.
    """
    speeds = speed_mps or {}
    if sport == "triathlon" and legs_km:
        swim_km, bike_km, run_km = legs_km
        hours = (
            _leg_hours("swimming", swim_km, speed_mps=speeds.get("swimming"))
            + _leg_hours("cycling", bike_km, speed_mps=speeds.get("cycling"))
            # The tri run is run on a bike-emptied set of legs; a standalone
            # run prediction flatters it badly over the longer distances.
            + _leg_hours("running", run_km, race_predictions, speeds.get("running")) * 1.10
            + _transition_minutes(distance_km) / 60.0
        )
        if _predictions_seconds(race_predictions):
            src = "race_predictions"
        elif any(speeds.get(s) for s in ("swimming", "cycling", "running")):
            src = "history_pace"
        else:
            src = "default_pace"
        return round(hours, 2), src

    if sport == "running" and _predictions_seconds(race_predictions):
        return round(_leg_hours("running", distance_km, race_predictions), 2), "race_predictions"
    if speeds.get(sport):
        return round(_leg_hours(sport, distance_km, speed_mps=speeds[sport]), 2), "history_pace"
    return round(_leg_hours(sport, distance_km), 2), "default_pace"


# --- The phase model ---------------------------------------------------------
# What a race actually does to a fueling plan, day by day. Three ideas:
#
#   1. Glycogen loading only pays for events long enough to empty the tank.
#      Under ~60 min it's dead weight; 60-90 min a top-up; past 90 min the
#      classic 1-3 day, 8-12 g/kg protocol (Burke et al.).
#   2. You cannot run a calorie deficit and load at the same time — the deficit
#      is suspended outright across the load, race day, and the first recovery
#      day, and softened through the taper.
#   3. The race itself is a training day with a very large burn, and the days
#      after it are for repair, not for resuming a cut.

def load_days_for(duration_hours: float) -> int:
    """How many days of carb loading the event earns."""
    if duration_hours >= 3.0:
        return 3
    if duration_hours >= 1.5:
        return 2
    if duration_hours >= 1.0:
        return 1
    return 0


def recovery_days_for(duration_hours: float) -> int:
    """How many days after the race the deficit stays off or eased.

    Unlike loading — which tops out at 3 days because glycogen supercompensation
    does, so an Ironman and a marathon load identically — recovery keeps scaling
    with the size of the day. A 10-hour race does muscle damage and immune
    suppression a 3-hour race doesn't, and resuming a cut 4 days later is how
    athletes turn one hard day into a month of flat training.
    """
    if duration_hours >= 8.0:
        return 7      # ironman, 100-miler: a full week before the cut resumes
    if duration_hours >= 5.0:
        return 5      # long ultras, gran fondos
    if duration_hours >= 3.0:
        return 3
    if duration_hours >= 1.5:
        return 2
    return 1


# Full taper length by race priority. The load days sit inside this window.
_TAPER_DAYS = {"A": 7, "B": 4, "C": 0}

# g of carbohydrate per kg bodyweight on each loading day, keyed by
# (total load days, days until the race). Loading ramps up as the race nears:
# the last 48 h do the heavy lifting, and starting at 12 g/kg three days out
# just means three days of feeling awful.
_LOAD_RAMP: dict[tuple[int, int], float] = {
    (3, 3): 8.0, (3, 2): 10.0, (3, 1): 10.0,
    (2, 2): 8.0, (2, 1): 10.0,
    (1, 1): 8.0,
}


def phase_for(days_until: int, duration_hours: float, priority: str = "A") -> dict | None:
    """The fueling directive for a day `days_until` days before a race.

    days_until: 0 on race day, positive before it, negative after.

    Returns None when the day falls outside every window this race touches, so
    callers can treat "no phase" as "plan this day normally". Otherwise:

      phase                 what it is
      ---------------------------------------------------------------
      taper                 volume coming down; deficit eased
      carb_load             glycogen loading; deficit off
      race_day              the event itself; deficit off
      recovery              first day after; deficit off, refuel
      post_race             the rest of the recovery window; deficit eased

      deficit_multiplier    scale on the day's calorie deficit (0 = none)
      carb_g_per_kg         floor on the day's carb target (None = untouched)
      protein_bump          extra g/kg protein for repair
    """
    prio = (priority or "A").strip().upper()
    if prio not in PRIORITIES:
        prio = "A"
    load = load_days_for(duration_hours)
    taper = _TAPER_DAYS[prio]
    recover = recovery_days_for(duration_hours)

    if days_until == 0:
        return {
            "phase": "race_day",
            "days_until": 0,
            "deficit_multiplier": 0.0,
            # Race-day carbs are set from the race fuel card (pre + during +
            # post), which needs bodyweight — so the caller computes it.
            "carb_g_per_kg": None,
            "protein_bump": 0.0,
            "label": "Race day",
            "note": "Race day — no deficit; eat to the race fuel card.",
        }

    if 1 <= days_until <= load:
        return {
            "phase": "carb_load",
            "days_until": days_until,
            "deficit_multiplier": 0.0,
            "carb_g_per_kg": _LOAD_RAMP[(load, days_until)],
            "protein_bump": 0.0,
            "label": f"Carb load · {days_until}d out",
            "note": (
                f"Carb load, {days_until} day{'s' if days_until > 1 else ''} out: "
                f"{_LOAD_RAMP[(load, days_until)]:.0f} g/kg carbs, deficit off. "
                "Keep training light and expect 1-2 kg of water weight — it is "
                "glycogen, not fat."
            ),
        }

    if load < days_until <= taper:
        # Deeper into the taper the cut can still run, just softened; the last
        # few days before loading it comes almost all the way off.
        mult = 0.25 if days_until <= 3 else 0.5
        return {
            "phase": "taper",
            "days_until": days_until,
            "deficit_multiplier": mult,
            "carb_g_per_kg": 5.0 if days_until <= 3 else None,
            "protein_bump": 0.0,
            "label": f"Taper · {days_until}d out",
            "note": (
                f"Taper week ({days_until} days out): deficit cut to "
                f"{round(mult * 100)}% so the taper actually restores glycogen "
                "instead of digging the hole deeper."
            ),
        }

    if days_until == -1:
        return {
            "phase": "recovery",
            "days_until": -1,
            "deficit_multiplier": 0.0,
            "carb_g_per_kg": 6.0,
            "protein_bump": 0.3,
            "label": "Recovery · day 1",
            "note": (
                "Day after the race: no deficit, carbs 6 g/kg and extra protein "
                "to refill glycogen and repair muscle damage."
            ),
        }

    if -recover <= days_until <= -2:
        return {
            "phase": "post_race",
            "days_until": days_until,
            "deficit_multiplier": 0.5,
            "carb_g_per_kg": 5.0,
            "protein_bump": 0.2,
            "label": f"Recovery · day {abs(days_until)}",
            "note": (
                f"Recovery day {abs(days_until)}: deficit at 50% while the "
                "repair from race day is still running."
            ),
        }

    return None


def window_days(duration_hours: float, priority: str = "A") -> tuple[int, int]:
    """(days before, days after) a race that carry a phase — the span over
    which this race can affect a plan at all."""
    prio = (priority or "A").strip().upper()
    if prio not in PRIORITIES:
        prio = "A"
    return (max(_TAPER_DAYS[prio], load_days_for(duration_hours)),
            recovery_days_for(duration_hours))
