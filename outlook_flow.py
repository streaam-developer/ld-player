"""Outlook (Microsoft) signup automation — run directly from the command line.

ALWAYS creates a brand-new LDPlayer instance per run (random mobile
identity, adb forced on), then inside it:

    1. opens Chrome (searched in the launcher like a human would)
    2. dismisses Chrome's first-run screens ("Welcome to Chrome" ->
       "Use without signing in" -> "More" -> "Got it")
    3. navigates to https://outlook.office.com/mail/ and waits for load
    4. taps "Create one", types a random 8-char letter+number username,
       Next; strong password, Next
    5. birth date Month/Day/Year more than 20 years back, Next
    6. random first/last name, Next
    7. "prove you're human" — YOU solve the puzzle by hand; the script waits
    8. "protect your account" — types an @dailykhabar.bond recovery email
       picked from the repo's used_emails.json pool (--recovery-email to
       force one), Next; fetches the code from the Cloudflare Worker and
       enters it, Next
    9. "we could not create passkey" — Cancel; everything stays open until
       you stop the script with Ctrl+C

Requires cf_worker_url + cf_worker_api_key in config.json for the OTP step.
Run on an EXISTING instance instead with --index N / --name NAME.
"""

from __future__ import annotations

import argparse
import sys
import traceback

from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ldplayer.config import find_ldconsole, load_config
from ldplayer.console import LdConsole
from ldplayer.adb import Adb
from ldplayer.outlook import create_signup_instance, new_instance_name, \
    outlook_flow


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)

    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--index", type=int,
                   help="run on an EXISTING instance instead of creating "
                        "a fresh one")
    p.add_argument("--name",
                   help="run on an EXISTING instance instead of creating "
                        "a fresh one")
    p.add_argument("--template",
                   help="clone THIS instance instead of creating a blank "
                        "one (should have Chrome ready)")
    p.add_argument("--prefix", default="otl",
                   help="name prefix for the fresh instance "
                        "(default: otl)")
    p.add_argument("--username",
                   help="fixed @outlook.com username "
                        "(default: random 8 letters+numbers)")
    p.add_argument("--password", help="fixed password (default: random)")
    p.add_argument("--first-name", help="fixed first name (default: random)")
    p.add_argument("--last-name", help="fixed last name (default: random)")
    p.add_argument("--recovery-email",
                   help="@dailykhabar.bond address for 'protect your "
                        "account' (default: random pick from "
                        "used_emails.json)")
    p.add_argument("--min-age", type=int, default=21,
                   help="minimum birth-year age (default: 21, always >20)")
    p.add_argument("--otp-timeout", type=float, default=240,
                   help="seconds to wait for the code from the CF Worker")
    p.add_argument("--boot-timeout", type=float, default=900,
                   help="seconds to wait for instance boot + launcher")
    p.add_argument("--flow-timeout", type=float, default=1500,
                   help="seconds allowed for one full signup flow")
    p.add_argument("--package", default="com.android.chrome")
    args = p.parse_args()

    console = LdConsole(find_ldconsole())
    cfg = load_config()
    adb = Adb(cfg["adb"])

    try:
        index = args.index
        name = args.name
        if args.index is None and args.name is None:
            # ALWAYS a fresh instance per run
            name = new_instance_name(args.prefix, console)
            inst = create_signup_instance(console, name,
                                          template=args.template)
            name = inst.name
            print(f"[{name}] new instance created — starting flow ...",
                  flush=True)

        outlook_flow(console, adb, index=index, name=name,
                     package=args.package,
                     boot_timeout=args.boot_timeout,
                     username=args.username, password=args.password,
                     first_name=args.first_name, last_name=args.last_name,
                     recovery_email=args.recovery_email,
                     cf_worker_url=cfg.get("cf_worker_url", ""),
                     cf_worker_api_key=cfg.get("cf_worker_api_key", ""),
                     otp_timeout=args.otp_timeout,
                     min_age_years=args.min_age,
                     flow_timeout=args.flow_timeout)
    except KeyboardInterrupt:
        print("\ninterrupted — instance left running.", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\nERROR: {exc}", flush=True)
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
