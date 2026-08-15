"""UI automation primitives layered on top of adb.

Provides coordinate actions, waits/polls, UI-tree text detection (uiautomator),
permission granting, and reusable helper patterns.
"""

from __future__ import annotations

import re
import time

from pathlib import Path

from .adb import Adb
from .console import LdConsole
from .instance import Instance

_NODE_RE = re.compile(r"<node\b[^>]*/>")


class AutomationError(RuntimeError):
    pass


class Waiter:
    """Poll a predicate until it succeeds or the deadline passes."""

    def __init__(self, timeout: float = 120, poll: float = 2.0,
                 label: str = "waiting"):
        self.timeout = timeout
        self.poll = poll
        self.label = label

    def until(self, predicate, description: str = "condition"):
        start = time.time()
        deadline = start + self.timeout
        last_tick = 0.0
        while time.time() < deadline:
            try:
                if predicate():
                    return True
            except Exception:
                pass
            now = time.time()
            if now - last_tick >= 10:
                last_tick = now
                print(f"  ... still {self.label} "
                      f"({now - start:.0f}/{self.timeout:.0f}s)", flush=True)
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

    # ------------------------------------------------------- UI-tree / text
    def dump_ui(self) -> str:
        """Return the current UI hierarchy XML (uiautomator dump)."""
        try:
            return self.adb.shell(self.instance.index,
                                  ["uiautomator", "dump", "/dev/tty"],
                                  timeout=30, discover=True)
        except Exception:
            self.adb.shell(self.instance.index,
                           ["uiautomator", "dump", "/sdcard/ldcli_ui.xml"],
                           timeout=30, discover=True)
            return self.adb.shell(self.instance.index,
                                  ["cat", "/sdcard/ldcli_ui.xml"],
                                  timeout=30, discover=True)

    def _text_nodes(self, xml: str) -> list[tuple[str, int, int]]:
        """Yield (text, cx, cy) for every UI node with text + bounds."""
        found = []
        for m in _NODE_RE.finditer(xml):
            node = m.group(0)
            t = re.search(r'text="([^"]*)"', node)
            b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
            if t and b and t.group(1).strip():
                cx = (int(b.group(1)) + int(b.group(3))) // 2
                cy = (int(b.group(2)) + int(b.group(4))) // 2
                found.append((t.group(1), cx, cy))
        return found

    def find_text(self, text: str) -> tuple[int, int] | None:
        """Find the center of a UI node whose text contains `text`."""
        try:
            xml = self.dump_ui()
        except Exception:
            return None
        low = text.lower()
        for label, cx, cy in self._text_nodes(xml):
            if low in label.lower():
                return cx, cy
        return None

    def wait_for_text(self, text: str, timeout: float = 120,
                      click: bool = False) -> tuple[int, int]:
        """Poll until `text` is on screen; optionally tap it. Returns (x, y)."""
        def pred():
            pos = self.find_text(text)
            return pos
        pos = Waiter(timeout, poll=2.0, label=f"looking for '{text}'").until(
            pred, f"text '{text}' on screen")
        if click:
            self.tap(*pos)
        return pos

    def grant_permission(self, package: str, permission: str) -> None:
        """Grant a runtime permission without touching the UI dialog."""
        self.adb.shell(self.instance.index,
                       ["pm", "grant", package, permission], timeout=30,
                       discover=True)

    # ------------------------------------------------------------ convenience
    def wait_and_tap(self, activity_fragment: str, x: int, y: int,
                     timeout: float = 120) -> None:
        """Wait until a screen matches, then tap at (x, y)."""
        self.wait_for_focus(activity_fragment, timeout)
        self.tap(x, y)

    def back_to_home(self) -> None:
        self.home()
        time.sleep(1)

    def wait_for_home(self, timeout: float = 180) -> None:
        """Wait until the instance is up and the launcher is focused.

        LDPlayer often never reports ``sys.boot_completed=1`` even though the
        system is fully up, so treat a focused launcher as proof of boot too —
        this is what the user observes (instance usable ~60s after launch).
        """
        def launcher_focused():
            focus = self.focused_activity()
            return focus and "launcher" in focus.lower()

        def booted_or_launcher():
            return (self.adb.is_boot_completed(self.instance.index,
                                               discover=True)
                    or launcher_focused())

        try:
            Waiter(timeout, poll=3.0, label="waiting for boot + launcher").until(
                booted_or_launcher, "Android boot completed / launcher focused")
        except AutomationError:
            # boot prop never flips; keep polling the launcher below
            pass
        self.home()
        def launcher():
            return launcher_focused()
        Waiter(timeout, poll=2.0, label="waiting for launcher").until(
            launcher, "launcher focused")
