#!/usr/bin/env python3
"""ldcli - LDPlayer automation CLI.

Usage: python ldcli.py <command> [options]

Run `python ldcli.py init` first to detect your LDPlayer 9 + adb install.
"""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

from ldplayer import __version__
from ldplayer.config import (find_adb, find_ldconsole, load_config,
                             save_config, status, version_of)
from ldplayer.console import LdConsole, LdConsoleError
from ldplayer.adb import Adb
from ldplayer.instance import Instance, InstanceError, create_instance, list_instances
from ldplayer.automation import Automator
from ldplayer import backup as backup_mod
from ldplayer import workflow as workflow_mod
from ldplayer import repair as repair_mod
from ldplayer import device as device_mod
from ldplayer import window as window_mod
from ldplayer.device import DeviceProfile


def _session(args) -> tuple[LdConsole, Adb, dict]:
    cfg = load_config()
    if not cfg.get("ldconsole") or not cfg.get("adb"):
        die("LDPlayer/adb not configured. Run: ldcli init")
    return (LdConsole(cfg["ldconsole"], timeout=cfg["command_timeout"],
                      base_port=cfg["adb_port_base"]),
            Adb(cfg["adb"], base_port=cfg["adb_port_base"]), cfg)


def _pick(args, console) -> tuple[str | None, int | None]:
    name = getattr(args, "name", None)
    index = getattr(args, "index", None)
    if not name and index is None and getattr(args, "instance", None):
        # e.g. --instance <name-or-index>
        raw = args.instance
        if raw.isdigit():
            return None, int(raw)
        return raw, None
    return name, index


def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ------------------------------------------------------------------ commands
def cmd_init(args):
    console = find_ldconsole()
    adb = find_adb()
    cfg = load_config()
    if console:
        cfg["ldconsole"] = str(console)
    if adb:
        cfg["adb"] = str(adb)
    if args.default_instance:
        cfg["default_instance"] = args.default_instance
    path = save_config(cfg)
    print(f"config written to {path}")
    cmd_doctor(None)


def cmd_doctor(_):
    info = status()
    print("LDPlayer toolchain status")
    print(f"  ldconsole : {info['ldconsole'] or 'NOT FOUND'}")
    if info["ldconsole"]:
        v = version_of(info["ldconsole"])
        if v:
            print(f"  version   : {v}")
    print(f"  adb       : {info['adb'] or 'NOT FOUND'}")
    print(f"  config    : {info['config_file']}")
    if not info["ldconsole_ok"]:
        print("  [!] LDPlayer 9 not found - set LDPLAYER_HOME or install it.")
    if not info["adb_ok"]:
        print("  [!] adb not found - set ANDROID_HOME or LDPlayer's bundled adb.")


def cmd_list(args):
    console, _, _ = _session(args)
    rows = list_instances(console)
    if not rows:
        print("no instances found")
        return
    print(f"{'idx':>4}  {'status':<8}  name")
    for r in rows:
        print(f"{r.index:>4}  {'running' if r.running else 'stopped':<8}  {r.name}")


def _instance(args, console, adb) -> Instance:
    name, index = _pick(args, console)
    inst = Instance(console, adb, name=name, index=index)
    try:
        inst.resolve()
    except InstanceError as exc:
        die(str(exc))
    return inst


def cmd_launch(args):
    console, adb, cfg = _session(args)
    _instance(args, console, adb).launch(
        boot_wait=not args.no_boot_wait)
    print("launched")


def cmd_quit(args):
    console, adb, _ = _session(args)
    _instance(args, console, adb).quit()
    print("quit")


def cmd_add(args):
    console, _, _ = _session(args)
    create_instance(console, args.name, source=args.source,
                    cpu=args.cpu_num, memory=args.memory,
                    resolution=args.resolution)


def cmd_modify(args):
    console, _, _ = _session(args)
    name, index = _pick(args, console)
    res = console.modify(name=name, index=index, cpu=args.cpu_num,
                         memory=args.memory, resolution=args.resolution)
    if not res.ok:
        die(res.text or res.stderr)
    print("modified")


def cmd_remove(args):
    console, _, _ = _session(args)
    name, index = _pick(args, console)
    res = console.remove(name=name, index=index)
    if not res.ok:
        die(res.text or res.stderr)
    print("removed")


