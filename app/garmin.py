"""Garmin Connect client wrapper.

Auth model:
  1. One-time bootstrap locally (see scripts/bootstrap.py). User completes MFA
     interactively. `garminconnect` writes Garth OAuth tokens to a dir.
  2. For deployment, those tokens are base64-encoded and stored in
     GARTH_TOKENS_B64. At startup we decode them to a temp dir and hand that to
     garth. The tokens auto-refresh internally for ~1 year; no password needed
     at runtime.
"""
from __future__ import annotations

import base64
import io
import os
import random
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, Optional

from garminconnect import Garmin

from . import cache

_client: Optional[Garmin] = None
_lock = Lock()

MAX_RANGE_DAYS = 366
FAN_OUT_WORKERS = 2
JITTER_MIN_SEC = 0.1
JITTER_MAX_SEC = 0.25
RATE_LIMIT_MAX_RETRIES = 4
RATE_LIMIT_BASE_DELAY_SEC = 2.0


class GarminError(Exception):
    pass


class GarminAuthError(GarminError):
    pass


class GarminRateLimitError(GarminError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class GarminNotFoundError(GarminError):
    pass


def _classify_exception(exc: BaseException) -> GarminError | None:
    msg = str(exc).lower()
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if "429" in msg or "too many requests" in msg or "rate limit" in msg or status == 429:
        retry_after = getattr(exc, "retry_after", None)
        try:
            retry_after = float(retry_after) if retry_after is not None else None
        except (TypeError, ValueError):
            retry_after = None
        return GarminRateLimitError(str(exc), retry_after=retry_after)
    if "401" in msg or "unauthorized" in msg or status == 401:
        return GarminAuthError(str(exc))
    if "404" in msg or "not found" in msg or status == 404:
        return GarminNotFoundError(str(exc))
    return None

# Whitelist of per-date methods we expose via get_daily_summaries.
DAILY_METHODS: dict[str, str] = {
    "steps": "get_steps_data",
    "sleep": "get_sleep_data",
    "stress": "get_all_day_stress",
    "body_battery_events": "get_body_battery_events",
    "hrv": "get_hrv_data",
    "rhr": "get_rhr_day",
    "respiration": "get_respiration_data",
    "training_readiness": "get_training_readiness",
    "training_status": "get_training_status",
    "stats": "get_stats",
    "stats_and_body": "get_stats_and_body",
    "user_summary": "get_user_summary",
    "max_metrics": "get_max_metrics",
    "floors": "get_floors",
    "intensity_minutes": "get_intensity_minutes_data",
    "heart_rates": "get_heart_rates",
    "morning_readiness": "get_morning_training_readiness",
    "fitness_age": "get_fitnessage_data",
    "hydration": "get_hydration_data",
    "spo2": "get_spo2_data",
    "all_day_events": "get_all_day_events",
}


def _tokens_dir_from_env() -> str:
    b64 = os.environ.get("GARTH_TOKENS_B64")
    if not b64:
        raise RuntimeError(
            "GARTH_TOKENS_B64 is not set. Run scripts/bootstrap.py locally "
            "first, then copy the printed value into your environment."
        )
    tmp = tempfile.mkdtemp(prefix="garth-")
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(b64)), mode="r:gz") as tf:
        tf.extractall(tmp)  # noqa: S202 (we created the archive ourselves)
    return tmp


def get_client() -> Garmin:
    global _client
    with _lock:
        if _client is not None:
            return _client
        tokens_dir = _tokens_dir_from_env()
        client = Garmin()
        client.garth.sess.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        })
        client.login(tokens_dir)
        _client = client
        return client


def _coerce_date(d: str | date) -> date:
    if isinstance(d, date):
        return d
    return datetime.strptime(d, "%Y-%m-%d").date()


def _validate_range(start: str | date, end: str | date) -> tuple[date, date]:
    s = _coerce_date(start)
    e = _coerce_date(end)
    if e < s:
        raise ValueError("enddate must be >= startdate")
    span = (e - s).days + 1
    if span > MAX_RANGE_DAYS:
        raise ValueError(
            f"date range is {span} days; max is {MAX_RANGE_DAYS}. "
            "Call repeatedly with smaller windows."
        )
    return s, e


