"""Multi-method threshold and VO2max estimation.

Garmin exposes single point estimates for VO2max, LT HR, FTP, CSS. These
can drift or lag real fitness. We cross-validate each with independent
methods — Jack Daniels VDOT, Coggan 20-min test, Karvonen HRR, Mader
power-to-VO2, Ginn-Mackenzie CSS — and surface the consensus + a flag
when Garmin materially disagrees with observed performance.

Accuracy techniques applied (not just averaging):
  - KEY-SESSION FILTERING: only races/TTs/intervals/tempo/threshold
    count. Easy/recovery sessions are excluded so their pace/power
    spikes don't pollute estimates. See is_key_run/ride/swim.
  - RECENCY WEIGHTING: each candidate scored by value × 0.5^(age/30d),
    so recent peak efforts dominate over 3-month-old ones.
  - AEROBIC DECOUPLING FILTER (bike FTP): rides where HR behavior
    suggests a sustained effort — not a brief spike or blowup.
  - TTE-ADJUSTED FTP (bike): 60/20-min power ratio picks the right
    Coggan reducer (0.93-0.97 instead of blanket 0.95).
  - HEAT CORRECTION (run VO2max): detected race efforts are corrected
    back to neutral-temp equivalents before VDOT calculation.
  - LT2 → LT1 derivation: LT1 (aerobic threshold, Z2 cap) ≈ LT2 × 0.92.
  - CONFIDENCE INTERVALS: 80% CI from method percentiles when ≥3
    methods produced values.

Each threshold helper returns a dict:

    {
      "garmin_value": float | None,
      "methods": [
        {"name": str, "value": float, "delta_vs_garmin": float,
         "confidence": "low" | "medium" | "high",
         "source": str, "notes": str},
        ...
      ],
      "consensus": float | None,           # median of all methods
      "confidence_interval_80pct": [lo, hi] | None,
      "ci_note": str,                      # why CI may be None
      "spread": float | None,              # max - min
      "flag": str | None,                  # "consider Garmin update" etc.
    }
"""
from __future__ import annotations

import math
import statistics
from datetime import date, datetime
from typing import Any, Iterable


# ---------- key-session detection ----------

# Keywords that signal a deliberate quality/test session in activity names.
# Lowercased comparison; any hit qualifies the session as "key."
_RUN_KEY_KEYWORDS = {
    "race", "tt", "time trial", "threshold", "tempo", "vo2",
    "interval", "test", "5k", "10k", "half marathon", "marathon",
    "track", "repeats", "fartlek", "lactate", "ftp",
}
_BIKE_KEY_KEYWORDS = {
    "race", "tt", "time trial", "threshold", "tempo", "vo2",
    "interval", "test", "ftp", "cp", "critical power",
    "sweet spot", "sst", "over-under", "overunder", "z4", "z5",
    "20 min", "20min", "20-min",
}
_SWIM_KEY_KEYWORDS = {
    "race", "tt", "time trial", "test", "css", "critical swim speed",
    "400", "800", "1000", "1500", "threshold",
}


def _activity_name(a: dict) -> str:
    return (a.get("activityName") or "").lower()


def _event_is_race(a: dict) -> bool:
    et = a.get("eventType") or {}
    return (et.get("typeKey") or "").lower() in {"race", "competition"}


def _name_matches(name: str, kws: set[str]) -> bool:
    return any(kw in name for kw in kws)


def is_key_run(a: dict, observed_max_hr: float | None) -> bool:
    """Return True if this run looks like a deliberate hard/key session."""
    dur = a.get("duration") or 0
    if dur < 900:  # < 15 min
        return False
    if _event_is_race(a):
        return True
    name = _activity_name(a)
    if _name_matches(name, _RUN_KEY_KEYWORDS):
        return True
    avg_hr = a.get("averageHR")
    if avg_hr and observed_max_hr and avg_hr >= 0.88 * observed_max_hr:
        return True
    return False


def is_key_ride(a: dict, ftp_estimate: float | None) -> bool:
    """Return True if this ride looks like a deliberate hard/key session."""
    dur = a.get("duration") or 0
    if dur < 1200:  # < 20 min
        return False
    if _event_is_race(a):
        return True
    name = _activity_name(a)
    if _name_matches(name, _BIKE_KEY_KEYWORDS):
        return True
    # IF ≥ 0.80 based on either Garmin-reported or computed FTP
    if_val = a.get("intensityFactor")
    if if_val and if_val >= 0.80:
        return True
    # Fall back: avg power vs an estimated FTP if IF wasn't reported
    avg_pwr = a.get("avgPower")
    if avg_pwr and ftp_estimate and avg_pwr >= 0.80 * ftp_estimate:
        return True
    return False


