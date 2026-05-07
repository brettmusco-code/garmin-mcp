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
                 → client.login(temp_dir) sets garth._garth_home = temp_dir
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
import logging
import os
import sys
import tarfile
import tempfile
import time

import garth

from . import cache

# Skip refresh if OAuth2 token has at least this much time left.
# Garmin OAuth2 tokens live for 3600s (1h); requests take ~1-5s. We want
# to avoid the "token expires mid-request" race while still letting all
# containers in a 1hr window share the same refresh.
REFRESH_SKIP_MARGIN_SEC = 600  # 10 minutes of buffer

logger = logging.getLogger(__name__)

TOKENS_SUBKEY = "auth/garth_tokens.tar.gz"


def _r2_key() -> str:
    return cache.PREFIX + TOKENS_SUBKEY


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

    # Token IS expired. Is this container authorized to refresh? Only the
    # "anchor" run (typically the 3am nightly) should call Garmin's
    # exchange endpoint. Hourly/sub-hourly runs that find an expired
    # token should fail fast and let the anchor run deal with it on its
    # next scheduled execution.
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
            "The next anchor run (daily-refresh) will rotate the token."
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
            global _refresh_circuit_tripped
            _refresh_circuit_tripped = ex
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