def _daterange(s: date, e: date) -> list[str]:
    n = (e - s).days + 1
    return [(s + timedelta(days=i)).isoformat() for i in range(n)]


def _call_with_backoff(fn, *args, **kwargs):
    """Run `fn` with jittered delay + exponential backoff on 429s.

    Raises GarminRateLimitError / GarminAuthError / GarminNotFoundError for
    classified errors after exhausting retries; re-raises other exceptions.
    """
    time.sleep(random.uniform(JITTER_MIN_SEC, JITTER_MAX_SEC))
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as ex:  # noqa: BLE001
            classified = _classify_exception(ex)
            if isinstance(classified, GarminRateLimitError) and attempt < RATE_LIMIT_MAX_RETRIES:
                delay = classified.retry_after if classified.retry_after is not None else RATE_LIMIT_BASE_DELAY_SEC * (2 ** attempt)
                delay += random.uniform(0, 0.5)
                time.sleep(delay)
                attempt += 1
                continue
            if classified is not None:
                raise classified from ex
            raise


# ---------- single-day (legacy) ----------


def get_activities(start: int = 0, limit: int = 10):
    return _call_with_backoff(get_client().get_activities, start, limit)


# ---------- bulk / historical ----------


def get_activities_in_range(
    startdate: str | date,
    enddate: str | date,
    activity_type: str | None = None,
    force_refresh: bool = False,
):
    s, e = _validate_range(startdate, enddate)
    args = {"start": s.isoformat(), "end": e.isoformat(), "type": activity_type}
    if not force_refresh:
        hit = cache.get("activities_in_range", args)
        if hit is not None:
            return hit
    data = _call_with_backoff(
        get_client().get_activities_by_date,
        s.isoformat(),
        e.isoformat(),
        activity_type,
    )
    cache.put("activities_in_range", args, data)
    return data


def get_activity_details(activity_id: str | int, force_refresh: bool = False) -> dict[str, Any]:
    aid = str(activity_id)
    if not force_refresh:
        hit = cache.get("activity_details", {"activity_id": aid})
        if hit is not None:
            return hit
    c = get_client()
    out: dict[str, Any] = {}
    # Some of these throw for certain activity types — capture errors per field.
    for key, call in [
        ("summary", lambda: c.get_activity(aid)),
        ("splits", lambda: c.get_activity_splits(aid)),
        ("hr_zones", lambda: c.get_activity_hr_in_timezones(aid)),
        ("weather", lambda: c.get_activity_weather(aid)),
        ("gear", lambda: c.get_activity_gear(aid)),
    ]:
        try:
            out[key] = _call_with_backoff(call)
        except Exception as ex:  # noqa: BLE001
            out[key] = {"error": str(ex)}
    cache.put("activity_details", {"activity_id": aid}, out)
    return out


def get_personal_records():
    return _call_with_backoff(get_client().get_personal_record)


def get_race_predictions(
    startdate: str | date | None = None, enddate: str | date | None = None
):
    c = get_client()
    if startdate and enddate:
        s, e = _validate_range(startdate, enddate)
        return _call_with_backoff(c.get_race_predictions, s.isoformat(), e.isoformat())
    return _call_with_backoff(c.get_race_predictions)


def get_body_composition(startdate: str | date, enddate: str | date | None = None):
    s = _coerce_date(startdate)
    if enddate:
        _, _ = _validate_range(startdate, enddate)
        return _call_with_backoff(
            get_client().get_body_composition, s.isoformat(), _coerce_date(enddate).isoformat()
        )
    return _call_with_backoff(get_client().get_body_composition, s.isoformat())


def get_training_score(
    metric: str,
    startdate: str | date,
    enddate: str | date | None = None,
):
    """Hill or endurance training score. Single date or range (max 366 days)."""
    methods = {"hill": "get_hill_score", "endurance": "get_endurance_score"}
    if metric not in methods:
        raise ValueError(f"metric must be one of {sorted(methods)}")
    fn = getattr(get_client(), methods[metric])
    s = _coerce_date(startdate)
    if enddate:
        _, _ = _validate_range(startdate, enddate)
        return _call_with_backoff(fn, s.isoformat(), _coerce_date(enddate).isoformat())
    return _call_with_backoff(fn, s.isoformat())