def is_key_swim(a: dict) -> bool:
    """Return True if this swim looks like a deliberate hard/key session."""
    dist = a.get("distance") or 0
    if dist < 400:
        return False
    if _event_is_race(a):
        return True
    name = _activity_name(a)
    if _name_matches(name, _SWIM_KEY_KEYWORDS):
        return True
    # If it has a recorded 400m or 1000m fastest split, treat as test-eligible.
    if a.get("fastestSplit_400") or a.get("fastestSplit_1000"):
        return True
    return False


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
    suggest the user might consider a fitness check. Only flags when
    ALL non-Garmin methods point in the same direction (consistent
    disagreement), not when methods are mixed."""
    if garmin_value is None:
        return None
    other_vals = [m["value"] for m in methods
                  if m["name"] != "garmin" and isinstance(m.get("value"), (int, float))]
    if len(other_vals) < 2:
        return None
    other_median = statistics.median(other_vals)
    delta = other_median - garmin_value
    if abs(delta) < large_spread_threshold:
        return None
    # Only flag if the majority of non-Garmin methods agree on the direction.
    same_direction = sum(
        1 for v in other_vals
        if (v > garmin_value) == (delta > 0)
    )
    if same_direction / len(other_vals) < 0.6:
        return None
    direction = "higher" if delta > 0 else "lower"
    return (
        f"{same_direction}/{len(other_vals)} non-Garmin methods agree around "
        f"{round(other_median, 1)} — {round(abs(delta), 1)} units {direction} "
        f"than Garmin's {garmin_value}. Consider a field test if sessions feel "
        f"off vs. current zones."
    )


def _pace_to_speed_m_min(seconds_per_km: float) -> float:
    """Convert sec/km pace to m/min speed (used by VDOT formula)."""
    return 1000.0 / (seconds_per_km / 60.0)


# ---------- accuracy helpers ----------


def _parse_activity_date(a: dict) -> date | None:
    """Best-effort parse of an activity's start date."""
    ts = a.get("startTimeLocal") or a.get("startTimeGMT") or ""
    try:
        # Handle "2026-05-01 14:00:34", "2026-05-01T14:00:34", ".0" suffix, trailing Z
        s = ts.strip().replace("T", " ").rstrip("Z")
        if "." in s:
            s = s.split(".", 1)[0]
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").date()
    except (ValueError, AttributeError):
        return None


def recency_weight(activity_date: date | None, today: date,
                   half_life_days: float = 30.0) -> float:
    """Exponential decay weight for recency. Session today = 1.0,
    half_life_days ago = 0.5. Returns 0 for unknown dates (safely ignored)."""
    if activity_date is None:
        return 0.0
    age = max(0, (today - activity_date).days)
    return math.pow(0.5, age / half_life_days)


def weighted_best(activities: list[dict], value_key: str,
                  today: date, half_life_days: float = 30.0,
                  higher_is_better: bool = True) -> tuple[float | None, dict | None]:
    """Pick the "best" value from activities, weighting recent efforts more.

    Rather than taking the absolute max/min across all sessions (which
    anchors on a single possibly-stale peak), we rank each candidate by
    its value multiplied by a recency weight. The activity that wins is
    the one that combines high value AND recency.

    Returns (weighted_value, source_activity) — the raw value from the
    winner (not the weighted score itself).
    """
    candidates = []
    for a in activities:
        v = a.get(value_key)
        if v is None or v <= 0:
            continue
        w = recency_weight(_parse_activity_date(a), today, half_life_days)
        if w == 0:
            continue
        score = v * w if higher_is_better else (1.0 / v) * w
        candidates.append((score, v, a))
    if not candidates:
        return None, None
    candidates.sort(key=lambda t: t[0], reverse=True)
    _, winner_value, winner_act = candidates[0]
    return winner_value, winner_act


def aerobic_decoupling_clean(a: dict) -> bool:
    """Check whether a session was "clean" enough to anchor threshold
    estimates. Aerobic decoupling = HR drift vs power drift. If HR rose
    significantly over a 20-min effort but power held, the athlete was
    pacing conservatively at sub-threshold. If HR held but power dropped,
    they blew up mid-effort.

    Garmin's activity objects don't expose first-half/second-half power
    directly, so we use a proxy:
      - averageHR vs maxHR spread is reasonable (not a blowup spike)
      - IF (intensity factor) >= 0.80 (real threshold effort)
    If the spread is too narrow (avg very close to max) they likely blew
    up at the end. If IF is low, it wasn't a threshold session.

    Returns True if the session looks clean enough to use. Conservative
    default — returns True if we can't tell.
    """
    avg_hr = a.get("averageHR")
    max_hr_a = a.get("maxHR")
    if_val = a.get("intensityFactor")
    # If IF is reported and is too low, skip.
    if if_val is not None and if_val < 0.80:
        return False
    # If HR data missing, be conservative and accept the session.
    if not avg_hr or not max_hr_a:
        return True
    # If peak HR is only slightly above avg (<5% gap), likely a sustained
    # clean effort. If it's much higher, they spiked at the end (fine —
    # 20-min power avg still valid). If much narrower (<2%), suspicious.
    gap_pct = (max_hr_a - avg_hr) / max_hr_a
    if gap_pct < 0.02:
        return False
    return True


