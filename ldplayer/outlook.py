"""Outlook (Microsoft) account signup inside Chrome on LDPlayer.

Always runs on a FRESH instance (created by :func:`create_signup_instance`)
with a random mobile identity. Flow:

1. open the instance and wait until the launcher (apps grid) is showing
2. search the system apps for Chrome and open it
3. dismiss Chrome's first-run screens ("Welcome to Chrome" ->
   "Use without signing in" -> "More" -> "Got it")
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
import threading
import time

from pathlib import Path

from .adb import Adb
from .appsearch import DEFAULT_LABEL, DEFAULT_PACKAGE, AppSearchError, \
    AppSearchFlow
from .automation import AutomationError, Waiter
from .config import load_config
from .console import LdConsole, LdConsoleError
from .device import apply_profile
from .email_otp import fetch_otp, OtpTimeout


START_URL = "https://outlook.office.com/mail/"
EMAIL_DOMAIN = "dailykhabar.bond"

# ------------------------------------------------------------- chrome first run
CHROME_WELCOME = "welcome to chrome"
USE_WITHOUT_BUTTONS = ["Use without signing in", "Use without an account",
                       "No thanks"]
GOT_IT_BUTTONS = ["Got it", "Got It", "GOT IT"]
#: intermediate first-run pages ("sync?" etc.) advance via a More button
MORE_BUTTONS = ["More"]

URL_BAR_IDS = ["com.android.chrome:id/url_bar",
               "com.android.chrome:id/search_box_text"]
URL_BAR_DESCS = ["Search or type URL"]

#: resource-id fragments of Chrome's OWN inputs (omnibox variants) — these
#: must never receive form typing; web-page inputs carry no resource-id
CHROME_FIELD_IDS = ["url_bar", "search_box_text", "location_bar",
                    "search_url_bar", "search"]

LOAD_FRAGMENTS = ["create one", "sign in"]

# ------------------------------------------------------------- microsoft pages
NEXT_BUTTONS = ["Next"]
CREATE_ONE = "Create one"

#: fragments identifying each Microsoft signup screen (lower-case)
PASSWORD_HEADERS = ["create your password", "create a password"]
NAME_FRAGMENTS = ["first name", "last name", "add your name"]
BIRTHDAY_HINTS = ("Month", "Day", "Year")
HUMAN_FRAGMENTS = ["prove you're human", "prove you’re human", "puzzle",
                   "not a robot"]
PROTECT_FRAGMENTS = ["protect your account", "recovery email", "add recovery"]
CODE_FRAGMENTS = ["enter the code", "we sent a code", "enter code",
                  "we just sent", "verify your identity"]
PASSKEY_FRAGMENT = "passkey"
CANCEL_BUTTONS = ["Cancel", "Not now"]

#: ONLY unambiguous cookie-consent buttons. Generic words like "Accept" or
#: "I agree" also appear as normal LINKS in Microsoft's signup footers —
#: tapping them navigates away mid-flow, so they must never be touched.
POPUP_BUTTONS = ["Accept all"]

#: username-taken wording shown when the picked address already exists
USERNAME_TAKEN_FRAGMENTS = ["already has that username", "try another",
                            "isn't available", "isn’t available",
                            "not available", "someone already"]

#: manual mode (human verification): how long we wait for the user to solve
#: the captcha and how many consecutive absent-polls mean "moved on"
MANUAL_WAIT_TIMEOUT = 1800.0
MANUAL_MISS_POLLS = 6

#: the challenge's hold-button label variants ("press and hold"), tried
#: in order — find_text is a substring match so the first hit wins
HUMAN_HOLD_BUTTONS = ["Press & Hold", "Press and hold", "Hold"]

FIRST_NAMES = ["Alex", "Jordan", "Taylor", "Casey", "Morgan", "Riley",
               "Jamie", "Drew", "Robin", "Skyler", "Aiden", "Leo", "Nina",
               "Sara", "Omar", "Liam", "Zoe", "Ethan", "Maya", "Noah"]
LAST_NAMES = ["Johnson", "Miller", "Davis", "Wilson", "Moore", "Anderson",
              "Thomas", "Jackson", "White", "Harris", "Clark", "Lewis",
              "Walker", "Hall", "Young", "King", "Wright", "Scott"]

CREDENTIALS_FILE = "raw.txt"

_FILE_LOCK = threading.Lock()


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


# ------------------------------------------------------------ random values
def random_username(length: int = 8) -> str:
    """Random letter+number word that always STARTS with a letter."""
    while True:
        s = random.choice(string.ascii_lowercase) + "".join(
            random.choices(string.ascii_lowercase + string.digits,
                           k=length - 1))
        if any(c.isdigit() for c in s):
            return s


def random_password(length: int = 16) -> str:
    """Strong password: upper/lower/digit/symbol mix.

    Symbols exclude '%' (``input text`` turns '%s' into a space) and
    shell-hostile quotes/backslashes.
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


