"""Outlook (Microsoft) account signup inside Chrome on LDPlayer.

Always runs on a FRESH instance (created by :func:`create_signup_instance`)
with a random mobile identity. Flow:

1. open the instance and wait until the launcher (apps grid) is showing
2. search the system apps for Chrome and open it
3. dismiss Chrome's first-run screens ("Welcome to Chrome" ->
   "Use without signing in" -> "Got it")
4. navigate to https://outlook.office.com/mail/ and wait until loaded
5. tap "Create one", type a random 8-char letter+number username, Next
6. create a strong password, Next
7. pick birth date (Month/Day/Year) at least 20 years back, Next
8. enter a random first/last name, Next
9. "prove you're human" — WAIT for the user to solve the captcha by hand
10. "protect your account" — type a random ``@dailykhabar.bond`` email,
    Next, fetch the verification code from the Cloudflare Worker, type it,
    Next
11. "we could not create passkey" — tap Cancel; the account now lives in
    this instance and everything stays open until the user stops the script

The screen handlers are adaptive (same style as the Facebook signup flow):
whatever page Microsoft currently shows gets classified and handled.
"""

from __future__ import annotations

import calendar
import json
import random
import re
import string
import time

from pathlib import Path

from .adb import Adb
from .appsearch import DEFAULT_LABEL, DEFAULT_PACKAGE, AppSearchError, \
    AppSearchFlow
from .automation import Automator, AutomationError, Waiter
from .config import load_config
from .console import LdConsole, LdConsoleError
from .device import apply_profile
from .email_otp import fetch_otp, OtpTimeout
from .instance import Instance


START_URL = "https://outlook.office.com/mail/"
EMAIL_DOMAIN = "dailykhabar.bond"

# ------------------------------------------------------------- chrome first run
CHROME_WELCOME = "welcome to chrome"
USE_WITHOUT_BUTTONS = ["Use without signing in", "Use without an account",
                       "No thanks"]
GOT_IT_BUTTONS = ["Got it", "Got It", "GOT IT"]

URL_BAR_IDS = ["com.android.chrome:id/url_bar",
               "com.android.chrome:id/search_box_text"]
URL_BAR_DESCS = ["Search or type URL"]

LOAD_FRAGMENTS = ["create one", "sign in"]

# ------------------------------------------------------------- microsoft pages
NEXT_BUTTONS = ["Next"]
CREATE_ONE = "Create one"

#: fragments identifying each Microsoft signup screen (lower-case)
PASSWORD_HEADER = "create a password"
NAME_FRAGMENTS = ["first name", "last name", "add your name"]
BIRTHDAY_HINTS = ("Month", "Day", "Year")
HUMAN_FRAGMENTS = ["prove you're human", "prove you’re human", "puzzle",
                   "not a robot"]
PROTECT_FRAGMENTS = ["protect your account", "recovery email", "add recovery"]
CODE_FRAGMENTS = ["enter the code", "we sent a code", "enter code"]
PASSKEY_FRAGMENT = "passkey"
CANCEL_BUTTONS = ["Cancel", "Not now"]

POPUP_BUTTONS = ["Accept all", "Accept", "I agree"]

#: manual mode (human verification): how long we wait for the user to solve
#: the captcha and how many consecutive absent-polls mean "moved on"
MANUAL_WAIT_TIMEOUT = 1800.0
MANUAL_MISS_POLLS = 6

FIRST_NAMES = ["Alex", "Jordan", "Taylor", "Casey", "Morgan", "Riley",
               "Jamie", "Drew", "Robin", "Skyler", "Aiden", "Leo", "Nina",
               "Sara", "Omar", "Liam", "Zoe", "Ethan", "Maya", "Noah"]
LAST_NAMES = ["Johnson", "Miller", "Davis", "Wilson", "Moore", "Anderson",
              "Thomas", "Jackson", "White", "Harris", "Clark", "Lewis",
              "Walker", "Hall", "Young", "King", "Wright", "Scott"]


class OutlookError(RuntimeError):
    pass


# ------------------------------------------------------------------ instance
def _vms_root() -> Path | None:
    lc = load_config().get("ldconsole")
    return Path(lc).parent / "vms" if lc else None


def _enable_adb(index: int) -> bool:
    """Force ``basicSettings.adbDebug = 1`` before first launch.

    Fresh instances inherit the GUI's global default which may have ADB off —
    then adb never attaches and boot-wait hangs forever even though Android
    boots fine. The flag is only read at VM start, so writing it while the
    instance is stopped takes effect on the very next launch.
    """
    vms = _vms_root()
    cfg_file = vms / "config" / f"leidian{index}.config" if vms else None
    if not cfg_file or not cfg_file.is_file():
        return False
    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        data["basicSettings.adbDebug"] = 1
        tmp = cfg_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=4, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(cfg_file)
        return True
    except (OSError, json.JSONDecodeError):
        return False


def new_instance_name(prefix: str, console: LdConsole) -> str:
    while True:
        name = f"{prefix}{time.strftime('%m%d%H%M%S')}{random.randint(10, 99)}"
        if not console.find(name=name):
            return name


