"""adb.exe wrapper with per-instance targeting and automatic port discovery.

LDPlayer maps each instance to a local adb port. The conventional mapping is
``5555 + index*2``, but LDPlayer 9.5.31.0 has been observed registering the
first instance as ``emulator-5554``, so we discover the live serial by
probing candidate ports and confirming the device is LDPlayer.

LDPlayer 14 (Android 14) keeps the ``emulator-<port>`` convention but its
adb channel silently drops every few minutes even while the instance keeps
running. Poking it through ldconsole (``ldconsole adb --index N``) makes the
emulator re-register with the adb server, so failing commands are retried
once behind that kick.
"""

from __future__ import annotations

import re
import subprocess
import time

from pathlib import Path


class AdbError(RuntimeError):
    pass


#: error fragments meaning the per-instance adb transport went away
_TRANSPORT_LOST_RE = re.compile(
    r"device ('[^']*' )?(?:not found|offline)"
    r"|device still connecting"
    r"|error: closed"
    r"|closed connection"
    r"|transport.*closed",
    re.IGNORECASE,
)


class Adb:
    def __init__(self, adb: str | Path, host: str = "127.0.0.1",
                 base_port: int = 5555, timeout: int = 120,
                 ldconsole: str | Path | None = None):
        self.adb = str(adb)
        self.host = host
        self.base_port = base_port
        self.timeout = timeout
        self.ldconsole = str(ldconsole) if ldconsole else self._default_console()
        self._ports: dict[int, str | None] = {}

    @staticmethod
    def _default_console() -> str | None:
        """Saved ldconsole path — used to kick dropped transports."""
        try:
            from .config import load_config
            return load_config().get("ldconsole")
        except Exception:  # noqa: BLE001
            return None

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
        """True if ``serial`` is a usable Android device.

        LDPlayer images report all kinds of manufacturers ("Google Phone",
        "samsung", sometimes empty), so name-based filtering misfires both
        ways.  Instead we simply require the device to answer a shell
        command — emulator console ports (5554, 5556, ...) accept TCP but
        fail here, which is exactly what we want to reject.
        """
        try:
            out = self._run(["-s", serial, "shell", "echo", "ok"], timeout=15)
            return "ok" in out
        except AdbError:
            return False

    def discover(self, index: int) -> str:
        """Return a working serial for the instance, caching the result.

        Order (all cheap checks first, so a half-open port can't stall
        discovery for minutes):
          1. cached serial (verify + drop stale transports explicitly)
          2. the auto-detected console serial expected for this index
             (``emulator-5554`` for index 0, ``emulator-5556`` for index 1…)
          3. a fresh ``adb connect`` to the conventional TCP endpoint
          4. nearby candidate ports — only after a sub-second TCP probe
             says something is actually listening there
          5. any already-connected device (last resort)
        """
        cached = self._ports.get(index)
        if cached:
            try:
                self._run(["-s", cached, "shell", "echo", "ok"], timeout=10)
                return cached
            except AdbError:
                # clear the stale transport so it can't wedge later connects
                try:
                    self._run(["disconnect", cached], timeout=10)
                except AdbError:
                    pass
                self._ports[index] = None

        devices = self._devices()

        # LDPlayer instances register with adb like stock emulators:
        # index i -> console port 5554+2i -> serial "emulator-<port>"
        expected_console = f"emulator-{self.port_for(index) - 1}"
        if expected_console in devices and self._looks_like_ldplayer(expected_console):
            self._ports[index] = expected_console
            return expected_console

        # the conventional per-index port next, so several running
        # instances never get cross-wired to each other's screens
        primary = self.endpoint(index)
        try:
            self._run(["connect", primary], timeout=8)
            if self._looks_like_ldplayer(primary):
                self._ports[index] = primary
                return primary
        except AdbError:
            pass

        for port in self._candidate_ports(index):
            if port == self.port_for(index):
                continue
            if not self._port_listening(port):
                continue
            serial = self._serial_for_port(port)
            try:
                self._run(["connect", serial], timeout=8)
            except AdbError:
                continue
            if self._looks_like_ldplayer(serial):
                self._ports[index] = serial
                return serial

        # last resort: any already-connected device
        for serial in devices:
            if self._looks_like_ldplayer(serial):
                self._ports[index] = serial
                return serial

        raise AdbError(f"no adb device found for LDPlayer instance index {index}")

    def _port_listening(self, port: int) -> bool:
        """Sub-second TCP probe so dead ports never reach slow adb calls."""
        import socket
        try:
            with socket.create_connection((self.host, port), timeout=0.5):
                return True
        except OSError:
            return False

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

    def wait_ready(self, index: int, timeout: float = 300,
                   poll: float = 10) -> str:
        """Poll until an adb serial for the instance becomes available."""
        deadline = time.time() + timeout
        last_err: AdbError | None = None
        tick = 0
        while time.time() < deadline:
            try:
                return self.discover(index)
            except AdbError as e:
                last_err = e
                tick += 1
                if tick % 6 == 0:
                    print(f"  ...still waiting for adb "
                          f"({int(deadline - time.time())}s left)")
                time.sleep(poll)
        raise AdbError(
            f"adb device for LDPlayer index {index} not available within "
            f"{timeout}s (last: {last_err})")

    # -------------------------------------------------------------- recovery
    @staticmethod
    def _transport_lost(exc: Exception) -> bool:
        return bool(_TRANSPORT_LOST_RE.search(str(exc)))

    def _recover(self, index: int) -> None:
        """Best effort: make the emulator re-register with the adb server.

        Order matters and every step is allowed to fail silently — any one
        of them is usually enough on LDPlayer 14:
          1. ``adb reconnect offline`` revives zombie transports
          2. an ldconsole adb poke re-registers the emulator channel
          3. a fresh ``devices`` listing triggers the server-side rescan
        """
        self._ports[index] = None
        try:
            self._run(["reconnect", "offline"], timeout=10)
        except AdbError:
            pass
        if self.ldconsole:
            try:
                subprocess.run(
                    [self.ldconsole, "adb", "--index", str(index),
                     "--command", "get-state"],
                    capture_output=True, timeout=30, check=False,
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            self._run(["devices"], timeout=15)
        except AdbError:
            pass

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
        try:
            return self._run(
                ["-s", self._serial(index, discover), "shell"] + cmd,
                timeout,
            )
        except AdbError as exc:
            if not self._transport_lost(exc):
                raise
            self._recover(index)
            return self._run(
                ["-s", self._serial(index, True), "shell"] + cmd,
                timeout,
            )

    def exec_out(self, index: int, cmd: list[str],
                 timeout: int | None = None) -> bytes:
        def _once(serial: str) -> bytes:
            proc = subprocess.run(
                [self.adb, "-s", serial, "exec-out"] + cmd,
                capture_output=True, timeout=timeout or self.timeout,
                check=False,
            )
            if proc.returncode != 0:
                raise AdbError(f"adb exec-out failed: "
                               f"{proc.stderr.decode(errors='replace')}")
            return proc.stdout

        try:
            return _once(self._serial(index))
        except (AdbError, subprocess.TimeoutExpired) as exc:
            if not (isinstance(exc, subprocess.TimeoutExpired)
                    or self._transport_lost(exc)):
                raise
            self._recover(index)
            return _once(self._serial(index))

    def push(self, index: int, local: str | Path, remote: str,
             discover: bool = False, timeout: int | None = None) -> str:
        return self._run(
            ["-s", self._serial(index, discover), "push", str(local), remote],
            timeout=timeout or 600)

    def install_multiple(self, index: int, apks: list[str | Path],
                         discover: bool = True) -> None:
        """Install split APKs together (base + config splits, same signature).

        Parts are pushed to the device's own storage first (each push retried
        individually so a flaky adb bridge can't lose the whole transfer),
        then installed locally with a single short `pm install-multiple`.
        """
        if len(apks) == 1:
            self._install_one(index, apks[0], discover)
            return

        remote_dir = "/data/local/tmp/ldcli"
        self.shell(index, ["mkdir", "-p", remote_dir], timeout=30,
                   discover=discover)
        remote_paths = []
        for apk in apks:
            remote = f"{remote_dir}/{Path(apk).name}"
            self._push_retry(index, apk, remote, discover)
            remote_paths.append(remote)

        try:
            out = self.shell(index, ["pm", "install-multiple", "-t"] +
                             remote_paths, timeout=300, discover=discover)
            if "Success" not in out:
                raise AdbError(
                    f"pm install-multiple did not report Success: {out.strip()}")
        finally:
            self.shell(index, ["rm", "-rf", remote_dir], timeout=30,
                       discover=discover)

    def _install_one(self, index: int, apk: str | Path,
                     discover: bool = True) -> None:
        remote = f"/data/local/tmp/{Path(apk).name}"
        self._push_retry(index, apk, remote, discover)
        try:
            out = self.shell(index, ["pm", "install", "-t", remote],
                             timeout=300, discover=discover)
            if "Success" not in out:
                raise AdbError(f"pm install did not report Success: {out.strip()}")
        finally:
            self.shell(index, ["rm", "-f", remote], timeout=30, discover=discover)

    def _push_retry(self, index: int, apk: str | Path, remote: str,
                    discover: bool, attempts: int = 8) -> None:
        """Push one file, retrying across connection windows."""
        last: AdbError | None = None
        for i in range(attempts):
            try:
                self.push(index, apk, remote, discover=discover)
                return
            except AdbError as e:
                last = e
                print(f"  push attempt {i + 1}/{attempts} interrupted "
                      f"({Path(apk).name}); retrying...")
                time.sleep(3)
        raise AdbError(f"could not push {Path(apk).name} to device: {last}")

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
        """Type into the focused field.

        adb joins argv with spaces and hands the result to the device's
        /system/bin/sh, so unquoted metacharacters (<, >, ;, |, (, ),
        ^ …) explode — e.g. ``input text ab<x`` tries to redirect stdin.
        Single-quoting the payload makes sh treat everything literally;
        embedded single quotes are escaped sh-style. Spaces ride along
        fine inside quotes (no %s gymnastics needed).
        """
        quoted = "'" + text.replace("'", "'\\''") + "'"
        return self.shell(index, ["input", "text", quoted], discover=discover)

    def keyevent(self, index: int, keycode: int, discover: bool = True) -> str:
        return self.shell(index, ["input", "keyevent", str(keycode)],
                          discover=discover)

    def wake(self, index: int, discover: bool = True) -> None:
        self.keyevent(index, 224, discover)   # WAKEUP
        self.keyevent(index, 82, discover)    # MENU (unlock)

    def is_boot_completed(self, index: int, discover: bool = True) -> bool:
        """True once Android is up. LDPlayer sometimes leaves
        ``sys.boot_completed`` empty, so also accept ``dev.bootcomplete`` and
        a responsive boot animation service as evidence."""
        try:
            for prop in ("sys.boot_completed", "dev.bootcomplete"):
                out = self.shell(index, ["getprop", prop],
                                 timeout=20, discover=discover)
                if out.strip() == "1":
                    return True
            out = self.shell(index, ["getprop", "init.svc.bootanim"],
                             timeout=20, discover=discover)
            return out.strip() == "stopped"
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
        # NOTE: LDPlayer 14 / Android 14 omits mCurrentFocus/mFocusedApp from
        # `dumpsys window windows`; the full `dumpsys window` dump carries
        # them on both v9 and v14.
        out = self.shell(index, ["dumpsys", "window"], timeout=30,
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
