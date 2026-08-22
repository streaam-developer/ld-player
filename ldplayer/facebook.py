"""Facebook signup automation.

Flow (matches the requested sequence):

1. open the instance and wait until the launcher (apps grid) is showing
2. if Facebook is not installed yet, install it (otherwise skip — never
   reinstalls an already-installed package)
3. open the Facebook app
4. wait until "Create new account" appears on the login screen, tap it, wait
5. on the create-account form, tap "Create new account" again
6. wait for the permission prompt (e.g. Contacts) and tap "Allow"
7. enter first/last name and press Next
8. open the birthday picker, scroll year back >20 years, press Set, press Next
9. select Male on the gender screen, press Next
10. tap "Sign up with email", enter random email, press Next
11. create an advanced password, press Next, save email|password to raw.txt
12. tap "I agree" on the terms screen, wait for processing
13. wait for the confirmation-code screen
14. fetch OTP from Cloudflare Worker and enter it, tap Next
15. check for "confirm you're human" block → update tracker.json

Steps log their progress; `--hold` pauses after step 4 for manual inspection.

Requires Cloudflare Worker config in ``config.json``:
  ``cf_worker_url`` — HTTP Worker URL
  ``cf_worker_api_key`` — shared API key
"""

from __future__ import annotations

import calendar
import json
import random
import re
import string
import threading
import time

from pathlib import Path

from .adb import Adb
from .automation import Automator, AutomationError, Waiter
from .console import LdConsole
from .email_otp import fetch_otp, OtpTimeout
from .instance import Instance

LOGIN_SCREEN_BUTTON = "Create new account"
CREATE_FORM_BUTTON = "Create new account"
PERMISSION_BUTTONS = ["Allow", "ALLOW", "Continue", "Not now"]

#: Buttons that start the signup flow, across Facebook UI generations.
#: Classic builds show "Create new account"; 2024+ Bloks landing screens
#: use "Get started" (or "Sign up") instead.
SIGNUP_ENTRY_BUTTONS = ["Create new account", "Get started", "Sign up"]

#: Screens Facebook may show while loading that we should skip past
LOADING_INDICATORS = ["loading", "connecting", "Logging in", "Signing in"]

#: Common popups/interstitials Facebook shows after first launch
SKIP_POPUP_TEXTS = ["Not now", "Skip", "Turn off", "Cancel", "Maybe later",
                   "No thanks", "Use Facebook without an account"]

#: Name-entry screen
NAME_SCREEN_HEADER = "What's your name"
FIRST_NAME_HINT = "First name"
LAST_NAME_HINT = "Last name"
NEXT_BUTTON = "Next"

#: Birthday screen
BIRTHDAY_SCREEN_HEADER = "What's your birthday"
SET_BUTTON = "Set"
DATE_PICKER_DONE = "Set"

#: Gender screen
GENDER_SCREEN_HEADER = "What's your gender"
GENDER_MALE = "Male"

#: Mobile / email screen
MOBILE_SCREEN_HEADER = "What's your mobile number"
SIGN_UP_WITH_EMAIL = "Sign up with email"
EMAIL_SCREEN_HEADER = "What's your email"
EMAIL_DOMAIN = "dailykhabar.bond"

#: Password screen
PASSWORD_SCREEN_HEADER = "Create a password"
PASSWORD_HINT = "Password"

#: Terms / agree screen
TERMS_HEADER_AGREE = "agree"
I_AGREE_BUTTON = "I agree"
AGREE_TEXT_FRAGMENTS = ["terms", "privacy", "policies", "agree"]

#: Confirmation code screen
CONFIRMATION_HEADER = "confirmation"
CONFIRM_WAIT_SECONDS = 30

#: Human verification block screen
#: NOTE: deliberately narrow. Facebook's *legitimate* confirmation-code
#: page also says things like "...to confirm your account", so greedy
#: phrases ("confirm your", bare "human") misrouted that page as a block
#: and aborted the signup before any OTP was entered. Only unmistakable
#: block wording belongs here.
HUMAN_BLOCK_FRAGMENTS = ["suspicious", "unusual activity",
                         "use your account", "temporarily locked",
                         "locked out", "prove you're human",
                         "prove you’re human", "are you a human",
                         "not a robot"]

#: File to save generated credentials
CREDENTIALS_FILE = "raw.txt"

#: File to track successful / failed signups
TRACKER_FILE = "tracker.json"

#: Serialises tracker.json / raw.txt writes across parallel worker threads
_FILE_LOCK = threading.Lock()

#: runtime permissions worth pre-granting so the dialog resolves cleanly
SIGNUP_PERMISSIONS = [
    "android.permission.READ_CONTACTS",
    "android.permission.WRITE_CONTACTS",
    "android.permission.ACCESS_FINE_LOCATION",
    "android.permission.READ_PHONE_STATE",
]


def find_facebook_apk(extra: Path | None = None) -> Path:
    """Locate a Facebook APK/bundle in the working dir (or an extra path)."""
    if extra:
        extra = Path(extra)
        if extra.is_file():
            return extra
    candidates: list[Path] = []
    for base in (Path.cwd(), Path(__file__).resolve().parent.parent):
        candidates.extend(base.glob("Facebook*"))
        candidates.extend(base.glob("com.facebook*"))
        candidates.extend(base.glob("*facebook*"))
    apks = [p for p in candidates
            if p.suffix.lower() in (".apk", ".apkm", ".xapk", ".apks")
            and p.is_file()]
    if not apks:
        raise FileNotFoundError(
            "no Facebook apk/apkm/xapk found in the working directory — "
            "pass --apk <file>")
    return max(apks, key=lambda p: p.stat().st_mtime)


