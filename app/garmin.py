"""Garmin Connect client wrapper.

Auth model:
  1. One-time bootstrap locally (see scripts/bootstrap.py). User completes MFA
     interactively. `bootstrap.py` writes DI Bearer tokens as a JSON blob.
  2. For deployment, those tokens are base64-encoded and stored in
     GARMIN_TOKENS_B64 as a *bootstrap* value. First startup: load from env,
     push to R2. Subsequent startups: load from R2 (survives Render restarts).
  3. garminconnect auto-refreshes the DI token via diauth.garmin.com. Our
     patched _refresh_session pushes the updated tokens back to R2 so restarts
     don't redo the exchange (Garmin rate-limits this endpoint aggressively).
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, Optional

from garminconnect import Garmin

from . import cache, thresholds, tokens, weather

_client: Optional[Garmin] = None
_lock = Lock()

MAX_RANGE_DAYS = 366
FAN_OUT_WORKERS = 2
RATE_LIMIT_MAX_RETRIES = int(os.environ.get("GARMIN_RATE_LIMIT_MAX_RETRIES", "2"))
# Soft throttles (empty-body 200s from Garmin's CDN) are noisier and more
# transient than hard 429s — they often clear on the next call. Give them
# their own retry budget so workflows that set GARMIN_RATE_LIMIT_MAX_RETRIES=0
# (e.g. workout-check, which wants strict no-retry behavior on real 429s)
# still tolerate a soft hiccup.
SOFT_THROTTLE_MAX_RETRIES = int(os.environ.get("GARMIN_SOFT_THROTTLE_MAX_RETRIES", "2"))
RATE_LIMIT_BASE_DELAY_SEC = float(os.environ.get("GARMIN_RATE_LIMIT_BASE_DELAY_SEC", "2.0"))
# Minimum gap between the start of consecutive Garmin API calls across the
# whole process (thread-safe). Prevents the refresh jobs from hitting Garmin
# as a burst. 1s default; set GARMIN_MIN_CALL_INTERVAL_SEC=0 to disable.
GARMIN_MIN_CALL_INTERVAL_SEC = float(os.environ.get("GARMIN_MIN_CALL_INTERVAL_SEC", "1.0"))
# Per-call jitter on top of the minimum gap (avoids perfectly synchronized
# runs from two processes making calls at exactly the same moment).
GARMIN_CALL_JITTER_SEC = float(os.environ.get("GARMIN_CALL_JITTER_SEC", "0.5"))
# Immutable historical data (completed activities, past daily summaries).
# ~100 years in seconds; effectively infinite for our purposes. Use
# force_refresh=true to bypass if you ever need to re-fetch.
IMMUTABLE_TTL = 100 * 365 * 24 * 3600

# How long to honor a "no data" sentinel from a soft-throttle response.
# Short enough that endpoints which produce data later in the day
# (morning_readiness after sleep processing, body_battery as the day
# accumulates) still get picked up. Long enough that we don't
# re-hammer Garmin every 6h refresh when the data genuinely isn't
# there yet.
NO_DATA_SOFT_THROTTLE_TTL_SEC = 4 * 3600

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
    def __init__(self, message: str, retry_after: float | None = None,
                 soft: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        # `soft` distinguishes empty-body (CDN soft throttle) from a real
        # 429. Soft signals should drive local backoff/retry but not trip
        # the process-level circuit breaker — they're noisy but the run
        # often recovers on retry, and stopping the whole refresh on the
        # first one is too aggressive.
        self.soft = soft


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
    # Empty-body 200 responses surface as JSONDecodeError ("Expecting value:
    # line 1 column 1 (char 0)") because garminconnect calls .json() on
    # an empty payload. Garmin's CDN returns these as a soft throttle —
    # less aggressive than 429 but the same root cause. Treat them as a
    # rate-limit signal so backoff/circuit-breaker logic kicks in.
    if isinstance(exc, json.JSONDecodeError) or "expecting value" in msg:
        return GarminRateLimitError(
            f"empty body (soft throttle): {exc}", soft=True
        )
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
        json_str, source = tokens.load_tokens_json()
        client = Garmin()
        # Use client.client.loads() instead of client.login(tokenstore=…).
        # client.login() eagerly fetches the social profile to populate
        # display_name, burning a Garmin API call on every container start.
        # loads() just deserializes the DI token JSON — no network call.
        # The first API call triggers auto-refresh via _run_request if needed,
        # gated by our patched _refresh_session.
        try:
            client.client.loads(json_str)
        except Exception as ex:  # noqa: BLE001
            msg = str(ex).lower()
            if "429" in msg or "too many requests" in msg or "rate limit" in msg:
                _auth_failed_until = time.time() + AUTH_COOLDOWN_SEC
                raise GarminRateLimitError(f"Garmin token load throttled: {ex}") from ex
            raise
        # Bootstrap case: first deploy loaded tokens from env — push to R2
        # so subsequent restarts use R2 and skip the Garmin exchange endpoint.
        if source == "env":
            try:
                tokens.save_tokens_json(json_str)
            except Exception:  # noqa: BLE001
                pass  # logged inside save_tokens_json
        # Populate display_name. garminconnect builds endpoint URLs as
        # /service/path/{display_name}, and without it requests like
        # get_steps_data hit /.../None and 403. We skip client.login()
        # (rate-limit pressure) so we have to fill display_name ourselves.
        # Cache to R2 with effectively-infinite TTL — this string never
        # changes for an account.
        try:
            client.display_name = _resolve_display_name(client)
        except Exception as ex:  # noqa: BLE001
            # Non-fatal — endpoints that don't need display_name still work.
            print(f"[garmin] WARN: could not resolve display_name: {ex}",
                  file=sys.stderr)
        _client = client
        return client


def _resolve_display_name(client: Garmin) -> str | None:
    cached = cache.get(
        "user_profile",
        {},
        key_parts=["display_name"],
        ttl_seconds=IMMUTABLE_TTL,
    )
    if isinstance(cached, str) and cached:
        return cached
    if isinstance(cached, dict):
        name = cached.get("displayName") or cached.get("display_name")
        if name:
            return name
    # Cache miss — fetch from Garmin (one cheap connectapi call, never
    # repeats for the life of this user account).
    profile = client.connectapi("/userprofile-service/socialProfile")
    name = (profile or {}).get("displayName")
    if name:
        cache.put("user_profile", {}, name, key_parts=["display_name"])
    return name


def ensure_oauth_ready() -> None:
    """Ensure the loaded Garmin DI Bearer token is usable.

    Calls the patched _refresh_session: idempotent (checks R2 for a fresh
    token first, only exchanges if truly near expiry), serialized by a lock
    so concurrent worker threads don't trigger simultaneous exchanges.

    Wraps through _classify_exception so callers see GarminRateLimitError
    (with soft=True for empty-body responses) instead of raw exceptions.
    """
    try:
        get_client().client._refresh_session()
    except Exception as ex:  # noqa: BLE001
        classified = _classify_exception(ex)
        if classified is not None:
            raise classified from ex
        raise


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


# Global rate limiter — enforces GARMIN_MIN_CALL_INTERVAL_SEC between the
# start of any two consecutive Garmin API calls within this process. The lock
# ensures FAN_OUT_WORKERS threads can't both fire a call at the same instant.
_rate_limit_lock = Lock()
_last_garmin_call_time: float = 0.0


def _rate_limit_sleep() -> None:
    global _last_garmin_call_time
    if GARMIN_MIN_CALL_INTERVAL_SEC <= 0:
        return
    with _rate_limit_lock:
        now = time.time()
        wait = (_last_garmin_call_time + GARMIN_MIN_CALL_INTERVAL_SEC) - now
        if wait > 0:
            time.sleep(wait)
        time.sleep(random.uniform(0, GARMIN_CALL_JITTER_SEC))
        _last_garmin_call_time = time.time()


# Process-level circuit breaker for regular Garmin API calls. Once any call
# exhausts its retry budget on a 429, this flag is set so every subsequent
# _call_with_backoff invocation in this process fails immediately — no more
# Garmin traffic in this run. Also persists to R2 via
# tokens.save_api_429_cooldown so the NEXT nightly process aborts at startup
# rather than re-hammering Garmin before even a single cache miss.
_api_circuit_tripped: GarminRateLimitError | None = None
_api_circuit_lock = Lock()


def _trip_api_circuit(ex: GarminRateLimitError) -> None:
    global _api_circuit_tripped
    with _api_circuit_lock:
        if _api_circuit_tripped is None:
            _api_circuit_tripped = ex
            try:
                tokens.save_api_429_cooldown(ex)
            except Exception:  # noqa: BLE001
                pass


def _call_with_backoff(fn, *args, **kwargs):
    """Run `fn` with jittered delay + exponential backoff on 429s.

    Raises GarminRateLimitError / GarminAuthError / GarminNotFoundError for
    classified errors after exhausting retries; re-raises other exceptions.
    """
    # Fail fast if a 429 already tripped the process circuit breaker.
    if _api_circuit_tripped is not None:
        raise _api_circuit_tripped

    _rate_limit_sleep()
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as ex:  # noqa: BLE001
            classified = _classify_exception(ex)
            if isinstance(classified, GarminRateLimitError):
                budget = SOFT_THROTTLE_MAX_RETRIES if classified.soft else RATE_LIMIT_MAX_RETRIES
                if attempt < budget:
                    delay = classified.retry_after if classified.retry_after is not None else RATE_LIMIT_BASE_DELAY_SEC * (2 ** attempt)
                    delay += random.uniform(0, 0.5)
                    time.sleep(delay)
                    attempt += 1
                    continue
            if classified is not None:
                if isinstance(classified, GarminRateLimitError) and not classified.soft:
                    _trip_api_circuit(classified)
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
    effectively infinite TTL.

    Enriched with `ambient_weather` (from Open-Meteo historical API). The
    watch-reported `weather` field is kept for reference but is usually
    distorted by wrist heat / sun / pavement. Prefer `ambient_weather` for
    heat-stress and aerobic-decoupling analysis.
    """
    aid = str(activity_id)
    args = {"activity_id": aid}
    key_parts = [aid]
    if not force_refresh:
        hit = cache.get("activity_details", args, key_parts=key_parts, ttl_seconds=IMMUTABLE_TTL)
        if hit is not None:
            # Backfill weather on cached entries that need it:
            #  - missing entirely (legacy cache)
            #  - prior lookup errored (e.g. unparseable timestamp before
            #    the parser was fixed)
            #  - indoor activity that was cached before the indoor-skip
            #    check existed (has weather data that's meaningless)
            existing = hit.get("ambient_weather")
            summary = hit.get("summary") or {}
            is_indoor = _is_indoor_activity(summary)
            needs_weather = (
                existing is None
                or (isinstance(existing, dict) and "error" in existing)
                # Indoor activity but old cache entry still has real weather data
                or (is_indoor and isinstance(existing, dict)
                    and not existing.get("skipped"))
            )
            if needs_weather:
                hit["ambient_weather"] = _ambient_weather_from_summary(summary)
                cache.put("activity_details", args, hit, key_parts=key_parts)
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
    # Ambient weather from Open-Meteo — uses lat/lon/start_time from the
    # summary. Falls back to {"error": ...} if any of those are missing.
    out["ambient_weather"] = _ambient_weather_from_summary(out.get("summary") or {})
    cache.put("activity_details", args, out, key_parts=key_parts)
    return out


