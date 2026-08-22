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

import json
import random
import shutil
import threading
import time
import traceback

from datetime import datetime
from pathlib import Path

from .adb import Adb
from .config import load_config
from .console import LdConsole, LdConsoleError
from .emails import claim_email
from .device import apply_profile
from .facebook import FacebookFlow
from .repair import _is_admin

ROOT = Path(__file__).resolve().parent.parent
SAVED_FILE = ROOT / "saved_instances.txt"

#: settings-template file — when present, every new instance is given these
#: settings (CPU/RAM, resolution, fps, window geometry, ...) on top of its
#: random device identity. Created with --settings-from <name|index>.
SETTINGS_FILE = ROOT / "ld_settings.json"

#: keys never copied from a settings template onto new instances:
#: identity must stay random per instance, adb must stay ON for the
#: automation, windows should not pile up at one spot, and above all
#: playerName IS the instance's unique name in leidianN.config —
#: copying the template's empty one makes LDPlayer rename/reset the
#: instance and every later find-by-name fails
_IDENTITY_PREFIX = "propertySettings."
_SETTINGS_SKIP_KEYS = {
    "basicSettings.adbDebug",
    "basicSettings.left",
    "basicSettings.top",
    "statusSettings.playerName",
}

#: prefix marking instances owned by this tool — only these ever get deleted
INSTANCE_PREFIX = "auto_"


def _vms_config_dir() -> Path | None:
    """Folder holding leidianN.config files (from saved ldconsole path)."""
    lc = load_config().get("ldconsole")
    return Path(lc).parent / "vms" / "config" if lc else None


def export_instance_settings(console: LdConsole, raw_target: str,
                             dest: Path = SETTINGS_FILE) -> tuple[Path, dict]:
    """Export an instance's settings to a JSON template file.

    ``raw_target`` is a name or index (e.g. ``LDPlayer`` or ``0``).
    Identity keys (IMEI/IMSI/MAC/model/manufacturer/...) are stripped so
    every instance created from this template keeps its own random
    identity; adbDebug / window position are dropped too (forced/managed
    at creation time). Returns (dest, exported_dict).
    """
    raw = str(raw_target).strip()
    inst = console.find(index=int(raw)) if raw.isdigit() else None
    if inst is None:
        inst = console.find(name=raw)
    if inst is None:
        raise RuntimeError(f"instance '{raw_target}' not found")
    cfg_dir = _vms_config_dir()
    src = cfg_dir / f"leidian{inst.index}.config" if cfg_dir else None
    if not src or not src.is_file():
        raise RuntimeError(
            f"could not read config of '{inst.name}' ({src})")
    data = json.loads(src.read_text(encoding="utf-8"))
    exported = {k: v for k, v in data.items()
                if not k.startswith(_IDENTITY_PREFIX)
                and k not in _SETTINGS_SKIP_KEYS}
    dest.write_text(json.dumps(exported, indent=4, ensure_ascii=False),
                    encoding="utf-8")
    return dest, exported

FIRST_NAMES = ["Alex", "Sam", "Jordan", "Taylor", "Casey", "Riley", "Morgan",
               "Jamie", "Avery", "Drew", "Reese", "Quinn", "Blake", "Skyler",
               "Charlie", "Emerson", "Finley", "Harper", "Rowan", "Sage"]
LAST_NAMES = ["Johnson", "Smith", "Brown", "Miller", "Davis", "Wilson",
              "Moore", "Taylor", "Anderson", "Thomas", "Jackson", "White",
              "Harris", "Martin", "Thompson", "Garcia", "Clark", "Lewis"]


