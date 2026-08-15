"""Instance lifecycle: create, clone, launch, wait-for-boot, quit."""

from __future__ import annotations

import re
import time

from pathlib import Path

from .adb import Adb
from .config import load_config
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
    def launch(self, wait: bool = True, boot_wait: bool = True) -> "Instance":
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
            self.wait_for_boot(timeout=cfg["boot_timeout"])
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
        apk = Path(apk)
        if not apk.is_file():
            raise InstanceError(f"APK not found: {apk}")
        res = self._console.install_apk(apk, index=self.index)
        if not res.ok:
            raise InstanceError(f"install failed: {res.text or res.stderr}")
        print(f"[{self.name}] installed {apk.name}")

    def install_apk_wait(self, apk: str | Path,
                         timeout: float = 180) -> str | None:
        """Install and wait until the app's package registers. Returns pkg."""
        apk = Path(apk)
        if not apk.is_file():
            raise InstanceError(f"APK not found: {apk}")
        pkg = _guess_package(apk)
        self.install_apk(apk)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._adb.package_installed(self.index, pkg, discover=True):
                print(f"[{self.name}] package {pkg} present")
                return pkg
            time.sleep(3)
        raise InstanceError(f"package {pkg} did not appear within {timeout}s")

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

def _guess_package(apk: Path) -> str:
    """Best-effort package name from the APK file name (no aapt required)."""
    name = apk.stem
    name = name.replace("_", ".").replace("-", ".").replace("+", ".")
    name = re.sub(r"[^a-zA-Z0-9.]", ".", name).strip(".")
    return name.lower()


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
    if cpu is not None or memory is not None or resolution is not None:
        res = console.modify(name=name, cpu=cpu, memory=memory,
                             resolution=resolution)
        if not res.ok:
            raise InstanceError(f"modify after create failed: "
                                f"{res.text or res.stderr}")
    print(f"created instance '{name}' (index {inst.index})"
          + (f" cloned from '{source}'" if source else ""))
    return inst