INDOOR_ACTIVITY_TYPES = {
    # Virtual rides have GPS but from the virtual course, not the rider's location.
    "virtual_ride",
    "indoor_cycling",
    "treadmill_running",
    "indoor_running",
    # Pool swimming — conditions outdoors are irrelevant to the session.
    "lap_swimming",
    "pool_swimming",
    # Strength / other indoor work.
    "strength_training",
    "indoor_cardio",
    "elliptical",
    "stair_climbing",
    "yoga",
    "pilates",
    "indoor_rowing",
}


def _is_indoor_activity(summary: dict) -> bool:
    """Check whether this activity happened indoors / doesn't benefit from
    ambient weather.

    Garmin exposes activity type under different keys depending on which
    endpoint produced the summary:
      - activity list: summary["activityType"]["typeKey"]
      - activity_details: summary["activityTypeDTO"]["typeKey"]
    """
    type_dict = (
        summary.get("activityType")
        or summary.get("activityTypeDTO")
        or (summary.get("summaryDTO") or {}).get("activityType")
        or {}
    )
    atype = type_dict.get("typeKey", "")
    if atype in INDOOR_ACTIVITY_TYPES:
        return True
    # parentTypeId 29 = fitness_equipment (generic indoor fitness-equipment bucket)
    if type_dict.get("parentTypeId") == 29:
        return True
    # Manufacturer "VIRTUALTRAINING" / "ZWIFT" / etc. = virtual indoor platform
    manufacturer = (summary.get("manufacturer") or "").upper()
    if manufacturer in ("VIRTUALTRAINING", "ZWIFT", "TRAINERROAD", "ROUVY"):
        return True
    return False


def _ambient_weather_from_summary(summary: dict) -> dict:
    """Extract lat/lon/start/duration from a Garmin activity summary and
    hand off to weather.summarize_activity_weather. Small wrapper so we
    can apply the same extraction in both the cold-fetch and the
    backfill-on-read paths. Returns a stub for indoor activities — no
    point looking up weather for a pool swim or trainer ride."""
    if _is_indoor_activity(summary):
        return {
            "skipped": True,
            "reason": "indoor activity",
            "activity_type": (summary.get("activityType") or {}).get("typeKey"),
        }
    try:
        lat = summary.get("startLatitude") or (summary.get("summaryDTO") or {}).get("startLatitude")
        lon = summary.get("startLongitude") or (summary.get("summaryDTO") or {}).get("startLongitude")
        start = (summary.get("startTimeGMT")
                 or (summary.get("summaryDTO") or {}).get("startTimeGMT"))
        duration = (summary.get("duration")
                    or (summary.get("summaryDTO") or {}).get("duration"))
        return weather.summarize_activity_weather(lat, lon, start, duration)
    except Exception as ex:  # noqa: BLE001
        return {"error": f"weather lookup failed: {ex}"}


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


def get_cycling_ftp(force_refresh: bool = False):
    """User's stored cycling FTP from Garmin Connect. Returned as the
    latest value the user has set (manually via the app, or auto-detected
    by Garmin from an FTP test). Separate from run FTP which lives on the
    lactate_threshold endpoint.

    This is the PREFERRED source of bike FTP — direct user setting. Fall
    back to 20-min-power inference only if this returns nothing or the
    user hasn't set it.

    Cached 24h.
    """
    cache_args = {}
    key_parts = ["latest"]
    if not force_refresh:
        hit = cache.get("cycling_ftp", cache_args, key_parts=key_parts)
        if hit is not None:
            return hit
    data = _call_with_backoff(get_client().get_cycling_ftp)
    cache.put("cycling_ftp", cache_args, data, key_parts=key_parts)
    return data


