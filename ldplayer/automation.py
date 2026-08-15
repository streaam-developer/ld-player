"""UI automation primitives layered on top of adb.

Provides coordinate actions, waits/polls, and reusable helper patterns
(screen info, screenshot, package/app checks).
"""

from __future__ import annotations

import time

from pathlib import Path

from .adb import Adb
from .console import LdConsole
from .instance import Instance


class AutomationError(RuntimeError):
    pass


class Waiter:
    """Poll a predicate until it succeeds or the deadline passes."""

    def __init__(self, timeout: float = 120, poll: float = 2.0):
        self.timeout = timeout
        self.poll = poll

    def until(self, predicate, description: str = "condition"):
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                if predicate():
                    return True
            except Exception:
                pass
            time.sleep(self.poll)
        raise AutomationError(f"timed out waiting for {description} "
                              f"({self.timeout}s)")


class Automator:
    """Coordinate-space actions against a specific instance."""

    def __init__(self, console: LdConsole, adb: Adb, instance: Instance):
        self.console = console
        self.adb = adb
        self.instance = instance

    # ------------------------------------------------------------ screen info
    def resolution(self) -> tuple[int, int]:
        """Return (width, height) of the instance display."""
        out = self.adb.shell(self.instance.index,
                             ["wm", "size"], timeout=20, discover=True)
        for line in out.splitlines():
            if "Physical size" in line:
                dims = line.split(":")[-1].strip().split("x")
                return int(dims[0]), int(dims[1])
        raise AutomationError(f"could not read resolution: {out}")

    # --------------------------------------------------------------- actions
    def tap(self, x: int, y: int, wait: float = 0.5) -> None:
        self.adb.input_tap(self.instance.index, x, y)
        time.sleep(wait)

    def tap_center(self, wait: float = 0.5) -> None:
        w, h = self.resolution()
        self.tap(w // 2, h // 2, wait)

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration_ms: int = 200, wait: float = 0.5) -> None:
        self.adb.input_swipe(self.instance.index, x1, y1, x2, y2, duration_ms)
        time.sleep(wait)

    def type_text(self, text: str) -> None:
        self.adb.input_text(self.instance.index, text)

    def key(self, keycode: int) -> None:
        self.adb.keyevent(self.instance.index, keycode)

    def home(self) -> None:
        self.key(3)

    def back(self) -> None:
        self.key(4)

    def enter(self) -> None:
        self.key(66)

    def screenshot(self, dest: str | Path) -> Path:
        return self.adb.screencap(self.instance.index, dest)

    # ---------------------------------------------------------------- checks
    def focused_activity(self) -> str | None:
        return self.adb.focused_activity(self.instance.index)

    def package_installed(self, package: str) -> bool:
        return self.adb.package_installed(self.instance.index, package)

    def app_running(self, package: str) -> bool:
        return self.adb.app_running(self.instance.index, package)

    def wait_for_app(self, package: str, timeout: float = 120) -> None:
        Waiter(timeout).until(lambda: self.package_installed(package),
                              f"package {package}")

    def wait_for_focus(self, activity_fragment: str, timeout: float = 120) -> None:
        def pred():
            focus = self.focused_activity()
            return focus and activity_fragment in focus
        Waiter(timeout, poll=1.0).until(
            pred, f"focus containing '{activity_fragment}'")

    # ------------------------------------------------------------ convenience
    def wait_and_tap(self, activity_fragment: str, x: int, y: int,
                     timeout: float = 120) -> None:
        """Wait until a screen matches, then tap at (x, y)."""
        self.wait_for_focus(activity_fragment, timeout)
        self.tap(x, y)

    def back_to_home(self) -> None:
        self.home()
        time.sleep(1)
