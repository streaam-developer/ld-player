"""Backup & restore for LDPlayer.

Verified against LDPlayer v9.5.31.0 command set:

  * full - ``ldconsole backup --index N --file X.ldbk``  (whole instance)
           ``ldconsole restore --index N --file X.ldbk``
  * app  - ``ldconsole backupapp --packagename PKG --file X.ldbk``
           ``ldconsole restoreapp --packagename PKG --file X.ldbk``

Both are native LDPlayer formats (`.ldbk`). App restores via adb `.ab` are
kept as a fallback for third-party restores.
"""

from __future__ import annotations

import time

from pathlib import Path

from .adb import Adb
from .config import load_config
from .console import LdConsole
from .instance import Instance


class BackupError(RuntimeError):
    pass


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _session(cfg: dict | None = None):
    cfg = cfg or load_config()
    return (
        LdConsole(cfg["ldconsole"], timeout=cfg["command_timeout"],
                  base_port=cfg["adb_port_base"]),
        Adb(cfg["adb"], base_port=cfg["adb_port_base"]),
    )


# ===========================================================================
# FULL instance snapshot
# ===========================================================================

def full_export(console: LdConsole, name: str | None = None,
                dest_dir: str | Path = "backups", tag: str | None = None,
                index: int | None = None) -> Path:
    inst = console.find(name=name, index=index)
    if not inst:
        raise BackupError(f"instance (name={name}, index={index}) does not exist")
    dest_dir = _ensure_dir(Path(dest_dir))
    stem = f"{inst.name}-{tag or 'backup'}-{_timestamp()}"
    out = dest_dir / f"{stem}.ldbk"
    res = console.backup_full(out, index=inst.index)
    if not res.ok:
        raise BackupError(f"backup failed: {res.text or res.stderr}")
    if not out.is_file():
        raise BackupError(f"backup did not produce {out}")
    print(f"[{inst.name}] full snapshot -> {out}")
    return out


def full_restore(console: LdConsole, name: str | None = None,
                 backup_file: str | Path = "", index: int | None = None) -> None:
    inst = console.find(name=name, index=index)
    if not inst:
        raise BackupError(f"instance (name={name}, index={index}) does not exist")
    backup_file = Path(backup_file)
    if not backup_file.is_file():
        raise BackupError(f"backup file not found: {backup_file}")
    res = console.restore_full(backup_file, index=inst.index)
    if not res.ok:
        raise BackupError(f"restore failed: {res.text or res.stderr}")
    print(f"[{inst.name}] restored {backup_file.name}")


# ===========================================================================
# Per-app backup
# ===========================================================================

def app_backup(console: LdConsole, adb: Adb, instance: Instance, package: str,
               dest_dir: str | Path, prefer_ldplayer: bool = True) -> Path:
    dest_dir = _ensure_dir(Path(dest_dir))
    inst = instance.resolve()

    if prefer_ldplayer:
        out = dest_dir / f"{inst.name}-{package}-{_timestamp()}.ldbk"
        res = console.backup_app(package, out, index=inst.index)
        if res.ok and out.is_file():
            print(f"[{inst.name}] app backup -> {out}")
            return out
        print(f"[{inst.name}] LDPlayer backup failed, using adb fallback: "
              f"{res.text or res.stderr}")

    out = adb.backup(inst.index, package, dest_dir / package)
    print(f"[{inst.name}] adb app backup -> {out}")
    return out


def app_restore(console: LdConsole, adb: Adb, instance: Instance, package: str,
                backup_file: str | Path, apk: str | Path | None = None) -> None:
    inst = instance.resolve()
    backup_file = Path(backup_file)
    if not backup_file.is_file():
        raise BackupError(f"backup file not found: {backup_file}")

    if backup_file.suffix.lower() == ".ldbk":
        res = console.restore_app(package, backup_file, index=inst.index)
        if not res.ok:
            raise BackupError(f"restoreapp failed: {res.text or res.stderr}")
        print(f"[{inst.name}] restored app data for {package}")
        return

    # adb .ab fallback
    if apk:
        res = console.install_apk(apk, index=inst.index)
        if not res.ok:
            raise BackupError(f"apk reinstall failed: {res.text or res.stderr}")
    adb.restore(inst.index, backup_file)
    print(f"[{inst.name}] restored {backup_file.name} via adb")


# ===========================================================================
# High-level helper used by workflows
# ===========================================================================

def backup_instance_now(cfg: dict | None = None, name: str | None = None,
                        dest_dir: str | Path = "backups") -> Path:
    cfg = cfg or load_config()
    console, _ = _session(cfg)
    name = name or cfg.get("default_instance", "leidian0")
    return full_export(console, name, dest_dir)
