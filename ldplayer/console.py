"""Thin, well-typed wrapper around LDPlayer 9's ldconsole.exe.

Verified against LDPlayer v9.5.31.0. This version's command set differs from
older docs:
  * instance list  -> ``list2`` (CSV: index,name,pid,...)
  * full backup    -> ``backup`` / ``restore`` with *.ldbk files
  * per-app backup -> ``backupapp`` / ``restoreapp``
  * install        -> ``installapp``; force-stop -> ``killapp``
  * no ``export``/``import``/``screencap`` (screencap goes via adb)
"""

from __future__ import annotations

import shlex
import subprocess
import time

from dataclasses import dataclass
from pathlib import Path


class LdConsoleError(RuntimeError):
    pass


@dataclass(frozen=True)
class LdInstance:
    index: int
    name: str
    pid: int = 0
    width: int = 0
    height: int = 0
    dpi: int = 0
    raw: str = ""

    @property
    def running(self) -> bool:
        return self.pid > 0


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def text(self) -> str:
        return self.stdout.strip()


class LdConsole:
    """Wraps ``ldconsole.exe <command> <args>`` invocations.

    Targets an instance by ``name`` or ``index``. ``--index`` is the most
    reliable handle on this version; names are display titles.
    """

    def __init__(self, console: str | Path, timeout: int = 300,
                 base_port: int = 5555):
        self.console = str(console)
        self.timeout = timeout
        self.base_port = base_port

    # ------------------------------------------------------------------ core
    def run(self, args: list[str], timeout: int | None = None) -> RunResult:
        cmd = [self.console] + args
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                check=False,
            )
        except FileNotFoundError:
            raise LdConsoleError(f"ldconsole.exe not found: {self.console}")
        except subprocess.TimeoutExpired:
            raise LdConsoleError(
                f"ldconsole timed out after {timeout or self.timeout}s: "
                + " ".join(shlex.quote(str(a)) for a in args)
            )
        return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")

    def _target(self, name: str | None, index: int | None) -> list[str]:
        if index is not None:
            return ["--index", str(index)]
        if name is not None:
            return ["--name", name]
        return []

    # ------------------------------------------------------------------ list
    def list_instances(self) -> list[LdInstance]:
        """``list2`` returns: index,name,pid,?,?,?,?,width,height,dpi"""
        res = self.run(["list2"])
        instances: list[LdInstance] = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line or not "," in line:
                continue
            fields = line.split(",")
            try:
                index = int(fields[0].strip())
                name = fields[1].strip()
            except (IndexError, ValueError):
                continue
            pid = 0
            try:
                pid = int(fields[2].strip())
            except (IndexError, ValueError):
                pass
            w = h = dpi = 0
            try:
                w, h, dpi = (int(fields[7]), int(fields[8]), int(fields[9]))
            except (IndexError, ValueError):
                pass
            instances.append(LdInstance(index, name, pid, w, h, dpi, line))
        return instances

    def running_instances(self) -> list[int]:
        """Indexes of currently running instances (``runninglist``)."""
        res = self.run(["runninglist"])
        out = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
        indexes: list[int] = []
        for item in out:
            for token in item.replace(",", " ").split():
                try:
                    indexes.append(int(token))
                except ValueError:
                    continue
        return indexes

    def find(self, name: str | None = None, index: int | None = None
             ) -> LdInstance | None:
        instances = self.list_instances()
        for inst in instances:
            if index is not None and inst.index == index:
                return inst
            if name is not None and inst.name == name:
                return inst
        return None

    def is_running(self, name: str | None = None,
                   index: int | None = None) -> bool:
        res = self.run(["isrunning"] + self._target(name, index))
        return res.text.lower() == "running"

    # --------------------------------------------------------------- lifecycle
    def launch(self, name: str | None = None, index: int | None = None) -> RunResult:
        return self.run(["launch"] + self._target(name, index))

    def quit(self, name: str | None = None, index: int | None = None) -> RunResult:
        return self.run(["quit"] + self._target(name, index))

    def quit_all(self) -> RunResult:
        return self.run(["quitall"])

    def reboot(self, name: str | None = None, index: int | None = None) -> RunResult:
        return self.run(["reboot"] + self._target(name, index))

    def add(self, name: str) -> RunResult:
        return self.run(["add", "--name", name])

    def copy(self, new_name: str, source_name: str | None = None,
             source_index: int | None = None) -> RunResult:
        args = ["copy", "--name", new_name]
        if source_index is not None:
            args += ["--from", str(source_index)]
        elif source_name is not None:
            args += ["--from", source_name]
        return self.run(args)

    def modify(self, *, name: str | None = None, index: int | None = None,
               cpu: int | None = None, memory: int | None = None,
               resolution: str | None = None, root: int | None = None,
               manufacturer: str | None = None, model: str | None = None,
               pnumber: str | None = None, imei: str | None = None,
               imsi: str | None = None, simserial: str | None = None,
               androidid: str | None = None, mac: str | None = None,
               autorotate: int | None = None,
               lockwindow: int | None = None) -> RunResult:
        args = ["modify"] + self._target(name, index)
        if cpu is not None:
            args += ["--cpu", str(cpu)]
        if memory is not None:
            args += ["--memory", str(memory)]
        if resolution is not None:
            args += ["--resolution", resolution]
        if root is not None:
            args += ["--root", str(root)]
        if manufacturer is not None:
            args += ["--manufacturer", manufacturer]
        if model is not None:
            args += ["--model", model]
        if pnumber is not None:
            args += ["--pnumber", pnumber]
        if imei is not None:
            args += ["--imei", imei]
        if imsi is not None:
            args += ["--imsi", imsi]
        if simserial is not None:
            args += ["--simserial", simserial]
        if androidid is not None:
            args += ["--androidid", androidid]
        if mac is not None:
            args += ["--mac", mac]
        if autorotate is not None:
            args += ["--autorotate", str(autorotate)]
        if lockwindow is not None:
            args += ["--lockwindow", str(lockwindow)]
        return self.run(args)

    def global_setting(self, *, fps: int | None = None, audio: int | None = None,
                       fastplay: int | None = None,
                       cleanmode: int | None = None) -> RunResult:
        """Apply emulator-wide settings (all instances)."""
        args = ["globalsetting"]
        if fps is not None:
            args += ["--fps", str(fps)]
        if audio is not None:
            args += ["--audio", str(audio)]
        if fastplay is not None:
            args += ["--fastplay", str(fastplay)]
        if cleanmode is not None:
            args += ["--cleanmode", str(cleanmode)]
        return self.run(args)

    def remove(self, name: str | None = None, index: int | None = None) -> RunResult:
        return self.run(["remove"] + self._target(name, index))

    def rename(self, title: str, name: str | None = None,
               index: int | None = None) -> RunResult:
        return self.run(["rename"] + self._target(name, index) +
                        ["--title", title])

    # ------------------------------------------------------------------- apps
    def install_apk(self, apk: str | Path, name: str | None = None,
                    index: int | None = None) -> RunResult:
        return self.run(
            ["installapp"] + self._target(name, index) +
            ["--filename", str(apk)]
        )

    def uninstall_app(self, package: str, name: str | None = None,
                      index: int | None = None) -> RunResult:
        return self.run(
            ["uninstallapp"] + self._target(name, index) +
            ["--packagename", package]
        )

    def run_app(self, package: str, name: str | None = None,
                index: int | None = None) -> RunResult:
        return self.run(
            ["runapp"] + self._target(name, index) +
            ["--packagename", package]
        )

    def stop_app(self, package: str, name: str | None = None,
                 index: int | None = None) -> RunResult:
        return self.run(
            ["killapp"] + self._target(name, index) +
            ["--packagename", package]
        )

    def launch_ex(self, package: str, name: str | None = None,
                  index: int | None = None) -> RunResult:
        return self.run(
            ["launchex"] + self._target(name, index) +
            ["--packagename", package]
        )

    # ------------------------------------------------------------- automation
    def action(self, key: str, value: str = "", *, name: str | None = None,
               index: int | None = None) -> RunResult:
        args = ["action"] + self._target(name, index) + ["--key", key]
        if value:
            args += ["--value", value]
        return self.run(args)

    def adb(self, command: str, name: str | None = None,
            index: int | None = None) -> RunResult:
        return self.run(["adb"] + self._target(name, index) +
                        ["--command", command])

    def getprop(self, key: str | None = None, name: str | None = None,
                index: int | None = None) -> RunResult:
        args = ["getprop"] + self._target(name, index)
        if key:
            args += ["--key", key]
        return self.run(args)

    # ---------------------------------------------------------------- backup
    def backup_full(self, dest: str | Path, name: str | None = None,
                    index: int | None = None) -> RunResult:
        return self.run(["backup"] + self._target(name, index) +
                        ["--file", str(dest)])

    def restore_full(self, file: str | Path, name: str | None = None,
                     index: int | None = None) -> RunResult:
        return self.run(["restore"] + self._target(name, index) +
                        ["--file", str(file)])

    def backup_app(self, package: str, dest: str | Path,
                   name: str | None = None, index: int | None = None) -> RunResult:
        return self.run(
            ["backupapp"] + self._target(name, index) +
            ["--packagename", package, "--file", str(dest)]
        )

    def restore_app(self, package: str, file: str | Path,
                    name: str | None = None, index: int | None = None) -> RunResult:
        return self.run(
            ["restoreapp"] + self._target(name, index) +
            ["--packagename", package, "--file", str(file)]
        )

    def pull(self, remote: str, local: str | Path, name: str | None = None,
             index: int | None = None) -> RunResult:
        return self.run(
            ["pull"] + self._target(name, index) +
            ["--remote", remote, "--local", str(local)]
        )

    def push(self, local: str | Path, remote: str, name: str | None = None,
             index: int | None = None) -> RunResult:
        return self.run(
            ["push"] + self._target(name, index) +
            ["--local", str(local), "--remote", remote]
        )

    # --------------------------------------------------------------- helpers
    def wait_until_running(self, name: str | None = None, index: int | None = None,
                           timeout: float = 60, poll: float = 2.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_running(name=name, index=index):
                return
            time.sleep(poll)
        raise LdConsoleError(f"instance (name={name}, index={index}) did not "
                             f"start within {timeout}s")

    def wait_until_quit(self, name: str | None = None, index: int | None = None,
                        timeout: float = 60, poll: float = 2.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_running(name=name, index=index):
                return
            time.sleep(poll)
        raise LdConsoleError(f"instance (name={name}, index={index}) did not "
                             f"quit within {timeout}s")
