# ldcli — LDPlayer 9 automation CLI

Open emulator instances, install APKs, spin up **new** instances cloned from an
old one, and take **full backups** — all from the command line. Python 3.9+
standard-library only, no pip installs.

Verified against **LDPlayer v9.5.31.0** (LDPlayer 9).

## Setup

```bat
python ldcli.py init        :: auto-detect LDPlayer 9 + adb, write config
python ldcli.py doctor      :: verify the toolchain
python ldcli.py init --default-instance <name>   :: set the default instance
```

Auto-detection checks the registry, `LDPLAYER_HOME`, and common folders
(`C:\LDPlayer\LDPlayer9`, `D:\LDPlayer\LDPlayer9`, ...). If it can't find the
install, set `LDPLAYER_HOME` (or `ANDROID_HOME` for adb) and run `init` again.
Config is saved to `%APPDATA%\ldplayer-cli\config.json`.

Optionally add this folder to your `PATH` — then just run `ldcli ...` via
`ldcli.bat`.

## Quick start

```bat
:: list your emulator instances
ldcli list

:: open an instance (name or index) and wait for Android to boot
ldcli launch --index 0

:: install an APK (plain .apk, or a .apkm/.xapk/.apks bundle)
ldcli install --index 0 C:\apps\myapp.apk --wait
ldcli setup --index 0 --apk C:\apps\app.apkm          :: profile + boot + install, one-shot

:: give an instance a unique phone identity (IMEI/SIM/MAC/model) + tune it
ldcli configure --index 0 --vendor samsung

:: THE FLAGSHIP: full-backup old -> install apk -> clone into NEW instance -> open it
ldcli roll --old-name LDPlayer --new-name fresh --apk C:\apps\myapp.apk
```

`roll` is the "open a new LDPlayer and keep the older one" command. It:

