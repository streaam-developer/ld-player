"""Find-an-app-and-open-it automation.

Opens an emulator instance, waits for the launcher, then locates the
target app the way a user would: scan what is on screen, peek inside
known folders ("System Apps"), pull up the app drawer, type into its
search box, and flip through remaining pages. Optionally falls back to
launching the package directly when every UI strategy fails.

Flow:
1. open the instance and wait until the launcher (apps grid) is showing
2. press HOME so we start from a known state
3. scan the home screen for the icon — including known system-app folders
4. swipe up to open the app drawer and rescan
5. tap the drawer search box, type the query, hide the keyboard, rescan
6. alternate horizontal page flips / vertical scrolls with rescans
7. tap the icon and confirm the package reached the foreground

Defaults target Google Chrome (`com.android.chrome`, label "Chrome");
both are configurable so any installed app can be opened the same way.
"""

from __future__ import annotations

import re
import time

from .adb import Adb
from .automation import Automator, AutomationError, Waiter
from .console import LdConsole
from .instance import Instance


DEFAULT_LABEL = "Chrome"
DEFAULT_PACKAGE = "com.android.chrome"

#: Folder names launchers group system apps under (English + Chinese)
FOLDER_NAMES = ["System Apps", "System apps", "系统应用"]

#: Hint / content-desc texts used by drawer + widget search boxes
SEARCH_BOX_HINTS = ["Search apps", "Search apps and games", "Search",
                    "搜索应用", "搜索"]

#: Page-flip attempts before declaring the icon unfindable
MAX_PAGE_FLIPS = 8

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


class AppSearchError(RuntimeError):
    pass


