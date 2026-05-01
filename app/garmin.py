"""Garmin Connect client wrapper.

Auth model:
  1. One-time bootstrap locally (see scripts/bootstrap.py). User completes MFA
     interactively. `garminconnect` writes Garth OAuth tokens to a dir.
  2. For deployment, those tokens are base64-encoded and stored in
     GARTH_TOKENS_B64 as a *bootstrap* value. First startup: load from env,
     push to R2. Subsequent startups: load from R2 (survives Render restarts).
  3. garth refreshes OAuth2 tokens ~daily. Our patched refresh pushes the
     updated tokens back to R2 so restarts don't redo the exchange (the
     Garmin endpoint Garmin aggressively rate-limits).
"""
from __future__ import annotations

import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, Optional

from garminconnect import Garmin

from . import cache, tokens

_client: Optional[Garmin] = None
_lock = Lock()

MAX_RANGE_DAYS = 366
FAN_OUT_WORKERS = 2
JITTER_MIN_SEC = 0.1
JITTER_MAX_SEC = 0.25
RATE_LIMIT_MAX_RETRIES = 4
RATE_LIMIT_BASE_DELAY_SEC = 2.0
# Immutable historical data (completed activities, past daily summaries).
# ~100 years in seconds; effectively infinite for our purposes. Use
# force_refresh=true to bypass if you ever need to re-fetch.
IMMUTABLE_TTL = 100 * 365 * 24 * 3600

# Readonly mode — set GARMIN_READONLY=true in the web service's env to
# disable all live Garmin calls. The nightly GitHub Action runs in normal
# mode (not readonly) and is the sole writer to R2. The web MCP only reads
# from R2 and returns cache misses as errors rather than trying Garmin.
# Prevents rate-limit exposure on the user-facing path.
READONLY_MODE = os.environ.get("GARMIN_READONLY", "").lower() in ("1", "true", "yes")


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
    "spo2": "get_spo2_data",
    "all_day_events": "get_all_day_events",
    "nutrition_food_log": "get_nutrition_daily_food_log",
    "nutrition_meals": "get_nutrition_daily_meals",
}


# Circuit breaker: when Garmin rate-limits us, stop hammering. Every failed
# OAuth attempt extends Garmin's throttle window. Remember the failure for
# AUTH_COOLDOWN_SEC and fail fast instead of retrying.
_auth_failed_until: float = 0.0
AUTH_COOLDOWN_SEC = 300  # 5 minutes


def get_client() -> Garmin:
    global _client, _auth_failed_until
    with _lock:
        if _client is not None:
            return _client
        if READONLY_MODE:
            raise GarminError(
                "GARMIN_READONLY=true — live Garmin calls disabled on this "
                "instance. Data comes from the nightly pre-warm run. If this "
                "metric/date isn't cached, it won't be until tomorrow's 3am "
                "refresh."
            )
        if time.time() < _auth_failed_until:
            remaining = int(_auth_failed_until - time.time())
            raise GarminRateLimitError(
                f"Garmin auth in cooldown for {remaining}s after recent 429. "
                "Serving cached data only."
            )
        tokens_dir, source = tokens.load_tokens_dir()
        client = Garmin()
        client.garth.sess.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        })
        try:
            client.login(tokens_dir)
        except Exception as ex:  # noqa: BLE001
            msg = str(ex).lower()
            if "429" in msg or "too many requests" in msg or "rate limit" in msg:
                _auth_failed_until = time.time() + AUTH_COOLDOWN_SEC
                raise GarminRateLimitError(f"Garmin login throttled: {ex}") from ex
            raise
        # Set _garth_home so garth's refresh_oauth2 dumps refreshed tokens
        # to disk. Our patched refresh (in tokens.py) then pushes them to R2.
        # Without this, refreshed tokens stay in memory only and are lost on
        # container restart — forcing a fresh OAuth exchange every time
        # Render wakes the container.
        client.garth._garth_home = tokens_dir
        # Bootstrap case: first deploy loaded tokens from env — push them
        # to R2 so subsequent restarts use R2 and skip the OAuth exchange
        # path that Garmin rate-limits.
        if source == "env":
            try:
                tokens.persist_tokens_dir(tokens_dir)
            except Exception:  # noqa: BLE001
                pass  # logged inside persist_tokens_dir
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


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    if month == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month + 1, 1)
    return first, next_first - timedelta(days=1)


