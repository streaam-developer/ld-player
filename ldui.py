#!/usr/bin/env python3
"""ldui - interactive control center for LDPlayer automation.

Runs entirely on the Python standard library (ANSI escape codes + msvcrt),
so no pip installs are needed. Start it with:

    python ldui.py

Everything the CLI (`ldcli`) can do is reachable from the menu tree, and the
config values (paths, ports, timeouts, default instance) can be edited right
inside the UI.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ldplayer.config import (find_adb, find_ldconsole, load_config,
                             save_config, status, version_of)
from ldplayer.console import LdConsole
from ldplayer.adb import Adb, AdbError
from ldplayer.instance import (Instance, InstanceError, create_instance,
                               list_instances)
from ldplayer.automation import Automator
from ldplayer import backup as backup_mod
from ldplayer import repair as repair_mod
from ldplayer import device as device_mod
from ldplayer import window as window_mod
from ldplayer import facebook as facebook_mod
from ldplayer.device import VENDORS

# ---------------------------------------------------------------------------
# terminal primitives
# ---------------------------------------------------------------------------

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"
WHITE = "\x1b[37m"
REVERSE = "\x1b[7m"
CLEAR = "\x1b[2J\x1b[H"


def enable_ansi() -> None:
    """Turn on ANSI/virtual-terminal processing in Windows conhost."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            kernel32.SetConsoleMode(
                h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def c(text, *codes) -> str:
    return "".join(codes) + str(text) + RESET


def clear() -> None:
    sys.stdout.write(CLEAR)
    sys.stdout.flush()


def getch() -> str:
    """Return a single key as a string; decodes arrow keys into names."""
    if not sys.stdin.isatty():
        raise OSError("stdin is not a TTY")
    if os.name == "nt":
        import msvcrt
        first = msvcrt.getwch()
        if first in ("\x00", "\xe0"):
            second = msvcrt.getwch()
            return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT",
                    "G": "HOME", "O": "END"}.get(second, second)
        return first
    import tty
    import termios
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            seq = sys.stdin.read(2)
            if seq == "[A":
                return "UP"
            if seq == "[B":
                return "DOWN"
            if seq == "[C":
                return "RIGHT"
            if seq == "[D":
                return "LEFT"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def pause(msg: str = "Press any key to continue...") -> None:
    print()
    print(c(msg, DIM), end="", flush=True)
    try:
        getch()
    except Exception:
        input()
    print()


