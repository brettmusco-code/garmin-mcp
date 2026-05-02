"""Historical weather enrichment via Open-Meteo archive API.

Garmin's on-watch weather is often wrong by 5-15°F because the wrist
sensor picks up body heat, sun, and pavement warmth. Ambient temp from
a nearby weather station is far more useful for interpreting heat
stress, aerobic decoupling, and hydration demands.

Open-Meteo is free, no API key, unlimited requests, decades of archive.
We cache each (rounded-location, date) response in R2 since historical
weather never changes.

Cache key: {PREFIX}weather/{lat_r}/{lon_r}/{date}.json
  lat_r, lon_r are rounded to 0.1 degree (~11km) so activities at
  similar locations share cached data.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from . import cache

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
# Recent data (last ~10 days) may not be in archive yet — fall back to
# the forecast API which includes recent history and near-real-time obs.
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_SEC = 15
HOURLY_VARS = "temperature_2m,relative_humidity_2m,dewpoint_2m,apparent_temperature,wind_speed_10m,wind_gusts_10m,precipitation,weather_code"


def _round_coord(x: float) -> float:
    return round(x, 1)


def _fetch(url: str, params: dict) -> dict | None:
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT_SEC)
        r.raise_for_status()
        return r.json()
    except Exception as ex:  # noqa: BLE001
        logger.warning("open-meteo fetch failed: %s", ex)
        return None


def _fetch_hourly(lat: float, lon: float, date_iso: str) -> dict | None:
    """Fetch one day of hourly weather. Try archive first, fall back to
    forecast for recent days not yet in archive."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date_iso,
        "end_date": date_iso,
        "hourly": HOURLY_VARS,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "GMT",
    }
    data = _fetch(ARCHIVE_URL, params)
    # Archive sometimes returns 200 but with null hourly arrays for dates
    # too recent. Check and fall back.
    if data and data.get("hourly", {}).get("temperature_2m") and any(
        t is not None for t in data["hourly"]["temperature_2m"]
    ):
        return data
    # Fallback: forecast API includes recent past_days.
    from datetime import date
    days_ago = (date.today() - datetime.fromisoformat(date_iso).date()).days
    if 0 <= days_ago <= 14:
        params2 = {
            "latitude": lat,
            "longitude": lon,
            "hourly": HOURLY_VARS,
            "past_days": min(14, days_ago + 1),
            "forecast_days": 1,
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "GMT",
        }
        return _fetch(FORECAST_URL, params2)
    return data


def get_day(lat: float, lon: float, date_iso: str) -> dict | None:
    """Cached hourly weather for one (location, date). R2-cached forever —
    historical weather never changes."""
    lat_r = _round_coord(lat)
    lon_r = _round_coord(lon)
    key_parts = [f"{lat_r:+.1f}", f"{lon_r:+.1f}", date_iso]
    args = {"lat": lat_r, "lon": lon_r, "date": date_iso}
    # 100-year TTL — historical weather is immutable.
    hit = cache.get("weather_day", args, key_parts=key_parts,
                    ttl_seconds=100 * 365 * 24 * 3600)
    if hit is not None:
        return hit
    data = _fetch_hourly(lat_r, lon_r, date_iso)
    if data is None:
        return None
    cache.put("weather_day", args, data, key_parts=key_parts)
    return data


def _iter_hours(hourly: dict, start_utc: datetime, end_utc: datetime):
    """Yield per-hour dicts covering [start_utc, end_utc] from an
    Open-Meteo hourly response."""
    times = hourly.get("time", [])
    for i, t_str in enumerate(times):
        # Open-Meteo returns "2026-05-01T14:00" with timezone=GMT
        try:
            t = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if start_utc <= t <= end_utc:
            row = {"time": t_str}
            for var in hourly:
                if var == "time":
                    continue
                vals = hourly[var]
                if i < len(vals):
                    row[var] = vals[i]
            yield row


