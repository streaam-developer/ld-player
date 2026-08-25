"""Facebook signup pipeline — two modes, parallel-capable.

MODES:

  outlook  (default — two phases)
    Phase 1 — OUTLOOK: fresh instance (4 CPU / 4 GB), Chrome signup.
    Phase 2 — FACEBOOK: same instance, OTP read from Outlook inbox.

  custom   (single phase)
    Skip Outlook entirely.  A fresh instance (2 CPU / 2 GB) opens
    Facebook directly.  OTP is fetched from the Cloudflare Worker.

MULTI-THREADING:

    --workers N   Launch N parallel signup instances (each gets its own
                  emulator, adb session and thread).  Instance resources
                  are reduced to 2 CPU / 2 GB so the host stays usable.

Run:
    python signup_flow.py                           # outlook, 1 worker
    python signup_flow.py --mode custom             # custom, 1 worker
    python signup_flow.py --mode custom --workers 3 # 3 parallel custom
    python signup_flow.py --workers 4               # 4 parallel outlook
"""

from __future__ import annotations

import argparse
import random
import string
import sys
import threading
import time
import traceback

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ldplayer.adb import Adb
from ldplayer.config import find_ldconsole, load_config
from ldplayer.console import LdConsole
from ldplayer.facebook import FacebookFlow, find_facebook_apk
from ldplayer.outlook import (FIRST_NAMES, LAST_NAMES, OutlookFlow,
                              create_signup_instance, new_instance_name)
from ldplayer.proxy import (ProxyError, setup_per_app_proxy,
                            teardown_per_app_proxy)

CHROME_PACKAGE = "com.android.chrome"
FB_PACKAGE = "com.facebook.katana"
SUCCESSFUL_FILE = "successful.txt"
BLOCKED_FILE = "blocked.txt"

# shared counters for the summary line at the end
_lock = threading.Lock()
_results: list[dict] = []


def _save_credential(email: str, password: str, filepath: str) -> None:
    """Append email|password to *filepath* (thread-safe)."""
    if not email or not password:
        return
    line = f"{email}|{password}\n"
    with _lock:
        with open(filepath, "a", encoding="utf-8") as fh:
            fh.write(line)


