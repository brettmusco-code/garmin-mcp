"""Multi-method threshold and VO2max estimation.

Garmin exposes single point estimates for VO2max, LT HR, FTP, CSS, etc.
Those are convenient but can drift — Garmin's VO2max algorithm is
sensitive to a few recent hard efforts and lags reality when training
shifts. By computing the same targets via multiple independent methods
(Jack Daniels VDOT, Coggan power tests, Karvonen HR, Mader equation,
etc.) we can:

  1. Cross-validate Garmin's number
  2. Flag when Garmin is stale or out of date
  3. Give the LLM a richer picture for pacing recommendations

Each helper returns a dict:

    {
      "garmin_value": float | None,
      "methods": [
        {"name": str, "value": float, "delta_vs_garmin": float,
         "confidence": "low" | "medium" | "high",
         "source": str, "notes": str},
        ...
      ],
      "consensus": float | None,         # median of all methods incl. Garmin
      "spread": float | None,            # max - min (flag large spreads)
      "flag": str | None,                # "consider Garmin update" etc.
    }
"""
from __future__ import annotations

import statistics
from typing import Any, Iterable


# ---------- shared helpers ----------


def _median_or_none(xs: list[float]) -> float | None:
    vals = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(vals), 1) if vals else None


def _spread(xs: list[float]) -> float | None:
    vals = [x for x in xs if isinstance(x, (int, float))]
    return round(max(vals) - min(vals), 1) if len(vals) >= 2 else None


def _flag_from_spread(methods: list[dict], garmin_value: float | None,
                      large_spread_threshold: float) -> str | None:
    """If most non-Garmin methods agree on a different value than Garmin,
    suggest the user might consider a fitness check."""
    if garmin_value is None:
        return None
    other_vals = [m["value"] for m in methods
                  if m["name"] != "garmin" and isinstance(m.get("value"), (int, float))]
    if len(other_vals) < 2:
        return None
    other_median = statistics.median(other_vals)
    if abs(other_median - garmin_value) >= large_spread_threshold:
        direction = "higher" if other_median > garmin_value else "lower"
        return (
            f"Non-Garmin methods agree around {round(other_median, 1)} but "
            f"Garmin says {garmin_value}. Recent performance suggests actual "
            f"value is {direction}. Consider a fitness check or updated field test."
        )
    return None


def _pace_to_speed_m_min(seconds_per_km: float) -> float:
    """Convert sec/km pace to m/min speed (used by VDOT formula)."""
    return 1000.0 / (seconds_per_km / 60.0)


# ---------- VO2max (run) ----------


def vdot_from_time(distance_m: float, time_seconds: float) -> float | None:
    """Jack Daniels VDOT from a race/time-trial time.
    VDOT ≈ -4.6 + 0.182258·v + 0.000104·v²   where v = distance / time_min (m/min)
    """
    if not distance_m or not time_seconds:
        return None
    try:
        v = distance_m / (time_seconds / 60.0)
        return round(-4.6 + 0.182258 * v + 0.000104 * (v ** 2), 1)
    except (ValueError, ZeroDivisionError):
        return None