def create_signup_instance(console: LdConsole, name: str,
                           template: str | None = None):
    """Create a FRESH instance every run: blank or cloned from *template*,
    with a unique random phone identity and adb debugging forced on."""
    if console.find(name=name):
        raise OutlookError(f"instance '{name}' already exists")

    src = None
    if template:
        raw = str(template).strip()
        src = console.find(index=int(raw)) if raw.isdigit() else None
        if src is None:
            src = console.find(name=raw)
        if src is None:
            raise OutlookError(f"template instance '{template}' not found")
        try:
            if console.is_running(index=src.index):
                print(f"template '{src.name}' is running — closing it "
                      "before cloning...", flush=True)
                console.quit(index=src.index)
                console.wait_until_quit(index=src.index, timeout=120,
                                        poll=2.0)
        except LdConsoleError:
            pass
        res = console.copy(name, source_index=src.index)
    else:
        res = console.add(name)

    inst = console.find(name=name)
    if not inst:
        raise OutlookError(f"create failed: {res.text or res.stderr}")

    profile = apply_profile(console, name=name)
    adb_ok = _enable_adb(inst.index)
    print(f"[{name}] fresh instance ready (index {inst.index}, "
          f"{'cloned from ' + src.name if src else 'blank'}) — "
          f"random mobile: {profile.summary()}"
          + ("" if adb_ok else " [WARN: could not force adbDebug on]"),
          flush=True)
    return inst


# ------------------------------------------------------------------ the flow
class OutlookFlow:
    def __init__(self, console: LdConsole, adb: Adb, index: int | None = None,
                 name: str | None = None, label: str = DEFAULT_LABEL,
                 package: str = DEFAULT_PACKAGE, cf_worker_url: str = "",
                 cf_worker_api_key: str = "", otp_timeout: float = 180.0):
        self.console = console
        self.adb = adb
        self.inst = Instance(console, adb, name=name, index=index)
        self.inst.resolve()
        self.label = label
        self.package = package
        self.auto = Automator(console, adb, self.inst)
        self.report: dict = {}
        #: "success" | "" (error / stopped early)
        self.success: str = ""
        self._username: str = ""
        self._password: str = ""
        self._recovery_email: str = ""
        self._cf_worker_url = cf_worker_url
        self._cf_worker_api_key = cf_worker_api_key
        self._otp_timeout = otp_timeout
        self._folders_seen: set[str] = set()

    # ---------------------------------------------------------------- steps
    def step(self, tag: str, msg: str) -> None:
        print(f"[{self.inst.name}] {msg}", flush=True)
        self.report[tag] = {"time": time.time(), "msg": msg}

    def go_home(self) -> None:
        self.auto.home()
        time.sleep(1.5)

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

    # ------------------------------------------------------------- scanning
    def _screen_labels(self) -> list[str]:
        try:
            xml = self.auto.dump_ui()
        except Exception:  # noqa: BLE001
            return []
        return [lbl.lower() for lbl, _x, _y, _c in self.auto._text_nodes(xml)]

    def _screen_joined(self) -> str:
        return " | ".join(self._screen_labels())

    def find_icon_on_screen(self) -> tuple[int, int] | None:
        """Center of the app icon on the current screen (skips text fields;
        prefers clickable nodes)."""
        try:
            xml = self.auto.dump_ui()
        except Exception:  # noqa: BLE001
            return None
        rects = _field_rects(self.auto, xml)
        low = self.label.lower()
        fallback: tuple[int, int] | None = None
        for label, cx, cy, clickable in self.auto._text_nodes(xml):
            if low not in label.lower():
                continue
            if _center_in_rects(cx, cy, rects):
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

    # ------------------------------------------------------------ launching
    def locate_icon(self, timeout: float = 180) -> tuple[int, int]:
        deadline = time.time() + timeout

        self.step("scan", f"scanning for '{self.label}' ...")
        pos = self.scan_current_screen()
        if pos:
            return pos

        self.step("drawer", "opening the app drawer (swipe up)...")
        w, h = self.auto.resolution()
        x = w // 2
        self.auto.swipe(x, int(h * 0.88), x, int(h * 0.30),
                        duration_ms=300, wait=1.5)
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

    def open_chrome(self, search_timeout: float = 180,
                    open_timeout: float = 90) -> None:
        """Search the system apps for Chrome and open it."""
        finder = AppSearchFlow(self.console, self.adb,
                               index=self.inst.index, label=self.label,
                               package=self.package)
        finder.ensure_app_installed()
        self.go_home()

        try:
            pos = finder.locate_icon(search_timeout)
        except AppSearchError:
            self.direct_launch()
        else:
            self.step("open", f"tapping '{self.label}' at {pos}")
            self.auto.tap(*pos, wait=2.5)

        self.wait_foreground(open_timeout)


def open_app_flow(console: LdConsole, adb: Adb, index: int | None = None,
                  name: str | None = None, label: str = DEFAULT_LABEL,
                  package: str = DEFAULT_PACKAGE, boot_timeout: float = 600,
                  search_timeout: float = 180, open_timeout: float = 90,
                  direct_fallback: bool = True) -> dict:
    flow = AppSearchFlow(console, adb, index=index, name=name, label=label,
                         package=package)
    return flow.run(boot_timeout=boot_timeout, search_timeout=search_timeout,
                    open_timeout=open_timeout, direct_fallback=direct_fallback)
