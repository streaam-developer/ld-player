"""Facebook signup automation — run directly from the command line.

Auto mode (default):
    python signup_flow.py [--workers 3] [--accounts N]

  Keeps `--workers` (default 3) emulator signups running in parallel, each
  cycle:
    1. creates a brand-new LDPlayer instance named ``auto_<ts><nn>``
    2. writes a unique random mobile profile into it (IMEI/IMSI/ICCID,
       Android ID, MAC, phone number, manufacturer/model, resolution)
    3. launches it and runs the Facebook signup flow with a
       guaranteed-unique email (never reused, tracked in used_emails.json)
    4. SUCCESS -> the instance is KEPT (account lives inside it) and logged
       to saved_instances.txt
    5. human-verification block / failure / error -> the instance is
       quit and DELETED

  Runs until --accounts successes are reached, or forever until Ctrl+C
  (first Ctrl+C waits for running signups to finish; second force-quits).

Single-instance mode (manual runs on an existing emulator):
    python signup_flow.py --index N | --name NAME [--hold]
        [--first-name FN] [--last-name LN]

Flow inside every signup:
  1. open the instance, wait until the apps grid (launcher) is showing
  2. open the Facebook app
  3. wait for "Create new account", tap it, wait there
  4. tap "Create new account" again on the form
  5. wait for the Contacts permission prompt and tap "Allow"
  6. enter first/last name, tap Next
  7. open birthday picker, scroll year >20 years back, tap Set, tap Next
  8. select Male, tap Next
  9. tap "Sign up with email", enter unique email, tap Next
  10. create password, tap Next, save email|password to raw.txt
  11. tap "I agree" on terms screen, wait
  12. fetch OTP from CF Worker, enter code, tap Next

Requires cf_worker_url + cf_worker_api_key in config.json.
Use --hold to pause after step 3 (press Enter to continue).
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
from ldplayer.facebook import signup_flow
from ldplayer.farm import SignupFarm


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)

    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--index", type=int, help="instance index "
                                            "(enables single-instance mode)")
    p.add_argument("--name", help="instance name "
                                  "(enables single-instance mode)")
    p.add_argument("--workers", type=int, default=3,
                   help="auto mode: parallel signup instances (default: 3)")
    p.add_argument("--accounts", type=int, default=0,
                   help="auto mode: stop after this many successful "
                        "signups (0 = keep going until Ctrl+C)")
    p.add_argument("--keep-open", action="store_true",
                   help="auto mode: leave successful instances running "
                        "instead of closing them")
    p.add_argument("--package", default="com.facebook.katana")
    p.add_argument("--apk",
                   help="path to the Facebook apk/apkm/xapk to install if "
                        "Facebook is not installed yet (auto-detected from "
                        "the working directory otherwise)")
    p.add_argument("--step-wait", type=float, default=3.0,
                   help="pause between steps (seconds)")
    p.add_argument("--hold", action="store_true",
                   help="single-instance mode: pause after the first "
                        "'Create new account' tap")
    p.add_argument("--no-grant", action="store_true",
                   help="skip pre-granting contacts/location permissions")
    p.add_argument("--boot-timeout", type=int, default=900,
                   help="seconds to wait for boot + launcher (first boot of "
                        "a fresh instance is slow; 3 at once slower still)")
    p.add_argument("--first-name",
                   help="fixed first name (auto mode picks random names "
                        "when omitted)")
    p.add_argument("--last-name",
                   help="fixed last name (auto mode picks random names "
                        "when omitted)")
    p.add_argument("--otp-timeout", type=float, default=120,
                   help="seconds to wait for OTP code from CF Worker")
    p.add_argument("--flow-timeout", type=float, default=1500,
                   help="seconds allowed for one full signup flow")
    args = p.parse_args()

    console = LdConsole(find_ldconsole())
    cfg = load_config()
    adb = Adb(cfg["adb"])

    # ------------------------------------------------------- auto farm mode
    if args.index is None and args.name is None:
        try:
            farm = SignupFarm(
                console, adb, workers=args.workers, accounts=args.accounts,
                package=args.package, apk_path=args.apk,
                cf_worker_url=cfg.get("cf_worker_url", ""),
                cf_worker_api_key=cfg.get("cf_worker_api_key", ""),
                otp_timeout=args.otp_timeout,
                boot_timeout=args.boot_timeout,
                flow_timeout=args.flow_timeout,
                quit_on_success=not args.keep_open)
            ok, bad = farm.run()
        except Exception as exc:
            print(f"\nERROR: {exc}", flush=True)
            traceback.print_exc()
            return 1
        print(f"signup farm finished — {ok} saved, {bad} deleted")
        return 0

    # ----------------------------------------------- single instance (manual)
    try:
        signup_flow(console, adb, index=args.index, name=args.name,
                    package=args.package, step_wait=args.step_wait,
                    hold=args.hold, grant_perms=not args.no_grant,
                    boot_timeout=args.boot_timeout, apk_path=args.apk,
                    first_name=args.first_name or "Alex",
                    last_name=args.last_name or "Johnson",
                    cf_worker_url=cfg.get("cf_worker_url", ""),
                    cf_worker_api_key=cfg.get("cf_worker_api_key", ""),
                    otp_timeout=args.otp_timeout)
    except Exception as exc:
        print(f"\nERROR: {exc}", flush=True)
        traceback.print_exc()
        return 1
    print("signup flow finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