def random_birthdate(min_age_years: int = 21,
                     max_age_years: int = 49) -> tuple[int, int, int]:
    """(month 1-12, day 5-14, year) making the person min..max years old.

    Day stays within 5-14 because Chrome's native day list only renders
    the first ~17 rows — this range is always visible without scrolling.
    """
    age = random.randint(min_age_years, max_age_years)
    year = time.localtime().tm_year - age
    month = random.randint(1, 12)
    day = random.randint(5, 14)
    return month, day, year


def pick_pool_email(path: str | Path | None = None,
                    domain: str = EMAIL_DOMAIN) -> str:
    """Random ``@dailykhabar.bond`` email from the repo's used_emails.json
    pool; fall back to a freshly claimed unique address."""
    p = Path(path) if path else \
        Path(__file__).resolve().parent.parent / "used_emails.json"
    if p.is_file():
        try:
            pool = [str(e).strip() for e in json.loads(
                p.read_text(encoding="utf-8"))]
            pool = [e for e in pool if e.lower().endswith("@" + domain)]
            if pool:
                return random.choice(pool)
        except (json.JSONDecodeError, OSError):
            pass
    from .emails import claim_email
    return claim_email(domain=domain)


# ------------------------------------------------------------------ the flow
class OutlookFlow(AppSearchFlow):
    """Full Chrome-first-run + Microsoft-signup automation for one instance."""

    def __init__(self, console: LdConsole, adb: Adb, index: int | None = None,
                 name: str | None = None, label: str = DEFAULT_LABEL,
                 package: str = DEFAULT_PACKAGE, cf_worker_url: str = "",
                 cf_worker_api_key: str = "", otp_timeout: float = 240.0):
        super().__init__(console, adb, index=index, name=name, label=label,
                         package=package)
        self.cf_worker_url = cf_worker_url
        self.cf_worker_api_key = cf_worker_api_key
        self.otp_timeout = otp_timeout
        #: filled in as the flow progresses
        self.username: str = ""
        self.password: str = ""
        self.recovery_email: str = ""
        self.outlook_address: str = ""
        self.success: bool = False
        self._creds_saved = False

    # ------------------------------------------------------------- helpers
    def _screen_labels(self) -> list[str]:
        try:
            xml = self.auto.dump_ui()
        except Exception:  # noqa: BLE001
            return []
        return [lbl.lower() for lbl, _x, _y, _c in self.auto._text_nodes(xml)]

    def screen_text(self) -> str:
        return " | ".join(self._screen_labels())

    def _wait_for_any_text(self, texts: list[str], timeout: float = 120,
                           dismiss_popups: bool = True
                           ) -> tuple[str, tuple[int, int]]:
        """Poll until any of `texts` shows up. Returns (text, (x, y))."""
        low = [t.lower() for t in texts]

        def find_any():
            joined = self.screen_text()
            if dismiss_popups:
                self._dismiss_popups()
            for t, l in zip(texts, low):
                if l in joined:
                    pos = self.auto.find_text(t)
                    if pos:
                        return t, pos
            return None

        return Waiter(timeout, poll=2.0,
                      label=f"looking for {' / '.join(texts)}").until(
            find_any, f"any of {texts} on screen")

    def _find_exact(self, text: str, min_size: int = 8
                    ) -> tuple[int, int] | None:
        """Center of a VISIBLE node whose label equals `text` exactly.

        Needed because short option values ("1", "15") substring-match their
        neighbours inside native select lists, and offscreen/stale nodes can
        carry the same label with zero-area bounds — tapping those is a no-op
        (they showed up as taps at (0, 0)), so they are filtered out here.
        """
        try:
            xml = self.auto.dump_ui()
        except Exception:  # noqa: BLE001
            return None
        low = text.strip().lower()
        fallback: tuple[int, int] | None = None
        for node in self.auto._all_nodes(xml):
            label = (node.get("text") or "").strip().lower() or \
                (node.get("content-desc") or "").strip().lower()
            if not label or label != low:
                continue
            b = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                         node.get("bounds", "") or "")
            if not b:
                continue
            x1, y1, x2, y2 = map(int, b.groups())
            if x2 - x1 < min_size or y2 - y1 < min_size:
                continue          # zero-size / hidden duplicate
            center = ((x1 + x2) // 2, (y1 + y2) // 2)
            if node.get("clickable") == "true":
                return center
            fallback = fallback or center
        return fallback

    def _dismiss_popups(self) -> bool:
        """Tap cookie-consent / one-tap popups whenever they overlay content."""
        for button in POPUP_BUTTONS:
            pos = self.auto.find_text(button)
            if pos:
                self.step("popup", f"tapping '{button}' popup at {pos}")
                self.auto.tap(*pos, wait=1.5)
                return True
        return False

    def _tap_next(self, timeout: float = 45) -> tuple[int, int]:
        """Wait for a Next button and tap it.

        Microsoft forms keep Next below the fold (birthday details page
        needs a scroll first), so when it is not visible we hide the
        keyboard and scroll the page until the button shows up.

        Deliberately does NOT dismiss popups first: the signup pages carry
        footer links ("...agree...") that a greedy dismissal would tap,
        navigating away from the form."""
        deadline = time.time() + timeout
        hid_keyboard = False
        scrolled = False
        scrolls = 0
        while time.time() < deadline:
            pos = self.auto.find_text("Next")
            if pos:
                # a Next hugging the very bottom edge is often half-hidden
                # behind the keyboard/nav-bar — scroll it into mid-screen
                # first so the tap lands cleanly
                _, screen_h = self.auto.resolution()
                if not scrolled and pos[1] > screen_h * 0.82:
                    self._hide_keyboard()
                    self.auto.scroll_down()
                    time.sleep(1.2)
                    scrolled = True
                    continue
                self.step("next", f"clicking 'Next' at {pos}")
                self.auto.tap(*pos, wait=2.0)
                return pos
            if not hid_keyboard:
                self._hide_keyboard()
                hid_keyboard = True
                continue
            scrolls += 1
            if scrolls % 4 == 0:
                self.auto.scroll_up()     # bounced past it — come back
            else:
                self.auto.scroll_down()
            time.sleep(0.8)
        raise AutomationError(f"'Next' button not found within "
                              f"{timeout:.0f}s")

    def _field_shows(self, value: str) -> bool:
        """True when any editable field currently holds exactly `value`."""
        try:
            xml = self.auto.dump_ui()
        except Exception:  # noqa: BLE001
            return False
        low = value.strip().lower()
        for node in self.auto._all_nodes(xml):
            if "EditText" in node.get("class", "") and \
                    (node.get("text") or "").strip().lower() == low:
                return True
        return False

    def _hide_keyboard(self) -> None:
        self.auto.key(4)
        time.sleep(0.5)

    def _page_fields(self) -> list[tuple[int, int]]:
        """Centers of editable WEB-page inputs only.

        Chrome's omnibox/url bar is an EditText itself and often sorts
        first in the dump — without this filter the username got typed
        into the address box instead of the signup form.
        """
        try:
            xml = self.auto.dump_ui()
        except Exception:  # noqa: BLE001
            return []
        out: list[tuple[int, int]] = []
        for node in self.auto._all_nodes(xml):
            if "EditText" not in node.get("class", ""):
                continue
            rid = (node.get("resource-id") or "").lower()
            if any(fid in rid for fid in CHROME_FIELD_IDS):
                continue
            center = self.auto._bounds_center(node.get("bounds", ""))
            if center:
                out.append(center)
        return out

    def _type_into_field(self, text: str, what: str,
                         fallback_frac: float = 0.42) -> None:
        fields = self._page_fields()
        if fields:
            fx, fy = fields[0]
        else:
            w, h = self.auto.resolution()
            fx, fy = w // 2, int(h * fallback_frac)
        self.step("type", f"typing {what} into field at {(fx, fy)}")
        self.auto.fill_field(fx, fy, text)
        self._hide_keyboard()

    def _manual_banner(self, lines: list[str]) -> None:
        bar = "=" * 66
        print(f"\n[{self.inst.name}] {bar}", flush=True)
        for line in lines:
            print(f"[{self.inst.name}]   {line}", flush=True)
        print(f"[{self.inst.name}] {bar}\n", flush=True)

    # -------------------------------------------------------- chrome steps
    def open_chrome(self, search_timeout: float = 180,
                    open_timeout: float = 90) -> None:
        """Locate Chrome in the launcher and bring it to the foreground."""
        self.ensure_app_installed()
        self.go_home()
        try:
            pos = self.locate_icon(search_timeout)
        except AppSearchError:
            self.direct_launch()
        else:
            self.step("open", f"tapping '{self.label}' at {pos}")
            self.auto.tap(*pos, wait=3.0)
        self.wait_foreground(open_timeout)

    def handle_first_run(self, timeout: float = 45) -> bool:
        """Dismiss Chrome's first-run wizard when present.

        "Welcome to Chrome" -> "Use without signing in" -> "More" ->
        "Got it". Returns True when anything was dismissed.
        """
        deadline = time.time() + timeout
        dismissed = False
        welcome_seen_at: float | None = None

        while time.time() < deadline:
            joined = self.screen_text()

            if CHROME_WELCOME in joined:
                welcome_seen_at = time.time()
                target = next((b for b in USE_WITHOUT_BUTTONS
                               if b.lower() in joined), None)
                pos = self.auto.find_text(target) if target else None
                if pos:
                    self.step("first_run", f"'{target}' at {pos} — tapping...")
                    self.auto.tap(*pos, wait=3.0)
                    dismissed = True
                time.sleep(1.5)
                continue

            got_it = next((b for b in GOT_IT_BUTTONS
                           if b.lower() in joined), None)
            if got_it:
                pos = self.auto.find_text(got_it)
                if pos:
                    self.step("first_run",
                              f"'{got_it}' page — tapping at {pos}...")
                    self.auto.tap(*pos, wait=3.0)
                    dismissed = True
                    break
                time.sleep(1.5)
                continue

            # intermediate first-run page: advance with its More button
            more = next((b for b in MORE_BUTTONS
                         if b.lower() in joined), None)
            if more and (dismissed or welcome_seen_at):
                pos = self.auto.find_text(more)
                if pos:
                    self.step("first_run",
                              f"'{more}' page — advancing at {pos}...")
                    self.auto.tap(*pos, wait=2.5)
                    dismissed = True
                time.sleep(1.5)
                continue

            # main-window evidence (url bar / sign-in page) means no wizard
            if self.auto.find_by_resource_id(URL_BAR_IDS[0]) or \
                    any(f in joined for f in LOAD_FRAGMENTS):
                break

            # grace period after Chrome opens before declaring "no wizard"
            if welcome_seen_at is None and time.time() > deadline - 5:
                break
            time.sleep(1.5)

        if dismissed:
            time.sleep(2.0)
        return dismissed

    def navigate(self, url: str = START_URL, load_timeout: float = 150
                 ) -> None:
        """Type the URL into Chrome's address bar and wait for the page."""
        pos: tuple[int, int] | None = None
        for rid in URL_BAR_IDS:
            pos = self.auto.find_by_resource_id(rid)
            if pos:
                break
        if pos is None:
            for desc in URL_BAR_DESCS:
                pos = self.auto.find_by_content_desc(desc)
                if pos:
                    break
        if pos is None:
            fields = self.auto.find_edit_texts()
            pos = fields[0] if fields else None
        if pos is None:
            w, h = self.auto.resolution()
            pos = (w // 2, int(h * 0.08))

        self.step("navigate", f"typing {url} into the address bar...")
        self.auto.fill_field(*pos, url)
        time.sleep(0.6)
        self.auto.enter()
        self.step("load", "waiting for outlook.office.com to load ...")
        self._wait_for_any_text(LOAD_FRAGMENTS, load_timeout)
        self._dismiss_popups()

    # ------------------------------------------------------- signup steps
    def start_signup(self, settle_seconds: float = 4.0, timeout: float = 90
                     ) -> None:
        """Tap the 'Create one' link on the Outlook sign-in page."""
        self.step("signup", "waiting for the 'Create one' button ...")
        t, pos = self._wait_for_any_text([CREATE_ONE], timeout)
        self.step("signup", f"clicking '{t}' at {pos}, waiting "
                            f"{settle_seconds:.0f}s ...")
        self.auto.tap(*pos, wait=settle_seconds)

    def enter_username(self, username: str | None = None, attempts: int = 3,
                       timeout: float = 60) -> None:
        """Fill the new-email box with an 8-char letter+number word, Next.

        Retries with fresh names when Microsoft says the handle is taken.
        """
        fixed = username
        for attempt in range(1, attempts + 1):
            uname = fixed or random_username(8)
            self.username = uname
            self.outlook_address = f"{uname}@outlook.com"
            if attempt == 1:
                self.step("username",
                          "waiting for the 'Create a Microsoft account' "
                          "box ...")
                Waiter(90, poll=2.0,
                       label="waiting for the username box").until(
                    self._page_fields, "username input box on the page")
            self.step("username",
                      f"[{attempt}/{attempts}] typing username "
                      f"'{uname}' ({self.outlook_address}) ...")
            self._type_into_field(uname, "username")
            self._tap_next(timeout)

            joined = self.screen_text()
            if not any(frag.lower() in joined
                       for frag in USERNAME_TAKEN_FRAGMENTS):
                self.step("username", f"username '{uname}' accepted")
                return
            self.step("username", f"'{uname}' is taken — trying another ...")
            time.sleep(1.5)
        raise AutomationError(
            f"could not find a free username after {attempts} attempts")

    def enter_password(self, password: str | None = None,
                       timeout: float = 60) -> None:
        """Wait for the 'Create your password' page, fill a strong one, Next."""
        self.step("password", f"waiting for '{PASSWORD_HEADERS[0]}' page ...")
        self._wait_for_any_text(PASSWORD_HEADERS, timeout)
        time.sleep(1.0)

        self.password = password or random_password()
        self._type_into_field(self.password, "password")
        self._tap_next(timeout)
        self._save_credentials()

    def set_birthday_web(self, min_age_years: int = 21,
                         max_age_years: int = 49,
                         timeout: float = 90) -> None:
        """Pick Month / Day and TYPE Year (age between min and max years).

        The native day list only renders ~17 rows at once, so its picker is
        scrolled until the target day becomes visible. The year goes straight
        into its box by typing — far faster than wheeling through ~100 rows.
        """
        self.step("birthday", "waiting for the Month/Day/Year boxes ...")
        hints_low = [h.lower() for h in BIRTHDAY_HINTS]

        def boxes_ready():
            joined = self.screen_text()
            return sum(h in joined for h in hints_low) >= 2

        Waiter(timeout, poll=2.0,
               label="waiting for birthday boxes").until(
            boxes_ready, "Month/Day/Year boxes on screen")
        time.sleep(1.0)

        month, day, year = random_birthdate(min_age_years, max_age_years)
        month_name = calendar.month_name[month]
        self.step("birthday",
                  f"setting birth date: {month_name} {day}, {year} "
                  f"(age {time.localtime().tm_year - year})")
        self._pick_select("Month", [month_name, month_name[:3]])
        self._pick_select("Day", [str(day)])
        self._fill_year_box(str(year))
        self._tap_next()

    def _fill_year_box(self, year: str) -> None:
        """Type the birth year into the Year box — ONE fill, no re-cycles.

        Chrome's WebView often never echoes the freshly typed value back
        through uiautomator, so a failed confirmation must NOT trigger
        delete-and-retype rounds (the values visibly land fine). We pick
        the editable field closest to the Year label, fill once, confirm
        best-effort, then move on to Next."""
        pos = self.auto.find_text("Year")
        if not pos:
            raise AutomationError("'Year' box not found")
        self.step("year", f"typing '{year}' into the Year box near {pos}...")
        self.auto.tap(*pos, wait=1.2)

        fields = self._page_fields()
        if not fields:
            # no editable input appeared — a native list probably opened
            self.step("year", "no text field found — using the picker ...")
            self._pick_select("Year", [year])
            return

        target = min(fields, key=lambda f: abs(f[0] - pos[0])
                     + abs(f[1] - pos[1]))
        self.auto.fill_field(*target, year)

        # close the keyboard and pull the below-fold Next button up into
        # view before the flow clicks it
        self._hide_keyboard()
        self.auto.scroll_down()

        deadline = time.time() + 4
        while time.time() < deadline:
            if self._find_exact(year) or self._field_shows(year):
                self.step("year", f"year '{year}' confirmed in the box")
                return
            time.sleep(1)
        # value landed but the UI tree does not echo it — trust and continue
        self.step("year",
                  f"'{year}' not echoed by the UI tree — continuing "
                  f"(box was filled)")

    def _pick_select(self, hint: str, variants: list[str],
                     timeout: float = 60) -> str:
        """Open one select box (native Chrome picker) and choose a value.

        The option list may need scrolling (the Year wheel holds ~100
        entries); we keep swiping inside it until an exact match shows.
        If no list ever opens the field is treated as a plain text input.
        """
        field = self.auto.find_text(hint)
        if not field:
            raise AutomationError(f"'{hint}' box not found")
        self.step("select", f"opening '{hint}' picker at {field} ...")
        self.auto.tap(*field, wait=2.0)

        w, h = self.auto.resolution()
        cx = w // 2
        deadline = time.time() + timeout
        scrolls = 0
        while time.time() < deadline:
            for v in variants:
                pos = self._find_exact(v)
                if pos:
                    self.step("select", f"choosing '{v}' at {pos}")
                    self.auto.tap(*pos, wait=1.5)
                    return v
            scrolls += 1
            if scrolls % 4 == 0:
                # occasional bounce-back guards against overshooting the end
                self.auto.swipe(cx, int(h * 0.40), cx, int(h * 0.70),
                                duration_ms=300, wait=1.0)
            else:
                self.auto.swipe(cx, int(h * 0.70), cx, int(h * 0.35),
                                duration_ms=300, wait=1.0)

        # no native list opened — maybe a plain text input instead
        fields = self._page_fields() or self.auto.find_edit_texts()
        if fields:
            self.step("select",
                      f"no picker opened — typing '{variants[0]}' directly")
            self.auto.fill_field(*fields[0], variants[0])
            self._hide_keyboard()
            return variants[0]
        raise AutomationError(f"could not pick a value for '{hint}'")

    def enter_name(self, first_name: str | None = None,
                   last_name: str | None = None, timeout: float = 60) -> None:
        """'Add your name' page: first + last field, then Next."""
        first_name = first_name or random.choice(FIRST_NAMES)
        last_name = last_name or random.choice(LAST_NAMES)
        self.step("name", f"waiting for the name page ({first_name} "
                          f"{last_name}) ...")
        self._wait_for_any_text(NAME_FRAGMENTS, timeout)
        time.sleep(1.0)

        edit_texts = self._page_fields()
        if len(edit_texts) >= 2:
            (f1x, f1y), (f2x, f2y) = edit_texts[0], edit_texts[1]
        elif len(edit_texts) == 1:
            f1x, f1y = edit_texts[0]
            w, h = self.auto.resolution()
            f2x, f2y = f1x, f1y + int(h * 0.08)
        else:
            w, h = self.auto.resolution()
            f1x, f1y = w // 2, int(h * 0.38)
            f2x, f2y = w // 2, int(h * 0.48)

        self.step("name_type", f"typing first name '{first_name}'")
        self.auto.fill_field(f1x, f1y, first_name)
        self.step("name_type", f"typing last name '{last_name}'")
        self.auto.fill_field(f2x, f2y, last_name)
        self._hide_keyboard()
        self._tap_next(timeout)

    def wait_human_verification(self, timeout: float = MANUAL_WAIT_TIMEOUT
                                ) -> None:
        """'Let's prove you're human' page.

        The challenge's 'press and hold' button is held down repeatedly
        (adb long-press) until the page moves on. If the button cannot be
        located, the console asks for its label or falls back to fully
        manual solving."""
        self.step("human", "waiting for the human-verification page ...")
        self._wait_for_any_text(HUMAN_FRAGMENTS, timeout=120)
        self.step("human_hold",
                  f"challenge detected — holding '{HUMAN_HOLD_BUTTONS[0]}'...")

        # give the hold-button a short grace period to render before
        # bothering the user
        pos = None
        for _ in range(10):
            pos = self._find_any_button(HUMAN_HOLD_BUTTONS)
            if pos:
                break
            time.sleep(1.0)

        if pos:
            self._hold_button_until_page_changes(HUMAN_HOLD_BUTTONS, timeout)
            return

        # ------------------------------------------------ interactive fallback
        self._manual_banner([
            "ACTION NEEDED — puzzle page is showing but the hold-button",
            f"({HUMAN_HOLD_BUTTONS[0]} / {HUMAN_HOLD_BUTTONS[1]}) was not found.",
            "Type the exact BUTTON LABEL to hold (empty = solve by hand):",
        ])
        button = input(f"  [{self.inst.name}] button to hold: ").strip()
        if button:
            self._hold_button_until_page_changes([button], timeout)
            return

        self.step("human_wait",
                  f"waiting for YOU to finish the puzzle "
                  f"(up to {timeout:.0f}s)...")

        start = time.time()
        misses = 0
        last_tick = time.time()
        while time.time() - start < timeout:
            joined = self.screen_text()
            if any(f.lower() in joined for f in PROTECT_FRAGMENTS):
                self.step("human_done",
                          "'protect your account' appeared — puzzle passed")
                return
            if not any(f.lower() in joined for f in HUMAN_FRAGMENTS):
                misses += 1
                if misses >= MANUAL_MISS_POLLS:
                    self.step("human_done",
                              "puzzle page gone — assuming it was passed")
                    return
            else:
                misses = 0
            now = time.time()
            if now - last_tick >= 20:
                last_tick = now
                print(f"  [{self.inst.name}] ... still waiting for you to "
                      f"solve the puzzle ({timeout - (now - start):.0f}s "
                      f"left)", flush=True)
            time.sleep(2.0)
        raise AutomationError(
            f"timed out waiting for manual human verification "
            f"({timeout:.0f}s)")

    def _find_any_button(self,
                         labels: list[str]) -> tuple[int, int] | None:
        """First on-screen match among `labels` (substring search)."""
        for lbl in labels:
            pos = self.auto.find_text(lbl)
            if pos:
                return pos
        return None

    def _hold_button_until_page_changes(self, labels: list[str],
                                        timeout: float) -> None:
        """Press-and-HOLD the challenge button until the page moves on.

        The hold is a swipe-in-place with a long duration (the closest adb
        equivalent to keeping a finger pressed). The page is considered
        passed once 'protect your account' shows up or every HUMAN_FRAGMENT
        has been absent for MANUAL_MISS_POLLS consecutive polls."""
        start = time.time()
        misses = 0          # polls without any HUMAN fragment
        not_found = 0       # polls without the button itself
        presses = 0
        last_tick = time.time()
        while time.time() - start < timeout:
            joined = self.screen_text()
            if any(f.lower() in joined for f in PROTECT_FRAGMENTS):
                self.step("human_done",
                          "'protect your account' appeared — puzzle passed")
                return
            if not any(f.lower() in joined for f in HUMAN_FRAGMENTS):
                misses += 1
                if misses >= MANUAL_MISS_POLLS:
                    self.step("human_done",
                              "puzzle page gone — assuming it was passed")
                    return
            else:
                misses = 0

            pos = self._find_any_button(labels)
            if pos:
                not_found = 0
                presses += 1
                if presses % 5 == 1:
                    self.step("human_hold", f"hold #{presses} at {pos}")
                # long-press = swipe from the point onto itself, slowly
                self.auto.swipe(pos[0], pos[1], pos[0], pos[1],
                                duration_ms=1500, wait=1.2)
            else:
                not_found += 1
                if not_found % 6 == 3:
                    # button may sit below the fold — nudge the page down
                    self.auto.scroll_down()

            now = time.time()
            if now - last_tick >= 20:
                last_tick = now
                print(f"  [{self.inst.name}] ... still holding "
                      f"({timeout - (now - start):.0f}s left)", flush=True)
            time.sleep(1.0)
        raise AutomationError(
            f"timed out holding '{labels[0]}' ({timeout:.0f}s)")

    def protect_account(self, recovery_email: str | None = None,
                        timeout: float = 90) -> None:
        """'Let's protect your account': type the @dailykhabar.bond email."""
        if not recovery_email:
            recovery_email = pick_pool_email()
        self.recovery_email = recovery_email
        self.step("protect",
                  f"waiting for 'protect your account' page "
                  f"(recovery: {recovery_email}) ...")
        self._wait_for_any_text(PROTECT_FRAGMENTS, timeout)
        time.sleep(1.0)
        self._type_into_field(recovery_email, "recovery email")
        self._tap_next(timeout)

    def enter_verification_code(self, timeout: float = 90) -> None:
        """Code page: fetch the OTP from the Cloudflare Worker, type, Next."""
        self.step("code", "waiting for the verification-code page ...")
        self._wait_for_any_text(CODE_FRAGMENTS, timeout)
        time.sleep(1.0)

        if not (self.cf_worker_url and self.cf_worker_api_key
                and self.recovery_email):
            raise AutomationError(
                "verification-code page reached but cf_worker_url/"
                "cf_worker_api_key/recovery email are not configured — add "
                "them to config.json or pass --recovery-email")

        self.step("otp_fetch",
                  f"polling CF Worker for OTP ({self.recovery_email}) ...")
        code = fetch_otp(self.cf_worker_url, self.cf_worker_api_key,
                         self.recovery_email, timeout=self.otp_timeout,
                         shape=re.compile(r"\d{4,8}"))
        self._type_into_field(code, f"verification code {code}")
        self._tap_next(timeout)

    def dismiss_passkey(self, timeout: float = 180) -> None:
        """"We could not create a passkey" -> Cancel; account is ready."""
        self.step("passkey", "waiting for the passkey page ...")
        self._wait_for_any_text([PASSKEY_FRAGMENT], timeout)
        time.sleep(1.0)
        t, pos = self._wait_for_any_text(CANCEL_BUTTONS, 20,
                                         dismiss_popups=False)
        self.step("passkey", f"clicking '{t}' at {pos}")
        self.auto.tap(*pos, wait=2.0)
        self.success = True

    # ------------------------------------------------------------- saving
    def _save_credentials(self) -> None:
        """Append ``outlook_address|password|recovery`` to outlook.txt
        (and the shared raw.txt feed) once."""
        if self._creds_saved or not self.username or not self.password:
            return
        line = (f"{self.outlook_address}|{self.password}"
                f"|{self.recovery_email or '-'}\n")
        root = Path(__file__).resolve().parent.parent
        dest = root / "outlook.txt"
        try:
            with _FILE_LOCK:
                with open(dest, "a", encoding="utf-8") as fh:
                    fh.write(line)
            self._creds_saved = True
            self.step("save_creds",
                      f"saved {self.outlook_address}|*** to {dest}")
        except OSError as exc:
            self.step("save_warn",
                      f"could not write outlook.txt: {exc}")
        # keep the shared registry (used by emails.py seeding) in sync
        try:
            with _FILE_LOCK:
                with open(root / CREDENTIALS_FILE, "a", encoding="utf-8") as fh:
                    fh.write(line)
        except OSError as exc:
            self.step("save_warn", f"could not write credentials file: {exc}")

    # --------------------------------------------------------------- runner
    def run(self, boot_timeout: float = 600, search_timeout: float = 180,
            open_timeout: float = 90, username: str | None = None,
            password: str | None = None, first_name: str | None = None,
            last_name: str | None = None, recovery_email: str | None = None,
            min_age_years: int = 21, max_age_years: int = 49,
            flow_timeout: float = 1500) -> dict:
        """Run the whole flow once. Returns the step report."""
        started = time.time()
        self.open_instance_and_home(boot_timeout)
        self.open_chrome(search_timeout, open_timeout)
        self.handle_first_run()
        self.navigate()

        stages = [
            ("signup", lambda: self.start_signup()),
            ("username", lambda: self.enter_username(username)),
            ("password", lambda: self.enter_password(password)),
            ("birthday",
             lambda: self.set_birthday_web(min_age_years, max_age_years)),
            ("name", lambda: self.enter_name(first_name, last_name)),
            ("human", lambda: self.wait_human_verification()),
            ("protect", lambda: self.protect_account(recovery_email)),
            ("code", lambda: self.enter_verification_code()),
            ("passkey", lambda: self.dismiss_passkey()),
        ]
        done: set[str] = set()

        while len(done) < len(stages) and time.time() - started < flow_timeout:
            tag, handler = stages[len(done)]
            try:
                self.step(tag, f">> stage '{tag}' starting")
                handler()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                self.step(f"{tag}_error",
                          f"stage '{tag}' failed: {exc}")
                if isinstance(exc, OtpTimeout):
                    raise
                # retry the same stage after things settle; abort when one
                # stage eats half the overall budget
                if time.time() - started > flow_timeout / 2:
                    raise
                self._dismiss_popups()
                time.sleep(3)
                continue
            done.add(tag)

        if not self.success:
            raise AutomationError(
                "flow did not reach the passkey/cancel step within "
                f"{flow_timeout:.0f}s")

        self._save_credentials()
        self.step("done",
                  f"account ready: {self.outlook_address} "
                  f"(recovery: {self.recovery_email})")
        return self.report

    def stay(self) -> None:
        """Keep the emulator open on the final screen until Ctrl+C."""
        print(f"\n[{self.inst.name}] "
              + "=" * 60, flush=True)
        print(f"[{self.inst.name}] ALL DONE — everything is okay.", flush=True)
        print(f"[{self.inst.name}]   account : {self.outlook_address}",
              flush=True)
        print(f"[{self.inst.name}]   password: {self.password}", flush=True)
        print(f"[{self.inst.name}]   recovery: {self.recovery_email}",
              flush=True)
        print(f"[{self.inst.name}] Staying here until you stop this "
              f"(Ctrl+C).", flush=True)
        print(f"[{self.inst.name}] " + "=" * 60 + "\n", flush=True)
        try:
            beat = 0
            while True:
                time.sleep(30)
                beat += 1
                print(f"[{self.inst.name}] ... holding session open "
                      f"({beat * 30 // 60} min)", flush=True)
        except KeyboardInterrupt:
            print(f"\n[{self.inst.name}] stopped by user — instance left "
                  f"running.", flush=True)


# ---------------------------------------------------------------- entry util
def outlook_flow(console: LdConsole, adb: Adb, index: int | None = None,
                 name: str | None = None, package: str = DEFAULT_PACKAGE,
                 boot_timeout: float = 600, search_timeout: float = 180,
                 open_timeout: float = 90, username: str | None = None,
                 password: str | None = None, first_name: str | None = None,
                 last_name: str | None = None,
                 recovery_email: str | None = None,
                 cf_worker_url: str = "", cf_worker_api_key: str = "",
                 otp_timeout: float = 240.0, min_age_years: int = 21,
                 max_age_years: int = 49,
                 flow_timeout: float = 1500) -> dict:
    flow = OutlookFlow(console, adb, index=index, name=name,
                       package=package, cf_worker_url=cf_worker_url,
                       cf_worker_api_key=cf_worker_api_key,
                       otp_timeout=otp_timeout)
    report = flow.run(boot_timeout=boot_timeout,
                      search_timeout=search_timeout,
                      open_timeout=open_timeout, username=username,
                      password=password, first_name=first_name,
                      last_name=last_name, recovery_email=recovery_email,
                      min_age_years=min_age_years,
                      max_age_years=max_age_years,
                      flow_timeout=flow_timeout)
    flow.stay()
    return report
