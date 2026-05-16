"""One-time Garmin login with MFA. Run locally, copy the printed token blob
into your environment (as GARTH_TOKENS_B64) for the deployed server.

Usage:
    # interactive (you type the MFA code)
    python scripts/bootstrap.py

    # write the base64 blob to a file (for `gh secret set ... < file`)
    python scripts/bootstrap.py --out /tmp/garth.b64

    # provide credentials via env (still need MFA interactively)
    GARMIN_EMAIL=... GARMIN_PASSWORD=... python scripts/bootstrap.py

You'll be prompted for email, password, and the 6-digit MFA code Garmin emails
to you. On success, prints (or writes) a base64-encoded tarball of the OAuth
tokens that can be set as GARTH_TOKENS_B64.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import io
import os
import sys
import tarfile
import tempfile
from pathlib import Path

import garth
from garminconnect import Garmin, GarminConnectAuthenticationError


CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


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

    # return_on_mfa=True splits the login into a credentials phase and an
    # MFA-resume phase. Cleaner than passing prompt_mfa= because it lets
    # callers (including future automated rotators) handle MFA out-of-band
    # — e.g. polling an inbox for the code instead of blocking on stdin.
    client = Garmin(email=email, password=password, return_on_mfa=True)
    try:
        garth.client.sess.headers["User-Agent"] = CHROME_UA
    except AttributeError:
        pass

    try:
        result1, result2 = client.login()
        if result1 == "needs_mfa":
            mfa_code = _prompt_mfa()
            client.resume_login(result2, mfa_code)
    except GarminConnectAuthenticationError as e:
        print(f"\nLogin failed: {e}", file=sys.stderr)
        return 1

    # After login, dump tokens via the module-level garth client. The newer
    # garminconnect doesn't expose client.garth, but garth keeps its own
    # module-global session whose state was populated by client.login().
    tokens_dir = Path(tempfile.mkdtemp(prefix="garth-bootstrap-"))
    garth.save(str(tokens_dir))

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in tokens_dir.iterdir():
            tf.add(p, arcname=p.name)

    encoded = base64.b64encode(buf.getvalue()).decode("ascii")

    if args.out:
        Path(args.out).write_text(encoded)
        print(f"\nLogin successful. Wrote {len(encoded)} chars to {args.out}")
        print(f"Update the GitHub secret:")
        print(f"  gh secret set GARTH_TOKENS_B64 -R <owner>/<repo> < {args.out}")
    else:
        print("\nLogin successful.\n")
        print("Set this as GARTH_TOKENS_B64 in Render (or locally in .env):\n")
        print(encoded)
    print(f"\nLocal files with tokens: {tokens_dir}")
    print("You can delete that directory after copying the blob.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
