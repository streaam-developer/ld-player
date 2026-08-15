"""adb.exe wrapper with per-instance targeting and automatic port discovery.

LDPlayer maps each instance to a local adb port. The conventional mapping is
``5555 + index*2``, but LDPlayer 9.5.31.0 has been observed registering the
first instance as ``emulator-5554``, so we discover the live serial by
probing candidate ports and confirming the device is LDPlayer.
"""

from __future__ import annotations

import subprocess
import time

from pathlib import Path


class AdbError(RuntimeError):
    pass


class Adb:
    def __init__(self, adb: str | Path, host: str = "127.0.0.1",
                 base_port: int = 5555, timeout: int = 120):
        self.adb = str(adb)
        self.host = host
        self.base_port = base_port
        self.timeout = timeout
        self._ports: dict[int, str | None] = {}

    # --------------------------------------------------------------- discovery
    def port_for(self, index: int) -> int:
        return self.base_port + index * 2

    def endpoint(self, index: int) -> str:
        return f"{self.host}:{self.port_for(index)}"

    def _candidate_ports(self, index: int) -> list[int]:
        primary = self.port_for(index)
        ports = [primary, primary - 1, primary + 1]
        for p in range(self.base_port, self.base_port + 16):
            if p not in ports:
                ports.append(p)
        return ports

    def _serial_for_port(self, port: int) -> str:
        if port == self.base_port + 16:  # degenerate; never used
            return f"emulator-{port}"
        return f"{self.host}:{port}"

    def _looks_like_ldplayer(self, serial: str) -> bool:
        try:
            out = self._run(
                ["-s", serial, "shell", "getprop", "ro.product.manufacturer"],
                timeout=15,
            )
            return "ld" in out.strip().lower() or "china" in out.strip().lower()
        except AdbError:
            # Some LDPlayer images return empty; accept any alive device.
            return True

    def discover(self, index: int) -> str:
        """Return a working serial for the instance, caching the result."""
        cached = self._ports.get(index)
        if cached:
            try:
                self._run(["-s", cached, "shell", "echo", "ok"], timeout=15)
                return cached
            except AdbError:
                self._ports[index] = None

        # already-connected emulator serials take priority
        serials = self._devices()
        if serials:
            for serial in serials:
                if self._looks_like_ldplayer(serial):
                    self._ports[index] = serial
                    return serial

        for port in self._candidate_ports(index):
            serial = self._serial_for_port(port)
            try:
                self._run(["connect", serial], timeout=15)
            except AdbError:
                continue
            if self._looks_like_ldplayer(serial):
                self._ports[index] = serial
                return serial

        raise AdbError(f"no adb device found for LDPlayer instance index {index}")

    def _devices(self) -> list[str]:
        out = self._run(["devices"], timeout=15)
        return [ln.split("\t")[0] for ln in out.splitlines()[1:]
                if "\tdevice" in ln and not ln.startswith("*")]

    def connect(self, index: int) -> bool:
        try:
            self.discover(index)
            return True
        except AdbError:
            return False

    # ------------------------------------------------------------------ core
    def _run(self, args: list[str], timeout: int | None = None) -> str:
        cmd = [self.adb] + args
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout or self.timeout, check=False,
            )
        except FileNotFoundError:
            raise AdbError(f"adb.exe not found: {self.adb}")
        except subprocess.TimeoutExpired:
            raise AdbError(f"adb timed out: {' '.join(cmd)}")
        if proc.returncode != 0:
            raise AdbError(
                f"adb failed ({proc.returncode}): "
                f"{' '.join(cmd)}\n{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc.stdout or ""

    def _serial(self, index: int, discover: bool = True) -> str:
        if discover:
            return self.discover(index)
        return self.endpoint(index)

    def shell(self, index: int, cmd: list[str], timeout: int | None = None,
              discover: bool = False) -> str:
        return self._run(
            ["-s", self._serial(index, discover), "shell"] + cmd,
            timeout,
        )

    def exec_out(self, index: int, cmd: list[str],
                 timeout: int | None = None) -> bytes:
        proc = subprocess.run(
            [self.adb, "-s", self._serial(index), "exec-out"] + cmd,
            capture_output=True, timeout=timeout or self.timeout, check=False,
        )
        if proc.returncode != 0:
            raise AdbError(f"adb exec-out failed: "
                           f"{proc.stderr.decode(errors='replace')}")
        return proc.stdout

    def push(self, index: int, local: str | Path, remote: str,
             discover: bool = False) -> str:
        return self._run(
            ["-s", self._serial(index, discover), "push", str(local), remote])

    def install_multiple(self, index: int, apks: list[str | Path],
                         discover: bool = True) -> None:
        """Install split APKs together (base + config splits, same signature)."""
        if len(apks) == 1:
            self._run(["-s", self._serial(index, discover), "install", "-t",
                       str(apks[0])])
            return
        cmd = [self.adb, "-s", self._serial(index, discover),
               "install-multiple", "-t"] + [str(a) for a in apks]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=self.timeout, check=False)
        if proc.returncode != 0:
            raise AdbError(
                f"adb install-multiple failed ({proc.returncode}): "
                f"{proc.stdout.strip() or proc.stderr.strip()}")
        if "Success" not in proc.stdout:
            raise AdbError(f"adb install-multiple did not report Success: "
                           f"{proc.stdout.strip()}")

    def pull(self, index: int, remote: str, local: str | Path,
             discover: bool = False) -> str:
        return self._run(
            ["-s", self._serial(index, discover), "pull", remote, str(local)])

    def screencap(self, index: int, local_dest: str | Path) -> Path:
        local_dest = Path(local_dest)
        remote = f"/sdcard/_ldcli_tmp_{int(time.time())}.png"
        self.shell(index, ["screencap", "-p", remote], discover=True)
        self.pull(index, remote, local_dest, discover=True)
        self.shell(index, ["rm", "-f", remote], discover=True)
        return local_dest

    # --------------------------------------------------------------- actions
    def input_tap(self, index: int, x: int, y: int,
                  discover: bool = True) -> str:
        return self.shell(index, ["input", "tap", str(x), str(y)], discover=discover)

    def input_swipe(self, index: int, x1: int, y1: int, x2: int, y2: int,
                    duration_ms: int = 200, discover: bool = True) -> str:
        return self.shell(
            index, ["input", "swipe", str(x1), str(y1), str(x2), str(y2),
                    str(duration_ms)], discover=discover)

    def input_text(self, index: int, text: str, discover: bool = True) -> str:
        return self.shell(index, ["input", "text", text], discover=discover)

    def keyevent(self, index: int, keycode: int, discover: bool = True) -> str:
        return self.shell(index, ["input", "keyevent", str(keycode)],
                          discover=discover)

    def wake(self, index: int, discover: bool = True) -> None:
        self.keyevent(index, 224, discover)   # WAKEUP
        self.keyevent(index, 82, discover)    # MENU (unlock)

    def is_boot_completed(self, index: int, discover: bool = True) -> bool:
        try:
            out = self.shell(index, ["getprop", "sys.boot_completed"],
                             timeout=20, discover=discover)
            return out.strip() == "1"
        except AdbError:
            return False

    def package_installed(self, index: int, package: str,
                          discover: bool = True) -> bool:
        try:
            out = self.shell(index, ["pm", "list", "packages", package],
                             timeout=30, discover=discover)
            return f"package:{package}" in out
        except AdbError:
            return False

    def app_running(self, index: int, package: str,
                    discover: bool = True) -> bool:
        try:
            out = self.shell(index, ["pidof", package], timeout=20,
                             discover=discover)
            return bool(out.strip())
        except AdbError:
            return False

    def focused_activity(self, index: int, discover: bool = True) -> str | None:
        out = self.shell(index, ["dumpsys", "window", "windows"], timeout=30,
                         discover=discover)
        for line in out.splitlines():
            line = line.strip()
            if "mCurrentFocus" in line or "mFocusedApp" in line:
                return line
        return None

    # ---------------------------------------------------------------- backup
    def backup(self, index: int, package: str, dest: str | Path,
               include_apk: bool = True, include_shared: bool = False) -> Path:
        """adb backup (Android Backup format, fallback path)."""
        dest = Path(dest)
        if dest.suffix.lower() not in (".ab", ".backup"):
            dest = dest.with_suffix(".ab")
        args = ["backup", "-f", str(dest)]
        if include_apk:
            args.append("-apk")
        if include_shared:
            args.append("-shared")
        args.append(package)
        serial = self._serial(index, discover=True)
        proc = subprocess.Popen([self.adb, "-s", serial] + args)
        try:
            proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AdbError("adb backup timed out "
                           "(did the device confirm the backup?)")
        if proc.returncode != 0:
            raise AdbError(f"adb backup failed with code {proc.returncode}")
        return dest

    def restore(self, index: int, backup_file: str | Path) -> None:
        serial = self._serial(index, discover=True)
        proc = subprocess.Popen(
            [self.adb, "-s", serial, "restore", str(backup_file)])
        try:
            proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AdbError("adb restore timed out "
                           "(did the device confirm the restore?)")
        if proc.returncode != 0:
            raise AdbError(f"adb restore failed with code {proc.returncode}")
