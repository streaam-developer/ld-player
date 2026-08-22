"""Auto-signup farm: fresh random instances, run in parallel.

Every worker cycle does:

1. create a brand-new LDPlayer instance (``auto_<ts><rand>``)
2. apply a unique random mobile device profile (IMEI/IMSI/MAC/model/…)
3. launch it and run the Facebook signup flow with a guaranteed-unique email
4. success  -> KEEP the instance (account lives inside it), log it to
   ``saved_instances.txt``
5. blocked / failed / error -> quit + DELETE the instance so nothing dirty
   is left behind

``workers`` cycles run concurrently (default 3). Instance creation and
deletion are serialised under a lock because ldconsole is not reliably
safe to drive concurrently; the long signup runs themselves are parallel.
"""

from __future__ import annotations

import random
import threading
import time
import traceback

from datetime import datetime
from pathlib import Path

from .adb import Adb
from .console import LdConsole, LdConsoleError
from .emails import claim_email
from .device import apply_profile
from .facebook import FacebookFlow

ROOT = Path(__file__).resolve().parent.parent
SAVED_FILE = ROOT / "saved_instances.txt"

#: prefix marking instances owned by this tool — only these ever get deleted
INSTANCE_PREFIX = "auto_"

FIRST_NAMES = ["Alex", "Sam", "Jordan", "Taylor", "Casey", "Riley", "Morgan",
               "Jamie", "Avery", "Drew", "Reese", "Quinn", "Blake", "Skyler",
               "Charlie", "Emerson", "Finley", "Harper", "Rowan", "Sage"]
LAST_NAMES = ["Johnson", "Smith", "Brown", "Miller", "Davis", "Wilson",
              "Moore", "Taylor", "Anderson", "Thomas", "Jackson", "White",
              "Harris", "Martin", "Thompson", "Garcia", "Clark", "Lewis"]