def _keep_alive(adb: Adb, index: int, quiet: bool = False) -> None:
    """Pin Chrome + Facebook into Android's 'active' standby bucket."""
    if not adb.connect(index):
        return
    for pkg in (CHROME_PACKAGE, FB_PACKAGE):
        try:
            adb.shell(index, ["am", "set-standby-bucket", pkg, "active"],
                      timeout=15, discover=True)
            print(f"[{index}] {pkg} pinned to the active bucket",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                print(f"[{index}] could not pin {pkg} ({exc}) — continuing",
                      flush=True)


def _wait_foreground(flow, package: str, timeout: float = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            act = flow.auto.focused_activity() or ""
        except Exception:  # noqa: BLE001
            act = ""
        if package in act:
            return True
        try:
            flow.inst.run_app(package)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3.0)
    return False


def _discard_instance(console: LdConsole, inst) -> None:
    name = inst.name
    print(f"[{name}] closing the instance ...", flush=True)
    try:
        console.quit(index=inst.index)
        console.wait_until_quit(index=inst.index, timeout=120, poll=2.0)
    except Exception as exc:  # noqa: BLE001
        print(f"[{name}] quit warning: {exc}", flush=True)
    print(f"[{name}] deleting the instance with all its data ...",
          flush=True)
    try:
        res = console.remove(index=inst.index)
        if getattr(res, "ok", True):
            print(f"[{name}] instance wiped from disk", flush=True)
        else:
            print(f"[{name}] remove failed: {res.text or res.stderr}",
                  flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[{name}] remove failed: {exc}", flush=True)


def _random_email(domain: str, length: int = 7) -> str:
    letters = string.ascii_lowercase
    user = "".join(random.choices(letters, k=length))
    return f"{user}@{domain}"


# ------------------------------------------------------------------ single
def _run_outlook_mode(console: LdConsole, adb: Adb, args: argparse.Namespace,
                      proxy_host: str, proxy_port: int,
                      first: str, last: str, cfg: dict) -> dict:
    """Full outlook -> facebook pipeline on ONE instance (thread-safe)."""
    index = args.index
    name = args.name
    t_start = time.time()
    oflow = None
    proxy_active = False

    try:
        # ---- Phase 1: Outlook ----
        if index is None and name is None:
            name = new_instance_name(args.prefix, console)
            inst = create_signup_instance(console, name,
                                          template=args.template,
                                          cpu=4, memory=4096)
            name = inst.name
            print(f"[{name}] PHASE 1/2 — new instance created "
                  f"(4 CPU / 4 GB), starting OUTLOOK signup ...", flush=True)
        index = inst.index if index is None else index

        oflow = OutlookFlow(console, adb, index=index, name=name,
                            cf_worker_url=cfg.get("cf_worker_url", ""),
                            cf_worker_api_key=cfg.get("cf_worker_api_key", ""))
        t_phase1 = time.time()
        oflow.run(boot_timeout=args.boot_timeout,
                  username=None, password=None,
                  first_name=first, last_name=last,
                  recovery_email=args.recovery_email,
                  min_age_years=args.min_age, max_age_years=args.max_age,
                  flow_timeout=args.flow_timeout)
        phase1_secs = time.time() - t_phase1

        if not oflow.success or not oflow.outlook_address:
            raise RuntimeError("outlook phase did not complete — aborting")

        print(f"\n[{oflow.inst.name}] OUTLOOK READY in "
              f"{phase1_secs / 60:.1f} min"
              f" — {oflow.outlook_address} (saved to outlook.txt)",
              flush=True)

        # ---- Phase 2: Facebook ----
        _keep_alive(adb, index)
        t_phase2 = time.time()
        print(f"[{oflow.inst.name}] back to the launcher ...", flush=True)
        oflow.auto.home()
        time.sleep(2)

        proxy_active = _setup_proxy(adb, oflow.inst.index, proxy_host,
                                    proxy_port, oflow.inst.name)

        fflow = FacebookFlow(console, adb, index=oflow.inst.index)

        def otp_provider() -> str:
            code = oflow.read_facebook_otp_from_outlook(
                timeout=args.fb_otp_timeout)
            print(f"[{oflow.inst.name}] hopping back into Facebook ...",
                  flush=True)
            _wait_foreground(fflow, FB_PACKAGE, timeout=30)
            time.sleep(2)
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

        result = _report(oflow.inst.name, "outlook", fflow, t_start,
                         t_phase1, t_phase2, oflow.outlook_address)

        if fflow.success == "success":
            _save_credential(oflow.outlook_address, fflow._password,
                             SUCCESSFUL_FILE)
            print(f"[{oflow.inst.name}] instance stays open "
                  f"(saved to {SUCCESSFUL_FILE})", flush=True)
        elif fflow.success == "blocked":
            _save_credential(oflow.outlook_address, fflow._password,
                             BLOCKED_FILE)
            _discard_instance(console, oflow.inst)
        return result

    finally:
        if proxy_active and oflow is not None:
            teardown_per_app_proxy(adb, oflow.inst.index)


def _run_custom_mode(console: LdConsole, adb: Adb, args: argparse.Namespace,
                     proxy_host: str, proxy_port: int,
                     first: str, last: str, cfg: dict) -> dict:
    """Facebook-only signup using CF Worker for OTP (thread-safe)."""
    index = args.index
    name = args.name
    t_start = time.time()
    proxy_active = False

    try:
        # ---- Phase 1 SKIPPED — create lightweight instance ----
        if index is None and name is None:
            name = new_instance_name(args.prefix, console)
            inst = create_signup_instance(console, name,
                                          template=args.template,
                                          cpu=2, memory=2048)
            name = inst.name
            print(f"[{name}] CUSTOM MODE — new instance created "
                  f"(2 CPU / 2 GB), starting FACEBOOK signup ...",
                  flush=True)
        index = inst.index if index is None else index

        # pick the email: explicit --email, or random at --domain
        email = args.email
        if not email:
            domain = args.domain or "dailykhabar.bond"
            email = _random_email(domain)

        _keep_alive(adb, index)
        proxy_active = _setup_proxy(adb, index, proxy_host, proxy_port, name)

        fflow = FacebookFlow(console, adb, index=index,
                             cf_worker_url=cfg.get("cf_worker_url", ""),
                             cf_worker_api_key=cfg.get("cf_worker_api_key", ""))
        # no _otp_provider — fflow.run() falls back to CF Worker directly

        print(f"[{name}] CUSTOM MODE — FACEBOOK signup "
              f"(email: {email}) ...", flush=True)
        apk_path = args.apk or str(find_facebook_apk())
        fflow.run(apk_path=apk_path,
                  first_name=first, last_name=last,
                  email=email,
                  boot_timeout=args.boot_timeout,
                  flow_timeout=args.flow_timeout)

        result = _report(name, "custom", fflow, t_start,
                         None, None, email)

        if fflow.success == "success":
            _save_credential(email, fflow._password, SUCCESSFUL_FILE)
            print(f"[{name}] instance stays open "
                  f"(saved to {SUCCESSFUL_FILE})", flush=True)
        elif fflow.success == "blocked":
            _save_credential(email, fflow._password, BLOCKED_FILE)
            _discard_instance(console, inst)
        return result

    finally:
        if proxy_active:
            teardown_per_app_proxy(adb, index)


# ---------------------------------------------------------------- helpers
def _setup_proxy(adb: Adb, index: int, proxy_host: str, proxy_port: int,
                 label: str) -> bool:
    if not proxy_host or not proxy_port:
        return False
    try:
        setup_per_app_proxy(adb, index, FB_PACKAGE, proxy_host, proxy_port)
        return True
    except ProxyError as exc:
        print(f"[{label}] PROXY SETUP FAILED: {exc} — "
              f"continuing without proxy", flush=True)
        return False


def _report(name: str, mode: str, fflow, t_start: float,
            t_phase1: float | None, t_phase2: float | None,
            email: str) -> dict:
    ok = fflow.success == "success"
    total_min = (time.time() - t_start) / 60
    tag = "SUCCESS" if ok else ("BLOCKED" if fflow.success == "blocked"
                                else "FAILED")
    detail = fflow.summary(name)

    with _lock:
        _results.append({"name": name, "mode": mode, "ok": ok,
                         "email": email, "detail": detail,
                         "total_min": total_min})

    bar = "=" * 60
    print(f"\n[{name}] {bar}", flush=True)
    print(f"[{name}] {tag} ({mode} mode) in {total_min:.1f} min",
          flush=True)
    if t_phase1 is not None and t_phase2 is not None:
        print(f"[{name}]   outlook {t_phase1 / 60:.1f} min | "
              f"facebook {(time.time() - t_phase2) / 60:.1f} min",
              flush=True)
    print(f"[{name}]   email    : {email}", flush=True)
    print(f"[{name}]   fb steps : {detail}", flush=True)
    print(f"[{name}] {bar}\n", flush=True)
    return {"ok": ok, "name": name, "email": email}


# ------------------------------------------------------------------ main
def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)

    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=("outlook", "custom"), default="outlook",
                   help="outlook = Phase1 Outlook + Phase2 Facebook (default); "
                        "custom = Facebook only, OTP via CF Worker")
    p.add_argument("--workers", type=int, default=1,
                   help="number of parallel signup instances (default: 1)")
    p.add_argument("--index", type=int,
                   help="run on an EXISTING instance (single-worker only)")
    p.add_argument("--name",
                   help="run on an EXISTING instance (single-worker only)")
    p.add_argument("--template",
                   help="clone THIS instance instead of creating a blank one")
    p.add_argument("--prefix",
                   help="name prefix for fresh instances "
                        "(default: 'otl' for outlook, 'fb' for custom)")
    p.add_argument("--apk",
                   help="Facebook apk/apkm/xapk path")
    p.add_argument("--first-name", help="fixed first name (default: random)")
    p.add_argument("--last-name", help="fixed last name (default: random)")
    p.add_argument("--email",
                   help="email address for Facebook signup "
                        "(custom mode only — random if omitted)")
    p.add_argument("--domain",
                   help="domain for random email generation "
                        "(custom mode — default: dailykhabar.bond)")
    p.add_argument("--recovery-email",
                   help="@dailykhabar.bond address for Outlook recovery "
                        "(outlook mode only)")
    p.add_argument("--min-age", type=int, default=21)
    p.add_argument("--max-age", type=int, default=49)
    p.add_argument("--fb-otp-timeout", type=float, default=300,
                   help="seconds to wait for Facebook's code inside "
                        "the Outlook inbox (outlook mode — default: 300)")
    p.add_argument("--boot-timeout", type=float, default=900)
    p.add_argument("--flow-timeout", type=float, default=1800,
                   help="seconds allowed PER phase")
    p.add_argument("--proxy",
                   help="HTTP proxy for Facebook traffic only "
                        "(format: host:port)")
    args = p.parse_args()

    if args.workers < 1:
        args.workers = 1

    # --index/--name only make sense with 1 worker
    if args.workers > 1 and (args.index is not None or args.name):
        print("ERROR: --index/--name cannot be combined with --workers > 1",
              flush=True)
        return 1

    # set default prefix based on mode
    if args.prefix is None:
        args.prefix = "fb" if args.mode == "custom" else "otl"

    # parse proxy
    proxy_host = ""
    proxy_port = 0
    if args.proxy:
        parts = args.proxy.rsplit(":", 1)
        if len(parts) == 2 and parts[1].isdigit():
            proxy_host = parts[0]
            proxy_port = int(parts[1])
        else:
            print(f"ERROR: invalid proxy format '{args.proxy}' — "
                  f"expected host:port", flush=True)
            return 1

    cfg = load_config()
    console = LdConsole(find_ldconsole())
    adb = Adb(cfg["adb"])

    first = args.first_name or random.choice(FIRST_NAMES)
    last = args.last_name or random.choice(LAST_NAMES)

    run_fn = (_run_outlook_mode if args.mode == "outlook"
              else _run_custom_mode)

    # ---- single worker: run directly (no threads) ----
    if args.workers == 1:
        proxy_active = False
        try:
            run_fn(console, adb, args, proxy_host, proxy_port,
                   first, last, cfg)
        except KeyboardInterrupt:
            print("\ninterrupted — instance left running.", flush=True)
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"\nERROR: {exc}", flush=True)
            traceback.print_exc()
            return 1
        return 0

    # ---- multi-worker: ThreadPoolExecutor ----
    print(f"[main] starting {args.workers} parallel workers "
          f"({args.mode} mode)", flush=True)

    futures = {}
    executor = ThreadPoolExecutor(max_workers=args.workers,
                                  thread_name_prefix="worker")
    try:
        for i in range(args.workers):
            # give each worker its own --index/--name so it creates
            # a fresh instance (clone the args so threads don't clash)
            w_args = argparse.Namespace(**vars(args))
            w_args.index = None
            w_args.name = None
            future = executor.submit(run_fn, console, adb, w_args,
                                     proxy_host, proxy_port,
                                     first, last, cfg)
            futures[future] = i
            time.sleep(2)   # stagger launches so adb discovery doesn't clash

        # wait for all workers
        for future in as_completed(futures):
            wid = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"\n[worker-{wid}] EXCEPTION: {exc}", flush=True)
                traceback.print_exc()

    except KeyboardInterrupt:
        print("\ninterrupted — cancelling pending workers ...", flush=True)
        executor.shutdown(wait=False, cancel_futures=True)
    finally:
        executor.shutdown(wait=False)

    # ---- summary ----
    with _lock:
        ok = sum(1 for r in _results if r["ok"])
        fail = len(_results) - ok

    print(f"\n{'=' * 60}", flush=True)
    print(f"[main] DONE — {ok} succeeded, {fail} failed "
          f"out of {len(_results)}", flush=True)
    if ok:
        print(f"[main]   saved to {SUCCESSFUL_FILE}", flush=True)
    if fail:
        print(f"[main]   saved to {BLOCKED_FILE}", flush=True)
    for r in _results:
        tag = "OK" if r["ok"] else "FAIL"
        print(f"  [{tag}] {r['name']} — {r['email']} "
              f"({r['mode']}, {r['total_min']:.1f} min)", flush=True)
    print(f"{'=' * 60}\n", flush=True)

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