def _fetch_activities_month(year: int, month: int, force_refresh: bool = False) -> list[dict]:
    """Fetch (and cache) one calendar month of activities, unfiltered.

    Cache key is (year, month). Past months are immutable — use an effectively-
    infinite TTL so a 2-year-old activity stays cached indefinitely. Current
    month uses 24h TTL because new activities are still being added; nightly
    refresh force-refreshes it regardless.
    """
    args = {"year": year, "month": month}
    key_parts = [f"{year:04d}-{month:02d}"]
    today = date.today()
    is_current_month = (year == today.year and month == today.month)
    ttl = 24 * 3600 if is_current_month else IMMUTABLE_TTL
    if not force_refresh:
        hit = cache.get("activities_month", args, key_parts=key_parts, ttl_seconds=ttl)
        if hit is not None:
            return hit
    start, end = _month_bounds(year, month)
    data = _call_with_backoff(
        get_client().get_activities_by_date,
        start.isoformat(),
        end.isoformat(),
        None,
    ) or []
    cache.put("activities_month", args, data, key_parts=key_parts)
    return data


def get_activities_in_range(
    startdate: str | date,
    enddate: str | date,
    activity_type: str | None = None,
    force_refresh: bool = False,
):
    """Return activities between startdate and enddate (inclusive).

    Internally cached per-month so that sliding-window queries (e.g. "last 7
    days") don't accumulate duplicate cache entries. Only fetches from Garmin
    when a covering month hasn't been cached yet.
    """
    s, e = _validate_range(startdate, enddate)

    # Enumerate all (year, month) buckets covering [s, e].
    months: list[tuple[int, int]] = []
    cur = date(s.year, s.month, 1)
    while cur <= e:
        months.append((cur.year, cur.month))
        cur = date(cur.year + (1 if cur.month == 12 else 0), 1 if cur.month == 12 else cur.month + 1, 1)

    out: list[dict] = []
    for year, month in months:
        monthly = _fetch_activities_month(year, month, force_refresh=force_refresh)
        out.extend(monthly)

    # Filter to requested window + optional type.
    def _in_range(a: dict) -> bool:
        start_ts = a.get("startTimeLocal") or a.get("startTimeGMT") or ""
        try:
            d = datetime.fromisoformat(str(start_ts).replace("Z", "+00:00")).date()
        except ValueError:
            return False
        return s <= d <= e

    out = [a for a in out if _in_range(a)]
    if activity_type:
        out = [a for a in out if (a.get("activityType") or {}).get("typeKey") == activity_type]
    # Newest-first to match Garmin's default ordering.
    out.sort(key=lambda a: a.get("startTimeLocal") or "", reverse=True)
    return out


def get_activity_details(activity_id: str | int, force_refresh: bool = False) -> dict[str, Any]:
    """Full details for one activity. Activities are immutable once complete —
    effectively infinite TTL."""
    aid = str(activity_id)
    args = {"activity_id": aid}
    key_parts = [aid]
    if not force_refresh:
        hit = cache.get("activity_details", args, key_parts=key_parts, ttl_seconds=IMMUTABLE_TTL)
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
    cache.put("activity_details", args, out, key_parts=key_parts)
    return out


def get_personal_records(force_refresh: bool = False):
    """PRs change rarely. Cached 24h."""
    args = {}
    key_parts = ["latest"]
    if not force_refresh:
        hit = cache.get("personal_records", args, key_parts=key_parts)
        if hit is not None:
            return hit
    data = _call_with_backoff(get_client().get_personal_record)
    cache.put("personal_records", args, data, key_parts=key_parts)
    return data