def cmd_rename(args):
    console, _, _ = _session(args)
    name, index = _pick(args, console)
    res = console.rename(args.title, name=name, index=index)
    if not res.ok:
        die(res.text or res.stderr)
    print(f"renamed to '{args.title}'")


def cmd_install(args):
    console, adb, _ = _session(args)
    inst = _instance(args, console, adb)
    if args.wait:
        inst.install_apk_wait(args.apk, adb_timeout=args.adb_timeout)
    else:
        inst.install_apk(args.apk)


def cmd_uninstall(args):
    console, adb, _ = _session(args)
    _instance(args, console, adb).uninstall_app(args.package)
    print(f"uninstalled {args.package}")


def cmd_run(args):
    console, adb, _ = _session(args)
    _instance(args, console, adb).run_app(args.package)
    print(f"started {args.package}")


def cmd_stop(args):
    console, adb, _ = _session(args)
    _instance(args, console, adb).stop_app(args.package)


def cmd_backup(args):
    console, adb, cfg = _session(args)
    if args.kind == "full":
        name, index = _pick(args, console)
        if not name and index is None:
            name = cfg.get("default_instance", "leidian0")
        backup_mod.full_export(console, name=name, dest_dir=args.dest,
                               tag=args.tag, index=index)
    elif args.kind == "app":
        inst = _instance(args, console, adb)
        backup_mod.app_backup(console, adb, inst, args.package, args.dest,
                              prefer_ldplayer=not args.adb_mode)
    else:
        die(f"unknown backup kind: {args.kind}")


def cmd_restore(args):
    console, adb, _ = _session(args)
    if args.kind == "full":
        name, index = _pick(args, console)
        if not name and index is None:
            name = cfg_default_name()
        backup_mod.full_restore(console, name=name, backup_file=args.file,
                                index=index)
    elif args.kind == "app":
        inst = _instance(args, console, adb)
        backup_mod.app_restore(console, adb, inst, args.package, args.file,
                               apk=args.apk)
    else:
        die(f"unknown restore kind: {args.kind}")


def cfg_default_name() -> str:
    return load_config().get("default_instance", "leidian0")


def cmd_roll(args):
    workflow_mod.roll(
        apk=args.apk,
        new_name=args.new_name,
        old_name=args.old_name,
        backup_dir=args.backup_dir,
        tag=args.tag,
        quit_old=args.quit_old,
        launch_old=not args.no_launch_old,
        boot_wait=not args.no_boot_wait,
        source=args.source,
        cpu=args.cpu_num,
        memory=args.memory,
        resolution=args.resolution,
    )


def cmd_action(args):
    console, adb, _ = _session(args)
    inst = _instance(args, console, adb)
    auto = Automator(console, adb, inst)
    if args.key == "tap":
        auto.tap(args.x, args.y)
        print(f"tapped ({args.x}, {args.y})")
    elif args.key == "tap-center":
        auto.tap_center()
    elif args.key == "swipe":
        auto.swipe(args.x, args.y, args.x2, args.y2, args.duration)
    elif args.key == "text":
        auto.type_text(args.text)
    elif args.key == "home":
        auto.home()
    elif args.key == "back":
        auto.back()
    elif args.key == "enter":
        auto.enter()
    elif args.key == "key":
        auto.key(args.keycode)
    elif args.key == "screencap":
        path = auto.screenshot(args.path)
        print(f"screenshot -> {path}")
    elif args.key == "focus":
        print(auto.focused_activity() or "(no focus info)")
    else:
        die(f"unknown action: {args.key}")


def cmd_screencap(args):
    console, adb, _ = _session(args)
    inst = _instance(args, console, adb)
    path = adb.screencap(inst.index, args.path)
    print(f"screenshot -> {path}")


def cmd_adb(args):
    console, adb, _ = _session(args)
    inst = _instance(args, console, adb)
    if args.ldconsole:
        res = console.adb(args.command, index=inst.index)
        print(res.stdout, end="")
        if not res.ok:
            sys.exit(res.returncode)
        return
    if args.raw:
        import subprocess
        serial = adb.discover(inst.index)
        proc = subprocess.run([adb.adb, "-s", serial] + args.raw,
                              text=True, check=False)
        sys.exit(proc.returncode)
    out = adb.shell(inst.index, args.command.split(), discover=True)
    print(out, end="")


