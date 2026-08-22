"""Open Chrome in an LDPlayer instance by searching the system apps.

Run directly from the command line:
    python chrome_flow.py [--index N | --name NAME] [--label Chrome]
                          [--package com.android.chrome]
                          [--boot-timeout S] [--search-timeout S]
                          [--no-direct-fallback]

Flow inside every run (mirrors signup_flow's opening steps):
  1. open the instance (starts it if stopped) and wait until the apps
     grid / launcher is showing
  2. confirm the package is installed
  3. search the system apps for Chrome: scan the home screen, peek into
     the "System Apps" folder, swipe up to the app drawer, type the
     query into its search box, flip through remaining pages
  4. tap the Chrome icon and wait until it is in the foreground

If every UI-search strategy fails, the package is launched directly via
ldconsole/adb unless --no-direct-fallback is given.
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
from ldplayer.appsearch import DEFAULT_LABEL, DEFAULT_PACKAGE, open_app_flow


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)

    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--index", type=int, help="instance index "
                                             "(default: config default_instance)")
    p.add_argument("--name", help="instance name")
    p.add_argument("--label", default=DEFAULT_LABEL,
                   help=f"app label to look for (default: {DEFAULT_LABEL})")
    p.add_argument("--package", default=DEFAULT_PACKAGE,
                   help=f"package to verify/launch "
                        f"(default: {DEFAULT_PACKAGE})")
    p.add_argument("--boot-timeout", type=float, default=600,
                   help="seconds to wait for boot + launcher")
    p.add_argument("--search-timeout", type=float, default=180,
                   help="seconds allowed for finding the icon in the UI")
    p.add_argument("--open-timeout", type=float, default=90,
                   help="seconds to wait for the app to reach the foreground")
    p.add_argument("--no-direct-fallback", action="store_true",
                   help="fail instead of launching the package directly "
                        "when UI search finds nothing")
    args = p.parse_args()

    console = LdConsole(find_ldconsole())
    cfg = load_config()
    adb = Adb(cfg["adb"])

    index = args.index
    name = args.name
    if index is None and name is None:
        name = cfg.get("default_instance")

    try:
        open_app_flow(console, adb, index=index, name=name,
                      label=args.label, package=args.package,
                      boot_timeout=args.boot_timeout,
                      search_timeout=args.search_timeout,
                      open_timeout=args.open_timeout,
                      direct_fallback=not args.no_direct_fallback)
    except Exception as exc:  # noqa: BLE001
        print(f"\nERROR: {exc}", flush=True)
        traceback.print_exc()
        return 1
    print("chrome flow finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