def save_weekly_snapshot(snapshot: dict) -> dict:
    """Persist a /weekly summary snapshot to R2. Keyed by the 'date' field
    in the snapshot (which should be the Monday of the week reviewed or
    the day /weekly ran). Next week's /weekly retrieves this via
    get_weekly_snapshots() to compute WHAT CHANGED deltas automatically.

    This eliminates the manual copy-paste-to-project-instructions loop.

    Returns {"saved": true, "key": "..."} on success.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be a dict")
    snap_date = snapshot.get("date") or date.today().isoformat()
    # Normalize to YYYY-MM-DD
    try:
        d = datetime.strptime(snap_date[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        d = date.today()
        snapshot["date"] = d.isoformat()
    key_parts = [d.isoformat()]
    args = {"date": d.isoformat()}
    cache.put("weekly_snapshots", args, snapshot, key_parts=key_parts)
    return {"saved": True, "date": d.isoformat()}


def get_weekly_snapshots(weeks_back: int = 1) -> list[dict]:
    """Return the most recent N weekly snapshots, newest-first.

    weeks_back=1 returns the single most recent snapshot (typical for
    WHAT CHANGED deltas). Larger values enable multi-week trajectory
    charts.
    """
    weeks_back = max(1, min(int(weeks_back), 52))
    keys = cache.list_keys(tool_prefix="weekly_snapshots", limit=500)
    # Keys look like "{PREFIX}weekly_snapshots/{YYYY-MM-DD}.json"
    dated = []
    for k in keys:
        try:
            base = k.rsplit("/", 1)[-1].replace(".json", "")
            d = datetime.strptime(base, "%Y-%m-%d").date()
            dated.append((d, k))
        except (ValueError, AttributeError):
            continue
    dated.sort(reverse=True)
    out = []
    for d, _ in dated[:weeks_back]:
        snap = cache.get(
            "weekly_snapshots",
            {"date": d.isoformat()},
            key_parts=[d.isoformat()],
            ttl_seconds=IMMUTABLE_TTL,  # snapshots are historical; never expire
        )
        if snap is not None:
            out.append(snap)
    return out


def nutrition_plan_vs_actual(days_back: int = 7) -> dict:
    """Compare /weekly's nutrition plan against actual food logged +
    actual expenditure.

    Three target concepts per day:
      - target_kcal       : the static plan from Sunday
      - adjusted_target   : plan + (actual expenditure - expected
                            expenditure). Reflects what the user
                            SHOULD have eaten given what actually
                            happened that day. Undefined on days
                            with no expenditure data.
      - garmin_goal       : Garmin Connect's own daily goal
                            (activity-adjusted in their app)

    adjusted_target is the most actionable for "did I eat enough
    today?" — a planned 2800 kcal day where the user went 30min longer
    should be 3100 kcal, not 2800.

    Returns per-day rows + weekly totals + logging summary.
    """
    days_back = max(1, min(int(days_back), 14))
    today_d = date.today()

    # Fetch the most recent weekly snapshot — need its nutrition_plan
    snapshots = get_weekly_snapshots(weeks_back=1)
    plan = {}
    plan_source_date = None
    if snapshots:
        snap = snapshots[0]
        plan_source_date = snap.get("date")
        plan = (snap.get("nutrition_plan") or {})

    # Pull nutrition + activity totals for the window
    start_d = today_d - timedelta(days=days_back - 1)
    metrics = ["nutrition_food_log", "stats_and_body"]
    daily = get_daily_summaries(
        startdate=start_d.isoformat(),
        enddate=today_d.isoformat(),
        metrics=metrics,
    )
    food_log = daily.get("nutrition_food_log", {})
    stats = daily.get("stats_and_body", {})

    # Weight for protein-target math
    weight_kg = None
    for d_iso, payload in stats.items():
        if isinstance(payload, dict) and "error" not in payload:
            w = payload.get("bodyWeight") or payload.get("weight")
            if w:
                weight_kg = round(w / 1000.0, 1) if w > 500 else round(w, 1)
                break

    # First pass: collect per-day raw data (no adjusted target yet —
    # need a fallback expected-expenditure baseline computed from the
    # window for days where the plan didn't store one).
    raw_days = []
    for i in range(days_back):
        d = start_d + timedelta(days=i)
        d_iso = d.isoformat()
        day_name = d.strftime("%a")
        day_plan = plan.get(d_iso) or plan.get(day_name) or {}

        fl = food_log.get(d_iso)
        consumed = {}
        foods_count = 0
        garmin_goal = None
        if isinstance(fl, dict) and "error" not in fl:
            consumed = fl.get("dailyNutritionContent") or {}
            foods_count = len(fl.get("loggedFoodsWithServingSizes") or [])
            goals = fl.get("dailyNutritionGoals") or {}
            garmin_goal = goals.get("adjustedCalories") or goals.get("calories")

        sb = stats.get(d_iso) or {}
        expenditure = None
        if isinstance(sb, dict) and "error" not in sb:
            bmr = sb.get("bmrKilocalories") or 0
            active = sb.get("activeKilocalories") or 0
            total_kcal = sb.get("totalKilocalories")
            expenditure = round(total_kcal or (bmr + active)) if (total_kcal or bmr or active) else None

        raw_days.append({
            "date": d_iso, "day": day_name, "day_plan": day_plan,
            "consumed": consumed, "foods_count": foods_count,
            "garmin_goal": garmin_goal, "expenditure": expenditure,
        })

    # Fallback "expected expenditure" for adjustment: median actual
    # expenditure from the window's completed days. If the plan stored
    # a per-day `expected_expenditure_kcal`, we prefer that.
    window_expenditures = [r["expenditure"] for r in raw_days if r["expenditure"]]
    median_expenditure = None
    if window_expenditures:
        sv = sorted(window_expenditures)
        median_expenditure = sv[len(sv) // 2]

    rows = []
    sums = {"target_kcal": 0, "adjusted_target_kcal": 0, "actual_kcal": 0,
            "target_p": 0, "actual_p": 0,
            "target_c": 0, "actual_c": 0, "target_f": 0, "actual_f": 0,
            "expenditure": 0, "days_with_target": 0, "days_logged": 0,
            "days_with_adjusted": 0}

    for raw in raw_days:
        d_iso = raw["date"]
        day_plan = raw["day_plan"]
        consumed = raw["consumed"]
        foods_count = raw["foods_count"]
        expenditure = raw["expenditure"]

        target_kcal = day_plan.get("target_kcal") or day_plan.get("kcal")
        expected_exp = (
            day_plan.get("expected_expenditure_kcal")
            or day_plan.get("planned_expenditure_kcal")
            or median_expenditure  # fallback
        )

        # Adjusted target: shift plan target by how much actual expenditure
        # over/under-shot the expected expenditure.
        adjusted_target = None
        adjustment_source = None
        if target_kcal is not None and expenditure is not None and expected_exp:
            adjustment = expenditure - expected_exp
            adjusted_target = round(target_kcal + adjustment)
            adjustment_source = (
                "plan.expected_expenditure_kcal"
                if (day_plan.get("expected_expenditure_kcal")
                    or day_plan.get("planned_expenditure_kcal"))
                else "window median expenditure (fallback — plan didn't "
                     "store expected expenditure)"
            )

        row = {
            "date": d_iso,
            "day": raw["day"],
            "target_kcal": target_kcal,
            "expected_expenditure_kcal": expected_exp,
            "adjusted_target_kcal": adjusted_target,
            "adjustment_source": adjustment_source,
            "target_p": day_plan.get("protein_g") or day_plan.get("protein"),
            "target_c": day_plan.get("carbs_g") or day_plan.get("carbs"),
            "target_f": day_plan.get("fat_g") or day_plan.get("fat"),
            "target_session": day_plan.get("session") or day_plan.get("workout"),
            "garmin_goal_kcal": raw["garmin_goal"],
            "actual_kcal": consumed.get("calories"),
            "actual_p": consumed.get("protein"),
            "actual_c": consumed.get("carbs"),
            "actual_f": consumed.get("fat"),
            "foods_logged": foods_count,
            "expenditure_kcal": expenditure,
        }

        # Deltas — both against the static plan AND against the adjusted target
        if target_kcal is not None and row["actual_kcal"] is not None:
            row["delta_kcal_vs_plan"] = round(row["actual_kcal"] - target_kcal)
        if adjusted_target is not None and row["actual_kcal"] is not None:
            row["delta_kcal_vs_adjusted"] = round(row["actual_kcal"] - adjusted_target)
        if row["target_p"] is not None and row["actual_p"] is not None:
            row["delta_p"] = round(row["actual_p"] - row["target_p"], 1)
        if row["actual_kcal"] is not None and expenditure is not None:
            row["net_kcal"] = round(row["actual_kcal"] - expenditure)
        rows.append(row)

        # Accumulate
        if target_kcal:
            sums["days_with_target"] += 1
            sums["target_kcal"] += target_kcal
            sums["target_p"] += (row["target_p"] or 0)
            sums["target_c"] += (row["target_c"] or 0)
            sums["target_f"] += (row["target_f"] or 0)
        if adjusted_target is not None:
            sums["days_with_adjusted"] += 1
            sums["adjusted_target_kcal"] += adjusted_target
        if foods_count > 0 and row["actual_kcal"]:
            sums["days_logged"] += 1
            sums["actual_kcal"] += row["actual_kcal"]
            sums["actual_p"] += (row["actual_p"] or 0)
            sums["actual_c"] += (row["actual_c"] or 0)
            sums["actual_f"] += (row["actual_f"] or 0)
        if expenditure:
            sums["expenditure"] += expenditure

    return {
        "window": {"start": start_d.isoformat(), "end": today_d.isoformat(), "days": days_back},
        "plan_source_weekly_snapshot": plan_source_date,
        "weight_kg": weight_kg,
        "rows": rows,
        "totals": sums,
        "no_plan_available": plan_source_date is None or not plan,
    }


def nutrition_trend(weeks: int = 4) -> dict:
    """4-week (or more) trend of nutrition adherence + weight.

    Per week returns: avg daily intake, avg expenditure, weekly delta,
    days logged, avg protein, protein-target-hit count, median weight.
    Plus an overall weight trajectory and logging-consistency summary.

    Data sources (prefers faster/cheaper):
      1. Weekly snapshots from R2 if present (holds pre-computed totals)
      2. Otherwise synthesize from raw daily nutrition_food_log +
         stats_and_body + body_composition entries
    """
    weeks = max(1, min(int(weeks), 26))
    today_d = date.today()
    window_days = weeks * 7
    window_start = today_d - timedelta(days=window_days - 1)

    # Pull the raw data once — both paths (snapshot + synthesis) can use it
    daily = get_daily_summaries(
        startdate=window_start.isoformat(),
        enddate=today_d.isoformat(),
        metrics=["nutrition_food_log", "stats_and_body"],
    )
    food_log = daily.get("nutrition_food_log", {})
    stats = daily.get("stats_and_body", {})

    # Weight readings over the window — dateWeightList is daily samples
    weight_readings: list[tuple[date, float]] = []
    try:
        bc = get_body_composition(
            startdate=window_start.isoformat(),
            enddate=today_d.isoformat(),
        )
        for entry in (bc.get("dateWeightList") or []):
            try:
                d_str = entry.get("date") or entry.get("calendarDate")
                w_grams = entry.get("weight")
                if d_str and w_grams:
                    d = datetime.strptime(d_str[:10], "%Y-%m-%d").date()
                    weight_readings.append((d, w_grams / 1000.0))
            except (ValueError, TypeError):
                continue
    except Exception:  # noqa: BLE001
        pass
    # Fall back to stats_and_body bodyWeight field
    if not weight_readings:
        for d_iso, payload in stats.items():
            if isinstance(payload, dict) and "error" not in payload:
                w = payload.get("bodyWeight") or payload.get("weight")
                if w:
                    try:
                        d = datetime.strptime(d_iso, "%Y-%m-%d").date()
                        # stats_and_body weight is typically in grams
                        weight_readings.append((d, w / 1000.0 if w > 500 else w))
                    except (ValueError, TypeError):
                        continue
    weight_readings.sort()

    # Pull available weekly snapshots for the window
    snapshots = get_weekly_snapshots(weeks_back=weeks + 2)  # grab a few extra

    # Build week buckets (Mon -> Sun, aligned so the window ends today)
    def _week_of(d: date) -> date:
        return d - timedelta(days=(today_d - d).days % 7)
    week_starts = [today_d - timedelta(days=(today_d.weekday() - 0) % 7 + 7 * i)
                   for i in range(weeks)]
    week_starts = sorted(set(week_starts))
    # Ensure we cover `weeks` full weeks ending at today
    week_starts = [today_d - timedelta(days=today_d.weekday() + 7 * i) for i in range(weeks)]
    week_starts = sorted(set(week_starts))

    week_rows = []
    baseline = get_athlete_baseline()
    weight_kg_current = (baseline.get("weight_kg") if isinstance(baseline, dict) else None)

    for ws in week_starts:
        we = ws + timedelta(days=6)
        week_days = [(ws + timedelta(days=i)).isoformat() for i in range(7)]

        # Check if a snapshot exists for this week
        snap_match = None
        for s in snapshots:
            s_date = s.get("date")
            if not s_date:
                continue
            try:
                sd = datetime.strptime(s_date[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if ws <= sd <= we:
                snap_match = s
                break

        if snap_match:
            # Use pre-computed values
            row = {
                "week_start": ws.isoformat(),
                "week_end": we.isoformat(),
                "avg_daily_kcal_intake": snap_match.get("avg_daily_kcal_intake"),
                "avg_daily_kcal_expenditure": snap_match.get("avg_daily_kcal_expenditure"),
                "avg_daily_delta": (
                    snap_match.get("weekly_kcal_delta", 0) / 7.0
                    if snap_match.get("weekly_kcal_delta") is not None
                    else None
                ),
                "days_logged": snap_match.get("days_logged"),
                "protein_target_hit_days": snap_match.get("protein_target_hit_days"),
                "source": "snapshot",
            }
        else:
            # Synthesize from raw daily data
            kcal_intake_vals = []
            kcal_expenditure_vals = []
            protein_vals = []
            days_logged = 0
            protein_hit = 0
            protein_target_per_day = (weight_kg_current * 1.6) if weight_kg_current else None
            for d_iso in week_days:
                fl = food_log.get(d_iso)
                if isinstance(fl, dict) and "error" not in fl:
                    content = fl.get("dailyNutritionContent") or {}
                    foods_count = len(fl.get("loggedFoodsWithServingSizes") or [])
                    k = content.get("calories")
                    p = content.get("protein")
                    if foods_count > 0 and k:
                        kcal_intake_vals.append(k)
                        days_logged += 1
                        if p:
                            protein_vals.append(p)
                            if protein_target_per_day and p >= protein_target_per_day:
                                protein_hit += 1
                sb = stats.get(d_iso) or {}
                if isinstance(sb, dict) and "error" not in sb:
                    total = sb.get("totalKilocalories")
                    bmr = sb.get("bmrKilocalories") or 0
                    active = sb.get("activeKilocalories") or 0
                    e = total or (bmr + active if (bmr or active) else None)
                    if e:
                        kcal_expenditure_vals.append(e)

            avg_intake = round(sum(kcal_intake_vals) / len(kcal_intake_vals)) if kcal_intake_vals else None
            avg_exp = round(sum(kcal_expenditure_vals) / len(kcal_expenditure_vals)) if kcal_expenditure_vals else None
            avg_delta = (avg_intake - avg_exp) if (avg_intake and avg_exp) else None
            avg_protein = round(sum(protein_vals) / len(protein_vals), 1) if protein_vals else None

            row = {
                "week_start": ws.isoformat(),
                "week_end": we.isoformat(),
                "avg_daily_kcal_intake": avg_intake,
                "avg_daily_kcal_expenditure": avg_exp,
                "avg_daily_delta": avg_delta,
                "avg_daily_protein_g": avg_protein,
                "days_logged": days_logged,
                "protein_target_hit_days": protein_hit,
                "source": "synthesized",
            }

        # Attach this week's median weight
        week_weights = [w for d, w in weight_readings if ws <= d <= we]
        if week_weights:
            row["avg_weight_kg"] = round(sum(week_weights) / len(week_weights), 2)
            row["weight_readings_count"] = len(week_weights)
        else:
            row["avg_weight_kg"] = None
            row["weight_readings_count"] = 0

        week_rows.append(row)

    # Overall weight trajectory
    weight_trajectory = None
    if len(weight_readings) >= 2:
        # Use first and last 3-reading medians for noise-robust endpoints
        first_vals = [w for _, w in weight_readings[:3]] or [weight_readings[0][1]]
        last_vals = [w for _, w in weight_readings[-3:]] or [weight_readings[-1][1]]
        start_weight = round(sum(first_vals) / len(first_vals), 2)
        end_weight = round(sum(last_vals) / len(last_vals), 2)
        weight_trajectory = {
            "start_weight_kg": start_weight,
            "end_weight_kg": end_weight,
            "delta_kg": round(end_weight - start_weight, 2),
            "readings_count": len(weight_readings),
            "window_days": window_days,
        }

    # Summary + logging consistency
    total_days_logged = sum(r.get("days_logged") or 0 for r in week_rows)
    total_window_days = weeks * 7
    logging_pct = round(100 * total_days_logged / total_window_days, 1) if total_window_days else 0

    # Trend direction
    intake_series = [r.get("avg_daily_kcal_intake") for r in week_rows if r.get("avg_daily_kcal_intake")]
    if len(intake_series) >= 3:
        early_avg = sum(intake_series[:len(intake_series)//2]) / max(1, len(intake_series)//2)
        late_avg = sum(intake_series[len(intake_series)//2:]) / max(1, len(intake_series) - len(intake_series)//2)
        intake_trend = "rising" if late_avg > early_avg * 1.03 else ("falling" if late_avg < early_avg * 0.97 else "stable")
    else:
        intake_trend = "insufficient data"

    weight_trend = None
    if weight_trajectory:
        delta = weight_trajectory["delta_kg"]
        if abs(delta) < 0.2:
            weight_trend = "stable"
        elif delta < 0:
            weight_trend = f"losing ({abs(delta)}kg over {weeks}w)"
        else:
            weight_trend = f"gaining ({delta}kg over {weeks}w)"

    return {
        "weeks": week_rows,
        "weight_trajectory": weight_trajectory,
        "summary": {
            "total_days_logged": total_days_logged,
            "total_window_days": total_window_days,
            "logging_consistency_pct": logging_pct,
            "intake_trend": intake_trend,
            "weight_trend": weight_trend,
            "weight_kg_current": weight_kg_current,
        },
    }


# ---------- Fueling: goal store + daily / per-workout plan generator ----------
#
# A "Fuelin"-style engine. Persist a weight goal, then fuse it with body
# composition + Garmin scheduled workouts into daily calorie/macro targets and
# a per-workout fuel card (pre / during / post + hydration). The formulas
# mirror skills/weekly.md + skills/project-instructions.md so the server-side
# numbers match what the skills produce:
#   BMR ............ Mifflin-St Jeor (fallback weight_kg x 22 for endurance)
#   Daily target ... BMR x 1.3 (NEAT) + session burn + goal adjustment,
#                    floored at BMR x 1.2
#   Deficit ........ (kg x 7700 / weeks) / 7, capped at 500 kcal/day
#   Carbs .......... periodized by session type (3-8 g/kg)
#   Protein ........ by bodyweight; Fat closes the gap to target (with a floor)

GOAL_TYPES = ("lose", "gain", "maintain")

# Baseline burn per hour by sport, used when history is too thin to
# calibrate. Intensity multipliers scale these around an easy baseline of 1.0.
_BASE_KCAL_PER_HOUR = {
    "cycling": 650, "running": 700, "swimming": 550, "strength": 350,
    "walking": 300, "default": 600,
}
_INTENSITY_MULT = {
    "rest": 0.0, "recovery": 0.8, "easy": 1.0, "endurance": 1.0,
    "long": 0.95, "tempo": 1.2, "threshold": 1.4, "vo2": 1.45,
}
_CARB_G_PER_KG = {
    "rest": 3.0, "recovery": 4.0, "easy": 4.0, "endurance": 5.0,
    "long": 6.0, "tempo": 5.5, "threshold": 7.5, "vo2": 7.5,
}
# Default planned duration (hours) when neither the workout detail nor the
# calendar item states one.
_DEFAULT_HOURS = {
    "rest": 0.0, "recovery": 0.75, "easy": 1.0, "endurance": 1.5,
    "long": 2.5, "tempo": 1.0, "threshold": 1.25, "vo2": 1.0,
}
_PROTEIN_G_PER_KG_DEFAULT = {"lose": 1.9, "maintain": 1.6, "gain": 1.7}
# Rank used to pick the day's "hardest" session for carb periodization.
_INTENSITY_ORDER = {
    "rest": 0, "recovery": 1, "easy": 2, "endurance": 3,
    "long": 4, "tempo": 5, "threshold": 6, "vo2": 7,
}


def set_fueling_goal(
    goal_type: str,
    target_weight_kg: float | None = None,
    target_date: str | None = None,
    start_weight_kg: float | None = None,
    sex: str | None = None,
    height_cm: float | None = None,
    age: int | None = None,
    protein_g_per_kg: float | None = None,
    notes: str | None = None,
) -> dict:
    """Persist the athlete's fueling goal to R2 (single active goal, keyed
    'current'). This is the target weight + timeline the fueling plan is built
    around, plus the BMR inputs Garmin doesn't reliably expose (sex/height/age).
    Overwrites any prior goal; the set date is recorded so skills can flag a
    stale goal.

    goal_type: 'lose' | 'gain' | 'maintain'. For 'lose'/'gain' provide
    target_weight_kg and target_date so a daily deficit/surplus can be computed.
    """
    gt = (goal_type or "").strip().lower()
    if gt not in GOAL_TYPES:
        raise ValueError(f"goal_type must be one of {GOAL_TYPES}")
    sex_n = (sex or "").strip().lower() or None
    if sex_n and sex_n not in ("male", "female"):
        raise ValueError("sex must be 'male' or 'female'")
    if target_date:
        try:
            datetime.strptime(target_date[:10], "%Y-%m-%d")
        except (ValueError, TypeError) as ex:
            raise ValueError("target_date must be YYYY-MM-DD") from ex
        target_date = target_date[:10]

    # Best-effort capture of current weight as the starting point (for
    # progress tracking) if the caller didn't supply one. Never fails the
    # call — reads the R2 baseline only.
    if start_weight_kg is None:
        try:
            base = get_athlete_baseline()
            if isinstance(base, dict):
                start_weight_kg = base.get("weight_kg")
        except Exception:  # noqa: BLE001
            pass

    goal = {
        "goal_type": gt,
        "target_weight_kg": round(float(target_weight_kg), 1) if target_weight_kg else None,
        "target_date": target_date,
        "start_weight_kg": round(float(start_weight_kg), 1) if start_weight_kg else None,
        "sex": sex_n,
        "height_cm": round(float(height_cm), 1) if height_cm else None,
        "age": int(age) if age else None,
        "protein_g_per_kg": round(float(protein_g_per_kg), 2) if protein_g_per_kg else None,
        "notes": notes,
        "set_date": date.today().isoformat(),
    }
    cache.put("fueling_goal", {"key": "current"}, goal, key_parts=["current"])
    return {"saved": True, "goal": goal}


def _weeks_remaining(target_date: str | None) -> int | None:
    if not target_date:
        return None
    try:
        td = datetime.strptime(target_date[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    days = (td - date.today()).days
    if days <= 0:
        return 0
    return max(1, (days + 6) // 7)


def get_fueling_goal() -> dict:
    """Return the active fueling goal + live progress (current weight vs
    target, weeks remaining, required daily kcal change, on/off pace + review
    flags). Returns {"goal": None, ...} if none set."""
    goal = cache.get(
        "fueling_goal", {"key": "current"}, key_parts=["current"],
        ttl_seconds=IMMUTABLE_TTL,
    )
    if not goal:
        return {"goal": None, "message": "No fueling goal set — call set_fueling_goal."}

    current_weight = None
    weight_staleness = None
    try:
        base = get_athlete_baseline()
        if isinstance(base, dict):
            current_weight = base.get("weight_kg")
            weight_staleness = (base.get("staleness_days") or {}).get("weight")
    except Exception:  # noqa: BLE001
        pass

    progress: dict[str, Any] = {
        "current_weight_kg": current_weight,
        "weight_staleness_days": weight_staleness,
        "weeks_remaining": _weeks_remaining(goal.get("target_date")),
        "goal_age_days": None,
    }
    sd = goal.get("set_date")
    if sd:
        try:
            progress["goal_age_days"] = (
                date.today() - datetime.strptime(sd, "%Y-%m-%d").date()
            ).days
        except (ValueError, TypeError):
            pass

    tgt = goal.get("target_weight_kg")
    wr = progress["weeks_remaining"]
    if goal["goal_type"] in ("lose", "gain") and tgt and current_weight:
        remaining_kg = round(current_weight - tgt, 1)  # + means still to lose
        progress["kg_to_target"] = remaining_kg
        if wr:
            req_daily = (abs(remaining_kg) * 7700 / wr) / 7  # 7700 kcal/kg
            progress["required_daily_kcal_change"] = (
                -round(req_daily) if goal["goal_type"] == "lose" else round(req_daily)
            )
            if goal["goal_type"] == "lose" and req_daily > 550:
                progress["pace_flag"] = (
                    "Target needs a >550 kcal/day deficit — faster than the "
                    "500/day cap. Consider extending the timeline."
                )

    # Review triggers (mirror skills/project-instructions.md)
    flags = []
    if (progress.get("goal_age_days") or 0) > 28:
        flags.append("goal >4 weeks old — confirm it still holds")
    if wr == 0:
        flags.append("target date has passed")
    if (goal["goal_type"] == "lose" and tgt and current_weight is not None
            and current_weight <= tgt):
        flags.append("target weight already reached")
    if (weight_staleness or 0) > 14:
        flags.append("weight not logged in >14 days — can't verify pace")
    progress["review_flags"] = flags

    return {"goal": goal, "progress": progress}


def _latest_body_stats(lookback_days: int = 30) -> dict:
    """Latest weight + body-fat + lean/muscle mass from Garmin body
    composition (Renpho syncs here). Only weight is consumed elsewhere; this
    surfaces the composition fields for recomposition context."""
    today = date.today()
    out: dict[str, Any] = {
        "weight_kg": None, "body_fat_pct": None, "lean_mass_kg": None,
        "muscle_mass_kg": None, "fat_mass_kg": None, "as_of": None,
        "staleness_days": None,
    }
    try:
        bc = get_body_composition(
            startdate=(today - timedelta(days=lookback_days)).isoformat(),
            enddate=today.isoformat(),
        )
    except Exception:  # noqa: BLE001
        return out
    entries = (bc or {}).get("dateWeightList") or []
    if not entries:
        return out
    latest = max(entries, key=lambda e: (e.get("date") or e.get("calendarDate") or ""))
    w_g = latest.get("weight")
    if w_g:
        out["weight_kg"] = round(w_g / 1000.0, 1)
    bf = latest.get("bodyFat")
    if bf and out["weight_kg"]:
        out["body_fat_pct"] = round(float(bf), 1)
        out["fat_mass_kg"] = round(out["weight_kg"] * bf / 100.0, 1)
        out["lean_mass_kg"] = round(out["weight_kg"] * (1 - bf / 100.0), 1)
    mm = latest.get("muscleMass")
    if mm:
        out["muscle_mass_kg"] = round(mm / 1000.0, 1)
    d_str = latest.get("date") or latest.get("calendarDate")
    if d_str:
        out["as_of"] = d_str[:10]
        try:
            out["staleness_days"] = (
                today - datetime.strptime(d_str[:10], "%Y-%m-%d").date()
            ).days
        except (ValueError, TypeError):
            pass
    return out


def _sport_bucket(title: str, hint: str | None = None) -> str:
    t = f"{hint or ''} {title or ''}".lower()
    if any(k in t for k in ("swim", "pool", "open water")):
        return "swimming"
    if any(k in t for k in ("run", "jog", "treadmill", "track")):
        return "running"
    if any(k in t for k in ("ride", "bike", "cycl", "spin", "rouvy", "zwift", "trainer")):
        return "cycling"
    if any(k in t for k in ("strength", "gym", "lift", "weights", "core", "mobility", "yoga")):
        return "strength"
    if "walk" in t or "hike" in t:
        return "walking"
    return "default"


def _classify_intensity(title: str) -> str:
    t = (title or "").lower()
    if not t.strip():
        return "easy"
    if "recovery" in t:
        return "recovery"
    if "vo2" in t or "v02" in t:
        return "vo2"
    if any(k in t for k in ("threshold", "lthr", "race pace", "interval",
                            "anaerobic", "hard")):
        return "threshold"
    if any(k in t for k in ("tempo", "sweet spot", "sweetspot", "sst",
                            "sub-threshold", "sub threshold")):
        return "tempo"
    if "long" in t:
        return "long"
    if any(k in t for k in ("easy", "endurance", "base", "aerobic",
                            "zone 2", "z2", "conversational")):
        return "easy"
    return "easy"


def _history_kcal_per_hour(history: list[dict]) -> dict[str, float]:
    """Median kcal/hr per sport bucket from recent completed activities."""
    samples: dict[str, list[float]] = {}
    for a in history or []:
        if not isinstance(a, dict):
            continue
        dur_s = a.get("duration") or a.get("elapsedDuration") or a.get("movingDuration")
        cal = a.get("calories")
        if not dur_s or not cal or dur_s < 600:  # skip <10min
            continue
        type_key = (a.get("activityType") or {}).get("typeKey") or ""
        bucket = _sport_bucket(a.get("activityName") or "", type_key)
        kcal_hr = cal / (dur_s / 3600.0)
        if 150 <= kcal_hr <= 1600:  # sanity bounds
            samples.setdefault(bucket, []).append(kcal_hr)
    out = {}
    for bucket, vals in samples.items():
        if len(vals) >= 3:
            vals.sort()
            out[bucket] = round(vals[len(vals) // 2])
    return out


def _planned_hours(item: dict, intensity: str) -> tuple[float, str]:
    """(hours, source). Prefer the linked workout's estimated duration, then
    any duration on the calendar item, then a per-intensity default."""
    for k in ("duration", "estimatedDurationInSecs", "estimatedDurationSecs"):
        v = item.get(k)
        if v:
            return round(v / 3600.0, 2), "calendar"
    wid = item.get("workoutId")
    if wid:
        try:
            wo = get_workout_by_id(wid) or {}
            secs = wo.get("estimatedDurationInSecs") or wo.get("estimatedDurationSecs")
            if secs:
                return round(secs / 3600.0, 2), "workout_detail"
        except Exception:  # noqa: BLE001
            pass
    return _DEFAULT_HOURS.get(intensity, 1.0), "type_default"


def _bmr(weight_kg, sex, height_cm, age):
    """Mifflin-St Jeor when sex/height/age are known, else weight_kg x 22."""
    if weight_kg and height_cm and age and sex in ("male", "female"):
        base = weight_kg * 10 + height_cm * 6.25 - age * 5
        return round(base + (5 if sex == "male" else -161))
    if weight_kg:
        return round(weight_kg * 22)
    return None


def generate_fueling_plan(
    start_date: str | date | None = None,
    days: int = 7,
    save: bool = False,
) -> dict:
    """Build a forward fueling plan: per-day calorie + macro targets and a
    per-workout fuel card for the next `days` days, from the stored fueling
    goal + body stats + Garmin scheduled workouts. Formulas mirror
    skills/weekly.md + skills/project-instructions.md.

    Session burn is calibrated from the athlete's own 90-day history (median
    kcal/hr per sport), falling back to a generic table. Every live fetch
    degrades gracefully so the read-only web service can serve this from the
    nightly pre-warmed cache.

    If `save=True`, merges the per-day plan into the weekly snapshot (under
    nutrition_plan) so nutrition_plan_vs_actual / /morning can track adherence;
    existing snapshot fields are preserved.
    """
    days = max(1, min(int(days), 28))
    start = _coerce_date(start_date) if start_date else date.today()
    end = start + timedelta(days=days - 1)
    notes: list[str] = []

    goal_info = get_fueling_goal()
    goal = goal_info.get("goal")
    if not goal:
        return {
            "no_goal_available": True,
            "message": "No fueling goal set. Call set_fueling_goal (goal_type + "
                       "target_weight_kg + target_date) first.",
            "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        }

    body = _latest_body_stats()
    weight_kg = body.get("weight_kg") or goal.get("start_weight_kg")
    if weight_kg is None:
        try:
            base = get_athlete_baseline()
            weight_kg = base.get("weight_kg") if isinstance(base, dict) else None
        except Exception:  # noqa: BLE001
            pass
    if not weight_kg:
        return {
            "error": "no_weight",
            "message": "No recent weight from Garmin body composition or baseline. "
                       "Log a weigh-in or pass start_weight_kg to set_fueling_goal.",
            "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        }

    has_bmr_inputs = bool(goal.get("height_cm") and goal.get("age") and goal.get("sex"))
    bmr = _bmr(weight_kg, goal.get("sex"), goal.get("height_cm"), goal.get("age"))
    bmr_source = "mifflin_st_jeor" if has_bmr_inputs else "weight_x22_fallback"
    if not has_bmr_inputs:
        notes.append("BMR estimated as weight x22 — set sex/height/age via "
                     "set_fueling_goal for a Mifflin-St Jeor value.")

    # Goal daily kcal adjustment (deficit/surplus)
    wr = _weeks_remaining(goal.get("target_date"))
    gt = goal["goal_type"]
    tgt = goal.get("target_weight_kg")
    protein_per_kg = goal.get("protein_g_per_kg") or _PROTEIN_G_PER_KG_DEFAULT.get(gt, 1.6)
    goal_adj = 0
    if gt == "lose":
        kg_to_lose = (weight_kg - tgt) if tgt else None
        if kg_to_lose and kg_to_lose > 0 and wr:
            raw = (kg_to_lose * 7700 / wr) / 7
        else:
            raw = 400  # moderate default cut when no target/timeline
        goal_adj = -min(round(raw), 500)  # cap deficit at 500/day
        if kg_to_lose and wr and raw > 500:
            notes.append(f"Reaching {tgt}kg by {goal.get('target_date')} needs a "
                         f"{round(raw)} kcal/day deficit; capped at 500 — extend "
                         "the timeline to stay safe.")
    elif gt == "gain":
        goal_adj = 400  # midpoint of +300-500, carb-led

    # Scheduled workouts across the window
    scheduled_by_date: dict[str, list[dict]] = {}
    try:
        for it in get_scheduled_workouts(start.isoformat(), end.isoformat()):
            d_str = (it.get("date") or "")[:10]
            if d_str:
                scheduled_by_date.setdefault(d_str, []).append(it)
    except Exception as ex:  # noqa: BLE001
        notes.append(f"Could not load scheduled workouts ({str(ex)[:120]}); days "
                     "shown assume rest/easy — re-run when the calendar is warm.")

    # 90-day history for burn calibration (best-effort)
    history: list[dict] = []
    try:
        history = get_activities_in_range(
            (start - timedelta(days=90)).isoformat(), start.isoformat()
        ) or []
    except Exception:  # noqa: BLE001
        pass
    hist_kcal_hr = _history_kcal_per_hour(history)

    last_scheduled_date = max(scheduled_by_date) if scheduled_by_date else None

    day_rows = []
    totals = {"target_kcal": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "est_burn_kcal": 0}
    plan_by_date: dict[str, dict] = {}

    for i in range(days):
        d = start + timedelta(days=i)
        d_iso = d.isoformat()
        sessions = []
        for it in scheduled_by_date.get(d_iso, []):
            title = (it.get("title") or it.get("workoutName")
                     or it.get("workoutNameKey") or "")
            intensity = _classify_intensity(title)
            sport = _sport_bucket(title, it.get("sportTypeKey") or "")
            hours, hrs_src = _planned_hours(it, intensity)
            base_hr = hist_kcal_hr.get(sport) or _BASE_KCAL_PER_HOUR.get(
                sport, _BASE_KCAL_PER_HOUR["default"])
            burn = round(base_hr * _INTENSITY_MULT.get(intensity, 1.0) * hours)
            sessions.append({
                "title": title or sport, "sport": sport, "intensity": intensity,
                "hours": hours, "hours_source": hrs_src, "burn_kcal": burn,
                "burn_source": "history" if sport in hist_kcal_hr else "generic_table",
            })
        if not sessions:
            sessions = [{"title": "rest", "sport": "rest", "intensity": "rest",
                         "hours": 0.0, "hours_source": "none", "burn_kcal": 0,
                         "burn_source": "none"}]

        total_burn = sum(s["burn_kcal"] for s in sessions)
        # Periodize carbs off the hardest session of the day
        primary = max(sessions, key=lambda s: (_INTENSITY_ORDER.get(s["intensity"], 2),
                                               s["hours"]))
        carb_ratio = _CARB_G_PER_KG.get(primary["intensity"], 4.0)
        if primary["intensity"] in ("endurance", "long") and primary["hours"] > 2:
            carb_ratio = max(carb_ratio, 7.0)

        expected_expenditure = round(bmr * 1.3 + total_burn)
        target_kcal = max(round(bmr * 1.3 + total_burn + goal_adj), round(bmr * 1.2))
        protein_g = round(weight_kg * protein_per_kg)
        carbs_g = round(weight_kg * carb_ratio)
        # Fat closes the gap to target, with a floor of ~0.5 g/kg
        fat_g = round(max((target_kcal - protein_g * 4 - carbs_g * 4) / 9.0,
                          weight_kg * 0.5))

        fuel_cards = []
        for s in sessions:
            needs = s["hours"] >= 1.25 or s["intensity"] in ("tempo", "threshold", "vo2")
            if not needs or s["intensity"] == "rest":
                continue
            hrs = max(s["hours"], 1.0)
            during_per_hr = 60 if s["hours"] > 2 else (45 if s["hours"] > 1.5 else 35)
            pre = 60 if (s["hours"] >= 2 or s["intensity"] in ("threshold", "vo2")) else 45
            card = {
                "session": s["title"], "intensity": s["intensity"], "hours": s["hours"],
                "pre_carbs_g": pre,
                "during_carbs_g_per_hr": during_per_hr,
                "during_carbs_g_total": round(during_per_hr * hrs),
                "post_protein_g": 25, "post_carbs_g": 60,
                "fluid_ml_per_hr": 600, "sodium_mg_per_hr": 600,
            }
            if s["intensity"] in ("threshold", "vo2", "long") or s["hours"] >= 2:
                card["caffeine_mg"] = round(weight_kg * 3)
            if s["hours"] > 2.5:
                card["note"] = ("long effort — push toward 60-90 g carbs/hr "
                                "(glucose:fructose mix)")
            fuel_cards.append(card)

        row = {
            "date": d_iso, "weekday": d.strftime("%a"),
            "sessions": sessions,
            "primary_intensity": primary["intensity"],
            "est_burn_kcal": total_burn,
            "expected_expenditure_kcal": expected_expenditure,
            "target_kcal": target_kcal,
            "protein_g": protein_g, "carbs_g": carbs_g, "fat_g": fat_g,
            "carb_g_per_kg": carb_ratio,
            "needs_fuel": bool(fuel_cards),
            "fuel": fuel_cards,
        }
        day_rows.append(row)
        for k in ("target_kcal", "protein_g", "carbs_g", "fat_g"):
            totals[k] += row[k]
        totals["est_burn_kcal"] += total_burn

        plan_by_date[d_iso] = {
            "session": "; ".join(s["title"] for s in sessions),
            "target_kcal": target_kcal,
            "expected_expenditure_kcal": expected_expenditure,
            "protein_g": protein_g, "carbs_g": carbs_g, "fat_g": fat_g,
            "notes": "fuel pre/during/post" if fuel_cards else "",
        }

    if last_scheduled_date and last_scheduled_date < end.isoformat():
        notes.append(f"Garmin calendar has workouts through {last_scheduled_date}; "
                     "later days assume rest/easy — re-run when the plan extends.")

    result = {
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "goal": goal,
        "goal_progress": goal_info.get("progress"),
        "body": body,
        "bmr": {"value": bmr, "source": bmr_source, "weight_kg": weight_kg},
        "daily_kcal_adjustment": goal_adj,
        "protein_g_per_kg": protein_per_kg,
        "days": day_rows,
        "totals": totals,
        "notes": notes,
        "no_goal_available": False,
    }

    if save:
        week_monday = (start - timedelta(days=start.weekday())).isoformat()
        existing = cache.get(
            "weekly_snapshots", {"date": week_monday}, key_parts=[week_monday],
            ttl_seconds=IMMUTABLE_TTL,
        )
        snap = existing if isinstance(existing, dict) else {}
        snap["date"] = snap.get("date") or week_monday
        merged = dict(snap.get("nutrition_plan") or {})
        merged.update(plan_by_date)
        snap["nutrition_plan"] = merged
        save_weekly_snapshot(snap)
        result["saved_to_weekly_snapshot"] = snap["date"]

    return result


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

    The python-garminconnect library doesn't wrap this, so we call
    `/calendar-service/year/{Y}/month/{M-1}` directly.
    (Garmin's month is 0-indexed.)
    """
    c = get_client()
    path = f"/calendar-service/year/{year}/month/{month - 1}"
    return _call_with_backoff(c.connectapi, path) or {}


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




