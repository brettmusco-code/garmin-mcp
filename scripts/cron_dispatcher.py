"""Single cron entry point for all Garmin refresh jobs.

Render cron services can only carry one schedule expression each, but
billing has a $1/mo per-service minimum floor. Running three separate
crons is ~$3/mo; routing all of them through one hourly cron and
dispatching internally is ~$1/mo.

Schedule (set in render.yaml): hourly at :30.

What runs at each tick (UTC):
  03:30  daily_refresh — full nightly anchor (close enough to the old
                        03:17 cadence)
  13:30  today_refresh [morning] — sleep/HRV/RHR/readiness after device
                        syncs (configurable via MORNING_REFRESH_HOUR_UTC,
                        default 13 = 7 AM MDT / 9 AM EDT)
  00:30  today_refresh [live]
  06:30  today_refresh [live]
  12:30  today_refresh [live]
  18:30  today_refresh [live]
  every  today_refresh [workout] — checks for newly-synced activities

Daily and live can run in the same tick: when they overlap (e.g. both
hit at 06:30 every day if we ever scheduled it that way) we still run
each in turn. Workout-check runs on top.

Cron at :30 (vs :00) keeps us off the top-of-hour spike that other
services hit.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

logging.getLogger("garminconnect").setLevel(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_script(name: str):
    """Load a sibling script file as a fresh module.

    Each refresh script reads config from os.environ at import time
    (e.g. today_refresh.MODE), so we have to re-import per tick to
    pick up the env we just set. spec_from_file_location is cleaner
    than making scripts/ a package just for this.
    """
    import importlib.util
    path = os.path.join(_SCRIPTS_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_module(name: str, env: dict) -> int:
    """Run a refresh script's main() with overridden env vars."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: v for k, v in env.items() if v is not None})
    try:
        mod = _load_script(name)
        return mod.main()
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


def main() -> int:
    now = datetime.now(timezone.utc)
    hour = now.hour
    print(f"=== cron_dispatcher {now.isoformat()} hour={hour:02d} ===")

    overall = 0

    # Daily anchor — once a day at 03:30 UTC.
    if hour == 3:
        print("\n[dispatcher] tick: daily_refresh (nightly anchor)")
        rc = _run_module("daily_refresh", {})
        if rc != 0:
            overall = rc

    # Morning overnight-data refresh — once a day after the device syncs.
    # Default 13 UTC = 7 AM MDT / 9 AM EDT. Override via MORNING_REFRESH_HOUR_UTC.
    morning_hour = int(os.environ.get("MORNING_REFRESH_HOUR_UTC", "13"))
    if hour == morning_hour:
        print("\n[dispatcher] tick: today_refresh [morning]")
        rc = _run_module("today_refresh", {"TODAY_REFRESH_MODE": "morning"})
        if rc != 0:
            overall = rc

    # Live intraday refresh — every 6h at :30 (00, 06, 12, 18 UTC).
    if hour % 6 == 0:
        print("\n[dispatcher] tick: today_refresh [live]")
        rc = _run_module("today_refresh", {"TODAY_REFRESH_MODE": "live"})
        if rc != 0:
            overall = rc

    # Workout sync check — every hour.
    print("\n[dispatcher] tick: today_refresh [workout]")
    rc = _run_module("today_refresh", {"TODAY_REFRESH_MODE": "workout"})
    if rc != 0:
        overall = rc

    return overall


if __name__ == "__main__":
    sys.exit(main())