class SignupFarm:
    def __init__(self, console: LdConsole, adb: Adb,
                 workers: int = 3,
                 accounts: int = 0,
                 package: str = "com.facebook.katana",
                 apk_path: str | None = None,
                 cf_worker_url: str = "",
                 cf_worker_api_key: str = "",
                 otp_timeout: float = 120.0,
                 boot_timeout: float = 600.0,
                 install_timeout: float = 840.0,
                 flow_timeout: float = 1500.0,
                 quit_on_success: bool = True):
        self.console = console
        self.adb = adb
        self.workers = max(1, workers)
        self.accounts = accounts          # 0 => keep going until Ctrl+C
        self.package = package
        self.apk_path = apk_path
        self.cf_worker_url = cf_worker_url
        self.cf_worker_api_key = cf_worker_api_key
        self.otp_timeout = otp_timeout
        self.boot_timeout = boot_timeout
        self.install_timeout = install_timeout
        self.flow_timeout = flow_timeout
        self.quit_on_success = quit_on_success

        self._stop = threading.Event()
        self._create_lock = threading.Lock()   # ldconsole add/modify/remove
        self._state_lock = threading.Lock()    # counters + active map
        self._active: dict[str, str] = {}      # name -> running|saved
        self.successes = 0
        self.failures = 0

    # ------------------------------------------------------------- reporting
    def _log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[farm {stamp}] {msg}", flush=True)

    # ------------------------------------------------------------ stop logic
    def stop(self) -> None:
        self._stop.set()

    def _target_reached(self) -> bool:
        with self._state_lock:
            return self.accounts > 0 and self.successes >= self.accounts

    # ------------------------------------------------------ instance helpers
    def _new_instance_name(self) -> str:
        while True:
            name = (f"{INSTANCE_PREFIX}{datetime.now().strftime('%m%d%H%M%S')}"
                    f"{random.randint(10, 99)}")
            if not self.console.find(name=name):
                return name

    def _create_instance(self, name: str) -> int:
        """Create + randomise device settings. Returns the instance index."""
        with self._create_lock:
            if self.console.find(name=name):
                raise RuntimeError(f"instance '{name}' already exists")
            res = self.console.add(name)
            inst = self.console.find(name=name)
            if not inst:
                raise RuntimeError(
                    f"create failed: {res.text or res.stderr}")
            profile = apply_profile(self.console, name=name)
            self._log(f"created '{name}' (index {inst.index}) — "
                      f"random mobile: {profile.summary()}")
            return inst.index

    def _discard_instance(self, name: str) -> None:
        """Quit + remove a failed/aborted instance (best effort)."""
        try:
            inst = self.console.find(name=name)
            if not inst:
                return
            try:
                if self.console.is_running(index=inst.index):
                    self.console.quit(index=inst.index)
                    self.console.wait_until_quit(index=inst.index,
                                                 timeout=90, poll=2.0)
            except LdConsoleError as exc:
                self._log(f"'{name}' quit problem ({exc}) — trying removal "
                          "anyway")
            for attempt in range(1, 4):
                res = self.console.remove(index=inst.index)
                if not self.console.find(name=name):
                    self._log(f"deleted instance '{name}' "
                              f"(attempt {attempt})")
                    return
                self._log(f"remove of '{name}' did not take "
                          f"(attempt {attempt}: {res.text or res.stderr})")
                time.sleep(3)
            self._log(f"WARNING: could not delete instance '{name}' — "
                      "remove it manually in LDPlayer")
        except Exception as exc:  # noqa: BLE001
            self._log(f"cleanup of '{name}' failed: {exc}")

    def _release(self, name: str, saved: bool) -> None:
        """Book-keeping shared by every exit path."""
        with self._state_lock:
            self._active.pop(name, None)
            if saved:
                self.successes += 1
            else:
                self.failures += 1
        total = (f"successes={self.successes} failures={self.failures}")
        if self.accounts > 0:
            total += f"/{self.accounts} target"
        self._log(total)

    def _save_record(self, name: str, index: int, email: str) -> None:
        line = f"{name}|index={index}|{email}|{datetime.now().isoformat(timespec='seconds')}\n"
        try:
            with open(SAVED_FILE, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            self._log(f"could not write {SAVED_FILE}: {exc}")

    # ---------------------------------------------------------------- worker
    def _run_once(self, worker_id: int) -> bool:
        """One full create->signup->keep/delete cycle. False = fatal setup
        error worth backing off from (e.g. cannot create any more VMs)."""
        name = self._new_instance_name()
        try:
            index = self._create_instance(name)
        except Exception as exc:  # noqa: BLE001
            self._log(f"worker {worker_id}: creating instance failed: {exc}")
            time.sleep(15)
            return False

        with self._state_lock:
            self._active[name] = "running"

        email = claim_email()
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        saved = False
        try:
            self._log(f"worker {worker_id}: signing up on '{name}' "
                      f"as {first} {last} <{email}>")
            flow = FacebookFlow(
                self.console, self.adb, name=name, package=self.package,
                cf_worker_url=self.cf_worker_url,
                cf_worker_api_key=self.cf_worker_api_key)
            flow.run(boot_timeout=self.boot_timeout,
                     install_timeout=self.install_timeout,
                     apk_path=self.apk_path,
                     first_name=first, last_name=last,
                     otp_timeout=self.otp_timeout,
                     flow_timeout=self.flow_timeout,
                     email=email)
            outcome = flow.success or "blocked"
            if outcome == "success":
                idx = flow.inst.info.index if flow.inst.info else index
                self._save_record(name, idx, email)
                self._log(f"SUCCESS on '{name}' ({email}) — keeping instance")
                saved = True
                if self.quit_on_success:
                    try:
                        flow.inst.quit()
                        self._log(f"closed '{name}' (kept on disk)")
                    except Exception as exc:  # noqa: BLE001
                        self._log(f"'{name}' close after success failed: {exc}")
            else:
                self._log(f"FAILED on '{name}' ({email}) — {outcome}; "
                          "deleting instance")
        except Exception as exc:  # noqa: BLE001
            self._log(f"ERROR during signup on '{name}': {exc}")
            traceback.print_exc()
        finally:
            if not saved:
                self._discard_instance(name)
            self._release(name, saved)
        return True

    def _worker(self, worker_id: int) -> None:
        while not self._stop.is_set() and not self._target_reached():
            progressed = self._run_once(worker_id)
            if self._stop.is_set() or self._target_reached():
                break
            if not progressed:
                continue
            # small breather so three VMs never boot at the exact same moment
            time.sleep(random.uniform(2.0, 6.0))
        self._log(f"worker {worker_id} exiting")

    # ------------------------------------------------------------------ main
    def run(self) -> tuple[int, int]:
        self._log(f"starting {self.workers} parallel workers"
                  + (f" — target {self.accounts} account(s)"
                     if self.accounts else " — Ctrl+C to stop"))
        threads = [threading.Thread(target=self._worker, args=(i + 1,),
                                    name=f"signup-{i + 1}")
                   for i in range(self.workers)]
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads):
                time.sleep(0.5)
        except KeyboardInterrupt:
            self._log("stop requested — waiting for the running signups to "
                      "finish (press Ctrl+C again to force-quit)")
            self.stop()
            try:
                while any(t.is_alive() for t in threads):
                    time.sleep(0.5)
            except KeyboardInterrupt:
                self._log("force quit — running instances left as-is")
                raise SystemExit(130) from None
        self._log(f"done — successes={self.successes} "
                  f"failures={self.failures}")
        return self.successes, self.failures