def cmd_console(args):
    console, _, _ = _session(args)
    res = console.run(args.command)
    print(res.stdout, end="")
    if res.stderr:
        print(res.stderr, file=sys.stderr, end="")
    sys.exit(res.returncode)


def cmd_props(args):
    console, adb, _ = _session(args)
    inst = _instance(args, console, adb)
    print(f"name        : {inst.name}")
    print(f"index       : {inst.index}")
    print(f"running     : {inst.info.running}")
    if inst.info.running:
        try:
            print(f"adb endpoint: {adb.discover(inst.index)}")
        except Exception as exc:  # noqa: BLE001
            print(f"adb endpoint: (unreachable: {exc})")
        print(f"booted      : {adb.is_boot_completed(inst.index)}")
    else:
        print("adb endpoint: (instance stopped)")


def cmd_repair(args):
    """Fix PowerUpFailed / VERR_VD_IMAGE_READ_ONLY conditions."""
    report = repair_mod.repair(verbose=True,
                               run_repair_tool=args.repair_tool,
                               kill=not args.no_kill)
    for w in report["warnings"]:
        print(f"  [!] {w}")
    print(f"  killed {len(report['killed'])} stale process(es), "
          f"cleared read-only on {len(report['fixed_vmdks'])} vmdk(s)")
    print("next: 'ldcli launch --index 0' (run from an elevated prompt "
          "if drivers need reinstalling)")


def cmd_window(args):
    """Resize the LDPlayer window so the whole screen is visible."""
    report = window_mod.fit_window(scale=args.scale, center=not args.top_left)
    if not report.get("found"):
        die(report.get("error", "LDPlayer window not found"))
    b = report["before"]
    a = report["after"]
    print(f"LDPlayer window resized: {b[2]-b[0]}x{b[3]-b[1]} -> "
          f"{a[2]-a[0]}x{a[3]-a[1]} (fits {report['screen'][2]}x"
          f"{report['screen'][3]} work area)")


def _profile_args(args) -> dict:
    resolution = None
    if args.resolution:
        r = args.resolution.replace("x", ",").replace("X", ",")
        parts = r.split(",")
        if len(parts) == 2:
            parts.append("240")
        resolution = ",".join(parts)
    return {
        "vendor": getattr(args, "vendor", None),
        "seed": getattr(args, "seed", None),
        "cpu": getattr(args, "cpu_num", None),
        "memory": getattr(args, "memory", None),
        "resolution": resolution,
        "root": getattr(args, "root", False),
        "fast": not getattr(args, "no_fast", False),
        "light": not getattr(args, "no_light", False),
        "audio_off": not getattr(args, "keep_audio", False),
    }


def cmd_configure(args):
    console, adb, _ = _session(args)
    name, index = _pick(args, console)
    if name is None and index is None:
        index = 0
    profile = device_mod.apply_profile(console, name=name, index=index,
                                       **_profile_args(args))
    print(f"[{name or index}] applied unique phone profile:")
    print(f"  {profile.summary()}")


def cmd_setup(args):
    """One-shot: unique phone profile + fast/light + launch + install APK."""
    console, adb, _ = _session(args)
    name, index = _pick(args, console)
    if name is None and index is None:
        index = 0
    if not args.apk:
        die("setup requires --apk <file>")

    apk = Path(args.apk)
    if not apk.is_file():
        die(f"APK not found: {apk}")

    profile = device_mod.apply_profile(console, name=name, index=index,
                                       **_profile_args(args))
    print(f"[{name or index}] phone profile: {profile.summary()}")

    inst = Instance(console, adb, name=name, index=index)
    inst.resolve()
    inst.launch(boot_wait=False)

    pkg = inst.install_apk_wait(apk, timeout=args.timeout,
                                adb_timeout=args.boot_timeout)
    print(f"[{name or index}] done: {pkg} installed, instance ready")