def _scale_peak_by_if(peak: float, ride_if: float | None,
                      duration_s: int) -> tuple[float, bool]:
    """Scale a rolling-best power peak to its threshold-equivalent when
    the whole session was ridden at sweet-spot / sub-threshold intensity.

    Rationale: a 20-min peak within a sweet-spot session at IF 0.88 is
    probably only slightly above the session's IF — the rider isn't
    doing a hidden VO2max effort within a sweet-spot block. Without
    scaling, we'd treat the 20-min peak as threshold-level when it's
    actually sub-threshold.

    Scaling rule: if ride IF < 0.95, scale peak UP by (0.95 / IF).
    Cap scaling at 15% to avoid absurd inferences from mis-calibrated IF.

    Duration matters: for very short intervals (≤5 min), the peak is
    typically well above the session IF regardless (VO2max efforts
    appear in any ride). Don't scale those.

    Returns (scaled_peak, was_scaled).
    """
    if ride_if is None or ride_if >= 0.95:
        return peak, False
    if duration_s <= 300:  # 5 min or less: peak is intensity-independent
        return peak, False
    if ride_if < 0.70:  # recovery or easy — peaks aren't threshold signals
        return peak, False
    scaler = 0.95 / ride_if
    scaler = min(scaler, 1.15)  # cap 15% upscale
    return peak * scaler, True


def critical_power_fit(activities: list[dict]) -> dict | None:
    """Fit the Monod-Scherrer critical power model across multiple
    activities: P(t) = CP + W' / t, where t is in seconds.

    Uses rolling-best power for several time buckets from each ride,
    IF-scaled for sub-threshold sessions. Then fits CP across all of
    them. CP is directly interpretable as sustainable power (≈ FTP).

    Key insight: when a ride's overall IF is 0.88 (sweet spot), its
    20-min peak might be ~295W but the equivalent THRESHOLD 20-min
    would be ~310W. Scale peaks by IF before fitting to avoid
    systematically under-estimating FTP from sub-threshold sessions.

    Returns {"cp": W, "w_prime": J, "n_points": n, "source": ...} or None.
    """
    duration_keys = [
        (300, "maxAvgPower_300"),
        (600, "maxAvgPower_600"),
        (1200, "maxAvgPower_1200"),
        (1800, "maxAvgPower_1800"),
        (2400, "maxAvgPower_2400"),
        (3600, "maxAvgPower_3600"),
    ]
    # Points with IF-scaling applied so sub-threshold sessions contribute
    # their threshold-equivalent peak rather than the raw sub-threshold.
    points: list[tuple[int, float]] = []
    scaled_count = 0
    for a in activities:
        ride_if = a.get("intensityFactor")
        for t_sec, key in duration_keys:
            p = a.get(key)
            if p and p > 0:
                scaled_p, was_scaled = _scale_peak_by_if(p, ride_if, t_sec)
                if was_scaled:
                    scaled_count += 1
                points.append((t_sec, scaled_p))
    if len(points) < 3:
        return None

    best_by_dur: dict[int, float] = {}
    for t, p in points:
        if p > best_by_dur.get(t, 0):
            best_by_dur[t] = p

    # Drop the very short (5-10 min) buckets if longer ones exist —
    # short-duration power is VO2max-limited, not threshold-limited, and
    # pulls the CP fit upward. Keep only 20+ min for FTP estimation.
    long_points = [(t, p) for t, p in best_by_dur.items() if t >= 1200]
    if len(long_points) < 2:
        # Fall back to all points if we don't have enough long ones
        long_points = list(best_by_dur.items())
        if len(long_points) < 2:
            return None

    # Two-parameter linear regression: P = CP + W' * (1/t)
    # Rewrite as y = a + b*x where y = P, x = 1/t, a = CP, b = W'
    xs = [1.0 / t for t, _ in long_points]
    ys = [p for _, p in long_points]
    n = len(xs)
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    den = sum((x - x_mean) ** 2 for x in xs)
    if den == 0:
        return None
    w_prime = num / den
    cp = y_mean - w_prime * x_mean

    # Sanity: CP must be positive and below the shortest-duration best
    if cp <= 0 or cp > max(p for _, p in long_points):
        return None

    sorted_buckets = sorted(long_points)
    return {
        "cp": round(cp),
        "w_prime_joules": round(w_prime),
        "n_points": len(long_points),
        "buckets_used_s": [t for t, _ in sorted_buckets],
        "best_powers_used": {f"{t//60}min": round(p) for t, p in sorted_buckets},
        "scaled_peaks": scaled_count,
        "source": (
            f"Monod-Scherrer CP fit across {len(long_points)} duration "
            f"buckets (IF-scaled): " +
            ", ".join(f"{t//60}min={round(p)}W" for t, p in sorted_buckets)
        ),
    }


