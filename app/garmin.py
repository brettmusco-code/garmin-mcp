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
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, Optional

from garminconnect import Garmin

from . import cache, races as races_lib, thresholds, tokens, weather

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


# The fueling plan's notion of "today" is a *local training day*, not a UTC
# day. The server runs in UTC (Render), so a bare date.today() rolls over to
# tomorrow at 20:00 Eastern — the plan would show Friday while it's still
# Thursday evening. Anchor "today" to the athlete's local zone instead.
# Override with FUELING_TZ (any IANA name) if you're not on US Eastern.
try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo(os.environ.get("FUELING_TZ", "America/New_York"))
except Exception:  # noqa: BLE001 — bad tz name or missing tzdata: fall back to UTC
    _LOCAL_TZ = None

# After this local time, a scheduled session that still isn't logged is treated
# as a no-show — the day's plan stops assuming it will happen (and stops
# fuelling for it). Late-evening sessions rarely materialise once it's this
# late. Local hour, 24h; override with FUELING_WORKOUT_CUTOFF_HOUR /
# FUELING_WORKOUT_CUTOFF_MIN.
try:
    _WORKOUT_CUTOFF_HOUR = int(os.environ.get("FUELING_WORKOUT_CUTOFF_HOUR", "20"))
    _WORKOUT_CUTOFF_MIN = int(os.environ.get("FUELING_WORKOUT_CUTOFF_MIN", "30"))
except (TypeError, ValueError):
    _WORKOUT_CUTOFF_HOUR, _WORKOUT_CUTOFF_MIN = 20, 30


def _coerce_garmin_date(raw: Any) -> date | None:
    """Parse a Garmin date field into a date. Garmin is inconsistent: usually
    an ISO string ('2026-07-24' or '2026-07-24T...'), but body-composition
    dateWeightList entries often send an epoch — in seconds (10 digits) or
    milliseconds (13). Returns None if it can't be parsed."""
    if raw is None or raw == "":
        return None
    # Numeric (or all-digit string) -> epoch. Milliseconds if it's too big to
    # be a plausible seconds timestamp (>~ year 5138).
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        if v > 1e11:          # milliseconds
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, tz=_LOCAL_TZ).date() if _LOCAL_TZ \
                else datetime.utcfromtimestamp(v).date()
        except (ValueError, OverflowError, OSError):
            return None
    s = str(raw)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _fmt_weight(kg: float | None, units: str | None, decimals: int = 1) -> str:
    """Format a weight for a user-facing string in the goal's display units
    (lb for imperial, else kg). Keeps notes consistent with the dashboard."""
    if kg is None:
        return "—"
    if (units or "").lower() == "imperial":
        return f"{round(kg * 2.20462, decimals)}lb"
    return f"{round(kg, decimals)}kg"


def _local_now() -> datetime:
    """Timezone-aware 'now' in the athlete's local zone (US Eastern default,
    FUELING_TZ override); naive UTC-based fallback if zoneinfo is unavailable."""
    if _LOCAL_TZ is not None:
        return datetime.now(_LOCAL_TZ)
    return datetime.now()


def _local_today() -> date:
    """Local calendar date — the plan's 'today'. Use in place of date.today()
    everywhere 'today' means the athlete's training day, not a UTC day."""
    return _local_now().date()


def _past_workout_cutoff(d: date | None = None) -> bool:
    """True when it's past the evening cutoff on local day `d` (default today),
    i.e. a not-yet-done scheduled session should be treated as a no-show."""
    now = _local_now()
    day = d or now.date()
    if now.date() > day:
        return True          # the day is already over
    if now.date() < day:
        return False         # a future day — nothing has been missed yet
    return (now.hour, now.minute) >= (_WORKOUT_CUTOFF_HOUR, _WORKOUT_CUTOFF_MIN)


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


# --- Weigh-in snapshot: a STABLE cache key for recent weigh-ins ------------
#
# get_body_composition caches per date-range key ({start}__{end}). Every
# weigh-in reader wants "recent weigh-ins ending today", so end=today — which
# rolls at every local midnight, minting a brand-new COLD key each day. On the
# readonly web instance a cold key is a cache miss (live calls are disabled),
# so the readers silently fall back to whatever older data exists and a fresh
# weigh-in never appears until some job happens to warm that exact day's key.
#
# Fix: the refresh jobs fetch recent body composition and persist the parsed
# weigh-ins under ONE stable key (weigh_in_snapshot/current) that never
# date-shifts. All readers (current weight, history, trend, chart) read that
# key, so a new weigh-in reaches the dashboard the moment the next cron tick
# writes it — no date-rollover blanking.
WEIGH_IN_WINDOW_DAYS = 35
# Generous TTL: we'd rather show a stale weight (with its as_of date and the
# >14-day review flag) than blank the dashboard if the cron misses a few runs.
WEIGH_IN_SNAPSHOT_TTL_SEC = 21 * 24 * 3600


def weigh_in_window() -> tuple[str, str]:
    """(start_iso, end_iso) window for a body-composition fetch, anchored to
    the athlete's LOCAL day. Only the refresh path and the cold-snapshot
    fallback hit this date-range key; steady-state reads use the stable
    snapshot below, so day-to-day key rollover no longer blanks the readers."""
    end = _local_today()
    start = end - timedelta(days=WEIGH_IN_WINDOW_DAYS)
    return start.isoformat(), end.isoformat()


def _parse_weigh_ins(bc: Any) -> list[dict]:
    """Normalise Garmin's dateWeightList into sorted (oldest-first) entries:
    {date: ISO, weight_kg, [body_fat_pct], [muscle_mass_kg]}. Handles grams and
    epoch (s/ms) dates. Empty when there are no usable readings."""
    rows = (bc or {}).get("dateWeightList") if isinstance(bc, dict) else None
    out: list[dict] = []
    for r in rows or []:
        d = _coerce_garmin_date(r.get("date") or r.get("calendarDate"))
        w = r.get("weight")
        if w is None or not d:
            continue
        try:
            w = float(w)
        except (TypeError, ValueError):
            continue
        if w > 500:            # grams -> kg
            w /= 1000.0
        entry: dict = {"date": d.isoformat(), "weight_kg": round(w, 2)}
        bf = r.get("bodyFat")
        if bf:
            try:
                entry["body_fat_pct"] = round(float(bf), 1)
            except (TypeError, ValueError):
                pass
        mm = r.get("muscleMass")
        if mm:
            try:
                entry["muscle_mass_kg"] = round(float(mm) / 1000.0, 1)
            except (TypeError, ValueError):
                pass
        out.append(entry)
    out.sort(key=lambda e: e["date"])
    return out


def store_weigh_in_snapshot(entries: list[dict]) -> None:
    """Persist parsed weigh-ins under the stable snapshot key. No-op on empty
    so a transient empty fetch never clobbers a good snapshot."""
    if entries:
        cache.put("weigh_in_snapshot", {}, entries, key_parts=["current"])


def refresh_weigh_in_snapshot(force_refresh: bool = True) -> list[dict]:
    """Fetch recent body composition and persist it under the stable weigh-in
    snapshot key every reader uses. Called by the refresh jobs (intraday +
    nightly) so a new weigh-in reaches the dashboard without waiting for a
    date-keyed cache entry to roll over. Returns the parsed entries."""
    start_iso, end_iso = weigh_in_window()
    bc = get_body_composition(start_iso, end_iso, force_refresh=force_refresh)
    entries = _parse_weigh_ins(bc)
    store_weigh_in_snapshot(entries)
    return entries


def _scale_nutrient(nc: dict, key: str, qty: float, decimals: int = 1) -> float | None:
    """A per-serving nutrient scaled by the logged serving quantity."""
    val = nc.get(key)
    if val is None:
        return None
    try:
        return round(float(val) * qty, decimals)
    except (TypeError, ValueError):
        return None


def _logged_food_entries(fl: Any) -> list[dict]:
    """The day's actual food-log entries, with the logged quantity applied.

    Garmin splits this across two fields and only one of them is the log:

    - `loggedFoodsWithServingSizes` is a reference CATALOGUE — one entry per
      distinct food, listing every serving-size variant that food offers (an
      egg carries large/medium/small/extra large/jumbo/100g). It records no
      quantity at all, so there is no way to know what was eaten from it.
    - `mealDetails[].loggedFoods[]` is the actual log: `servingQty` is how many
      were eaten and `nutritionContent` (singular) holds the per-serving values.

    Reading the catalogue and taking its first variant is how "3 eggs" came out
    as one large egg's 74 kcal instead of 222. Returns entries in meal order
    with quantity applied. Empty when the day has no log.
    """
    if not isinstance(fl, dict) or "error" in fl:
        return []
    out: list[dict] = []
    for md in (fl.get("mealDetails") or []):
        if not isinstance(md, dict):
            continue
        meal_name = (md.get("meal") or {}).get("mealName")
        for it in (md.get("loggedFoods") or []):
            if not isinstance(it, dict):
                continue
            meta = it.get("foodMetaData") or {}
            name = " ".join(x for x in [meta.get("brandName"), meta.get("foodName")] if x) or "food"
            nc = it.get("nutritionContent") or {}
            try:
                qty = float(it.get("servingQty"))
            except (TypeError, ValueError):
                qty = 1.0
            if qty <= 0:
                qty = 1.0
            kcal = _scale_nutrient(nc, "calories", qty, 0)
            out.append({
                "name": name,
                "qty": round(qty, 2),
                "unit": nc.get("servingUnit"),
                "kcal": round(kcal) if kcal is not None else None,
                "protein_g": _scale_nutrient(nc, "protein", qty),
                "carbs_g": _scale_nutrient(nc, "carbs", qty),
                "fat_g": _scale_nutrient(nc, "fat", qty),
                "meal": meal_name,
            })
    return out


def _weigh_in_entries() -> list[dict]:
    """Canonical recent weigh-ins (oldest first) that every reader shares —
    Garmin's series with any manual weigh-ins merged on top (manual wins for
    the days it covers). Returns [] when nothing is available."""
    return _merge_manual_weigh_ins(_garmin_weigh_in_entries())


def _garmin_weigh_in_entries() -> list[dict]:
    """Garmin-sourced recent weigh-ins (oldest first), before any manual merge.
    Reads the STABLE snapshot key first (written by the refresh jobs); falls
    back to a date-range fetch and, on a writer instance, seeds the snapshot so
    later reads are stable. Returns [] when nothing is available."""
    snap = cache.get("weigh_in_snapshot", {}, key_parts=["current"],
                     ttl_seconds=WEIGH_IN_SNAPSHOT_TTL_SEC)
    if isinstance(snap, list):
        return snap
    # Cold snapshot (first run after deploy, or expired): fall back to the
    # date-range endpoint. On a writer this does a live fetch and seeds the
    # stable key; on the readonly web instance it returns the date-keyed cache
    # entry if present, else [] (live calls disabled).
    try:
        start_iso, end_iso = weigh_in_window()
        bc = get_body_composition(start_iso, end_iso)
        entries = _parse_weigh_ins(bc)
    except Exception:  # noqa: BLE001
        entries = []
    # Last resort (readonly web instance, before the first cron tick seeds the
    # snapshot): today's date-range key may miss, but an earlier day's key is
    # likely still cached. Scan the body_composition cache and use the freshest
    # entry we can find, so the dashboard shows a weight instead of blanking.
    if not entries and READONLY_MODE and cache.enabled():
        entries = _scan_cached_weigh_ins()
    if entries and not READONLY_MODE:
        store_weigh_in_snapshot(entries)
    return entries


# --- Manual weigh-ins: user-logged weights stored under a stable R2 key -----
#
# Logged from the dashboard (or an MCP tool) when the Garmin scale isn't the
# source of truth — a manual entry fills a gap or corrects a bad reading. Kept
# in ONE stable key (manual_weigh_in/log), a date->entry map so re-logging the
# same day overwrites rather than duplicates. Merged into the canonical weigh-in
# series by _weigh_in_entries, where a manual entry WINS over Garmin's for the
# same day — so the trend, projection, BMR and EA all pick it up automatically.
# Writable from the readonly web instance (READONLY_MODE only blocks live
# Garmin calls, not R2 writes).
_MANUAL_WEIGH_IN_TTL_SEC = 365 * 24 * 3600  # keep a year; readers window it down


def _manual_weigh_ins() -> dict[str, dict]:
    """The stored manual weigh-in map (date ISO -> entry). {} when none/unset."""
    got = cache.get("manual_weigh_in", {}, key_parts=["log"],
                    ttl_seconds=_MANUAL_WEIGH_IN_TTL_SEC)
    return got if isinstance(got, dict) else {}


def log_weight(weight_kg: float, entry_date: str | date | None = None,
               body_fat_pct: float | None = None) -> dict:
    """Record a manual weigh-in. Overwrites any existing manual entry for the
    same day. Persists to R2 under the stable manual-weigh-in key, then rebuilds
    the weigh-in snapshot so the new reading reaches every reader (current
    weight, trend, projection, BMR, EA) on the next dashboard load.

    weight_kg: the weight in KILOGRAMS (callers convert from lb before here).
    entry_date: ISO date or date; defaults to the athlete's local today.
    Returns {saved, date, weight_kg, count} or {saved: False, error}."""
    try:
        w = float(weight_kg)
    except (TypeError, ValueError):
        return {"saved": False, "error": "weight must be a number"}
    if not (20.0 <= w <= 400.0):   # kg sanity bounds — catch lb entered as kg
        return {"saved": False, "error": f"weight {w} kg is out of range (20–400 kg)"}
    d = _coerce_date(entry_date) if entry_date else _local_today()
    d_iso = d.isoformat()
    if d_iso > _local_today().isoformat():
        return {"saved": False, "error": "cannot log a weigh-in in the future"}
    if not cache.enabled():
        return {"saved": False, "error": "cache/storage unavailable — cannot persist the weigh-in"}
    entry: dict = {"date": d_iso, "weight_kg": round(w, 2), "source": "manual"}
    if body_fat_pct is not None:
        try:
            entry["body_fat_pct"] = round(float(body_fat_pct), 1)
        except (TypeError, ValueError):
            pass
    log = _manual_weigh_ins()
    log[d_iso] = entry
    cache.put("manual_weigh_in", {}, log, key_parts=["log"])
    # Rebuild the merged snapshot so readers see the entry immediately (rather
    # than waiting for the next cron tick to reseed it).
    try:
        merged = _merge_manual_weigh_ins(_garmin_weigh_in_entries())
        if merged:
            store_weigh_in_snapshot(merged)
    except Exception:  # noqa: BLE001
        pass
    return {"saved": True, "date": d_iso, "weight_kg": round(w, 2), "count": len(log)}


def _merge_manual_weigh_ins(garmin_entries: list[dict]) -> list[dict]:
    """Merge manual weigh-ins into a Garmin-sourced series. Manual entries WIN
    for any day they cover; other days keep the Garmin reading. Returns the
    combined series sorted oldest-first."""
    manual = _manual_weigh_ins()
    if not manual:
        return garmin_entries
    by_date: dict[str, dict] = {e["date"]: e for e in garmin_entries}
    by_date.update(manual)   # manual wins on same-day collisions
    return sorted(by_date.values(), key=lambda e: e["date"])


def _scan_cached_weigh_ins() -> list[dict]:
    """Best-effort: read every cached body_composition object and return the
    parsed weigh-ins from whichever range holds the newest reading. Bounded to
    a handful of keys; swallows all errors (pure fallback)."""
    best: list[dict] = []
    best_latest = ""
    try:
        for key in cache.list_keys("body_composition", limit=40):
            # key: PREFIX/body_composition/<start>__<end>.json
            leaf = key.rsplit("/", 1)[-1][:-5] if key.endswith(".json") else None
            if not leaf or "__" not in leaf:
                continue
            start_s, _, end_s = leaf.partition("__")
            try:
                bc = get_body_composition(start_s, end_s)
            except Exception:  # noqa: BLE001
                continue
            entries = _parse_weigh_ins(bc)
            if entries and entries[-1]["date"] > best_latest:
                best_latest = entries[-1]["date"]
                best = entries
    except Exception:  # noqa: BLE001
        return best
    return best


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
    today_d = _local_today()   # local day so "today is in progress" is correct

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

    # Days the athlete flagged as inaccurately/incompletely logged.
    ignored_days = _load_ignored_food_days()

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
            foods_count = len(_logged_food_entries(fl))
            goals = fl.get("dailyNutritionGoals") or {}
            garmin_goal = goals.get("adjustedCalories") or goals.get("calories")

        # Explicitly ignored: present the day as unlogged so every consumer of
        # these rows (rebalance drift, coaching suggestions, window totals)
        # skips it, and carry the marker so the UI can explain the gap rather
        # than reading it as a day the athlete simply forgot to log.
        ignored_entry = ignored_days.get(d_iso)
        if ignored_entry:
            consumed, foods_count = {}, 0

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
            "ignored": bool(ignored_entry),
            "ignored_reason": (ignored_entry or {}).get("reason"),
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
            # True when the athlete flagged this day as badly logged — its
            # intake is deliberately excluded, not merely missing.
            "ignored": raw["ignored"],
            "ignored_reason": raw["ignored_reason"],
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
    today_d = _local_today()   # local day anchors the trailing window correctly
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
                # date may be ISO or a Garmin epoch (s/ms) — coerce both.
                d = _coerce_garmin_date(entry.get("date") or entry.get("calendarDate"))
                w_grams = entry.get("weight")
                if d and w_grams:
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
    ignored_days = _load_ignored_food_days()

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

        # A snapshot's intake totals were computed before the athlete flagged
        # any of its days as badly logged, so it still bakes in the bad day.
        # Re-synthesise the week from raw data instead, where the exclusion
        # can actually be applied.
        if snap_match and any(d in ignored_days for d in week_days):
            snap_match = None

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
                # Ignored days contribute no intake (the burn below still
                # counts — the flag is about what was eaten, not what was
                # spent), so a badly-logged day can't drag the week's average
                # down and, through it, the adaptive-TDEE maintenance estimate.
                if isinstance(fl, dict) and "error" not in fl and d_iso not in ignored_days:
                    content = fl.get("dailyNutritionContent") or {}
                    foods_count = len(_logged_food_entries(fl))
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
# Rough default paces (m/s) to convert a distance-only workout into a duration
# when neither an explicit estimate nor time-based steps are available.
_DEFAULT_PACE_MPS = {"running": 3.0, "cycling": 7.8, "swimming": 1.0, "walking": 1.4}
_PROTEIN_G_PER_KG_DEFAULT = {"lose": 2.2, "maintain": 1.6, "gain": 1.8}
# Non-exercise activity factor on top of RMR (NEAT only, *excluding* the
# thermic effect of food and exercise, which are added separately and
# explicitly). A sedentary-to-light desk-plus-training athlete sits ~1.15;
# BMR x 1.15 + explicit TEF recovers the classic ~1.3 "lightly active" TDEE
# at maintenance, but now reacts to protein and to the size of the deficit.
_NEAT_MULT = 1.15
# Thermic effect of food: fraction of each macro's energy burned digesting it.
# Protein is far more expensive to process than carbs or fat — the lever that
# rewards a high-protein cut.
_TEF_FRAC = {"protein": 0.25, "carbs": 0.08, "fat": 0.02}
# Deficit-periodization weights: how much of the weekly deficit each day
# type attracts. Rest/easy days bank the cut; hard days stay near
# maintenance ("fuel for the work required").
_DEFICIT_WEIGHTS = {
    "rest": 1.4, "recovery": 1.2, "easy": 1.0, "endurance": 1.0,
    "long": 0.4, "tempo": 0.7, "threshold": 0.4, "vo2": 0.4,
}
# Rank used to pick the day's "hardest" session for carb periodization.
_INTENSITY_ORDER = {
    "rest": 0, "recovery": 1, "easy": 2, "endurance": 3,
    "long": 4, "tempo": 5, "threshold": 6, "vo2": 7,
}
# Intensity factor (fraction of threshold effort) per intensity label. Turns a
# session's duration into a TSS-style training-load score:
#     TSS = hours x IF^2 x 100     (one hour at threshold == 100)
# The square is the point: intensity counts non-linearly, so a hard hour drives
# far more load than an easy one — which is what should decide how much of the
# weekly deficit a day can safely absorb. A flat duration x label multiplier
# (the old proxy) treated two easy hours the same as ~50 min of threshold and
# so mislabeled a genuinely demanding aerobic day as "easy."
_INTENSITY_FACTOR = {
    "rest": 0.0, "recovery": 0.55, "easy": 0.65, "endurance": 0.70,
    "long": 0.70, "tempo": 0.85, "threshold": 0.95, "vo2": 1.05,
}


def _session_tss(intensity: str, hours: float) -> float:
    """Lightweight TSS proxy for one session: hours x IF^2 x 100."""
    if_ = _INTENSITY_FACTOR.get(intensity, 0.65)
    return max(0.0, hours) * if_ * if_ * 100.0


