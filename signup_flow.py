"""Facebook signup automation — run directly from the command line.

    python signup_flow.py [--index N | --name NAME] [--hold]

Flow:
  1. open the instance, wait until the apps grid (launcher) is showing
  2. open the Facebook app
  3. wait for "Create new account", tap it, wait there
  4. tap "Create new account" again on the form
  5. wait for the Contacts permission prompt and tap "Allow"

Use --hold to pause after step 3 (press Enter to continue).
"""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ldplayer.config import find_ldconsole, load_config
from ldplayer.console import LdConsole
from ldplayer.adb import Adb
from ldplayer.facebook import signup_flow


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--index", type=int, help="instance index")
    p.add_argument("--name", help="instance name")
    p.add_argument("--package", default="com.facebook.katana")
    p.add_argument("--apk",
                   help="path to the Facebook apk/apkm/xapk to install if "
                        "Facebook is not installed yet (auto-detected from "
                        "the working directory otherwise)")
    p.add_argument("--step-wait", type=float, default=3.0,
                   help="pause between steps (seconds)")
    p.add_argument("--hold", action="store_true",
                   help="pause after the first 'Create new account' tap")
    p.add_argument("--no-grant", action="store_true",
                   help="skip pre-granting contacts/location permissions")
    p.add_argument("--boot-timeout", type=int, default=600,
                   help="seconds to wait for boot + launcher")
    args = p.parse_args()

    console = LdConsole(find_ldconsole())
    cfg = load_config()
    adb = Adb(cfg["adb"])

    signup_flow(console, adb, index=args.index, name=args.name,
                package=args.package, step_wait=args.step_wait,
                hold=args.hold, grant_perms=not args.no_grant,
                boot_timeout=args.boot_timeout, apk_path=args.apk)
    print("signup flow finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