def run_vo2max_methods(
    garmin_vo2max: float | None,
    race_predictions: dict | None,
    run_activities: list[dict],
    lt_hr: float | None,
) -> dict[str, Any]:
    """Compare multiple VO2max estimates for running.

    Methods:
      1. Garmin's reported VO2max (from max_metrics.generic)
      2. Jack Daniels VDOT from Garmin's 5K prediction
      3. VDOT from best recent 5K fastestSplit across all runs
      4. VDOT from best 10K split
      5. "Sustained threshold effort" — best 20+ min at HR near LT (proxy)
    """
    methods: list[dict[str, Any]] = []

    if garmin_vo2max is not None:
        methods.append({
            "name": "garmin",
            "value": garmin_vo2max,
            "delta_vs_garmin": 0.0,
            "confidence": "medium",
            "source": "max_metrics.generic",
            "notes": "Garmin's lab-validated proxy. Lags real fitness by ~1-2 weeks.",
        })

    # Method 2: Garmin 5K prediction → VDOT (same algorithm as actual 5K below)
    t5k = (race_predictions or {}).get("5k_seconds")
    if t5k:
        v = vdot_from_time(5000.0, t5k)
        if v is not None:
            methods.append({
                "name": "vdot_from_garmin_5k_prediction",
                "value": v,
                "delta_vs_garmin": round(v - garmin_vo2max, 1) if garmin_vo2max else None,
                "confidence": "medium",
                "source": f"Jack Daniels VDOT from Garmin's predicted 5K ({round(t5k/60, 1)} min)",
                "notes": "Depends on Garmin's prediction, which itself is based on VO2max. Circular but useful as a check.",
            })

    # Method 3: VDOT from best actual 5K split in a real run
    best_5k_s = min(
        (a.get("fastestSplit_5000") for a in run_activities if a.get("fastestSplit_5000")),
        default=None,
    )
    if best_5k_s:
        v = vdot_from_time(5000.0, best_5k_s)
        if v is not None:
            methods.append({
                "name": "vdot_from_actual_5k_split",
                "value": v,
                "delta_vs_garmin": round(v - garmin_vo2max, 1) if garmin_vo2max else None,
                "confidence": "high",
                "source": f"VDOT from best 5K split in last 60 days ({round(best_5k_s/60, 1)} min)",
                "notes": "Actual performance, not prediction. Most reliable when the split was an all-out effort.",
            })

    # Method 4: VDOT from best 1-mile split (equivalent conversion)
    best_mile_s = min(
        (a.get("fastestSplit_1609") for a in run_activities if a.get("fastestSplit_1609")),
        default=None,
    )
    if best_mile_s:
        v = vdot_from_time(1609.0, best_mile_s)
        if v is not None:
            methods.append({
                "name": "vdot_from_actual_mile_split",
                "value": v,
                "delta_vs_garmin": round(v - garmin_vo2max, 1) if garmin_vo2max else None,
                "confidence": "medium",
                "source": f"VDOT from best mile split in last 60 days ({round(best_mile_s/60, 1)} min)",
                "notes": "Mile is short — tends to overestimate VO2max vs 5K. Use as a ceiling reference.",
            })

    # Method 5: HR-based threshold sustain — VO2max estimate from % max HR
    # effort and avg pace. Rough but useful when actual racing data is thin.
    # Formula: VO2max ≈ speed × 0.2 + 3.5 (ACSM)
    # Take best recent sustained pace at >=90% max HR for >=20 min.
    # (We don't have per-split HR here, so skip for now and flag.)

    # Compute consensus + spread + flag
    all_vals = [m["value"] for m in methods if m["value"] is not None]
    consensus = _median_or_none(all_vals)
    spread = _spread(all_vals)
    # Flag if non-Garmin methods agree on a value ≥1.0 different from Garmin
    flag = _flag_from_spread(methods, garmin_vo2max, large_spread_threshold=1.0)

    return {
        "garmin_value": garmin_vo2max,
        "methods": methods,
        "consensus": consensus,
        "spread": spread,
        "flag": flag,
    }


# ---------- LT HR (run) ----------


