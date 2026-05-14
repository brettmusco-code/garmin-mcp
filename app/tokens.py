"""Persist garth OAuth tokens to R2 so they survive Render container restarts.

Without this, every Render cold start loses the refreshed OAuth2 token
(held in a tempfile that /tmp wipes on restart) and the next Garmin call
triggers a fresh oauth-service/oauth/exchange — which Garmin aggressively
rate-limits with 429s that can lock us out for hours.

Storage:
  R2 key: {cache.PREFIX}auth/garth_tokens.tar.gz
  Format: gzipped tar of the garth tokens directory

Flow:
  startup        → load R2 (or fall back to GARTH_TOKENS_B64 env)
                 → extract to temp dir
                 → client.garth.load(temp_dir) + set garth._garth_home
                 → if loaded from env (first deploy), also push to R2
  api call       → garth checks if oauth2_token.expired; if fresh
                   (remaining validity > REFRESH_SKIP_MARGIN_SEC), SKIP
                   the refresh entirely. This is the key rate-limit
                   mitigation — most container starts find a still-valid
                   token in R2 and bypass the OAuth exchange endpoint.
  token refresh  → only when necessary: garth.Client.refresh_oauth2
                   (patched) writes updated tokens to disk AND uploads
                   to R2 so the next container sees the fresh token.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import sys
import tarfile
import tempfile
import time
from threading import Lock

import garth

from . import cache

# Skip refresh if OAuth2 token has at least this much time left.
# Garmin OAuth2 tokens live for 3600s (1h); requests take ~1-5s. We want
# to avoid the "token expires mid-request" race while still letting all
# containers in a 1hr window share the same refresh.
REFRESH_SKIP_MARGIN_SEC = 600  # 10 minutes of buffer
# Garmin's OAuth exchange throttle can last much longer than a normal API
# backoff window. 48h (vs. prior 24h) gives Garmin's throttle window time
# to fully reset before we attempt another exchange. Set
# OAUTH_429_COOLDOWN_SEC=0 to disable (not recommended).
OAUTH_429_COOLDOWN_SEC = int(os.environ.get("OAUTH_429_COOLDOWN_SEC", str(48 * 3600)))
# Separate cooldown for regular Garmin API calls (activities, daily
# summaries, etc.). When any API call exhausts its retries on a 429,
# this flag is persisted to R2 so subsequent refresh runs abort before
# making a single Garmin call — instead of re-hammering and extending
# Garmin's throttle window further.
API_429_COOLDOWN_SEC = int(os.environ.get("GARMIN_API_429_COOLDOWN_SEC", str(48 * 3600)))

logger = logging.getLogger(__name__)

TOKENS_SUBKEY = "auth/garth_tokens.tar.gz"
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
        logger.warning("garth token load from R2 failed: %s", ex)
        return None


def _save_to_r2(data: bytes) -> None:
    if not cache.enabled():
        return
    try:
        cache._client().put_object(  # noqa: SLF001
            Bucket=cache.BUCKET,
            Key=_r2_key(),
            Body=data,
            ContentType="application/gzip",
        )
        logger.info("persisted garth tokens to R2 (%d bytes)", len(data))
    except Exception as ex:  # noqa: BLE001
        logger.warning("garth token save to R2 failed: %s", ex)


def _extract(tarball: bytes) -> str:
    tmp = tempfile.mkdtemp(prefix="garth-")
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
        tf.extractall(tmp)  # noqa: S202
    return tmp


def _tar_dir(directory: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for root, _, files in os.walk(directory):
            for name in files:
                full = os.path.join(root, name)
                arcname = os.path.relpath(full, directory)
                tf.add(full, arcname=arcname)
    return buf.getvalue()


def load_tokens_dir() -> tuple[str, str]:
    """Return (temp_dir_path, source) where source is 'r2' or 'env'."""
    data = _load_from_r2()
    if data is not None:
        logger.info("loaded garth tokens from R2")
        return _extract(data), "r2"
    b64 = os.environ.get("GARTH_TOKENS_B64")
    if not b64:
        raise RuntimeError(
            "No garth tokens in R2 and GARTH_TOKENS_B64 is not set. "
            "Run scripts/bootstrap.py locally to mint initial tokens."
        )
    data = base64.b64decode(b64)
    logger.info("loaded garth tokens from GARTH_TOKENS_B64 env var (bootstrap)")
    return _extract(data), "env"


def persist_tokens_dir(directory: str) -> None:
    """Tar a tokens directory and push to R2."""
    _save_to_r2(_tar_dir(directory))


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
    """Return active R2 API-call 429 cooldown as (seconds_remaining, reason).

    This is separate from the OAuth exchange cooldown. It trips when any
    regular Garmin API call (activities, daily summaries, etc.) exhausts its
    retries on a 429 and persists across process boundaries so subsequent
    refresh runs abort before making a single call to Garmin.
    """
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
    """Persist an API-call 429 cooldown to R2 so later processes fail fast.

    Called by garmin._call_with_backoff when a Garmin API call exhausts its
    retry budget on a 429. The flag remains active for API_429_COOLDOWN_SEC
    (default 48 h) so nightly/hourly refresh jobs abort at startup without
    hammering Garmin further.
    """
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


def _try_load_fresh_r2_token(client) -> tuple[bool, int]:
    """Reload tokens from R2 in case another process refreshed first."""
    data = _load_from_r2()
    if data is None:
        return False, 0
    tokens_dir = _extract(data)
    try:
        client.load(tokens_dir)
        client._garth_home = tokens_dir
    except Exception as ex:  # noqa: BLE001
        logger.warning("reload of garth tokens from R2 failed: %s", ex)
        return False, 0
    return _token_still_valid(getattr(client, "oauth2_token", None))


# Patch garth.Client.refresh_oauth2 so:
#   1. We SKIP calling Garmin's exchange endpoint if the existing OAuth2
#      token still has >REFRESH_SKIP_MARGIN_SEC of validity remaining.
#      This is the main rate-limit mitigation — multiple containers
#      within the same ~50 min window reuse the same token instead of
#      each triggering an exchange call.
#   2. When we DO refresh (token actually expired), persist the new
#      token to R2 immediately so the NEXT container starts with the
#      fresh token and won't need its own refresh.
_original_refresh = garth.Client.refresh_oauth2

# Process-level circuit breaker. Once Garmin 429s the OAuth exchange
# endpoint, every subsequent Garmin API call in this run will trigger
# another refresh attempt (garth calls ensure_oauth2 before each request).
# Without this breaker, one 429 cascades into hundreds of hits, which
# makes the rate limit lockout worse. Once tripped, stays tripped for
# the life of the process — the next scheduled run starts fresh.
_refresh_circuit_tripped: Exception | None = None
_refresh_lock = Lock()


def _is_rate_limit(ex: BaseException) -> bool:
    s = str(ex).lower()
    return "429" in s or "too many requests" in s


def _token_still_valid(token) -> tuple[bool, int]:
    """Return (is_valid, seconds_until_expiry). Missing/invalid token
    returns (False, 0)."""
    if token is None:
        return False, 0
    expires_at = getattr(token, "expires_at", None)
    if expires_at is None:
        return False, 0
    remaining = int(expires_at - time.time())
    return remaining > REFRESH_SKIP_MARGIN_SEC, remaining


def _patched_refresh(self):
    global _refresh_circuit_tripped
    # Short-circuit: if the current OAuth2 token is still fresh enough,
    # don't contact Garmin's exchange endpoint at all. This is what
    # keeps us under Garmin's aggressive per-account rate limit.
    valid, remaining = _token_still_valid(getattr(self, "oauth2_token", None))
    if valid:
        logger.info(
            "oauth2 token still valid (%ds remaining) — skipping refresh",
            remaining,
        )
        return self.oauth2_token

    # Circuit breaker: if an earlier refresh in this process already got
    # 429'd, don't keep hammering the exchange endpoint. Re-raise the
    # cached exception so callers see the same error without making
    # Garmin's lockout worse.
    if _refresh_circuit_tripped is not None:
        raise _refresh_circuit_tripped

    # The first Garmin calls in a refresh job fan out across worker
    # threads. If the shared token is expired, more than one thread can
    # enter garth's refresh path at once. Serialize the actual exchange
    # and re-check after acquiring the lock so one expired token produces
    # at most one Garmin OAuth exchange in this process.
    with _refresh_lock:
        valid, remaining = _token_still_valid(getattr(self, "oauth2_token", None))
        if valid:
            logger.info(
                "oauth2 token refreshed by another thread (%ds remaining) — "
                "skipping refresh",
                remaining,
            )
            return self.oauth2_token

        if _refresh_circuit_tripped is not None:
            raise _refresh_circuit_tripped

        valid, remaining = _try_load_fresh_r2_token(self)
        if valid:
            print(
                "[tokens] loaded fresher OAuth2 token from R2 "
                f"({remaining}s remaining) — skipping exchange",
                flush=True,
            )
            return self.oauth2_token

        cooldown_remaining, reason = load_cooldown_remaining()
        if cooldown_remaining > 0:
            raise RuntimeError(
                "Garmin OAuth exchange in cooldown for "
                f"{cooldown_remaining}s after recent 429/rate limit. "
                f"Last error: {reason or 'unknown'}"
            )

        # Token IS expired. Is this container authorized to refresh? Refresh
        # jobs may opt in; the readonly web service and other cache-only
        # paths fail fast so user traffic cannot amplify rate-limit pressure.
        allow = os.environ.get("ALLOW_OAUTH_REFRESH", "true").lower() in ("1", "true", "yes")
        if not allow:
            logger.info(
                "oauth2 token expired, but ALLOW_OAUTH_REFRESH is false — "
                "skipping refresh (anchor run will handle it)"
            )
            raise RuntimeError(
                "OAuth2 token expired and this run is not authorized to "
                "refresh (ALLOW_OAUTH_REFRESH=false). This prevents multiple "
                "concurrent refreshes from compounding rate-limit pressure. "
                "The next refresh-authorized run will rotate the token."
            )

        # Otherwise proceed with the real refresh. Print (not just log) so
        # the refresh lifecycle shows up in GitHub Actions output.
        print("[tokens] oauth2 expired — calling Garmin exchange endpoint",
              flush=True)
        try:
            result = _original_refresh(self)
        except Exception as ex:  # noqa: BLE001
            print(f"[tokens] REFRESH FAILED: {type(ex).__name__}: {ex}",
                  file=sys.stderr, flush=True)
            if _is_rate_limit(ex):
                _refresh_circuit_tripped = ex
                _save_cooldown(ex)
                print("[tokens] circuit breaker TRIPPED — further refresh "
                      "attempts in this run will fail fast without contacting "
                      "Garmin.", file=sys.stderr, flush=True)
            raise
        print("[tokens] refresh succeeded — new token expires at "
              f"{getattr(self.oauth2_token, 'expires_at', '?')}", flush=True)

        # Persist the freshly-refreshed token to R2 so other containers can
        # reuse it within the 1-hour window.
        home = getattr(self, "_garth_home", None)
        if not home:
            print("[tokens] WARNING: _garth_home not set — token refresh will "
                  "NOT persist to R2. Next container will hit expired cached "
                  "token.", file=sys.stderr, flush=True)
        else:
            try:
                persist_tokens_dir(str(home))
                print(f"[tokens] persisted refreshed token to R2", flush=True)
            except Exception as ex:  # noqa: BLE001
                print(f"[tokens] R2 persist FAILED: {ex}", file=sys.stderr,
                      flush=True)
        return result


garth.Client.refresh_oauth2 = _patched_refresh