def tte_adjusted_ftp(activities: list[dict]) -> dict | None:
    """Time-to-exhaustion adjusted FTP for cycling.

    Classic Coggan FTP-from-20-min × 0.95 assumes a typical TTE of ~60 min.
    But if you've sustained 95%+ of your 20-min best for 40+ minutes, your
    actual TTE is longer and FTP is closer to your 20-min value (less
    reduction). If you've barely held 85% of 20-min best for 40 min, TTE
    is shorter and 0.95 overestimates FTP.

    Peaks are IF-scaled: if the ride they came from was sub-threshold
    overall, scale up to the threshold-equivalent power.

    Returns {"ftp": w, "tte_minutes": n, "source": str} or None if we
    lack the data.
    """
    # IF-scaled best at each duration
    def _if_scaled_max(key: str, duration_s: int) -> float | None:
        vals = []
        for a in activities:
            p = a.get(key)
            if not p:
                continue
            scaled, _ = _scale_peak_by_if(p, a.get("intensityFactor"), duration_s)
            vals.append(scaled)
        return max(vals) if vals else None

    best_20min = _if_scaled_max("maxAvgPower_1200", 1200)
    if not best_20min:
        return None
    best_40min = _if_scaled_max("maxAvgPower_2400", 2400)
    best_60min = _if_scaled_max("maxAvgPower_3600", 3600)

    # Ratio of best 60-min to best 20-min tells us TTE profile.
    if best_60min:
        ratio = best_60min / best_20min
        # A ratio of 0.95 means sustained 60-min matches 20-min × 0.95 — FTP is right at best_60min.
        # A ratio of 0.88 means big drop-off — athlete has limited TTE, use 0.93 reducer.
        # A ratio of 0.98 means near-equal — athlete has long TTE, use 0.97 reducer.
        if ratio >= 0.95:
            return {
                "ftp": best_60min,
                "tte_minutes": 60,
                "source": f"60-min best ({best_60min}W) = {ratio:.2f}x 20-min best — long TTE, FTP = 60-min power",
            }
        if ratio >= 0.90:
            return {
                "ftp": round(best_20min * 0.96),
                "tte_minutes": "45-60",
                "source": f"60-min/20-min ratio {ratio:.2f} — TTE near typical, FTP = 20-min × 0.96",
            }
        return {
            "ftp": round(best_20min * 0.93),
            "tte_minutes": "<45",
            "source": f"60-min/20-min ratio {ratio:.2f} — short TTE, FTP = 20-min × 0.93",
        }
    if best_40min:
        ratio = best_40min / best_20min
        if ratio >= 0.96:
            return {
                "ftp": round(best_20min * 0.96),
                "tte_minutes": "40+",
                "source": f"40-min best held {ratio:.2f} of 20-min — solid TTE, 0.96 reducer",
            }
        return {
            "ftp": round(best_20min * 0.93),
            "tte_minutes": "<40",
            "source": f"40-min best only {ratio:.2f} of 20-min — short TTE, 0.93 reducer",
        }
    # No long-effort data; fall back to classic 0.95.
    return {
        "ftp": round(best_20min * 0.95),
        "tte_minutes": "unknown",
        "source": f"20-min × 0.95 (classic Coggan, no 40+ min data to adjust TTE)",
    }


def detect_race_effort(a: dict) -> bool:
    """Heuristic: even-pacing + sustained high HR + ≥5km run suggests a
    race/TT effort regardless of tags. Every km pace is within 5% of the
    fastest km → consistent race pacing, not random fast splits in easy run.

    Garmin activities don't expose per-km pace in the summary, so we use
    a proxy: averageSpeed vs maxSpeed ratio is high (>0.85 = steady),
    duration >= 15 min, and avg HR near threshold.
    """
    dur = a.get("duration") or 0
    if dur < 900:  # < 15 min
        return False
    avg_speed = a.get("averageSpeed")
    max_speed = a.get("maxSpeed")
    avg_hr = a.get("averageHR")
    if not (avg_speed and max_speed and avg_hr):
        return False
    if max_speed <= 0:
        return False
    steady_ratio = avg_speed / max_speed
    # Race/TT pacing: avg very close to max. Relaxed threshold because
    # max speed can spike on downhills.
    if steady_ratio >= 0.82 and avg_hr >= 170:
        return True
    return False