def get_race_predictions(
    startdate: str | date | None = None,
    enddate: str | date | None = None,
    force_refresh: bool = False,
):
    """Latest race predictions or a history range. Cached for 24h — Garmin
    recomputes these daily at most, and the endpoint is slow and rate-sensitive."""
    args = {"startdate": str(startdate) if startdate else None, "enddate": str(enddate) if enddate else None}
    key_parts = [f"{startdate or 'latest'}__{enddate or 'latest'}"]
    if not force_refresh:
        hit = cache.get("race_predictions", args, key_parts=key_parts)
        if hit is not None:
            return hit
    c = get_client()
    if startdate and enddate:
        s, e = _validate_range(startdate, enddate)
        data = _call_with_backoff(c.get_race_predictions, s.isoformat(), e.isoformat())
    else:
        data = _call_with_backoff(c.get_race_predictions)
    cache.put("race_predictions", args, data, key_parts=key_parts)
    return data


def get_body_composition(
    startdate: str | date,
    enddate: str | date | None = None,
    force_refresh: bool = False,
):
    """Body composition (weight/fat/muscle) entries in a range. Cached — Garmin
    entries are manual logs and rarely backfilled, so a 24h TTL is plenty."""
    s = _coerce_date(startdate)
    e_iso = _coerce_date(enddate).isoformat() if enddate else None
    args = {"startdate": s.isoformat(), "enddate": e_iso}
    key_parts = [f"{s.isoformat()}__{e_iso or 'single'}"]
    if not force_refresh:
        hit = cache.get("body_composition", args, key_parts=key_parts)
        if hit is not None:
            return hit
    if enddate:
        _, _ = _validate_range(startdate, enddate)
        data = _call_with_backoff(
            get_client().get_body_composition, s.isoformat(), e_iso
        )
    else:
        data = _call_with_backoff(get_client().get_body_composition, s.isoformat())
    cache.put("body_composition", args, data, key_parts=key_parts)
    return data


def get_training_score(
    metric: str,
    startdate: str | date,
    enddate: str | date | None = None,
    force_refresh: bool = False,
):
    """Hill or endurance training score. Single date or range (max 366 days).
    Cached — Garmin updates these once daily."""
    methods = {"hill": "get_hill_score", "endurance": "get_endurance_score"}
    if metric not in methods:
        raise ValueError(f"metric must be one of {sorted(methods)}")
    s = _coerce_date(startdate)
    e_iso = _coerce_date(enddate).isoformat() if enddate else None
    args = {"metric": metric, "startdate": s.isoformat(), "enddate": e_iso}
    key_parts = [metric, f"{s.isoformat()}__{e_iso or 'single'}"]
    if not force_refresh:
        hit = cache.get("training_score", args, key_parts=key_parts)
        if hit is not None:
            return hit
    fn = getattr(get_client(), methods[metric])
    if enddate:
        _, _ = _validate_range(startdate, enddate)
        data = _call_with_backoff(fn, s.isoformat(), e_iso)
    else:
        data = _call_with_backoff(fn, s.isoformat())
    cache.put("training_score", args, data, key_parts=key_parts)
    return data


def get_lactate_threshold(
    startdate: str | date | None = None,
    enddate: str | date | None = None,
    aggregation: str = "daily",
    force_refresh: bool = False,
):
    """Lactate threshold (HR + power). Cached 24h — Garmin updates when you
    complete threshold-eligible efforts."""
    args = {
        "startdate": str(startdate) if startdate else None,
        "enddate": str(enddate) if enddate else None,
        "aggregation": aggregation,
    }
    key_parts = [aggregation, f"{startdate or 'latest'}__{enddate or 'latest'}"]
    if not force_refresh:
        hit = cache.get("lactate_threshold", args, key_parts=key_parts)
        if hit is not None:
            return hit
    c = get_client()
    if startdate and enddate:
        s, e = _validate_range(startdate, enddate)
        data = _call_with_backoff(
            c.get_lactate_threshold,
            latest=False,
            start_date=s.isoformat(),
            end_date=e.isoformat(),
            aggregation=aggregation,
        )
    else:
        data = _call_with_backoff(c.get_lactate_threshold, latest=True)
    cache.put("lactate_threshold", args, data, key_parts=key_parts)
    return data