class FacebookFlow:
    def __init__(self, console: LdConsole, adb: Adb, index: int | None = None,
                 name: str | None = None, package: str = "com.facebook.katana",
                 cf_worker_url: str = "", cf_worker_api_key: str = ""):
        self.inst = Instance(console, adb, name=name, index=index)
        self.inst.resolve()
        self.package = package
        self.auto = Automator(console, adb, self.inst)
        self.report: dict = {}
        #: "success" | "blocked" | "" (error / never finished) — set by run()
        self.success: str = ""
        self._email: str = ""
        self._password: str = ""
        self._cf_worker_url = cf_worker_url
        self._cf_worker_api_key = cf_worker_api_key

    # ---------------------------------------------------------------- steps
    def step(self, tag: str, msg: str) -> None:
        print(f"[{self.inst.name}] {msg}", flush=True)
        self.report[tag] = {"time": time.time(), "msg": msg}

    def open_instance_and_launcher(self, timeout: float = 600) -> None:
        self.step("instance", f"checking {self.inst.name} status...")
        if not self.inst.running:
            self.step("instance",
                      f"{self.inst.name} is stopped — starting it now...")
            self.inst.launch(wait=True, boot_wait=False)
        else:
            self.step("instance", f"{self.inst.name} already running")
        self.step("instance",
                  "waiting for boot + launcher (apps grid)...")
        self.auto.wait_for_home(timeout)

    def ensure_facebook_installed(self, apk_path: str | Path | None = None,
                                  timeout: float = 840) -> None:
        if self.auto.package_installed(self.package):
            self.step("install",
                      f"{self.package} already installed — skipping install")
            return
        apk = find_facebook_apk(Path(apk_path) if apk_path else None)
        self.step("install",
                  f"{self.package} not installed — installing {apk.name} "
                  f"(waiting for adb if needed)...")
        self.inst.install_apk_wait(apk, adb_timeout=timeout)

    def open_facebook(self, timeout: float = 240) -> None:
        self.step("launch", f"opening {self.package} ...")
        try:
            self.inst.run_app(self.package)
        except Exception as exc:  # noqa: BLE001
            self.step("launch_warn",
                      f"runapp failed ({exc}) — retrying via adb monkey...")
            try:
                self.auto.adb.shell(
                    self.inst.index,
                    ["monkey", "-p", self.package,
                     "-c", "android.intent.category.LAUNCHER", "1"],
                    timeout=60, discover=True)
            except Exception as exc2:  # noqa: BLE001
                self.step("launch_warn",
                          f"adb monkey fallback also failed ({exc2}) — "
                          "continuing to wait for UI")

        def focused():
            try:
                return self.auto.focused_activity()
            except Exception:
                return None

        try:
            Waiter(timeout, poll=3.0,
                   label=f"waiting for {self.package} to open").until(
                lambda: bool(focused() and "facebook" in focused()),
                "facebook app in the foreground")
            self.step("launch_ok", "facebook is in the foreground")
        except AutomationError:
            self.step("launch_warn",
                      "could not confirm facebook foreground (adb flaky) — "
                      "continuing to the 'Create new account' wait")

        self._dismiss_loading_screens(timeout=min(timeout, 90))

    def _dismiss_loading_screens(self, timeout: float = 60) -> None:
        """Wait for Facebook's splash/loading screens to finish, and dismiss
        any interstitial popups that may appear on first launch."""
        start = time.time()
        while time.time() - start < timeout:
            for indicator in LOADING_INDICATORS:
                if self.auto.find_text(indicator):
                    self.step("loading", f"Facebook still loading ('{indicator}') — waiting...")
                    time.sleep(3)
                    break
            else:
                break
        self._dismiss_interstitial_popups()

    def _dismiss_interstitial_popups(self, timeout: float = 30) -> None:
        """Dismiss Facebook interstitial popups (login prompts, cookie banners, etc.)."""
        start = time.time()
        while time.time() - start < timeout:
            dismissed = False
            for popup_text in SKIP_POPUP_TEXTS:
                pos = self.auto.find_text(popup_text)
                if pos:
                    self.step("popup_dismiss", f"dismissing popup: tapping '{popup_text}'")
                    self.auto.tap(*pos)
                    time.sleep(2)
                    dismissed = True
                    break
            if not dismissed:
                break

    def click_login_create_account(self, timeout: float = 180,
                                   hold: bool = False) -> None:
        # the app may have resumed straight into the signup flow
        # (e.g. relaunched while the name screen was still on top)
        if self.auto.find_text(NAME_SCREEN_HEADER):
            self.step("login_skip",
                      "already on the name screen — skipping entry tap")
            return
        self.step("login_cna",
                  f"waiting for a signup entry button "
                  f"({' / '.join(SIGNUP_ENTRY_BUTTONS)}) ...")
        text, pos = self._wait_for_any_text_with_retry(
            SIGNUP_ENTRY_BUTTONS, timeout, press_back=False)
        self.step("login_cna_click", f"clicking '{text}' at {pos}")
        self.auto.tap(*pos, wait=2)
        self._wait_for_screen_change(SIGNUP_ENTRY_BUTTONS, timeout=30)
        if hold:
            input("Paused on the create-account screen. Press Enter to "
                  "continue...")

    def submit_create_form(self, timeout: float = 120) -> None:
        """Handle whatever follows the signup entry tap.

        Classic builds show an intermediate screen with a second
        'Create new account' button; newer Bloks builds jump straight to
        the name screen. Either way, we end up ready for name entry.
        """
        self.step("form_cna", "waiting for create-form button or "
                              "name screen ...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.auto.find_text(NAME_SCREEN_HEADER):
                self.step("form_skip",
                          "already on the name screen — no second "
                          "button needed")
                return
            for t in SIGNUP_ENTRY_BUTTONS:
                pos = self.auto.find_text(t)
                if pos:
                    self.step("form_cna_click", f"clicking '{t}' at {pos}")
                    self.auto.tap(*pos, wait=2)
                    self._wait_for_screen_change(t, timeout=30)
                    return
            time.sleep(1.5)
        self.step("form_warn",
                  "no create-form button or name screen within timeout — "
                  "continuing anyway")

    def pre_grant_permissions(self) -> None:
        for perm in SIGNUP_PERMISSIONS:
            try:
                self.auto.grant_permission(self.package, perm)
            except Exception:
                pass

    # -------------------------------------------------------- adaptive helpers
    def _wait_for_any_text(self, texts: list[str],
                           timeout: float = 120) -> tuple[str, tuple[int, int]]:
        """Poll until any of `texts` is on screen. Returns (text, (x, y))."""
        def find_any():
            for t in texts:
                pos = self.auto.find_text(t)
                if pos:
                    return t, pos
            return None
        return Waiter(timeout, poll=2.0,
                      label=f"looking for {' / '.join(texts)}").until(
            find_any, f"any of {texts} on screen")

    def _wait_for_any_text_with_retry(self, texts: list[str],
                                      timeout: float = 120,
                                      max_retries: int = 3,
                                      press_back: bool = True
                                      ) -> tuple[str, tuple[int, int]]:
        """Wait for any of several texts, retrying with back-off if the
        UI is still settling."""
        last_error: AutomationError | None = None
        for attempt in range(1, max_retries + 1):
            try:
                return self._wait_for_any_text(texts, timeout)
            except AutomationError as exc:
                last_error = exc
                if attempt < max_retries:
                    self.step("retry", f"none of {texts} found "
                              f"(attempt {attempt}/{max_retries}) — "
                              f"backing off and retrying...")
                    if press_back:
                        self.auto.back()
                    time.sleep(3)
        raise AutomationError(
            f"could not find any of {texts} after {max_retries} attempts: "
            f"{last_error}")

    def _wait_for_screen_change(self, text_gone: str | list[str],
                                timeout: float = 10) -> None:
        """Wait until the screen visibly moves on after a tap.

        Two ways to succeed: the watched text disappears, OR the visible
        label set changes substantially (the next screen often keeps a
        'Next' button of its own, so waiting for the word to vanish would
        burn the full timeout every single step).
        """
        texts = [text_gone] if isinstance(text_gone, str) else list(text_gone)
        low = [t.lower() for t in texts]
        start = time.time()
        baseline: set[str] | None = None
        while time.time() - start < timeout:
            labels = [l.lower() for l in self._screen_labels()]
            if not any(t in label for label in labels for t in low):
                self.step("screen_change",
                          f"{texts} gone — screen transitioned")
                return
            current = set(labels)
            if baseline is not None and current != baseline:
                changed = len(current ^ baseline)
                if changed >= max(4, len(baseline) // 2):
                    self.step("screen_change",
                              f"screen content changed "
                              f"({changed} labels differ) — transitioned")
                    return
            baseline = current
            time.sleep(1.0)
        self.step("screen_change_warn",
                  f"{texts} still visible after {timeout}s — continuing anyway")

    def _wait_for_text_with_retry(self, text: str, timeout: float = 120,
                                  max_retries: int = 3) -> tuple[int, int]:
        """Wait for text with retries — if the UI is still settling,
        dump + tap again after a short back-off."""
        last_error: AutomationError | None = None
        for attempt in range(1, max_retries + 1):
            try:
                return self.auto.wait_for_text(text, timeout)
            except AutomationError as exc:
                last_error = exc
                if attempt < max_retries:
                    self.step("retry", f"text '{text}' not found "
                              f"(attempt {attempt}/{max_retries}) — "
                              f"backing off and retrying...")
                    self.auto.back()
                    time.sleep(3)
        raise AutomationError(
            f"could not find '{text}' after {max_retries} attempts: {last_error}")

    def allow_permission(self, timeout: float = 120) -> bool:
        """Wait for the permission dialog and tap Allow/Continue.

        Handles multiple rounds of permission dialogs (Facebook often asks
        for contacts, then location, then phone).  Returns True if at least
        one dialog was handled.
        """
        handled_any = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            for button in PERMISSION_BUTTONS:
                pos = self.auto.find_text(button)
                if pos:
                    self.step("permission_allow", f"permission dialog: "
                                                  f"tapping '{button}' at {pos}")
                    self.auto.tap(*pos, wait=2)
                    handled_any = True
                    break
            else:
                if handled_any:
                    break
                remaining = deadline - time.time()
                if remaining > 3:
                    time.sleep(2)
                else:
                    break
        if not handled_any:
            self.step("permission_wait", "no permission dialog detected within timeout")
        return handled_any

    # -------------------------------------------------------- name entry
    def enter_name(self, first_name: str, last_name: str,
                   timeout: float = 60) -> None:
        """Wait for the 'What's your name?' screen, tap the first-name
        field, type, then tap the last-name field, type, and press Next."""
        self.step("name_screen", f"waiting for '{NAME_SCREEN_HEADER}' ...")
        self.auto.wait_for_text(NAME_SCREEN_HEADER, timeout)
        time.sleep(1)

        edit_texts = self.auto.find_edit_texts()
        if len(edit_texts) >= 2:
            first_x, first_y = edit_texts[0]
            last_x, last_y = edit_texts[1]
        elif len(edit_texts) == 1:
            first_x, first_y = edit_texts[0]
            w, h = self.auto.resolution()
            last_x, last_y = first_x, first_y + int(h * 0.08)
        else:
            w, h = self.auto.resolution()
            first_x, first_y = w // 2, int(h * 0.38)
            last_x, last_y = w // 2, int(h * 0.48)

        self.step("name_type", f"typing first name '{first_name}'")
        self.auto.fill_field(first_x, first_y, first_name)

        self.step("name_type", f"typing last name '{last_name}'")
        self.auto.fill_field(last_x, last_y, last_name)

        self.auto.key(4)  # dismiss keyboard
        time.sleep(0.4)

        next_pos = self._wait_for_text_with_retry(NEXT_BUTTON, timeout)
        self.step("name_next", f"clicking Next at {next_pos}")
        self.auto.tap(*next_pos, wait=2)
        self._wait_for_screen_change(NEXT_BUTTON, timeout=30)

    # -------------------------------------------------------- birthday
    def _date_picker_open(self) -> bool:
        """True when the Android DatePicker popup is showing."""
        return bool(self.auto.find_by_class("DatePicker"))

    def set_birthday(self, timeout: float = 90,
                     min_age_years: int = 21) -> None:
        """Set a birth date at least ``min_age_years`` in the past.

        Newer Facebook builds auto-open an Android DatePicker popup
        (month/day/year NumberPicker wheels + SET/CANCEL) over the
        birthday page, hiding the page's own texts — so we detect the
        popup itself and never block on the header being visible.
        """
        # wait until either the picker is already open or the page is there
        start = time.time()
        while time.time() - start < min(timeout, 30):
            if self._date_picker_open():
                break
            if self.auto.find_text("birthday"):
                break
            time.sleep(1.5)

        if not self._date_picker_open():
            self.step("birthday_picker", "opening date picker ...")
            w, h = self.auto.resolution()
            taps: list[tuple[int, int]] = []
            for label in ("Enter your birthday", "Birthday"):
                pos = self.auto.find_text(label)
                if pos:
                    taps.append(pos)
            taps.extend(self.auto.find_edit_texts())
            taps.append((w // 2, int(h * 0.45)))

            opened = False
            for tx, ty in taps:
                self.auto.tap(tx, ty, wait=2)
                if self._date_picker_open():
                    opened = True
                    break
            if not opened:
                raise AutomationError("date picker did not open")

        target_year = time.localtime().tm_year - min_age_years - 1
        # random calendar date within a legal month length for target year;
        # month FIRST so the day column has its final row count before the
        # day wheel is set
        month = random.randint(1, 12)
        day = random.randint(1, calendar.monthrange(target_year, month)[1])
        self.step("birthday_pick",
                  f"random date {calendar.month_name[month]} "
                  f"{day}, {target_year}")

        self._scroll_wheel_at(0, month)     # left-most  = month
        self._scroll_wheel_at(1, day)       # middle    = day
        self._scroll_wheel_at(-1, target_year)   # right-most = year

        set_pos = self._wait_for_text_with_retry(SET_BUTTON, timeout=20)
        self.step("birthday_set", f"clicking Set at {set_pos}")
        self.auto.tap(*set_pos, wait=2)

        # popup must close; then continue with whatever comes next
        start = time.time()
        while time.time() - start < 15 and self._date_picker_open():
            time.sleep(1)
        self._tap_next_if_present(timeout=30)

    def _picker_wheels(self) -> list[dict]:
        """DatePicker NumberPicker nodes sorted left -> right."""
        wheels = [n for n in self.auto.find_by_class("NumberPicker")
                  if n.get("bounds")]
        wheels.sort(key=lambda n: int(n["bounds"].strip("[]").split(",")[0]))
        return wheels

    #: month name -> 1..12, keyed by the first three letters so both
    #: "Jan" and "January" resolve
    _MONTH_ABBR: dict[str, int] = {
        name[:3].lower(): num for num, name in enumerate(
            calendar.month_name[1:], start=1)}

    def _wheel_value(self, wheel: dict) -> int | None:
        """Read the selected (centre) value of one NumberPicker wheel.

        The selected item is exposed as an EditText child of the wheel;
        neighbours are Buttons. Numeric wheels ("2025", "22") return the
        number; the month wheel shows names ("Jan") and resolves through
        :data:`_MONTH_ABBR`.
        """
        b = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", wheel.get("bounds", ""))
        if not b:
            return None
        x1, y1, x2, y2 = map(int, b.groups())
        for node in self.auto.find_by_class("EditText"):
            nb = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                          node.get("bounds", "") or "")
            if not nb:
                continue
            nx1, ny1, nx2, ny2 = map(int, nb.groups())
            if nx1 >= x1 and nx2 <= x2 and ny1 >= y1 and ny2 <= y2:
                text = (node.get("text") or "").strip()
                digits = re.sub(r"[^0-9]", "", text)
                if digits:
                    return int(digits)
                return self._MONTH_ABBR.get(text.lower()[:3])
        return None

    def _scroll_wheel_at(self, pos: int, target: int,
                         timeout: float = 150) -> None:
        """Bring the wheel at ``pos`` (0 = left-most … -1 = right-most)
        to ``target``.

        Deterministic strategy shared by month/day/year wheels: read the
        centre value, cover bulk distance with bursts of *gentle*
        single-row swipes (no re-dump between rows), verify per-swipe on
        final approach. Gentle swipes never trigger fling momentum.
        Wheels are re-located every round because changing the month
        re-lays-out neighbouring columns. If a burst moves the value
        AWAY from the target the assumed direction flips automatically,
        so an unexpected wheel orientation can't cause endless ping-pong.
        """
        deadline = time.time() + timeout
        invert = False
        prev_dist: int | None = None

        while time.time() < deadline:
            self.auto._invalidate_ui()   # fresh dump, never the TTL cache
            wheels = self._picker_wheels()
            if not wheels:
                raise AutomationError("date picker wheels not found")
            wheel = wheels[pos]
            value = self._wheel_value(wheel)

            b = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                         wheel.get("bounds", ""))
            x = (int(b.group(1)) + int(b.group(3))) // 2
            cy = (int(b.group(2)) + int(b.group(4))) // 2

            if value == target:
                if prev_dist == 0:
                    self.step("birthday_scroll",
                              f"wheel[{pos}] settled on {value}")
                    return
                prev_dist = 0            # confirm with a second read
                continue

            if value is None:
                # unreadable (mid-animation): gentle backward nudge
                self.auto.swipe(x, cy - 60, x, cy + 60,
                                duration_ms=300, wait=0.4)
                continue

            dist = abs(value - target)
            # a burst that moved us further away => wrong direction guess
            if prev_dist is not None and dist > prev_dist + 1:
                invert = not invert
                self.step("birthday_scroll",
                          f"wheel[{pos}] direction flipped")
                prev_dist = None
                continue
            prev_dist = dist

            going_back = (value > target) != invert
            y1, y2 = ((cy - 60, cy + 60) if going_back
                      else (cy + 60, cy - 60))

            if dist <= 3:
                # final approach: one row per swipe, verified every time
                self.auto.swipe(x, y1, x, y2, duration_ms=320, wait=0.45)
                continue

            # bulk distance: burst of single-row swipes, then re-read
            burst = min(dist - 1, 6)
            for _i in range(burst):
                self.auto.swipe(x, y1, x, y2, duration_ms=280, wait=0.2)

        raise AutomationError(
            f"wheel[{pos}] did not reach {target} in time")

    def _tap_next_if_present(self, timeout: float = 30) -> bool:
        """Tap 'Next' when it shows up; tolerate screens that have none."""
        try:
            next_pos = self._wait_for_text_with_retry(NEXT_BUTTON,
                                                      timeout=timeout)
        except AutomationError:
            self.step("next_warn", "no Next button found — continuing")
            return False
        self.step("next_click", f"clicking Next at {next_pos}")
        self.auto.tap(*next_pos, wait=2)
        self._wait_for_screen_change(NEXT_BUTTON, timeout=30)
        return True

    # -------------------------------------------------------- gender
    def select_gender(self, gender: str = "Male",
                      timeout: float = 60) -> None:
        """Tap the requested gender option, then press Next.

        Tolerates screens where the 'What's your gender?' header text is
        worded differently — the option itself is what matters.
        """
        start = time.time()
        while time.time() - start < timeout:
            if self.auto.find_text(gender):
                break
            time.sleep(1.5)

        self.step("gender_select", f"selecting '{gender}' ...")
        pos = self._wait_for_text_with_retry(gender, timeout)
        self.auto.tap(*pos, wait=1.5)

        next_pos = self._wait_for_text_with_retry(NEXT_BUTTON, timeout)
        self.step("gender_next", f"clicking Next at {next_pos}")
        self.auto.tap(*next_pos, wait=2)
        self._wait_for_screen_change(NEXT_BUTTON, timeout=30)

    # -------------------------------------------------------- email signup
    def enter_email(self, email: str | None = None,
                    timeout: float = 60) -> None:
        """Get to the email-entry screen (tapping 'Sign up with email' if
        the mobile-number screen appears first), type the address, press
        Next.

        If *email* is ``None`` a random 7-letter address at
        ``dailykhabar.bond`` is generated.
        """
        # reach the email field from whichever variant shows up
        start = time.time()
        on_email_screen = False
        while time.time() - start < timeout:
            if self.auto.find_text(EMAIL_SCREEN_HEADER):
                on_email_screen = True
                break
            btn = self.auto.find_text(SIGN_UP_WITH_EMAIL)
            if btn:
                self.step("email_switch", "tapping 'Sign up with email' ...")
                self.auto.tap(*btn, wait=2)
                self._wait_for_screen_change(SIGN_UP_WITH_EMAIL, timeout=30)
                continue
            time.sleep(1.5)
        if not on_email_screen:
            raise AutomationError(
                "neither the email field nor 'Sign up with email' appeared")

        if email is None:
            email = self._random_email()
        self._email = email
        self.step("email_screen",
                  f"waiting for '{EMAIL_SCREEN_HEADER}' ...")
        self.auto.wait_for_text(EMAIL_SCREEN_HEADER, timeout)
        time.sleep(1)

        edit_texts = self.auto.find_edit_texts()
        if edit_texts:
            ex, ey = edit_texts[0]
        else:
            w, h = self.auto.resolution()
            ex, ey = w // 2, int(h * 0.42)

        self.step("email_type", f"typing email '{email}'")
        self.auto.fill_field(ex, ey, email)

        self.auto.key(4)  # dismiss keyboard
        time.sleep(0.4)

        next_pos = self._wait_for_text_with_retry(NEXT_BUTTON, timeout)
        self.step("email_next", f"clicking Next at {next_pos}")
        self.auto.tap(*next_pos, wait=2)
        self._wait_for_screen_change(NEXT_BUTTON, timeout=30)

    @staticmethod
    def _random_email(length: int = 7) -> str:
        """Generate a random lowercase email like ``xbqkmlj@dailykhabar.bond``."""
        letters = "abcdefghijklmnopqrstuvwxyz"
        user = "".join(random.choices(letters, k=length))
        return f"{user}@{EMAIL_DOMAIN}"

    # -------------------------------------------------------- password
    def create_password(self, password: str | None = None,
                        timeout: float = 60) -> None:
        """Type a strong password, press Next, then save email|password
        to ``raw.txt``."""
        if not self.auto.find_text(PASSWORD_SCREEN_HEADER):
            self.step("password_screen",
                      f"waiting for '{PASSWORD_SCREEN_HEADER}' ...")
            self.auto.wait_for_text(PASSWORD_SCREEN_HEADER, timeout)

        if password is None:
            password = self._random_password()
        self._password = password

        edit_texts = self.auto.find_edit_texts()
        if edit_texts:
            px, py = edit_texts[0]
        else:
            w, h = self.auto.resolution()
            px, py = w // 2, int(h * 0.42)

        self.step("password_type", "typing password")
        self.auto.fill_field(px, py, password)

        self.auto.key(4)  # dismiss keyboard
        time.sleep(0.4)

        next_pos = self._wait_for_text_with_retry(NEXT_BUTTON, timeout)
        self.step("password_next", f"clicking Next at {next_pos}")
        self.auto.tap(*next_pos, wait=2)
        self._wait_for_screen_change(NEXT_BUTTON, timeout=30)

        self._save_credentials()

    @staticmethod
    def _random_password(length: int = 16) -> str:
        """Generate an advanced password: upper, lower, digit, symbol mix.

        Symbols exclude '%' (``input text`` turns a literal '%s' into a
        space) and shell-hostile quotes/backslashes.
        """
        cats = [
            random.choices(string.ascii_uppercase, k=2),
            random.choices(string.ascii_lowercase, k=length // 2 - 1),
            random.choices(string.digits, k=3),
            random.choices("!@#$^&*()-_=+[]{};:,.<>?", k=2),
        ]
        pw = "".join(c for group in cats for c in group)
        remaining = length - len(pw)
        if remaining > 0:
            pw += "".join(random.choices(
                string.ascii_letters + string.digits + "!@#$^&*-_=?",
                k=remaining))
        pw_list = list(pw)
        random.shuffle(pw_list)
        return "".join(pw_list)

    def _save_credentials(self) -> None:
        """Append ``email|password`` to the credentials file."""
        if not self._email or not self._password:
            self.step("save_warn", "email or password empty — skipping save")
            return
        line = f"{self._email}|{self._password}\n"
        dest = Path(__file__).resolve().parent.parent / CREDENTIALS_FILE
        try:
            with _FILE_LOCK:
                with open(dest, "a", encoding="utf-8") as fh:
                    fh.write(line)
            self.step("save_creds",
                      f"saved to {dest}: {self._email}|{'*' * len(self._password)}")
        except OSError as exc:
            self.step("save_warn", f"could not write credentials file: {exc}")

    # -------------------------------------------------------- agree to terms
    def agree_to_terms(self, timeout: float = 60,
                       wait_after: float = 30) -> None:
        """Wait for the terms/policies screen, tap 'I agree', then wait
        ``wait_after`` seconds for the next screen to load."""
        self.step("terms_screen", "waiting for terms / policies screen ...")
        found = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            for frag in AGREE_TEXT_FRAGMENTS:
                if self.auto.find_text(frag):
                    found = True
                    break
            if found:
                break
            time.sleep(2)
        time.sleep(1)

        self.step("terms_click", "looking for 'I agree' button ...")
        pos = self._wait_for_text_with_retry(I_AGREE_BUTTON, timeout)
        self.auto.tap(*pos, wait=2)

        # wait for terms to process — exit early the moment the screen
        # content changes instead of sleeping the full window blindly
        self.step("terms_wait",
                  f"waiting up to {wait_after:.0f}s for terms to process ...")
        baseline = set(self._screen_labels())
        deadline = time.time() + wait_after
        while time.time() < deadline:
            current = set(self._screen_labels())
            changed = len(current ^ baseline)
            if changed >= 4:
                self.step("terms_wait",
                          f"screen changed ({changed} labels differ) "
                          f"— terms processed")
                return
            time.sleep(1.5)

    # -------------------------------------------------------- confirmation
    def wait_for_confirmation(self, timeout: float = 60,
                              otp_timeout: float = 120) -> None:
        """Wait for the confirmation-code screen, fetch the OTP from the
        Cloudflare Worker, enter it, then tap Next.

        If no CF Worker is configured (empty URL/key), falls back to the
        old behaviour: wait a fixed 30s then tap Next blindly.
        """
        self.step("confirm_screen",
                  "waiting for confirmation code screen ...")
        found = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            for frag in ("confirmation", "code", "enter the", "verify"):
                if self.auto.find_text(frag):
                    found = True
                    break
            if found:
                break
            time.sleep(2)
        time.sleep(1)

        # --- Try to fetch + enter the OTP automatically ---
        otp_entered = False
        if self._cf_worker_url and self._cf_worker_api_key and self._email:
            try:
                self.step("otp_fetch",
                          f"polling CF Worker for OTP ({self._email}) ...")
                code = fetch_otp(
                    self._cf_worker_url, self._cf_worker_api_key,
                    self._email, timeout=otp_timeout)
                self._enter_otp_code(code)
                otp_entered = True
            except OtpTimeout:
                self.step("otp_timeout",
                          "OTP not received within timeout — tapping Next anyway")
            except Exception as exc:  # noqa: BLE001
                self.step("otp_error", f"OTP fetch failed ({exc}) — continuing")
        else:
            self.step("otp_skip",
                      "no CF Worker configured — waiting 30s then tapping Next")
            time.sleep(30)

        next_pos = self._wait_for_text_with_retry(NEXT_BUTTON, timeout)
        self.step("confirm_next", f"clicking Next at {next_pos}")
        self.auto.tap(*next_pos, wait=2)

    def _enter_otp_code(self, code: str) -> None:
        """Find the OTP input field on the confirmation screen and type
        the code."""
        self.step("otp_enter", f"entering OTP code: {code}")
        edit_texts = self.auto.find_edit_texts()
        if edit_texts:
            ox, oy = edit_texts[0]
        else:
            w, h = self.auto.resolution()
            ox, oy = w // 2, int(h * 0.42)

        self.auto.fill_field(ox, oy, code)

        self.auto.key(4)  # dismiss keyboard
        time.sleep(0.4)

    # -------------------------------------------------------- human-block check
    def check_human_block(self, timeout: float = 30) -> bool:
        """After the confirmation Next tap, wait briefly then scan the screen
        for Facebook's 'confirm you're human' / 'suspicious activity' block.

        Returns ``True`` if the block screen was detected (account failed),
        ``False`` if it was not found (account succeeded).
        """
        time.sleep(5)
        deadline = time.time() + timeout
        while time.time() < deadline:
            for frag in HUMAN_BLOCK_FRAGMENTS:
                if self.auto.find_text(frag):
                    self.step("human_block",
                              f"detected human-verification block ('{frag}')")
                    return True
            time.sleep(2)
        self.step("human_check", "no human-block detected — account looks good")
        return False

    # -------------------------------------------------------- tracker
    @staticmethod
    def _update_tracker(success: bool) -> None:
        """Increment the ``successful`` or ``failed`` counter in
        ``tracker.json``.  Creates the file with defaults if absent."""
        dest = Path(__file__).resolve().parent.parent / TRACKER_FILE
        with _FILE_LOCK:
            data: dict = {"successful": 0, "failed": 0}
            if dest.exists():
                try:
                    data = json.loads(dest.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
            if success:
                data["successful"] = data.get("successful", 0) + 1
            else:
                data["failed"] = data.get("failed", 0) + 1
            dest.write_text(json.dumps(data, indent=2) + "\n",
                            encoding="utf-8")
        status = "SUCCESS" if success else "FAILED"
        print(f"  [tracker] {status} — "
              f"successful={data['successful']} failed={data['failed']} "
              f"({dest})", flush=True)

    # -------------------------------------------------------- screen state
    #: (screen-id, any-of fragments). Order matters: earlier entries win.
    #: 'confirmation' MUST precede 'human_block' — the OTP page shares
    #: vocabulary with block screens ("confirm ..."), and the block
    #: fragments are only unmistakable wording, never generic.
    _SCREEN_MATCHERS: list[tuple[str, tuple[str, ...]]] = [
        ("confirmation", ("confirmation", "enter the code",
                          "we've sent", "check your email")),
        ("human_block", HUMAN_BLOCK_FRAGMENTS),
        # NB: the *button* only — footer text on other pages says
        # "...you agree to our Terms..." and must not match here
        ("terms", ("I agree",)),
        ("password", (PASSWORD_SCREEN_HEADER,)),
        ("email", (EMAIL_SCREEN_HEADER,)),
        ("mobile", (MOBILE_SCREEN_HEADER, SIGN_UP_WITH_EMAIL,
                    "Enter your mobile number")),
        ("gender", (GENDER_SCREEN_HEADER, "Male", "Female")),
        ("birthday", (BIRTHDAY_SCREEN_HEADER, "birthday")),
        ("name", (NAME_SCREEN_HEADER,)),
        ("entry", tuple(t.lower() for t in SIGNUP_ENTRY_BUTTONS)),
    ]

    def _screen_labels(self) -> list[str]:
        """Lower-cased labels of everything visible right now."""
        try:
            xml = self.auto.dump_ui()
        except Exception:
            return []
        return [label.lower() for label, _x, _y, _c in
                self.auto._text_nodes(xml)]

    def _classify_screen(self, labels: list[str]) -> str:
        joined = " | ".join(labels).lower()
        for screen_id, fragments in self._SCREEN_MATCHERS:
            if any(f.lower() in joined for f in fragments):
                return screen_id
        return "unknown"

    def _tap_permission_if_present(self) -> bool:
        """One quick pass over the known permission-dialog buttons."""
        for button in PERMISSION_BUTTONS:
            pos = self.auto.find_text(button)
            if pos:
                self.step("permission_allow",
                          f"tapping '{button}' at {pos}")
                self.auto.tap(*pos, wait=1.5)
                return True
        return False

    # --------------------------------------------------------------- runner
    def run(self, step_wait: float = 3.0, hold: bool = False,
            grant_perms: bool = True, boot_timeout: float = 600,
            install_timeout: float = 840,
            apk_path: str | Path | None = None,
            first_name: str = "Alex", last_name: str = "Johnson",
            otp_timeout: float = 120,
            flow_timeout: float = 1500,
            email: str | None = None) -> dict:
        """Adaptive signup loop.

        Instead of a rigid step order, we classify whatever screen Facebook
        currently shows and run its handler — so popups, extra pages, and
        mid-flow resumes all work. Each stage runs once; permission dialogs
        and interstitials are handled whenever they appear.

        ``email`` — a pre-generated address to sign up with; when omitted a
        random one is created at typing time. On return, :attr:`success`
        holds "success", "blocked", or "" (flow errored / never completed).
        """
        self.open_instance_and_launcher(boot_timeout)
        self.ensure_facebook_installed(apk_path, timeout=install_timeout)
        if grant_perms:
            try:
                self.pre_grant_permissions()
            except Exception:  # noqa: BLE001
                pass
        self.open_facebook(timeout=240)

        done: set[str] = set()
        attempts: dict[str, int] = {}
        stall = 0
        deadline = time.time() + flow_timeout

        while time.time() < deadline:
            labels = self._screen_labels()

            # popups first — they overlay and hide real content
            if not self._date_picker_open():
                self._dismiss_interstitial_popups(timeout=4)

            screen = self._classify_screen(labels)
            # a DatePicker popup covers the page and hides its texts —
            # its wheels carry no 'birthday' wording, so classify by the
            # popup itself, never by what is visible underneath
            if self._date_picker_open():
                screen = "birthday"
            attempts[screen] = attempts.get(screen, 0) + 1
            self.step("screen", f"on '{screen}' screen "
                      f"(visit {attempts[screen]})")

            try:
                if screen == "human_block":
                    # last-resort guard: a page with an input field and
                    # 'code' wording is the confirmation screen, not a block
                    joined = " | ".join(labels)
                    if (self.auto.find_edit_texts()
                            and "code" in joined):
                        self.step(
                            "screen_fix",
                            "'human_block' has an OTP field — "
                            "treating as confirmation screen")
                        screen = "confirmation"
                        self.wait_for_confirmation(otp_timeout=otp_timeout)
                        done.add("confirmation")
                        break  # final human-block scan decides success

                    self._update_tracker(success=False)
                    self.success = "blocked"
                    self.step("done",
                              "flow finished — BLOCKED by human verification")
                    return self.report

                elif screen == "confirmation":
                    self.wait_for_confirmation(otp_timeout=otp_timeout)
                    done.add("confirmation")
                    break  # final human-block scan below decides success

                elif screen == "terms" and "terms" not in done:
                    self.agree_to_terms()
                    done.add("terms")

                elif screen == "password" and "password" not in done:
                    self.create_password()
                    done.add("password")

                elif (screen in ("email", "mobile")
                      and "contact" not in done):
                    self.enter_email(email=email)
                    done.add("contact")

                elif screen == "gender" and "gender" not in done:
                    self.select_gender()
                    done.add("gender")

                elif (screen == "birthday"
                      and "birthday" not in done):
                    self.set_birthday()
                    done.add("birthday")

                elif screen == "name" and "name" not in done:
                    self.enter_name(first_name, last_name)
                    done.add("name")

                elif screen == "entry" and "entry" not in done:
                    self.click_login_create_account(hold=hold)
                    self.submit_create_form()
                    done.add("entry")

                else:
                    # unknown screen / stage already complete
                    if self._tap_permission_if_present():
                        continue
                    if screen == "unknown":
                        stall += 1
                        self.step("stall",
                                  f"unrecognised screen ({stall} consecutive)")
                        # BACK would close an open DatePicker popup — the
                        # birthday handler needs it, so never press it here
                        if (stall % 5 == 0
                                and not self._date_picker_open()):
                            self.auto.back()
                            time.sleep(1.5)
                        time.sleep(2)
                        continue
                    time.sleep(1.5)
                    continue
            except Exception as exc:  # noqa: BLE001
                self.step("stage_error",
                          f"'{screen}' handler failed: {exc}")
                if attempts.get(screen, 0) >= 3:
                    raise
                time.sleep(2)
                continue

            # a completed stage means we made progress
            stall = 0

        # flow window over — final human-block scan decides success
        blocked = self.check_human_block(timeout=20)
        self._update_tracker(success=not blocked)
        if blocked:
            self.success = "blocked"
            self.step("done", "flow finished — BLOCKED by human verification")
        else:
            self.success = "success"
            self.step("done", "flow complete — account created successfully")
        return self.report


def signup_flow(console: LdConsole, adb: Adb, index: int | None = None,
                name: str | None = None, package: str = "com.facebook.katana",
                step_wait: float = 3.0, hold: bool = False,
                grant_perms: bool = True, boot_timeout: float = 600,
                install_timeout: float = 840,
                apk_path: str | Path | None = None,
                first_name: str = "Alex", last_name: str = "Johnson",
                cf_worker_url: str = "", cf_worker_api_key: str = "",
                otp_timeout: float = 120,
                email: str | None = None) -> dict:
    flow = FacebookFlow(console, adb, index=index, name=name, package=package,
                        cf_worker_url=cf_worker_url,
                        cf_worker_api_key=cf_worker_api_key)
    return flow.run(step_wait=step_wait, hold=hold, grant_perms=grant_perms,
                    boot_timeout=boot_timeout, install_timeout=install_timeout,
                    apk_path=apk_path, first_name=first_name,
                    last_name=last_name, otp_timeout=otp_timeout,
                    email=email)
