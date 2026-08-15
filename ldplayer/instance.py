"""Instance lifecycle: create, clone, launch, wait-for-boot, quit."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
import zipfile

from pathlib import Path

from .adb import Adb, AdbError
from .config import find_ldconsole, load_config
from .console import LdConsole, LdConsoleError, LdInstance


class InstanceError(RuntimeError):
    pass


class Instance:
    """A single LDPlayer emulator instance, resolved from name or index."""

    def __init__(self, console: LdConsole, adb: Adb, name: str | None = None,
                 index: int | None = None):
        self._console = console
        self._adb = adb
        self._name = name
        self._index = index
        self.info: LdInstance | None = None

    @classmethod
    def default(cls, cfg: dict | None = None) -> "Instance":
        cfg = cfg or load_config()
        return cls(
            LdConsole(cfg["ldconsole"], timeout=cfg["command_timeout"],
                      base_port=cfg["adb_port_base"]),
            Adb(cfg["adb"], base_port=cfg["adb_port_base"]),
            name=cfg.get("default_instance"),
        )

    # ------------------------------------------------------------ resolution
    def resolve(self) -> LdInstance:
        """Resolve this instance against ldconsole's current list."""
        if self._index is not None:
            inst = self._console.find(index=self._index)
            if not inst:
                raise InstanceError(f"instance index '{self._index}' "
                                    f"does not exist")
        elif self._name:
            inst = self._console.find(name=self._name)
            if not inst:
                raise InstanceError(f"instance '{self._name}' does not exist")
        else:
            raise InstanceError("no instance target given")
        self.info = inst
        return inst

    @property
    def name(self) -> str:
        if self.info:
            return self.info.name
        if self._name:
            return self._name
        raise InstanceError("instance name unknown")

    @property
    def index(self) -> int:
        if self.info:
            return self.info.index
        if self._index is not None:
            return self._index
        raise InstanceError("instance index unknown")

    @property
    def adb_port(self) -> int:
        return self._adb.port_for(self.index)

    # ------------------------------------------------------------- lifecycle
    def launch(self, wait: bool = True, boot_wait: bool = True,
               boot_timeout: float | None = None) -> "Instance":
        cfg = load_config()
        self.resolve()
        if not self._console.is_running(index=self.index):
            res = self._console.launch(index=self.index)
            if not res.ok:
                raise InstanceError(f"launch failed: {res.text or res.stderr}")
        if wait:
            self._console.wait_until_running(index=self.index,
                                             timeout=cfg["launch_timeout"])
        if boot_wait:
            self.wait_for_boot(timeout=boot_timeout or cfg["boot_timeout"])
        return self

    def quit(self, wait: bool = True) -> None:
        self.resolve()
        res = self._console.quit(index=self.index)
        if not res.ok:
            raise InstanceError(f"quit failed: {res.text or res.stderr}")
        if wait:
            self._console.wait_until_quit(index=self.index)

    def wait_for_boot(self, timeout: float = 180, poll: float = 3.0) -> None:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                if self._adb.is_boot_completed(self.index, discover=True):
                    self._adb.wake(self.index, discover=True)
                    print(f"[{self.name}] boot completed "
                          f"(adb {self._adb.endpoint(self.index)})")
                    return
                last = "not booted"
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
            time.sleep(poll)
        raise InstanceError(f"instance '{self.name}' did not finish booting "
                            f"within {timeout}s ({last})")

    # ------------------------------------------------------------------ apps
    def install_apk(self, apk: str | Path) -> None:
        """Install an APK, or extract+install an .apkm/.xapk/.apks bundle."""
        apk = Path(apk)
        if not apk.is_file():
            raise InstanceError(f"APK not found: {apk}")

        bundle = _prepare_bundle(apk)
        try:
            if len(bundle) == 1:
                res = self._console.install_apk(bundle[0], index=self.index)
                if not res.ok:
                    raise InstanceError(
                        f"install failed: {res.text or res.stderr}")
            else:
                self._adb.install_multiple(self.index, bundle)
            _push_obb(self, bundle[0].parent)
            print(f"[{self.name}] installed {apk.name} "
                  f"({len(bundle)} part(s))")
        finally:
            _cleanup_bundle(apk, bundle)

    def install_apk_wait(self, apk: str | Path,
                         timeout: float = 180,
                         adb_timeout: float = 600) -> str | None:
        """Retry: wait for adb, install, then wait until the package registers.

        The LDPlayer guest's adb bridge can flap (drops mid-transfer) on weak
        hosts, so we keep retrying until the package is actually registered.
        """
        apk = Path(apk)
        if not apk.is_file():
            raise InstanceError(f"APK not found: {apk}")
        pkg = _package_name(apk)
        if not pkg:
            raise InstanceError(
                f"could not determine package name for {apk.name}")

        deadline = time.time() + max(adb_timeout, timeout) + 120
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            print(f"[{self.name}] waiting for adb (attempt {attempt}, "
                  f"up to {remaining:.0f}s left)...")
            try:
                self._adb.wait_ready(self.index, timeout=remaining, poll=5)
            except AdbError:
                continue
            print(f"[{self.name}] installing {apk.name} "
                  f"(package {pkg}) ...")
            try:
                self.install_apk(apk)
            except (InstanceError, AdbError) as e:
                if "OLDER_SDK" in str(e).upper():
                    raise _sdk_hint(apk, e)
                print(f"[{self.name}] install attempt {attempt} failed "
                      f"({e}); retrying...")
                time.sleep(5)
                continue
            deadline_ok = time.time() + timeout
            while time.time() < deadline_ok:
                if self._adb.package_installed(self.index, pkg, discover=True):
                    print(f"[{self.name}] package {pkg} present")
                    return pkg
                time.sleep(3)
            print(f"[{self.name}] installed but {pkg} not registered yet; "
                  f"rechecking...")
        raise InstanceError(
            f"could not install {apk.name}: adb stayed unavailable for the "
            f"whole window (instance may need more RAM, or the host is under "
            f"too much memory pressure)")

    def run_app(self, package: str) -> None:
        res = self._console.run_app(package, index=self.index)
        if not res.ok:
            raise InstanceError(f"runapp failed: {res.text or res.stderr}")

    def stop_app(self, package: str) -> None:
        res = self._console.stop_app(package, index=self.index)
        if not res.ok:
            raise InstanceError(f"killapp failed: {res.text or res.stderr}")

    def uninstall_app(self, package: str) -> None:
        res = self._console.uninstall_app(package, index=self.index)
        if not res.ok:
            raise InstanceError(f"uninstall failed: {res.text or res.stderr}")

    def __repr__(self) -> str:
        return f"Instance(name={self.name!r}, index={self.index})"


