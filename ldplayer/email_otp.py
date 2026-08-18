"""Fetch OTP codes from the Cloudflare HTTP Worker.

Uses only the Python standard library (``urllib``, ``json``) —
no third-party dependencies required.

Usage::

    from ldplayer.email_otp import fetch_otp, OtpTimeout

    code = fetch_otp(
        worker_url="https://otp-http.my-sub.workers.dev",
        api_key="k9x2m5p8q3w7",
        email="xbqkmlj@dailykhabar.cfd",
    )
    print(code)  # "12345"
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request


class OtpTimeout(RuntimeError):
    """Raised when the OTP code is not received within the timeout."""


def fetch_otp(worker_url: str, api_key: str, email: str, *,
              timeout: float = 120, poll: float = 5.0) -> str:
    """Poll the Cloudflare HTTP Worker until an OTP code arrives.

    Parameters
    ----------
    worker_url:
        Base URL of the HTTP Worker (e.g. ``https://otp-http.xxx.workers.dev``).
    api_key:
        The shared secret set as ``API_KEY`` in the Worker environment.
    email:
        The recipient email address to look up (e.g. ``x@dailykhabar.cfd``).
    timeout:
        Maximum seconds to wait before raising :class:`OtpTimeout`.
    poll:
        Seconds between polling attempts.

    Returns
    -------
    str
        The numeric OTP code (e.g. ``"12345"``).

    Raises
    ------
    OtpTimeout
        If no code is received within *timeout* seconds.
    """
    url = f"{worker_url.rstrip('/')}/otp?email={urllib.parse.quote(email)}"
    start = time.time()
    last_log = 0.0

    while True:
        elapsed = time.time() - start
        if elapsed >= timeout:
            raise OtpTimeout(
                f"OTP for {email} not received within {timeout:.0f}s")

        # Log progress every 10 seconds
        if elapsed - last_log >= 10:
            last_log = elapsed
            print(f"  ... polling OTP for {email} "
                  f"({elapsed:.0f}/{timeout:.0f}s)", flush=True)

        code = _poll_once(url, api_key)
        if code:
            print(f"  [otp] received code for {email}: {code}", flush=True)
            return code

        time.sleep(poll)


def _poll_once(url: str, api_key: str) -> str | None:
    """Make a single GET request.  Returns the code or ``None``."""
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            if resp.status == 200 and "code" in data:
                return str(data["code"])
            return None
    except urllib.error.HTTPError as exc:
        # 202 = not ready yet, 401 = bad key — both mean "try again"
        if exc.code in (202, 401):
            return None
        # 5xx or other unexpected errors — log and retry
        print(f"  [otp] HTTP {exc.code} from Worker — retrying...", flush=True)
        return None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        return None