def get_progress_summary(
    startdate: str | date,
    enddate: str | date,
    metric: str = "distance",
    group_by_activities: bool = True,
    force_refresh: bool = False,
):
    """Activity totals/averages over a range. Cached per (range, metric, group) —
    historical ranges never change; ranges ending 'today' benefit from the 24h
    TTL to avoid slamming Garmin on repeated queries."""
    s, e = _validate_range(startdate, enddate)
    args = {
        "startdate": s.isoformat(),
        "enddate": e.isoformat(),
        "metric": metric,
        "group_by_activities": group_by_activities,
    }
    key_parts = [metric, "grouped" if group_by_activities else "flat", f"{s.isoformat()}__{e.isoformat()}"]
    if not force_refresh:
        hit = cache.get("progress_summary", args, key_parts=key_parts)
        if hit is not None:
            return hit
    data = _call_with_backoff(
        get_client().get_progress_summary_between_dates,
        s.isoformat(),
        e.isoformat(),
        metric,
        group_by_activities,
    )
    cache.put("progress_summary", args, data, key_parts=key_parts)
    return data


def get_weekly_summaries(
    enddate: str | date,
    weeks: int = 52,
    metrics: list[str] | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Weekly aggregates (steps / stress / intensity_minutes).

    intensity_minutes uses a (start, end) range under the hood — we derive
    start as (enddate - weeks*7 days) to stay consistent.

    Cached per (metric, enddate, weeks) with 24h TTL.
    """
    if weeks < 1 or weeks > 104:
        raise ValueError("weeks must be between 1 and 104")
    end = _coerce_date(enddate)
    metrics = metrics or ["steps", "stress", "intensity_minutes"]
    allowed = {"steps", "stress", "intensity_minutes"}
    unknown = [m for m in metrics if m not in allowed]
    if unknown:
        raise ValueError(f"unknown weekly metrics: {unknown}. Supported: {sorted(allowed)}")

    out: dict[str, Any] = {}
    need_client = False
    to_fetch: list[str] = []
    for m in metrics:
        cache_args = {"metric": m, "enddate": end.isoformat(), "weeks": weeks}
        key_parts = [m, end.isoformat(), str(weeks)]
        if not force_refresh:
            hit = cache.get("weekly_summary", cache_args, key_parts=key_parts)
            if hit is not None:
                out[m] = hit
                continue
        to_fetch.append(m)
        need_client = True

    if not need_client:
        return out

    c = get_client()
    for m in to_fetch:
        cache_args = {"metric": m, "enddate": end.isoformat(), "weeks": weeks}
        key_parts = [m, end.isoformat(), str(weeks)]
        try:
            if m == "steps":
                data = _call_with_backoff(c.get_weekly_steps, end.isoformat(), weeks)
            elif m == "stress":
                data = _call_with_backoff(c.get_weekly_stress, end.isoformat(), weeks)
            elif m == "intensity_minutes":
                start = end - timedelta(days=weeks * 7)
                data = _call_with_backoff(
                    c.get_weekly_intensity_minutes, start.isoformat(), end.isoformat()
                )
            out[m] = data
            cache.put("weekly_summary", cache_args, data, key_parts=key_parts)
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


def get_workout_by_id(workout_id: str | int, force_refresh: bool = False):
    """Full step-by-step workout structure. Immutable once created — cached
    long-term keyed by workout_id."""
    wid = str(workout_id)
    args = {"workout_id": wid}
    key_parts = [wid]
    if not force_refresh:
        hit = cache.get("workout_by_id", args, key_parts=key_parts, ttl_seconds=30 * 24 * 3600)
        if hit is not None:
            return hit
    data = _call_with_backoff(get_client().get_workout_by_id, wid)
    cache.put("workout_by_id", args, data, key_parts=key_parts)
    return data


def _calendar_month(year: int, month: int) -> dict:
    """Fetch one Garmin Connect calendar month via the web-gateway endpoint.

    The python-garminconnect library doesn't wrap this, so we use the
    underlying garth client to hit `/calendar-service/year/{Y}/month/{M-1}`
    directly (Garmin's month is 0-indexed).
    """
    c = get_client()
    # Garmin's calendar service uses 0-indexed months (Jan=0 .. Dec=11).
    path = f"/calendar-service/year/{year}/month/{month - 1}"
    return _call_with_backoff(c.garth.connectapi, path) or {}


def get_scheduled_workouts(
    startdate: str | date,
    enddate: str | date,
    force_refresh: bool = False,
) -> list[dict]:
    """Return scheduled/planned workouts between two dates (inclusive).

    Walks the Garmin calendar month-by-month, filters entries tagged as
    workouts (not completed activities), and returns the relevant fields.
    Cached per (year, month) like activities_month.
    """
    s, e = _validate_range(startdate, enddate)

    # Enumerate covering (year, month) buckets.
    months: list[tuple[int, int]] = []
    cur = date(s.year, s.month, 1)
    while cur <= e:
        months.append((cur.year, cur.month))
        cur = date(
            cur.year + (1 if cur.month == 12 else 0),
            1 if cur.month == 12 else cur.month + 1,
            1,
        )

    out: list[dict] = []
    for year, month in months:
        args = {"year": year, "month": month}
        key_parts = [f"{year:04d}-{month:02d}"]
        today = date.today()
        is_current_or_future = (year, month) >= (today.year, today.month)
        # Future/current months: 24h TTL since plans can change.
        # Past months: immutable — already-scheduled workouts in the past don't
        # get edited in practice.
        ttl = 24 * 3600 if is_current_or_future else IMMUTABLE_TTL
        data: dict | None = None
        if not force_refresh:
            data = cache.get("calendar_month", args, key_parts=key_parts, ttl_seconds=ttl)
        if data is None:
            data = _calendar_month(year, month)
            cache.put("calendar_month", args, data, key_parts=key_parts)

        # Garmin's calendar items come back as `calendarItems` with an `itemType`.
        # We want scheduled workouts ("workout") and training-plan workouts,
        # not completed activities ("activity").
        for item in data.get("calendarItems", []) or []:
            itype = (item.get("itemType") or "").lower()
            if itype in ("activity",):
                continue
            date_str = item.get("date") or ""
            try:
                d = datetime.fromisoformat(date_str).date()
            except ValueError:
                continue
            if not (s <= d <= e):
                continue
            out.append(item)

    out.sort(key=lambda x: x.get("date") or "")
    return out


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

    result: dict[str, dict[str, Any]] = {m: {} for m in metrics}

    tasks: list[tuple[str, str]] = []
    today_str = date.today().isoformat()
    yesterday_str = (date.today() - timedelta(days=1)).isoformat()
    for m in metrics:
        for d in dates:
            if not force_refresh:
                # Today and yesterday can still be updating (late device syncs,
                # sleep data finalizing the morning after). Everything older is
                # immutable → effectively infinite TTL.
                ttl = 24 * 3600 if d >= yesterday_str else IMMUTABLE_TTL
                hit = cache.get(
                    "daily_summary",
                    {"metric": m, "date": d},
                    key_parts=[m, d],
                    ttl_seconds=ttl,
                )
                if hit is not None:
                    result[m][d] = hit
                    continue
            tasks.append((m, d))

    if not tasks:
        return result

    # Only build the Garmin client if we actually need to fetch something.
    # If the client itself fails (e.g. Garmin SSO/OAuth 429), degrade
    # gracefully: mark the uncached metric-days as errors so the caller
    # still gets every cached metric-day that DID hit R2. Previously a
    # client-init failure killed the whole call, hiding cached data that
    # was sitting right there in R2.
    try:
        client = get_client()
    except Exception as ex:  # noqa: BLE001
        err = {"error": f"Garmin client unavailable: {ex}"}
        for m, d in tasks:
            result[m][d] = err
        return result

    def _one(metric: str, d: str) -> tuple[str, str, Any]:
        method = getattr(client, DAILY_METHODS[metric])
        try:
            data = _call_with_backoff(method, d)
            cache.put(
                "daily_summary",
                {"metric": metric, "date": d},
                data,
                key_parts=[metric, d],
            )
            return metric, d, data
        except Exception as ex:  # noqa: BLE001
            return metric, d, {"error": str(ex)}

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

    Uses the cached per-month activities (same source as get_activities_in_range)
    so repeated calls over the same window don't pound Garmin.
    """
    s, e = _validate_range(startdate, enddate)
    acts = get_activities_in_range(startdate, enddate) or []

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