# -------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ldcli",
        description="LDPlayer 9 automation CLI "
                    "(open, install APKs, multi-instance, full backup).")
    p.add_argument("--version", action="version",
                   version=f"ldcli {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def inst_args(sp):
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--name", help="target instance by name")
        g.add_argument("--index", type=int, help="target instance by index")
        g.add_argument("-i", "--instance",
                       help="target instance by name or numeric index")

    # init / doctor
    s = sub.add_parser("init", help="detect & save LDPlayer/adb paths")
    s.add_argument("--default-instance", help="set default instance name")
    s.set_defaults(func=cmd_init)
    sub.add_parser("doctor", help="show toolchain status").set_defaults(func=cmd_doctor)

    # list
    sub.add_parser("list", help="list all instances").set_defaults(func=cmd_list)

    s = sub.add_parser(
        "repair",
        help="fix PowerUpFailed / read-only vmdk / stuck processes")
    s.add_argument("--repair-tool", action="store_true",
                   help="also open LDPlayer's built-in repairer")
    s.add_argument("--no-kill", action="store_true",
                   help="do not kill running emulator processes")
    s.set_defaults(func=cmd_repair)

    # lifecycle
    s = sub.add_parser("launch", help="launch (open) an instance")
    inst_args(s)
    s.add_argument("--no-boot-wait", action="store_true",
                   help="do not wait for Android boot")
    s.set_defaults(func=cmd_launch)

    s = sub.add_parser("quit", help="shut down an instance")
    inst_args(s)
    s.set_defaults(func=cmd_quit)

    s = sub.add_parser("add", help="create a new instance")
    s.add_argument("name", help="new instance name")
    s.add_argument("--source", help="clone from an existing instance")
    s.add_argument("--cpu-num", type=int)
    s.add_argument("--memory", type=int, help="MB of RAM")
    s.add_argument("--resolution", help="e.g. 1280x720")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("modify", help="change CPU/RAM/resolution")
    inst_args(s)
    s.add_argument("--cpu-num", type=int)
    s.add_argument("--memory", type=int)
    s.add_argument("--resolution")
    s.set_defaults(func=cmd_modify)

    s = sub.add_parser("remove", help="delete an instance")
    inst_args(s)
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("rename", help="rename an instance")
    inst_args(s)
    s.add_argument("title")
    s.set_defaults(func=cmd_rename)

    # phone profile / tuning
    def profile_opts(sp, default_fast=True, default_light=True):
        sp.add_argument("--vendor", choices=list(device_mod.VENDORS),
                        help="phone vendor (default: random per instance)")
        sp.add_argument("--seed", type=int,
                        help="reproducible profile seed")
        sp.add_argument("--cpu-num", type=int, choices=[1, 2, 3, 4],
                        help="CPU cores (default 4)")
        sp.add_argument("--memory", type=int, help="RAM in MB (default 768 "
                        "in light mode, else 1024)")
        sp.add_argument("--resolution",
                        help="resolution W,H[,dpi] e.g. 1280,720,240 or 960x540")
        sp.add_argument("--root", action="store_true", help="enable root")
        if default_fast:
            sp.add_argument("--no-fast", action="store_true",
                            help="keep default fps/audio/fastplay")
        if default_light:
            sp.add_argument("--no-light", action="store_true",
                            help="keep default resolution/rotation settings")
        sp.add_argument("--keep-audio", action="store_true",
                        help="do not mute emulator audio")

    s = sub.add_parser("configure",
                       help="set a unique phone profile per instance "
                            "(imei/imsi/sim/mac/model) + performance tuning")
    inst_args(s)
    profile_opts(s)
    s.set_defaults(func=cmd_configure)

    s = sub.add_parser(
        "setup",
        help="one-shot: unique phone profile + fast/light + launch + install apk")
    inst_args(s)
    profile_opts(s)
    s.add_argument("--apk", help="APK or .apkm/.xapk/.apks bundle to install")
    s.add_argument("--no-boot-wait", action="store_true",
                   help="deprecated (install now waits for adb, not boot)")
    s.add_argument("--boot-timeout", type=int, default=600,
                   help="seconds to wait for adb before install (default 600)")
    s.add_argument("--timeout", type=int, default=180,
                   help="seconds to wait for the package to register")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("window",
                       help="fit the LDPlayer window to the screen "
                            "(fixes cut-off/full-screen issues)")
    s.add_argument("--scale", type=float, default=1.0,
                   help="shrink the fitted window, e.g. 0.9 leaves a border")
    s.add_argument("--top-left", action="store_true",
                   help="place at top-left instead of centering")
    s.set_defaults(func=cmd_window)

    # apps
    s = sub.add_parser("install",
                       help="install an APK or .apkm/.xapk/.apks bundle")
    inst_args(s)
    s.add_argument("apk")
    s.add_argument("--wait", action="store_true",
                   help="wait for adb (up to --adb-timeout) then poll the "
                        "package until it registers")
    s.add_argument("--adb-timeout", type=int, default=600,
                   help="seconds to wait for the instance's adb (default 600)")
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("uninstall", help="uninstall an app")
    inst_args(s)
    s.add_argument("package")
    s.set_defaults(func=cmd_uninstall)

    s = sub.add_parser("run", help="launch an app inside the instance")
    inst_args(s)
    s.add_argument("package")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("stop", help="force-stop an app")
    inst_args(s)
    s.add_argument("package")
    s.set_defaults(func=cmd_stop)

    # backup / restore
    s = sub.add_parser("backup", help="full instance or per-app backup")
    s.add_argument("kind", choices=["full", "app"])
    inst_args(s)
    s.add_argument("--package", help="package name (app backup)")
    s.add_argument("--dest", default="backups", help="destination dir")
    s.add_argument("--tag", help="optional label for the snapshot")
    s.add_argument("--adb-mode", action="store_true",
                   help="force adb backup instead of LDPlayer backup")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("restore", help="restore a full or app backup")
    s.add_argument("kind", choices=["full", "app"])
    inst_args(s)
    s.add_argument("file")
    s.add_argument("--package", help="package name (app restore)")
    s.add_argument("--apk", help="APK to (re)install before app-data restore")
    s.set_defaults(func=cmd_restore)

    # roll
    s = sub.add_parser(
        "roll",
        help="backup old -> install apk -> clone new instance -> open it "
             "(old stays running)")
    s.add_argument("--apk", help="APK to install into the old instance")
    s.add_argument("--old-name", help="existing instance (default from config)")
    s.add_argument("--new-name", help="name for the cloned instance")
    s.add_argument("--backup-dir", default="backups")
    s.add_argument("--tag", default=None)
    s.add_argument("--quit-old", action="store_true",
                   help="quit the old instance after cloning (snapshot kept)")
    s.add_argument("--no-launch-old", action="store_true",
                   help="fail if old instance is not already running")
    s.add_argument("--no-boot-wait", action="store_true",
                   help="launch without waiting for Android boot")
    s.add_argument("--source", help="clone source (defaults to old-name)")
    s.add_argument("--cpu-num", type=int)
    s.add_argument("--memory", type=int)
    s.add_argument("--resolution")
    s.set_defaults(func=cmd_roll)

    # automation
    s = sub.add_parser("action", help="UI actions (tap/swipe/text/key/screencap)")
    inst_args(s)
    s.add_argument("key", choices=["tap", "tap-center", "swipe", "text",
                                   "home", "back", "enter", "key",
                                   "screencap", "focus"])
    s.add_argument("--x", type=int, default=0)
    s.add_argument("--y", type=int, default=0)
    s.add_argument("--x2", type=int, default=0)
    s.add_argument("--y2", type=int, default=0)
    s.add_argument("--duration", type=int, default=200)
    s.add_argument("--text", default="")
    s.add_argument("--keycode", type=int, default=0)
    s.add_argument("--path", default="screenshot.png")
    s.set_defaults(func=cmd_action)

    s = sub.add_parser("screencap", help="capture a screenshot")
    inst_args(s)
    s.add_argument("path", nargs="?", default="screenshot.png")
    s.set_defaults(func=cmd_screencap)

    # passthrough
    s = sub.add_parser("adb", help="run adb on the instance's serial")
    inst_args(s)
    s.add_argument("command", nargs="*", help="shell command, e.g. "
                                              "'shell pm list packages'")
    s.add_argument("--raw", nargs=argparse.REMAINDER,
                   help="pass args straight to adb (after --)")
    s.add_argument("--ldconsole", action="store_true",
                   help="use ldconsole adb instead")
    s.set_defaults(func=cmd_adb)

    s = sub.add_parser("console", help="run a raw ldconsole command")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_console)

    s = sub.add_parser("props", help="show resolved instance properties")
    inst_args(s)
    s.set_defaults(func=cmd_props)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except LdConsoleError as exc:
        die(str(exc))
    except InstanceError as exc:
        die(str(exc))
    except backup_mod.BackupError as exc:
        die(str(exc))
    except workflow_mod.WorkflowError as exc:
        die(str(exc))
    except Exception as exc:  # noqa: BLE001
        die(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