def _allocate_deficit(bases: list[float], intensities: list[str], loads: list[float],
                      flat_adj: float, floors: list[float]) -> tuple[list[int], int]:
    """Spread the weekly deficit (flat_adj x days, negative) across days,
    weighted toward rest/easy days — but scaled by each day's actual
    training LOAD, a lightweight TSS proxy (hours x IF^2 x 100, one hour at
    threshold == 100), not just its single hardest session's category label.
    TSS counts intensity non-linearly, so two easy hours (~85 TSS) read as a
    real training day while twenty minutes of the same easy work (~14 TSS)
    reads as near-rest — a duration-only proxy conflated them.

    Constraints: every day's target stays >= its floor (floors[i], >= 0 —
    typically max(BMR-multiple, EA-min x FFM + that day's burn)). A
    categorically hard session (tempo/threshold/vo2/long) OR a day whose
    cumulative TSS crosses LOAD_PROTECT_TSS never takes a deeper cut than the
    flat per-day amount — periodization can only make those days easier,
    never harder. Water-fills clamped days' residual onto the rest.
    Returns (per-day adjustments, unabsorbed weekly residual <= 0)."""
    n = len(bases)
    budget = flat_adj * n
    hard = {"tempo", "threshold", "vo2", "long"}
    # ~1.75h of easy aerobic work, or ~50min at threshold: above this a day
    # carries enough training load to be protected from a deeper-than-flat
    # cut, even when its hardest single session is only labelled "easy".
    LOAD_PROTECT_TSS = 75.0

    def _weight(i: int) -> float:
        base = _DEFICIT_WEIGHTS.get(intensities[i], 1.0)
        # High same-day training load pulls the weight down — bank less of
        # the cut there — even when the hardest single session that day is
        # only "easy". 0.004/TSS ~ a 0.37 pull at Sunday's ~92 TSS.
        return max(0.25, base - 0.004 * loads[i])

    def _min_adj(i: int) -> float:
        lo = floors[i] - bases[i]           # keep target >= its floor
        if intensities[i] in hard or loads[i] >= LOAD_PROTECT_TSS:
            lo = max(lo, flat_adj)          # never deeper than the flat cut
        return min(lo, 0.0)

    adjs = [0.0] * n
    active = set(range(n))
    remaining = budget
    for _ in range(n + 2):
        if remaining >= -1 or not active:
            break
        sw = sum(_weight(i) for i in active)
        if sw <= 0:
            break
        rem0 = remaining
        for i in list(active):
            share = rem0 * _weight(i) / sw
            lo = _min_adj(i)
            if adjs[i] + share <= lo:
                adjs[i] = lo
                active.discard(i)
            else:
                adjs[i] += share
        remaining = budget - sum(adjs)
    return [round(a) for a in adjs], round(min(remaining, 0))


def set_fueling_goal(
    goal_type: str,
    target_weight_kg: float | None = None,
    target_date: str | None = None,
    start_weight_kg: float | None = None,
    sex: str | None = None,
    height_cm: float | None = None,
    age: int | None = None,
    protein_g_per_kg: float | None = None,
    max_deficit_kcal: float | None = None,
    ea_floor: float | None = None,
    ea_min: float | None = None,
    min_kcal: float | None = None,
    bmr_floor_mult: float | None = None,
    periodize_deficit: bool | None = None,
    front_load: float | None = None,
    max_loss_lb_per_week: float | None = None,
    use_adaptive_tdee: bool | None = None,
    home_lat: float | None = None,
    home_lon: float | None = None,
    skip_breakfast_weekdays: bool | None = None,
    current_weight_kg: float | None = None,
    units: str | None = None,
    notes: str | None = None,
    aggressive: bool | None = None,
    rebalance_deficit_only: bool | None = None,
) -> dict:
    """Persist the athlete's fueling goal to R2 (single active goal, keyed
    'current'). This is the target weight + timeline the fueling plan is built
    around, plus the BMR inputs Garmin doesn't reliably expose (sex/height/age).
    Overwrites any prior goal; the set date is recorded so skills can flag a
    stale goal.

    Safety knobs (defaults keep the plan conservative):
      max_deficit_kcal: cap on the daily deficit. Default 500 when unset;
        pass 0 (or negative) to remove the cap entirely. The BMR x 1.2 floor
        on the daily target is separate and always applies.
      ea_floor: energy-availability warning threshold in kcal/kg fat-free
        mass. Default 30 when unset; lower it (or 0) to suppress warnings.
      ea_min: ENFORCED energy-availability minimum — every day's calorie
        target is floored at ea_min x FFM + that day's exercise burn, so the
        floor scales with training. Unset = not enforced. (Reference: 30
        conservative, 25 aggressive-but-reasonable, below that is RED-S
        territory.)
      min_kcal: absolute daily calorie floor regardless of anything else.
        Unset = none.
      bmr_floor_mult: floor on the daily calorie target as a BMR multiple.
        Default 1.2 when unset; pass 0 to drop the floor entirely (targets
        may then fall below BMR — the plan will say so, loudly).
      periodize_deficit: shift the weekly deficit toward rest/easy days so
        hard sessions stay near maintenance. Default true for lose goals;
        pass false for a flat daily deficit.
      front_load: 0..0.9 — steeper deficit early, tapering as weight nears
        target (fat loss slows as you lean out). At the start weight the
        deficit runs (1 + front_load)x the linear pace; at the midpoint,
        the linear pace; near target, (1 - front_load)x. Recomputed from
        each weigh-in, so it self-tapers. 0/unset = flat linear pace.
      aggressive: hold the MAX sustainable deficit (bounded by the EA/BMR
        floors + max_loss_lb_per_week cap) instead of pacing to the target
        date, and don't ease off when ahead of schedule. Losing weight then
        pulls the finish date EARLIER rather than just softening the cut.
        Default false — the date-paced, ahead-of-schedule-easing behavior.
        The per-day energy-availability floor still applies, so "aggressive"
        can never push a day below the safe EA minimum.
      rebalance_deficit_only: make the rolling rebalance one-directional. When
        true, rebalancing may only TIGHTEN the coming days' targets to pay back
        an overage (you ate more than the adjusted target) — it will never RAISE
        them to give calories back after you undereat. Keeps the deficit from
        being eroded by good days. Default false (two-way rebalance).

    goal_type: 'lose' | 'gain' | 'maintain'. For 'lose'/'gain' provide
    target_weight_kg and target_date so a daily deficit/surplus can be computed.
    """
    gt = (goal_type or "").strip().lower()
    if gt not in GOAL_TYPES:
        raise ValueError(f"goal_type must be one of {GOAL_TYPES}")
    sex_n = (sex or "").strip().lower() or None
    if sex_n and sex_n not in ("male", "female"):
        raise ValueError("sex must be 'male' or 'female'")
    units_n = (units or "").strip().lower() or None
    if units_n and units_n not in ("metric", "imperial"):
        raise ValueError("units must be 'metric' or 'imperial'")
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

    # start_date marks when the BASELINE was set, not when the goal was last
    # written. Re-anchor only if start_weight_kg actually moved; otherwise carry
    # the stored anchor forward. Stamping it unconditionally meant rewriting a
    # goal with unchanged values silently re-anchored the block and re-windowed
    # the weight chart (see _goal_baseline_date), discarding history for an edit
    # that didn't touch the baseline at all.
    start_w = round(float(start_weight_kg), 1) if start_weight_kg else None
    start_date = _local_today().isoformat()
    prior = cache.get(
        "fueling_goal", {"key": "current"}, key_parts=["current"],
        ttl_seconds=IMMUTABLE_TTL,
    )
    if isinstance(prior, dict) and start_w is not None:
        prior_w = prior.get("start_weight_kg")
        # Goals written before start_date existed keep their anchor in set_date,
        # which at that point still meant "when the goal was set".
        prior_anchor = prior.get("start_date") or prior.get("set_date")
        if prior_anchor and prior_w is not None:
            try:
                if abs(float(prior_w) - start_w) < 0.05:
                    start_date = str(prior_anchor)[:10]
            except (TypeError, ValueError):
                pass

    goal = {
        "goal_type": gt,
        "target_weight_kg": round(float(target_weight_kg), 1) if target_weight_kg else None,
        "target_date": target_date,
        "start_weight_kg": start_w,
        "sex": sex_n,
        "height_cm": round(float(height_cm), 1) if height_cm else None,
        "age": int(age) if age else None,
        "protein_g_per_kg": round(float(protein_g_per_kg), 2) if protein_g_per_kg else None,
        "max_deficit_kcal": round(float(max_deficit_kcal)) if max_deficit_kcal is not None else None,
        "ea_floor": round(float(ea_floor), 1) if ea_floor is not None else None,
        "ea_min": round(float(ea_min), 1) if ea_min is not None else None,
        "min_kcal": round(float(min_kcal)) if min_kcal is not None else None,
        "bmr_floor_mult": round(float(bmr_floor_mult), 2) if bmr_floor_mult is not None else None,
        "periodize_deficit": bool(periodize_deficit) if periodize_deficit is not None else None,
        "front_load": round(float(front_load), 2) if front_load is not None else None,
        "max_loss_lb_per_week": round(float(max_loss_lb_per_week), 2) if max_loss_lb_per_week is not None else None,
        "use_adaptive_tdee": bool(use_adaptive_tdee) if use_adaptive_tdee is not None else None,
        "home_lat": round(float(home_lat), 4) if home_lat is not None else None,
        "home_lon": round(float(home_lon), 4) if home_lon is not None else None,
        "skip_breakfast_weekdays": bool(skip_breakfast_weekdays) if skip_breakfast_weekdays is not None else None,
        "aggressive": bool(aggressive) if aggressive is not None else None,
        "rebalance_deficit_only": bool(rebalance_deficit_only) if rebalance_deficit_only is not None else None,
        # Manual current-weight override — used when Garmin's synced weight is
        # stale or wrong. Wins over the Garmin reading everywhere (progress,
        # BMR, EA, projection) until cleared. Stamped with the date it was set.
        "current_weight_kg": round(float(current_weight_kg), 1) if current_weight_kg else None,
        "current_weight_as_of": _local_today().isoformat() if current_weight_kg else None,
        "units": units_n,
        "notes": notes,
        # When this block started — the day start_weight_kg was established.
        # set_fueling_goal is a re-anchor, so it moves; update_fueling_goal is a
        # tweak to the block in progress, so it preserves it. Distinct from
        # set_date, which both stamp and therefore only means "last edited".
        # The weight chart windows on this, so re-anchoring drops the previous
        # block's weigh-ins from the plot — which is why it moves only when
        # start_weight_kg does (resolved above).
        "start_date": start_date,
        "set_date": _local_today().isoformat(),
    }
    cache.put("fueling_goal", {"key": "current"}, goal, key_parts=["current"])
    # Verify the write actually landed. cache.put swallows errors (e.g. the
    # read-only web service having write-denied R2 credentials), so without
    # this read-back the caller would be told the goal saved when it silently
    # vanished — which is exactly how a set goal can fail to persist and leave
    # every later generate_fueling_plan returning no_goal_available.
    persisted = cache.get(
        "fueling_goal", {"key": "current"}, key_parts=["current"],
        ttl_seconds=IMMUTABLE_TTL,
    )
    if not persisted:
        return {
            "saved": False,
            "goal": goal,
            "error": (
                "Goal was NOT persisted — the server could not write it to the "
                "cache (R2). This usually means the web service has read-only "
                "S3/R2 credentials; give it read-write keys (AWS_ACCESS_KEY_ID / "
                "AWS_SECRET_ACCESS_KEY, matching the cron writer) and retry."
            ),
        }
    return {"saved": True, "goal": goal}


# Sentinel for update_fueling_goal: distinguishes "field not supplied, keep the
# existing value" from "supplied as None, clear it". None can't do double duty
# here because clearing (e.g. removing a max_deficit cap) is a real edit.
_KEEP = object()


def update_fueling_goal(
    goal_type: str | object = _KEEP,
    target_weight_kg: float | None | object = _KEEP,
    target_date: str | None | object = _KEEP,
    protein_g_per_kg: float | None | object = _KEEP,
    max_deficit_kcal: float | None | object = _KEEP,
    max_loss_lb_per_week: float | None | object = _KEEP,
    periodize_deficit: bool | None | object = _KEEP,
    front_load: float | None | object = _KEEP,
    aggressive: bool | None | object = _KEEP,
    skip_breakfast_weekdays: bool | None | object = _KEEP,
    units: str | None | object = _KEEP,
    current_weight_kg: float | None | object = _KEEP,
) -> dict:
    """Merge a partial set of changes onto the CURRENT fueling goal and persist.

    Unlike set_fueling_goal (which rewrites the whole goal and clears any param
    the caller omits), this reads the stored goal, overlays only the fields you
    pass, and leaves everything else — BMR inputs, EA settings, home coords, the
    manual current_weight override and its as-of stamp — untouched. This is what
    the dashboard's goal-edit form uses so it can expose a handful of knobs
    without wiping the rest of the goal.

    Every parameter defaults to a keep-sentinel: omit it to preserve the stored
    value; pass None to clear it (where clearing is meaningful, e.g. removing a
    deficit cap). set_date is refreshed so stale-goal flags reset on any edit.

    current_weight_kg is the one field here that is normally left alone but can
    now be cleared: pass None once Garmin syncs a real weigh-in, so the override
    stops standing in for it. Clearing it through set_fueling_goal instead would
    also re-stamp start_date and so re-anchor the block — which is a different
    decision, and not one that clearing a stale override should make for you.
    """
    current = cache.get(
        "fueling_goal", {"key": "current"}, key_parts=["current"],
        ttl_seconds=IMMUTABLE_TTL,
    )
    if not isinstance(current, dict) or not current.get("goal_type"):
        return {
            "saved": False,
            "error": (
                "No fueling goal exists yet to edit — create one first with "
                "set_fueling_goal (run /fuel in Claude)."
            ),
        }
    goal = dict(current)

    changes = {
        "goal_type": goal_type,
        "target_weight_kg": target_weight_kg,
        "target_date": target_date,
        "protein_g_per_kg": protein_g_per_kg,
        "max_deficit_kcal": max_deficit_kcal,
        "max_loss_lb_per_week": max_loss_lb_per_week,
        "periodize_deficit": periodize_deficit,
        "front_load": front_load,
        "aggressive": aggressive,
        "skip_breakfast_weekdays": skip_breakfast_weekdays,
        "units": units,
        "current_weight_kg": current_weight_kg,
    }

    def _num(v, nd=None):
        if v is None:
            return None
        f = float(v)
        return round(f, nd) if nd is not None else round(f)

    for field, val in changes.items():
        if val is _KEEP:
            continue
        if field == "goal_type":
            gt = (val or "").strip().lower()
            if gt not in GOAL_TYPES:
                raise ValueError(f"goal_type must be one of {GOAL_TYPES}")
            goal["goal_type"] = gt
        elif field == "target_date":
            if val:
                try:
                    datetime.strptime(str(val)[:10], "%Y-%m-%d")
                except (ValueError, TypeError) as ex:
                    raise ValueError("target_date must be YYYY-MM-DD") from ex
                goal["target_date"] = str(val)[:10]
            else:
                goal["target_date"] = None
        elif field == "units":
            un = (val or "").strip().lower() or None
            if un and un not in ("metric", "imperial"):
                raise ValueError("units must be 'metric' or 'imperial'")
            goal["units"] = un
        elif field == "target_weight_kg":
            goal["target_weight_kg"] = _num(val, 1) if val else None
        elif field == "current_weight_kg":
            # The as-of stamp only means something while an override exists, so
            # the two move together.
            if val:
                w = float(val)
                if not (20.0 <= w <= 400.0):
                    raise ValueError(f"current_weight_kg {w} is out of range (20-400 kg)")
                goal["current_weight_kg"] = round(w, 1)
                goal["current_weight_as_of"] = _local_today().isoformat()
            else:
                goal["current_weight_kg"] = None
                goal["current_weight_as_of"] = None
        elif field == "protein_g_per_kg":
            goal["protein_g_per_kg"] = _num(val, 2) if val else None
        elif field == "front_load":
            goal["front_load"] = _num(val, 2) if val is not None else None
        elif field == "max_loss_lb_per_week":
            goal["max_loss_lb_per_week"] = _num(val, 2) if val is not None else None
        elif field == "max_deficit_kcal":
            goal["max_deficit_kcal"] = _num(val) if val is not None else None
        elif field in ("periodize_deficit", "aggressive", "skip_breakfast_weekdays"):
            goal[field] = bool(val) if val is not None else None

    # A tweak, not a re-anchor: keep the block's start_date. Goals stored before
    # start_date existed get it backfilled from their set_date — at that point
    # set_date still means "when the goal was set", so it's the right anchor,
    # and capturing it here stops the stamp below from losing it.
    if not goal.get("start_date"):
        goal["start_date"] = goal.get("set_date") or _local_today().isoformat()
    goal["set_date"] = _local_today().isoformat()

    cache.put("fueling_goal", {"key": "current"}, goal, key_parts=["current"])
    persisted = cache.get(
        "fueling_goal", {"key": "current"}, key_parts=["current"],
        ttl_seconds=IMMUTABLE_TTL,
    )
    if not persisted:
        return {
            "saved": False,
            "goal": goal,
            "error": (
                "Goal changes were NOT persisted — the server could not write to "
                "the cache (R2). The web service likely has read-only S3/R2 "
                "credentials; give it read-write keys and retry."
            ),
        }
    return {"saved": True, "goal": goal}


def skip_scheduled_session(
    date: str,
    sport: str | None = None,
    title_contains: str | None = None,
) -> dict:
    """Exclude a scheduled session from generate_fueling_plan — for when the
    Garmin calendar still lists something you're not actually doing (skipped,
    swapped, rescheduled) and you don't want it inflating that day's calorie
    or carb targets. Persisted to R2; entries auto-expire once their date is
    in the past.

    date: YYYY-MM-DD, the scheduled day to affect.
    sport: optional sport filter (e.g. 'swimming', 'cycling') — case-insensitive.
    title_contains: optional case-insensitive substring of the session title.

    Omit both sport and title_contains to skip every session scheduled that
    day. If both are given, a session must match both to be skipped.
    """
    try:
        datetime.strptime(date[:10], "%Y-%m-%d")
    except (ValueError, TypeError) as ex:
        raise ValueError("date must be YYYY-MM-DD") from ex
    entry = {
        "date": date[:10],
        "sport": (sport or "").strip().lower() or None,
        "title_contains": (title_contains or "").strip().lower() or None,
    }
    skips = _load_skipped_sessions()
    skips.append(entry)
    cache.put("fueling_skips", {"key": "current"}, skips, key_parts=["current"])
    return {"saved": True, "skips": skips}


def get_skipped_sessions() -> list[dict]:
    """The currently active (not-yet-past) skipped-session entries."""
    return _load_skipped_sessions()


def _load_skipped_sessions() -> list[dict]:
    """Read the stored skip list, pruning entries whose date has passed (and
    persisting the pruned list so it doesn't grow unbounded)."""
    skips = cache.get(
        "fueling_skips", {"key": "current"}, key_parts=["current"],
        ttl_seconds=IMMUTABLE_TTL,
    ) or []
    if not isinstance(skips, list):
        return []
    today_iso = _local_today().isoformat()
    pruned = [s for s in skips if isinstance(s, dict) and (s.get("date") or "") >= today_iso]
    if len(pruned) != len(skips):
        cache.put("fueling_skips", {"key": "current"}, pruned, key_parts=["current"])
    return pruned


def _is_session_skipped(skips: list[dict], d_iso: str, sport: str, title: str) -> bool:
    """True if a scheduled item on d_iso matches any stored skip entry — by
    sport and/or a title substring, or unconditionally if neither is set."""
    tl = (title or "").lower()
    for s in skips:
        if s.get("date") != d_iso:
            continue
        sp, tc = s.get("sport"), s.get("title_contains")
        if sp and sp != sport:
            continue
        if tc and tc not in tl:
            continue
        return True
    return False


# --- Retroactively ignoring a day's food log ---------------------------------
# A day you know you logged badly — forgot dinner, ate out and guessed, gave up
# halfway — is worse than a day you didn't log at all. Every intake-derived
# calculation reads a half-logged day as a genuine low-intake day: the rebalance
# spreads a phantom deficit across the coming week, adaptive TDEE pulls your
# measured maintenance down, and the coaching notes scold you for under-eating.
# Marking the day ignored drops it from all of them — treated exactly like an
# unlogged day — while leaving Garmin's raw data untouched and the decision
# reversible. Expenditure/burn for that day is unaffected; only intake is.
_IGNORED_DAYS_RETENTION_DAYS = 400   # outlives the 366-day max query window