1. takes a **full snapshot** of the old instance to `backups\`;
2. launches the old instance (if stopped) and installs your APK into it;
3. clones the old instance into a brand-new instance;
4. launches the new instance; the old one **keeps running** unless you add
   `--quit-old` (its snapshot is kept either way).

## Command reference

### Instances
| command | description |
|---|---|
| `list` | list all instances (index / status / name) |
| `repair [--repair-tool] [--no-kill]` | fix PowerUpFailed / read-only vmdk / stuck processes |
| `launch [--name X \| --index N \| -i Y] [--no-boot-wait]` | open an instance |
| `quit [target]` | shut an instance down |
| `add NAME [--source X] [--cpu-num N] [--memory MB] [--resolution WxH]` | create a new instance (blank, or cloned) |
| `modify [target] [--cpu-num] [--memory] [--resolution]` | change resources |
| `configure [target] [--vendor V] [--seed N] [--cpu-num] [--memory] [--resolution] [--root] [--no-fast] [--no-light] [--keep-audio]` | apply a **unique phone profile** (IMEI/IMSI/SIM/MAC/Android ID/number/model) + fast/light tuning |
| `setup [target] --apk FILE [same options as configure] [--no-boot-wait] [--boot-timeout S] [--timeout S]` | one-shot: configure → launch → install → verify package |
| `remove [target]` | delete an instance |
| `rename [target] TITLE` | rename an instance |
| `props [target]` | show resolved name / index / running / adb endpoint |

### Apps
| command | description |
|---|---|
| `install [target] APK [--wait]` | install an APK or a `.apkm`/`.xapk`/`.apks` bundle (`--wait` polls until the package registers) |
| `uninstall [target] PACKAGE` | remove an app |
| `run [target] PACKAGE` / `stop [target] PACKAGE` | start / force-stop an app |

### Backup & restore
| command | description |
|---|---|
| `backup full [target] [--dest DIR] [--tag X]` | **full instance snapshot** → `*.ldbk` (7z) |
| `backup app [target] --package PKG [--dest DIR] [--adb-mode]` | per-app backup (LDPlayer `backupapp`, adb `.ab` fallback) |
| `restore full [target] FILE` | restore a snapshot **into an existing instance** |
| `restore app [target] --package PKG FILE [--apk APK]` | restore an app backup (`restoreapp` / adb restore) |

> Full backups use LDPlayer's native `backup`/`restore` and are safe to run
> while the emulator is running. Snapshots land in `backups\` (gitignored).

### UI automation
| command | description |
|---|---|
| `action [target] tap --x 500 --y 500` | tap a coordinate |
| `action [target] swipe --x 100 --y 100 --x2 300 --y2 300 --duration 300` | swipe |
| `action [target] text --text hello` | type text |
| `action [target] home\|back\|enter\|key --keycode 66` | system keys |
| `action [target] screencap --path shot.png` | screenshot (via adb) |
| `action [target] focus` | dump the focused window/activity |
| `screencap [target] out.png` | screenshot shortcut |

### Passthrough
```bat
ldcli adb --index 0 'shell pm list packages'     :: shell on that instance
ldcli adb --index 0 --raw shell wm size           :: raw adb passthrough
ldcli console list2                               :: raw ldconsole output
```

Every target accepts `--name NAME`, `--index N`, or `-i NAME-or-index`.

## How it works

- **Console control** — `ldconsole.exe` (LDPlayer 9 install folder). The tool
  targets instances by `--index` because that is the reliable handle in v9.5.
- **Instance list** — parsed from `ldconsole list2`.
- **Full backup** — `ldconsole backup --file X.ldbk` snapshots the whole
  instance; `restore --file X.ldbk` puts it back (`.ldbk` = 7z archive).
- **App backup** — `ldconsole backupapp` with an `adb backup` `.ab` fallback.
- **adb targeting** — the live serial is auto-discovered (probing
  `127.0.0.1:5555 + index*2` and neighbors), so both `emulator-5554`-style and
  `127.0.0.1:PORT` connections work.

## Automation recipes

Install an APK, launch it, tap through it, screenshot:

```bat
ldcli roll --old-name LDPlayer --new-name test-run --apk myapp.apk
ldcli run --index 0 com.example.myapp
ldcli action --index 0 tap --x 300 --y 400
ldcli action --index 0 screencap --path after-tap.png
```

Snapshot an instance every evening before rollover:

```bat
ldcli backup full --index 0 --dest D:\ldbackups --tag nightly
```

One-command takeover with an old backup kept:

```bat
ldcli roll --old-name LDPlayer --new-name worker1 --apk job.apk --quit-old
```

## Unique phone profiles & performance tuning

Every instance gets its own **distinct, realistic device identity**:

```bat
ldcli configure --index 0 --vendor samsung      :: fixed brand, random IMEI/IMSI/...
ldcli configure --index 0 --seed 12345          :: reproducible profile (same values each run)
ldcli configure --index 0 --no-light            :: keep the default resolution/rotation
ldcli configure --index 0 --memory 1024         :: keep resolution, force 1 GB RAM
```

Each profile generates unique, checksum-valid **IMEI**, **IMSI**, **SIM ICCID**,
**Android ID**, **MAC address**, phone number, and a **manufacturer/model** pair
(chosen from samsung/google/xiaomi/oneplus/honor/oppo/vivo/nokia). Profiles are
written to the instance's config via `ldconsole modify` — no rooting needed.

By default `configure` also applies "fast & lightweight" settings (unlocked on
the same command):

- `modify --cpu 4 --memory 768` and a light `960,540,240` resolution;
- `globalsetting --fps 30 --fastplay 1 --cleanmode 1 --audio 0` (global, applies
  to every instance) — keep audio with `--keep-audio`, skip tuning with
  `--no-fast`/`--no-light`.

> Note: 768 MB is the absolute floor for Android on LDPlayer; if an instance
> crash-loops, raise it with `--memory 1024`.

## Installing APK bundles

APKMirror-style `.apkm`, plus `.xapk`/`.apks`, are zip bundles containing a
`base.apk` + split configs. `ldcli install` and `ldcli setup` detect these,
extract them to a temp dir, and install all parts in one shot with
`adb install-multiple`:

```bat
ldcli setup --index 0 --apk "com.facebook.katana_..._apkmirror.com.apkm"
```

The package name is read from the bundle's `info.json`, or from the APK with
`aapt.exe dump badging` (ships with LDPlayer) when present; filename guessing is
only a last resort.

## Notes

- The bundled `adb.exe` (from the LDPlayer folder) is preferred; `ANDROID_HOME`
  is used if set and valid.
- `ldconsole copy` reports a non-zero exit code on success in v9.5.31.0; the
  tool verifies instance creation by querying `list2` instead of trusting the
  exit code.

## Troubleshooting

### "Power-up of virtual machine failed (PowerUpFailed)"

The most common cause is a **read-only vmdk** left behind by a clone:

```
Failed to open image '...\vms\leidian0\data.vmdk' for writing due to wrong permissions
Power up failed (vrc=VERR_VD_IMAGE_READ_ONLY)
```

Fix it:

```bat
ldcli repair            :: kills stale emulator processes, clears read-only
                          :: flags on every vmdk, restarts the adb server
ldcli launch --index 0
```

`repair` is safe to run with instances stopped. If the VirtualBox drivers
themselves need reinstalling, run `ldcli repair --repair-tool` (opens LDPlayer's
built-in repairer) from an **elevated** prompt, or click **Fix** in LDPlayer's
error dialog.

`ldcli roll` avoids this entirely: it shuts the source instance down **before**
cloning, so the disk is never locked mid-clone.

### Emulator starts but reboots in a loop / gets killed

This is a resource problem, not a config one:

- LDPlayer 9 instances need ~1 GB RAM **each** and a hardware-virtualized CPU.
- If the host has no VT-x/AMD-V (e.g. this machine is itself a VM), LDPlayer
  falls back to software emulation (NEM) which is very slow and unstable.
- Reduce memory pressure: quit other instances/apps, or lower the instance RAM
  with `ldcli modify --index N --memory 1024`. Minimum sensible is 1024 MB;
  anything lower than ~768 MB makes Android crash-loop.

Check your host with:

```bat
ldcli props --index 0      :: shows running / booted state
(Get-CimInstance Win32_Processor).VirtualizationFirmwareEnabled
```

