"""Delete cached Garmin auth state from R2 so the next run falls back to
GARTH_TOKENS_B64 from the environment. Run this after bootstrapping a
fresh token to force GitHub Actions / Render to pick up the new one.

Reads the same env vars the production code uses:
  S3_CACHE_BUCKET, S3_ENDPOINT_URL, S3_CACHE_PREFIX,
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY

Usage:
  # export R2 creds first (same values as GitHub Actions secrets)
  export S3_CACHE_BUCKET=...
  export S3_ENDPOINT_URL=...
  export S3_CACHE_PREFIX=garmin-mcp/
  export AWS_ACCESS_KEY_ID=...
  export AWS_SECRET_ACCESS_KEY=...
  python scripts/delete_r2_token.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import cache, tokens  # noqa: E402


def _delete_key(client, key: str) -> bool:
    print(f"deleting s3://{cache.BUCKET}/{key}")
    try:
        client.head_object(Bucket=cache.BUCKET, Key=key)
    except Exception as ex:  # noqa: BLE001
        msg = str(ex).lower()
        if "not found" in msg or "404" in msg or "nosuchkey" in msg:
            print("  object does not exist — nothing to do")
            return True
        print(f"  head_object failed: {ex}", file=sys.stderr)
        return False

    client.delete_object(Bucket=cache.BUCKET, Key=key)
    print("  deleted")
    return True


def main() -> int:
    if not cache.enabled():
        print("ERROR: R2 cache not configured. Export S3_CACHE_BUCKET, "
              "S3_ENDPOINT_URL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY.",
              file=sys.stderr)
        return 1

    client = cache._client()  # noqa: SLF001
    ok = True
    ok = _delete_key(client, tokens._r2_key()) and ok  # noqa: SLF001
    if hasattr(tokens, "_cooldown_key"):
        ok = _delete_key(client, tokens._cooldown_key()) and ok  # noqa: SLF001
    if hasattr(tokens, "_api_cooldown_key"):
        ok = _delete_key(client, tokens._api_cooldown_key()) and ok  # noqa: SLF001

    if ok:
        print("Done. Next run will load from GARTH_TOKENS_B64 env var "
              "and both 429 cooldowns are cleared.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
