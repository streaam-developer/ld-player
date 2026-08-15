# ldcli — LDPlayer 9 automation CLI

Open emulator instances, install APKs, spin up **new** instances cloned from an
old one, and take **full backups** — all from the command line. Python 3.9+
standard-library only, no pip installs.

## Setup

```bat
python ldcli.py init        :: auto-detect LDPlayer 9 + adb, write config
python ldcli.py doctor      :: verify the toolchain
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
ldcli launch --name leidian0

:: install an APK
ldcli install --name leidian0 C:\apps\myapp.apk --wait

:: THE FLAGSHIP: full-backup old -> install apk -> clone into NEW instance -> open it
ldcli roll --old-name leidian0 --new-name leidian0-fresh --apk C:\apps\myapp.apk
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
| `launch [--name NAME \| --index N \| -i X] [--no-boot-wait]` | open an instance |
| `quit [target]` | shut an instance down |
| `add NAME [--source X] [--cpu-num N] [--memory MB] [--resolution WxH]` | create a new instance (blank, or cloned) |
| `modify [target] [--cpu-num] [--memory] [--resolution]` | change resources |
| `props [target]` | show resolved name / index / adb port / boot state |

### Apps
| command | description |
|---|---|
| `install [target] APK [--wait]` | install an APK (`--wait` polls until the package registers) |
| `uninstall [target] PACKAGE` | remove an app |
| `run [target] PACKAGE` / `stop [target] PACKAGE` | start / force-stop an app |

### Backup & restore
| command | description |
|---|---|
| `backup full [target] [--dest DIR] [--tag X]` | full instance snapshot (export → `*.7z`) |
| `backup app [target] --package PKG [--dest DIR] [--adb-mode]` | per-app backup (LDPlayer native, adb `.ab` fallback) |
| `restore full FILE [--name NEW]` | import a snapshot (optionally under a new name) |
| `restore app [target] FILE [--apk APK]` | reinstall apk, then restore app data |

Snapshots are written to `backups\` by default (gitignored).

### UI automation
| command | description |
|---|---|
| `action [target] tap --x 500 --y 500` | tap a coordinate |
| `action [target] swipe --x1.. --y1.. --x2.. --y2.. --duration` | swipe |
| `action [target] text --text hello` | type text |
| `action [target] home\|back\|enter\|key --keycode 66` | system keys |
| `action [target] screencap --path shot.png` | screenshot |
| `action [target] focus` | dump the focused window/activity |
| `screencap [target] out.png` | screenshot shortcut |

### Passthrough
```bat
ldcli adb --name leidian0 'shell pm list packages'   :: shell on that instance
ldcli adb --name leidian0 --raw shell wm size          :: raw adb passthrough
ldcli console installapk --filename my.apk --name leidian0   :: raw ldconsole
```

Every target accepts `--name NAME`, `--index N`, or `-i NAME-or-index`.

## How it works

- **Console control** — `ldconsole.exe` (from the LDPlayer 9 install folder).
- **adb targeting** — each instance maps to `127.0.0.1:5555 + index*2`.
- **Full backup** — `ldconsole export/import` snapshots the entire instance
  (safe to run while the emulator is running).
- **App backup** — LDPlayer's `backup` command with an automatic fallback to
  `adb backup`/`restore` (`.ab`).

## Automation recipes

Install an APK, launch it, tap through it, screenshot:

```bat
ldcli roll --old-name leidian0 --new-name test-run --apk myapp.apk
ldcli run --name test-run com.example.myapp
ldcli action --name test-run tap --x 300 --y 400
ldcli action --name test-run screencap --path after-tap.png
```

Snapshot a whole instance every evening before rollover:

```bat
ldcli backup full --name leidian0 --dest D:\ldbackups --tag nightly
```
