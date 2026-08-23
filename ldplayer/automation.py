"""UI automation primitives layered on top of adb.

Provides coordinate actions, waits/polls, UI-tree text detection (uiautomator),
permission granting, and reusable helper patterns.
"""

from __future__ import annotations

import re
import time

from pathlib import Path

from .adb import Adb, AdbError
from .console import LdConsole
from .instance import Instance

_NODE_RE = re.compile(r"<node\b[^>]*>")

#: Where the UI hierarchy is staged on the device. /dev/tty is NOT usable on
#: LDPlayer builds — ``uiautomator dump /dev/tty`` exits 0 but the XML never
#: reaches stdout, so the dump must go to a file and be read back.
_UI_DUMP_PATH = "/sdcard/ldcli_ui.xml"

#: How long a dumped hierarchy stays fresh. Text lookups scan several labels
#: back-to-back; without this each lookup paid for its own slow dump.
_UI_CACHE_TTL = 1.2


def _parse_node_attrs(node_xml: str) -> dict[str, str]:
    """Extract all attributes from a single <node …/> XML fragment."""
    attrs: dict[str, str] = {}
    for m in re.finditer(r'(\w[\w-]*)="([^"]*)"', node_xml):
        attrs[m.group(1)] = m.group(2)
    return attrs


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
        """Poll until ``predicate()`` returns something truthy.

        Returns the predicate's value (not just ``True``) so callers can
        retrieve e.g. a found position or a matched label.
        """
        start = time.time()
        deadline = start + self.timeout
        last_tick = 0.0
        while time.time() < deadline:
            try:
                result = predicate()
                if result:
                    return result
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


class StepLogger:
    """Mixin for flows: ``step()`` printing with elapsed-time stamps.

    Every line carries the time spent in the PREVIOUS step and the TOTAL
    elapsed since flow start — e.g.
    ``[inst] (+12.3s | total 67.9s) opening Chrome ...``
    so slow steps are immediately visible in the console.
    """

    def init_steps(self) -> None:
        self.report: dict = {}
        self._t0: float | None = None
        self._prev_step_at: float | None = None

    def log_step(self, name: str, tag: str, msg: str,
                 quiet: bool = False) -> None:
        now = time.time()
        if self._t0 is None:
            self._t0 = now
            stamp = "[  start      ]"
        elif self._prev_step_at is not None:
            delta = now - self._prev_step_at
            total = now - self._t0
            stamp = f"[+{delta:6.1f}s | T{total:6.1f}s]"
        else:
            stamp = "[" + " " * 13 + "]"
        self._prev_step_at = now
        if not quiet:
            print(f"{name}: {stamp} {msg}", flush=True)
        self.report[tag] = {"time": now, "elapsed": (
            now - self._t0) if self._t0 else 0.0, "msg": msg}

    def summary(self, name: str) -> str:
        """One-line 'where did the time go' report of all logged steps."""
        if not self.report:
            return ""
        parts = []
        prev_t = None
        for tag, info in self.report.items():
            t = info["elapsed"]
            d = t - prev_t if prev_t is not None else t
            parts.append(f"{tag} {d:.0f}s")
            prev_t = t
        return f"{name}: step breakdown -> " + ", ".join(parts)