class SignupFarm:
    def __init__(self, console: LdConsole, adb: Adb,
                 workers: int = 2,
                 accounts: int = 0,
                 package: str = "com.facebook.katana",
                 apk_path: str | None = None,
                 cf_worker_url: str = "",
                 cf_worker_api_key: str = "",
                 otp_timeout: float = 120.0,
                 boot_timeout: float = 600.0,
                 install_timeout: float = 840.0,
                 flow_timeout: float = 1500.0,
                 quit_on_success: bool = True,
                 manual_input: bool = False,
                 template: str | None = None,
                 settings_file: str | Path | None = None):
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
        #: when True the USER types the email / confirmation code in the
        #: emulator and the flow just waits for them
        self.manual_input = manual_input
        #: optional existing instance (name or index) cloned for every new
        #: signup instance instead of creating a blank one — lets Facebook
        #: come pre-installed
        self.template = template
        self._template_target: tuple[int, str] | None = None
        #: settings template applied to every new instance (see SETTINGS_FILE)
        self.settings_file = Path(settings_file) if settings_file else None

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
    def _vms_root(self) -> Path | None:
        lc = load_config().get("ldconsole")
        return Path(lc).parent / "vms" if lc else None

    def _instance_files_ok(self, index: int) -> bool:
        """Verify a fresh instance actually got fully written.

        NOTE: ``leidian.vbox`` and friends are only generated on *first
        boot*, so a healthy un-booted instance has just its two disk
        images. What ``add``/``modify`` must produce is:
          vms\\leidianN\\data.vmdk + sdcard.vmdk
          vms\\config\\leidianN.config      <- missing on half-written VMs,
                                              which later crash startup with
                                              WriteDataDenied
        """
        vms = self._vms_root()
        if not vms:
            return True
        d = vms / f"leidian{index}"
        cfg_file = vms / "config" / f"leidian{index}.config"
        return ((d / "data.vmdk").is_file()
                and (d / "sdcard.vmdk").is_file()
                and cfg_file.is_file())

    def _force_remove_files(self, index: int) -> None:
        """Delete a leftover vms folder ldconsole's remove may leave behind."""
        vms = self._vms_root()
        d = vms / f"leidian{index}" if vms else None
        if d and d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            self._log(f"removed leftover files in {d}")

    def _new_instance_name(self) -> str:
        while True:
            name = (f"{INSTANCE_PREFIX}{datetime.now().strftime('%m%d%H%M%S')}"
                    f"{random.randint(10, 99)}")
            if not self.console.find(name=name):
                return name

    def _enable_adb(self, index: int) -> bool:
        """Turn on ``adbDebug`` in the instance config before first launch.

        Freshly created instances inherit the LDPlayer GUI's global default,
        which may have ADB debugging OFF (``basicSettings.adbDebug: 0``) —
        then adbd never starts inside the guest, every adb port refuses
        connections, and boot-wait hangs forever even though Android itself
        boots fine. The flag is only read at VM start, so writing it here
        (instance stopped) takes effect on the very next launch.
        """
        vms = self._vms_root()
        cfg_file = vms / "config" / f"leidian{index}.config" if vms else None
        if not cfg_file or not cfg_file.is_file():
            return False
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            if data.get("basicSettings.adbDebug") == 1:
                return True
            data["basicSettings.adbDebug"] = 1
            tmp = cfg_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=4, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(cfg_file)
            return True
        except (OSError, json.JSONDecodeError) as exc:
            self._log(f"could not enable adb on leidian{index}: {exc}")
            return False

    def _resolve_template(self) -> tuple[int, str]:
        """``--template`` accepts an index or a name -> (index, name)."""
        raw = str(self.template).strip()
        inst = self.console.find(index=int(raw)) if raw.isdigit() else None
        if inst is None:
            inst = self.console.find(name=raw)
        if inst is None:
            raise RuntimeError(
                f"template instance '{raw}' not found — check the LDPlayer "
                "instance list")
        return inst.index, inst.name

    def _apply_settings(self, index: int, instance_name: str = "") -> bool:
        """Overlay the settings template (ld_settings.json) onto an instance.

        Copies every key except per-instance identity (IMEI/IMSI/MAC/model/
        ... stays random), forces adbDebug back ON, preserves the instance's
        own name, and leaves window position to LDPlayer. Must run while the
        instance is STOPPED.
        """
        if not self.settings_file or not self.settings_file.is_file():
            return False
        vms = self._vms_root()
        cfg_file = vms / "config" / f"leidian{index}.config" if vms else None
        if not cfg_file or not cfg_file.is_file():
            return False
        try:
            overlay = json.loads(
                self.settings_file.read_text(encoding="utf-8"))
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            copied = 0
            for key, value in overlay.items():
                if key.startswith(_IDENTITY_PREFIX):
                    continue
                if key in _SETTINGS_SKIP_KEYS:
                    continue
                data[key] = value
                copied += 1
            data["basicSettings.adbDebug"] = 1
            # the instance name is its identity — never let a template
            # clobber it (an empty/wrong one makes every find-by-name fail)
            if instance_name:
                data["statusSettings.playerName"] = instance_name
            tmp = cfg_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=4, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(cfg_file)
            self._log(f"applied {copied} settings from "
                      f"{self.settings_file.name} to {instance_name or f'leidian{index}'}")
            return True
        except (OSError, ValueError) as exc:
            self._log(f"could not apply settings template to "
                      f"leidian{index}: {exc}")
            return False

    def _create_instance(self, name: str) -> int:
        """Create (+randomise device settings). Returns the instance index.

        With ``--template`` set, the new instance is a CLONE of that
        instance (Facebook etc. come pre-installed) instead of a blank one.
        """
        with self._create_lock:
            if self.console.find(name=name):
                raise RuntimeError(f"instance '{name}' already exists")
            if self._template_target:
                src_idx, src_name = self._template_target
                try:
                    running = self.console.is_running(index=src_idx)
                except LdConsoleError:
                    running = False
                if running:
                    raise RuntimeError(
                        f"template '{src_name}' is running — close it "
                        "before cloning")
                res = self.console.copy(name, source_index=src_idx)
            else:
                res = self.console.add(name)
            inst = self.console.find(name=name)
            if not inst:
                raise RuntimeError(
                    f"create failed: {res.text or res.stderr}")
            profile = apply_profile(self.console, name=name)
            # verify the write actually landed — a partial instance would
            # break the whole LDPlayer install ("Failed to load ... /
            # WriteDataDenied") the next time it starts
            if not self._instance_files_ok(inst.index):
                raise RuntimeError(
                    f"instance '{name}' was created incompletely "
                    "(config/vmdk missing) — discarding it")
            # ADB debugging must be ON or the automation can never attach
            if not self._enable_adb(inst.index):
                raise RuntimeError(
                    f"could not enable adbDebug on '{name}' "
                    f"(index {inst.index}) — discarding it")
            # give it the saved settings template (CPU/RAM, fps, window
            # geometry, ...) — identity stays random
            self._apply_settings(inst.index, instance_name=name)
            mode = "cloned from template" if self._template_target else \
                "created blank"
            self._log(f"{mode} '{name}' (index {inst.index}) — "
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
                    self._force_remove_files(inst.index)
                    return
                self._log(f"remove of '{name}' did not take "
                          f"(attempt {attempt}: {res.text or res.stderr})")
                time.sleep(3)
            # last resort for stubborn half-created instances
            self._force_remove_files(inst.index)
            if not self.console.find(name=name):
                self._log(f"deleted instance '{name}' (file-level)")
                return
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

    def _saved_names(self) -> set[str]:
        """Names of instances that completed a signup and must be kept."""
        names: set[str] = set()
        if SAVED_FILE.is_file():
            try:
                for line in SAVED_FILE.read_text(
                        encoding="utf-8", errors="replace").splitlines():
                    if "|" in line:
                        names.add(line.split("|", 1)[0].strip())
            except OSError:
                pass
        return names

    # ------------------------------------------------------------- preflight
    def cleanup_leftovers(self) -> int:
        """Remove auto_* junk left by a previously killed/interrupted run.

        Half-created instances are exactly what crashes LDPlayer at startup
        with "Failed to load / WriteDataDenied". Instances recorded in
        saved_instances.txt (successful signups) are never touched, and
        neither are running instances (another live farm may own them).
        Returns the number of leftovers removed.
        """
        keep = self._saved_names()
        if self.template:
            try:
                tidx, tname = self._resolve_template()
                keep.add(tname)
            except RuntimeError:
                pass
        removed = 0
        try:
            leftovers = [i for i in self.console.list_instances()
                         if i.name.startswith(INSTANCE_PREFIX)]
        except Exception as exc:  # noqa: BLE001
            self._log(f"could not list instances for leftover sweep: {exc}")
            return 0
        for inst in leftovers:
            if inst.name in keep:
                continue
            try:
                running = self.console.is_running(index=inst.index)
            except LdConsoleError:
                running = False
            if running:
                continue
            self._log(f"leftover from an earlier run: '{inst.name}' "
                      "(index "
                      f"{inst.index}) — removing")
            self._discard_instance(inst.name)
            removed += 1
        return removed

    def _preflight(self) -> None:
        """Warn about host conditions that cause WriteDataDenied."""
        if not _is_admin():
            self._log("NOT running as administrator — if you ever see "
                      "'Failed to write data (WriteDataDenied)', run the "
                      "terminal as admin and add C:\\LDPlayer to your "
                      "antivirus exclusions")
        vms = self._vms_root()
        if vms:
            drive = vms.anchor or "C:"
            import shutil as _shutil
            try:
                free = _shutil.disk_usage(drive).free
                gb = free / 2 ** 30
                if gb < 15:
                    self._log(f"WARNING: only {gb:.1f} GB free on {drive} — "
                              "each instance needs ~3 GB; low disk causes "
                              "write failures")
            except OSError:
                pass

    # ---------------------------------------------------------------- worker
    def _run_once(self, worker_id: int) -> bool:
        """One full create->signup->keep/delete cycle. False = fatal setup
        error worth backing off from (e.g. cannot create any more VMs)."""
        name = self._new_instance_name()
        try:
            index = self._create_instance(name)
        except Exception as exc:  # noqa: BLE001
            self._log(f"worker {worker_id}: creating instance failed: {exc}")
            # never leave a half-created instance behind
            self._discard_instance(name)
            time.sleep(15)
            return False

        with self._state_lock:
            self._active[name] = "running"

        email = "" if self.manual_input else claim_email()
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        saved = False
        try:
            who = (f"as {first} {last} <{email}>" if email else
                   f"as {first} {last} — YOU will type the email")
            self._log(f"worker {worker_id}: signing up on '{name}' {who}")
            flow = FacebookFlow(
                self.console, self.adb, name=name, package=self.package,
                cf_worker_url=self.cf_worker_url,
                cf_worker_api_key=self.cf_worker_api_key,
                manual_email=self.manual_input,
                manual_otp=self.manual_input)
            flow.run(boot_timeout=self.boot_timeout,
                     install_timeout=self.install_timeout,
                     apk_path=self.apk_path,
                     first_name=first, last_name=last,
                     otp_timeout=self.otp_timeout,
                     flow_timeout=self.flow_timeout,
                     email=email or None)
            outcome = flow.success or "blocked"
            if outcome == "success":
                # lock in the verdict FIRST — nothing below may un-save it
                saved = True
            else:
                shown = email or getattr(flow, "_email", "") or "email?"
                self._log(f"FAILED on '{name}' ({shown}) — {outcome}; "
                          "deleting instance")
        except Exception as exc:  # noqa: BLE001
            self._log(f"ERROR during signup on '{name}': {exc}")
            traceback.print_exc()
        if saved:
            info = getattr(flow.inst, "info", None)
            idx = info.index if info else index
            record_email = (email or getattr(flow, "_email", "")
                            or "manual")
            try:
                self._save_record(name, idx, record_email)
            except Exception as exc:  # noqa: BLE001
                self._log(f"'{name}' could not be logged to "
                          f"{SAVED_FILE.name}: {exc}")
            self._log(f"SUCCESS on '{name}' ({record_email}) — keeping instance")
            if self.quit_on_success:
                try:
                    flow.inst.quit()
                    self._log(f"closed '{name}' (kept on disk)")
                except Exception as exc:  # noqa: BLE001
                    self._log(f"'{name}' close after success failed: {exc} "
                          "— instance stays running")
        try:
            if not saved:
                self._discard_instance(name)
            self._release(name, saved)
        except Exception as exc:  # noqa: BLE001
            self._log(f"post-cycle handling of '{name}' failed: {exc}")
            with self._state_lock:
                self._active.pop(name, None)
                if not saved:
                    self.failures += 1
                else:
                    self.successes += 1
        return True

    def _worker(self, worker_id: int) -> None:
        # stagger the FIRST creations: three simultaneous `ldconsole add`
        # calls racing on the same vms folder is what produced half-written
        # instances (the WriteDataDenied corruption)
        delay = (worker_id - 1) * 20 + random.uniform(0, 5)
        if delay > 0 and not self._stop.is_set():
            self._log(f"worker {worker_id} starting in {delay:.0f}s "
                      "(staggered so VM creation never races)")
            self._stop.wait(delay)
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
        self._preflight()
        if self.settings_file and self.settings_file.is_file():
            try:
                tpl = json.loads(
                    self.settings_file.read_text(encoding="utf-8"))
                cpu = tpl.get("advancedSettings.cpuCount", "?")
                mem = tpl.get("advancedSettings.memorySize", "?")
                res = tpl.get("advancedSettings.resolution", {})
                fps = tpl.get("basicSettings.fps", "?")
                self._log(f"settings template: {self.settings_file.name} — "
                          f"cpu={cpu} ram={mem}MB fps={fps} "
                          f"res={res.get('width')}x{res.get('height')}")
            except (OSError, ValueError):
                pass
        elif self.settings_file:
            self._log(f"NOTE: settings template {self.settings_file} not "
                      "found — instances keep their generated settings")
        if self.template:
            tidx, tname = self._resolve_template()
            self._template_target = (tidx, tname)
            self._log(f"template mode: every new instance will be cloned "
                      f"from '{tname}' (index {tidx}) — make sure it has "
                      "Facebook installed and NO accounts signed in")
        removed = self.cleanup_leftovers()
        if removed:
            self._log(f"cleaned {removed} leftover instance(s) from earlier "
                      "runs")
        self._log(f"starting {self.workers} parallel worker(s)"
                  + (f" — target {self.accounts} account(s)"
                     if self.accounts else " — Ctrl+C to stop"))
        if self.manual_input and self.workers > 1:
            self._log("NOTE: manual input is ON with multiple workers — "
                      "whichever instance reaches its manual step first "
                      "needs your hands")
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
