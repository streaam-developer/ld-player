"""Backup & restore for LDPlayer.

Verified against LDPlayer v9.5.31.0 command set:

  * full - ``ldconsole backup --index N --file X.ldbk``  (whole instance)
           ``ldconsole restore --index N --file X.ldbk``
  * app  - ``ldconsole backupapp --packagename PKG --file X.ldbk``
           ``ldconsole restoreapp --packagename PKG --file X.ldbk``
  * tarball - raw ``.tar.gz`` filesystem snapshot (third-party / manual backups)
              extracted and pushed to ``/data/data/<pkg>`` via adb

Both LDPlayer native formats (``.ldbk``) and adb ``.ab`` are supported.
Tarball restores are a convenience path for raw app-data archives.
"""

from __future__ import annotations

import shutil
import tarfile
import time
import tempfile

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


# ===========================================================================
# Tarball (raw .tar.gz) restore
# ===========================================================================

def _detect_package_from_tarball(tarball: Path) -> str:
    """Peek inside the tarball and extract the Android package name.

    Expects the common layout ``data/data/<package>/...``.
    """
    try:
        with tarfile.open(tarball, "r:gz") as tf:
            for member in tf.getmembers():
                parts = member.name.replace("\\", "/").split("/")
                if len(parts) >= 3 and parts[0] in (".", "data") and parts[1] == "data":
                    pkg = parts[2]
                    if "." in pkg and not pkg.startswith("."):
                        return pkg
    except (tarfile.TarError, OSError) as exc:
        raise BackupError(f"cannot read tarball {tarball.name}: {exc}")
    raise BackupError(
        f"could not detect package name from {tarball.name} — "
        f"expected a data/data/<package>/... layout")


def _get_app_uid(adb: Adb, index: int, package: str) -> str:
    """Return the UID assigned to *package* on the device (e.g. ``u0_a137``)."""
    out = adb.shell(index, ["dumpsys", "package", package],
                    timeout=30, discover=True)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("userId="):
            # userId=10137  ->  uid string "u0_a137"
            try:
                uid_num = int(line.split("=", 1)[1].split()[0])
                app_id = uid_num % 100000
                user_id = uid_num // 100000
                return f"u{user_id}_a{app_id}"
            except (ValueError, IndexError):
                pass
    return "u0_a1000"  # fallback — unlikely to be correct but won't crash


def tarball_restore(console: LdConsole, adb: Adb,
                    instance: Instance, tarball: str | Path,
                    force: bool = False) -> None:
    """Restore a raw ``.tar.gz`` app-data snapshot into *instance*.

    The tarball must have a ``data/data/<package>/...`` layout.  The
    steps are:

      1. Extract to a temp directory.
      2. Detect the package name.
      3. Force-stop the app (if running).
      4. Remove the old ``/data/data/<pkg>`` directory.
      5. Push the new files.
      6. Fix ownership (``chown``) to the app's UID.
      7. Force-start the app so it re-reads the data.
    """
    inst = instance.resolve()
    tarball = Path(tarball)
    if not tarball.is_file():
        raise BackupError(f"tarball not found: {tarball}")

    # --- extract to temp dir ------------------------------------------------
    tmpdir = Path(tempfile.mkdtemp(prefix="ldcli_tarball_"))
    try:
        print(f"[{inst.name}] extracting {tarball.name} ...", flush=True)
        with tarfile.open(tarball, "r:gz") as tf:
            # guard against path traversal
            for member in tf.getmembers():
                dest = (tmpdir / member.name).resolve()
                if not str(dest).startswith(str(tmpdir.resolve())):
                    raise BackupError(
                        f"refusing to extract {member.name} — "
                        f"path traversal detected")
            tf.extractall(tmpdir)

        # --- detect package --------------------------------------------------
        pkg = _detect_package_from_tarball(tmpdir)
        print(f"[{inst.name}] detected package: {pkg}", flush=True)

        # verify the package is installed
        if not adb.package_installed(inst.index, pkg, discover=True):
            raise BackupError(
                f"package {pkg} is not installed on instance '{inst.name}' — "
                f"install the matching APK first")

        # --- locate the source data ------------------------------------------
        # try ./data/data/<pkg> first, then data/data/<pkg>
        data_root = None
        for candidate in (tmpdir / "data" / "data" / pkg,
                          tmpdir / pkg):
            if candidate.is_dir():
                data_root = candidate
                break
        if data_root is None:
            # fallback: search for the package dir anywhere
            for d in tmpdir.rglob(pkg):
                if d.is_dir() and d.parent.name in ("data", pkg):
                    data_root = d
                    break
        if data_root is None:
            raise BackupError(
                f"extracted tarball does not contain a "
                f"data/data/{pkg}/ directory")

        # --- force-stop the app ----------------------------------------------
        remote_data = f"/data/data/{pkg}"
        print(f"[{inst.name}] stopping {pkg} ...", flush=True)
        adb.shell(inst.index, ["am", "force-stop", pkg],
                  timeout=15, discover=True)

        # --- remove old data & push new data ---------------------------------
        print(f"[{inst.name}] clearing old data at {remote_data} ...",
              flush=True)
        adb.shell(inst.index, ["rm", "-rf", remote_data],
                  timeout=60, discover=True)
        adb.shell(inst.index, ["mkdir", "-p", remote_data],
                  timeout=15, discover=True)

        print(f"[{inst.name}] pushing data to {remote_data} ...", flush=True)
        adb.push(inst.index, str(data_root), remote_data,
                 discover=True, timeout=600)

        # --- fix ownership ---------------------------------------------------
        uid = _get_app_uid(adb, inst.index, pkg)
        print(f"[{inst.name}] chown {uid}:{uid} on {remote_data} ...",
              flush=True)
        adb.shell(inst.index, ["chown", "-R", f"{uid}:{uid}", remote_data],
                  timeout=60, discover=True)

        print(f"[{inst.name}] restore complete — starting {pkg} ...",
              flush=True)
        adb.shell(inst.index, ["monkey", "-p", pkg, "-c",
                               "android.intent.category.LAUNCHER", "1"],
                  timeout=15, discover=True)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