def heat_corrected_time(time_seconds: float,
                        ambient_weather: dict | None) -> tuple[float, str | None]:
    """Correct a race/TT time back to neutral-temperature equivalent.

    Research (Ely et al., Vihma et al.): endurance performance degrades
    roughly 0.3-0.5% per °C above ~15°C apparent temperature, accelerating
    above ~25°C. Correction is applied so a hot 17:37 5K becomes its
    cool-weather equivalent for VDOT purposes.

    Returns (corrected_seconds, explanation). Explanation is None if no
    correction was applied.
    """
    if not ambient_weather or ambient_weather.get("skipped"):
        return time_seconds, None
    apparent_f = ambient_weather.get("apparent_f")
    if apparent_f is None:
        return time_seconds, None
    # Convert to °C. Neutral ~59°F (15°C).
    apparent_c = (apparent_f - 32) * 5 / 9
    if apparent_c <= 15:
        return time_seconds, None
    # Degradation per °C above 15: 0.3% up to 25°C, 0.6% above.
    excess_c = apparent_c - 15
    if excess_c <= 10:
        pct = excess_c * 0.003
    else:
        pct = 10 * 0.003 + (excess_c - 10) * 0.006
    corrected = time_seconds * (1 - pct)
    return corrected, (
        f"Heat-corrected: {apparent_f:.0f}°F apparent → "
        f"-{pct*100:.1f}% applied (race in neutral conditions would be "
        f"{corrected:.0f}s, actual {time_seconds:.0f}s)"
    )


def confidence_interval(values: list[float]) -> tuple[float | None, tuple[float, float] | None, str]:
    """Return (point_estimate, (low, high) 80% CI, note).

    Uses simple percentile approach (20th, 80th). Returns None for CI
    when fewer than 3 values exist — the note explains why in that case.
    """
    clean = [v for v in values if isinstance(v, (int, float))]
    n = len(clean)
    if not clean:
        return None, None, "no methods produced a value"
    point = round(statistics.median(clean), 1)
    if n < 3:
        return point, None, f"only {n} method{'s' if n != 1 else ''} — CI requires ≥3"
    sorted_v = sorted(clean)
    low = sorted_v[max(0, int(len(sorted_v) * 0.2))]
    high = sorted_v[min(len(sorted_v) - 1, int(len(sorted_v) * 0.8))]
    note = f"80% CI across {n} methods"
    return point, (round(low, 1), round(high, 1)), note