def confirm(prompt_text: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            ans = input(f"{prompt_text} [{hint}] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print(c("  please answer y or n", YELLOW))


def prompt(question: str, default: str = "") -> str:
    """Text input with an optional default returned on empty Enter."""
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = input(f"{question}{suffix}: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not raw and default:
            return default
        if not raw and not default:
            print(c("  required - enter a value", YELLOW))
            continue
        return raw.strip()


# ---------------------------------------------------------------------------
# menu engine
# ---------------------------------------------------------------------------

class _QuitTui(Exception):
    pass


class _BackOne(Exception):
    pass


class Menu:
    """A navigable list of items; each item runs an action or a submenu.

    Items are (label, handler) pairs. A handler is either a zero-arg callable
    (run the action, then return to this menu) or a Menu instance (submenu).
    Navigation: up/down arrows, Enter, numeric shortcuts, esc/back/q.
    """

    def __init__(self, title: str, items: list, app=None, header: str = ""):
        self.title = title
        self.items = list(items)
        self.app = app
        self.header = header
        self.selected = 0

    def run(self) -> None:
        while True:
            clear()
            self._draw()
            key = None
            while key is None:
                try:
                    key = getch()
                except Exception:
                    self._numbered_input()
                    return
            if key == "UP":
                self.selected = (self.selected - 1) % len(self.items)
            elif key == "DOWN":
                self.selected = (self.selected + 1) % len(self.items)
            elif key in ("ENTER", "\r", "\n", "RIGHT"):
                self._invoke(self.selected)
            elif key in ("ESC", "\x1b", "BACK", "q", "Q", "LEFT"):
                if self.app is not None and self.app._stack:
                    self.app._stack.pop()
                    return
                raise _QuitTui()
            elif key in ("h", "H"):
                self.selected = 0
            elif key in ("g", "G"):
                self.selected = len(self.items) - 1
            elif key.isdigit():
                n = int(key)
                if 1 <= n <= len(self.items):
                    self.selected = n - 1
                    self._invoke(self.selected)

    def _invoke(self, i: int) -> None:
        if i < 0 or i >= len(self.items):
            return
        label, handler = self.items[i]
        if isinstance(handler, Menu):
            if self.app is not None:
                self.app._stack.append(handler)
            handler.run()
            return
        if callable(handler):
            try:
                handler()
            except _QuitTui:
                raise
            except _BackOne:
                return
            except Exception as exc:  # noqa: BLE001
                clear()
                print(c("ERROR", RED, BOLD), c(str(exc), RED))
                pause()

    def _draw(self) -> None:
        width = max((len(t) for t, _ in self.items), default=20)
        width = min(width, 70)
        title_w = max(len(self.title), width + 8)
        bar = "=" * title_w
        print(c(bar, DIM))
        print(c(" " + self.title, BOLD, WHITE))
        if self.app is not None:
            target = self.app.target_label()
            if target:
                print(c(" target: " + target, CYAN))
        if self.header:
            print(c(self.header, DIM))
        print(c(bar, DIM))
        for i, (label, _h) in enumerate(self.items):
            num = f"{i + 1:>2}." if i < 9 else "   "
            line = f" {num} {label}"
            if i == self.selected:
                print(c(line.ljust(title_w), REVERSE))
            else:
                print(line)
        print(c(bar, DIM))
        print(c(" ^/v navigate   Enter select   esc back   q quit", DIM))

    def _numbered_input(self) -> None:
        """Fallback when raw key input is unavailable (piped terminals)."""
        print(c("  (raw keys unavailable - type a number to choose)", YELLOW))
        try:
            sel = input("  choice [1-%d, q to quit, b to go back]: "
                        % len(self.items)).strip()
        except (EOFError, KeyboardInterrupt):
            raise _QuitTui()
        if sel.lower() in ("q", "quit"):
            raise _QuitTui()
        if sel.lower() in ("b", "back"):
            if self.app is not None and self.app._stack:
                self.app._stack.pop()
            return
        if sel.isdigit():
            i = int(sel) - 1
            if 0 <= i < len(self.items):
                self._invoke(i)


# ---------------------------------------------------------------------------
# application state + shared helpers
# ---------------------------------------------------------------------------

CONFIG_EDITABLE = [
    ("default_instance", "default target instance name", "text"),
    ("ldconsole", "path to ldconsole.exe", "path"),
    ("adb", "path to adb.exe", "path"),
    ("adb_port_base", "base adb port", "int"),
    ("launch_timeout", "seconds to wait for launch", "int"),
    ("boot_timeout", "seconds to wait for boot", "int"),
    ("command_timeout", "seconds for ldconsole commands", "int"),
]


class App:
    def __init__(self):
        self.cfg = load_config()
        self._console: LdConsole | None = None
        self._adb: Adb | None = None
        self.target_name: str | None = None
        self.target_index: int | None = None
        self._stack: list[Menu] = []

    # ------------------------------------------------------------ sessions
    def ensure_configured(self) -> bool:
        if self.cfg.get("ldconsole") and self.cfg.get("adb"):
            return True
        clear()
        print(c("LDPlayer/adb not configured yet.", YELLOW, BOLD))
        if confirm("Run auto-detect now (ldcli init)?", default=True):
            self.do_init()
        return bool(self.cfg.get("ldconsole") and self.cfg.get("adb"))

    def console(self) -> LdConsole:
        if self._console is None:
            self._console = LdConsole(self.cfg["ldconsole"],
                                      timeout=self.cfg["command_timeout"],
                                      base_port=self.cfg["adb_port_base"])
        return self._console

    def adb(self) -> Adb:
        if self._adb is None:
            self._adb = Adb(self.cfg["adb"],
                            base_port=self.cfg["adb_port_base"])
        return self._adb

    def refresh_cfg(self) -> None:
        self.cfg = load_config()
        self._console = None
        self._adb = None

    # ------------------------------------------------------------- targeting
    def target_label(self) -> str:
        if self.target_name is not None:
            return f"{self.target_name} (index {self.target_index})"
        if self.target_index is not None:
            return f"index {self.target_index}"
        return "(none - pick per operation)"

    def set_target(self, name: str | None, index: int | None) -> None:
        self.target_name = name
        self.target_index = index

    def pick_instance(self) -> Instance | None:
        """Let the user pick an instance; returns a resolved Instance."""
        if not self.ensure_configured():
            return None
        rows = list_instances(self.console())
        if not rows:
            clear()
            print(c("no instances exist yet", YELLOW))
            pause()
            return None
        options = [(f"[{r.index}] {r.name}  "
                    f"({'running' if r.running else 'stopped'})", r)
                   for r in rows]
        pick = self.list_menu("Select instance", options, allow_cancel=True)
        if pick is None:
            return None
        inst = Instance(self.console(), self.adb(), name=pick.name,
                        index=pick.index)
        inst.resolve()
        self.set_target(pick.name, pick.index)
        return inst

    def target_instance(self) -> Instance:
        """Resolve the global target (or fall back to config default)."""
        if not self.ensure_configured():
            raise InstanceError("LDPlayer/adb not configured")
        name = self.target_name
        index = self.target_index
        if name is None and index is None:
            name = self.cfg.get("default_instance") or "leidian0"
        inst = Instance(self.console(), self.adb(), name=name, index=index)
        inst.resolve()
        return inst

    # --------------------------------------------------------- menu plumbing
    def list_menu(self, title: str, options: list, allow_cancel: bool = True):
        """Single-choice picker; returns the selected value or None."""
        if not options:
            return None
        items = [(f"{i}. {label}", v) for i, (label, v) in enumerate(options)]
        if allow_cancel:
            items.append(("(cancel)", None))
        captured: dict = {}

        def _choose(value):
            captured["value"] = value
            raise _QuitTui()

        menu = Menu(title, [(label, lambda v=v: _choose(v))
                            for label, v in items])
        try:
            menu.run()
        except _QuitTui:
            pass
        return captured.get("value")

    def run_main(self, root: Menu) -> None:
        self._stack = [root]
        try:
            root.run()
        except (KeyboardInterrupt, EOFError, _QuitTui):
            return

    # ------------------------------------------------------------------ init
    def do_init(self) -> None:
        console = find_ldconsole()
        adb = find_adb()
        if console:
            self.cfg["ldconsole"] = str(console)
        if adb:
            self.cfg["adb"] = str(adb)
        save_config(self.cfg)
        self.refresh_cfg()

    def show_status(self) -> None:
        info = status()
        print()
        print(c("LDPlayer toolchain status", BOLD, WHITE))
        print(f"  ldconsole : {info['ldconsole'] or c('NOT FOUND', RED)}")
        if info["ldconsole"]:
            v = version_of(info["ldconsole"])
            if v:
                print(f"  version   : {v}")
        print(f"  adb       : {info['adb'] or c('NOT FOUND', RED)}")
        print(f"  config    : {info['config_file']}")
        if not info["ldconsole_ok"]:
            print(c("  [!] LDPlayer 9 not found", RED))
        if not info["adb_ok"]:
            print(c("  [!] adb not found", RED))
        print()
        print(c("  config:", BOLD))
        for key, val in self.cfg.items():
            print(f"    {key:<20} {val}")


# ---------------------------------------------------------------------------
# action handlers
# ---------------------------------------------------------------------------

def _require_instance(app: App) -> Instance:
    inst = app.pick_instance()
    if inst is None:
        raise _BackOne()
    return inst


def _wrap(app, fn):
    """Run an action with a cleared screen + uniform error reporting."""
    def inner():
        clear()
        try:
            fn(app)
        except (_QuitTui, _BackOne):
            raise
        except Exception as exc:  # noqa: BLE001
            print(c("ERROR", RED, BOLD), c(str(exc), RED))
        pause()
    return inner


# --------------------------------------------------------------- instances
def act_list_instances(app: App) -> None:
    rows = list_instances(app.console())
    print(c("Instances", BOLD, WHITE))
    if not rows:
        print("  (none)")
        return
    for r in rows:
        mark = c("running", GREEN) if r.running else c("stopped", DIM)
        tag = c("  <-- target", CYAN) if (app.target_index == r.index
                                          or app.target_name == r.name) else ""
        print(f"  [{r.index}] {r.name:<20} {mark}  "
              f"{r.width}x{r.height}@{r.dpi}{tag}")


def act_launch(app: App) -> None:
    inst = _require_instance(app)
    wait = confirm("Wait for Android boot?", default=True)
    inst.launch(wait=True, boot_wait=wait)
    print(c(f"launched {inst.name}", GREEN))


def act_quit(app: App) -> None:
    inst = _require_instance(app)
    inst.quit()
    print(c(f"quit {inst.name}", GREEN))


def act_create(app: App) -> None:
    console = app.console()
    name = prompt("New instance name")
    if not name:
        return
    rows = list_instances(console)
    source = None
    if rows and confirm("Clone from an existing instance?", default=False):
        source = app.list_menu("Clone source",
                               [(f"[{r.index}] {r.name}", r.name) for r in rows])
    cpu = prompt("CPU cores", "2")
    memory = prompt("RAM (MB)", "2048")
    res = prompt("Resolution (WxH or WxHxD)", "720,1280,320")
    inst = create_instance(
        console, name, source=source,
        cpu=int(cpu) if cpu.isdigit() else None,
        memory=int(memory) if memory.isdigit() else None,
        resolution=res)
    app.set_target(inst.name, inst.index)
    print(c(f"created '{name}' (index {inst.index})", GREEN))


def act_modify(app: App) -> None:
    inst = _require_instance(app)
    cpu = prompt("CPU cores (blank to keep)", "")
    memory = prompt("RAM in MB (blank to keep)", "")
    res = prompt("Resolution WxH[xD] (blank to keep)", "")
    res_out = app.console().modify(
        index=inst.index,
        cpu=int(cpu) if cpu.isdigit() else None,
        memory=int(memory) if memory.isdigit() else None,
        resolution=res or None)
    if not res_out.ok:
        raise RuntimeError(res_out.text or res_out.stderr)
    print(c(f"modified {inst.name}", GREEN))


def act_remove(app: App) -> None:
    inst = _require_instance(app)
    if not confirm(f"Really DELETE instance '{inst.name}'?", default=False):
        return
    res = app.console().remove(index=inst.index)
    if not res.ok:
        raise RuntimeError(res.text or res.stderr)
    if app.target_index == inst.index:
        app.set_target(None, None)
    print(c(f"removed {inst.name}", GREEN))


def act_rename(app: App) -> None:
    inst = _require_instance(app)
    title = prompt("New title", inst.name)
    res = app.console().rename(title, index=inst.index)
    if not res.ok:
        raise RuntimeError(res.text or res.stderr)
    app.set_target(title, inst.index)
    print(c(f"renamed to '{title}'", GREEN))


def act_props(app: App) -> None:
    inst = _require_instance(app)
    print(f"name        : {inst.name}")
    print(f"index       : {inst.index}")
    print(f"running     : {inst.info.running}")
    if inst.info.running:
        try:
            print(f"adb endpoint: {app.adb().discover(inst.index)}")
        except AdbError as exc:
            print(f"adb endpoint: (unreachable: {exc})")
        print(f"booted      : {app.adb().is_boot_completed(inst.index)}")
    else:
        print("adb endpoint: (instance stopped)")


def act_select_target(app: App) -> None:
    _require_instance(app)
    print(c(f"target set: {app.target_label()}", GREEN))


# ------------------------------------------------------------------- apps
def act_install(app: App) -> None:
    inst = _require_instance(app)
    apk = prompt("APK path (.apk/.apkm/.xapk/.apks)")
    if not Path(apk).is_file():
        raise InstanceError(f"APK not found: {apk}")
    wait = confirm("Wait for adb + verify install?", default=True)
    if wait:
        inst.install_apk_wait(apk)
    else:
        inst.install_apk(apk)
    print(c(f"installed {Path(apk).name} into {inst.name}", GREEN))


def act_uninstall(app: App) -> None:
    inst = _require_instance(app)
    pkg = prompt("Package name", "")
    if pkg:
        inst.uninstall_app(pkg)
        print(c(f"uninstalled {pkg}", GREEN))


def act_run_app(app: App) -> None:
    inst = _require_instance(app)
    pkg = prompt("Package name", "")
    if pkg:
        inst.run_app(pkg)
        print(c(f"started {pkg}", GREEN))


def act_stop_app(app: App) -> None:
    inst = _require_instance(app)
    pkg = prompt("Package name", "")
    if pkg:
        inst.stop_app(pkg)
        print(c(f"stopped {pkg}", GREEN))


def act_list_packages(app: App) -> None:
    inst = _require_instance(app)
    out = app.adb().shell(inst.index, ["pm", "list", "packages", "-3"],
                          discover=True)
    pkgs = [ln.strip().replace("package:", "") for ln in out.splitlines()
            if "package:" in ln]
    print(c(f"Third-party packages on {inst.name} ({len(pkgs)}):",
            BOLD, WHITE))
    for p in sorted(pkgs):
        print(f"  {p}")


# ---------------------------------------------------------- backup/restore
def act_backup_full(app: App) -> None:
    inst = _require_instance(app)
    dest = prompt("Destination dir", "backups")
    tag = prompt("Tag (optional)", "")
    path = backup_mod.full_export(app.console(), index=inst.index,
                                  dest_dir=dest, tag=tag or None)
    print(c(f"snapshot -> {path}", GREEN))


def act_restore_full(app: App) -> None:
    inst = _require_instance(app)
    file_ = prompt("Backup .ldbk path", "")
    if file_:
        backup_mod.full_restore(app.console(), backup_file=file_,
                                index=inst.index)
        print(c("restored", GREEN))


def act_backup_app(app: App) -> None:
    inst = _require_instance(app)
    pkg = prompt("Package name", "")
    if not pkg:
        return
    dest = prompt("Destination dir", "backups")
    prefer_ld = confirm("Use LDPlayer backup first?", default=True)
    path = backup_mod.app_backup(app.console(), app.adb(), inst, pkg, dest,
                                 prefer_ldplayer=prefer_ld)
    print(c(f"app backup -> {path}", GREEN))


def act_restore_app(app: App) -> None:
    inst = _require_instance(app)
    pkg = prompt("Package name", "")
    file_ = prompt("Backup file (.ldbk or .ab)", "")
    if not pkg or not file_:
        return
    apk = ""
    if Path(file_).suffix.lower() == ".ab":
        apk = prompt("APK to reinstall first (optional)", "")
    backup_mod.app_restore(app.console(), app.adb(), inst, pkg, file_,
                           apk=apk or None)
    print(c("restored", GREEN))


# ------------------------------------------------------------- phone setup
def _profile_options(app: App) -> dict:
    vendor = app.list_menu(
        "Vendor (random = any)",
        [(v, v) for v in VENDORS] + [("(random)", None)])
    cpu = prompt("CPU cores", "2")
    memory = prompt("RAM (MB)", "2048")
    res = prompt("Resolution", "720,1280,320")
    return {
        "vendor": vendor,
        "cpu": int(cpu) if cpu.isdigit() else 2,
        "memory": int(memory) if memory.isdigit() else 2048,
        "resolution": res or "720,1280,320",
    }


def act_configure(app: App) -> None:
    inst = _require_instance(app)
    opts = _profile_options(app)
    root = confirm("Enable root?", default=False)
    fast = confirm("Performance tweaks (fast fps/fastplay)?", default=True)
    light = confirm("Lightweight resolution / lock window?", default=True)
    audio_off = confirm("Mute audio?", default=True)
    profile = device_mod.apply_profile(
        app.console(), index=inst.index, **opts,
        root=root, fast=fast, light=light, audio_off=audio_off)
    print(c("applied unique phone profile:", GREEN))
    print(f"  {profile.summary()}")


def act_setup(app: App) -> None:
    """One-shot: unique profile + launch + install an APK."""
    inst = _require_instance(app)
    apk = prompt("APK path")
    if not Path(apk).is_file():
        raise InstanceError(f"APK not found: {apk}")
    opts = _profile_options(app)
    root = confirm("Enable root?", default=False)
    profile = device_mod.apply_profile(app.console(), index=inst.index,
                                       **opts, root=root)
    print(f"phone profile: {profile.summary()}")
    inst.launch(boot_wait=False)
    pkg = inst.install_apk_wait(apk)
    print(c(f"done: {pkg} installed, {inst.name} ready", GREEN))


# ------------------------------------------------------------------ actions
def act_tap(app: App) -> None:
    inst = _require_instance(app)
    x = prompt("X", "0")
    y = prompt("Y", "0")
    Automator(app.console(), app.adb(), inst).tap(int(x), int(y))
    print(c(f"tapped ({x}, {y})", GREEN))


def act_tap_center(app: App) -> None:
    inst = _require_instance(app)
    Automator(app.console(), app.adb(), inst).tap_center()
    print(c("tapped center", GREEN))


def act_swipe(app: App) -> None:
    inst = _require_instance(app)
    x1 = prompt("from X", "0")
    y1 = prompt("from Y", "0")
    x2 = prompt("to X", "0")
    y2 = prompt("to Y", "0")
    dur = prompt("duration ms", "200")
    Automator(app.console(), app.adb(), inst).swipe(
        int(x1), int(y1), int(x2), int(y2), int(dur))
    print(c("swiped", GREEN))


def act_text(app: App) -> None:
    inst = _require_instance(app)
    text = prompt("Text to type")
    if text:
        Automator(app.console(), app.adb(), inst).type_text(text)
        print(c("typed", GREEN))


def act_key(app: App) -> None:
    inst = _require_instance(app)
    key = app.list_menu("Key event", [
        ("HOME", "3"), ("BACK", "4"), ("ENTER", "66"),
        ("MENU/UNLOCK", "82"), ("WAKEUP", "224"),
        ("custom keycode", "custom")])
    if key is None:
        return
    auto = Automator(app.console(), app.adb(), inst)
    if key == "custom":
        code = prompt("Keycode")
        if not code.isdigit():
            return
        auto.key(int(code))
    else:
        auto.key(int(key))
    print(c(f"sent key {key}", GREEN))


def act_screencap(app: App) -> None:
    inst = _require_instance(app)
    dest = prompt("Output path", "screenshot.png")
    path = app.adb().screencap(inst.index, dest)
    print(c(f"screenshot -> {path}", GREEN))


def act_focus(app: App) -> None:
    inst = _require_instance(app)
    print(c(f"focused: {app.adb().focused_activity(inst.index)}", CYAN))


def act_facebook(app: App) -> None:
    inst = _require_instance(app)
    pkg = prompt("Package", "com.facebook.katana")
    apk = prompt("Facebook APK path (blank = auto-detect)", "")
    hold = confirm("Pause after first 'Create new account'?", default=False)
    grant = confirm("Pre-grant permissions?", default=True)
    boot_to = prompt("Boot timeout (s)", "600")
    facebook_mod.signup_flow(
        app.console(), app.adb(), index=inst.index, package=pkg,
        hold=hold, grant_perms=grant,
        boot_timeout=int(boot_to) if boot_to.isdigit() else 600,
        apk_path=apk or None)
    print(c("facebook flow finished", GREEN))


# ----------------------------------------------------------------- emulator
def act_window_fit(app: App) -> None:
    scale = prompt("Scale (e.g. 1.0, 0.9)", "1.0")
    center = confirm("Center the window?", default=True)
    report = window_mod.fit_window(scale=float(scale), center=center)
    if not report.get("found"):
        raise RuntimeError(report.get("error", "window not found"))
    b = report["before"]
    a = report["after"]
    print(c(f"window resized to {a[2]-a[0]}x{a[3]-a[1]} "
            f"(fits {report['screen'][2]}x{report['screen'][3]} work area)",
            GREEN))


def act_repair(app: App) -> None:
    kill = confirm("Kill stale emulator processes?", default=True)
    report = repair_mod.repair(verbose=True, kill=kill)
    for w in report["warnings"]:
        print(c(f"  [!] {w}", YELLOW))
    print(f"  killed {len(report['killed'])} stale process(es), "
          f"cleared read-only on {len(report['fixed_vmdks'])} vmdk(s)")


def act_reboot(app: App) -> None:
    inst = _require_instance(app)
    res = app.console().reboot(index=inst.index)
    if not res.ok:
        raise RuntimeError(res.text or res.stderr)
    print(c("reboot sent", GREEN))


def act_adb_shell(app: App) -> None:
    inst = _require_instance(app)
    cmd = prompt("adb shell command", "")
    if cmd:
        out = app.adb().shell(inst.index, shlex.split(cmd), discover=True)
        print(out, end="" if out.endswith("\n") else "\n")


def act_console_raw(app: App) -> None:
    cmd = prompt("ldconsole command", "")
    if not cmd:
        return
    res = app.console().run(shlex.split(cmd))
    if res.stdout:
        print(res.stdout, end="")
    if res.stderr:
        print(res.stderr, end="")
    if not res.ok:
        print(c(f"(exit code {res.returncode})", YELLOW))


def act_restart_adb(app: App) -> None:
    adb = Path(app.cfg["adb"])
    subprocess.run([str(adb), "kill-server"], check=False)
    subprocess.run([str(adb), "start-server"], check=False)
    print(c("adb server restarted", GREEN))


# -------------------------------------------------------------------- config
def act_edit_config(app: App) -> None:
    entries = [(f"{key}  =  {app.cfg.get(key)}", key) for key, _d, _t
               in CONFIG_EDITABLE]
    key = app.list_menu("Edit config value", entries)
    if key is None:
        return
    _label, _desc, kind = next(k for k in CONFIG_EDITABLE if k[0] == key)
    current = str(app.cfg.get(key, ""))
    val = prompt(f"New value for '{key}'", current)
    if not val:
        return
    if kind == "int":
        if not val.isdigit():
            raise RuntimeError(f"'{key}' expects an integer")
        app.cfg[key] = int(val)
    else:
        app.cfg[key] = val
    save_config(app.cfg)
    app.refresh_cfg()
    print(c(f"saved {key} = {app.cfg[key]}", GREEN))


def act_set_default_instance(app: App) -> None:
    if not app.ensure_configured():
        return
    rows = list_instances(app.console())
    options = [(f"[{r.index}] {r.name}", r.name) for r in rows]
    options.append(("(none)", ""))
    pick = app.list_menu("Set default instance", options)
    app.cfg["default_instance"] = pick or ""
    save_config(app.cfg)
    app.refresh_cfg()
    print(c(f"default instance set to '{pick or 'none'}'", GREEN))


def act_init(app: App) -> None:
    app.do_init()
    print(c("config written; re-detected LDPlayer + adb", GREEN))


def act_show_status(app: App) -> None:
    app.show_status()


# ---------------------------------------------------------------------------
# menu tree
# ---------------------------------------------------------------------------

def build_menus(app: App) -> Menu:
    def leaf(label, fn):
        return (label, _wrap(app, fn))

    instances = Menu("Instances", [
        leaf("List instances", act_list_instances),
        leaf("Select target instance", act_select_target),
        leaf("Launch instance", act_launch),
        leaf("Quit instance", act_quit),
        leaf("Reboot instance", act_reboot),
        leaf("Create instance", act_create),
        leaf("Modify instance", act_modify),
        leaf("Rename instance", act_rename),
        leaf("Remove instance", act_remove),
        leaf("Show instance properties", act_props),
    ], app)

    apps = Menu("Apps", [
        leaf("Install APK / bundle", act_install),
        leaf("Uninstall app", act_uninstall),
        leaf("Run app", act_run_app),
        leaf("Stop app", act_stop_app),
        leaf("List installed (3rd-party) apps", act_list_packages),
    ], app)

    backups = Menu("Backup & Restore", [
        leaf("Full instance backup", act_backup_full),
        leaf("Full instance restore", act_restore_full),
        leaf("App backup", act_backup_app),
        leaf("App restore", act_restore_app),
    ], app)

    setup = Menu("Phone Profile & Setup", [
        leaf("Configure unique phone profile", act_configure),
        leaf("One-shot setup (profile + launch + install)", act_setup),
    ], app)

    automation = Menu("Automation / Actions", [
        leaf("Tap at coordinates", act_tap),
        leaf("Tap center", act_tap_center),
        leaf("Swipe", act_swipe),
        leaf("Type text", act_text),
        leaf("Send key event", act_key),
        leaf("Take screenshot", act_screencap),
        leaf("Show focused activity", act_focus),
        leaf("Facebook signup flow", act_facebook),
    ], app)

    emulator = Menu("Emulator", [
        leaf("Fit LDPlayer window to screen", act_window_fit),
        leaf("Repair (kill stale / read-only vmdk)", act_repair),
        leaf("Restart adb server", act_restart_adb),
        leaf("Run adb shell command", act_adb_shell),
        leaf("Run raw ldconsole command", act_console_raw),
    ], app)

    config = Menu("Config & System", [
        leaf("Show status / doctor", act_show_status),
        leaf("Auto-detect LDPlayer + adb (init)", act_init),
        leaf("Edit config values", act_edit_config),
        leaf("Set default instance", act_set_default_instance),
    ], app)

    def _quit():
        raise _QuitTui()

    root = Menu("LDCLI CONTROL CENTER", [
        ("Instances", instances),
        ("Apps", apps),
        ("Backup & Restore", backups),
        ("Phone Profile & Setup", setup),
        ("Automation / Actions", automation),
        ("Emulator", emulator),
        ("Config & System", config),
        ("Quit", _quit),
    ], app)
    return root


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> int:
    enable_ansi()
    app = App()
    if not app.ensure_configured():
        clear()
        print(c("LDPlayer/adb not configured - run `python ldcli.py init` "
                "first.", RED))
        return 1
    try:
        app.run_main(build_menus(app))
    finally:
        clear()
        print(c("bye", DIM))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
