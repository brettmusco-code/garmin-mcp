"""One-time Garmin login with MFA. Run locally, copy the printed token blob
into your environment (as GARMIN_TOKENS_B64) for the deployed server.

Usage:
    # interactive (you type the MFA code)
    python scripts/bootstrap.py

    # write the base64 blob to a file (for `gh secret set ... < file`)
    python scripts/bootstrap.py --out /tmp/garmin.b64

    # provide credentials via env (still need MFA interactively)
    GARMIN_EMAIL=... GARMIN_PASSWORD=... python scripts/bootstrap.py

You'll be prompted for email, password, and the 6-digit MFA code Garmin emails
to you. On success, prints (or writes) a base64-encoded JSON token blob that
can be set as GARMIN_TOKENS_B64.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import os
import sys
from pathlib import Path

from garminconnect import Garmin, GarminConnectAuthenticationError


def _prompt_mfa() -> str:
    return input("MFA code from email: ").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", help="Write base64 token blob to this file instead of stdout"
    )
    args = parser.parse_args()

    email = os.environ.get("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = os.environ.get("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")

    # return_on_mfa=True splits login into credentials + MFA phases so callers
    # can handle MFA out-of-band (e.g., polling an inbox instead of blocking
    # on stdin).
    client = Garmin(email=email, password=password, return_on_mfa=True)

    try:
        result1, result2 = client.login()
        if result1 == "needs_mfa":
            mfa_code = _prompt_mfa()
            client.resume_login(result2, mfa_code)
    except GarminConnectAuthenticationError as e:
        print(f"\nLogin failed: {e}", file=sys.stderr)
        return 1

    # Dump the DI Bearer token state to a compact JSON string.
    token_json = client.client.dumps()
    encoded = base64.b64encode(token_json.encode()).decode("ascii")

    if args.out:
        Path(args.out).write_text(encoded)
        print(f"\nLogin successful. Wrote {len(encoded)} chars to {args.out}")
        print("Update the GitHub secret:")
        print(f"  gh secret set GARMIN_TOKENS_B64 -R <owner>/<repo> < {args.out}")
    else:
        print("\nLogin successful.\n")
        print("Set this as GARMIN_TOKENS_B64 in Render (or locally in .env):\n")
        print(encoded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