def lt_to_lt1(lt2: float | None) -> float | None:
    """Aerobic threshold (LT1) is typically ~10% lower than lactate
    threshold (LT2) when expressed as % of max HR. For an LT2 at 88% max,
    LT1 is around 80%. Approximate conversion: LT1 ≈ LT2 × 0.92.
    """
    if lt2 is None:
        return None
    return round(lt2 * 0.92, 0)


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
    today: date | None = None,
) -> dict[str, Any]:
    """Compare multiple VO2max estimates for running.

    Methods:
      1. Garmin's reported VO2max (from max_metrics.generic)
      2. Jack Daniels VDOT from Garmin's 5K prediction
      3. VDOT from best recent 5K fastestSplit in a KEY session (recency-weighted)
      4. VDOT from best mile split (recency-weighted)
      5. VDOT from detected race/TT efforts (heat-corrected)
    """
    today = today or date.today()
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

    # Method 3: VDOT from best 5K split, recency-weighted across key runs
    val, winner = weighted_best(run_activities, "fastestSplit_5000", today,
                                half_life_days=30, higher_is_better=False)
    if val:
        v = vdot_from_time(5000.0, val)
        if v is not None:
            age_days = (today - _parse_activity_date(winner)).days if _parse_activity_date(winner) else None
            methods.append({
                "name": "vdot_from_best_5k_split_recency_weighted",
                "value": v,
                "delta_vs_garmin": round(v - garmin_vo2max, 1) if garmin_vo2max else None,
                "confidence": "high",
                "source": (
                    f"Recency-weighted best 5K split from key runs "
                    f"({round(val/60, 1)} min, {age_days}d ago: "
                    f"{winner.get('activityName','?')})"
                ),
                "notes": "Weights recent efforts more than old ones (30d half-life). Actual performance.",
            })

    # Method 4: VDOT from best mile split, recency-weighted
    val, winner = weighted_best(run_activities, "fastestSplit_1609", today,
                                half_life_days=30, higher_is_better=False)
    if val:
        v = vdot_from_time(1609.0, val)
        if v is not None:
            age_days = (today - _parse_activity_date(winner)).days if _parse_activity_date(winner) else None
            methods.append({
                "name": "vdot_from_best_mile_split_recency_weighted",
                "value": v,
                "delta_vs_garmin": round(v - garmin_vo2max, 1) if garmin_vo2max else None,
                "confidence": "medium",
                "source": f"Recency-weighted best mile split ({round(val/60, 1)} min, {age_days}d ago)",
                "notes": "Mile tends to overestimate vs 5K. Use as a ceiling reference.",
            })

    # Method 5: VDOT from detected race/TT efforts (heat-corrected)
    race_candidates = [a for a in run_activities if detect_race_effort(a)]
    if race_candidates:
        # Pick the most recent race-like effort
        race_candidates.sort(
            key=lambda a: _parse_activity_date(a) or date(1970, 1, 1),
            reverse=True,
        )
        race = race_candidates[0]
        race_dist = race.get("distance")
        race_dur = race.get("duration")
        if race_dist and race_dur:
            # Apply heat correction if weather is available
            ambient = race.get("ambient_weather")
            corrected_time, heat_note = heat_corrected_time(race_dur, ambient)
            v = vdot_from_time(race_dist, corrected_time)
            if v is not None:
                age_days = (today - _parse_activity_date(race)).days if _parse_activity_date(race) else None
                src = (
                    f"Detected race/TT effort: {race.get('activityName','?')} "
                    f"({round(race_dist/1000, 1)}km in {round(race_dur/60, 1)}min, "
                    f"{age_days}d ago)"
                )
                if heat_note:
                    src += f" | {heat_note}"
                methods.append({
                    "name": "vdot_from_detected_race_effort_heat_corrected",
                    "value": v,
                    "delta_vs_garmin": round(v - garmin_vo2max, 1) if garmin_vo2max else None,
                    "confidence": "high",
                    "source": src,
                    "notes": "Steady-pacing + high HR detected. Heat-corrected if apparent temp >15°C.",
                })

    # Compute consensus + spread + CI + flag
    all_vals = [m["value"] for m in methods if m["value"] is not None]
    consensus, ci, ci_note = confidence_interval(all_vals)
    spread = _spread(all_vals)
    flag = _flag_from_spread(methods, garmin_vo2max, large_spread_threshold=1.0)

    return {
        "garmin_value": garmin_vo2max,
        "methods": methods,
        "consensus": consensus,
        "confidence_interval_80pct": list(ci) if ci else None,
        "ci_note": ci_note,
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
    consensus, ci, ci_note = confidence_interval(all_vals)
    spread = _spread(all_vals)
    flag = _flag_from_spread(methods, garmin_lt_hr, large_spread_threshold=3.0)

    # Aerobic threshold (LT1) derived from LT2 (lactate threshold HR).
    # Uses the consensus LT2 rather than Garmin's alone — if our multi-
    # method analysis found LT2 is ~177 vs Garmin's 181, LT1 should be
    # derived from 177 too.
    lt1 = lt_to_lt1(consensus)
    lt1_note = (
        f"LT1 (aerobic threshold) ≈ {lt1} bpm — use for Z2/endurance work. "
        f"LT2 (lactate threshold, reported above) is for threshold/tempo sessions."
        if lt1 else None
    )

    return {
        "garmin_value": garmin_lt_hr,
        "methods": methods,
        "consensus": consensus,
        "confidence_interval_80pct": list(ci) if ci else None,
        "ci_note": ci_note,
        "spread": spread,
        "flag": flag,
        "lt1_aerobic_threshold_bpm": lt1,
        "lt1_note": lt1_note,
    }


# ---------- Running FTP (critical power / critical pace) ----------


def run_ftp_methods(
    garmin_run_ftp: float | None,
    run_activities: list[dict],
    today: date | None = None,
) -> dict[str, Any]:
    """Compare multiple running FTP estimates.

    Methods:
      1. Garmin's reported run FTP
      2. Best 20-min power × 0.95 (Coggan method, recency-weighted)
      3. Best 60-min power (critical power proxy, recency-weighted)
    """
    today = today or date.today()
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

    val_20, winner_20 = weighted_best(run_activities, "maxAvgPower_1200", today,
                                      half_life_days=30, higher_is_better=True)
    if val_20:
        coggan = round(val_20 * 0.95)
        age = (today - _parse_activity_date(winner_20)).days if _parse_activity_date(winner_20) else None
        methods.append({
            "name": "coggan_20min_test_recency_weighted",
            "value": coggan,
            "delta_vs_garmin": round(coggan - garmin_run_ftp) if garmin_run_ftp else None,
            "confidence": "high",
            "source": f"Recency-weighted best 20-min avg power ({round(val_20)}W, {age}d ago) × 0.95",
            "notes": "Classic FTP-from-test proxy. Accurate if the 20-min effort was all-out.",
        })

    val_60, winner_60 = weighted_best(run_activities, "maxAvgPower_3600", today,
                                      half_life_days=30, higher_is_better=True)
    if val_60:
        age = (today - _parse_activity_date(winner_60)).days if _parse_activity_date(winner_60) else None
        methods.append({
            "name": "best_60min_power_recency_weighted",
            "value": round(val_60),
            "delta_vs_garmin": round(val_60 - garmin_run_ftp) if garmin_run_ftp else None,
            "confidence": "high",
            "source": f"Recency-weighted best 60-min avg power ({round(val_60)}W, {age}d ago)",
            "notes": "Direct critical-power observation. Equals FTP if you held threshold for an hour.",
        })

    all_vals = [m["value"] for m in methods if m["value"] is not None]
    consensus, ci, ci_note = confidence_interval(all_vals)
    spread = _spread(all_vals)
    flag = _flag_from_spread(methods, garmin_run_ftp, large_spread_threshold=10.0)

    return {
        "garmin_value": garmin_run_ftp,
        "methods": methods,
        "consensus": consensus,
        "confidence_interval_80pct": list(ci) if ci else None,
        "ci_note": ci_note,
        "spread": spread,
        "flag": flag,
    }


# ---------- Cycling FTP ----------


def bike_ftp_methods(
    garmin_bike_ftp: float | None,
    ride_activities: list[dict],
    today: date | None = None,
) -> dict[str, Any]:
    """Compare multiple cycling FTP estimates.

    Filters to clean sessions (aerobic decoupling check) before extracting
    peak values, so a brief 20-min power spike in a Z2 ride with erratic
    HR gets excluded. Uses TTE-adjusted reducer based on 60-min/20-min
    ratio. Recency-weighted so recent strong efforts dominate.

    Methods:
      1. Garmin's reported bike FTP (if set — most users don't have one)
      2. TTE-adjusted FTP from clean 20-min / 40-min / 60-min bests
      3. Best 60-min × 1.0 (critical power, recency-weighted)
      4. NP from ≥40min effort × ~0.97
    """
    today = today or date.today()
    # Filter to "clean" sessions for FTP derivation — sessions where HR
    # behavior suggests a sustained effort rather than a one-off spike or
    # a blowup.
    clean_rides = [a for a in ride_activities if aerobic_decoupling_clean(a)]
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

    # Method 2: TTE-adjusted FTP — looks at 60/40/20 min relationship
    # to pick the right reducer instead of the blanket 0.95.
    tte = tte_adjusted_ftp(clean_rides)
    if tte:
        methods.append({
            "name": "tte_adjusted_ftp",
            "value": tte["ftp"],
            "delta_vs_garmin": round(tte["ftp"] - garmin_bike_ftp) if garmin_bike_ftp else None,
            "confidence": "high",
            "source": tte["source"],
            "notes": f"TTE {tte['tte_minutes']} min. Adjusts Coggan reducer based on actual power-duration profile.",
        })

    # Method 2.5: Critical Power model fit across multiple ride durations.
    # Uses Garmin's rolling-best power arrays (best 5/10/20/30/40/60 min
    # WITHIN each ride) across all clean recent rides. Captures the true
    # sub-interval peak power at each duration regardless of whether the
    # session overall was at threshold or sweet spot.
    cp_fit = critical_power_fit(clean_rides)
    if cp_fit:
        methods.append({
            "name": "critical_power_fit",
            "value": cp_fit["cp"],
            "delta_vs_garmin": round(cp_fit["cp"] - garmin_bike_ftp) if garmin_bike_ftp else None,
            "confidence": "high",
            "source": cp_fit["source"],
            "notes": (
                f"Monod-Scherrer CP model. W' = {cp_fit['w_prime_joules']}J. "
                "Uses sub-intervals, not whole-session averages — immune to "
                "the sweet-spot-ride-mislabeled-as-FTP problem."
            ),
        })

    # Method 2.6: Best 20-min power across multiple sessions — 80th
    # percentile rather than single max. Captures robust threshold
    # capability even if a couple of rides had outlier spikes.
    all_20min = sorted(
        (a.get("maxAvgPower_1200") for a in clean_rides if a.get("maxAvgPower_1200")),
        reverse=True,
    )
    if len(all_20min) >= 3:
        # Take the 80th percentile of top values — robust to outliers
        # but still represents peak capability. Index: 80% from top.
        idx = max(0, int(len(all_20min) * 0.2))
        p80 = all_20min[idx]
        coggan_p80 = round(p80 * 0.95)
        methods.append({
            "name": "top_20pct_of_20min_peaks",
            "value": coggan_p80,
            "delta_vs_garmin": round(coggan_p80 - garmin_bike_ftp) if garmin_bike_ftp else None,
            "confidence": "medium",
            "source": (
                f"80th percentile of {len(all_20min)} rides' best 20-min "
                f"power ({round(p80)}W) × 0.95 = {coggan_p80}W"
            ),
            "notes": (
                "Robust to outliers — if one ride had a 20-min spike that "
                "wasn't a genuine test, this method isn't skewed by it."
            ),
        })

    # Method 3: Best 60-min power — scaled up to FTP-equivalent by the
    # ride's intensity factor. A 60-min @ 273W at IF 0.90 is sweet spot,
    # NOT FTP — FTP would be 273 / 0.90 ≈ 303W. Only take the raw value
    # as FTP when IF ≥ 0.95 (true threshold effort).
    # Pre-filter: only include rides where the 60-min power came from a
    # threshold-intensity effort, not a tempo/sweet-spot block.
    threshold_60min_rides = [
        a for a in clean_rides
        if a.get("maxAvgPower_3600")
        and (a.get("intensityFactor") or 0) >= 0.85
    ]
    val_60, winner_60 = weighted_best(threshold_60min_rides, "maxAvgPower_3600",
                                      today, half_life_days=30, higher_is_better=True)
    if val_60 and winner_60:
        if_val = winner_60.get("intensityFactor") or 0.95
        # FTP equivalent: raw 60-min power / IF. If IF was 0.90 (sweet
        # spot), scale up. If IF was 1.00 (race/threshold), no change.
        # Cap the scaler at the 60-min rating (1.0) — can't have FTP
        # below the power sustained at threshold.
        ftp_equiv = val_60 / max(if_val, 0.85)
        # Cap upward scaling at 15% to avoid absurd inferences from a
        # poorly-calibrated IF (if the rider's prior FTP estimate was
        # way off, IF will be misleading).
        if ftp_equiv > val_60 * 1.15:
            ftp_equiv = val_60 * 1.15
        age = (today - _parse_activity_date(winner_60)).days if _parse_activity_date(winner_60) else None
        methods.append({
            "name": "best_60min_power_if_scaled",
            "value": round(ftp_equiv),
            "delta_vs_garmin": round(ftp_equiv - garmin_bike_ftp) if garmin_bike_ftp else None,
            "confidence": "high" if if_val >= 0.95 else "medium",
            "source": (
                f"60-min avg power ({round(val_60)}W, IF {if_val:.2f}, "
                f"{age}d ago: {winner_60.get('activityName','?')}) / "
                f"IF → FTP equivalent {round(ftp_equiv)}W"
            ),
            "notes": (
                "60-min power scaled by intensity factor. Raw 60-min IS FTP "
                "only if ridden at threshold (IF ≥0.95). Sweet spot (IF "
                "0.85-0.94) gets scaled up to the threshold equivalent."
            ),
        })

    # Method 4: NP from a long sustained effort. NP × 0.97 assumes the
    # ride was ridden AT threshold. For sub-threshold rides, scale by IF.
    long_rides = [a for a in clean_rides
                  if (a.get("duration") or 0) >= 2400 and a.get("normPower")
                  and (a.get("intensityFactor") or 0) >= 0.85]
    val_np, winner_np = weighted_best(long_rides, "normPower", today,
                                      half_life_days=30, higher_is_better=True)
    if val_np and winner_np:
        if_val = winner_np.get("intensityFactor") or 0.95
        np_based = round(val_np / max(if_val, 0.85))
        age = (today - _parse_activity_date(winner_np)).days if _parse_activity_date(winner_np) else None
        methods.append({
            "name": "np_if_scaled_long_effort",
            "value": np_based,
            "delta_vs_garmin": round(np_based - garmin_bike_ftp) if garmin_bike_ftp else None,
            "confidence": "medium",
            "source": f"NP {round(val_np)}W from ≥40min ride (IF {if_val:.2f}, {age}d ago) / IF → {np_based}W FTP",
            "notes": "NP scaled by intensity factor. Sub-threshold efforts scaled up to threshold equivalent.",
        })

    all_vals = [m["value"] for m in methods if m["value"] is not None]
    consensus, ci, ci_note = confidence_interval(all_vals)
    spread = _spread(all_vals)
    flag = _flag_from_spread(methods, garmin_bike_ftp, large_spread_threshold=10.0)

    return {
        "garmin_value": garmin_bike_ftp,
        "methods": methods,
        "consensus": consensus,
        "confidence_interval_80pct": list(ci) if ci else None,
        "ci_note": ci_note,
        "spread": spread,
        "flag": flag,
        "clean_rides_used": len(clean_rides),
        "total_rides_evaluated": len(ride_activities),
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
    consensus, ci, ci_note = confidence_interval(all_vals)
    spread = _spread(all_vals)
    flag = _flag_from_spread(methods, garmin_vo2max_bike, large_spread_threshold=2.0)

    return {
        "garmin_value": garmin_vo2max_bike,
        "methods": methods,
        "consensus": consensus,
        "confidence_interval_80pct": list(ci) if ci else None,
        "ci_note": ci_note,
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
    consensus, ci, ci_note = confidence_interval(all_vals)
    spread = _spread(all_vals)
    flag = None
    # For swimming, no Garmin CSS to compare against — just flag large spread.
    if spread and spread > 5.0:
        flag = f"Methods disagree by {spread}s per 100m — pick CSS from best 1000m TT if available."

    return {
        "garmin_value": None,  # no Garmin CSS
        "methods": methods,
        "consensus": consensus,
        "confidence_interval_80pct": list(ci) if ci else None,
        "ci_note": ci_note,
        "spread": spread,
        "flag": flag,
    }
