"""S3-compatible cache for Garmin API responses.

Works with AWS S3 or any S3-compatible object store (Cloudflare R2, Backblaze
B2, MinIO) by setting S3_ENDPOINT_URL. Keyed on (tool_name, arg-dict). Cache
is a no-op if S3_CACHE_BUCKET is unset, so local dev and tests keep working
without credentials.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from functools import wraps
from typing import Any, Callable

logger = logging.getLogger(__name__)

BUCKET = os.environ.get("S3_CACHE_BUCKET")
PREFIX = os.environ.get("S3_CACHE_PREFIX", "garmin-mcp/").rstrip("/") + "/"
DEFAULT_TTL_SECONDS = int(os.environ.get("S3_CACHE_TTL_SECONDS", str(24 * 3600)))
ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL")  # set for R2 / B2 / MinIO
REGION = os.environ.get("S3_REGION") or os.environ.get("AWS_DEFAULT_REGION", "auto")

_s3 = None


def _client():
    global _s3
    if _s3 is None:
        import boto3  # noqa: PLC0415
        kwargs: dict[str, Any] = {"region_name": REGION}
        if ENDPOINT_URL:
            kwargs["endpoint_url"] = ENDPOINT_URL
        _s3 = boto3.client("s3", **kwargs)
    return _s3


def enabled() -> bool:
    return bool(BUCKET)


def _key(tool: str, args: dict) -> str:
    payload = json.dumps(args, sort_keys=True, default=str).encode()
    h = hashlib.sha256(payload).hexdigest()[:16]
    return f"{PREFIX}{tool}/{h}.json"


def get(tool: str, args: dict, ttl_seconds: int | None = None, raise_on_error: bool = False) -> Any | None:
    if not enabled():
        return None
    key = _key(tool, args)
    ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_TTL_SECONDS
    try:
        obj = _client().get_object(Bucket=BUCKET, Key=key)
        body = obj["Body"].read()
        payload = json.loads(body)
        age = payload.get("_age_check")
        if age is not None:
            from time import time
            if time() - age > ttl:
                return None
        return payload.get("data")
    except Exception as ex:  # noqa: BLE001
        msg = str(ex).lower()
        if "nosuchkey" in msg or "not found" in msg or "404" in msg:
            return None
        logger.warning("cache get failed: %s", ex)
        if raise_on_error:
            raise
        return None


def list_keys(tool_prefix: str | None = None, limit: int = 100) -> list[str]:
    """List cached object keys under the configured prefix. Paginates."""
    if not enabled():
        return []
    prefix = PREFIX + (tool_prefix.rstrip("/") + "/" if tool_prefix else "")
    keys: list[str] = []
    continuation: str | None = None
    c = _client()
    while len(keys) < limit:
        kwargs: dict[str, Any] = {
            "Bucket": BUCKET,
            "Prefix": prefix,
            "MaxKeys": min(1000, limit - len(keys)),
        }
        if continuation:
            kwargs["ContinuationToken"] = continuation
        resp = c.list_objects_v2(**kwargs)
        keys.extend(obj["Key"] for obj in resp.get("Contents", []))
        if not resp.get("IsTruncated"):
            break
        continuation = resp.get("NextContinuationToken")
        if not continuation:
            break
    return keys


def count_keys(tool_prefix: str | None = None) -> int:
    """Count cached object keys under the configured prefix (no size limit)."""
    if not enabled():
        return 0
    prefix = PREFIX + (tool_prefix.rstrip("/") + "/" if tool_prefix else "")
    total = 0
    continuation: str | None = None
    c = _client()
    while True:
        kwargs: dict[str, Any] = {"Bucket": BUCKET, "Prefix": prefix, "MaxKeys": 1000}
        if continuation:
            kwargs["ContinuationToken"] = continuation
        resp = c.list_objects_v2(**kwargs)
        total += resp.get("KeyCount", 0)
        if not resp.get("IsTruncated"):
            break
        continuation = resp.get("NextContinuationToken")
        if not continuation:
            break
    return total


def put(tool: str, args: dict, data: Any, raise_on_error: bool = False) -> None:
    if not enabled():
        return
    key = _key(tool, args)
    from time import time
    body = json.dumps({"_age_check": time(), "data": data}, default=str).encode()
    try:
        _client().put_object(Bucket=BUCKET, Key=key, Body=body, ContentType="application/json")
    except Exception as ex:  # noqa: BLE001
        logger.warning("cache put failed: %s", ex)
        if raise_on_error:
            raise


def cached(tool: str, ttl_seconds: int | None = None) -> Callable:
    """Decorator: cache a function's return value keyed on its kwargs.

    The wrapped function must be called with only kwargs (or must accept
    force_refresh=False). Pass `force_refresh=True` to skip the cache.
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            force = kwargs.pop("force_refresh", False)
            # Build a stable arg signature.
            sig_args = {"args": args, "kwargs": kwargs}
            if not force:
                hit = get(tool, sig_args, ttl_seconds=ttl_seconds)
                if hit is not None:
                    return hit
            data = fn(*args, **kwargs)
            put(tool, sig_args, data)
            return data
        return wrapper
    return decorator