def run_lt_hr_methods(
    garmin_lt_hr: float | None,
    max_hr: float | None,
    rhr: float | None,
    run_activities: list[dict],
) -> dict[str, Any]:
    """Compare multiple LT HR estimates for running.

    Methods:
      1. Garmin's reported LT HR
      2. Karvonen-derived: RHR + (Max HR - RHR) × 0.85
      3. Joe Friel percentage: ~88-90% of max HR
      4. Highest avg HR sustained in 20+ min hard efforts (from recent runs)
    """
    methods: list[dict[str, Any]] = []

    if garmin_lt_hr is not None:
        methods.append({
            "name": "garmin",
            "value": garmin_lt_hr,
            "delta_vs_garmin": 0.0,
            "confidence": "high",
            "source": "lactate_threshold endpoint",
            "notes": "Auto-detected from recent threshold efforts. Updates every few weeks.",
        })

    # Method 2: Karvonen heart-rate reserve × 0.85
    if max_hr and rhr:
        karv = rhr + (max_hr - rhr) * 0.85
        methods.append({
            "name": "karvonen_85pct_hrr",
            "value": round(karv, 0),
            "delta_vs_garmin": round(karv - garmin_lt_hr, 1) if garmin_lt_hr else None,
            "confidence": "medium",
            "source": f"RHR {rhr} + (MaxHR {max_hr} - RHR) × 0.85",
            "notes": "Population-based estimate. Personal LT can be ±5 bpm from this.",
        })

    # Method 3: 88% of max HR (Joe Friel method for endurance athletes)
    if max_hr:
        friel = max_hr * 0.88
        methods.append({
            "name": "friel_88pct_max_hr",
            "value": round(friel, 0),
            "delta_vs_garmin": round(friel - garmin_lt_hr, 1) if garmin_lt_hr else None,
            "confidence": "low",
            "source": f"MaxHR {max_hr} × 0.88",
            "notes": "Rough rule of thumb. Varies widely by individual.",
        })

    # Method 4: Highest sustained avg HR in recent hard runs ≥20 min
    # Use activities whose avgHR is high and duration ≥ 20 min.
    candidates = [
        a.get("averageHR") for a in run_activities
        if a.get("averageHR") and (a.get("duration") or 0) >= 1200
        and a.get("averageHR") > (garmin_lt_hr or 160) * 0.95
    ]
    if candidates:
        # Use the max observed — a real threshold effort.
        observed = max(candidates)
        methods.append({
            "name": "max_sustained_threshold_hr",
            "value": round(observed, 0),
            "delta_vs_garmin": round(observed - garmin_lt_hr, 1) if garmin_lt_hr else None,
            "confidence": "high",
            "source": f"Highest avg HR in a ≥20min effort in last 60 days",
            "notes": "Directly observed. If much higher than Garmin's LT, Garmin may be stale.",
        })

    all_vals = [m["value"] for m in methods if m["value"] is not None]
    consensus = _median_or_none(all_vals)
    spread = _spread(all_vals)
    flag = _flag_from_spread(methods, garmin_lt_hr, large_spread_threshold=3.0)

    return {
        "garmin_value": garmin_lt_hr,
        "methods": methods,
        "consensus": consensus,
        "spread": spread,
        "flag": flag,
    }


# ---------- Running FTP (critical power / critical pace) ----------


def run_ftp_methods(
    garmin_run_ftp: float | None,
    run_activities: list[dict],
) -> dict[str, Any]:
    """Compare multiple running FTP estimates.

    Methods:
      1. Garmin's reported run FTP
      2. Best 20-min power × 0.95 (Coggan method)
      3. Best 60-min power (critical power proxy)
    """
    methods: list[dict[str, Any]] = []

    if garmin_run_ftp is not None:
        methods.append({
            "name": "garmin",
            "value": garmin_run_ftp,
            "delta_vs_garmin": 0.0,
            "confidence": "high",
            "source": "lactate_threshold.power",
            "notes": "Garmin's running power FTP. Updated on threshold-eligible efforts.",
        })

    best_20min = max(
        (a.get("maxAvgPower_1200") for a in run_activities if a.get("maxAvgPower_1200")),
        default=None,
    )
    if best_20min:
        coggan = round(best_20min * 0.95)
        methods.append({
            "name": "coggan_20min_test",
            "value": coggan,
            "delta_vs_garmin": round(coggan - garmin_run_ftp) if garmin_run_ftp else None,
            "confidence": "high",
            "source": f"Best 20-min avg power ({best_20min}W) × 0.95",
            "notes": "Classic FTP-from-test proxy. Accurate if the 20-min effort was all-out.",
        })

    best_60min = max(
        (a.get("maxAvgPower_3600") for a in run_activities if a.get("maxAvgPower_3600")),
        default=None,
    )
    if best_60min:
        methods.append({
            "name": "best_60min_power",
            "value": best_60min,
            "delta_vs_garmin": round(best_60min - garmin_run_ftp) if garmin_run_ftp else None,
            "confidence": "high",
            "source": f"Best 60-min avg power in last 60 days",
            "notes": "Direct critical-power observation. Equals FTP if you held threshold for an hour.",
        })

    all_vals = [m["value"] for m in methods if m["value"] is not None]
    consensus = _median_or_none(all_vals)
    spread = _spread(all_vals)
    flag = _flag_from_spread(methods, garmin_run_ftp, large_spread_threshold=10.0)

    return {
        "garmin_value": garmin_run_ftp,
        "methods": methods,
        "consensus": consensus,
        "spread": spread,
        "flag": flag,
    }


# ---------- Cycling FTP ----------