def ignore_food_day(date: str, reason: str | None = None) -> dict:
    """Retroactively exclude one day's food log from every intake-derived
    calculation: rebalance drift, adaptive-TDEE maintenance, trend averages and
    the logging/coaching suggestions. Use it for a day you know wasn't logged
    accurately or completely, so a partial log doesn't masquerade as a real
    low-intake day and skew the plan.

    date: YYYY-MM-DD (today or earlier).
    reason: optional free-text note, for your own reference later.

    The day's burn/expenditure still counts — only intake is dropped. Reversible
    with unignore_food_day. Persisted to R2."""
    try:
        datetime.strptime(str(date)[:10], "%Y-%m-%d")
    except (ValueError, TypeError) as ex:
        raise ValueError("date must be YYYY-MM-DD") from ex
    d_iso = str(date)[:10]
    if d_iso > _local_today().isoformat():
        return {"saved": False, "error": "cannot ignore a future day"}
    if not cache.enabled():
        return {"saved": False, "error": "cache/storage unavailable — cannot persist"}
    ignored = _load_ignored_food_days()
    ignored[d_iso] = {
        "date": d_iso,
        "reason": (reason or "").strip() or None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    cache.put("fueling_ignored_days", {"key": "current"}, ignored, key_parts=["current"])
    return {"saved": True, "date": d_iso, "ignored_days": get_ignored_food_days()}


def unignore_food_day(date: str) -> dict:
    """Undo ignore_food_day — the day's logged food counts again everywhere."""
    try:
        datetime.strptime(str(date)[:10], "%Y-%m-%d")
    except (ValueError, TypeError) as ex:
        raise ValueError("date must be YYYY-MM-DD") from ex
    d_iso = str(date)[:10]
    if not cache.enabled():
        return {"saved": False, "error": "cache/storage unavailable — cannot persist"}
    ignored = _load_ignored_food_days()
    if d_iso not in ignored:
        return {"saved": False, "error": f"{d_iso} is not currently ignored",
                "ignored_days": get_ignored_food_days()}
    ignored.pop(d_iso)
    cache.put("fueling_ignored_days", {"key": "current"}, ignored, key_parts=["current"])
    return {"saved": True, "date": d_iso, "ignored_days": get_ignored_food_days()}


def get_ignored_food_days() -> list[dict]:
    """Days whose food log is currently being ignored, newest first."""
    return sorted(_load_ignored_food_days().values(),
                  key=lambda e: e.get("date") or "", reverse=True)


def _load_ignored_food_days() -> dict:
    """Stored {date_iso: entry} map of ignored days. Unlike skipped sessions
    (which prune once their date is past), these are retroactive by design and
    must survive — pruning is by age only, so the list can't grow unbounded."""
    raw = cache.get(
        "fueling_ignored_days", {"key": "current"}, key_parts=["current"],
        ttl_seconds=IMMUTABLE_TTL,
    ) or {}
    if not isinstance(raw, dict):
        return {}
    cutoff = (_local_today() - timedelta(days=_IGNORED_DAYS_RETENTION_DAYS)).isoformat()
    pruned = {k: v for k, v in raw.items()
              if isinstance(k, str) and isinstance(v, dict) and k >= cutoff}
    if len(pruned) != len(raw):
        cache.put("fueling_ignored_days", {"key": "current"}, pruned, key_parts=["current"])
    return pruned


# --- The race calendar -------------------------------------------------------
# A race is the one training event whose fueling can't be derived from the
# Garmin calendar. The calendar knows a 4-hour ride is scheduled; it doesn't
# know it's an A-priority gran fondo that should have suspended the cut three
# days earlier and loaded 10 g/kg of carbs the day before.
#
# Storing date + sport + distance is enough to derive all of it: the distance
# gives an expected duration (from Garmin's own race predictions where they
# exist, else the athlete's history), the duration decides how many loading
# days the event earns, and the priority decides how long the taper runs.
# app/races.py owns that model; this section owns persistence and the bridge
# into generate_fueling_plan.
_RACE_RETENTION_DAYS = 180   # keep finished races around for context, then drop


def _race_id(d_iso: str, name: str | None, sport: str) -> str:
    """Stable slug for a race: date + name (or sport). Re-saving the same race
    therefore *updates* it rather than adding a duplicate, which is what makes
    set_race usable as an edit."""
    tail = re.sub(r"[^a-z0-9]+", "-", (name or sport or "race").lower()).strip("-")
    return f"{d_iso}-{tail or 'race'}"[:80]


def _race_speed_samples(days_back: int = 120) -> dict[str, float]:
    """Median moving speed (m/s) per sport from recent activities, used to
    estimate a race duration when Garmin has no prediction for it (everything
    that isn't a straight run). Best-effort: returns {} if history is
    unavailable, and the caller falls back to a default pace table."""
    try:
        acts = get_activities_in_range(
            (_local_today() - timedelta(days=days_back)).isoformat(),
            _local_today().isoformat(),
        ) or []
    except Exception:  # noqa: BLE001
        return {}
    by_sport: dict[str, list[float]] = {}
    for a in acts:
        if not isinstance(a, dict):
            continue
        dist_m = a.get("distance")
        dur_s = a.get("movingDuration") or a.get("duration") or a.get("elapsedDuration")
        if not dist_m or not dur_s or dur_s < 900:   # skip <15 min efforts
            continue
        sport = _sport_bucket(a.get("activityName") or "",
                              (a.get("activityType") or {}).get("typeKey") or "")
        if sport not in ("running", "cycling", "swimming"):
            continue
        mps = float(dist_m) / float(dur_s)
        # Sanity bounds per sport — drops GPS glitches and mis-typed activities
        # that would otherwise drag the median somewhere absurd.
        lo, hi = {"running": (1.5, 7.0), "cycling": (3.0, 20.0),
                  "swimming": (0.4, 2.5)}[sport]
        if lo <= mps <= hi:
            by_sport.setdefault(sport, []).append(mps)
    # Racing is faster than the median training session, but not by the margin
    # a best-effort would suggest — a modest uplift keeps the estimate honest.
    return {s: round(_median(v) * 1.08, 3) for s, v in by_sport.items() if len(v) >= 3}


def _estimate_race_duration(sport: str, distance_km: float,
                            legs_km: tuple | None) -> tuple[float, str]:
    """Expected finish time for a race, with the source of the estimate.
    Degrades gracefully: Garmin race predictions, then the athlete's own
    median race-adjusted pace, then a generic table."""
    preds = None
    if sport in ("running", "triathlon"):
        try:
            rp = get_race_predictions()
            if isinstance(rp, dict):
                preds = rp
        except Exception:  # noqa: BLE001
            preds = None
    return races_lib.estimate_duration_hours(
        sport, distance_km, legs_km=tuple(legs_km) if legs_km else None,
        race_predictions=preds, speed_mps=_race_speed_samples(),
    )


def set_race(
    date: str,
    name: str | None = None,
    sport: str | None = None,
    distance: float | None = None,
    distance_unit: str = "km",
    distance_label: str | None = None,
    priority: str = "A",
    target_time_hours: float | None = None,
    hot: bool = False,
    notes: str | None = None,
) -> dict:
    """Add a race to the calendar (or update one already stored for that date
    and name). generate_fueling_plan then builds the carb load, taper, race-day
    fuelling and post-race recovery around it automatically — no need to pass
    carb_load by hand.

    date: YYYY-MM-DD.
    name: e.g. 'Chicago Marathon'. Optional, but it's what the plan calls it.
    sport: running | cycling | triathlon | swimming | other.
    distance + distance_unit: e.g. 42.2 'km', 100 'mi', 1500 'm'.
    distance_label: a preset instead of a raw distance — 'marathon', 'half',
      '10k', '50k', '70.3', 'ironman', 'olympic', 'sprint', 'century',
      'gran fondo', ... A preset also fills in the sport and, for triathlon,
      the per-leg distances.
    priority: 'A' (full 7-day taper), 'B' (4-day), 'C' (raced through, load
      days only).
    target_time_hours: your own expected finish time. Overrides the estimate,
    which otherwise comes from Garmin's race predictions or your recent pace —
    and the estimate is what decides how many loading days the race earns, so
    it's worth setting for anything unusual (hilly, hot, a first attempt).
    hot: expect heat — raises race-day fluid and sodium.

    Re-saving a race refreshes its duration estimate against current fitness.
    """
    try:
        datetime.strptime(str(date)[:10], "%Y-%m-%d")
    except (ValueError, TypeError) as ex:
        raise ValueError("date must be YYYY-MM-DD") from ex
    d_iso = str(date)[:10]
    if not cache.enabled():
        return {"saved": False, "error": "cache/storage unavailable — cannot persist"}

    sp, km, label, legs = races_lib.resolve_distance(
        sport=sport, distance=distance, unit=distance_unit,
        distance_label=distance_label,
    )
    prio = (priority or "A").strip().upper()
    if prio not in races_lib.PRIORITIES:
        raise ValueError(f"priority must be one of {races_lib.PRIORITIES}")

    if target_time_hours:
        hours, source = round(float(target_time_hours), 2), "user"
    else:
        hours, source = _estimate_race_duration(sp, km, legs)

    rid = _race_id(d_iso, name, sp)
    entry = {
        "id": rid,
        "date": d_iso,
        "name": (name or "").strip() or f"{label} {sp}",
        "sport": sp,
        "distance_km": km,
        "distance_label": label,
        "legs_km": list(legs) if legs else None,
        "priority": prio,
        "duration_hours": hours,
        "duration_source": source,
        "hot": bool(hot),
        "notes": (notes or "").strip() or None,
        "carb_load_days": races_lib.load_days_for(hours),
        "saved_at": _local_today().isoformat(),
    }
    stored = [r for r in _load_races() if r.get("id") != rid]
    stored.append(entry)
    stored.sort(key=lambda r: r.get("date") or "")
    cache.put("fueling_races", {"key": "current"}, stored, key_parts=["current"])
    persisted = cache.get(
        "fueling_races", {"key": "current"}, key_parts=["current"],
        ttl_seconds=IMMUTABLE_TTL,
    )
    if not persisted:
        return {
            "saved": False,
            "race": entry,
            "error": (
                "Race was NOT persisted — the server could not write it to the "
                "cache (R2). The web service likely has read-only S3/R2 "
                "credentials; give it read-write keys and retry."
            ),
        }
    return {"saved": True, "race": entry, "races": get_races()}


def delete_race(race_id: str) -> dict:
    """Remove a race from the calendar by its id (see get_races)."""
    if not cache.enabled():
        return {"saved": False, "error": "cache/storage unavailable — cannot persist"}
    stored = _load_races()
    kept = [r for r in stored if r.get("id") != race_id]
    if len(kept) == len(stored):
        return {"saved": False, "error": f"no race with id '{race_id}'",
                "races": get_races()}
    cache.put("fueling_races", {"key": "current"}, kept, key_parts=["current"])
    return {"saved": True, "deleted": race_id, "races": get_races()}


def get_races(include_past_days: int = 21) -> list[dict]:
    """Stored races — everything upcoming, plus those finished within the last
    `include_past_days` (their recovery windows still shape the plan). Each
    carries its estimated duration, the number of carb-loading days it earns,
    and how many days out it is."""
    today = _local_today()
    cutoff = (today - timedelta(days=max(0, int(include_past_days)))).isoformat()
    out = []
    for r in _load_races():
        if (r.get("date") or "") < cutoff:
            continue
        r = dict(r)
        try:
            r["days_until"] = (date.fromisoformat(r["date"]) - today).days
        except (ValueError, TypeError, KeyError):
            r["days_until"] = None
        out.append(r)
    return out


def _load_races() -> list[dict]:
    """The stored race list in its raw shape, dropping entries older than the
    retention window (and persisting the pruned list so it can't grow
    unbounded). get_races layers the display fields on top; the write paths and
    the plan integration want this."""
    raw = cache.get(
        "fueling_races", {"key": "current"}, key_parts=["current"],
        ttl_seconds=IMMUTABLE_TTL,
    ) or []
    if not isinstance(raw, list):
        return []
    cutoff = (_local_today() - timedelta(days=_RACE_RETENTION_DAYS)).isoformat()
    pruned = [r for r in raw
              if isinstance(r, dict) and (r.get("date") or "") >= cutoff]
    if len(pruned) != len(raw):
        cache.put("fueling_races", {"key": "current"}, pruned, key_parts=["current"])
    return sorted(pruned, key=lambda r: r.get("date") or "")


def _race_phases_for_window(start: date, days: int) -> dict[str, tuple[dict, dict]]:
    """Map each date in the plan window to the (race, phase) governing it.

    When two races' windows overlap — a B race inside an A race's taper, say —
    the day takes whichever phase protects it more (the smallest deficit
    multiplier), and among equals the nearer race. That way a tune-up race
    can't quietly reinstate a deficit that the goal race had suspended.
    """
    out: dict[str, tuple[dict, dict]] = {}
    stored = _load_races()
    if not stored:
        return out
    for i in range(days):
        d = start + timedelta(days=i)
        best: tuple[dict, dict] | None = None
        for r in stored:
            try:
                r_date = date.fromisoformat(str(r.get("date"))[:10])
            except (ValueError, TypeError):
                continue
            hours = float(r.get("duration_hours") or 0)
            phase = races_lib.phase_for((r_date - d).days, hours,
                                        r.get("priority") or "A")
            if not phase:
                continue
            if best is None or (
                phase["deficit_multiplier"],
                abs(phase["days_until"]),
            ) < (best[1]["deficit_multiplier"], abs(best[1]["days_until"])):
                best = (r, phase)
        if best:
            out[d.isoformat()] = best
    return out


def _safe_races() -> list[dict]:
    """get_races that can't fail the plan — the race calendar is context, not
    a dependency, so a storage hiccup should cost the races section and
    nothing else."""
    try:
        return get_races()
    except Exception:  # noqa: BLE001
        return []


def _race_fuel_card(race: dict, weight_kg: float) -> dict:
    """The race-day fuel card, derived from get_race_fueling and shaped like
    the plan's ordinary per-workout cards so the dashboard and the meal split
    can render it without special-casing."""
    hours = float(race.get("duration_hours") or 1.0)
    rf = get_race_fueling(
        race.get("sport") or "running", hours,
        weight_kg=weight_kg, hot=bool(race.get("hot")),
    )
    during, pre, post = rf["during"], rf["pre_race_meal"], rf["post"]
    return {
        "session": race.get("name") or "Race",
        "intensity": "race",
        "hours": round(hours, 2),
        "pre_carbs_g": pre["carbs_g"],
        "during_carbs_g_per_hr": during["carbs_g_per_hr"],
        "during_carbs_g_total": during["carbs_g_total"],
        "post_protein_g": post["protein_g"],
        "post_carbs_g": post["carbs_g"],
        "fluid_ml_per_hr": during["fluid_ml_per_hr"],
        "sodium_mg_per_hr": during["sodium_mg_per_hr"],
        "caffeine_mg": rf["caffeine"]["pre_mg"],
        "race": True,
        "note": (f"{pre['timing']} pre-race meal; " + (during["note"] or "")).strip("; "),
    }


# --- Resetting app-owned fueling history -------------------------------------
# Starting a fresh cut means re-baselining: the old weekly snapshots, manual
# weigh-ins and per-day overrides describe a block that's over, and leaving them
# in place drags the new plan's trend, projection and adaptive TDEE toward the
# old one.
#
# This resets ONLY state this app wrote. Garmin-derived caches are deliberately
# out of reach: the web instance runs GARMIN_READONLY=true and cannot re-fetch,
# so deleting activities_month / body_composition / daily_summary / calendar_month
# / nutrition food logs would blank the dashboard until the nightly cron and
# permanently lose the burn-calibration history behind athlete_baseline. Today's
# workouts and nutrition therefore survive a reset automatically — they live in
# Garmin-derived keys this tool never touches.
#
# The active goal is also out of scope: it isn't history, and set_fueling_goal
# already overwrites it wholesale.
RESET_SCOPES: dict[str, str] = {
    "weekly_snapshots": "weekly_snapshots",       # one object per week
    "manual_weigh_ins": "manual_weigh_in",        # single map: date -> entry
    "ignored_days": "fueling_ignored_days",       # single map: date -> entry
    "skipped_sessions": "fueling_skips",          # single list
    "weigh_in_snapshot": "weigh_in_snapshot",     # derived; rebuilt on next read
}
# Scopes cleared when the caller doesn't name any. weigh_in_snapshot is excluded
# because it's derived — it gets rebuilt below whenever weigh-ins change anyway.
_DEFAULT_RESET_SCOPES = ("weekly_snapshots", "manual_weigh_ins",
                         "ignored_days", "skipped_sessions")


def reset_fueling_history(
    scopes: list[str] | None = None,
    keep_today: bool = True,
    confirm: bool = False,
) -> dict:
    """Clear this app's own fueling history so a new goal starts from a clean
    baseline. Deletes weekly snapshots, manual weigh-ins, ignored days and
    skipped sessions — NOT your Garmin data (activities, food logs, body
    composition), which the app cannot re-fetch and so must never delete.

    scopes: which to clear; defaults to everything except the derived weigh-in
        snapshot. Valid: weekly_snapshots, manual_weigh_ins, ignored_days,
        skipped_sessions, weigh_in_snapshot.
    keep_today: keep entries dated today (default true), so a weigh-in or
        snapshot you logged this morning survives the reset.
    confirm: must be true to actually delete. The default is a DRY RUN that
        reports exactly what would be removed, and changes nothing.

    Returns per-scope counts. Deleting is irreversible — there is no undo."""
    # Validate before the storage check so a typo'd scope is always reported,
    # rather than being masked by an unconfigured cache.
    requested = list(scopes) if scopes else list(_DEFAULT_RESET_SCOPES)
    unknown = [s for s in requested if s not in RESET_SCOPES]
    if unknown:
        raise ValueError(
            f"unknown scope(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(RESET_SCOPES)}"
        )
    if not cache.enabled():
        return {"reset": False, "error": "cache/storage unavailable — nothing to reset"}
    today_iso = _local_today().isoformat()
    dry_run = not confirm
    detail: dict[str, dict] = {}

    def _reset_dated_map(scope: str, prefix: str, loader, args: dict,
                         key_parts: list[str]) -> None:
        """Scopes stored as one object holding a {date: entry} map. Rewrites the
        object with only the surviving days, or drops it entirely if none."""
        current = loader()
        kept = {k: v for k, v in current.items() if keep_today and k == today_iso}
        removing = len(current) - len(kept)
        detail[scope] = {"removed": removing, "kept": len(kept)}
        if dry_run or not removing:
            return
        if kept:
            cache.put(prefix, args, kept, key_parts=key_parts)
        else:
            cache.delete_prefix(prefix)

    for scope in dict.fromkeys(requested):   # de-dupe, preserve order
        prefix = RESET_SCOPES[scope]
        if scope == "weekly_snapshots":
            keys = cache.list_keys(tool_prefix=prefix, limit=10000)
            doomed = [k for k in keys
                      if not (keep_today and k.endswith(f"/{today_iso}.json"))]
            detail[scope] = {"removed": len(doomed), "kept": len(keys) - len(doomed)}
            if not dry_run and doomed:
                cache.delete_keys(doomed)
        elif scope == "manual_weigh_ins":
            _reset_dated_map(scope, prefix, _manual_weigh_ins, {}, ["log"])
        elif scope == "ignored_days":
            _reset_dated_map(scope, prefix, _load_ignored_food_days,
                             {"key": "current"}, ["current"])
        elif scope == "skipped_sessions":
            current = _load_skipped_sessions()
            detail[scope] = {"removed": len(current), "kept": 0}
            if not dry_run and current:
                cache.delete_prefix(prefix)
        elif scope == "weigh_in_snapshot":
            n = cache.count_keys(tool_prefix=prefix)
            detail[scope] = {"removed": n, "kept": 0}
            if not dry_run and n:
                cache.delete_prefix(prefix)

    # Manual weigh-ins feed the shared snapshot every reader uses, so a stale
    # snapshot would keep serving weights we just deleted. Drop it and rebuild
    # from Garmin plus whatever manual entries survived.
    rebuilt = None
    if not dry_run and detail.get("manual_weigh_ins", {}).get("removed"):
        try:
            cache.delete_prefix("weigh_in_snapshot")
            merged = _merge_manual_weigh_ins(_garmin_weigh_in_entries())
            store_weigh_in_snapshot(merged)
            rebuilt = len(merged)
        except Exception:  # noqa: BLE001
            rebuilt = None

    total = sum(d["removed"] for d in detail.values())
    out: dict[str, Any] = {
        "reset": not dry_run,
        "dry_run": dry_run,
        "kept_today": keep_today,
        "total_removed": total,
        "scopes": detail,
        "garmin_data_untouched": True,
    }
    if rebuilt is not None:
        out["weigh_in_snapshot_rebuilt"] = rebuilt
    if dry_run:
        out["message"] = (
            f"Dry run — nothing deleted. {total} item(s) would be removed. "
            "Re-run with confirm=true to apply."
        )
    return out


def _weeks_remaining(target_date: str | None) -> int | None:
    if not target_date:
        return None
    try:
        td = datetime.strptime(target_date[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    days = (td - _local_today()).days
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
    # Read the current weight from the SAME shared weigh-in snapshot the
    # dashboard chart + fueling plan use, so the "now" card can't disagree
    # with the chart's latest actual point. The athlete baseline is a
    # nightly-recomputed blob (36h TTL) whose weight comes from the stale LT
    # power.weight; using it here made "now" lag a same-day weigh-in until
    # the next nightly recompute. Fall back to the baseline only if no
    # weigh-in has been recorded at all.
    try:
        bs = _latest_body_stats()
        if isinstance(bs, dict) and bs.get("weight_kg") is not None:
            current_weight = bs.get("weight_kg")
            weight_staleness = bs.get("staleness_days")
    except Exception:  # noqa: BLE001
        pass
    if current_weight is None:
        try:
            base = get_athlete_baseline()
            if isinstance(base, dict):
                current_weight = base.get("weight_kg")
                weight_staleness = (base.get("staleness_days") or {}).get("weight")
        except Exception:  # noqa: BLE001
            pass
    # A manual current-weight override on the goal wins over Garmin's reading.
    manual_weight = goal.get("current_weight_kg")
    if manual_weight:
        current_weight = manual_weight
        weight_staleness = None

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
                _local_today() - datetime.strptime(sd, "%Y-%m-%d").date()
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


def _latest_body_stats() -> dict:
    """Latest weight + body-fat + lean/muscle mass from the stable weigh-in
    snapshot (Renpho syncs into Garmin body composition). Only weight is
    consumed elsewhere; this surfaces the composition fields for recomp."""
    today = _local_today()
    out: dict[str, Any] = {
        "weight_kg": None, "body_fat_pct": None, "lean_mass_kg": None,
        "muscle_mass_kg": None, "fat_mass_kg": None, "as_of": None,
        "staleness_days": None,
    }
    entries = _weigh_in_entries()          # oldest first, already parsed
    if not entries:
        return out
    latest = entries[-1]                    # newest
    w = latest.get("weight_kg")
    if w:
        out["weight_kg"] = round(w, 1)
    bf = latest.get("body_fat_pct")
    if bf and out["weight_kg"]:
        out["body_fat_pct"] = round(float(bf), 1)
        out["fat_mass_kg"] = round(out["weight_kg"] * bf / 100.0, 1)
        out["lean_mass_kg"] = round(out["weight_kg"] * (1 - bf / 100.0), 1)
    mm = latest.get("muscle_mass_kg")
    if mm:
        out["muscle_mass_kg"] = round(mm, 1)
    d = _coerce_garmin_date(latest.get("date"))
    if d:
        out["as_of"] = d.isoformat()
        out["staleness_days"] = (today - d).days
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


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _to_active_calories(items: list[dict], resting_from_activity: float | None,
                         rmr_per_hr: float | None = None) -> int | None:
    """Convert a list of {"kcal", "hours"} workout dicts from gross to Active
    Calories, in place, and return the new summed total (or None if `items`
    is empty).

    Garmin's per-workout "calories" is gross — it includes a resting-calorie
    equivalent for that workout's own duration (what Garmin's own activity
    page splits out as "Resting Calories" vs "Active Calories": e.g. a
    Strength session showing Total 285 / Resting 47 / Active 238). The
    day-level stats_and_body exposes that resting-equivalent, aggregated
    across all of the day's workout time, as restingCaloriesFromActivity.
    Subtract it, prorated by duration across `items`, so both the individual
    figures and their sum are Active Calories, matching what Garmin itself
    shows for a workout.

    restingCaloriesFromActivity is frequently still null for hours after a
    workout syncs (Garmin's backend hasn't finished processing the day yet),
    which used to make this silently fall back to gross — i.e. today's
    freshly-logged session would show its full gross total as "Active" until
    Garmin caught up. When that happens, net each item against rmr_per_hr x
    hours instead — the same resting-during-exercise proxy already used for
    scheduled-session estimates and the 30-day typical-day average. Only pure
    gross (no correction at all) when neither figure is available.
    """
    if not items:
        return None
    gross_total = sum(w["kcal"] for w in items)
    if resting_from_activity:
        resting_from_activity = min(resting_from_activity, gross_total)  # defensive clamp
        total_hours = sum(w["hours"] for w in items) or 1.0
        for w in items:
            share = resting_from_activity * (w["hours"] / total_hours)
            w["kcal"] = max(0, round(w["kcal"] - share))
        return sum(w["kcal"] for w in items)
    if rmr_per_hr:
        for w in items:
            w["kcal"] = max(0, round(w["kcal"] - rmr_per_hr * w["hours"]))
        return sum(w["kcal"] for w in items)
    return gross_total


def _parse_activity_start(s: str) -> datetime | None:
    """Best-effort parse of Garmin's startTimeLocal/GMT string into a naive
    datetime. Handles 'YYYY-MM-DD HH:MM:SS' and ISO 'T'/'Z' variants; returns
    None on anything unparseable."""
    if not s:
        return None
    t = s.strip().replace("T", " ").replace("Z", "")
    t = t.split(".")[0].split("+")[0].strip()  # drop fractional secs / tz offset
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def _dedupe_actual_workouts(items: list[dict]) -> list[dict]:
    """Collapse duplicate recordings of the SAME workout. Two entries are the
    same session when they share a sport and start within ~10 min of each other
    (a smart-trainer app + the watch both logging one ride, or a Strava
    re-import). The higher-calorie record wins (the fuller recording); when
    start times are missing we fall back to same-sport + near-equal duration so
    a genuine second session of the same sport (hours clearly different) is
    still kept. Order preserved. Entries carrying the same activity_id are
    always treated as the same activity regardless of timing."""
    START_TOL_MIN = 10.0        # starts within 10 min → same session
    DUR_TOL_HRS = 0.20          # ~12 min: near-equal duration when no start time
    kept: list[dict] = []
    for it in items:
        dup_of = None
        for k in kept:
            if k["sport"] != it["sport"]:
                continue
            aid, kid = it.get("activity_id"), k.get("activity_id")
            if aid is not None and kid is not None and aid == kid:
                dup_of = k
                break
            ts_i, ts_k = _parse_activity_start(it.get("start") or ""), _parse_activity_start(k.get("start") or "")
            if ts_i and ts_k:
                if abs((ts_i - ts_k).total_seconds()) <= START_TOL_MIN * 60:
                    dup_of = k
                    break
            else:
                # No usable start times: treat as the same only when the
                # durations also match closely, so distinct same-sport sessions
                # (e.g. a short bike + a long bike) aren't wrongly merged.
                if abs((it.get("hours") or 0) - (k.get("hours") or 0)) <= DUR_TOL_HRS:
                    dup_of = k
                    break
        if dup_of is None:
            kept.append(it)
        elif (it.get("kcal") or 0) > (dup_of.get("kcal") or 0):
            # Keep the fuller recording: overwrite in place, preserving order.
            dup_of.update(it)
    return kept


def _history_samples(history: list[dict]) -> dict[str, list[tuple[float, float]]]:
    """Per sport bucket, a list of (hours, kcal_per_hour) from recent
    completed activities. Retaining duration lets us match a planned session
    to historically *similar* sessions, not just the sport average."""
    samples: dict[str, list[tuple[float, float]]] = {}
    for a in history or []:
        if not isinstance(a, dict):
            continue
        dur_s = a.get("duration") or a.get("elapsedDuration") or a.get("movingDuration")
        cal = a.get("calories")
        if not dur_s or not cal or dur_s < 600:  # skip <10min
            continue
        type_key = (a.get("activityType") or {}).get("typeKey") or ""
        bucket = _sport_bucket(a.get("activityName") or "", type_key)
        hours = dur_s / 3600.0
        kcal_hr = cal / hours
        if 150 <= kcal_hr <= 1600:  # sanity bounds
            samples.setdefault(bucket, []).append((hours, kcal_hr))
    return samples


def _mean_daily_exercise(history: list[dict], rmr_per_hr: float,
                         as_of: date, window_days: int = 30) -> dict[str, tuple[float, float]]:
    """Mean *daily* net-exercise burn (Active Calories) and mean daily training
    hours over the trailing `window_days`, split into weekday vs weekend — each
    spread across every day of that type in the window (training days and off
    days alike). Weekends carry the long stuff, so a phantom Saturday shouldn't
    assume a typical *weekday* burn; splitting keeps each assumed day true to
    its own day-of-week pattern. Every activity's gross calories are netted
    against the resting-during-exercise proxy (rmr_per_hr x hours), the same
    conversion a scheduled-session estimate gets. Used to fill runs of
    unscheduled days that will almost certainly pick up a workout later, so
    they aren't banked as deep-deficit rest.

    Returns {"weekday": (mean_kcal, mean_hours), "weekend": (mean_kcal,
    mean_hours)}; zeros for a bucket with no days/history. Sat/Sun are the
    weekend."""
    empty = {"weekday": (0.0, 0.0), "weekend": (0.0, 0.0)}
    if window_days <= 0:
        return empty
    lo = as_of - timedelta(days=window_days)
    # Count weekday vs weekend *days* in the window (lo, as_of] — the correct
    # denominator so each mean is per-day-of-that-type, not diluted by the
    # other type's day count.
    wk_days = we_days = 0
    for i in range(window_days):
        d = as_of - timedelta(days=i)          # covers lo+1 .. as_of
        if d.weekday() >= 5:
            we_days += 1
        else:
            wk_days += 1
    wk_net = wk_hrs = we_net = we_hrs = 0.0
    for a in history or []:
        if not isinstance(a, dict):
            continue
        # startTimeLocal is usually an ISO string but Garmin occasionally
        # returns an epoch int — coerce before slicing so a stray int doesn't
        # blow up the whole plan ("'int' object is not subscriptable").
        raw = a.get("startTimeLocal") or a.get("startTimeGMT") or ""
        d_str = str(raw)[:10]
        try:
            a_date = date.fromisoformat(d_str)
        except (ValueError, TypeError):
            continue
        if not (lo < a_date <= as_of):
            continue
        dur_s = a.get("duration") or a.get("elapsedDuration") or a.get("movingDuration")
        cal = a.get("calories")
        if not dur_s or not cal or dur_s < 600:  # skip <10min / no-calorie
            continue
        hours = dur_s / 3600.0
        net = max(0.0, cal - rmr_per_hr * hours)
        if a_date.weekday() >= 5:
            we_net += net; we_hrs += hours
        else:
            wk_net += net; wk_hrs += hours
    return {
        "weekday": (wk_net / wk_days, wk_hrs / wk_days) if wk_days else (0.0, 0.0),
        "weekend": (we_net / we_days, we_hrs / we_days) if we_days else (0.0, 0.0),
    }


def _kcal_per_hour_for(sport: str, hours: float,
                       samples: dict[str, list[tuple[float, float]]]) -> tuple[float, str]:
    """Best kcal/hr estimate for a planned session, calibrated from the
    athlete's own history. Prefers activities of the same sport AND similar
    duration (within ±40%); falls back to the sport median, then a generic
    table. Returns (kcal_per_hour, source)."""
    lst = samples.get(sport) or []
    if hours and lst:
        lo, hi = hours * 0.6, hours * 1.4
        near = [k for (h, k) in lst if lo <= h <= hi]
        if len(near) >= 3:
            return round(_median(near)), "history_similar"
    if len(lst) >= 3:
        return round(_median([k for _, k in lst])), "history_sport"
    return _BASE_KCAL_PER_HOUR.get(sport, _BASE_KCAL_PER_HOUR["default"]), "generic_table"


def _duration_from_title(title: str) -> float | None:
    """Parse a session duration out of a workout title, e.g. '50min Aerobic
    Run', "Master's Swim - 90min", '1.5h ride'. Returns hours or None.

    Skips interval notation like '3x15min' or '4min Repeats' — those describe
    a set, not the session length — by ignoring a minutes token preceded by
    'x'/a digit and requiring 2-3 digits."""
    t = (title or "").lower()
    mh = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hour)\b", t)
    if mh:
        return round(float(mh.group(1)), 2)
    mm = re.search(r"(?<![x\d])(\d{2,3})\s*min", t)
    if mm:
        return round(int(mm.group(1)) / 60.0, 2)
    return None


def _workout_duration_secs(wo: dict) -> float | None:
    """Total planned duration (seconds) of a structured Garmin workout,
    summed from its time-based steps. Repeat groups multiply their nested
    steps by the iteration count. Distance- or lap-button-ended steps can't
    be converted to time and contribute nothing, so the result is a floor —
    used only when Garmin doesn't expose a top-level duration estimate."""
    if not isinstance(wo, dict):
        return None

    def walk(steps) -> float:
        tot = 0.0
        for st in steps or []:
            if not isinstance(st, dict):
                continue
            nested = st.get("workoutSteps") or st.get("steps")
            if nested:  # RepeatGroupDTO — multiply the loop body by its reps
                reps = st.get("numberOfIterations") or st.get("iterations") or 1
                tot += walk(nested) * (reps if reps and reps > 0 else 1)
                continue
            ec = st.get("endCondition")
            ec_key = ec.get("conditionTypeKey") if isinstance(ec, dict) else ec
            val = st.get("endConditionValue") or st.get("endConditionValueInSecs")
            if val and str(ec_key or "").lower().startswith("time"):
                tot += float(val)
        return tot

    total = 0.0
    segs = wo.get("workoutSegments") or []
    if segs:
        for seg in segs:
            total += walk(seg.get("workoutSteps") or [])
    else:
        total += walk(wo.get("workoutSteps") or [])
    return total if total > 0 else None


def _planned_hours(item: dict, intensity: str) -> tuple[float, str]:
    """(hours, source). Prefer an explicit calendar/workout duration, then the
    structured workout's own step durations, then a duration embedded in the
    title, then a per-intensity default."""
    for k in ("duration", "estimatedDurationInSecs", "estimatedDurationSecs"):
        v = item.get(k)
        if v:
            return round(v / 3600.0, 2), "calendar"
    wid = item.get("workoutId")
    if wid:
        try:
            wo = get_workout_by_id(wid) or {}
            secs = (wo.get("estimatedDurationInSecs") or wo.get("estimatedDurationSecs")
                    or _workout_duration_secs(wo))
            if secs:
                return round(secs / 3600.0, 2), "workout_detail"
            # Distance-only workout: convert distance to time at a default pace.
            dist = wo.get("estimatedDistanceInMeters")
            if dist:
                sport = (wo.get("sportType") or {}).get("sportTypeKey") or ""
                mps = _DEFAULT_PACE_MPS.get(sport)
                if mps:
                    return round(dist / mps / 3600.0, 2), "workout_distance"
        except Exception:  # noqa: BLE001
            pass
    title_hours = _duration_from_title(item.get("title") or item.get("workoutName") or "")
    if title_hours:
        return title_hours, "title"
    return _DEFAULT_HOURS.get(intensity, 1.0), "type_default"


def _bmr(weight_kg, sex, height_cm, age, ffm_kg=None):
    """Resting metabolic rate, returned as (kcal, source).

    Katch-McArdle (370 + 21.6 x fat-free mass) when a *measured* fat-free mass
    is known — most accurate for a lean athlete with a real body-fat reading,
    since it keys off lean mass rather than total weight. Falls back to
    Mifflin-St Jeor from sex/height/age, then weight x 22."""
    if ffm_kg and ffm_kg > 0:
        return round(370 + 21.6 * ffm_kg), "katch_mcardle"
    if weight_kg and height_cm and age and sex in ("male", "female"):
        base = weight_kg * 10 + height_cm * 6.25 - age * 5
        return round(base + (5 if sex == "male" else -161)), "mifflin_st_jeor"
    if weight_kg:
        return round(weight_kg * 22), "weight_x22_fallback"
    return None, "unavailable"


def _today_actuals() -> dict | None:
    """Logged intake + the actual foods eaten today, with measured
    expenditure. Always today — never falls back to an older logged day, so
    an unlogged today reads as zero/empty rather than silently showing
    yesterday's numbers under a 'today' label. Powers the dashboard's
    'today so far' card."""
    today = _local_today()
    today_iso = today.isoformat()
    try:
        ds = get_daily_summaries(today_iso, today_iso,
                                  ["nutrition_food_log", "stats_and_body"])
    except Exception:  # noqa: BLE001
        return None
    fl_all = ds.get("nutrition_food_log") or {}
    sb_all = ds.get("stats_and_body") or {}
    chosen = today_iso
    fl = fl_all.get(chosen)
    sb = sb_all.get(chosen)
    content, goals = {}, {}
    if isinstance(fl, dict) and "error" not in fl:
        content = fl.get("dailyNutritionContent") or {}
        goals = fl.get("dailyNutritionGoals") or {}
    logged = _logged_food_entries(fl)
    foods = logged[:60]
    expenditure = None
    bmr_kcal = active_kcal = None
    if isinstance(sb, dict) and "error" not in sb:
        bmr_kcal = sb.get("bmrKilocalories")
        active_kcal = sb.get("activeKilocalories")
        # Prefer bmr + active (our own sum) over Garmin's own totalKilocalories:
        # for a day still in progress, Garmin's total bakes in the FULL day's
        # BMR estimate from hour zero while only activeKilocalories actually
        # accumulates in real time — so mid-day, totalKilocalories overstates
        # what's actually been burned so far. Fall back to Garmin's total only
        # if we don't have both components to build our own.
        expenditure = ((bmr_kcal or 0) + (active_kcal or 0)) or sb.get("totalKilocalories")
    # Split the day's measured burn into workout vs everyday (BMR + non-exercise
    # activity), and surface each completed workout so unplanned ones are visible.
    workouts: list[dict] = []
    try:
        for a in (get_activities_in_range(chosen, chosen) or []):
            if not isinstance(a, dict):
                continue
            if (str(a.get("startTimeLocal") or a.get("startTimeGMT") or "")[:10]) != chosen:
                continue
            dur_s = a.get("duration") or a.get("elapsedDuration") or 0
            cal = a.get("calories")
            if not cal or dur_s < 300:
                continue
            workouts.append({
                "name": a.get("activityName")
                        or ((a.get("activityType") or {}).get("typeKey") or "workout"),
                "kcal": round(cal), "hours": round(dur_s / 3600.0, 2),
            })
    except Exception:  # noqa: BLE001
        pass
    resting_from_activity = None
    if isinstance(sb, dict) and "error" not in sb:
        resting_from_activity = sb.get("restingCaloriesFromActivity")
    rmr_per_hr = (bmr_kcal / 24.0) if bmr_kcal else None
    workout_kcal = _to_active_calories(workouts, resting_from_activity, rmr_per_hr)
    # Non-workout ("everyday") burn = activeKilocalories minus (now
    # active-only) workouts — movement above BMR that isn't a formal session
    # (steps/NEAT only). BMR itself is excluded here (broken out separately
    # as bmr_kcal) so bmr_kcal + non_workout_kcal + workout_kcal partition
    # the day's burn without double-counting.
    non_workout_kcal = None
    if active_kcal is not None:
        non_workout_kcal = max(0, round(active_kcal) - (workout_kcal or 0))
    return {
        "date": chosen,
        "is_today": chosen == today_iso,
        "consumed_kcal": content.get("calories"),
        "protein_g": content.get("protein"),
        "carbs_g": content.get("carbs"),
        "fat_g": content.get("fat"),
        "foods_logged": len(logged),
        "foods": foods,
        "expenditure_kcal": round(expenditure) if expenditure else None,
        "bmr_kcal": round(bmr_kcal) if bmr_kcal else None,
        # Everyday burn = BMR + non-exercise activity (steps/NEAT), i.e. total
        # measured expenditure minus what workouts accounted for.
        "non_workout_kcal": non_workout_kcal,
        "workout_kcal": workout_kcal,
        "workouts": workouts,
        "garmin_goal_kcal": goals.get("adjustedCalories") or goals.get("calories"),
    }


def _recent_days(n: int = 2) -> list[dict]:
    """The last n days' actual consumed calories vs the planned target (from
    the saved weekly snapshot) and measured expenditure — for a quick
    'am I on plan' readout."""
    today = _local_today()
    try:
        ds = get_daily_summaries((today - timedelta(days=n)).isoformat(),
                                 (today - timedelta(days=1)).isoformat(),
                                 ["nutrition_food_log", "stats_and_body"])
    except Exception:  # noqa: BLE001
        return []
    fl = ds.get("nutrition_food_log") or {}
    sb = ds.get("stats_and_body") or {}
    plan: dict = {}
    try:
        for snap in reversed(get_weekly_snapshots(weeks_back=2)):
            plan.update(snap.get("nutrition_plan") or {})
    except Exception:  # noqa: BLE001
        pass
    ignored_days = _load_ignored_food_days()
    out = []
    for i in range(n, 0, -1):
        d = (today - timedelta(days=i)).isoformat()
        v = fl.get(d)
        s = sb.get(d)
        consumed, foods = None, 0
        if isinstance(v, dict) and "error" not in v:
            consumed = (v.get("dailyNutritionContent") or {}).get("calories")
            foods = len(_logged_food_entries(v))
        # Flagged as badly logged: blank the intake so the card can't imply a
        # real number, and mark it so the UI shows "ignored" rather than a
        # scary red "0 foods logged".
        ignored_entry = ignored_days.get(d)
        if ignored_entry:
            consumed, foods = None, 0
        exp = None
        if isinstance(s, dict) and "error" not in s:
            exp = s.get("totalKilocalories") or (
                (s.get("bmrKilocalories") or 0) + (s.get("activeKilocalories") or 0)) or None
        out.append({
            "date": d,
            "consumed_kcal": round(consumed) if consumed else None,
            "foods_logged": foods,
            "plan_target_kcal": (plan.get(d) or {}).get("target_kcal"),
            "expenditure_kcal": round(exp) if exp else None,
            # Actual measured deficit that day (burned minus eaten) — distinct
            # from "vs plan" (eaten vs the planned target), and available even
            # on days with no saved plan.
            "deficit_kcal": (round(exp - consumed) if (exp is not None and consumed is not None) else None),
            "ignored": bool(ignored_entry),
            "ignored_reason": (ignored_entry or {}).get("reason"),
        })
    return out


def _logging_suggestions(protein_target_g: float | None = None,
                         lookback_days: int = 7) -> list[str]:
    """Mine the last week of logged days for actionable coaching notes.

    Draws on nutrition_plan_vs_actual (which already computes per-day actuals
    vs the expenditure-adjusted target and macro deltas) and surfaces the
    handful of patterns worth acting on: chronic under/over-eating vs the
    adjusted target, protein routinely short of the goal, and logging gaps
    that make the plan's self-correction unreliable. Returns [] when there
    isn't enough logged history to say anything honest."""
    try:
        pva = nutrition_plan_vs_actual(days_back=min(max(lookback_days, 2), 14))
    except Exception:  # noqa: BLE001
        return []
    today_iso = _local_today().isoformat()
    # Drop deliberately-ignored days entirely rather than letting them count as
    # unlogged — otherwise flagging a badly-logged day would trade a bogus
    # calorie claim for a bogus "you're not logging" nag.
    rows = [r for r in (pva.get("rows") or [])
            if (r.get("date") or "") < today_iso and not r.get("ignored")]
    if not rows:
        return []

    logged = [r for r in rows if r.get("foods_logged")]
    n_rows, n_logged = len(rows), len(logged)
    out: list[str] = []

    # 1) Logging consistency — the plan's rebalance and trend only work on
    #    logged days. Flag gaps before making calorie claims.
    if n_rows >= 3 and n_logged < n_rows:
        missed = n_rows - n_logged
        if n_logged == 0:
            return [f"No food logged in the last {n_rows} days — log meals so the "
                    "plan can self-correct from your actual intake (targets below "
                    "are model-only until then)."]
        if missed >= 2 or missed / n_rows >= 0.4:
            out.append(f"Logging gap: {missed} of the last {n_rows} days have no "
                       "food logged. The plan rebalances from logged days only, so "
                       "gaps let drift accumulate silently — aim to log every day.")

    # 2) Calorie drift vs the expenditure-adjusted target (the honest
    #    'what you should have eaten given what you actually did' number).
    drifts = [r["delta_kcal_vs_adjusted"] for r in logged
              if r.get("delta_kcal_vs_adjusted") is not None]
    if len(drifts) >= 2:
        avg = sum(drifts) / len(drifts)
        over_days = sum(1 for d in drifts if d > 150)
        under_days = sum(1 for d in drifts if d < -150)
        if avg >= 200 and over_days >= max(2, len(drifts) // 2):
            out.append(f"You've averaged {round(avg):+} kcal/day over your "
                       f"adjusted target across {len(drifts)} logged days — the deficit "
                       "is smaller than planned, which slows the trajectory. Tighten "
                       "portions on the highest days rather than every meal.")
        elif avg <= -200 and under_days >= max(2, len(drifts) // 2):
            out.append(f"You've averaged {round(avg)} kcal/day UNDER your adjusted "
                       f"target across {len(drifts)} logged days. Under-eating on a "
                       "heavy training block risks energy availability and muscle loss "
                       "— eat closer to target, especially post-workout.")

    # 3) Protein adherence — the macro that protects lean mass on a cut.
    if protein_target_g:
        p_actuals = [r["actual_p"] for r in logged if r.get("actual_p") is not None]
        if len(p_actuals) >= 2:
            avg_p = sum(p_actuals) / len(p_actuals)
            short_days = sum(1 for p in p_actuals if p < protein_target_g * 0.85)
            if avg_p < protein_target_g * 0.85 and short_days >= max(2, len(p_actuals) // 2):
                out.append(f"Protein has averaged ~{round(avg_p)} g/day vs your "
                           f"~{round(protein_target_g)} g target ({short_days} of "
                           f"{len(p_actuals)} logged days short). On a cut this is the "
                           "macro to hit — add a lean protein source to the meal you "
                           "most often miss it in.")

    return out


def _is_outdoor_session(sport: str, title: str) -> bool:
    """Best-effort: is this planned session likely outdoors (so heat matters)?
    Pools and indoor-trainer/treadmill cues are indoor; other runs/rides are
    assumed outdoor."""
    t = (title or "").lower()
    if sport == "swimming":
        return False
    if any(k in t for k in ("trainer", "zwift", "rouvy", "indoor", "treadmill",
                            "peloton", "wahoo", "kickr", "erg")):
        return False
    return sport in ("running", "cycling", "walking")


_BREAKFAST_CAP_KCAL = 650   # a breakfast shouldn't balloon on a big training day


def _cap_meal(meals: list[dict], name: str, cap_kcal: int, spill_to: str) -> None:
    """Cap one meal's calories, moving the trimmed macros to another meal so
    the day's totals are preserved. Keeps big-day energy out of breakfast."""
    src = next((m for m in meals if m["meal"] == name), None)
    dst = next((m for m in meals if m["meal"] == spill_to), None)
    if not src or not dst:
        return
    kc = src["protein_g"] * 4 + src["carbs_g"] * 4 + src["fat_g"] * 9
    if kc <= cap_kcal:
        return
    scale = cap_kcal / kc
    for k in ("protein_g", "carbs_g", "fat_g"):
        keep = round(src[k] * scale)
        dst[k] += src[k] - keep
        src[k] = keep


def _fill_macros(target_kcal, protein_g, carb_target_g, weight_kg):
    """Given a calorie target, fixed protein, and the day's periodized carb
    target, settle carbs + fat so the macros sum to the target. Fat fills the
    gap with a 0.5 g/kg floor and a 30%-of-calories ceiling; when the target is
    too low to hold the carbs, carbs are trimmed (flagged). Returns
    (carbs_g, fat_g, carbs_trimmed)."""
    carbs_g = carb_target_g
    fat_floor_kcal = weight_kg * 0.5 * 9
    fat_ceiling_g = max(weight_kg * 0.5, target_kcal * 0.30 / 9.0)
    fat_g = max((target_kcal - protein_g * 4 - carbs_g * 4) / 9.0, weight_kg * 0.5)
    carbs_trimmed = False
    if protein_g * 4 + carbs_g * 4 + fat_g * 9 > target_kcal + 1:
        carbs_g = max(0, round((target_kcal - protein_g * 4 - fat_floor_kcal) / 4.0))
        fat_g = weight_kg * 0.5
        carbs_trimmed = carbs_g < carb_target_g
    elif fat_g > fat_ceiling_g:
        fat_g = fat_ceiling_g
        carbs_g = max(carbs_g, round((target_kcal - protein_g * 4 - fat_g * 9) / 4.0))
    return carbs_g, round(fat_g), carbs_trimmed


def _meal_split(target_kcal: int, protein_g: int, carbs_g: int, fat_g: int,
                needs_fuel: bool, skip_breakfast: bool = False,
                fuel_carbs_g: int = 0, fuel_protein_g: int = 0) -> list[dict]:
    """Split a day's macros into meals so targets are actionable.

    The carbs/protein the fuel cards prescribe around the session(s) — pre +
    during + post, summed across every fueled session — are carved out first
    into a single 'Workout fuel' line, so the meal plan reconciles exactly with
    the per-workout fuel timeline. The remaining energy goes to sit-down meals,
    weighted toward lunch/dinner (not spread evenly) with a hard breakfast cap,
    so a high-expenditure day doesn't produce a 1,500-kcal breakfast.

    skip_breakfast: time-restricted eating — drop the Breakfast meal (weekends
    keep it) and shift its share into the later meals."""
    out: list[dict] = []
    fc = max(0, min(int(round(fuel_carbs_g or 0)), carbs_g))
    fp = max(0, min(int(round(fuel_protein_g or 0)), protein_g))
    if needs_fuel and (fc > 0 or fp > 0):
        out.append({"meal": "Workout fuel (pre/during/post)",
                    "protein_g": fp, "carbs_g": fc, "fat_g": 0})
    rem_p, rem_c, rem_f = protein_g - fp, carbs_g - fc, fat_g

    # Sit-down meals: weighted toward lunch/dinner, breakfast kept modest.
    sit = [("Breakfast", 0.22), ("Lunch", 0.30), ("Dinner", 0.33), ("Snack", 0.15)]
    if skip_breakfast:
        sit = [(n, w) for (n, w) in sit if n != "Breakfast"]
    sw = sum(w for _, w in sit) or 1.0
    meals = [{"meal": n,
              "protein_g": round(rem_p * w / sw),
              "carbs_g": round(rem_c * w / sw),
              "fat_g": round(rem_f * w / sw)} for (n, w) in sit]
    if not skip_breakfast:
        _cap_meal(meals, "Breakfast", _BREAKFAST_CAP_KCAL, "Dinner")
    out.extend(meals)

    for m in out:
        m["kcal"] = m["protein_g"] * 4 + m["carbs_g"] * 4 + m["fat_g"] * 9
    return out


def get_race_fueling(
    sport: str | None = None,
    duration_hours: float | None = None,
    intensity: str = "race",
    weight_kg: float | None = None,
    hot: bool = False,
    race_id: str | None = None,
) -> dict:
    """Race-day fueling calculator: pre-race meal, carb loading (if long),
    and hour-by-hour carbs / fluid / sodium / caffeine for the event.

    sport: 'cycling' | 'running' | 'triathlon' | 'swimming' | ...
    duration_hours: expected event duration.
    race_id: instead of the two above, the id of a race on the stored calendar
      (see get_races) — its sport, estimated duration and heat flag are used.
    """
    if race_id:
        stored = next((r for r in _load_races() if r.get("id") == race_id), None)
        if not stored:
            raise ValueError(f"no race with id '{race_id}' — see get_races")
        sport = sport or stored.get("sport")
        duration_hours = duration_hours or stored.get("duration_hours")
        hot = hot or bool(stored.get("hot"))
    if duration_hours is None:
        raise ValueError("pass duration_hours, or a race_id to read it from")
    dur = max(0.25, float(duration_hours))
    sport = (sport or "").lower()
    if weight_kg is None:
        try:
            b = get_athlete_baseline()
            weight_kg = b.get("weight_kg") if isinstance(b, dict) else None
        except Exception:  # noqa: BLE001
            pass
    w = weight_kg or 70.0

    # Carbs/hr scales with duration (gut tolerance + demand).
    if dur < 1.0:
        carbs_hr = 0
    elif dur <= 2.0:
        carbs_hr = 45
    elif dur <= 3.0:
        carbs_hr = 70
    else:
        carbs_hr = 90  # multiple transportable carbs (glucose:fructose ~1:0.8)
    during_total = round(carbs_hr * dur)
    fluid_hr = 750 if hot else 550
    sodium_hr = (900 if hot else 600) + (300 if dur > 3 else 0)

    plan = {
        "sport": sport, "duration_hours": round(dur, 2), "intensity": intensity,
        "weight_kg": round(w, 1),
        "pre_race_meal": {
            "timing": "3-4 h before",
            "carbs_g": round(w * (2 if dur < 2 else 3)),
            "note": "low fat/fiber; familiar foods; top off with 30 g carbs 15 min pre",
        },
        "during": {
            "carbs_g_per_hr": carbs_hr,
            "carbs_g_total": during_total,
            "fluid_ml_per_hr": fluid_hr,
            "sodium_mg_per_hr": sodium_hr,
            "gels_equiv": round(during_total / 22) if during_total else 0,
            "note": ("swim leg: no feeding; fuel the bike, then run"
                     if sport == "triathlon" else
                     ("mix glucose:fructose sources above 60 g/hr" if carbs_hr >= 70 else "")),
        },
        "caffeine": {
            "pre_mg": round(w * 3),
            "during": "another ~1-2 mg/kg mid-race for events > 2 h",
        },
        "post": {"protein_g": round(w * 0.4), "carbs_g": round(w * 1.0),
                 "note": "within 60 min; then a full meal"},
    }
    if dur >= 1.5:
        plan["carb_load"] = {
            "days": 2 if dur < 3 else 3,
            "carbs_g_per_kg_per_day": "8-10" if dur < 3 else "10-12",
            "carbs_g_per_day_example": f"{round(w * 8)}-{round(w * 12)}",
            "note": "taper training while loading; expect +1-2 kg water weight",
        }
    return plan


def get_adaptive_tdee(weeks: int = 6) -> dict:
    """Estimate the athlete's TRUE maintenance from logged intake vs actual
    weight change — far more accurate than BMR x 1.3 once there's data.

    maintenance = mean daily intake − (weight change kg × 7700 / window days).
    Also splits out the non-exercise base (maintenance − mean daily exercise
    burn) so generate_fueling_plan can use it in place of BMR x 1.3, then add
    each day's actual planned session burn on top.

    Confidence is gated on logging consistency and weight readings; when thin,
    the plan should keep using the formula base.
    """
    weeks = max(2, min(int(weeks), 12))
    window_days = weeks * 7
    trend = nutrition_trend(weeks=weeks)
    rows = trend.get("weeks", [])

    intake_num = intake_den = 0.0
    for r in rows:
        di = r.get("avg_daily_kcal_intake")
        dl = r.get("days_logged") or 0
        if di and dl:
            intake_num += di * dl
            intake_den += dl
    days_logged = int(intake_den)
    mean_intake = (intake_num / intake_den) if intake_den else None

    traj = trend.get("weight_trajectory") or {}
    delta_kg = traj.get("delta_kg")
    readings = traj.get("readings_count") or 0

    # Mean daily exercise burn over the window (from completed activities).
    mean_ex = None
    try:
        acts = get_activities_in_range(
            (date.today() - timedelta(days=window_days)).isoformat(),
            date.today().isoformat(),
        ) or []
        ex_sum = sum(a.get("calories") or 0 for a in acts if isinstance(a, dict))
        if ex_sum:
            mean_ex = round(ex_sum / window_days)
    except Exception:  # noqa: BLE001
        pass

    baseline = get_athlete_baseline()
    weight_kg = baseline.get("weight_kg") if isinstance(baseline, dict) else None
    formula_base = round(weight_kg * 22 * 1.3) if weight_kg else None

    maintenance = None
    non_ex_base = None
    if mean_intake is not None and delta_kg is not None:
        maintenance = round(mean_intake - (delta_kg * 7700 / window_days))
        if mean_ex is not None:
            non_ex_base = maintenance - mean_ex

    # Confidence: need decent logging AND multiple weight readings.
    log_pct = round(100 * days_logged / window_days, 1) if window_days else 0
    if maintenance is None or log_pct < 40 or readings < 4:
        confidence = "low"
    elif log_pct < 65 or readings < 8:
        confidence = "medium"
    else:
        confidence = "high"

    note = None
    if confidence == "low":
        note = ("Not enough logged data yet (need ~4+ weeks of consistent "
                "food logging + regular weigh-ins) — the plan keeps using the "
                "BMR-formula base until this firms up.")
    elif maintenance and formula_base:
        drift = maintenance - formula_base
        note = (f"Measured maintenance runs {drift:+} kcal vs the BMR formula "
                f"({formula_base}) — the plan will use the measured value.")

    return {
        "total_maintenance_kcal": maintenance,
        "non_exercise_base_kcal": non_ex_base,
        "mean_daily_exercise_kcal": mean_ex,
        "formula_base_kcal": formula_base,
        "days_logged": days_logged,
        "window_days": window_days,
        "logging_pct": log_pct,
        "weight_readings": readings,
        "weight_delta_kg": delta_kg,
        "confidence": confidence,
        "note": note,
    }


def _project_trajectory(weight_kg, target, start_w, target_date, front_load_val,
                        bmr, mean_expenditure, ffm_kg, ea_min_val, floor_mult,
                        min_kcal_val, deficit_cap, uncapped, aggressive=False,
                        non_ex_base=None) -> dict:
    """Simulate the weight curve forward week by week under the SAME deficit
    logic the plan uses (front-load + floors), so the taper is visible and we
    can report a realistic finish date. FFM held constant (slightly
    conservative). Returns weekly points + finish date.

    aggressive: mirror the plan's aggressive mode — hold the max sustainable
    deficit every week instead of pacing to the target date, so a lighter
    current weight yields an EARLIER finish rather than the same date."""
    if not (target and weight_kg and weight_kg > target):
        return {}

    # Max sustainable daily deficit under the active floors. The EA floor's
    # allowance is burn-independent: expenditure − (ea_min·FFM + burn) =
    # non_exercise_base − ea_min·FFM. Use the plan's real modeled base (RMR ×
    # NEAT + adaptation) so the projected rate matches the per-day targets;
    # fall back to bmr·1.3 only when the base wasn't supplied.
    ne_base = non_ex_base if non_ex_base else bmr * 1.3
    caps = []
    if ea_min_val and ffm_kg:
        caps.append(ne_base - ea_min_val * ffm_kg)
    if floor_mult and floor_mult > 0:
        caps.append(bmr * (1.3 - floor_mult))
    if min_kcal_val:
        caps.append(mean_expenditure - min_kcal_val)
    if not uncapped and deficit_cap:
        caps.append(deficit_cap)
    max_daily = min(caps) if caps else 1500.0
    max_daily = max(0.0, max_daily)

    try:
        td = datetime.strptime(str(target_date)[:10], "%Y-%m-%d").date() if target_date else None
    except (ValueError, TypeError):
        td = None

    points = [{"date": _local_today().isoformat(), "weight_kg": round(weight_kg, 2)}]
    w = weight_kg
    d = _local_today()
    finish = None
    for _ in range(60):  # up to ~14 months
        if w <= target:
            finish = d
            break
        if aggressive:
            # Hold the max sustainable deficit — the finish date then falls out
            # of the pace, so losing weight brings it forward.
            daily = max_daily
        else:
            weeks_left = None
            if td and (td - d).days > 0:
                weeks_left = max(1, (td - d).days / 7.0)
            d_lin = ((w - target) * 7700 / weeks_left) / 7 if weeks_left else max_daily
            mult = 1.0
            if front_load_val and start_w and start_w > target:
                frac = max(0.0, min(1.0, (w - target) / (start_w - target)))
                mult = max(0.2, 1 + front_load_val * (2 * frac - 1))
            daily = min(d_lin * mult, max_daily)
        daily = max(0.0, daily)
        w = w - daily * 7 / 7700.0
        d = d + timedelta(days=7)
        points.append({"date": d.isoformat(), "weight_kg": round(max(w, target), 2)})
        if w <= target:
            finish = d
            break

    out = {
        "points": points,
        "target_weight_kg": round(target, 1),
        "target_date": str(target_date)[:10] if target_date else None,
        "max_sustainable_deficit_kcal": round(max_daily),
        "max_weekly_loss_kg": round(max_daily * 7 / 7700.0, 2),
    }
    if finish:
        out["projected_finish_date"] = finish.isoformat()
        out["projected_weeks"] = round((finish - _local_today()).days / 7.0, 1)
        if td:
            out["beats_target_date"] = finish <= td
    else:
        out["projected_finish_date"] = None
        out["note"] = "Target not reached within the projection horizon at the sustainable rate."
    return out


def _goal_baseline_date(goal: dict | None) -> str | None:
    """The day the current block's baseline was set, for windowing the weight
    chart. Prefers start_date; falls back to set_date for goals stored before
    start_date existed (on those, set_date still means "when the goal was set").
    None when there's no goal, which leaves the chart unwindowed."""
    if not goal:
        return None
    anchor = goal.get("start_date") or goal.get("set_date")
    return str(anchor)[:10] if anchor else None


def _weight_series(since: str | None = None) -> list[tuple[str, float]]:
    """Recent (date, weight_kg) points, oldest first, from the shared weigh-in
    snapshot — the same source as the current-weight reader, so a new weigh-in
    appears in the history/trend/chart the moment the refresh job writes it.

    `since` (ISO date) drops readings from before the current block started, so
    a re-anchored goal doesn't plot the previous block's weights. Callers that
    want the full window — trend calibration, adaptive TDEE — omit it."""
    return [(e["date"], e["weight_kg"]) for e in _weigh_in_entries()
            if e.get("weight_kg") is not None
            and (since is None or e["date"] >= since)]


# Modeled adaptive thermogenesis: on a sustained cut the body downregulates
# NEAT (the classic mid-diet stall). This is the REAL driver of a plateau —
# true maintenance drifts below the textbook estimate, so a plan that doesn't
# account for it feeds you at a deficit smaller than it thinks and you lose
# slower than planned. Trimming the expenditure estimate keeps the deficit
# honest → faster, not slower, loss (subject to the EA / BMR floors below).
#
# Adaptation isn't gated by an absolute bodyweight; it begins early in any
# deficit and scales with fat mass shed (Rosenbaum/Leibel; Biggest Loser
# follow-ups: ~5-10%+ TDEE suppression, tracking % mass lost). kg-lost-from-
# start is the best proxy available here. ~1.2% of NEAT per kg lost, capped so
# the modeled estimate can only shave a modest slice — the *measured* weigh-in
# trend overrides it entirely once there's enough scale data to calibrate.
_ADAPT_PER_KG_LOST = 0.012
# Cap the modeled discount at 12% of NEAT. Leibel/Rosenbaum 1995 (NEJM) put
# non-resting EE decline at 3-4 kcal/kg-FFM/day for a 10% loss (~23-31
# kcal/day per kg here) — the 1.2%/kg slope matches its lower edge, and a 12%
# cap (reached ~10kg/22lb lost) tracks that range for a deep cut rather than
# truncating it early at 8%. The measured weigh-in trend overrides this once
# there's enough scale data.
_ADAPT_MAX = 0.12


def _modeled_adaptation_factor(goal: dict, weight_kg: float | None) -> float:
    """Multiplicative NEAT discount (<=1.0) from modeled metabolic adaptation,
    scaling with kg lost from the start weight (the fat-loss proxy that tracks
    real adaptive thermogenesis). 1.0 (no discount) for non-lose goals, unknown
    weight, or no measurable loss yet. This is the fallback for when the
    *measured* weigh-in trend can't calibrate — the two must not both apply."""
    if (goal or {}).get("goal_type") != "lose":
        return 1.0
    start_w = goal.get("start_weight_kg")
    if not (start_w and weight_kg) or weight_kg >= start_w:
        return 1.0
    kg_lost = start_w - weight_kg
    return 1.0 - min(_ADAPT_MAX, _ADAPT_PER_KG_LOST * kg_lost)


def _weight_trend_calibration(goal: dict, weight_kg: float) -> dict | None:
    """Compare the measured weight trend to what the planned deficit predicts
    and, when there's enough data, nudge the non-exercise base to close the gap
    — self-correcting for estimation error and metabolic adaptation.

    Only ever *tightens* the base (losing slower than planned) and bounds the
    correction to 12%, since loosening on noisy scale data could stall a cut.
    Assumes the athlete has been eating roughly to target — stated in the note.
    Returns a dict (possibly note-only) or None for non-lose goals."""
    if (goal or {}).get("goal_type") != "lose":
        return None
    series = _weight_series()
    if len(series) < 3:
        return {"note": "Trend check: not enough recent weigh-ins to calibrate against "
                        "your actual loss rate — sync weight a few times a week to unlock "
                        "trend-based correction.", "confidence": "none"}
    d0 = datetime.strptime(series[0][0], "%Y-%m-%d").date()
    xs = [(datetime.strptime(d, "%Y-%m-%d").date() - d0).days for d, _ in series]
    ys = [w for _, w in series]
    if xs[-1] - xs[0] < 10:
        return {"note": "Trend check: weigh-ins span under ~10 days — need a longer "
                        "window to read the trend.", "confidence": "low"}
    n = len(xs); sx = sum(xs); sy = sum(ys)
    sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in zip(xs, ys))
    denom = (n * sxx - sx * sx) or 1
    slope = (n * sxy - sx * sy) / denom                       # kg/day (neg = losing)
    intercept = (sy - slope * sx) / n
    fitted_weight = round(intercept + slope * xs[-1], 2)      # trend value at latest weigh-in
    measured_kg_wk = round(slope * 7, 3)
    wr = _weeks_remaining(goal.get("target_date"))
    tgt = goal.get("target_weight_kg")
    if not (wr and tgt and weight_kg > tgt):
        return {"note": None, "confidence": "low", "measured_kg_per_week": measured_kg_wk,
                "fitted_weight_kg": fitted_weight}
    expected_kg_wk = -round((weight_kg - tgt) / wr, 3)
    conf = "high" if (n >= 4 and (xs[-1] - xs[0]) >= 21) else "medium"
    exp, meas = abs(expected_kg_wk), -measured_kg_wk       # positive loss rates
    factor = note = None
    if exp > 0.05 and -0.05 <= meas < exp * 0.8:
        shortfall = min(1.0, (exp - meas) / exp)
        if conf == "high":
            factor = round(max(0.88, 1 - 0.12 * shortfall), 3)
            note = (f"Trend check: losing ~{abs(measured_kg_wk):.2f} kg/wk vs the "
                    f"~{exp:.2f} kg/wk your deficit predicts — tightened the base "
                    f"{round((1 - factor) * 100)}% to get back on pace (assumes you've "
                    "eaten to target; log food to confirm).")
        else:
            note = (f"Trend check: losing ~{abs(measured_kg_wk):.2f} kg/wk vs ~{exp:.2f} "
                    "predicted — need a few more weigh-ins before I auto-tighten the base.")
    elif exp > 0.05 and meas > exp * 1.25:
        note = (f"Trend check: losing ~{abs(measured_kg_wk):.2f} kg/wk, faster than the "
                f"~{exp:.2f} kg/wk plan — ease the deficit if that's unintended.")
    return {"applied_factor": factor, "note": note, "confidence": conf,
            "measured_kg_per_week": measured_kg_wk, "expected_kg_per_week": expected_kg_wk,
            "fitted_weight_kg": fitted_weight}


# How far ahead of the straight-line schedule the athlete can get before the
# plan deliberately eases the cut, and how gently. "Ahead" is measured in weeks
# of *scheduled* loss; past the threshold the deficit is shaved, capped so a
# big lead can never gut the cut (steady momentum > yo-yo).
_AHEAD_EASE_THRESHOLD_WEEKS = 2.0
_AHEAD_EASE_PER_WEEK = 0.10        # 10% ease per week ahead beyond the threshold
_AHEAD_EASE_MAX = 0.50             # never ease more than 50%
_AGGR_EASE_WEIGHT_KG = 157 / 2.20462   # <157 lb (~71.2 kg): gate for easing even in aggressive mode


def _schedule_lead(goal: dict, weight_kg: float | None,
                   trend_cal: dict | None) -> dict | None:
    """How far ahead of the straight-line schedule the athlete is, and the
    resulting deficit-ease factor (<=1.0).

    The schedule is the line from start_weight (at set_date) to target_weight
    (at target_date). "Where should I be today" is read off that line; the lead
    is (expected_today - actual) converted to *weeks of scheduled loss*. Prefer
    the weigh-in trend's fitted weight for 'actual' when it's trustworthy (less
    noisy than a single scale reading), else the current weight.

    Returns None when it can't be computed (no timeline / not a lose goal /
    already at or past target). Otherwise a dict with lead_weeks and ease_factor
    (1.0 = no ease)."""
    if (goal or {}).get("goal_type") != "lose":
        return None
    tgt = goal.get("target_weight_kg")
    start_w = goal.get("start_weight_kg")
    set_date = goal.get("set_date")
    target_date = goal.get("target_date")
    if not (tgt and start_w and set_date and target_date and weight_kg):
        return None
    try:
        d_start = datetime.strptime(set_date[:10], "%Y-%m-%d").date()
        d_target = datetime.strptime(target_date[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    total_days = (d_target - d_start).days
    if total_days <= 0 or start_w <= tgt:
        return None
    elapsed = (_local_today() - d_start).days
    if elapsed <= 0:
        return None
    frac = min(1.0, elapsed / total_days)
    expected_today = start_w - (start_w - tgt) * frac      # straight-line schedule
    weekly_sched = (start_w - tgt) / (total_days / 7.0)     # scheduled kg/wk
    # 'actual' weight: the trend's fitted current value when trustworthy, else
    # the current scale/goal weight.
    actual = weight_kg
    if trend_cal and trend_cal.get("confidence") in ("medium", "high") \
            and trend_cal.get("fitted_weight_kg") is not None:
        actual = trend_cal["fitted_weight_kg"]
    lead_kg = expected_today - actual                       # >0 = ahead (lighter)
    if weekly_sched <= 0:
        return None
    lead_weeks = lead_kg / weekly_sched
    ease = 0.0
    if lead_weeks >= _AHEAD_EASE_THRESHOLD_WEEKS:
        ease = min(_AHEAD_EASE_MAX,
                   _AHEAD_EASE_PER_WEEK * (lead_weeks - _AHEAD_EASE_THRESHOLD_WEEKS))
    return {
        "lead_weeks": round(lead_weeks, 2),
        "lead_kg": round(lead_kg, 2),
        "ease_factor": round(1.0 - ease, 3),
        "source": "trend" if actual != weight_kg else "current_weight",
    }


def generate_fueling_plan(
    start_date: str | date | None = None,
    days: int = 7,
    save: bool = False,
    carb_load: bool = False,
    max_deficit_kcal: float | None = None,
    ea_floor: float | None = None,
    fuel_min_minutes: int = 90,
    bmr_floor_mult: float | None = None,
    periodize_deficit: bool | None = None,
    ea_min: float | None = None,
    min_kcal: float | None = None,
    rebalance: bool | int = False,
    front_load: float | None = None,
    use_adaptive_tdee: bool | None = None,
    max_loss_lb_per_week: float | None = None,
    heat_c: float | None = None,
    skip_breakfast_weekdays: bool | None = None,
) -> dict:
    """Build a forward fueling plan: per-day calorie + macro targets and a
    per-workout fuel card for the next `days` days, from the stored fueling
    goal + body stats + Garmin scheduled workouts. Formulas mirror
    skills/weekly.md + skills/project-instructions.md.

    Session burn is calibrated from the athlete's own 90-day history (median
    kcal/hr per sport), falling back to a generic table, then converted to
    Active Calories (net of the resting metabolism the athlete would have
    burned anyway) — "burned"/"projected" totals and the energy-availability
    calc should reflect exercise only, not a resting-calorie baseline for the
    workout's duration. Every live fetch degrades gracefully so the read-only
    web service can serve this from the nightly pre-warmed cache.

    If `save=True`, merges the per-day plan into the weekly snapshot (under
    nutrition_plan) so nutrition_plan_vs_actual / /morning can track adherence;
    existing snapshot fields are preserved.
    """
    days = max(1, min(int(days), 28))
    start = _coerce_date(start_date) if start_date else _local_today()
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
    # A manual current-weight override on the goal wins over Garmin's synced
    # reading (used when Garmin is stale/wrong).
    manual_weight = goal.get("current_weight_kg")
    weight_kg = manual_weight or body.get("weight_kg") or goal.get("start_weight_kg")
    if weight_kg is None:
        try:
            base = get_athlete_baseline()
            weight_kg = base.get("weight_kg") if isinstance(base, dict) else None
        except Exception:  # noqa: BLE001
            pass
    if manual_weight:
        notes.append(
            f"Using your manual weight {manual_weight}kg "
            f"({round(manual_weight * 2.20462, 1)} lb) set "
            f"{goal.get('current_weight_as_of') or 'manually'} — clear it via "
            "set_fueling_goal once Garmin syncs a current weigh-in."
        )
    if not weight_kg:
        return {
            "error": "no_weight",
            "message": "No recent weight from Garmin body composition or baseline. "
                       "Log a weigh-in or pass start_weight_kg to set_fueling_goal.",
            "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        }

    # Fat-free mass: a *measured* value (from a Renpho body-fat reading) when
    # present, else an ~80% of bodyweight fallback. The measured value keys
    # both the RMR (Katch-McArdle) and the energy-availability guard.
    measured_ffm = body.get("lean_mass_kg")
    ffm_kg = measured_ffm or round(weight_kg * 0.8, 1)

    bmr, bmr_source = _bmr(weight_kg, goal.get("sex"), goal.get("height_cm"),
                           goal.get("age"), ffm_kg=measured_ffm)
    if bmr_source == "weight_x22_fallback":
        notes.append("BMR estimated as weight x22 — set sex/height/age via "
                     "set_fueling_goal (or sync a Renpho body-fat reading) for a "
                     "measured value.")

    # Non-exercise daily base = RMR x NEAT factor (activity only, no exercise,
    # no TEF — both added explicitly below), or the athlete's measured
    # maintenance (adaptive TDEE) once there's enough logged food. Net exercise
    # energy is added per day on top; TEF is added from the day's macros.
    neat_base = bmr * _NEAT_MULT
    base_source = "rmr_formula"
    tef_applies = True
    adaptive = None
    at_raw = use_adaptive_tdee if use_adaptive_tdee is not None else goal.get("use_adaptive_tdee")
    if at_raw:
        try:
            adaptive = get_adaptive_tdee(weeks=6)
            neb = adaptive.get("non_exercise_base_kcal")
            if neb and adaptive.get("confidence") in ("medium", "high") \
                    and 0.7 * bmr * 1.3 <= neb <= 1.6 * bmr * 1.3:
                # Measured maintenance already bakes in this athlete's real NEAT
                # and TEF, so don't double-count TEF on top of it.
                neat_base = neb
                base_source = "adaptive_tdee"
                tef_applies = False
                if adaptive.get("note"):
                    notes.append(adaptive["note"])
            elif adaptive.get("confidence") == "low":
                notes.append(adaptive.get("note") or "Adaptive TDEE: not enough data yet.")
        except Exception:  # noqa: BLE001
            pass

    # Weigh-in trend recalibration: compare the measured weight trend to what
    # the planned deficit predicts and nudge the base to close the gap.
    trend_cal = _weight_trend_calibration(goal, weight_kg)
    measured_trend_applied = False
    if trend_cal:
        if trend_cal.get("applied_factor"):
            neat_base *= trend_cal["applied_factor"]
            base_source = base_source + "+weigh_in_trend"
            measured_trend_applied = True
        if trend_cal.get("note"):
            notes.append(trend_cal["note"])

    # Modeled adaptive thermogenesis — only when the *measured* trend didn't
    # already correct the base (else we'd double-count the same slowdown). Uses
    # kg-lost-so-far as the proxy for how much NEAT has downregulated.
    if not measured_trend_applied and base_source.startswith("rmr"):
        adapt = _modeled_adaptation_factor(goal, weight_kg)
        if adapt < 1.0:
            neat_base *= adapt
            base_source += "+modeled_adaptation"
            notes.append(
                f"Modeled metabolic adaptation: NEAT trimmed {round((1 - adapt) * 100)}% "
                f"for the ~{_fmt_weight((goal.get('start_weight_kg') or weight_kg) - weight_kg, goal.get('units'))} "
                "lost so far (the mid-cut slowdown — keeps your real deficit honest so you "
                "don't stall). Sync weigh-ins regularly to replace this estimate with your "
                "measured trend."
            )

    # Goal daily kcal adjustment (deficit/surplus)
    wr = _weeks_remaining(goal.get("target_date"))
    gt = goal["goal_type"]
    tgt = goal.get("target_weight_kg")
    protein_per_kg = goal.get("protein_g_per_kg") or _PROTEIN_G_PER_KG_DEFAULT.get(gt, 1.6)

    # Resolve the safety knobs: explicit call arg > stored goal > default.
    # max_loss_lb_per_week is a friendlier cap: 1 lb ≈ 3500 kcal.
    mll_raw = max_loss_lb_per_week if max_loss_lb_per_week is not None else goal.get("max_loss_lb_per_week")
    if mll_raw and mll_raw > 0:
        deficit_cap = mll_raw * 3500.0 / 7.0
        uncapped = False
    else:
        cap_raw = max_deficit_kcal if max_deficit_kcal is not None else goal.get("max_deficit_kcal")
        deficit_cap = 500 if cap_raw is None else cap_raw
        uncapped = deficit_cap <= 0
    floor_raw = ea_floor if ea_floor is not None else goal.get("ea_floor")
    ea_threshold = 30 if floor_raw is None else floor_raw
    fm_raw = bmr_floor_mult if bmr_floor_mult is not None else goal.get("bmr_floor_mult")
    floor_mult = 1.2 if fm_raw is None else fm_raw
    floor_val = round(bmr * floor_mult) if floor_mult > 0 else 0
    pd_raw = periodize_deficit if periodize_deficit is not None else goal.get("periodize_deficit")
    periodize = True if pd_raw is None else bool(pd_raw)
    em_raw = ea_min if ea_min is not None else goal.get("ea_min")
    ea_min_val = em_raw if (em_raw and em_raw > 0) else None
    mk_raw = min_kcal if min_kcal is not None else goal.get("min_kcal")
    min_kcal_val = mk_raw if (mk_raw and mk_raw > 0) else None
    fl_raw = front_load if front_load is not None else goal.get("front_load")
    front_load_val = max(0.0, min(0.9, fl_raw)) if fl_raw else 0.0
    sb_raw = skip_breakfast_weekdays if skip_breakfast_weekdays is not None \
        else goal.get("skip_breakfast_weekdays")
    skip_breakfast_val = bool(sb_raw)
    home_lat = goal.get("home_lat")
    home_lon = goal.get("home_lon")

    aggressive = bool(goal.get("aggressive"))
    goal_adj = 0
    if gt == "lose":
        if aggressive:
            # Aggressive: hold the max sustainable deficit (bounded by the
            # deficit cap; the per-day EA/BMR floors below still clamp it), so
            # being ahead pulls the finish date earlier instead of easing the
            # cut. No date-pacing.
            raw = deficit_cap if (deficit_cap and not uncapped) else 1500.0
            goal_adj = -round(raw)
            notes.append(
                "Aggressive mode: holding the maximum sustainable deficit "
                f"(~{round(raw)} kcal/day, floored by energy availability) rather "
                "than pacing to the target date — losing faster pulls your finish "
                "date earlier. Turn it off via set_fueling_goal (aggressive=false)."
            )
            # Safety valve on top of aggressive: once you're both lean enough
            # (< 157 lb / 71.2 kg) AND banking a real lead (>2 weeks ahead of
            # the straight-line schedule), ease the cut so you don't grind out
            # the last stretch at max deficit while already ahead. Both gates
            # must hold — being ahead while still heavier keeps full aggression.
            lead = _schedule_lead(goal, weight_kg, trend_cal)
            if (weight_kg is not None and weight_kg < _AGGR_EASE_WEIGHT_KG
                    and lead and lead["ease_factor"] < 1.0):
                goal_adj = round(goal_adj * lead["ease_factor"])
                notes.append(
                    f"Lean + ahead: under {_fmt_weight(_AGGR_EASE_WEIGHT_KG, goal.get('units'))} "
                    f"and ~{lead['lead_weeks']:.1f} weeks ahead of schedule"
                    f"{' by trend' if lead['source'] == 'trend' else ''} — eased the "
                    f"aggressive deficit {round((1 - lead['ease_factor']) * 100)}% for a "
                    "gentler finish. It re-tightens automatically if you drift back to schedule."
                )
        else:
            kg_to_lose = (weight_kg - tgt) if tgt else None
            if kg_to_lose and kg_to_lose > 0 and wr:
                raw = (kg_to_lose * 7700 / wr) / 7
            else:
                raw = 400  # moderate default cut when no target/timeline
            # Front-load: steeper while far from target, easing as weight nears
            # goal. Recomputed from current weight each run, so it self-tapers.
            start_w = goal.get("start_weight_kg")
            if front_load_val and start_w and tgt and start_w > tgt:
                fl_frac = max(0.0, min(1.0, (weight_kg - tgt) / (start_w - tgt)))
                fl_mult = max(0.2, 1 + front_load_val * (2 * fl_frac - 1))
                raw *= fl_mult
            goal_adj = -round(raw) if uncapped else -min(round(raw), deficit_cap)

            # Ahead-of-schedule ease: if the athlete has banked a comfortable
            # lead on the straight-line schedule (>=2 weeks of scheduled loss),
            # softens the cut — capped at 20% so a big lead can never gut it.
            # This is on top of the self-tapering already baked into raw
            # (kg-left / weeks-left recomputes each run). Trend-based when
            # weigh-ins allow, else current weight vs schedule.
            lead = _schedule_lead(goal, weight_kg, trend_cal)
            if lead and lead["ease_factor"] < 1.0:
                goal_adj = round(goal_adj * lead["ease_factor"])
                notes.append(
                    f"Ahead of schedule by ~{lead['lead_weeks']:.1f} weeks "
                    f"({_fmt_weight(lead['lead_kg'], goal.get('units'))} under the target line"
                    f"{' by trend' if lead['source'] == 'trend' else ''}) — eased the deficit "
                    f"{round((1 - lead['ease_factor']) * 100)}% for a gentler cut while you hold "
                    "the lead. It re-tightens automatically if you drift back to schedule."
                )
    elif gt == "gain":
        goal_adj = 400  # midpoint of +300-500, carb-led

    if carb_load:
        goal_adj = 0  # no deficit during a carb-load / race week
        notes.append("Race-week carb load: deficit suspended; carbs raised to "
                     "~9 g/kg on every day of the window.")

    # Rolling self-correction: compare recent logged days' actual intake
    # against their expenditure-adjusted targets and spread the accumulated
    # error across this window. rebalance=True looks at the current week to
    # date (Mon..yesterday); an integer looks back that many days.
    rebalance_adj = 0          # per-day kcal shift applied by rebalancing (surfaced in output)
    rebalance_detail = None    # human-readable breakdown for the target math
    if rebalance and not carb_load:
        if rebalance is True:
            rb_days = _local_today().weekday()  # Monday -> 0: fresh week, skip
        else:
            try:
                rb_days = max(0, min(int(rebalance), 14))
            except (TypeError, ValueError):
                rb_days = 0
        if rb_days:
            try:
                pva = nutrition_plan_vs_actual(days_back=min(rb_days + 1, 14))
                today_iso = _local_today().isoformat()
                drift = 0.0
                counted = 0
                for r in pva.get("rows", []):
                    if (r.get("date") or "") >= today_iso:
                        continue  # today is still in progress
                    if not r.get("foods_logged"):
                        continue  # unlogged days are noise, not signal
                    tgt_adj = r.get("adjusted_target_kcal") or r.get("target_kcal")
                    if r.get("actual_kcal") is not None and tgt_adj is not None:
                        drift += r["actual_kcal"] - tgt_adj
                        counted += 1
                # One-directional rebalance: only tighten to pay back an
                # overage (drift>0), never loosen to give calories back after
                # undereating (drift<0). Protects the deficit on good days.
                deficit_only = bool(goal.get("rebalance_deficit_only"))
                if counted and deficit_only and drift < 0:
                    rebalance_detail = {
                        "logged_days": counted,
                        "net_drift_kcal": round(drift),
                        "per_day_kcal": 0,
                        "deficit_only_suppressed": True,
                    }
                    notes.append(
                        f"Rebalance: you ate {round(-drift)} kcal UNDER your "
                        f"adjusted targets over {counted} logged day(s), but "
                        "deficit-only rebalancing is on, so targets aren't being "
                        "raised to give it back — your deficit stands."
                    )
                elif counted and abs(drift) > 100:
                    rebalance_adj = round(-drift / days)
                    goal_adj = goal_adj + rebalance_adj
                    rebalance_detail = {
                        "logged_days": counted,
                        "net_drift_kcal": round(drift),
                        "per_day_kcal": rebalance_adj,
                        "deficit_only_suppressed": False,
                    }
                    notes.append(
                        f"Rebalanced from the last {counted} logged day(s): net "
                        f"{round(drift):+} kcal vs expenditure-adjusted targets — "
                        f"spreading {rebalance_adj:+} kcal/day across this "
                        "window. Floors still apply."
                    )
            except Exception as ex:  # noqa: BLE001
                notes.append(f"Rebalance skipped ({str(ex)[:100]}).")

    # (ffm_kg and bmr were resolved above, before the base calculation.)

    # Scheduled workouts across the window
    scheduled_by_date: dict[str, list[dict]] = {}
    try:
        for it in get_scheduled_workouts(start.isoformat(), end.isoformat()):
            d_str = str(it.get("date") or "")[:10]
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
    hist_samples = _history_samples(history)

    # BMR/24 x hours: the resting-during-exercise proxy used both to net a
    # scheduled-session estimate to Active Calories (no precise per-activity
    # resting split exists for a session that hasn't happened yet) and, below,
    # as the fallback for a logged workout when Garmin's own resting split
    # (restingCaloriesFromActivity) hasn't synced yet.
    rmr_per_hr = (bmr or 0) / 24.0

    # Completed activities *today* — used to swap the estimate for the real
    # burn on the current day once a session is done, so today's target and
    # energy-availability track reality instead of a projection.
    today_iso = _local_today().isoformat()
    actual_today: list[dict] = []
    if start.isoformat() <= today_iso <= end.isoformat():
        for a in history:
            if not isinstance(a, dict):
                continue
            if (str(a.get("startTimeLocal") or a.get("startTimeGMT") or "")[:10]) != today_iso:
                continue
            dur_s = a.get("duration") or a.get("elapsedDuration") or a.get("movingDuration")
            cal = a.get("calories")
            if not dur_s or not cal or dur_s < 300:  # skip <5 min / no-calorie entries
                continue
            tk = (a.get("activityType") or {}).get("typeKey") or ""
            hrs = round(dur_s / 3600.0, 2)
            actual_today.append({
                "sport": _sport_bucket(a.get("activityName") or "", tk),
                "hours": hrs, "kcal": round(cal), "name": a.get("activityName") or "",
                "start": str(a.get("startTimeLocal") or a.get("startTimeGMT") or ""),
                "activity_id": a.get("activityId"),
            })
        # Collapse the same session recorded twice (e.g. a smart-trainer app AND
        # the watch both logging one ride, or a Strava re-import) before it can
        # spawn a phantom "unplanned" duplicate in the session list.
        actual_today = _dedupe_actual_workouts(actual_today)
        if actual_today:
            # Convert gross per-activity calories to Active Calories (see
            # _to_active_calories) so today's session list agrees with the
            # "today so far" card instead of showing Garmin's gross total.
            # restingCaloriesFromActivity is frequently still null for hours
            # after a workout syncs, in which case _to_active_calories falls
            # back to the rmr_per_hr proxy above rather than gross.
            try:
                sb_today = get_daily_summaries(
                    startdate=today_iso, enddate=today_iso, metrics=["stats_and_body"],
                ).get("stats_and_body", {}).get(today_iso)
                resting_from_activity = (
                    sb_today.get("restingCaloriesFromActivity")
                    if isinstance(sb_today, dict) and "error" not in sb_today else None
                )
            except Exception:  # noqa: BLE001
                resting_from_activity = None
            _to_active_calories(actual_today, resting_from_activity, rmr_per_hr)

    last_scheduled_date = max(scheduled_by_date) if scheduled_by_date else None

    skipped_sessions = _load_skipped_sessions()
    skipped_titles: list[str] = []
    fuel_trim_dates: list[str] = []
    no_show_titles: list[str] = []   # today's sessions dropped as late no-shows

    # Typical-day exercise (net Active Calories + hours) over the last 30 days,
    # split weekday vs weekend, for filling runs of unscheduled days that will
    # almost certainly pick up a workout later (see the phantom-day pass below).
    # A phantom Saturday assumes a typical Saturday, not a typical weekday.
    phantom_avg = _mean_daily_exercise(history, rmr_per_hr, start, window_days=30)
    # Whether any phantom-fillable average exists at all (either bucket).
    phantom_available = any(k > 0 and h > 0 for (k, h) in phantom_avg.values())

    # Races in (or adjacent to) this window, and the phase each day sits in.
    # Read once here so pass 1 can inject the race itself as a session and pass
    # 2 can apply the loading carbs, the eased deficit and the race fuel card.
    try:
        race_phases = _race_phases_for_window(start, days)
    except Exception:  # noqa: BLE001
        race_phases = {}   # a broken race store must never take the plan down
    race_cards: dict[str, dict] = {}

    # Pass 1: resolve each day's sessions, burn, and carb ratio.
    prelim: list[dict] = []
    unscheduled_idx: list[int] = []   # days with no scheduled/actual session at all
    for i in range(days):
        d = start + timedelta(days=i)
        d_iso = d.isoformat()
        had_scheduled = bool(scheduled_by_date.get(d_iso))
        sessions = []
        for it in scheduled_by_date.get(d_iso, []):
            title = (it.get("title") or it.get("workoutName")
                     or it.get("workoutNameKey") or "")
            intensity = _classify_intensity(title)
            sport = _sport_bucket(title, it.get("sportTypeKey") or "")
            if _is_session_skipped(skipped_sessions, d_iso, sport, title):
                skipped_titles.append(f"{title or sport} on {d_iso}")
                continue
            hours, hrs_src = _planned_hours(it, intensity)
            base_hr, burn_source = _kcal_per_hour_for(sport, hours, hist_samples)
            gross_est = base_hr * _INTENSITY_MULT.get(intensity, 1.0) * hours
            burn = max(0, round(gross_est - rmr_per_hr * hours))  # Active Calories estimate
            sessions.append({
                "title": title or sport, "sport": sport, "intensity": intensity,
                "hours": hours, "hours_source": hrs_src, "burn_kcal": burn,
                "kcal_per_hour": base_hr, "burn_source": burn_source, "done": False,
                "unplanned": False,
            })

        # Race day: the event is the day's biggest session by a wide margin and
        # the Garmin calendar usually has nothing on it (or a placeholder), so
        # add it explicitly — otherwise a marathon reads as a rest day and gets
        # planned a deficit. Skipped when something of the same sport and
        # roughly the race's length is already scheduled, which is the case
        # when the race was built as a workout. Injected before the
        # actual-today swap below so a finished race picks up its real burn.
        race_day = race_phases.get(d_iso)
        if race_day and race_day[1]["phase"] == "race_day":
            race = race_day[0]
            r_sport = race.get("sport") or "running"
            r_hours = float(race.get("duration_hours") or 1.0)
            # Triathlon has no burn history of its own; price it off cycling,
            # which dominates the day in both duration and calories.
            burn_sport = "cycling" if r_sport == "triathlon" else r_sport
            already = any(s["sport"] == burn_sport and s["hours"] >= 0.5 * r_hours
                          for s in sessions)
            if not already:
                base_hr, burn_src = _kcal_per_hour_for(burn_sport, r_hours, hist_samples)
                # Race intensity: the shorter the event the closer to all-out.
                race_mult = 1.15 if r_hours >= 3 else (1.25 if r_hours >= 1.5 else 1.35)
                gross = base_hr * race_mult * r_hours
                sessions = [s for s in sessions if s["intensity"] != "rest"]
                sessions.append({
                    "title": race.get("name") or "Race",
                    "sport": r_sport,
                    # Categorised for carb/deficit purposes, not for burn — the
                    # burn above already used the race multiplier.
                    "intensity": "long" if r_hours >= 2 else "threshold",
                    "hours": round(r_hours, 2), "hours_source": "race",
                    "burn_kcal": max(0, round(gross - rmr_per_hr * r_hours)),
                    "kcal_per_hour": round(base_hr * race_mult),
                    "burn_source": f"race_estimate_{burn_src}",
                    "done": False, "unplanned": False, "race": True,
                })

        # Today: swap the estimate for the actual burn on sessions already done,
        # and fold in any unplanned workouts, so "burned" reflects reality.
        if d_iso == today_iso and actual_today:
            remaining = list(actual_today)
            for s in sessions:
                m = next((x for x in remaining if x["sport"] == s["sport"]), None)
                if not m:
                    continue
                remaining.remove(m)
                s["burn_kcal"] = m["kcal"]                 # already Active Calories (see actual_today conversion above)
                s["hours"] = m["hours"] or s["hours"]
                s["kcal_per_hour"] = round(m["kcal"] / m["hours"]) if m["hours"] else s["kcal_per_hour"]
                s["burn_source"] = "actual_today"
                s["done"] = True
            for m in remaining:                            # unplanned completed workouts
                sessions.append({
                    "title": m["name"] or f"{m['sport'].title()} (logged)",
                    "sport": m["sport"], "intensity": _classify_intensity(m["name"]),
                    "hours": m["hours"], "hours_source": "actual", "burn_kcal": m["kcal"],
                    "kcal_per_hour": round(m["kcal"] / m["hours"]) if m["hours"] else 0,
                    "burn_source": "actual_today", "done": True, "unplanned": True,
                })

        # Late-evening no-show: once it's past the local cutoff (default 20:30
        # Eastern), a scheduled session for TODAY that still hasn't been logged
        # almost certainly won't happen — drop it so the day stops planning
        # (and fuelling) around a workout that isn't coming. Completed/unplanned
        # sessions stay; only not-yet-done scheduled ones are removed.
        if d_iso == today_iso and _past_workout_cutoff(d):
            kept = []
            for s in sessions:
                if (not s.get("done")) and s.get("burn_source") not in ("none",) \
                        and s.get("intensity") != "rest":
                    no_show_titles.append(f"{s['title']} on {d_iso}")
                else:
                    kept.append(s)
            sessions = kept

        if not sessions:
            sessions = [{"title": "rest", "sport": "rest", "intensity": "rest",
                         "hours": 0.0, "hours_source": "none", "burn_kcal": 0,
                         "burn_source": "none", "done": False}]
            # An empty day with nothing scheduled and nothing logged is a
            # candidate for the phantom-day fill below (a scheduled rest day
            # that happens to have no calendar entry is indistinguishable here,
            # but the "run of >=2 consecutive" rule keeps isolated true-rest
            # days untouched).
            if not had_scheduled:
                unscheduled_idx.append(i)

        # Every session's burn_kcal is already Active Calories (net of resting)
        # — actual/unplanned sessions come net via _to_active_calories above,
        # scheduled estimates are netted against rmr_per_hr x hours above — so
        # "burned"/"projected"/EA all reflect workout Active Calories only,
        # never a resting-calorie baseline for the workout's duration.
        total_burn = sum(s["burn_kcal"] for s in sessions)
        # Split into what's already been logged today (completed sessions) vs
        # what's still projected from sessions not yet done. On past/future
        # days everything is "projected" (nothing is marked done), so
        # burned_kcal is 0 and projected_burn_kcal == total_burn.
        burned_kcal = sum(s["burn_kcal"] for s in sessions if s.get("done"))
        projected_burn_kcal = total_burn - burned_kcal
        # Net exercise energy on top of the NEAT base is just the same active
        # total — no further subtraction needed since burn_kcal is net already.
        net_burn = total_burn
        # Periodize carbs off the hardest session of the day
        primary = max(sessions, key=lambda s: (_INTENSITY_ORDER.get(s["intensity"], 2),
                                               s["hours"]))
        carb_ratio = _CARB_G_PER_KG.get(primary["intensity"], 4.0)
        if primary["intensity"] in ("endurance", "long") and primary["hours"] > 2:
            carb_ratio = max(carb_ratio, 7.0)
        if carb_load:
            carb_ratio = 9.0
        # Training-load score for deficit periodization: TSS (hours x IF^2 x
        # 100), summed across the day's sessions (rest excluded). Using only
        # the day's single hardest session's category ("easy") to decide how
        # much deficit a day can absorb ignores both volume and the non-linear
        # cost of intensity — 2h of easy work and 20min of it are not the same
        # day, and neither is a 2h aerobic day and a 2h threshold day.
        day_load = sum(_session_tss(s["intensity"], s["hours"])
                       for s in sessions if s["intensity"] != "rest")
        prelim.append({
            "date": d_iso, "weekday": d.strftime("%a"), "sessions": sessions,
            "total_burn": total_burn, "net_burn": net_burn, "day_load": day_load,
            "burned_kcal": burned_kcal, "projected_burn_kcal": projected_burn_kcal,
            "primary": primary,
            "carb_ratio": carb_ratio,
            "base_target": neat_base + net_burn,
        })

    # Race phases: loading carbs and the race fuel card. Called after the
    # phantom-day fill below, so an unscheduled day inside a carb load keeps
    # its loading carbs instead of being overwritten by the typical-day
    # defaults. (The deficit side is applied later still, once the week's
    # deficit has been allocated across days.)
    def _apply_race_phases() -> None:
        for p in prelim:
            entry = race_phases.get(p["date"])
            if not entry:
                continue
            race, phase = entry
            p["race"] = {
                "id": race.get("id"), "name": race.get("name"),
                "date": race.get("date"), "sport": race.get("sport"),
                "distance_km": race.get("distance_km"),
                "distance_label": race.get("distance_label"),
                "priority": race.get("priority"),
                "duration_hours": race.get("duration_hours"),
                "duration_source": race.get("duration_source"),
                "phase": phase["phase"], "days_until": phase["days_until"],
                "label": phase["label"],
            }
            if phase["phase"] == "race_day":
                # Race-day carbs are whatever the race fuel card prescribes —
                # pre-race meal + everything taken on course + the post-race
                # refuel — so the day's macro target and the fuel timeline are
                # the same number rather than two competing ones. A malformed
                # stored race costs the card, not the day.
                try:
                    card = _race_fuel_card(race, weight_kg)
                except Exception:  # noqa: BLE001
                    continue
                race_cards[p["date"]] = card
                need = (card["pre_carbs_g"] + card["during_carbs_g_total"]
                        + card["post_carbs_g"])
                p["carb_ratio"] = round(
                    max(p["carb_ratio"], min(need / weight_kg, 14.0)), 1)
            elif phase["carb_g_per_kg"]:
                p["carb_ratio"] = max(p["carb_ratio"], phase["carb_g_per_kg"])

    # Phantom-day fill: a *run of 2 or more* consecutive unscheduled days is
    # almost never a genuine multi-day rest block — it's a stretch the calendar
    # just hasn't been filled in yet (workouts get synced from TrainingPeaks a
    # few days out). Left as true rest, deficit periodization banks its deepest
    # cuts there, so the day a session finally lands it's badly under-fuelled.
    # Assume each such day will carry a *typical* day's training (mean net
    # Active Calories + hours over the last 30 days) so it's planned — and
    # protected — like the training day it will most likely become. Isolated
    # single unscheduled days are left as true rest.
    phantom_dates: list[str] = []
    if phantom_available and len(unscheduled_idx) >= 2:
        idx_set = set(unscheduled_idx)
        run: list[int] = []

        def _phantom_avg_for(d: date) -> tuple[float, float]:
            """Weekend average on Sat/Sun, weekday average otherwise — falling
            back to the other bucket when the athlete has no history of that
            day-type in the window (so the day is still filled, not left rest)."""
            primary_key = "weekend" if d.weekday() >= 5 else "weekday"
            other_key = "weekday" if primary_key == "weekend" else "weekend"
            k, h = phantom_avg.get(primary_key, (0.0, 0.0))
            if k > 0 and h > 0:
                return k, h
            return phantom_avg.get(other_key, (0.0, 0.0))

        def _flush_run(r: list[int]) -> None:
            if len(r) < 2:
                return
            for j in r:
                p = prelim[j]
                p_date = date.fromisoformat(p["date"])
                p_kcal, p_hours = _phantom_avg_for(p_date)
                if p_kcal <= 0 or p_hours <= 0:
                    continue   # no usable average for this day — leave as rest
                sess = {
                    "title": "Assumed training (not yet scheduled)",
                    "sport": "training", "intensity": "easy",
                    "hours": round(p_hours, 2), "hours_source": "assumed",
                    "burn_kcal": round(p_kcal),
                    "kcal_per_hour": (round(p_kcal / p_hours) if p_hours else 0),
                    "burn_source": "assumed_30d_avg", "done": False,
                    "unplanned": False, "assumed": True,
                }
                p["sessions"] = [sess]
                p["total_burn"] = sess["burn_kcal"]
                p["net_burn"] = sess["burn_kcal"]
                p["burned_kcal"] = 0
                p["projected_burn_kcal"] = sess["burn_kcal"]
                p["primary"] = sess
                p["carb_ratio"] = _CARB_G_PER_KG.get("easy", 4.0)
                p["day_load"] = _session_tss("easy", p_hours)
                p["base_target"] = neat_base + sess["burn_kcal"]
                phantom_dates.append(p["date"])

        for i in range(days):
            if i in idx_set:
                run.append(i)
            else:
                _flush_run(run)
                run = []
        _flush_run(run)

    # After the phantom fill, so a loading day the calendar hasn't been filled
    # in for yet keeps its loading carbs rather than a typical-day default.
    _apply_race_phases()

    # Per-day floors: BMR multiple (if active), enforced EA minimum (scales
    # with each day's burn), and the absolute min_kcal — whichever is highest.
    floors: list[float] = []
    for p in prelim:
        f = float(floor_val)
        if ea_min_val and ffm_kg:
            f = max(f, ea_min_val * ffm_kg + p["total_burn"])
        if min_kcal_val:
            f = max(f, min_kcal_val)
        floors.append(f)

    # Per-day deficit: flat, or periodized toward rest/easy days (hard days
    # never take a deeper cut than the flat amount).
    periodize_applied = (periodize and gt == "lose" and goal_adj < 0
                         and not carb_load and days > 1)
    residual = 0
    if periodize_applied:
        day_adjs, residual = _allocate_deficit(
            [p["base_target"] for p in prelim],
            [p["primary"]["intensity"] for p in prelim],
            [p["day_load"] for p in prelim],
            goal_adj, floors,
        )
    else:
        day_adjs = [goal_adj] * days

    # Race phases scale the deficit *after* it has been allocated across the
    # week: suspended outright over the carb load, race day and the first
    # recovery day, softened through the taper and the rest of the recovery
    # window. Only cuts are scaled — a 'gain' goal's surplus is exactly what a
    # loading day wants, so it passes through untouched. The per-day floors
    # still apply below, so easing can only ever raise a day's target.
    race_notes: list[str] = []
    if race_phases:
        for i, p in enumerate(prelim):
            entry = race_phases.get(p["date"])
            if not entry:
                continue
            race, phase = entry
            mult = phase["deficit_multiplier"]
            if day_adjs[i] < 0 and mult < 1.0:
                day_adjs[i] = round(day_adjs[i] * mult)
            note = f"{race.get('name') or 'Race'} ({race['date']}) — {phase['note']}"
            if note not in race_notes:
                race_notes.append(note)

    # (Readiness-aware easing removed by request — the plan no longer softens
    # the deficit on low-recovery days. The daily target is driven only by the
    # goal, the deficit policy, and the day's training.)

    # Pass 2: targets, macros, EA, fuel cards.
    day_rows = []
    totals = {"target_kcal": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0,
              "est_burn_kcal": 0, "target_deficit_kcal": 0}
    plan_by_date: dict[str, dict] = {}
    for i, p in enumerate(prelim):
        d_iso = p["date"]
        sessions = p["sessions"]
        total_burn = p["total_burn"]
        primary = p["primary"]
        carb_ratio = p["carb_ratio"]

        # Expenditure before the thermic effect of food (RMR-NEAT base + net
        # exercise), and the pre-TEF target after the goal deficit + floors.
        exp_pre = round(p["base_target"])
        target_pre = max(round(p["base_target"] + day_adjs[i]), round(floors[i]), 0)
        pre_def = exp_pre - target_pre

        # This day's share of the rolling rebalance. rebalance_adj was added
        # into goal_adj before periodization split the total across days, so
        # attribute it proportionally to each day's share of goal_adj (flat
        # days: the full rebalance_adj; on a periodized week it scales).
        _day_rebalance = (rebalance_adj * (day_adjs[i] / goal_adj)
                          if (rebalance_adj and goal_adj) else 0.0)

        # Protein anchored to GOAL weight on a cut (not current scale weight),
        # so it holds steady as you lean out instead of drifting down; nudged
        # up on hard/long days and in a steep deficit to spare lean mass.
        protein_bump = 0.0
        if primary["intensity"] in ("threshold", "vo2", "long"):
            protein_bump += 0.2
        elif primary["intensity"] == "tempo":
            protein_bump += 0.1
        if pre_def >= 500:
            protein_bump += 0.1
        # Post-race repair needs more protein than the session that caused it
        # would normally earn — race day itself is scored by its intensity
        # above, the days after it by the recovery phase.
        _rp = race_phases.get(d_iso)
        if _rp:
            protein_bump += _rp[1]["protein_bump"]
        protein_per_kg_day = round(protein_per_kg + protein_bump, 2)
        protein_ref_kg = tgt if (gt == "lose" and tgt) else weight_kg
        protein_g = round(protein_ref_kg * protein_per_kg_day)
        carb_target_g = round(weight_kg * carb_ratio)   # periodized training need

        # Thermic effect of food: energy burned digesting the day's macros —
        # real expenditure, protein-heavy. It raises both the burn and the food
        # you can eat at the same net deficit, and rewards the high protein.
        # Sized from the macros actually eaten (settle them at the pre-TEF
        # target first, so a carb-trimmed day doesn't over-credit carb TEF).
        c0, f0, _ = _fill_macros(target_pre, protein_g, carb_target_g, weight_kg)
        tef_kcal = round(_TEF_FRAC["protein"] * protein_g * 4
                         + _TEF_FRAC["carbs"] * c0 * 4
                         + _TEF_FRAC["fat"] * f0 * 9) if tef_applies else 0
        expected_expenditure = exp_pre + tef_kcal
        target_kcal = target_pre + tef_kcal      # net deficit preserved
        target_deficit = expected_expenditure - target_kcal  # == pre_def; >0 = deficit
        carbs_g, fat_g, carbs_trimmed = _fill_macros(
            target_kcal, protein_g, carb_target_g, weight_kg)

        # Energy availability = (dietary intake − exercise energy) / fat-free
        # mass. Intake is the food eaten (target_kcal, TEF included — that's the
        # canonical definition). Sports-science low-EA threshold is ~30 kcal/kg
        # FFM/day; below ~25 is a clear RED-S / under-fueling risk.
        energy_availability = round((target_kcal - total_burn) / ffm_kg, 1) if ffm_kg else None
        # Auditable breakdown so the EA number is legible on the card: how
        # intake and exercise combine. (kcal_per_ea_point was dropped — it was
        # just FFM restated, static day to day, and the dashboard no longer
        # renders it. ffm_kg is retained as the divisor for anyone recomputing.)
        energy_availability_detail = {
            "intake_kcal": target_kcal,
            "exercise_kcal": total_burn,
            "tef_kcal": tef_kcal,          # part of intake, shown for transparency
            "ffm_kg": ffm_kg,
        } if ffm_kg else None

        # Fuel only sessions at/above the minimum duration (default 90 min).
        fuel_cards = []
        race_card = race_cards.get(d_iso)
        for s in sessions:
            if s.get("race") and race_card:
                # The race gets race-day numbers (60-90 g carbs/hr, race fluid
                # and sodium), not the deliberately conservative training rates
                # below — including for a swim, where the no-feeding rule is
                # about pool sessions, not an open-water race.
                fuel_cards.append(race_card)
                continue
            if s["hours"] * 60 < fuel_min_minutes or s["intensity"] == "rest":
                continue
            hrs = max(s["hours"], 1.0)
            is_swim = s["sport"] == "swimming"
            # Training carb rates — deliberately below race numbers (60-90 g/hr
            # is a race-day target; get_race_fueling still uses those).
            during_per_hr = 45 if s["hours"] > 3 else (40 if s["hours"] > 2 else 30)
            pre = 40 if (s["hours"] >= 2 or s["intensity"] in ("threshold", "vo2")) else 30
            card = {
                "session": s["title"], "intensity": s["intensity"], "hours": s["hours"],
                # Never fuel swims pre or during — pool sessions don't take
                # feeding well; post-swim refuel only.
                "pre_carbs_g": (0 if is_swim else pre),
                "during_carbs_g_per_hr": (0 if is_swim else during_per_hr),
                "during_carbs_g_total": (0 if is_swim else round(during_per_hr * hrs)),
                "post_protein_g": 25, "post_carbs_g": 60,
                "fluid_ml_per_hr": (0 if is_swim else 600),
                "sodium_mg_per_hr": (0 if is_swim else 600),
            }
            if is_swim:
                card["note"] = "swim — no pre/during fuel; refuel after"
            else:
                if s["intensity"] in ("threshold", "vo2", "long") or s["hours"] >= 2:
                    card["caffeine_mg"] = round(weight_kg * 3)
                if s["hours"] > 2.5:
                    card["note"] = ("long effort — 45 g/hr is the training target; "
                                    "save 60-90 g/hr (glucose:fructose) for racing")
                # Heat: bump fluid + sodium for outdoor sessions on hot days.
                if _is_outdoor_session(s["sport"], s["title"]):
                    high_c = heat_c
                    if high_c is None and home_lat is not None and home_lon is not None:
                        try:
                            high_c = weather.forecast_high_c(home_lat, home_lon, d_iso)
                        except Exception:  # noqa: BLE001
                            high_c = None
                    if high_c is not None and high_c >= 28:
                        card["fluid_ml_per_hr"] = round(card["fluid_ml_per_hr"] * 1.3)
                        card["sodium_mg_per_hr"] = round(card["sodium_mg_per_hr"] * 1.6)
                        card["heat_c"] = round(high_c)
                        card["note"] = (f"~{round(high_c)}°C forecast — pre-cool, drink to "
                                        "thirst+, extra sodium") + (
                                        "; " + card["note"] if card.get("note") else "")
            fuel_cards.append(card)

        # Total carbs/protein the fuel cards prescribe around the session(s) —
        # pre + during + post, summed across every fueled session — so the meal
        # plan's 'Workout fuel' line reconciles with the per-workout timeline.
        fuel_carbs = sum(c["pre_carbs_g"] + c["during_carbs_g_total"] + c["post_carbs_g"]
                         for c in fuel_cards)
        fuel_protein = sum(c["post_protein_g"] for c in fuel_cards)
        # If the day's whole carb budget (already possibly cut to fit a low
        # target_kcal — common when deficit periodization pushes a deep cut
        # onto an "easy" day that still has real training) is smaller than
        # what the fuel windows prescribe, the meal-line carve-out below
        # clamps to what's actually available. Without this, the per-workout
        # timeline kept showing the un-trimmed, unaffordable grams while the
        # 'Workout fuel' meal line silently showed less — the two disagreed.
        # Scale every card's carbs down to the same affordable total so both
        # views always match.
        if fuel_cards and fuel_carbs > carbs_g >= 0:
            ratio = carbs_g / fuel_carbs  # safe: fuel_carbs > carbs_g >= 0 implies fuel_carbs > 0
            for c in fuel_cards:
                c["pre_carbs_g"] = round(c["pre_carbs_g"] * ratio)
                c["during_carbs_g_total"] = round(c["during_carbs_g_total"] * ratio)
                c["during_carbs_g_per_hr"] = (
                    round(c["during_carbs_g_total"] / c["hours"]) if c["hours"] else 0)
                c["post_carbs_g"] = round(c["post_carbs_g"] * ratio)
            fuel_carbs = sum(c["pre_carbs_g"] + c["during_carbs_g_total"] + c["post_carbs_g"]
                             for c in fuel_cards)
            fuel_trim_dates.append(d_iso)
        # Weekday breakfast-skip (time-restricted eating) if the athlete set it.
        is_weekday = date.fromisoformat(d_iso).weekday() < 5
        # Never skip breakfast while loading or on race morning — the pre-race
        # meal is the point, and a loading day can't hit 10 g/kg in two sittings.
        no_skip = bool(_rp and _rp[1]["phase"] in ("race_day", "carb_load"))
        skip_bf = bool(skip_breakfast_val and is_weekday and not no_skip)

        row = {
            "date": d_iso, "weekday": p["weekday"],
            "sessions": sessions,
            "primary_intensity": primary["intensity"],
            "est_burn_kcal": total_burn,
            "burned_kcal": p["burned_kcal"],
            "projected_burn_kcal": p["projected_burn_kcal"],
            "net_exercise_kcal": p["net_burn"],
            "tef_kcal": tef_kcal,
            "expected_expenditure_kcal": expected_expenditure,
            "target_kcal": target_kcal,
            "target_deficit_kcal": target_deficit,
            "kcal_adjustment": day_adjs[i],
            # How the day's calorie adjustment breaks down, so the target math
            # is legible: the base goal deficit vs the rolling rebalance shift.
            # rebalance_adj was folded into goal_adj *before* periodization, so
            # on periodized days the rebalance portion scales with that day's
            # share of the total (day_adjs[i]/goal_adj), not a flat amount.
            "adjustment_breakdown": {
                "base_deficit_kcal": round(day_adjs[i] - _day_rebalance),
                "rebalance_kcal": round(_day_rebalance),
                "total_adjustment_kcal": day_adjs[i],
                "floored": target_pre > round(p["base_target"] + day_adjs[i]),
            },
            "protein_g": protein_g, "carbs_g": carbs_g, "fat_g": fat_g,
            "protein_g_per_kg": protein_per_kg_day,
            "carb_g_per_kg": carb_ratio,
            "carbs_trimmed": carbs_trimmed,
            "energy_availability_kcal_per_kg_ffm": energy_availability,
            "energy_availability_detail": energy_availability_detail,
            "needs_fuel": bool(fuel_cards),
            "fuel": fuel_cards,
            # Which race window (if any) this day sits in, and what phase.
            # None on an ordinary day.
            "race": p.get("race"),
            "skip_breakfast": skip_bf,
            "meals": _meal_split(target_kcal, protein_g, carbs_g, fat_g,
                                 bool(fuel_cards), skip_breakfast=skip_bf,
                                 fuel_carbs_g=fuel_carbs, fuel_protein_g=fuel_protein),
        }
        day_rows.append(row)
        for k in ("target_kcal", "protein_g", "carbs_g", "fat_g", "target_deficit_kcal"):
            totals[k] += row[k]
        totals["est_burn_kcal"] += total_burn

        plan_by_date[d_iso] = {
            "session": "; ".join(s["title"] for s in sessions),
            "target_kcal": target_kcal,
            "expected_expenditure_kcal": expected_expenditure,
            "protein_g": protein_g, "carbs_g": carbs_g, "fat_g": fat_g,
            "notes": "fuel pre/during/post" if fuel_cards else "",
        }

    if race_notes:
        notes.extend(race_notes)

    if skipped_titles:
        notes.append("Skipped (per skip_scheduled_session): " + "; ".join(skipped_titles) + ".")

    # Late no-show sessions are still dropped from today's plan (so the target
    # and fuel don't assume a workout that won't happen), but per request we no
    # longer surface a note about it — the drop is silent.

    if phantom_dates:
        wk = phantom_avg.get("weekday", (0.0, 0.0))
        we = phantom_avg.get("weekend", (0.0, 0.0))
        avg_bits = []
        if wk[0] > 0:
            avg_bits.append(f"~{round(wk[0])} kcal on weekdays")
        if we[0] > 0:
            avg_bits.append(f"~{round(we[0])} kcal on weekends")
        avg_txt = (" (" + ", ".join(avg_bits) + ", your 30-day averages)") if avg_bits else ""
        notes.append(
            f"On {', '.join(phantom_dates)} nothing is scheduled yet, but they fall in a "
            f"run of 2+ consecutive open days — so each is planned as a typical training day "
            f"for that day of week{avg_txt} rather than banking a deep-deficit rest day that a "
            "later-synced workout would leave under-fuelled. Actual sessions replace these "
            "as they appear on the calendar."
        )

    if fuel_trim_dates:
        notes.append(
            f"On {', '.join(fuel_trim_dates)} the per-workout fuel windows were scaled "
            "down to fit the day's carb budget (so the fuel timeline and the 'Workout "
            "fuel' meal line agree) — you won't hit ideal during-workout carb rates on "
            "those sessions. Ease the deficit on training days to fix it."
        )

    trimmed = [d["date"] for d in day_rows if d.get("carbs_trimmed")]
    if trimmed:
        notes.append(f"On {', '.join(trimmed)} the calorie target is too low to hold the "
                     "carbs your training needs — carbs were cut to fit and those sessions "
                     "will be under-fueled. Ease the deficit on training days to fix it.")

    low_ea = [d["date"] for d in day_rows
              if (d.get("energy_availability_kcal_per_kg_ffm") or 99) < ea_threshold]
    if low_ea:
        notes.append(f"Low energy availability (<{round(ea_threshold)} kcal/kg fat-free "
                     f"mass) on {', '.join(low_ea)} — under-fueling / RED-S risk; ease "
                     "the deficit or add carbs on those days.")

    # Forward weight-trajectory projection under the same deficit logic.
    projection = {}
    if gt == "lose":
        mean_exp = (sum(d["expected_expenditure_kcal"] for d in day_rows) / len(day_rows)
                    if day_rows else neat_base)
        projection = _project_trajectory(
            weight_kg, tgt, goal.get("start_weight_kg"), goal.get("target_date"),
            front_load_val, bmr, mean_exp, ffm_kg, ea_min_val, floor_mult,
            min_kcal_val, deficit_cap, uncapped, aggressive=aggressive,
            non_ex_base=neat_base,
        )
        if projection.get("projected_finish_date") and projection.get("beats_target_date") is False:
            notes.append(f"At the sustainable rate you reach "
                         f"{_fmt_weight(tgt, goal.get('units'))} around "
                         f"{projection['projected_finish_date']} — later than the "
                         f"{projection['target_date']} target.")

    # Coaching suggestions mined from the last week of actual logging (calorie
    # drift vs the adjusted target, protein adherence, logging gaps). Appended
    # after the model notes so the actionable "based on what you actually did"
    # guidance sits alongside the plan.
    try:
        protein_target_g = (round(protein_per_kg * weight_kg)
                            if (protein_per_kg and weight_kg) else None)
        for s in _logging_suggestions(protein_target_g=protein_target_g):
            if s not in notes:
                notes.append(s)
    except Exception:  # noqa: BLE001
        pass

    result = {
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "units": goal.get("units") or "metric",
        "goal": goal,
        "goal_progress": goal_info.get("progress"),
        "body": body,
        "bmr": {"value": bmr, "source": bmr_source, "weight_kg": weight_kg},
        "energy_base": {"value": round(neat_base), "source": base_source,
                        "neat_mult": _NEAT_MULT if base_source.startswith("rmr") else None,
                        "model": "RMR x NEAT + net exercise + TEF" if tef_applies
                                 else "measured maintenance + net exercise"},
        "adaptive_tdee": adaptive,
        "weight_trend": trend_cal,
        # Windowed to the current block. Trend calibration and adaptive TDEE
        # above deliberately still read the full series — narrowing those would
        # change the calorie targets, not just the picture.
        "weight_history": [{"date": d, "weight_kg": w}
                           for d, w in _weight_series(_goal_baseline_date(goal))],
        "fat_free_mass_kg": ffm_kg,
        "body_fat_pct": body.get("body_fat_pct"),
        "daily_kcal_adjustment": goal_adj,
        # Decompose the daily adjustment: base goal deficit vs the rolling
        # rebalance shift, so the target math is auditable at the top level.
        "adjustment_breakdown": {
            "base_deficit_kcal": goal_adj - rebalance_adj,
            "rebalance_kcal": rebalance_adj,
            "total_kcal": goal_adj,
        },
        "rebalance": rebalance_detail,
        "protein_g_per_kg": protein_per_kg,
        "carb_load": carb_load,
        # The stored race calendar (upcoming + recently finished), so the
        # dashboard can list and edit it without a second round trip.
        "races": _safe_races(),
        "config": {
            "deficit_cap_kcal": (None if uncapped else round(deficit_cap)),
            "max_loss_lb_per_week": (round(deficit_cap * 7 / 3500.0, 2) if not uncapped else None),
            "ea_floor_kcal_per_kg_ffm": ea_threshold,
            "fuel_min_minutes": fuel_min_minutes,
            "bmr_floor_mult": (floor_mult if floor_mult > 0 else None),
            "periodize_deficit": periodize_applied,
            "ea_min_kcal_per_kg_ffm": ea_min_val,
            "min_kcal": min_kcal_val,
            "front_load": front_load_val or None,
            "skip_breakfast_weekdays": skip_breakfast_val or None,
            "energy_base_source": base_source,
        },
        "projection": projection,
        "today_actuals": _today_actuals(),
        "recent_days": _recent_days(2),
        # Days whose food log the athlete flagged as inaccurate — excluded from
        # rebalance, adaptive TDEE, trend averages and coaching suggestions.
        "ignored_food_days": get_ignored_food_days(),
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


def push_nutrition_targets_to_garmin(
    target_date: str | date | None = None,
    days: int = 1,
) -> dict:
    """EXPERIMENTAL: write the fueling plan's daily calorie/macro targets into
    Garmin Connect's nutrition goals, so the Connect app shows OUR target
    instead of Garmin's default. Garmin then applies its own activity
    adjustment on top (dailyNutritionGoals.adjustedCalories rises with actual
    burn), which yields in-app auto-adjustment after each workout.

    Targets are read from the weekly snapshot's nutrition_plan — write one
    first via generate_fueling_plan(save=true) or /weekly. Requires live
    (non-readonly) mode: run from the cron env, not the web MCP.

    Garmin has no official API for this; we PUT to the nutrition-service
    endpoints the Connect app itself uses and report per-endpoint
    diagnostics, so one live run tells you exactly what stuck.
    """
    days = max(1, min(int(days), 7))
    start = _coerce_date(target_date) if target_date else _local_today()

    # Collect per-day targets from recent snapshots (latest wins per date).
    plan: dict[str, dict] = {}
    for snap in reversed(get_weekly_snapshots(weeks_back=2)):
        plan.update(snap.get("nutrition_plan") or {})

    c = get_client()
    results = []
    for i in range(days):
        d_iso = (start + timedelta(days=i)).isoformat()
        day = plan.get(d_iso)
        if not isinstance(day, dict) or not day.get("target_kcal"):
            results.append({"date": d_iso, "status": "no_plan_for_date"})
            continue
        payload = {
            "calendarDate": d_iso,
            "calories": day.get("target_kcal"),
            "protein": day.get("protein_g"),
            "carbohydrates": day.get("carbs_g"),
            "fat": day.get("fat_g"),
        }
        attempts = []
        ok = False
        for method, path in (
            ("PUT", "/nutrition-service/goals"),
            ("PUT", f"/nutrition-service/nutrition/goals/{d_iso}"),
            ("POST", "/nutrition-service/goals"),
        ):
            try:
                resp = _call_with_backoff(c.connectapi, path, method=method, json=payload)
                attempts.append({"endpoint": f"{method} {path}", "ok": True,
                                 "response": resp})
                ok = True
                break
            except TypeError:
                # connectapi without method kwarg — go through the raw client
                try:
                    raw = c.client.request(method, "connectapi", path, json=payload)
                    attempts.append({"endpoint": f"raw {method} {path}", "ok": True,
                                     "status_code": getattr(raw, "status_code", None)})
                    ok = True
                    break
                except Exception as ex2:  # noqa: BLE001
                    attempts.append({"endpoint": f"raw {method} {path}", "ok": False,
                                     "error": str(ex2)[:160]})
            except Exception as ex:  # noqa: BLE001
                attempts.append({"endpoint": f"{method} {path}", "ok": False,
                                 "error": str(ex)[:160]})
        results.append({"date": d_iso, "status": "pushed" if ok else "failed",
                        "targets": payload, "attempts": attempts})

    return {
        "experimental": True,
        "results": results,
        "note": ("Garmin applies its own activity adjustment on top of the base "
                 "goal (dailyNutritionGoals.adjustedCalories). Check tomorrow's "
                 "food-log payload to confirm the pushed goal stuck; if every "
                 "endpoint 4xx'd, Garmin changed the API and the attempts list "
                 "shows what to fix."),
    }


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
    # Garmin's calendar-month view includes the leading/trailing days of the
    # ADJACENT months (the padding cells that complete the week grid). So a range
    # spanning a month boundary sees the same scheduled workout in two different
    # month buckets, and both copies clear the date filter — which showed up
    # downstream as duplicated sessions (a 2-workout day rendering as
    # "4 sessions", each once as actual and once as est). Dedupe on the calendar
    # item's own identity, keeping the first copy.
    seen_keys: set = set()
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
            # Prefer Garmin's own item id; fall back to the fields that identify a
            # scheduled workout when id is missing, so a null id can't collapse
            # two genuinely different workouts into one.
            ident = item.get("id")
            key = (("id", ident) if ident is not None else
                   ("fallback", date_str, item.get("workoutId"),
                    item.get("title"), item.get("sportTypeKey")))
            if key in seen_keys:
                continue
            seen_keys.add(key)
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
    # Local day, not UTC: in the evening a UTC "today" is already tomorrow, so
    # the athlete's current local day would be classified as older-than-
    # yesterday and frozen at IMMUTABLE_TTL — never re-fetched, exactly the
    # stale-food symptom. Anchor the "still mutable" window to the local day.
    today_str = _local_today().isoformat()
    yesterday_str = (_local_today() - timedelta(days=1)).isoformat()
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
        # Accept ISO strings ("2026-04-15", "2026-04-15T10:58:02") and Garmin
        # epoch ints (body-comp dates) — _coerce_garmin_date handles both.
        d = _coerce_garmin_date(d_str)
        return (today - d).days if d else None

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
        # LT's power.weight is whatever weight was on file at the threshold
        # test — often stale. Keep it only as a fallback; the weigh-in
        # snapshot (actual scale reading) overrides it below so current
        # weight can't disagree with the fueling plan / dashboard chart.
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

    # --- Weight (ALWAYS prefer the weigh-in snapshot; fall back to LT) ---
    # The weigh-in snapshot is the actual scale reading and the same source
    # the fueling plan's current weight + the dashboard chart use, so it must
    # win over LT's power.weight (set above) — otherwise the "now" card shows
    # a stale threshold-test weight while the chart shows today's weigh-in.
    try:
        entries = _weigh_in_entries()
        if entries:
            latest = entries[-1]        # newest
            w = latest.get("weight_kg")
            if w:
                out["weight_kg"] = round(w, 1)
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
    today = _local_today().isoformat()
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
    today = _local_today().isoformat()
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
