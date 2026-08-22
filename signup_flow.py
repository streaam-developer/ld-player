"""Combined Outlook -> Facebook signup pipeline.

One LDPlayer instance, two phases:

  PHASE 1 — OUTLOOK (ldplayer/outlook.py)
    Creates a FRESH instance (4 CPU / 4 GB RAM, random mobile identity),
    opens Chrome, dismisses its first-run screens and signs up a new
    Microsoft/Outlook account. Credentials land in outlook.txt
    (email|password|recovery) the moment the account is ready.

  PHASE 2 — FACEBOOK (ldplayer/facebook.py)
    Returns to the launcher, installs the Facebook APK if missing and
    runs the adaptive signup loop with:
      * email     = the @outlook.com address created in phase 1
      * OTP       = read straight from the signed-in Outlook inbox in
                    Chrome (the script hops back to Chrome, finds the
                    Facebook mail, extracts the code, then returns to
                    Facebook to type it)
    On success facebook.txt gets email|password.

Run:
    python signup_flow.py                 # fresh instance every run
    python signup_flow.py --template X    # clone instance X instead
    python signup_flow.py --index N       # reuse an existing instance
"""

from __future__ import annotations

import argparse
import random
import sys
import time
import traceback

from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ldplayer.adb import Adb
from ldplayer.config import find_ldconsole, load_config
from ldplayer.console import LdConsole
from ldplayer.facebook import FacebookFlow, find_facebook_apk
from ldplayer.outlook import (FIRST_NAMES, LAST_NAMES, OutlookFlow,
                              create_signup_instance, new_instance_name)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)

    p = argparse.ArgumentParser(
        description=__doc__,
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
                   help="name prefix for the fresh instance (default: otl)")
    p.add_argument("--apk",
                   help="Facebook apk/apkm/xapk path (default: newest "
                        "found in the working directory)")
    p.add_argument("--first-name", help="fixed first name (default: random)")
    p.add_argument("--last-name", help="fixed last name (default: random)")
    p.add_argument("--recovery-email",
                   help="@dailykhabar.bond address for Outlook's 'protect "
                        "your account' step")
    p.add_argument("--min-age", type=int, default=21)
    p.add_argument("--max-age", type=int, default=49)
    p.add_argument("--fb-otp-timeout", type=float, default=300,
                   help="seconds to wait for Facebook's code inside the "
                        "Outlook inbox (default: 300)")
    p.add_argument("--boot-timeout", type=float, default=900)
    p.add_argument("--flow-timeout", type=float, default=1800,
                   help="seconds allowed PER phase")
    args = p.parse_args()

    console = LdConsole(find_ldconsole())
    cfg = load_config()
    adb = Adb(cfg["adb"])

    first = args.first_name or random.choice(FIRST_NAMES)
    last = args.last_name or random.choice(LAST_NAMES)

    try:
        # ================================================== PHASE 1: outlook
        index = args.index
        name = args.name
        if index is None and name is None:
            name = new_instance_name(args.prefix, console)
            inst = create_signup_instance(console, name,
                                          template=args.template)
            name = inst.name
            print(f"[{name}] PHASE 1/2 — new instance created "
                  f"(4 CPU / 4 GB), starting OUTLOOK signup ...", flush=True)

        oflow = OutlookFlow(console, adb, index=index, name=name,
                            cf_worker_url=cfg.get("cf_worker_url", ""),
                            cf_worker_api_key=cfg.get("cf_worker_api_key", ""))
        oflow.run(boot_timeout=args.boot_timeout,
                  username=None, password=None,
                  first_name=first, last_name=last,
                  recovery_email=args.recovery_email,
                  min_age_years=args.min_age, max_age_years=args.max_age,
                  flow_timeout=args.flow_timeout)

        if not oflow.success or not oflow.outlook_address:
            raise RuntimeError("outlook phase did not complete — aborting")

        print(f"\n[{oflow.inst.name}] OUTLOOK READY — "
              f"{oflow.outlook_address} (saved to outlook.txt)\n",
              flush=True)

        # ================================================ PHASE 2: facebook
        print(f"[{oflow.inst.name}] back to the launcher ...", flush=True)
        oflow.auto.home()
        time.sleep(2)

        fflow = FacebookFlow(console, adb, index=oflow.inst.index)

        def otp_provider() -> str:
            """Facebook's code arrives AT the new outlook address — read
            it from the inbox in Chrome, then hop back into Facebook."""
            code = oflow.read_facebook_otp_from_outlook(
                timeout=args.fb_otp_timeout)
            print(f"[{oflow.inst.name}] hopping back into Facebook ...",
                  flush=True)
            fflow.inst.run_app(fflow.package)
            time.sleep(4)
            return code

        fflow._otp_provider = otp_provider

        print(f"[{oflow.inst.name}] PHASE 2/2 — FACEBOOK signup "
              f"(email: {oflow.outlook_address}) ...", flush=True)
        apk_path = args.apk or str(find_facebook_apk())
        fflow.run(apk_path=apk_path,
                  first_name=first, last_name=last,
                  email=oflow.outlook_address,
                  boot_timeout=args.boot_timeout,
                  flow_timeout=args.flow_timeout)

        if fflow.success == "success":
            print(f"\n[{oflow.inst.name}] " + "=" * 60, flush=True)
            print(f"[{oflow.inst.name}] PIPELINE COMPLETE", flush=True)
            print(f"[{oflow.inst.name}]   outlook  : {oflow.outlook_address}"
                  f"  -> outlook.txt", flush=True)
            print(f"[{oflow.inst.name}]   facebook : {fflow._email}"
                  f"  -> facebook.txt", flush=True)
            print(f"[{oflow.inst.name}] " + "=" * 60 + "\n", flush=True)
        else:
            print(f"\n[{oflow.inst.name}] facebook phase finished with "
                  f"status '{fflow.success or 'error'}' — check the log "
                  f"above.", flush=True)
            return 1

        print(f"[{oflow.inst.name}] Instance stays open — Ctrl+C to stop.",
              flush=True)
        while True:
            time.sleep(30)
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