def bike_ftp_methods(
    garmin_bike_ftp: float | None,
    ride_activities: list[dict],
) -> dict[str, Any]:
    """Compare multiple cycling FTP estimates.

    Methods:
      1. Garmin's reported bike FTP (if set — most users don't have one)
      2. Best 20-min × 0.95 (Coggan)
      3. Best 60-min × 1.0 (critical power)
      4. Normalized Power from best 40-60 min effort × ~0.97 (TTE-adjusted)
    """
    methods: list[dict[str, Any]] = []

    if garmin_bike_ftp is not None:
        methods.append({
            "name": "garmin",
            "value": garmin_bike_ftp,
            "delta_vs_garmin": 0.0,
            "confidence": "high",
            "source": "Garmin-reported bike FTP",
            "notes": "Depends on whether user ran an explicit FTP test in Garmin.",
        })

    best_20min = max(
        (a.get("maxAvgPower_1200") for a in ride_activities if a.get("maxAvgPower_1200")),
        default=None,
    )
    if best_20min:
        coggan = round(best_20min * 0.95)
        methods.append({
            "name": "coggan_20min_test",
            "value": coggan,
            "delta_vs_garmin": round(coggan - garmin_bike_ftp) if garmin_bike_ftp else None,
            "confidence": "high",
            "source": f"Best 20-min avg power ({best_20min}W) × 0.95",
            "notes": "Reliable when the 20-min was truly all-out. Overestimates if paced conservatively.",
        })

    best_60min = max(
        (a.get("maxAvgPower_3600") for a in ride_activities if a.get("maxAvgPower_3600")),
        default=None,
    )
    if best_60min:
        methods.append({
            "name": "best_60min_power",
            "value": best_60min,
            "delta_vs_garmin": round(best_60min - garmin_bike_ftp) if garmin_bike_ftp else None,
            "confidence": "high",
            "source": f"Best 60-min avg power in last 60 days ({best_60min}W)",
            "notes": "Direct critical-power observation. This IS FTP if it was a full hour at threshold.",
        })

    # Normalized Power is typically slightly higher than avg for variable efforts;
    # Best recent NP over a long ride gives a low-cost FTP floor.
    best_np_long = max(
        (a.get("normPower") for a in ride_activities
         if a.get("normPower") and (a.get("duration") or 0) >= 2400),  # >=40 min
        default=None,
    )
    if best_np_long:
        # Over a 40-60 min effort, NP tends to run ~5-10% above FTP if sustained hard.
        np_based = round(best_np_long * 0.97)
        methods.append({
            "name": "np_adjusted_long_effort",
            "value": np_based,
            "delta_vs_garmin": round(np_based - garmin_bike_ftp) if garmin_bike_ftp else None,
            "confidence": "medium",
            "source": f"Best NP ({best_np_long}W) from a ≥40min ride × 0.97",
            "notes": "NP-adjusted floor. Lower bound on FTP if the ride was paced aggressively.",
        })

    all_vals = [m["value"] for m in methods if m["value"] is not None]
    consensus = _median_or_none(all_vals)
    spread = _spread(all_vals)
    flag = _flag_from_spread(methods, garmin_bike_ftp, large_spread_threshold=10.0)

    return {
        "garmin_value": garmin_bike_ftp,
        "methods": methods,
        "consensus": consensus,
        "spread": spread,
        "flag": flag,
    }


# ---------- Cycling VO2max ----------


def bike_vo2max_methods(
    garmin_vo2max_bike: float | None,
    ride_activities: list[dict],
    weight_kg: float | None,
) -> dict[str, Any]:
    """Compare cycling VO2max estimates.

    Methods:
      1. Garmin's reported cycling VO2max
      2. Mader equation from best 5-min power: VO2max ≈ (P5min × 10.8 / weight + 7) / weight
    """
    methods: list[dict[str, Any]] = []

    if garmin_vo2max_bike is not None:
        methods.append({
            "name": "garmin",
            "value": garmin_vo2max_bike,
            "delta_vs_garmin": 0.0,
            "confidence": "medium",
            "source": "max_metrics.cycling",
            "notes": "Garmin estimate from power + HR data.",
        })

    best_5min = max(
        (a.get("maxAvgPower_300") for a in ride_activities if a.get("maxAvgPower_300")),
        default=None,
    )
    if best_5min and weight_kg and weight_kg > 0:
        # Mader approximation: VO2max (ml/kg/min) ≈ (W × 10.8 / weight_kg + 7) / weight_kg
        # Adjusted for typical cycling gross efficiency.
        mader_vo2 = (best_5min * 10.8 / weight_kg + 7) / weight_kg
        # Note: the above formula is actually ACSM-derived; Mader's original
        # uses blood lactate. Here we're using a simplified power→VO2 proxy:
        #   VO2 (ml/kg/min) = 10.8 × W/kg + 7  (rough endurance-athlete approx)
        wkg = best_5min / weight_kg
        simple_vo2 = round(10.8 * wkg + 7, 1)
        methods.append({
            "name": "power_to_vo2_from_5min_best",
            "value": simple_vo2,
            "delta_vs_garmin": round(simple_vo2 - garmin_vo2max_bike, 1) if garmin_vo2max_bike else None,
            "confidence": "medium",
            "source": f"Best 5-min power ({best_5min}W @ {weight_kg}kg = {round(wkg,2)} W/kg)",
            "notes": "Linear power-to-VO2 approximation. Accurate ±3 ml/kg/min for trained cyclists.",
        })

    all_vals = [m["value"] for m in methods if m["value"] is not None]
    consensus = _median_or_none(all_vals)
    spread = _spread(all_vals)
    flag = _flag_from_spread(methods, garmin_vo2max_bike, large_spread_threshold=2.0)

    return {
        "garmin_value": garmin_vo2max_bike,
        "methods": methods,
        "consensus": consensus,
        "spread": spread,
        "flag": flag,
    }


