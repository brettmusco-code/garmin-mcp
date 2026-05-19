"""Persist garminconnect OAuth tokens to R2 so they survive container restarts.

Storage:
  R2 key:  {cache.PREFIX}auth/garmin_tokens.json
  Format:  JSON: {"di_token": "...", "di_refresh_token": "...", "di_client_id": "..."}
  Env var: GARMIN_TOKENS_B64 = base64-encoded JSON string (from bootstrap.py)

Flow:
  startup     → load R2 JSON (or fall back to GARMIN_TOKENS_B64 env)
              → client.client.loads(json_str) — no network call
              → if from env (first deploy), push to R2 so next container uses R2

  api call    → garminconnect auto-checks _token_expires_soon() before each request
              → if near expiry (< 15 min), calls our patched _refresh_session which:
                  1. checks circuit breaker + cooldown flags
                  2. checks R2 for a fresher token from another process
                  3. calls _refresh_di_token() if truly expired
                  4. persists fresh token to R2 on success

  keeper      → web-service background thread checks every 5 min
              → proactive_refresh_if_needed() keeps R2 always warm
                (acts at KEEPER_MARGIN_SEC = 30 min before expiry, well before
                the built-in 15-min _token_expires_soon trigger)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
from threading import Lock

from garminconnect import client as _gc_client
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from . import cache

# Keeper trigger: proactively refresh when token has less than this many
# seconds remaining (must be > _token_expires_soon threshold of 900s).
REFRESH_SKIP_MARGIN_SEC = int(os.environ.get("REFRESH_SKIP_MARGIN_SEC", "1200"))

# How long to wait after a 429 on the DI refresh endpoint before retrying.
# 2h is appropriate for Render's non-flagged IPs.
OAUTH_429_COOLDOWN_SEC = int(os.environ.get("OAUTH_429_COOLDOWN_SEC", str(2 * 3600)))

# Separate cooldown for regular Garmin API calls (activities, daily summaries…).
API_429_COOLDOWN_SEC = int(os.environ.get("GARMIN_API_429_COOLDOWN_SEC", str(48 * 3600)))

logger = logging.getLogger(__name__)

TOKENS_SUBKEY = "auth/garmin_tokens.json"
COOLDOWN_SUBKEY = "auth/oauth_refresh_cooldown.json"
API_COOLDOWN_SUBKEY = "auth/api_429_cooldown.json"


def _r2_key() -> str:
    return cache.PREFIX + TOKENS_SUBKEY


def _cooldown_key() -> str:
    return cache.PREFIX + COOLDOWN_SUBKEY


def _api_cooldown_key() -> str:
    return cache.PREFIX + API_COOLDOWN_SUBKEY


def _load_from_r2() -> bytes | None:
    if not cache.enabled():
        return None
    try:
        obj = cache._client().get_object(Bucket=cache.BUCKET, Key=_r2_key())  # noqa: SLF001
        return obj["Body"].read()
    except Exception as ex:  # noqa: BLE001
        msg = str(ex).lower()
        if "nosuchkey" in msg or "not found" in msg or "404" in msg:
            return None
        logger.warning("garmin token load from R2 failed: %s", ex)
        return None


def _save_to_r2(data: bytes) -> None:
    if not cache.enabled():
        return
    try:
        cache._client().put_object(  # noqa: SLF001
            Bucket=cache.BUCKET,
            Key=_r2_key(),
            Body=data,
            ContentType="application/json",
        )
        logger.info("persisted garmin tokens to R2 (%d bytes)", len(data))
    except Exception as ex:  # noqa: BLE001
        logger.warning("garmin token save to R2 failed: %s", ex)


def load_tokens_json() -> tuple[str, str]:
    """Return (json_str, source) where source is 'r2' or 'env'."""
    data = _load_from_r2()
    if data is not None:
        logger.info("loaded garmin tokens from R2")
        return data.decode(), "r2"
    b64 = os.environ.get("GARMIN_TOKENS_B64")
    if not b64:
        raise RuntimeError(
            "No garmin tokens in R2 and GARMIN_TOKENS_B64 is not set. "
            "Run scripts/bootstrap.py locally to mint initial tokens."
        )
    data = base64.b64decode(b64)
    logger.info("loaded garmin tokens from GARMIN_TOKENS_B64 env var (bootstrap)")
    return data.decode(), "env"


def save_tokens_json(json_str: str) -> None:
    """Push token JSON string to R2."""
    _save_to_r2(json_str.encode())


def load_cooldown_remaining() -> tuple[int, str | None]:
    """Return active R2 OAuth exchange cooldown as (seconds_remaining, reason)."""
    if not cache.enabled():
        return 0, None
    try:
        obj = cache._client().get_object(Bucket=cache.BUCKET, Key=_cooldown_key())  # noqa: SLF001
        payload = json.loads(obj["Body"].read())
    except Exception as ex:  # noqa: BLE001
        msg = str(ex).lower()
        if "nosuchkey" not in msg and "not found" not in msg and "404" not in msg:
            logger.warning("oauth cooldown load from R2 failed: %s", ex)
        return 0, None

    try:
        until = float(payload.get("until", 0))
    except (TypeError, ValueError):
        return 0, None
    remaining = int(until - time.time())
    if remaining <= 0:
        return 0, None
    return remaining, payload.get("reason")


def _save_cooldown(ex: BaseException) -> None:
    """Persist an OAuth exchange cooldown so later processes fail fast."""
    if not cache.enabled() or OAUTH_429_COOLDOWN_SEC <= 0:
        return
    now = time.time()
    payload = {
        "created_at": now,
        "until": now + OAUTH_429_COOLDOWN_SEC,
        "reason": str(ex)[:500],
    }
    try:
        cache._client().put_object(  # noqa: SLF001
            Bucket=cache.BUCKET,
            Key=_cooldown_key(),
            Body=json.dumps(payload).encode(),
            ContentType="application/json",
        )
        print(
            "[tokens] OAuth exchange cooldown saved to R2 for "
            f"{OAUTH_429_COOLDOWN_SEC}s",
            file=sys.stderr,
            flush=True,
        )
    except Exception as save_ex:  # noqa: BLE001
        logger.warning("oauth cooldown save to R2 failed: %s", save_ex)


def load_api_429_cooldown_remaining() -> tuple[int, str | None]:
    """Return active R2 API-call 429 cooldown as (seconds_remaining, reason)."""
    if not cache.enabled():
        return 0, None
    try:
        obj = cache._client().get_object(Bucket=cache.BUCKET, Key=_api_cooldown_key())  # noqa: SLF001
        payload = json.loads(obj["Body"].read())
    except Exception as ex:  # noqa: BLE001
        msg = str(ex).lower()
        if "nosuchkey" not in msg and "not found" not in msg and "404" not in msg:
            logger.warning("api 429 cooldown load from R2 failed: %s", ex)
        return 0, None

    try:
        until = float(payload.get("until", 0))
    except (TypeError, ValueError):
        return 0, None
    remaining = int(until - time.time())
    if remaining <= 0:
        return 0, None
    return remaining, payload.get("reason")


def save_api_429_cooldown(ex: BaseException) -> None:
    """Persist an API-call 429 cooldown to R2 so later processes fail fast."""
    if not cache.enabled() or API_429_COOLDOWN_SEC <= 0:
        return
    now = time.time()
    payload = {
        "created_at": now,
        "until": now + API_429_COOLDOWN_SEC,
        "reason": str(ex)[:500],
    }
    try:
        cache._client().put_object(  # noqa: SLF001
            Bucket=cache.BUCKET,
            Key=_api_cooldown_key(),
            Body=json.dumps(payload).encode(),
            ContentType="application/json",
        )
        print(
            "[tokens] Garmin API 429 cooldown saved to R2 for "
            f"{API_429_COOLDOWN_SEC}s — refresh jobs will abort at startup "
            "until it expires.",
            file=sys.stderr,
            flush=True,
        )
    except Exception as save_ex:  # noqa: BLE001
        logger.warning("api 429 cooldown save to R2 failed: %s", save_ex)


def _decode_jwt_exp(token: str) -> int | None:
    """Decode a JWT and return the exp claim as epoch seconds, or None."""
    try:
        parts = str(token).split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
        exp = payload.get("exp")
        return int(exp) if exp else None
    except Exception:  # noqa: BLE001
        return None


def di_token_remaining_seconds(client) -> int | None:
    """Seconds until the DI Bearer token (access token) expires, or None."""
    gc = getattr(client, "client", client)
    token = getattr(gc, "di_token", None)
    if not token:
        return None
    exp = _decode_jwt_exp(token)
    return int(exp - time.time()) if exp else None


def di_refresh_token_remaining_seconds(client) -> int | None:
    """Seconds until the DI refresh token expires, or None if opaque/unknown."""
    gc = getattr(client, "client", client)
    token = getattr(gc, "di_refresh_token", None)
    if not token:
        return None
    exp = _decode_jwt_exp(token)
    return int(exp - time.time()) if exp else None


def refresh_token_remaining_seconds(client) -> int | None:
    """Back-compat alias: returns di_refresh_token expiry (None if opaque)."""
    return di_refresh_token_remaining_seconds(client)


def mfa_token_remaining_seconds(client) -> int | None:
    """Not applicable for garminconnect 0.3.x (no OAuth1/MFA token layer)."""
    return None


# ---------- garminconnect.client.Client patches ----------
#
# Patch 1: _refresh_di_token — surface 429 as TooManyRequestsError so callers
#   can distinguish it from other auth failures.
# Patch 2: _refresh_session — add circuit breaker, cooldown check,
#   ALLOW_OAUTH_REFRESH guard, R2 freshness check, and R2 persist on success.

_original_refresh_di_token = _gc_client.Client._refresh_di_token

_refresh_circuit_tripped: Exception | None = None
_refresh_lock = Lock()


def _is_rate_limit(ex: BaseException) -> bool:
    s = str(ex).lower()
    return "429" in s or "too many requests" in s


def _patched_refresh_di_token(self) -> None:
    """Wrap original to convert 429 AuthenticationErrors to TooManyRequestsError."""
    try:
        _original_refresh_di_token(self)
    except GarminConnectAuthenticationError as ex:
        if _is_rate_limit(ex):
            raise GarminConnectTooManyRequestsError(
                f"DI token refresh 429 rate limited: {ex}"
            ) from ex
        raise


_gc_client.Client._refresh_di_token = _patched_refresh_di_token

_original_refresh_session = _gc_client.Client._refresh_session


def _patched_refresh_session(self) -> None:
    """Wrap _refresh_session with circuit breaker, cooldown, R2 checks, and persist."""
    global _refresh_circuit_tripped

    if _refresh_circuit_tripped is not None:
        raise RuntimeError(
            f"Garmin OAuth 429 circuit tripped: {_refresh_circuit_tripped}"
        )

    cooldown_remaining, reason = load_cooldown_remaining()
    if cooldown_remaining > 0:
        raise RuntimeError(
            f"Garmin OAuth 429 cooldown active ({cooldown_remaining}s). "
            f"Last error: {reason or 'unknown'}"
        )

    allow = os.environ.get("ALLOW_OAUTH_REFRESH", "true").lower() in ("1", "true", "yes")
    if not allow:
        raise RuntimeError(
            "OAuth refresh not authorized on this instance (ALLOW_OAUTH_REFRESH=false). "
            "The scheduled refresh job will handle token rotation."
        )

    with _refresh_lock:
        if _refresh_circuit_tripped is not None:
            raise RuntimeError(
                f"Garmin OAuth 429 circuit tripped: {_refresh_circuit_tripped}"
            )

        # Re-check: another process may have already refreshed and written to R2.
        fresh = _load_from_r2()
        if fresh:
            try:
                temp = _gc_client.Client()
                temp.loads(fresh.decode())
                if not temp._token_expires_soon():
                    self.di_token = temp.di_token
                    self.di_refresh_token = temp.di_refresh_token
                    self.di_client_id = temp.di_client_id
                    print(
                        "[tokens] loaded fresher DI token from R2 — skipping exchange",
                        flush=True,
                    )
                    return
            except Exception as ex:  # noqa: BLE001
                logger.debug("R2 fresher-token check failed: %s", ex)

        # No DI token: fall back to original JWT_WEB refresh (no R2 persist needed)
        if not self.di_token:
            _original_refresh_session(self)
            return

        print("[tokens] DI token near expiry — calling refresh endpoint", flush=True)
        try:
            self._refresh_di_token()
        except GarminConnectTooManyRequestsError as ex:
            _refresh_circuit_tripped = ex
            _save_cooldown(ex)
            print(
                "[tokens] circuit breaker TRIPPED — 429 on DI refresh endpoint",
                file=sys.stderr,
                flush=True,
            )
            raise RuntimeError(f"Garmin OAuth 429: {ex}") from ex
        except Exception as ex:  # noqa: BLE001
            if _is_rate_limit(ex):
                _refresh_circuit_tripped = ex
                _save_cooldown(ex)
                print(
                    f"[tokens] circuit breaker TRIPPED — rate limit: {ex}",
                    file=sys.stderr,
                    flush=True,
                )
                raise RuntimeError(f"Garmin OAuth 429: {ex}") from ex
            print(f"[tokens] refresh failed (non-429): {ex}", file=sys.stderr, flush=True)
            return

        print("[tokens] refresh succeeded", flush=True)

        if self._tokenstore_path:
            import contextlib
            with contextlib.suppress(Exception):
                self.dump(self._tokenstore_path)

        try:
            _save_to_r2(self.dumps().encode())
            print("[tokens] refreshed DI token persisted to R2", flush=True)
        except Exception as ex:  # noqa: BLE001
            print(f"[tokens] R2 persist FAILED: {ex}", file=sys.stderr, flush=True)


_gc_client.Client._refresh_session = _patched_refresh_session


# ---------- background keeper ----------

KEEPER_MARGIN_SEC = REFRESH_SKIP_MARGIN_SEC + 600  # default 30 min


def proactive_refresh_if_needed() -> str:
    """Check R2 token expiry and refresh proactively if near expiry.

    Designed for the web-service background keeper. Runs independently of
    READONLY_MODE and ALLOW_OAUTH_REFRESH — those are request-path guards;
    this is a scheduled maintenance operation. Returns a short status string.
    """
    if not cache.enabled():
        return "skip: cache not configured"

    cooldown_remaining, _ = load_cooldown_remaining()
    if cooldown_remaining > 0:
        return f"skip: oauth cooldown {cooldown_remaining}s"

    global _refresh_circuit_tripped
    if _refresh_circuit_tripped is not None:
        return "skip: circuit tripped"

    data = _load_from_r2()
    if data is None:
        return "skip: no token in R2"

    gc = _gc_client.Client()
    try:
        gc.loads(data.decode())
    except Exception as ex:  # noqa: BLE001
        return f"error: load failed: {ex}"

    token = gc.di_token
    if not token:
        return "skip: no di_token in R2"

    exp = _decode_jwt_exp(token)
    if exp is None:
        return "skip: token has no exp claim"

    remaining = int(exp - time.time())
    if remaining > KEEPER_MARGIN_SEC:
        return f"ok: {remaining}s remaining"

    print(
        f"[token-keeper] proactive refresh ({remaining}s remaining, "
        f"threshold {KEEPER_MARGIN_SEC}s)",
        flush=True,
    )

    with _refresh_lock:
        if _refresh_circuit_tripped is not None:
            return "skip: circuit tripped (inside lock)"
        try:
            gc._refresh_di_token()
        except GarminConnectTooManyRequestsError as ex:
            _refresh_circuit_tripped = ex
            _save_cooldown(ex)
            print("[token-keeper] circuit tripped by 429", file=sys.stderr, flush=True)
            return f"error: 429 rate limited"
        except Exception as ex:  # noqa: BLE001
            if _is_rate_limit(ex):
                _refresh_circuit_tripped = ex
                _save_cooldown(ex)
            return f"error: {ex}"

    print("[token-keeper] refresh succeeded — persisting to R2", flush=True)
    try:
        _save_to_r2(gc.dumps().encode())
        print("[token-keeper] fresh token persisted to R2", flush=True)
        return "refreshed"
    except Exception as ex:  # noqa: BLE001
        print(f"[token-keeper] R2 persist failed: {ex}", file=sys.stderr, flush=True)
        return "refreshed (R2 persist failed)"
