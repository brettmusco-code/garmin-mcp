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
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, Optional

from garminconnect import Garmin

_client: Optional[Garmin] = None
_lock = Lock()

MAX_RANGE_DAYS = 90
FAN_OUT_WORKERS = 5

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
    "user_summary": "get_user_summary",
    "max_metrics": "get_max_metrics",
    "floors": "get_floors",
    "intensity_minutes": "get_intensity_minutes_data",
    "heart_rates": "get_heart_rates",
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


# ---------- single-day (legacy) ----------


def get_activities(start: int = 0, limit: int = 10):
    return get_client().get_activities(start, limit)


def get_steps(d: str | date):
    return get_client().get_steps_data(_coerce_date(d).isoformat())


def get_sleep(d: str | date):
    return get_client().get_sleep_data(_coerce_date(d).isoformat())


def get_heart_rate(d: str | date):
    return get_client().get_heart_rates(_coerce_date(d).isoformat())


def get_user_info():
    return get_client().get_user_profile()


# ---------- bulk / historical ----------


def get_activities_in_range(
    startdate: str | date,
    enddate: str | date,
    activity_type: str | None = None,
):
    s, e = _validate_range(startdate, enddate)
    return get_client().get_activities_by_date(
        s.isoformat(), e.isoformat(), activity_type
    )


def get_activity_details(activity_id: str | int) -> dict[str, Any]:
    c = get_client()
    aid = str(activity_id)
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
            out[key] = call()
        except Exception as ex:  # noqa: BLE001
            out[key] = {"error": str(ex)}
    return out


def get_body_battery_range(startdate: str | date, enddate: str | date):
    s, e = _validate_range(startdate, enddate)
    return get_client().get_body_battery(s.isoformat(), e.isoformat())


def get_personal_records():
    return get_client().get_personal_record()


def get_race_predictions(
    startdate: str | date | None = None, enddate: str | date | None = None
):
    c = get_client()
    if startdate and enddate:
        s, e = _validate_range(startdate, enddate)
        return c.get_race_predictions(s.isoformat(), e.isoformat())
    return c.get_race_predictions()


def get_daily_summaries(
    startdate: str | date,
    enddate: str | date,
    metrics: list[str],
) -> dict[str, Any]:
    """Fan out one or more per-day Garmin endpoints across a date range.

    Returns: { metric: { date: data_or_error, ... }, ... }
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

    tasks: list[tuple[str, str]] = [(m, d) for m in metrics for d in dates]

    def _one(metric: str, d: str) -> tuple[str, str, Any]:
        method = getattr(client, DAILY_METHODS[metric])
        try:
            return metric, d, method(d)
        except Exception as ex:  # noqa: BLE001
            return metric, d, {"error": str(ex)}

    with ThreadPoolExecutor(max_workers=FAN_OUT_WORKERS) as pool:
        futures = [pool.submit(_one, m, d) for m, d in tasks]
        for f in as_completed(futures):
            m, d, data = f.result()
            result[m][d] = data

    return result
