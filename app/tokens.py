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
  token refresh  → garth.Client.refresh_oauth2 (patched) writes updated
                   tokens to temp dir AND uploads to R2
"""
from __future__ import annotations

import base64
import io
import logging
import os
import tarfile
import tempfile

import garth

from . import cache

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


# Patch garth.Client.refresh_oauth2 so refreshed tokens get uploaded.
# Every garth.Client instance (including the one inside Garmin) inherits this.
_original_refresh = garth.Client.refresh_oauth2


def _patched_refresh(self):
    result = _original_refresh(self)
    home = getattr(self, "_garth_home", None)
    if home:
        try:
            persist_tokens_dir(str(home))
        except Exception as ex:  # noqa: BLE001
            logger.warning("post-refresh R2 persist failed: %s", ex)
    return result


garth.Client.refresh_oauth2 = _patched_refresh
