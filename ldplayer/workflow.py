"""End-to-end workflows that compose the lower-level pieces.

The flagship command is ``roll``:

    backup old instance  ->  launch old  ->  install APK into old
    ->  clone old into a NEW instance  ->  launch the new one
    ->  keep the old one running (or quit it)

This gives you a fresh LDPlayer ready to work while the original stays
available, with a full snapshot taken first.
"""

from __future__ import annotations

from pathlib import Path

from .adb import Adb
from .config import load_config
from .console import LdConsole
from .instance import Instance, InstanceError, create_instance
from .backup import full_export


class WorkflowError(RuntimeError):
    pass


def _session(cfg):
    return (
        LdConsole(cfg["ldconsole"], timeout=cfg["command_timeout"],
                  base_port=cfg["adb_port_base"]),
        Adb(cfg["adb"], base_port=cfg["adb_port_base"]),
    )


def roll(
    apk: str | Path | None = None,
    new_name: str | None = None,
    old_name: str | None = None,
    backup_dir: str | Path = "backups",
    tag: str | None = None,
    quit_old: bool = False,
    launch_old: bool = True,
    boot_wait: bool = True,
    source: str | None = None,
    cpu: int | None = None,
    memory: int | None = None,
    resolution: str | None = None,
) -> dict:
    """Full roll: snapshot old -> launch old -> install apk -> clone -> open new."""
    cfg = load_config()
    console, adb = _session(cfg)
    old_name = old_name or cfg.get("default_instance", "leidian0")
    new_name = new_name or f"{old_name}-fresh"

    old_inst = console.find(name=old_name)
    if not old_inst:
        raise WorkflowError(f"old instance '{old_name}' does not exist")

    results: dict = {"old": old_name, "new": new_name}

    # 1) full backup of the old instance
    snapshot = full_export(console, old_name, backup_dir, tag or "pre-roll")
    results["snapshot"] = str(snapshot)

    old = Instance(console, adb, name=old_name)
    old.resolve()

    # 2) ensure old instance is running
    if not old.info.running and not console.is_running(index=old.index):
        if not launch_old:
            raise WorkflowError(
                f"old instance '{old_name}' is not running and --no-launch-old set")
        print(f"launching old instance '{old_name}' ...")
        old.launch(boot_wait=boot_wait)

    # 3) install the APK into the old instance
    if apk:
        old.install_apk_wait(apk)

    # 4) clone old into a brand new instance
    print(f"cloning '{old_name}' -> '{new_name}' ...")
    create_instance(console, new_name, source=source or old_name,
                    cpu=cpu, memory=memory, resolution=resolution)
    new = Instance(console, adb, name=new_name)
    new.resolve()
    results["new_index"] = new.index

    # 5) launch the new instance
    new.launch(boot_wait=boot_wait)
    results["new_launched"] = True

    # 6) decide fate of the old one
    if quit_old:
        print(f"quitting old instance '{old_name}' (snapshot kept)...")
        old.quit()
        results["old_running"] = False
    else:
        results["old_running"] = True
        print(f"[+] old instance '{old_name}' kept running "
              f"(it was fully backed up to {snapshot.name})")

    print(f"[+] new instance '{new_name}' is up (adb port {new.adb_port})")
    return results
