"""Detection and persistence of LDPlayer / adb paths.

Config file lives at %USERPROFILE%\\.ldplayer-cli\\config.json
"""

import json
import os
import re
import winreg

from pathlib import Path

CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "ldplayer-cli"
CONFIG_FILE = CONFIG_DIR / "config.json"

LDPLAYER_COMMON_DIRS = [
    
    "C:\\leidian\\LDPlayer9",
    "C:\\LDPlayer\\LDPlayer9",
    "D:\\LDPlayer\\LDPlayer9",
    "E:\\LDPlayer\\LDPlayer9",
    "C:\\Program Files\\LDPlayer\\LDPlayer9",
    "C:\\Program Files (x86)\\LDPlayer\\LDPlayer9",
    "D:\\Program Files\\LDPlayer\\LDPlayer9",
    "C:\\dnplayer",
]

ADB_COMMON_DIRS = [
    "C:\\ADB",
    "C:\\adb",
    "C:\\Platform-Tools",
    "C:\\platform-tools",
]


def _walk_dir(root: str | Path, max_depth: int = 4) -> Path | None:
    """Look for ldconsole.exe under a root directory (bounded depth)."""
    root = Path(root)
    if not root.is_dir():
        return None
    stack = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        if depth > max_depth:
            continue
        candidate = cur / "ldconsole.exe"
        if candidate.is_file():
            return candidate
        try:
            for child in cur.iterdir():
                if child.is_dir():
                    stack.append((child, depth + 1))
        except OSError:
            continue
    return None


def _find_ldconsole_by_registry() -> Path | None:
    try:
        roots = [
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hkey, subkey in roots:
            try:
                with winreg.OpenKey(hkey, subkey) as base:
                    for i in range(winreg.QueryInfoKey(base)[0]):
                        try:
                            with winreg.OpenKey(base, winreg.EnumKey(base, i)) as k:
                                name = winreg.QueryValueEx(k, "DisplayName")[0]
                                if name and "ldplayer" in name.lower():
                                    loc = winreg.QueryValueEx(k, "InstallLocation")[0]
                                    if loc:
                                        cand = Path(loc) / "ldconsole.exe"
                                        if cand.is_file():
                                            return cand
                        except OSError:
                            continue
            except OSError:
                continue
    except Exception:
        return None
    return None


def find_ldconsole() -> Path | None:
    """Locate ldconsole.exe for LDPlayer 9."""
    env = os.environ.get("LDPLAYER_HOME") or os.environ.get("LDPLAYER9_HOME")
    if env:
        cand = Path(env) / "ldconsole.exe"
        if cand.is_file():
            return cand
        found = _walk_dir(env)
        if found:
            return found

    reg = _find_ldconsole_by_registry()
    if reg:
        return reg

    for d in LDPLAYER_COMMON_DIRS:
        cand = Path(d) / "ldconsole.exe"
        if cand.is_file():
            return cand
        found = _walk_dir(d)
        if found:
            return found

    return None


def find_adb() -> Path | None:
    """Locate adb.exe (Android SDK platform-tools or the one LDPlayer ships)."""
    env = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if env:
        cand = Path(env) / "platform-tools" / "adb.exe"
        if cand.is_file():
            return cand

    console = find_ldconsole()
    if console:
        for cand in (console.parent / "adb.exe",):
            if cand.is_file():
                return cand

    for d in ADB_COMMON_DIRS:
        cand = Path(d) / "adb.exe"
        if cand.is_file():
            return cand

    return None


def _defaults() -> dict:
    return {
        "ldconsole": str(find_ldconsole()) if find_ldconsole() else None,
        "adb": str(find_adb()) if find_adb() else None,
        "adb_port_base": 5555,
        "default_instance": "leidian0",
        "launch_timeout": 120,
        "boot_timeout": 180,
        "command_timeout": 300,
        "cf_worker_url": "",
        "cf_worker_api_key": "",
    }


def load_config() -> dict:
    if CONFIG_FILE.is_file():
        try:
            stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            merged = _defaults()
            merged.update(stored)
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return _defaults()


def save_config(cfg: dict) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return CONFIG_FILE


def status() -> dict:
    """Health-check snapshot of the detected toolchain."""
    cfg = load_config()
    console = Path(cfg["ldconsole"]) if cfg.get("ldconsole") else find_ldconsole()
    adb = Path(cfg["adb"]) if cfg.get("adb") else find_adb()
    return {
        "ldconsole": str(console) if console else None,
        "ldconsole_ok": bool(console and console.is_file()),
        "adb": str(adb) if adb else None,
        "adb_ok": bool(adb and adb.is_file()),
        "config_file": str(CONFIG_FILE),
        "config": cfg,
    }


def version_of(console_path: str | Path) -> str | None:
    """Query ldconsole.exe --version when supported."""
    import subprocess
    try:
        out = subprocess.run(
            [str(console_path), "--version"],
            capture_output=True, text=True, timeout=15, check=False,
        ).stdout.strip()
        m = re.search(r"(\d+\.\d+[\.\d]*)", out)
        return m.group(1) if m else out
    except Exception:
        return None
