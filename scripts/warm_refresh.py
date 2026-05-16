"""Weekly forced OAuth2 refresh.

The patched garth refresh in app/tokens.py skips Garmin's exchange endpoint
when the access_token still has >REFRESH_SKIP_MARGIN_SEC of validity. That's
correct for normal traffic, but it means the refresh_token (30-day lifetime)
can quietly age out without ever being rotated — exactly the failure mode
that locked us out for 16 days in May 2026.

This script forces a real exchange call once a week, regardless of access
token freshness. A successful exchange always issues a new refresh_token,
which keeps the chain rolling forward indefinitely. A failed exchange fails
the workflow loudly so the issue is visible immediately.

Required env: GARTH_TOKENS_B64, S3_CACHE_BUCKET, S3_ENDPOINT_URL,
              AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, ALLOW_OAUTH_REFRESH=true
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date

logging.getLogger("garminconnect").setLevel(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import cache, garmin, tokens  # noqa: E402


def main() -> int:
    if garmin.READONLY_MODE:
        print("ERROR: GARMIN_READONLY set. Unset it for warm-refresh.", file=sys.stderr)
        return 1
    if not cache.enabled():
        print("ERROR: R2 cache not configured.", file=sys.stderr)
        return 1

    print(f"=== warm_refresh {date.today().isoformat()} ===")

    api_remaining, api_reason = tokens.load_api_429_cooldown_remaining()
    if api_remaining > 0:
        print(
            f"ERROR: Garmin API 429 cooldown active ({api_remaining}s). "
            f"Reason: {api_reason or 'unknown'}.",
            file=sys.stderr,
        )
        return 1

    oauth_remaining, oauth_reason = tokens.load_cooldown_remaining()
    if oauth_remaining > 0:
        print(
            f"ERROR: OAuth exchange cooldown active ({oauth_remaining}s). "
            f"Reason: {oauth_reason or 'unknown'}.",
            file=sys.stderr,
        )
        return 1

    client = garmin.get_client()

    before = tokens.refresh_token_remaining_seconds(client)
    print(f"refresh_token before: {before/86400:.1f} days remaining"
          if before is not None else "refresh_token before: unknown")

    try:
        tokens.force_refresh(client)
    except Exception as ex:  # noqa: BLE001
        print(f"FATAL: forced refresh failed: {type(ex).__name__}: {ex}",
              file=sys.stderr)
        return 1

    after = tokens.refresh_token_remaining_seconds(client)
    print(f"refresh_token after:  {after/86400:.1f} days remaining"
          if after is not None else "refresh_token after: unknown")

    if before is not None and after is not None and after <= before:
        # Garmin sometimes returns the same refresh_token on exchange.
        # Not necessarily fatal, but worth flagging — if this persists
        # the refresh_token will eventually expire anyway.
        print(
            "  WARN: refresh_token did not roll forward. Garmin may have "
            "reused the existing token. Watch for repeated occurrences.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