class Automator:
    """Coordinate-space actions against a specific instance."""

    def __init__(self, console: LdConsole, adb: Adb, instance: Instance):
        self.console = console
        self.adb = adb
        self.instance = instance
        self._ui_cache: tuple[float, str] | None = None
        self._anim_settled = False

    def _invalidate_ui(self) -> None:
        self._ui_cache = None

    def _settle_animations(self) -> None:
        """Disable global animations once per session.

        Facebook's Bloks screens run infinite spinners; uiautomator waits
        for the accessibility tree to go idle before dumping and otherwise
        stalls or reports "could not get idle state". Zeroing the animation
        scales removes most of that churn.
        """
        if self._anim_settled:
            return
        self._anim_settled = True
        for key in ("window_animation_scale", "transition_animation_scale",
                    "animator_duration_scale"):
            try:
                self.adb.shell(self.instance.index,
                               ["settings", "put", "global", key, "0"],
                               timeout=15, discover=True)
            except Exception:  # noqa: BLE001
                pass

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
        self._invalidate_ui()
        self.adb.input_tap(self.instance.index, x, y)
        time.sleep(wait)

    def tap_center(self, wait: float = 0.5) -> None:
        w, h = self.resolution()
        self.tap(w // 2, h // 2, wait)

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration_ms: int = 200, wait: float = 0.5) -> None:
        self._invalidate_ui()
        self.adb.input_swipe(self.instance.index, x1, y1, x2, y2, duration_ms)
        time.sleep(wait)

    def type_text(self, text: str) -> None:
        self._invalidate_ui()
        self.adb.input_text(self.instance.index, text)

    def clear_focused(self, keys: int = 30) -> None:
        """Blast DEL keyevents into the focused field — one adb call.

        Guarantees an empty field before typing, so retries never
        concatenate garbage into half-typed inputs.
        """
        self._invalidate_ui()
        self.adb.shell(self.instance.index,
                       [f"i=0; while [ $i -lt {keys} ]; do "
                        f"input keyevent 67; i=$((i+1)); done"],
                       timeout=30, discover=True)

    def fill_field(self, x: int, y: int, text: str,
                   wait: float = 0.4) -> None:
        """Tap a field, wipe it, type text into it."""
        self.tap(x, y, wait=wait)
        self.clear_focused()
        self.type_text(text)
        time.sleep(0.3)

    def key(self, keycode: int) -> None:
        self._invalidate_ui()
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
    def dump_ui(self, retries: int = 3, use_cache: bool = True) -> str:
        """Return the current UI hierarchy XML (uiautomator dump).

        The hierarchy is dumped to a file on the device and read back with
        ``exec-out cat`` (binary-safe). The destination is removed first so
        a failed dump can never serve the previous screen's XML, and every
        result is validated to actually contain nodes.

        A non-zero exit from ``uiautomator dump`` (e.g. "could not get idle
        state" while a spinner animates) does NOT abort the read — many
        builds still write the file, so we always try to cat it and accept
        whatever contains nodes.
        """
        now = time.time()
        if use_cache and self._ui_cache and now - self._ui_cache[0] < _UI_CACHE_TTL:
            return self._ui_cache[1]
        self._settle_animations()

        idx = self.instance.index
        last_err = ""
        for _ in range(retries):
            try:
                try:
                    self.adb.shell(idx, ["rm", "-f", _UI_DUMP_PATH],
                                   timeout=10, discover=True)
                except AdbError:
                    pass
                try:
                    out = self.adb.shell(
                        idx, ["uiautomator", "dump", _UI_DUMP_PATH],
                        timeout=25, discover=True)
                except AdbError as exc:
                    out = str(exc)
                raw = self.adb.exec_out(idx, ["cat", _UI_DUMP_PATH], timeout=20)
                xml = raw.decode("utf-8", errors="replace")
                if "<node" in xml:
                    self._ui_cache = (time.time(), xml)
                    return xml
                lines = out.strip().splitlines()
                last_err = lines[-1].strip() if lines else "dump had no nodes"
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
            time.sleep(1.2)
        raise AutomationError(
            f"uiautomator dump failed after {retries} attempts ({last_err})")

    def _text_nodes(self, xml: str) -> list[tuple[str, int, int, bool]]:
        """Yield (label, cx, cy, clickable) for every UI node carrying text.

        The label comes from ``text`` or, failing that, ``content-desc`` —
        Facebook's Bloks screens often expose labels only through
        content-desc. Every node tag is considered (container nodes too,
        not just self-closing leaves).
        """
        found = []
        for m in _NODE_RE.finditer(xml):
            node = m.group(0)
            b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
            if not b:
                continue
            t = re.search(r'\btext="([^"]*)"', node)
            d = re.search(r'\bcontent-desc="([^"]*)"', node)
            label = ""
            if t and t.group(1).strip():
                label = t.group(1)
            elif d and d.group(1).strip():
                label = d.group(1)
            if not label:
                continue
            cx = (int(b.group(1)) + int(b.group(3))) // 2
            cy = (int(b.group(2)) + int(b.group(4))) // 2
            clickable = 'clickable="true"' in node
            found.append((label, cx, cy, clickable))
        return found

    def _all_nodes(self, xml: str) -> list[dict]:
        """Return parsed attribute dicts for every <node> in the UI XML."""
        nodes = []
        for m in _NODE_RE.finditer(xml):
            attrs = _parse_node_attrs(m.group(0))
            if "bounds" in attrs:
                nodes.append(attrs)
        return nodes

    def _bounds_center(self, bounds_str: str) -> tuple[int, int] | None:
        """Parse '[x1,y1][x2,y2]' and return the center (cx, cy)."""
        b = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
        if not b:
            return None
        return ((int(b.group(1)) + int(b.group(3))) // 2,
                (int(b.group(2)) + int(b.group(4))) // 2)

    def find_edit_texts(self) -> list[tuple[int, int]]:
        """Return center coordinates of every EditText on screen."""
        try:
            xml = self.dump_ui()
        except Exception:
            return []
        centers = []
        for node in self._all_nodes(xml):
            cls = node.get("class", "")
            if "EditText" in cls:
                center = self._bounds_center(node.get("bounds", ""))
                if center:
                    centers.append(center)
        return centers

    def find_by_class(self, class_fragment: str) -> list[dict]:
        """Return attribute dicts for all nodes whose class contains the fragment."""
        try:
            xml = self.dump_ui()
        except Exception:
            return []
        low = class_fragment.lower()
        return [n for n in self._all_nodes(xml) if low in n.get("class", "").lower()]

    def find_by_resource_id(self, resource_id: str) -> tuple[int, int] | None:
        """Find a node by its resource-id and return its center."""
        try:
            xml = self.dump_ui()
        except Exception:
            return None
        for node in self._all_nodes(xml):
            if node.get("resource-id", "") == resource_id:
                return self._bounds_center(node.get("bounds", ""))
        return None

    def find_by_content_desc(self, desc: str) -> tuple[int, int] | None:
        """Find a node by its content-desc and return its center."""
        try:
            xml = self.dump_ui()
        except Exception:
            return None
        low = desc.lower()
        for node in self._all_nodes(xml):
            if low in node.get("content-desc", "").lower():
                return self._bounds_center(node.get("bounds", ""))
        return None

    def scroll_down(self, fraction: float = 0.4, duration_ms: int = 600) -> None:
        """Scroll from bottom-third to top-third of screen."""
        w, h = self.resolution()
        cx = w // 2
        y_start = int(h * 0.7)
        y_end = int(h * 0.3)
        self.swipe(cx, y_start, cx, y_end, duration_ms=duration_ms, wait=1.0)

    def scroll_up(self, fraction: float = 0.4, duration_ms: int = 600) -> None:
        """Scroll from top-third to bottom-third of screen."""
        w, h = self.resolution()
        cx = w // 2
        y_start = int(h * 0.3)
        y_end = int(h * 0.7)
        self.swipe(cx, y_start, cx, y_end, duration_ms=duration_ms, wait=1.0)

    def find_text(self, text: str) -> tuple[int, int] | None:
        """Find the center of a UI node whose text (or content-desc)
        contains `text`. Clickable matches win over plain labels."""
        try:
            xml = self.dump_ui()
        except Exception:
            return None
        low = text.lower()
        fallback: tuple[int, int] | None = None
        for label, cx, cy, clickable in self._text_nodes(xml):
            if low in label.lower():
                if clickable:
                    return cx, cy
                fallback = fallback or (cx, cy)
        return fallback

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
        # LDPlayer launchers may appear under various names
        _LAUNCHER_KEYWORDS = ("launcher", "leidian", "daemon",
                              "systemui", "home", "launcher3")

        def launcher_focused():
            focus = self.focused_activity()
            if not focus:
                return False
            low = focus.lower()
            return any(kw in low for kw in _LAUNCHER_KEYWORDS)

        def device_responsive():
            """Fallback: if adb responds at all, the device is up."""
            try:
                out = self.adb.shell(self.instance.index,
                                     ["echo", "ok"], timeout=10,
                                     discover=True)
                return "ok" in out
            except Exception:
                return False

        def booted_or_ready():
            if launcher_focused():
                return True
            if self.adb.is_boot_completed(self.instance.index,
                                          discover=True):
                return True
            # plain responsiveness alone proves nothing during the first
            # seconds: adbd answers long before PackageManager works,
            # which made downstream 'pm' checks see an empty app list
            if time.time() - phase_t0 > 30 and device_responsive():
                return True
            return False

        phase_t0 = time.time()
        # Phase 1: wait for boot or launcher (uses most of the timeout)
        boot_timeout = timeout * 0.7
        try:
            Waiter(boot_timeout, poll=3.0,
                   label="waiting for boot + launcher").until(
                booted_or_ready,
                "Android boot completed / launcher focused")
        except AutomationError:
            pass

        # Phase 2: press home and confirm launcher is visible
        self.home()
        try:
            Waiter(timeout * 0.3, poll=2.0,
                   label="waiting for launcher").until(
                lambda: launcher_focused() or device_responsive(),
                "launcher focused / device responsive")
        except AutomationError:
            # Last resort: if the device is alive at all, carry on
            if not device_responsive():
                raise
