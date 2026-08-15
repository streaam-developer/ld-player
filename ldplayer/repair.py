"""Repair / health-check utilities for a broken LDPlayer install.

Addresses the classic LDPlayer failure:

    Power-up of virtual machine failed (PowerUpFailed)
    VERR_VD_IMAGE_READ_ONLY - Image is read-only.

Typical causes:
  * a cloned instance left its vmdk chain read-only / locked
  * a stale dnplayer / VBoxHeadless process still holds a disk
  * the adb server has stale serials

``repair`` clears those states so the emulator can power up again.
"""

from __future__ import annotations

import os
import subprocess
import time

from pathlib import Path

from .config import find_adb, find_ldconsole, load_config

PROCESS_NAMES = [
    "dnplayer",      # LDPlayer main / emulator launcher
    "VBoxHeadless",  # VirtualBox VM headless host
    "ld.exe",        # LDPlayer overlay/assistant
    "ldrecord",      # recorder helper
    "ldtool",        # tool helper
    "ldcam",         # camera helper
]


def _is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def kill_stale_processes(verbose: bool = True) -> list[str]:
    """Terminate leftover LDPlayer / VirtualBox processes holding disks."""
    killed: list[str] = []
    try:
        out = subprocess.run(
            ["taskkill", "/F", "/IM", "dnplayer.exe"],
            capture_output=True, text=True, check=False,
        )
        out2 = subprocess.run(
            ["taskkill", "/F", "/IM", "VBoxHeadless.exe"],
            capture_output=True, text=True, check=False,
        )
        for line in (out.stdout + out2.stdout).splitlines():
            if "SUCCESS" in line:
                killed.append(line.strip())
    except FileNotFoundError:
        pass
    if verbose:
        for line in killed:
            print(f"  killed: {line}")
    return killed


def clear_readonly_vmdks(base_dir: str | Path) -> list[Path]:
    """Clear the read-only attribute on every vmdk under the LDPlayer root."""
    base = Path(base_dir)
    fixed: list[Path] = []
    if not base.is_dir():
        return fixed
    for vmdk in base.rglob("*.vmdk"):
        try:
            if vmdk.stat().st_file_attributes & 1:  # FILE_ATTRIBUTE_READONLY
                os.chmod(vmdk, 0o666)
                fixed.append(vmdk)
        except OSError:
            continue
    return fixed


def restart_adb_server(adb: str | Path, verbose: bool = True) -> None:
    adb = str(adb)
    subprocess.run([adb, "kill-server"], capture_output=True, text=True,
                   check=False)
    time.sleep(1)
    subprocess.run([adb, "start-server"], capture_output=True, text=True,
                   check=False)
    if verbose:
        print("  adb server restarted")


def run_ldplayer_repair(console_dir: str | Path) -> bool:
    """Invoke LDPlayer's built-in repair tool if present."""
    exe = Path(console_dir) / "dnrepairer.exe"
    if not exe.is_file():
        return False
    print("launching LDPlayer repair tool (follow the UI)...")
    subprocess.Popen([str(exe)])
    return True


def repair(verbose: bool = True, run_repair_tool: bool = False,
           kill: bool = True) -> dict:
    """Perform a health check and apply fixes. Returns a status report."""
    cfg = load_config()
    report: dict = {"fixed_vmdks": [], "killed": [], "warnings": []}

    console = Path(cfg["ldconsole"]) if cfg.get("ldconsole") else find_ldconsole()
    if not console:
        report["warnings"].append("ldconsole.exe not found")
    else:
        ldroot = console.parent

        if kill:
            report["killed"] = kill_stale_processes(verbose)

        report["fixed_vmdks"] = [str(p) for p in
                                 clear_readonly_vmdks(ldroot)]
        if verbose and report["fixed_vmdks"]:
            print(f"  cleared read-only on "
                  f"{len(report['fixed_vmdks'])} vmdk file(s)")

        adb = Path(cfg["adb"]) if cfg.get("adb") else find_adb()
        if adb:
            restart_adb_server(adb, verbose)

        if run_repair_tool:
            run_ldplayer_repair(ldroot)

        if not _is_admin():
            report["warnings"].append(
                "not running as admin - if drivers need reinstall, "
                "re-run 'ldcli repair' from an elevated prompt")

    if verbose:
        print("repair complete")
    return report