def _is_no_data_sentinel(payload: Any) -> bool:
    """True if `payload` is a no-data marker written after a soft throttle
    or empty Garmin response. Sentinels are short-lived cache entries that
    let frequent refreshes skip endpoints which clearly aren't returning
    data yet (e.g. morning_readiness before sleep finishes processing)."""
    return isinstance(payload, dict) and payload.get("_no_data") is True


def _sentinel_expired(sentinel: dict) -> bool:
    """True if a no-data sentinel is past NO_DATA_SOFT_THROTTLE_TTL_SEC."""
    ts = sentinel.get("ts")
    if not isinstance(ts, (int, float)):
        return True  # malformed — re-fetch
    return (time.time() - ts) > NO_DATA_SOFT_THROTTLE_TTL_SEC


def get_daily_summaries(
    startdate: str | date,
    enddate: str | date,
    metrics: list[str],
    force_refresh: bool = False,
    bypass_no_data: bool = False,
) -> dict[str, Any]:
    """Fan out one or more per-day Garmin endpoints across a date range.

    Returns: { metric: { date: data_or_error, ... }, ... }

    Caching: per (metric, date) cached in S3 (if configured). Set
    `force_refresh=True` to bypass the cache entirely.

    `bypass_no_data=True` re-fetches metric-days whose cache entry is a
    no-data sentinel (written when a previous call hit a soft throttle or
    empty body). Use this in the daily anchor refresh so morning data
    that arrived late still gets captured, while the every-6h refresh
    leaves the sentinel alone for its TTL.
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
                    # No-data sentinel: surface as cached miss unless the
                    # sentinel has expired (its own short TTL) or the
                    # caller asked to bypass it (daily anchor run).
                    if _is_no_data_sentinel(hit):
                        if bypass_no_data or _sentinel_expired(hit):
                            tasks.append((m, d))
                            continue
                        result[m][d] = hit
                        continue
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
        except GarminRateLimitError as ex:
            # Soft throttle = empty body. Cache a sentinel so subsequent
            # refreshes within NO_DATA_SOFT_THROTTLE_TTL_SEC skip this
            # metric-day instead of re-hitting Garmin. Hard 429s also
            # write the sentinel so we don't retry within the TTL — the
            # circuit breaker handles process-level abort separately.
            sentinel = {
                "_no_data": True,
                "reason": "soft_throttle" if ex.soft else "rate_limited",
                "ts": time.time(),
            }
            cache.put(
                "daily_summary",
                {"metric": metric, "date": d},
                sentinel,
                key_parts=[metric, d],
            )
            return metric, d, {"error": str(ex)}
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


# ---------- Athlete baseline (dynamic physiology snapshot) ----------


def get_athlete_baseline(force_refresh: bool = False) -> dict[str, Any]:
    """Current physiology snapshot with multi-method threshold cross-
    validation. Computed once/night by daily_refresh.py and served from
    R2 during skill invocations so /morning, /weekly, /session-review
    return in <1s instead of 10-15s.

    Aggregates:
      - VO2max (run + bike) from max_metrics
      - LT HR and run FTP from lactate_threshold
      - Endurance + hill scores from training_score
      - Weight from body_composition
      - Race predictions (5K/10K/half/marathon)
      - Sport-specific 90-day fitness (run/bike/swim)
      - Multi-method threshold analysis with CI, flags, LT1 derivation
      - staleness_days per field

    Caching:
      - Under GARMIN_READONLY=true (web service): always serves from R2,
        NEVER attempts live Garmin calls. Returns whatever's there even
        if technically stale.
      - Under normal mode (nightly Action): TTL is 36h so nightly can
        drift a few hours late without causing a recompute. Pass
        force_refresh=true (as the nightly does) to bypass and rebuild.
    """
    cache_args = {"v": 2}  # bump when baseline schema changes
    key_parts = ["latest"]

    # Readonly mode: always serve from R2. Never attempt live Garmin
    # calls — this is the web MCP path. If cache is genuinely empty,
    # return a clear error payload instead of a misleading partial.
    if READONLY_MODE:
        hit = cache.get("athlete_baseline", cache_args, key_parts=key_parts,
                        ttl_seconds=IMMUTABLE_TTL)
        if hit is not None:
            return hit
        return {
            "error": (
                "No baseline in cache yet. The nightly refresh job computes "
                "this — if it failed or hasn't run since deploy, trigger "
                "daily-refresh from GitHub Actions."
            ),
            "as_of": date.today().isoformat(),
        }

    # Live mode (nightly Action): 36h TTL unless force_refresh=true.
    if not force_refresh:
        hit = cache.get("athlete_baseline", cache_args, key_parts=key_parts,
                        ttl_seconds=36 * 3600)
        if hit is not None:
            return hit

    today = date.today()
    today_iso = today.isoformat()

    def _age_days(d_str: str | None) -> int | None:
        if not d_str:
            return None
        try:
            # Accept "2026-04-15" or "2026-04-15T10:58:02"
            d_clean = d_str.split("T")[0].split(" ")[0]
            d = datetime.strptime(d_clean, "%Y-%m-%d").date()
            return (today - d).days
        except (ValueError, AttributeError):
            return None

    out: dict[str, Any] = {
        "as_of": today_iso,
        "staleness_days": {},
        "notes": [],
    }

    # --- VO2max (via max_metrics for today, fall back to recent days) ---
    # max_metrics returns a LIST of record objects, each with:
    #   {userId, generic: {calendarDate, vo2MaxValue, vo2MaxPreciseValue, ...},
    #    cycling: {...}, heatAltitudeAcclimation: {...}}
    # Garmin may publish same-day data late; extend lookback to 30 days.
    vo2_run = None
    vo2_bike = None
    vo2_run_date = None
    vo2_bike_date = None
    for back in range(0, 30):
        d = (today - timedelta(days=back)).isoformat()
        mm = get_daily_summaries(startdate=d, enddate=d, metrics=["max_metrics"])
        payload = mm.get("max_metrics", {}).get(d)
        # Payload can be list (normal), dict (some older shape), or error.
        records = []
        if isinstance(payload, list):
            records = [r for r in payload if isinstance(r, dict)]
        elif isinstance(payload, dict) and "error" not in payload:
            records = [payload]
        if not records:
            continue
        for rec in records:
            generic = rec.get("generic") or {}
            cycling = rec.get("cycling") or {}
            # Prefer the precise value if present
            run_val = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
            bike_val = cycling.get("vo2MaxPreciseValue") or cycling.get("vo2MaxValue")
            if run_val and vo2_run is None:
                vo2_run = run_val
                vo2_run_date = generic.get("calendarDate") or d
            if bike_val and vo2_bike is None:
                vo2_bike = bike_val
                vo2_bike_date = cycling.get("calendarDate") or d
        if vo2_run is not None and vo2_bike is not None:
            break
    out["vo2max_run"] = round(vo2_run, 1) if vo2_run else None
    out["vo2max_bike"] = round(vo2_bike, 1) if vo2_bike else None
    out["staleness_days"]["vo2max_run"] = _age_days(vo2_run_date)
    out["staleness_days"]["vo2max_bike"] = _age_days(vo2_bike_date)

    # --- LT HR + run FTP (lactate_threshold endpoint) ---
    try:
        lt = get_lactate_threshold()
        shr = lt.get("speed_and_heart_rate") or {}
        pwr = lt.get("power") or {}
        out["lt_hr"] = shr.get("heartRate")
        out["run_ftp_watts"] = pwr.get("functionalThresholdPower")
        weight_kg = pwr.get("weight")
        ptw = pwr.get("powerToWeight")
        out["run_ftp_wkg"] = round(ptw, 2) if ptw else None
        out["weight_kg"] = round(weight_kg, 1) if weight_kg else None
        out["staleness_days"]["lt_hr"] = _age_days(shr.get("calendarDate"))
        out["staleness_days"]["run_ftp"] = _age_days(pwr.get("calendarDate"))
    except Exception as ex:  # noqa: BLE001
        out["notes"].append(f"lactate_threshold lookup failed: {str(ex)[:100]}")

    # --- Endurance + hill scores ---
    try:
        es = get_training_score("endurance", startdate=today_iso)
        dto = es.get("enduranceScoreDTO") or {}
        out["endurance_score"] = dto.get("overallScore") or es.get("avg")
        cls_id = dto.get("classification")
        class_map = {
            1: "Untrained", 2: "Recreational", 3: "Intermediate",
            4: "Trained", 5: "Well-trained", 6: "Expert",
            7: "Superior", 8: "Elite",
        }
        out["endurance_classification"] = class_map.get(cls_id, f"class_{cls_id}")
        out["staleness_days"]["endurance_score"] = _age_days(dto.get("calendarDate"))
    except Exception as ex:  # noqa: BLE001
        out["notes"].append(f"endurance_score lookup failed: {str(ex)[:100]}")

    try:
        hs = get_training_score("hill", startdate=today_iso)
        latest = (hs.get("hillScoreDTOList") or [None])[0] or {}
        out["hill_score"] = latest.get("overallScore")
        hill_cls_id = latest.get("hillScoreClassificationId")
        # Garmin's hill classifications (approximate — 1=lowest, 6=highest)
        hill_class_map = {
            1: "Very low", 2: "Low", 3: "Moderate",
            4: "High", 5: "Very high", 6: "Extreme",
        }
        out["hill_classification"] = hill_class_map.get(hill_cls_id, f"class_{hill_cls_id}")
        out["staleness_days"]["hill_score"] = _age_days(latest.get("calendarDate"))
    except Exception as ex:  # noqa: BLE001
        out["notes"].append(f"hill_score lookup failed: {str(ex)[:100]}")

    # --- Weight (prefer body_composition if logged recently, else LT endpoint) ---
    if out.get("weight_kg") is None:
        try:
            bc = get_body_composition(
                startdate=(today - timedelta(days=30)).isoformat(),
                enddate=today_iso,
            )
            entries = bc.get("dateWeightList") or []
            if entries:
                latest = max(entries, key=lambda e: e.get("date", ""))
                w_grams = latest.get("weight")
                if w_grams:
                    out["weight_kg"] = round(w_grams / 1000.0, 1)
                    out["staleness_days"]["weight"] = _age_days(latest.get("date"))
        except Exception as ex:  # noqa: BLE001
            out["notes"].append(f"body_composition lookup failed: {str(ex)[:100]}")

    # --- Race predictions ---
    try:
        rp = get_race_predictions()
        out["race_predictions"] = {
            "5k_seconds": rp.get("time5K"),
            "10k_seconds": rp.get("time10K"),
            "half_marathon_seconds": rp.get("timeHalfMarathon"),
            "marathon_seconds": rp.get("timeMarathon"),
        }
        out["staleness_days"]["race_predictions"] = _age_days(rp.get("calendarDate"))
    except Exception as ex:  # noqa: BLE001
        out["notes"].append(f"race_predictions lookup failed: {str(ex)[:100]}")

    # --- Derived metrics (MCP-computed, not Garmin) ---
    # W/kg if we have both FTP and weight but not already from LT endpoint.
    ftp = out.get("run_ftp_watts")
    wt = out.get("weight_kg")
    if ftp and wt and not out.get("run_ftp_wkg"):
        out["run_ftp_wkg"] = round(ftp / wt, 2)

    # Cycling FTP: prefer measured 20-min best × 0.95 (classic proxy) over
    # the run-FTP inference. Only fall back to the ratio estimate if no
    # rides have been logged.
    # (We compute this later, after sport_fitness is built, to access the
    # measured value.)

    # VDOT estimate from 5K prediction (Jack Daniels formula)
    t5k_s = (out.get("race_predictions") or {}).get("5k_seconds")
    if t5k_s:
        # Jack Daniels VDOT approximation from 5K time:
        # VDOT ≈ -4.6 + 0.182258 × v + 0.000104 × v²  where v = 5000 / t_min in m/min
        try:
            v = 5000.0 / (t5k_s / 60.0)
            vdot = -4.6 + 0.182258 * v + 0.000104 * (v ** 2)
            out["vdot_from_5k"] = round(vdot, 1)
        except Exception:  # noqa: BLE001
            pass

    # --- Sport-specific activity-derived fitness (last 90 days) ---
    # Uses 3 months of cached activities. 90 days is long enough to catch
    # genuine key sessions (tests, races, intervals) even if training
    # weeks are heavy on recovery, but not so long that stale fitness
    # contaminates current thresholds. Metrics derived from this window
    # are also weighted toward key sessions, not every activity.
    try:
        window_start = (today - timedelta(days=90)).isoformat()
        acts = get_activities_in_range(
            startdate=window_start, enddate=today_iso
        ) or []

        out["sport_fitness"] = {
            "run": _summarize_run_fitness(acts),
            "bike": _summarize_bike_fitness(acts),
            "swim": _summarize_swim_fitness(acts),
        }

        # Split activities by sport for threshold analysis
        run_acts = [a for a in acts
                    if (a.get("activityType") or {}).get("typeKey")
                    in ("running", "treadmill_running", "trail_running")]
        ride_acts = [a for a in acts
                     if (a.get("activityType") or {}).get("typeKey")
                     in ("cycling", "virtual_ride", "indoor_cycling",
                         "gravel_cycling", "road_biking", "mountain_biking")]
        swim_acts = [a for a in acts
                     if (a.get("activityType") or {}).get("typeKey")
                     in ("lap_swimming", "open_water_swimming", "swimming")]

        # Filter to KEY sessions for threshold estimation. All-activity
        # averages include easy/recovery work that pollutes threshold
        # estimates. Key sessions are races, tests, intervals, tempo,
        # threshold, VO2 — sessions where the athlete was deliberately
        # close to or at their limit.
        observed_max_hr_for_filter = max(
            (a.get("maxHR") for a in run_acts if a.get("maxHR")),
            default=None,
        )
        ftp_hint = (out["sport_fitness"].get("bike") or {}).get(
            "ftp_est_from_20min_watts"
        )
        key_run_acts = [a for a in run_acts
                        if thresholds.is_key_run(a, observed_max_hr_for_filter)]
        key_ride_acts = [a for a in ride_acts
                         if thresholds.is_key_ride(a, ftp_hint)]
        key_swim_acts = [a for a in swim_acts if thresholds.is_key_swim(a)]

        out["key_session_counts"] = {
            "run_total": len(run_acts),
            "run_key": len(key_run_acts),
            "bike_total": len(ride_acts),
            "bike_key": len(key_ride_acts),
            "swim_total": len(swim_acts),
            "swim_key": len(key_swim_acts),
        }

        # Observed max HR from any recent hard effort (can be from easy runs
        # too — max HR can spike on hills even in low-effort sessions).
        observed_max_hr = max(
            (a.get("maxHR") for a in acts if a.get("maxHR")),
            default=None,
        )
        # Recent RHR from daily summaries (re-use the recent cache)
        rhr_val = None
        for back in range(0, 14):
            d = (today - timedelta(days=back)).isoformat()
            rr = get_daily_summaries(startdate=d, enddate=d, metrics=["rhr"])
            payload = rr.get("rhr", {}).get(d)
            if isinstance(payload, dict) and "error" not in payload:
                all_m = payload.get("allMetrics", {}).get("metricsMap", {})
                wellness = all_m.get("WELLNESS_RESTING_HEART_RATE", [])
                if wellness:
                    rhr_val = wellness[0].get("value")
                    if rhr_val:
                        break

        # Cycling FTP — prefer user's Garmin Connect setting, which is
        # what shows up in their app zones and is what they trust. Fall
        # back to activity-derived inference if unset.
        user_ftp = None
        user_ftp_date = None
        try:
            cftp = get_cycling_ftp()
            # API returns either a dict or a list of dicts across Garmin versions.
            if isinstance(cftp, list) and cftp:
                cftp = cftp[0]
            if isinstance(cftp, dict):
                user_ftp = (
                    cftp.get("functionalThresholdPower")
                    or cftp.get("ftp")
                    or cftp.get("value")
                )
                user_ftp_date = (
                    cftp.get("ftpCreateTime")
                    or cftp.get("calendarDate")
                    or cftp.get("date")
                )
        except Exception as ex:  # noqa: BLE001
            out["notes"].append(f"cycling_ftp endpoint failed: {str(ex)[:100]}")

        # Inference fallback
        best_20min_key = max(
            (r.get("maxAvgPower_1200") for r in key_ride_acts
             if r.get("maxAvgPower_1200")),
            default=None,
        )

        # Stash Garmin's user-set value as reference; multi_method will
        # compute the authoritative consensus further down.
        if user_ftp:
            out["bike_ftp_garmin_setting_watts"] = round(user_ftp)
            out["staleness_days"]["bike_ftp_garmin_setting"] = _age_days(user_ftp_date)
        # Temporary placeholder — the real bike_ftp_watts comes from the
        # multi_method consensus computed below (after sport_fitness).
        if best_20min_key:
            out["bike_ftp_20min_inference_watts"] = round(best_20min_key * 0.95)

        # --- Multi-method threshold analysis ---
        # Each helper returns {garmin_value, methods, consensus, spread, flag}.
        # Consensus is the median of all methods — a robust cross-check vs.
        # any single source (especially Garmin, which can lag real fitness).
        # Enrich detected-race run candidates with ambient_weather for
        # heat correction. Only fetches details for activities that pass
        # the race-detection heuristic (usually 0-3 per 90 days).
        race_enriched = []
        for a in key_run_acts:
            if thresholds.detect_race_effort(a):
                aid = a.get("activityId")
                if aid:
                    try:
                        det = get_activity_details(str(aid))
                        a = {**a, "ambient_weather": det.get("ambient_weather")}
                    except Exception:  # noqa: BLE001
                        pass
            race_enriched.append(a)

        out["multi_method"] = {
            "run_vo2max": thresholds.run_vo2max_methods(
                garmin_vo2max=out.get("vo2max_run"),
                race_predictions=out.get("race_predictions"),
                run_activities=race_enriched,
                lt_hr=out.get("lt_hr"),
                today=today,
            ),
            "run_lt_hr": thresholds.run_lt_hr_methods(
                garmin_lt_hr=out.get("lt_hr"),
                max_hr=observed_max_hr,
                rhr=rhr_val,
                run_activities=key_run_acts,
            ),
            "run_ftp": thresholds.run_ftp_methods(
                garmin_run_ftp=out.get("run_ftp_watts"),
                run_activities=key_run_acts,
                today=today,
            ),
            "bike_ftp": thresholds.bike_ftp_methods(
                garmin_bike_ftp=out.get("bike_ftp_garmin_setting_watts"),
                ride_activities=key_ride_acts,
                today=today,
            ),
            "bike_vo2max": thresholds.bike_vo2max_methods(
                garmin_vo2max_bike=out.get("vo2max_bike"),
                ride_activities=key_ride_acts,
                weight_kg=out.get("weight_kg"),
            ),
            "swim_css": thresholds.swim_css_methods(
                swim_activities=key_swim_acts,
            ),
        }
        # Observed data points that the threshold helpers used
        out["observed"] = {
            "max_hr": observed_max_hr,
            "rhr": rhr_val,
        }

        # Promote multi-method consensus to the authoritative top-level
        # threshold values. Multi-method IS the source of truth; Garmin's
        # value (when present) lives in the _garmin_setting_watts field
        # for reference.
        bike_mm = out["multi_method"].get("bike_ftp", {})
        bike_consensus = (
            bike_mm.get("if_weighted_consensus")
            or bike_mm.get("consensus")
        )
        if bike_consensus:
            out["bike_ftp_watts"] = round(bike_consensus)
            out["bike_ftp_source"] = (
                f"multi-method consensus (IF-weighted across "
                f"{len(bike_mm.get('methods', []))} methods, excluding Garmin)"
            )

        run_vo2max_mm = out["multi_method"].get("run_vo2max", {})
        run_vo2max_consensus = run_vo2max_mm.get("consensus")
        if run_vo2max_consensus:
            out["vo2max_run_consensus"] = run_vo2max_consensus
            out["vo2max_run_garmin_value"] = out.get("vo2max_run")
            out["vo2max_run"] = round(run_vo2max_consensus, 1)

        run_lt_mm = out["multi_method"].get("run_lt_hr", {})
        run_lt_consensus = run_lt_mm.get("consensus")
        if run_lt_consensus:
            out["lt_hr_consensus"] = run_lt_consensus
            out["lt_hr_garmin_value"] = out.get("lt_hr")
            out["lt_hr"] = round(run_lt_consensus)
    except Exception as ex:  # noqa: BLE001
        out["notes"].append(f"sport_fitness / multi_method aggregation failed: {str(ex)[:150]}")

    cache.put("athlete_baseline", cache_args, out, key_parts=key_parts)
    return out


def _summarize_run_fitness(acts: list[dict]) -> dict[str, Any]:
    """Run-specific fitness from recent activities.
    Pulls fastest recent splits, weekly volume, HR-at-pace baseline.
    """
    runs = [a for a in acts
            if (a.get("activityType") or {}).get("typeKey")
            in ("running", "treadmill_running", "trail_running")]
    if not runs:
        return {"count": 0, "note": "no runs in last 60 days"}

    # Best fastest splits across all runs
    best_5k_s = min(
        (a.get("fastestSplit_5000") for a in runs if a.get("fastestSplit_5000")),
        default=None,
    )
    best_1k_s = min(
        (a.get("fastestSplit_1000") for a in runs if a.get("fastestSplit_1000")),
        default=None,
    )
    best_mile_s = min(
        (a.get("fastestSplit_1609") for a in runs if a.get("fastestSplit_1609")),
        default=None,
    )

    total_m = sum(a.get("distance") or 0 for a in runs)
    total_dur_s = sum(a.get("duration") or 0 for a in runs)
    avg_hr_samples = [a.get("averageHR") for a in runs if a.get("averageHR")]
    vo2_samples = [a.get("vO2MaxValue") for a in runs if a.get("vO2MaxValue")]

    return {
        "count": len(runs),
        "total_km": round(total_m / 1000, 1),
        "total_hours": round(total_dur_s / 3600, 1),
        "weekly_km_avg": round((total_m / 1000) / (len(runs) / 4 if len(runs) >= 4 else 60 / 7), 1),
        "best_1k_seconds": best_1k_s,
        "best_mile_seconds": best_mile_s,
        "best_5k_seconds": best_5k_s,
        "avg_hr": round(sum(avg_hr_samples) / len(avg_hr_samples), 1) if avg_hr_samples else None,
        "vo2max_from_runs_avg": round(sum(vo2_samples) / len(vo2_samples), 1) if vo2_samples else None,
    }


def _summarize_bike_fitness(acts: list[dict]) -> dict[str, Any]:
    """Bike-specific fitness from recent activities.
    Captures 20-min best power, FTP candidate, avg NP, volume.
    """
    rides = [a for a in acts
             if (a.get("activityType") or {}).get("typeKey")
             in ("cycling", "virtual_ride", "indoor_cycling", "gravel_cycling", "road_biking", "mountain_biking")]
    if not rides:
        return {"count": 0, "note": "no rides in last 60 days"}

    # Best 20-min average power — classic FTP proxy (FTP ≈ 95% of 20-min best)
    best_20min_w = max(
        (a.get("maxAvgPower_1200") for a in rides if a.get("maxAvgPower_1200")),
        default=None,
    )
    best_60min_w = max(
        (a.get("maxAvgPower_3600") for a in rides if a.get("maxAvgPower_3600")),
        default=None,
    )
    ftp_est_from_20min = round(best_20min_w * 0.95) if best_20min_w else None

    total_m = sum(a.get("distance") or 0 for a in rides)
    total_dur_s = sum(a.get("duration") or 0 for a in rides)
    np_samples = [a.get("normPower") for a in rides if a.get("normPower")]
    tss_samples = [a.get("trainingStressScore") for a in rides if a.get("trainingStressScore")]

    return {
        "count": len(rides),
        "total_km": round(total_m / 1000, 1),
        "total_hours": round(total_dur_s / 3600, 1),
        "best_20min_watts": best_20min_w,
        "best_60min_watts": best_60min_w,
        "ftp_est_from_20min_watts": ftp_est_from_20min,
        "avg_np_watts": round(sum(np_samples) / len(np_samples)) if np_samples else None,
        "total_tss": round(sum(tss_samples)) if tss_samples else None,
    }


def _summarize_swim_fitness(acts: list[dict]) -> dict[str, Any]:
    """Swim-specific fitness from recent activities.
    Derives CSS (critical swim speed) from best 400m and 1000m splits,
    SWOLF trends, volume.
    """
    swims = [a for a in acts
             if (a.get("activityType") or {}).get("typeKey")
             in ("lap_swimming", "open_water_swimming", "swimming")]
    if not swims:
        return {"count": 0, "note": "no swims in last 60 days"}

    # Critical Swim Speed (CSS) is typically derived from the difference
    # between best 400m and best 200m (or similar) pace. Here we use
    # fastest_split_400 / fastest_split_100 when available.
    best_100_s = min(
        (a.get("fastestSplit_100") for a in swims if a.get("fastestSplit_100")),
        default=None,
    )
    best_400_s = min(
        (a.get("fastestSplit_400") for a in swims if a.get("fastestSplit_400")),
        default=None,
    )
    best_1000_s = min(
        (a.get("fastestSplit_1000") for a in swims if a.get("fastestSplit_1000")),
        default=None,
    )
    best_750_s = min(
        (a.get("fastestSplit_750") for a in swims if a.get("fastestSplit_750")),
        default=None,
    )

    # CSS calculation (Ginn & Mackenzie): (1500m time - 400m time) / 1100m = pace in sec/m
    # Convert to sec/100m for readability.
    css_sec_per_100m = None
    if best_400_s and best_1000_s:
        # Approximate 1500m time by extrapolation if we have 1000m
        d1, t1 = 400, best_400_s
        d2, t2 = 1000, best_1000_s
        # Linear speed model: assume sustainable pace between these points
        sec_per_m_css = (t2 - t1) / (d2 - d1)
        css_sec_per_100m = round(sec_per_m_css * 100, 1)

    total_m = sum(a.get("distance") or 0 for a in swims)
    total_dur_s = sum(a.get("duration") or 0 for a in swims)
    swolf_samples = [a.get("averageSwolf") for a in swims if a.get("averageSwolf")]
    stroke_samples = [
        a.get("averageSwimCadenceInStrokesPerMinute")
        for a in swims
        if a.get("averageSwimCadenceInStrokesPerMinute")
    ]

    return {
        "count": len(swims),
        "total_km": round(total_m / 1000, 1),
        "total_hours": round(total_dur_s / 3600, 1),
        "best_100m_seconds": best_100_s,
        "best_400m_seconds": best_400_s,
        "best_750m_seconds": best_750_s,
        "best_1000m_seconds": best_1000_s,
        "css_sec_per_100m": css_sec_per_100m,
        "css_source": "calculated from best 400m and 1000m splits" if css_sec_per_100m else None,
        "avg_swolf": round(sum(swolf_samples) / len(swolf_samples), 1) if swolf_samples else None,
        "avg_stroke_rate": round(sum(stroke_samples) / len(stroke_samples), 1) if stroke_samples else None,
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