class AppSearchFlow:
    def __init__(self, console: LdConsole, adb: Adb, index: int | None = None,
                 name: str | None = None, label: str = DEFAULT_LABEL,
                 package: str = DEFAULT_PACKAGE):
        self.inst = Instance(console, adb, name=name, index=index)
        self.inst.resolve()
        self.label = label
        self.package = package
        self.auto = Automator(console, adb, self.inst)
        self.report: dict = {}
        #: folder labels already visited during this run
        self._folders_seen: set[str] = set()

    # ---------------------------------------------------------------- steps
    def step(self, tag: str, msg: str) -> None:
        print(f"[{self.inst.name}] {msg}", flush=True)
        self.report[tag] = {"time": time.time(), "msg": msg}

    def open_instance_and_home(self, timeout: float = 600) -> None:
        self.step("instance", f"checking {self.inst.name} status...")
        if not self.inst.running:
            self.step("instance",
                      f"{self.inst.name} is stopped — starting it now...")
            self.inst.launch(wait=True, boot_wait=False)
        else:
            self.step("instance", f"{self.inst.name} already running")
        self.step("instance", "waiting for boot + launcher (apps grid)...")
        self.auto.wait_for_home(timeout)
        self.go_home()

    def ensure_app_installed(self) -> None:
        if self.auto.package_installed(self.package):
            self.step("install", f"{self.package} is installed")
            return
        raise AppSearchError(
            f"{self.package} is not installed on {self.inst.name} — install "
            f"it first (e.g. `python ldcli.py setup --index {self.inst.index}`)"
            f" or point --package at another installed app")

    def go_home(self) -> None:
        self.auto.home()
        time.sleep(1.5)

    # ------------------------------------------------------------- scanning
    def _field_rects(self, xml: str) -> list[tuple[int, int, int, int]]:
        """(x1, y1, x2, y2) of every EditText — used to skip the search
        input itself, whose text equals the query once typed."""
        rects: list[tuple[int, int, int, int]] = []
        for node in self.auto._all_nodes(xml):
            if "EditText" in node.get("class", ""):
                b = _BOUNDS_RE.match(node.get("bounds", ""))
                if b:
                    x1, y1, x2, y2 = map(int, b.groups())
                    rects.append((x1, y1, x2, y2))
        return rects

    @staticmethod
    def _center_in_rects(cx: int, cy: int,
                         rects: list[tuple[int, int, int, int]]) -> bool:
        return any(x1 <= cx <= x2 and y1 <= cy <= y2
                   for x1, y1, x2, y2 in rects)

    def find_icon_on_screen(self) -> tuple[int, int] | None:
        """Center of a node whose label/content-desc contains the query,
        preferring clickable nodes and ignoring text fields."""
        try:
            xml = self.auto.dump_ui()
        except Exception:  # noqa: BLE001
            return None
        low = self.label.lower()
        rects = self._field_rects(xml)
        fallback: tuple[int, int] | None = None
        for label, cx, cy, clickable in self.auto._text_nodes(xml):
            if low not in label.lower():
                continue
            if self._center_in_rects(cx, cy, rects):
                continue
            if clickable:
                return cx, cy
            fallback = fallback or (cx, cy)
        return fallback

    def _scan_folders(self) -> tuple[int, int] | None:
        """Open known system-app folders one by one and rescan inside."""
        for folder in FOLDER_NAMES:
            if folder in self._folders_seen:
                continue
            pos = self.auto.find_text(folder)
            if not pos:
                continue
            self._folders_seen.add(folder)
            self.step("folder", f"opening '{folder}' at {pos}...")
            self.auto.tap(*pos, wait=1.5)
            found = self.find_icon_on_screen()
            if found:
                return found
            self.auto.back()
            time.sleep(1.0)
        return None

    def scan_current_screen(self) -> tuple[int, int] | None:
        return self.find_icon_on_screen() or self._scan_folders()

    # -------------------------------------------------------------- gestures
    def swipe_up_drawer(self) -> None:
        w, h = self.auto.resolution()
        x = w // 2
        self.auto.swipe(x, int(h * 0.88), x, int(h * 0.30),
                        duration_ms=300, wait=1.5)

    def flip_page(self) -> None:
        w, h = self.auto.resolution()
        self.auto.swipe(int(w * 0.80), int(h * 0.50), int(w * 0.15),
                        int(h * 0.50), duration_ms=300, wait=1.5)

    def scroll_list(self) -> None:
        self.auto.scroll_down()

    # ----------------------------------------------------------- search box
    def use_search_box(self) -> tuple[int, int] | None:
        """Type the query into a drawer/widget search field and rescan."""
        fields = self.auto.find_edit_texts()
        if not fields:
            for hint in SEARCH_BOX_HINTS:
                pos = self.auto.find_text(hint)
                if pos:
                    fields = [pos]
                    break
        if not fields:
            return None
        fx, fy = fields[0]
        self.step("search",
                  f"tapping search box at {(fx, fy)}, "
                  f"typing '{self.label}'...")
        self.auto.fill_field(fx, fy, self.label)
        time.sleep(2.0)
        self.auto.key(4)          # hide the keyboard
        time.sleep(1.0)
        return self.find_icon_on_screen()

    # ------------------------------------------------------------- locating
    def locate_icon(self, timeout: float = 180) -> tuple[int, int]:
        deadline = time.time() + timeout

        self.step("scan", "scanning the home screen...")
        pos = self.scan_current_screen()
        if pos:
            return pos

        self.step("drawer", "opening the app drawer (swipe up)...")
        self.swipe_up_drawer()
        pos = self.scan_current_screen()
        if pos:
            return pos

        pos = self.use_search_box()
        if pos:
            return pos

        flips = 0
        while time.time() < deadline and flips < MAX_PAGE_FLIPS:
            self.step("scan",
                      f"'{self.label}' not visible yet — flipping pages "
                      f"({flips + 1}/{MAX_PAGE_FLIPS})...")
            if flips % 2 == 0:
                self.flip_page()
            else:
                self.scroll_list()
            pos = self.scan_current_screen()
            if pos:
                return pos
            flips += 1

        raise AppSearchError(
            f"'{self.label}' was not found in the launcher within {timeout}s")

    # ------------------------------------------------------------ launching
    def wait_foreground(self, timeout: float = 90) -> None:
        frag = self.package.lower()

        def focused():
            act = self.auto.focused_activity()
            return bool(act and frag in act.lower())

        Waiter(timeout, poll=3.0,
               label=f"waiting for {self.package} to reach the "
                     f"foreground").until(focused,
                                          f"{self.package} in the foreground")
        self.step("foreground", f"{self.package} is in the foreground")

    def direct_launch(self) -> None:
        self.step("direct",
                  f"UI search failed — launching {self.package} directly...")
        try:
            self.inst.run_app(self.package)
        except Exception as exc:  # noqa: BLE001
            self.step("direct_warn",
                      f"runapp failed ({exc}) — trying adb monkey...")
            self.auto.adb.shell(
                self.inst.index,
                ["monkey", "-p", self.package,
                 "-c", "android.intent.category.LAUNCHER", "1"],
                timeout=60, discover=True)

    # --------------------------------------------------------------- runner
    def run(self, boot_timeout: float = 600, search_timeout: float = 180,
            open_timeout: float = 90,
            direct_fallback: bool = True) -> dict:
        """Full flow; returns the step report."""
        self.open_instance_and_home(boot_timeout)
        self.ensure_app_installed()

        try:
            pos = self.locate_icon(search_timeout)
        except AppSearchError:
            if not direct_fallback:
                raise
            self.direct_launch()
        else:
            self.step("open", f"tapping '{self.label}' at {pos}")
            self.auto.tap(*pos, wait=2.5)

        self.wait_foreground(open_timeout)
        self.step("done", "flow finished")
        return self.report


def open_app_flow(console: LdConsole, adb: Adb, index: int | None = None,
                  name: str | None = None, label: str = DEFAULT_LABEL,
                  package: str = DEFAULT_PACKAGE, boot_timeout: float = 600,
                  search_timeout: float = 180, open_timeout: float = 90,
                  direct_fallback: bool = True) -> dict:
    flow = AppSearchFlow(console, adb, index=index, name=name, label=label,
                         package=package)
    return flow.run(boot_timeout=boot_timeout, search_timeout=search_timeout,
                    open_timeout=open_timeout, direct_fallback=direct_fallback)