def get_lactate_threshold(
    startdate: str | date | None = None,
    enddate: str | date | None = None,
    aggregation: str = "daily",
):
    c = get_client()
    if startdate and enddate:
        s, e = _validate_range(startdate, enddate)
        return _call_with_backoff(
            c.get_lactate_threshold,
            latest=False,
            start_date=s.isoformat(),
            end_date=e.isoformat(),
            aggregation=aggregation,
        )
    return _call_with_backoff(c.get_lactate_threshold, latest=True)


def get_progress_summary(
    startdate: str | date,
    enddate: str | date,
    metric: str = "distance",
    group_by_activities: bool = True,
):
    s, e = _validate_range(startdate, enddate)
    return _call_with_backoff(
        get_client().get_progress_summary_between_dates,
        s.isoformat(),
        e.isoformat(),
        metric,
        group_by_activities,
    )


def get_weekly_summaries(
    enddate: str | date,
    weeks: int = 52,
    metrics: list[str] | None = None,
) -> dict[str, Any]:
    """Weekly aggregates (steps / stress / intensity_minutes).

    intensity_minutes uses a (start, end) range under the hood — we derive
    start as (enddate - weeks*7 days) to stay consistent.
    """
    if weeks < 1 or weeks > 104:
        raise ValueError("weeks must be between 1 and 104")
    end = _coerce_date(enddate)
    metrics = metrics or ["steps", "stress", "intensity_minutes"]
    allowed = {"steps", "stress", "intensity_minutes"}
    unknown = [m for m in metrics if m not in allowed]
    if unknown:
        raise ValueError(f"unknown weekly metrics: {unknown}. Supported: {sorted(allowed)}")

    c = get_client()
    out: dict[str, Any] = {}
    for m in metrics:
        try:
            if m == "steps":
                out[m] = _call_with_backoff(c.get_weekly_steps, end.isoformat(), weeks)
            elif m == "stress":
                out[m] = _call_with_backoff(c.get_weekly_stress, end.isoformat(), weeks)
            elif m == "intensity_minutes":
                start = end - timedelta(days=weeks * 7)
                out[m] = _call_with_backoff(
                    c.get_weekly_intensity_minutes, start.isoformat(), end.isoformat()
                )
        except Exception as ex:  # noqa: BLE001
            out[m] = {"error": str(ex)}
    return out


def get_devices():
    return _call_with_backoff(get_client().get_devices)


# ---------- planned training ----------


def get_workouts(start: int = 0, limit: int = 100):
    """List saved/custom workouts in the user's library."""
    limit = max(1, min(int(limit), 100))
    return _call_with_backoff(get_client().get_workouts, int(start), limit)


def get_workout_by_id(workout_id: str | int):
    return _call_with_backoff(get_client().get_workout_by_id, str(workout_id))


def get_training_plans():
    """Active and available training plans (Garmin Coach + custom)."""
    return _call_with_backoff(get_client().get_training_plans)


def get_training_plan_by_id(plan_id: str | int, adaptive: bool = False):
    c = get_client()
    if adaptive:
        return _call_with_backoff(c.get_adaptive_training_plan_by_id, str(plan_id))
    return _call_with_backoff(c.get_training_plan_by_id, str(plan_id))




