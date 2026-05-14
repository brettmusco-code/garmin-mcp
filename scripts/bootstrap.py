"""One-time Garmin login with MFA. Run locally, copy the printed token blob
into your environment (as GARTH_TOKENS_B64) for the deployed server.

Usage:
    python scripts/bootstrap.py

You'll be prompted for email, password, and the 6-digit MFA code Garmin emails
to you. On success, prints a base64-encoded tarball of the OAuth tokens.
"""
from __future__ import annotations

import base64
import getpass
import io
import sys
import tarfile
import tempfile
from pathlib import Path

import garth
from garminconnect import Garmin, GarminConnectAuthenticationError


def main() -> int:
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")

    client = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("MFA code from email: ").strip(),
    )
    # Set UA on the module-level garth client (works for any garminconnect
    # version since garth.client is shared). Garmin throttles the default UA.
    chrome_ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    try:
        garth.client.sess.headers["User-Agent"] = chrome_ua
    except AttributeError:
        pass

    try:
        client.login()
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

    print("\nLogin successful.\n")
    print("Set this as GARTH_TOKENS_B64 in Render (or locally in .env):\n")
    print(encoded)
    print("\nLocal files with tokens:", tokens_dir)
    print("You can delete that directory after copying the blob.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
