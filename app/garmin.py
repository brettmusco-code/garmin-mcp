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
from datetime import date, datetime
from threading import Lock
from typing import Optional

from garminconnect import Garmin

_client: Optional[Garmin] = None
_lock = Lock()


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