# ---------- Swim CSS (Critical Swim Speed) ----------


def swim_css_methods(
    swim_activities: list[dict],
) -> dict[str, Any]:
    """Compare multiple CSS (Critical Swim Speed) estimates.

    Methods:
      1. Ginn & Mackenzie: (1000m time - 400m time) / 600m (seconds per meter)
      2. 400m pace projection: best 400m split rescaled
      3. 1000m-pace as CSS approximation (if a genuine TT exists)
    """
    methods: list[dict[str, Any]] = []

    best_100 = min(
        (a.get("fastestSplit_100") for a in swim_activities if a.get("fastestSplit_100")),
        default=None,
    )
    best_400 = min(
        (a.get("fastestSplit_400") for a in swim_activities if a.get("fastestSplit_400")),
        default=None,
    )
    best_750 = min(
        (a.get("fastestSplit_750") for a in swim_activities if a.get("fastestSplit_750")),
        default=None,
    )
    best_1000 = min(
        (a.get("fastestSplit_1000") for a in swim_activities if a.get("fastestSplit_1000")),
        default=None,
    )

    # Method 1: Two-point linear regression on 400m and 1000m
    if best_400 and best_1000:
        sec_per_m = (best_1000 - best_400) / (1000 - 400)
        css_per_100m = round(sec_per_m * 100, 1)
        methods.append({
            "name": "ginn_mackenzie_400_1000",
            "value": css_per_100m,
            "delta_vs_garmin": None,
            "confidence": "high",
            "source": f"400m ({best_400}s) and 1000m ({best_1000}s) best splits",
            "notes": "Standard CSS formula: linear speed/distance regression.",
        })

    # Method 2: Two-point on 400m and 750m (shorter window — less reliable but faster)
    if best_400 and best_750 and not (best_400 and best_1000):
        sec_per_m = (best_750 - best_400) / (750 - 400)
        css_per_100m = round(sec_per_m * 100, 1)
        methods.append({
            "name": "two_point_400_750",
            "value": css_per_100m,
            "delta_vs_garmin": None,
            "confidence": "medium",
            "source": f"400m ({best_400}s) and 750m ({best_750}s) best splits",
            "notes": "Shorter distance base — CSS tends to be underestimated.",
        })

    # Method 3: 1000m pace directly as CSS floor
    if best_1000:
        pace_1000 = round(best_1000 / 10, 1)  # sec/100m
        methods.append({
            "name": "best_1000m_avg_pace",
            "value": pace_1000,
            "delta_vs_garmin": None,
            "confidence": "medium",
            "source": f"Best 1000m split avg pace ({best_1000}s / 1000m)",
            "notes": "Direct observation. If the 1000m was all-out, this IS close to CSS.",
        })

    all_vals = [m["value"] for m in methods if m["value"] is not None]
    consensus = _median_or_none(all_vals)
    spread = _spread(all_vals)
    flag = None
    # For swimming, no Garmin CSS to compare against — just flag large spread.
    if spread and spread > 5.0:
        flag = f"Methods disagree by {spread}s per 100m — pick CSS from best 1000m TT if available."

    return {
        "garmin_value": None,  # no Garmin CSS
        "methods": methods,
        "consensus": consensus,
        "spread": spread,
        "flag": flag,
    }