# ---------------------------------------------------------------------------
# module-level helpers
# ---------------------------------------------------------------------------

BUNDLE_SUFFIXES = {".apkm", ".xapk", ".apks"}
TEMP_BUNDLE_DIRS: list[Path] = []


def _aapt_path() -> Path | None:
    console = find_ldconsole()
    if console:
        cand = console.parent / "aapt.exe"
        if cand.is_file():
            return cand
    return None


def _package_from_aapt(apk: Path) -> str | None:
    aapt = _aapt_path()
    if not aapt:
        return None
    try:
        out = subprocess.run(
            [str(aapt), "dump", "badging", str(apk)],
            capture_output=True, text=True, timeout=60, check=False,
        ).stdout
    except Exception:
        return None
    m = re.search(r"package:\s*name='([^']+)'", out)
    return m.group(1) if m else None


def _package_from_manifest(bundle_dir: Path) -> str | None:
    """Read package name from info.json (APKMirror) or manifest.json (APKPure)."""
    for fname in ("info.json", "manifest.json"):
        info = bundle_dir / fname
        if not info.is_file():
            continue
        try:
            import json
            data = json.loads(info.read_text(encoding="utf-8"))
            pkg = data.get("pname") or data.get("package_name")
            if pkg:
                return pkg
        except Exception:
            continue
    return None


def _guess_package(apk: Path) -> str:
    """Best-effort package name from the APK file name."""
    name = apk.stem
    # take the leading reverse-domain part, drop the _version/_dpi suffix
    m = re.match(r"^([a-zA-Z0-9]+(\.[a-zA-Z0-9]+)+)", name)
    if m:
        return m.group(1).lower()
    name = name.replace("_", ".").replace("-", ".").replace("+", ".")
    name = re.sub(r"[^a-zA-Z0-9.]", ".", name).strip(".")
    return name.lower()


def _package_name(apk: Path) -> str | None:
    """Best-effort package name for an apk / bundle (aapt > info.json > name)."""
    if apk.suffix.lower() in BUNDLE_SUFFIXES:
        extracted = _prepare_bundle(apk)
        try:
            pkg = _package_from_manifest(extracted[0].parent)
            if pkg:
                return pkg
            for part in extracted:
                pkg = _package_from_aapt(part)
                if pkg:
                    return pkg
        finally:
            _cleanup_bundle(apk, extracted)
        return _guess_package(apk)
    pkg = _package_from_aapt(apk)
    return pkg or _guess_package(apk)