def get_daily_summaries(
    startdate: str | date,
    enddate: str | date,
    metrics: list[str],
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Fan out one or more per-day Garmin endpoints across a date range.

    Returns: { metric: { date: data_or_error, ... }, ... }

    Caching: per (metric, date) cached in S3 (if configured). Set
    `force_refresh=True` to bypass the cache.
    """
    if not metrics:
        raise ValueError("metrics must be a non-empty list")
    unknown = [m for m in metrics if m not in DAILY_METHODS]
    if unknown:
        raise ValueError(
            f"unknown metrics: {unknown}. Supported: {sorted(DAILY_METHODS)}"
        )
    s, e = _validate_range(startdate, enddate)
    dates = _daterange(s, e)

    client = get_client()
    result: dict[str, dict[str, Any]] = {m: {} for m in metrics}

    tasks: list[tuple[str, str]] = []
    for m in metrics:
        for d in dates:
            if not force_refresh:
                hit = cache.get("daily_summary", {"metric": m, "date": d})
                if hit is not None:
                    result[m][d] = hit
                    continue
            tasks.append((m, d))

    def _one(metric: str, d: str) -> tuple[str, str, Any]:
        method = getattr(client, DAILY_METHODS[metric])
        try:
            data = _call_with_backoff(method, d)
            cache.put("daily_summary", {"metric": metric, "date": d}, data)
            return metric, d, data
        except Exception as ex:  # noqa: BLE001
            return metric, d, {"error": str(ex)}

    if tasks:
        with ThreadPoolExecutor(max_workers=FAN_OUT_WORKERS) as pool:
            futures = [pool.submit(_one, m, d) for m, d in tasks]
            for f in as_completed(futures):
                m, d, data = f.result()
                result[m][d] = data

    return result


# ---------- aggregation / analysis ----------


def _safe_num(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _meters_to_miles(m: float | None) -> float | None:
    return round(m * 0.000621371, 2) if m is not None else None


def _seconds_to_hours(s: float | None) -> float | None:
    return round(s / 3600, 2) if s is not None else None


def analyze_training_period(
    startdate: str | date,
    enddate: str | date,
) -> dict[str, Any]:
    """Summarize activities across a date range into training-load stats.

    Returns totals, averages, per-activity-type breakdowns, and a weekly
    timeline — pre-computed so the LLM doesn't have to crunch raw activity JSON.
    """
    s, e = _validate_range(startdate, enddate)
    acts = _call_with_backoff(
        get_client().get_activities_by_date, s.isoformat(), e.isoformat(), None
    ) or []

    totals = {"count": len(acts), "distance_mi": 0.0, "duration_hr": 0.0, "calories": 0, "elevation_gain_m": 0.0}
    by_type: dict[str, dict[str, Any]] = {}
    weekly: dict[str, dict[str, Any]] = {}

    for a in acts:
        atype = (a.get("activityType") or {}).get("typeKey") or "unknown"
        dist_m = _safe_num(a.get("distance")) or 0.0
        dur_s = _safe_num(a.get("duration")) or 0.0
        cal = _safe_num(a.get("calories")) or 0.0
        elev = _safe_num(a.get("elevationGain")) or 0.0
        hr_avg = _safe_num(a.get("averageHR"))

        totals["distance_mi"] += dist_m * 0.000621371
        totals["duration_hr"] += dur_s / 3600
        totals["calories"] += cal
        totals["elevation_gain_m"] += elev

        b = by_type.setdefault(atype, {"count": 0, "distance_mi": 0.0, "duration_hr": 0.0, "calories": 0, "avg_hr_samples": []})
        b["count"] += 1
        b["distance_mi"] += dist_m * 0.000621371
        b["duration_hr"] += dur_s / 3600
        b["calories"] += cal
        if hr_avg is not None:
            b["avg_hr_samples"].append(hr_avg)

        start_ts = a.get("startTimeLocal") or a.get("startTimeGMT")
        if start_ts:
            try:
                d = datetime.fromisoformat(str(start_ts).replace("Z", "+00:00")).date()
            except ValueError:
                d = None
            if d:
                iso_year, iso_week, _ = d.isocalendar()
                wk = f"{iso_year}-W{iso_week:02d}"
                w = weekly.setdefault(wk, {"count": 0, "distance_mi": 0.0, "duration_hr": 0.0})
                w["count"] += 1
                w["distance_mi"] += dist_m * 0.000621371
                w["duration_hr"] += dur_s / 3600

    for b in by_type.values():
        samples = b.pop("avg_hr_samples")
        b["avg_hr"] = round(sum(samples) / len(samples), 1) if samples else None
        b["distance_mi"] = round(b["distance_mi"], 2)
        b["duration_hr"] = round(b["duration_hr"], 2)
        b["calories"] = int(b["calories"])

    for w in weekly.values():
        w["distance_mi"] = round(w["distance_mi"], 2)
        w["duration_hr"] = round(w["duration_hr"], 2)

    totals["distance_mi"] = round(totals["distance_mi"], 2)
    totals["duration_hr"] = round(totals["duration_hr"], 2)
    totals["calories"] = int(totals["calories"])
    totals["elevation_gain_m"] = round(totals["elevation_gain_m"], 1)

    return {
        "range": {"start": s.isoformat(), "end": e.isoformat(), "days": (e - s).days + 1},
        "totals": totals,
        "by_activity_type": by_type,
        "weekly": dict(sorted(weekly.items())),
    }


def compare_activities(activity_ids: list[str | int]) -> dict[str, Any]:
    """Side-by-side comparison of 2–10 activities.

    Pulls the summary for each id and emits normalized rows plus deltas from
    the first activity (the "baseline") for common fields.
    """
    if not (2 <= len(activity_ids) <= 10):
        raise ValueError("activity_ids must have 2-10 entries")

    c = get_client()
    rows: list[dict[str, Any]] = []
    for aid in activity_ids:
        try:
            a = _call_with_backoff(c.get_activity, str(aid))
        except Exception as ex:  # noqa: BLE001
            rows.append({"activity_id": str(aid), "error": str(ex)})
            continue
        rows.append({
            "activity_id": str(aid),
            "type": (a.get("activityType") or {}).get("typeKey"),
            "start_time_local": a.get("startTimeLocal"),
            "distance_mi": _meters_to_miles(_safe_num(a.get("distance"))),
            "duration_hr": _seconds_to_hours(_safe_num(a.get("duration"))),
            "moving_time_hr": _seconds_to_hours(_safe_num(a.get("movingDuration"))),
            "avg_hr": _safe_num(a.get("averageHR")),
            "max_hr": _safe_num(a.get("maxHR")),
            "avg_pace_min_per_mi": (
                round(((_safe_num(a.get("averageSpeed")) or 0) ** -1) * 26.8224, 2)
                if _safe_num(a.get("averageSpeed")) else None
            ),
            "calories": _safe_num(a.get("calories")),
            "elevation_gain_m": _safe_num(a.get("elevationGain")),
            "training_effect_aerobic": _safe_num(a.get("aerobicTrainingEffect")),
            "training_effect_anaerobic": _safe_num(a.get("anaerobicTrainingEffect")),
        })

    baseline = rows[0]
    deltas = []
    numeric_keys = [
        "distance_mi", "duration_hr", "moving_time_hr", "avg_hr", "max_hr",
        "avg_pace_min_per_mi", "calories", "elevation_gain_m",
        "training_effect_aerobic", "training_effect_anaerobic",
    ]
    for row in rows[1:]:
        if "error" in row:
            deltas.append({"activity_id": row["activity_id"], "error": row["error"]})
            continue
        d = {"activity_id": row["activity_id"]}
        for k in numeric_keys:
            b = baseline.get(k)
            v = row.get(k)
            if b is None or v is None:
                d[k] = None
            else:
                d[k] = round(v - b, 2)
        deltas.append(d)

    return {"rows": rows, "baseline_id": baseline.get("activity_id"), "deltas_vs_baseline": deltas}


def analyze_sleep_trend(enddate: str | date, days: int = 30) -> dict[str, Any]:
    """Summarize sleep over the last N days: avg duration, stages, scores + simple trend."""
    if not (1 <= days <= 180):
        raise ValueError("days must be between 1 and 180")
    end = _coerce_date(enddate)
    start = end - timedelta(days=days - 1)
    _, _ = _validate_range(start, end)
    raw = get_daily_summaries(start, end, ["sleep"])["sleep"]

    durations_hr: list[float] = []
    scores: list[float] = []
    deep_hr: list[float] = []
    light_hr: list[float] = []
    rem_hr: list[float] = []
    awake_hr: list[float] = []
    daily: list[dict[str, Any]] = []

    for d_iso in sorted(raw.keys()):
        entry = raw[d_iso]
        if not isinstance(entry, dict) or "error" in entry:
            daily.append({"date": d_iso, "error": entry.get("error") if isinstance(entry, dict) else "no data"})
            continue
        dto = entry.get("dailySleepDTO") or entry.get("sleepDTO") or {}
        dur_s = _safe_num(dto.get("sleepTimeSeconds"))
        deep_s = _safe_num(dto.get("deepSleepSeconds"))
        light_s = _safe_num(dto.get("lightSleepSeconds"))
        rem_s = _safe_num(dto.get("remSleepSeconds"))
        awake_s = _safe_num(dto.get("awakeSleepSeconds"))
        score = _safe_num(((dto.get("sleepScores") or {}).get("overall") or {}).get("value"))

        if dur_s is not None:
            durations_hr.append(dur_s / 3600)
        if score is not None:
            scores.append(score)
        if deep_s is not None:
            deep_hr.append(deep_s / 3600)
        if light_s is not None:
            light_hr.append(light_s / 3600)
        if rem_s is not None:
            rem_hr.append(rem_s / 3600)
        if awake_s is not None:
            awake_hr.append(awake_s / 3600)

        daily.append({
            "date": d_iso,
            "duration_hr": round(dur_s / 3600, 2) if dur_s is not None else None,
            "score": score,
            "deep_hr": round(deep_s / 3600, 2) if deep_s is not None else None,
            "light_hr": round(light_s / 3600, 2) if light_s is not None else None,
            "rem_hr": round(rem_s / 3600, 2) if rem_s is not None else None,
            "awake_hr": round(awake_s / 3600, 2) if awake_s is not None else None,
        })

    def _avg(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 2) if xs else None

    half = len(durations_hr) // 2
    first_half_avg = _avg(durations_hr[:half]) if half else None
    second_half_avg = _avg(durations_hr[half:]) if half else None
    trend = None
    if first_half_avg is not None and second_half_avg is not None:
        diff = round(second_half_avg - first_half_avg, 2)
        trend = {"first_half_avg_hr": first_half_avg, "second_half_avg_hr": second_half_avg, "delta_hr": diff}

    return {
        "range": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "averages": {
            "duration_hr": _avg(durations_hr),
            "score": _avg(scores),
            "deep_hr": _avg(deep_hr),
            "light_hr": _avg(light_hr),
            "rem_hr": _avg(rem_hr),
            "awake_hr": _avg(awake_hr),
        },
        "trend": trend,
        "daily": daily,
    }


# ---------- MCP resources ----------


def resource_athlete_profile() -> dict[str, Any]:
    c = get_client()
    try:
        profile = _call_with_backoff(c.get_user_profile) or {}
    except Exception as ex:  # noqa: BLE001
        profile = {"error": str(ex)}
    try:
        settings = _call_with_backoff(c.get_userprofile_settings) or {}
    except Exception as ex:  # noqa: BLE001
        settings = {"error": str(ex)}
    try:
        full_name = _call_with_backoff(c.get_full_name)
    except Exception as ex:  # noqa: BLE001
        full_name = {"error": str(ex)}
    try:
        unit_system = _call_with_backoff(c.get_unit_system)
    except Exception as ex:  # noqa: BLE001
        unit_system = {"error": str(ex)}

    return {
        "full_name": full_name,
        "unit_system": unit_system,
        "profile": profile,
        "settings": settings,
    }


def resource_today_summary() -> dict[str, Any]:
    c = get_client()
    today = date.today().isoformat()
    out: dict[str, Any] = {"date": today}
    for key, call in [
        ("stats", lambda: c.get_stats(today)),
        ("steps", lambda: c.get_steps_data(today)),
        ("sleep", lambda: c.get_sleep_data(today)),
        ("heart_rates", lambda: c.get_heart_rates(today)),
        ("body_battery_events", lambda: c.get_body_battery_events(today)),
    ]:
        try:
            out[key] = _call_with_backoff(call)
        except Exception as ex:  # noqa: BLE001
            out[key] = {"error": str(ex)}
    return out


def resource_training_readiness() -> dict[str, Any]:
    c = get_client()
    today = date.today().isoformat()
    out: dict[str, Any] = {"date": today}
    for key, call in [
        ("training_readiness", lambda: c.get_training_readiness(today)),
        ("training_status", lambda: c.get_training_status(today)),
        ("hrv", lambda: c.get_hrv_data(today)),
        ("morning_readiness", lambda: c.get_morning_training_readiness(today)),
    ]:
        try:
            out[key] = _call_with_backoff(call)
        except Exception as ex:  # noqa: BLE001
            out[key] = {"error": str(ex)}
    return out
