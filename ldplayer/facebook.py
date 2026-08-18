"""Facebook signup automation.

Flow (matches the requested sequence):

1. open the instance and wait until the launcher (apps grid) is showing
2. if Facebook is not installed yet, install it (otherwise skip — never
   reinstalls an already-installed package)
3. open the Facebook app
4. wait until "Create new account" appears on the login screen, tap it, wait
5. on the create-account form, tap "Create new account" again
6. wait for the permission prompt (e.g. Contacts) and tap "Allow"

Steps log their progress; `--hold` pauses after step 4 for manual inspection.
"""

from __future__ import annotations

import time

from pathlib import Path

from .adb import Adb
from .automation import Automator, AutomationError, Waiter
from .console import LdConsole
from .instance import Instance

LOGIN_SCREEN_BUTTON = "Create new account"
CREATE_FORM_BUTTON = "Create new account"
PERMISSION_BUTTONS = ["Allow", "ALLOW", "Continue", "Not now"]

#: Screens Facebook may show while loading that we should skip past
LOADING_INDICATORS = ["loading", "connecting", "Logging in", "Signing in"]

#: Common popups/interstitials Facebook shows after first launch
SKIP_POPUP_TEXTS = ["Not now", "Skip", "Turn off", "Cancel", "Maybe later",
                   "No thanks", "Use Facebook without an account"]

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
                 name: str | None = None, package: str = "com.facebook.katana"):
        self.inst = Instance(console, adb, name=name, index=index)
        self.inst.resolve()
        self.package = package
        self.auto = Automator(console, adb, self.inst)
        self.report: dict = {}

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
        self.step("login_cna", f"waiting for '{LOGIN_SCREEN_BUTTON}' ...")
        pos = self._wait_for_text_with_retry(LOGIN_SCREEN_BUTTON, timeout)
        self.step("login_cna_click", f"clicking 'Create new account' at {pos}")
        self.auto.tap(*pos, wait=2)
        self._wait_for_screen_change(LOGIN_SCREEN_BUTTON, timeout=30)
        if hold:
            input("Paused on the create-account screen. Press Enter to "
                  "continue...")

    def submit_create_form(self, timeout: float = 120) -> None:
        self.step("form_cna", f"waiting for '{CREATE_FORM_BUTTON}' button ...")
        pos = self._wait_for_text_with_retry(CREATE_FORM_BUTTON, timeout)
        self.step("form_cna_click", f"clicking again at {pos}")
        self.auto.tap(*pos, wait=2)
        self._wait_for_screen_change(CREATE_FORM_BUTTON, timeout=30)

    def pre_grant_permissions(self) -> None:
        for perm in SIGNUP_PERMISSIONS:
            try:
                self.auto.grant_permission(self.package, perm)
            except Exception:
                pass

    # -------------------------------------------------------- adaptive helpers
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

    def _wait_for_screen_change(self, text_gone: str,
                                timeout: float = 30) -> None:
        """Wait until `text_gone` disappears from the screen, confirming
        the tap registered and the UI transitioned."""
        start = time.time()
        while time.time() - start < timeout:
            if not self.auto.find_text(text_gone):
                self.step("screen_change", f"'{text_gone}' gone — screen transitioned")
                return
            time.sleep(1.5)
        self.step("screen_change_warn",
                  f"'{text_gone}' still visible after {timeout}s — continuing anyway")

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

    # --------------------------------------------------------------- runner
    def run(self, step_wait: float = 3.0, hold: bool = False,
            grant_perms: bool = True, boot_timeout: float = 600,
            install_timeout: float = 840,
            apk_path: str | Path | None = None) -> dict:
        self.open_instance_and_launcher(boot_timeout)
        time.sleep(step_wait)
        self.ensure_facebook_installed(apk_path, timeout=install_timeout)
        time.sleep(step_wait)
        if grant_perms:
            self.pre_grant_permissions()
        self.open_facebook(timeout=240)
        time.sleep(step_wait)
        self.click_login_create_account(hold=hold)
        time.sleep(step_wait)
        self.submit_create_form()
        time.sleep(step_wait)
        self.allow_permission()
        self.step("done", "flow complete — permission allowed")
        return self.report


def signup_flow(console: LdConsole, adb: Adb, index: int | None = None,
                name: str | None = None, package: str = "com.facebook.katana",
                step_wait: float = 3.0, hold: bool = False,
                grant_perms: bool = True, boot_timeout: float = 600,
                install_timeout: float = 840,
                apk_path: str | Path | None = None) -> dict:
    flow = FacebookFlow(console, adb, index=index, name=name, package=package)
    return flow.run(step_wait=step_wait, hold=hold, grant_perms=grant_perms,
                    boot_timeout=boot_timeout, install_timeout=install_timeout,
                    apk_path=apk_path)