def _prepare_bundle(apk: Path) -> list[Path]:
    """Return installable apk part(s). Plain .apk -> [path]. Bundle -> extract."""
    if apk.suffix.lower() not in BUNDLE_SUFFIXES:
        return [apk]

    dest = Path(tempfile.mkdtemp(prefix="ldcli_bundle_"))
    TEMP_BUNDLE_DIRS.append(dest)
    try:
        with zipfile.ZipFile(apk) as z:
            parts = [n for n in z.namelist() if n.lower().endswith(".apk")]
            if not parts:
                raise InstanceError(f"{apk.name} contains no .apk entries")
            z.extractall(dest)
    except zipfile.BadZipFile:
        raise InstanceError(f"{apk.name} is not a valid bundle zip")

    # order: main apk first, then split apks (manifest.json lists the splits)
    splits: set[str] = set()
    info = dest / "manifest.json"
    if info.is_file():
        try:
            import json
            data = json.loads(info.read_text(encoding="utf-8"))
            splits = {s.get("file", s) if isinstance(s, dict) else s
                      for s in data.get("split_apks", [])}
        except Exception:
            splits = set()

    def order_key(p: Path) -> tuple:
        in_split = p.name in splits
        return (in_split, p.name != "base.apk", p.name)

    return sorted((dest / p for p in parts), key=order_key)


def _sdk_hint(apk: Path, err: InstanceError) -> InstanceError:
    """Rewrite confusing install failures with a helpful SDK hint."""
    msg = str(err)
    if "OLDER_SDK" in msg or "OLDER_SDK" in msg.upper():
        return InstanceError(
            f"{msg}\n  => the app needs a newer Android than this instance "
            f"provides (Android 9 images run 'minSdk <= 28'; apps targeting "
            f"newer Android 13+ may need an LDPlayer 9 64-bit / Android 9+ "
            f"instance). Try `ldcli add <name> --android 9` if available.")
    return err


def _push_obb(self, bundle_dir: Path) -> None:
    """Push any .obb files from an extracted bundle to the app's obb dir."""
    obbs = list(bundle_dir.rglob("*.obb")) if bundle_dir.exists() else []
    if not obbs:
        return
    pkg = _package_name_from_dir(bundle_dir)
    remote = f"/sdcard/Android/obb/{pkg or 'unknown'}"
    self._adb.shell(self.index, f"mkdir -p {remote}", discover=True)
    for obb in obbs:
        self._adb.push(self.index, obb, f"{remote}/{obb.name}",
                       discover=True)
        print(f"[{self.name}] pushed obb {obb.name}")


def _package_name_from_dir(bundle_dir: Path) -> str | None:
    pkg = _package_from_manifest(bundle_dir)
    if pkg:
        return pkg
    for apk in bundle_dir.rglob("*.apk"):
        pkg = _package_from_aapt(apk)
        if pkg:
            return pkg
    return None


def _cleanup_bundle(apk: Path, parts: list[Path]) -> None:
    if apk.suffix.lower() not in BUNDLE_SUFFIXES:
        return
    for tmp in TEMP_BUNDLE_DIRS:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    TEMP_BUNDLE_DIRS.clear()


def list_instances(console: LdConsole) -> list[LdInstance]:
    return console.list_instances()


def create_instance(console: LdConsole, name: str, source: str | None = None,
                    cpu: int | None = None, memory: int | None = None,
                    resolution: str | None = None) -> LdInstance:
    if console.find(name=name):
        raise InstanceError(f"instance '{name}' already exists")
    if source:
        res = console.copy(name, source_name=source)
        # LDPlayer 9.5.31.0 returns a non-zero exit code from `copy` even on
        # success, so verify by instance presence rather than exit code.
        inst = console.find(name=name)
        if not inst:
            raise InstanceError(f"create failed: {res.text or res.stderr}")
    else:
        res = console.add(name)
        inst = console.find(name=name)
        if not inst or not res.ok:
            raise InstanceError(f"create failed: {res.text or res.stderr}")
    # every instance defaults to 2 cores / 2 GB / phone portrait
    cpu = cpu if cpu is not None else 2
    memory = memory if memory is not None else 2048
    resolution = resolution or "720,1280,320"
    res = console.modify(name=name, cpu=cpu, memory=memory,
                         resolution=resolution)
    if not res.ok:
        raise InstanceError(f"modify after create failed: "
                            f"{res.text or res.stderr}")
    print(f"created instance '{name}' (index {inst.index}) "
          f"cpu={cpu} mem={memory} res={resolution}"
          + (f" cloned from '{source}'" if source else ""))
    return inst