def summarize_activity_weather(
    lat: Optional[float],
    lon: Optional[float],
    start_time_gmt: Optional[str],
    duration_sec: Optional[float],
) -> dict[str, Any]:
    """Return a compact weather summary for one activity's time+location.

    Fields:
      temp_f, apparent_f        — avg over the session window (rounded)
      temp_f_start, temp_f_end  — start/end ambient temp
      humidity, dewpoint_f      — avg
      wind_mph, gust_mph        — avg / peak
      precipitation_in          — total over window
      conditions                — short verbal tag (clear / light rain / etc.)
      hours_covered             — how many hourly buckets contributed
      source                    — "open-meteo"
    """
    if lat is None or lon is None or not start_time_gmt or not duration_sec:
        return {"error": "missing lat/lon/time"}

    # Garmin returns multiple formats in practice:
    #   "2026-05-01 14:00:34"      (summary top-level, when present)
    #   "2026-05-01T21:43:51.0"    (summaryDTO, with fractional seconds)
    #   "2026-05-01T21:43:51.0Z"   (some endpoints)
    # Normalize then parse flexibly.
    try:
        s = start_time_gmt.strip()
        if s.endswith("Z"):
            s = s[:-1]
        s = s.replace("T", " ")
        # Strip fractional seconds if present
        if "." in s:
            s = s.split(".", 1)[0]
        start_utc = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return {"error": f"unparseable start_time_gmt: {start_time_gmt}"}

    end_utc = datetime.fromtimestamp(
        start_utc.timestamp() + float(duration_sec), tz=timezone.utc
    )
    # Open-Meteo returns per-hour; widen the window slightly so we catch
    # the hour the activity starts in and ends in.
    from datetime import timedelta
    query_start = start_utc - timedelta(hours=1)
    query_end = end_utc + timedelta(hours=1)

    # Fetch each date in the covered range (usually 1, occasionally 2).
    dates_needed = set()
    cur = query_start.date()
    while cur <= query_end.date():
        dates_needed.add(cur.isoformat())
        cur = (cur + timedelta(days=1))

    combined_hourly: dict[str, list] = {}
    for d in sorted(dates_needed):
        day = get_day(lat, lon, d)
        if not day:
            continue
        hourly = day.get("hourly", {})
        for var, vals in hourly.items():
            combined_hourly.setdefault(var, []).extend(vals)

    if not combined_hourly.get("temperature_2m"):
        return {"error": "no weather data returned"}

    rows = list(_iter_hours(combined_hourly, start_utc, end_utc))
    if not rows:
        return {"error": "no hours covered the activity window"}

    def avg(key, decimals=1):
        vals = [r[key] for r in rows if r.get(key) is not None]
        if not vals:
            return None
        return round(sum(vals) / len(vals), decimals)

    def maxv(key, decimals=1):
        vals = [r[key] for r in rows if r.get(key) is not None]
        if not vals:
            return None
        return round(max(vals), decimals)

    def total(key, decimals=2):
        vals = [r[key] for r in rows if r.get(key) is not None]
        if not vals:
            return None
        return round(sum(vals), decimals)

    # WMO weather codes → short tag
    code = rows[len(rows) // 2].get("weather_code")  # midpoint
    condition = _wmo_to_tag(code) if code is not None else None

    return {
        "temp_f": avg("temperature_2m"),
        "temp_f_start": rows[0].get("temperature_2m"),
        "temp_f_end": rows[-1].get("temperature_2m"),
        "apparent_f": avg("apparent_temperature"),
        "humidity_pct": avg("relative_humidity_2m", decimals=0),
        "dewpoint_f": avg("dewpoint_2m"),
        "wind_mph": avg("wind_speed_10m"),
        "gust_mph": maxv("wind_gusts_10m"),
        "precipitation_in": total("precipitation"),
        "conditions": condition,
        "hours_covered": len(rows),
        "source": "open-meteo",
    }


def _wmo_to_tag(code: int | float | None) -> str:
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "unknown"
    mapping = {
        0: "clear",
        1: "mainly clear", 2: "partly cloudy", 3: "overcast",
        45: "fog", 48: "rime fog",
        51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain",
        71: "light snow", 73: "snow", 75: "heavy snow",
        77: "snow grains",
        80: "light showers", 81: "showers", 82: "heavy showers",
        85: "light snow showers", 86: "snow showers",
        95: "thunderstorm", 96: "thunderstorm + hail", 99: "severe thunderstorm",
    }
    return mapping.get(c, f"code_{c}")
