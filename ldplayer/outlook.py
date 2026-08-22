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
CODE_FRAGMENTS = ["enter the code", "we sent a code", "enter code",
                  "we just sent", "verify your identity"]
PASSKEY_FRAGMENT = "passkey"
CANCEL_BUTTONS = ["Cancel", "Not now"]

POPUP_BUTTONS = ["Accept all", "Accept", "I agree"]

#: username-taken wording shown when the picked address already exists
USERNAME_TAKEN_FRAGMENTS = ["already has that username", "try another",
                            "isn't available", "isn’t available",
                            "not available", "someone already"]

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
    """Random letter+number word like ``ab3k9x2m`` (always both kinds)."""
    while True:
        s = "".join(random.choices(string.ascii_lowercase + string.digits,
                                   k=length))
        if any(c.isalpha() for c in s) and any(c.isdigit() for c in s):
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


def random_birthdate(min_age_years: int = 21) -> tuple[int, int, int]:
    """(month 1-12, day 1-28, year) making the person > min_age_years old."""
    year = time.localtime().tm_year - random.randint(
        min_age_years + 1, min_age_years + 15)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
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

    def _find_exact(self, text: str) -> tuple[int, int] | None:
        """Center of a node whose label equals `text` exactly (case-insensitive).

        Needed because short option values ("1", "15") substring-match their
        neighbours inside native select lists.
        """
        try:
            xml = self.auto.dump_ui()
        except Exception:  # noqa: BLE001
            return None
        low = text.strip().lower()
        fallback: tuple[int, int] | None = None
        for label, cx, cy, clickable in self.auto._text_nodes(xml):
            if label.strip().lower() == low:
                if clickable:
                    return cx, cy
                fallback = fallback or (cx, cy)
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

    def _tap_next(self, timeout: float = 30) -> tuple[int, int]:
        """Wait for a Next button and tap it."""
        t, pos = self._wait_for_any_text(NEXT_BUTTONS, timeout,
                                         dismiss_popups=False)
        self.step("next", f"clicking '{t}' at {pos}")
        self.auto.tap(*pos, wait=2.0)
        return pos

    def _hide_keyboard(self) -> None:
        self.auto.key(4)
        time.sleep(0.5)

    def _type_into_field(self, text: str, what: str,
                         fallback_frac: float = 0.42) -> None:
        fields = self.auto.find_edit_texts()
        if fields:
            fx, fy = fields[0]
        else:
            w, h = self.auto.resolution()
            fx, fy = w // 2, int(h * fallback_frac)
        self.step("type", f"typing {what}")
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

        "Welcome to Chrome" -> "Use without signing in" -> "Got it".
        Returns True when anything was dismissed.
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
        """Wait for 'Create a password', fill a strong one, Next."""
        self.step("password", f"waiting for '{PASSWORD_HEADER}' page ...")
        self._wait_for_any_text([PASSWORD_HEADER], timeout)
        time.sleep(1.0)

        self.password = password or random_password()
        self._type_into_field(self.password, "password")
        self._tap_next(timeout)
        self._save_credentials()

    def set_birthday_web(self, min_age_years: int = 21,
                         timeout: float = 90) -> None:
        """Pick Month / Day / Year on the details page (>20 years back)."""
        self.step("birthday", "waiting for the Month/Day/Year boxes ...")
        hints_low = [h.lower() for h in BIRTHDAY_HINTS]

        def boxes_ready():
            joined = self.screen_text()
            return sum(h in joined for h in hints_low) >= 2

        Waiter(timeout, poll=2.0,
               label="waiting for birthday boxes").until(
            boxes_ready, "Month/Day/Year boxes on screen")
        time.sleep(1.0)

        month, day, year = random_birthdate(min_age_years)
        month_name = calendar.month_name[month]
        self.step("birthday",
                  f"setting birth date: {month_name} {day}, {year} "
                  f"(age {time.localtime().tm_year - year})")
        self._pick_select("Month", [month_name, month_name[:3]])
        self._pick_select("Day", [str(day)])
        self._pick_select("Year", [str(year)])
        self._tap_next()

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
        fields = self.auto.find_edit_texts()
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

        edit_texts = self.auto.find_edit_texts()
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
        """'Let's prove you're human' — WAIT until YOU solve the puzzle."""
        self.step("human", "waiting for the human-verification page ...")
        self._wait_for_any_text(HUMAN_FRAGMENTS, timeout=120)
        self._manual_banner([
            "ACTION NEEDED — the puzzle page is showing in the emulator.",
            "Solve the 'prove you're human' challenge by hand.",
            "This script resumes automatically once you pass it.",
        ])
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
        """Append ``outlook_address|password|recovery`` to raw.txt once."""
        if self._creds_saved or not self.username or not self.password:
            return
        line = (f"{self.outlook_address}|{self.password}"
                f"|{self.recovery_email or '-'}\n")
        dest = Path(__file__).resolve().parent.parent / CREDENTIALS_FILE
        try:
            with _FILE_LOCK:
                with open(dest, "a", encoding="utf-8") as fh:
                    fh.write(line)
            self._creds_saved = True
            self.step("save_creds",
                      f"saved {self.outlook_address}|*** to {dest}")
        except OSError as exc:
            self.step("save_warn", f"could not write credentials file: {exc}")

    # --------------------------------------------------------------- runner
    def run(self, boot_timeout: float = 600, search_timeout: float = 180,
            open_timeout: float = 90, username: str | None = None,
            password: str | None = None, first_name: str | None = None,
            last_name: str | None = None, recovery_email: str | None = None,
            min_age_years: int = 21, flow_timeout: float = 1500) -> dict:
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
            ("birthday", lambda: self.set_birthday_web(min_age_years)),
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
                      flow_timeout=flow_timeout)
    flow.stay()
    return report
