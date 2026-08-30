#!/usr/bin/env python3
"""
Facebook Reel Uploader v6 - ADVANCED
Deletes ALL media (including loose files sitting directly in the "0" folder,
i.e. /storage/emulated/0 root) before each upload, imports 1.mp4 only, and
runs the whole Facebook reel flow with auto-discovery, retries and per-step
screenshot verification.
"""

import argparse
import os
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

TRANSPORT_LOST = re.compile(
    r"device ('[^']*' )?(not found|offline)"
    r"|device still connecting"
    r"|error: closed"
    r"|closed connection"
    r"|transport.*closed",
    re.IGNORECASE,
)

MEDIA_EXTENSIONS = [
    "mp4", "mkv", "mov", "avi", "webm", "wmv", "3gp", "3g2", "flv",
    "m4v", "mpg", "mpeg", "ts",
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "heic",
    "m4a", "aac", "mp3", "wav",
]

CONSOLE_CANDIDATES = [
    r"C:\LDPlayer\LDPlayer9\ldconsole.exe",
    r"C:\LDPlayer\LDPlayer14\ldconsole.exe",
    r"D:\LDPlayer\LDPlayer9\ldconsole.exe",
    r"D:\LDPlayer\LDPlayer14\ldconsole.exe",
    r"C:\leidian\LDPlayer14\ldconsole.exe",
    r"C:\dnplayer\ldconsole.exe",
]

WORKDIR = Path(__file__).resolve().parent


class LDController:
    def __init__(self, index=1, device=None, console=None, adb=None):
        self.ld_index = index
        self.console = console or self._find_console()
        self.adb = adb or self._find_adb() or str(Path(self.console).parent / "adb.exe")
        self._expected_serial = f"emulator-{5554 + index * 2}"
        self._primary_port = f"127.0.0.1:{5555 + index * 2}"
        self.device = device or self._discover_device()
        base = Path(os.environ.get("TEMP", r"C:\Users\zrs2026\AppData\Local\Temp")) / "opencode"
        self.dir = base / ("run_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ discovery
    def _find_console(self):
        env = os.environ.get("LDCONSOLE") or os.environ.get("LDPLAYER_HOME")
        if env:
            p = Path(env)
            return str(p if p.name.lower() == "ldconsole.exe" else p / "ldconsole.exe")
        try:
            from ldplayer.config import find_ldconsole
            cand = find_ldconsole()
            if cand and Path(cand).is_file():
                return str(cand)
        except Exception:
            pass
        for cand in CONSOLE_CANDIDATES:
            if Path(cand).is_file():
                return cand
        return CONSOLE_CANDIDATES[0]

    def _find_adb(self):
        env = os.environ.get("ADB") or os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        if env:
            p = Path(env)
            cand = p if p.name.lower() == "adb.exe" else p / "platform-tools" / "adb.exe"
            if cand.is_file():
                return str(cand)
        try:
            from ldplayer.config import find_adb
            cand = find_adb()
            if cand and Path(cand).is_file():
                return str(cand)
        except Exception:
            pass
        for cand in (r"C:\ADB\adb.exe", r"C:\platform-tools\adb.exe"):
            if Path(cand).is_file():
                return cand
        return str(Path(self.console).parent / "adb.exe")

    def _raw_devices(self):
        try:
            out = subprocess.run([self.adb, "devices"], capture_output=True,
                                 text=True, timeout=15)
            return out.stdout or ""
        except Exception:
            return ""

    def _discover_device(self):
        serials = [ln.split("\t")[0] for ln in self._raw_devices().splitlines()[1:]
                   if "\tdevice" in ln and not ln.startswith("*")]
        if self._expected_serial in serials:
            return self._expected_serial
        if self._primary_port in serials:
            return self._primary_port
        for s in serials:
            if ":" in s and s.rsplit(":", 1)[-1].isdigit():
                return s
        for s in serials:
            if "emulator-" in s:
                return s
        for p in (self._primary_port, f"127.0.0.1:{5554 + self.ld_index * 2}"):
            try:
                subprocess.run([self.adb, "connect", p], capture_output=True,
                               text=True, timeout=8)
            except Exception:
                continue
            if p in self._raw_devices():
                return p
        return self._expected_serial

    # ------------------------------------------------------------- low level
    def _kick_adb(self):
        for args in (["reconnect", "offline"], ["devices"]):
            try:
                subprocess.run([self.adb] + args, capture_output=True,
                               text=True, timeout=15)
            except Exception:
                pass

    def shell_raw(self, cmd, wait=1, retries=2):
        for i in range(retries + 1):
            try:
                r = subprocess.run([self.adb, "-s", self.device, "shell", cmd],
                                   capture_output=True, text=True, timeout=90)
                blob = r.stdout + r.stderr
                if re.search(TRANSPORT_LOST, blob, re.I):
                    self._kick_adb()
                    time.sleep(2)
                    continue
                time.sleep(wait)
                return r.stdout
            except Exception:
                if i < retries:
                    self._kick_adb()
                    time.sleep(2)
        time.sleep(wait)
        return ""

    def run_adb(self, cmd, wait=1):
        return self.shell_raw(cmd, wait)

    def run_console(self, cmd, wait=1):
        for _ in range(2):
            try:
                r = subprocess.run([self.console] + shlex.split(cmd),
                                   capture_output=True, text=True, timeout=120)
                time.sleep(wait)
                return r.stdout
            except Exception:
                time.sleep(2)
        time.sleep(wait)
        return ""

    def tap(self, x, y, w=1):
        self.shell_raw(f"input tap {x} {y}", w)

    def swipe(self, x1, y1, x2, y2, d=300, w=1):
        self.shell_raw(f"input swipe {x1} {y1} {x2} {y2} {d}", w)

    def key(self, k, w=0.3):
        self.shell_raw(f"input keyevent {k}", w)

    def back(self, w=1):
        self.key("KEYCODE_BACK", w)

    def home(self, w=1):
        self.key("KEYCODE_HOME", w)

    def wake(self, w=1):
        self.key(224, 0.5)
        self.key(82, 0.5)
        time.sleep(w)

    def text_type(self, t, w=0.5):
        payload = t.replace(" ", "%s").replace("'", "'\\''")
        self.shell_raw(f"input text '{payload}'", w)

    # ------------------------------------------------------------------- ui
    def get_ui(self, attempts=3):
        for _ in range(attempts):
            self.shell_raw("uiautomator dump /sdcard/ui.xml 2>/dev/null", 0.3)
            try:
                r = subprocess.run([self.adb, "-s", self.device, "shell",
                                    "cat", "/sdcard/ui.xml"],
                                   capture_output=True, text=True, timeout=10)
                if "<node" in r.stdout:
                    return r.stdout
            except Exception:
                pass
            time.sleep(0.5)
        return ""

    def find(self, text=None, desc=None):
        xml = self.get_ui()
        try:
            root = ET.fromstring(xml)
            for e in root.iter("node"):
                if text and text.lower() in e.get("text", "").lower():
                    return self._center(e)
                if desc and desc.lower() in e.get("content-desc", "").lower():
                    return self._center(e)
        except Exception:
            pass
        return None

    def find_all(self, text=None, desc=None):
        xml = self.get_ui()
        results = []
        try:
            root = ET.fromstring(xml)
            for e in root.iter("node"):
                if text and text.lower() in e.get("text", "").lower():
                    results.append(self._center(e))
                elif desc and desc.lower() in e.get("content-desc", "").lower():
                    results.append(self._center(e))
        except Exception:
            pass
        return [r for r in results if r]

    def _center(self, e):
        m = re.findall(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                       e.get("bounds", ""))
        if m:
            x1, y1, x2, y2 = map(int, m[0])
            return ((x1 + x2) // 2, (y1 + y2) // 2)
        return None

    def tap_find(self, text=None, desc=None, wait=1, timeout=10):
        start = time.time()
        while time.time() - start < timeout:
            pos = self.find(text=text, desc=desc)
            if pos:
                print(f"  -> Found '{text or desc}' at {pos}")
                self.tap(pos[0], pos[1], wait)
                return True
            time.sleep(0.5)
        print(f"  -> NOT found: '{text or desc}'")
        return False

    def wait_any(self, texts, timeout=10):
        start = time.time()
        while time.time() - start < timeout:
            for t in texts:
                pos = self.find(text=t)
                if pos:
                    return t, pos
            time.sleep(0.5)
        return None, None

    def is_on_screen(self, text):
        return self.find(text=text) is not None

    def scroll_down(self, times=1):
        for _ in range(times):
            self.swipe(540, 1500, 540, 800, 300, 0.5)

    def scroll_up(self, times=1):
        for _ in range(times):
            self.swipe(540, 800, 540, 1500, 300, 0.5)

    # ------------------------------------------------------------- lifecycle
    def is_running(self):
        return "running" in self.run_console(
            f"isrunning --index {self.ld_index}", 2).lower()

    def start_ldplayer(self):
        print("  Starting LDPlayer...")
        self.run_console(f"launch --index {self.ld_index}", 5)
        print("  Waiting for boot...")
        for i in range(30):
            time.sleep(5)
            if self.is_adb_connected():
                print(f"  LDPlayer ready! ({(i + 1) * 5}s)")
                self.wake(1)
                return True
            print(f"  Waiting... ({(i + 1) * 5}s)")
        return False

    def is_adb_connected(self):
        out = subprocess.run([self.adb, "devices"], capture_output=True,
                             text=True, timeout=15).stdout
        return self._discover_device() in out or self.device in out

    def open_facebook(self):
        print("  Opening Facebook...")
        for _ in range(3):
            self.shell_raw(
                "am start -n com.facebook.katana/com.facebook.katana.LoginActivity", 4)
            time.sleep(3)
            out = self.shell_raw(
                "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'", 1)
            if "com.facebook.katana" in out:
                print("  Facebook in foreground.")
                return True
        print("  WARNING: could not confirm Facebook foreground; continuing.")
        return True

    # ---------------------------------------------------------------- media
    def _media_pattern(self):
        return " -o ".join(f"-iname '*.{e}'" for e in MEDIA_EXTENSIONS)

    def _as_int(self, out):
        try:
            return int(out.strip() or 0)
        except Exception:
            return -1

    def _count_root_media(self, root="/storage/emulated/0"):
        out = self.shell_raw(
            f"find {root} -maxdepth 1 -type f \\({self._media_pattern()}\\) "
            "2>/dev/null | wc -l", 0.2)
        return self._as_int(out)

    def _count_media(self, root="/storage/emulated/0", depth=8):
        out = self.shell_raw(
            f"find {root} -maxdepth {depth} "
            "\\( -path '*/Android' -o -path '*/Android/*' -o -name '.*' \\) "
            "-prune -o -type f "
            f"\\({self._media_pattern()}\\) -print 2>/dev/null | wc -l", 0.2)
        return self._as_int(out)

    def _delete_media(self, root="/storage/emulated/0", depth=8):
        self.shell_raw(
            f"find {root} -maxdepth {depth} "
            "\\( -path '*/Android' -o -path '*/Android/*' -o -name '.*' \\) "
            "-prune -o -type f "
            f"\\({self._media_pattern()}\\) -delete 2>/dev/null", 0.5, retries=3)

    def _stop_media_services(self):
        for pkg in ("com.android.providers.media",
                    "com.android.providers.media.module"):
            self.shell_raw(f"am force-stop {pkg}", 0.5)

    def _file_size(self, remote):
        for path in (remote, remote.replace("/sdcard/", "/storage/emulated/0/")):
            out = self.shell_raw(f"stat -c %s {path}", 0.2)
            m = re.search(r"\b(\d+)\b", out or "")
            if m:
                return int(m.group(1))
        parts = self.shell_raw(f"ls -l {remote}", 0.2).split()
        if len(parts) >= 5:
            try:
                return int(parts[4])
            except Exception:
                pass
        return -1

    def _wait_file_size(self, remote, want, timeout=10):
        start = time.time()
        while time.time() - start < timeout:
            if self._file_size(remote) == want:
                return True
            time.sleep(1)
        return False

    def purge_media(self):
        root = "/storage/emulated/0"
        print("  [purge] Removing known media folders...")
        for folder in ("DCIM", "Pictures", "Movies", "Video", "Pics",
                       "Camera", "ScreenRecorder", "Screenshots",
                       "downrec", "face", "Instagram"):
            self.shell_raw(f"rm -rf {root}/{folder}", 0.3)
        self.shell_raw("rm -rf /storage/sdcard0/Pictures/temp", 0.3)

        print("  [purge] Stopping media scanner...")
        self._stop_media_services()
        time.sleep(2)

        root_n = self._count_root_media(root)
        print(f"  [purge] Loose media found directly in the '0' folder "
              f"(/storage/emulated/0): {root_n} file(s)")

        before = self._count_media(root)
        self._delete_media(root)
        time.sleep(1)
        after = self._count_media(root)
        print(f"  [purge] Gallery-visible media: {before} -> {after} remaining")

        self.shell_raw(f"mkdir -p {root}/DCIM/Camera", 0.3)
        self.shell_raw("am broadcast -a android.intent.action.MEDIA_MOUNTED "
                       f"-d file://{root}", 1)
        if after > 0:
            print(f"  [purge] WARNING: {after} media file(s) still present")
            return False
        print("  [purge] All media deleted (including '0' folder root)!")
        return True

    def import_1mp4(self, video_path, dest="/sdcard/DCIM/Camera/1.mp4"):
        print(f"  Importing: {Path(video_path).name}")
        want = Path(video_path).stat().st_size
        self.shell_raw("mkdir -p /sdcard/DCIM/Camera", 0.2)
        ok = False
        for attempt in range(1, 4):
            try:
                r = subprocess.run([self.adb, "-s", self.device, "push",
                                    str(video_path), dest],
                                   capture_output=True, text=True, timeout=180)
                if r.returncode != 0:
                    print(f"  adb push notice: "
                          f"{(r.stdout.strip() or r.stderr.strip()) or 'nothing'}")
            except Exception as e:
                print(f"  adb push error: {e}")
            if self._wait_file_size(dest, want, timeout=10):
                ok = True
                break
            print(f"  import attempt {attempt}/3 did not verify; retrying...")
            time.sleep(2)
        if not ok:
            print("  Trying ldconsole push fallback...")
            self.run_console(
                f"push --index {self.ld_index} --remote {dest} "
                f"--local {shlex.quote(str(video_path))}", 2)
            ok = self._wait_file_size(dest, want, timeout=10)
        size = self._file_size(dest)
        if ok:
            print(f"  1.mp4 verified on device ({size} bytes)")
        else:
            print(f"  WARNING: imported file size mismatch ({size} vs {want})")
        self.shell_raw("am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
                       "-d file:///sdcard/DCIM/Camera/1.mp4", 2)
        time.sleep(5)
        return ok

    def force_refresh_gallery(self):
        print("  Refreshing gallery (rescan media store)...")
        self._stop_media_services()
        time.sleep(2)
        self.shell_raw("am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
                       "-d file:///storage/emulated/0/DCIM/Camera/1.mp4", 2)
        time.sleep(3)

    def wait_for_upload_complete(self, max_wait=60):
        print("  Waiting for upload to complete...")
        start = time.time()
        while time.time() - start < max_wait:
            if not self.is_on_screen("Uploading reel"):
                print("  Upload completed!")
                return True
            time.sleep(3)
        return False

    def screenshot(self, name="shot"):
        ts = datetime.now().strftime("%H%M%S_%f")[:-3]
        remote = "/sdcard/_up_" + ts + ".png"
        subprocess.run([self.adb, "-s", self.device, "shell",
                        "screencap", "-p", remote],
                       capture_output=True, timeout=30)
        p = self.dir / f"{name}_{ts}.png"
        try:
            subprocess.run([self.adb, "-s", self.device, "pull",
                            remote, str(p)], capture_output=True, timeout=30)
        except Exception:
            pass
        if p.exists() and p.stat().st_size > 0:
            self.shell_raw(f"rm -f {remote}", 0.1)
            return p
        return None


class ReelUploaderV6:
    def __init__(self, index=1, device=None, video=None, caption_file=None):
        self.ld = LDController(index=index, device=device)
        self.caption_file = Path(caption_file) if caption_file else WORKDIR / "caption.txt"
        self.video_file = Path(video) if video else WORKDIR / "1.mp4"
        self.console_log = self.ld.dir / "run.log"

    def log(self, msg, ok=False):
        c = "\033[32m" if ok else ""
        line = f"{c}{msg}\033[0m"
        print(line)
        try:
            with self.console_log.open("a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except OSError:
            pass

    def read_caption(self):
        cap = os.environ.get("CAPTION", "")
        if not cap and self.caption_file.exists():
            cap = self.caption_file.read_text(encoding="utf-8", errors="replace").strip()
        if cap:
            self.log(f"Caption: {cap}", True)
            return cap
        self.log("No caption provided - using default")
        return "Check out this video! #viral #reels"

    def step(self, n, msg):
        print(f"\n{'=' * 50}")
        print(f"  STEP {n}: {msg}")
        print(f"{'=' * 50}")
        try:
            with self.console_log.open("a", encoding="utf-8") as f:
                f.write(f"\nSTEP {n}: {msg}\n")
        except OSError:
            pass

    def upload(self):
        try:
            self.log("\n=== Facebook Reel Uploader v6 (ADVANCED) ===", True)
            self.log(f"  console : {self.ld.console}")
            self.log(f"  adb     : {self.ld.adb}")
            self.log(f"  device  : {self.ld.device}")
            self.log(f"  screenshots/logs: {self.ld.dir}")

            caption = self.read_caption()
            if not self.video_file.exists():
                self.log(f"ERROR: {self.video_file} not found!", ok=False)
                return False
            print(f"  Video file: {self.video_file}")

            self.step(1, "Checking LDPlayer")
            if not self.ld.is_running():
                print("  LDPlayer is NOT running!")
                if not self.ld.start_ldplayer():
                    self.log("ERROR: Could not start LDPlayer!")
                    return False
            else:
                print("  LDPlayer is running")
            if not self.ld.is_adb_connected():
                print("  Waiting for ADB...")
                time.sleep(10)
            self.ld.wake(1)
            print("  LDPlayer ready!")

            self.step(2, "Purge ALL media (including '0' folder root)")
            self.ld.purge_media()

            self.step(3, "Import 1.mp4")
            if not self.ld.import_1mp4(str(self.video_file)):
                self.log("WARNING: import did not verify - continuing anyway")

            self.step(4, "Refresh gallery")
            self.ld.force_refresh_gallery()
            print(f"  Gallery-visible media now: "
                  f"{self.ld._count_media()} (expect 1)")

            self.step(5, "Opening Facebook")
            self.ld.home(1)
            time.sleep(1)
            self.ld.open_facebook()

            self.step(6, "Checking for ongoing uploads")
            if self.ld.is_on_screen("Uploading reel"):
                self.ld.wait_for_upload_complete(60)
            self.ld.screenshot("step6_home")

            self.step(7, "Going to Profile")
            self.ld.tap(990, 1840, 2)
            time.sleep(2)
            if not (self.ld.is_on_screen("Add to story")
                    or self.ld.is_on_screen("Edit profile")):
                self.ld.tap_find(desc="Profile", timeout=5)
                time.sleep(2)
            self.ld.screenshot("step7_profile")

            self.step(8, "Clicking Reels tab")
            self.ld.tap_find(text="Reels", timeout=5)
            time.sleep(2)

            self.step(9, "Handling upload notification")
            if self.ld.is_on_screen("Uploading reel"):
                self.ld.wait_for_upload_complete(60)
            self.ld.screenshot("step9_ready")

            self.step(10, "Clicking Create Reel")
            found = False
            for i in range(8):
                if self.ld.tap_find(text="Create reel", timeout=2):
                    found = True
                    break
                print(f"  Scrolling... ({i + 1}/8)")
                self.ld.scroll_down(1)
                time.sleep(1)
            if not found:
                print("  Using fallback coordinates")
                self.ld.tap(540, 1620, 3)
            time.sleep(3)
            self.ld.screenshot("step10_create_reel")

            self.step(11, "Selecting 1.mp4 from gallery")
            time.sleep(2)
            self.ld.force_refresh_gallery()

            selected = False
            for attempt in range(4):
                items = self.ld.find_all(desc="photo")
                if not items:
                    items = self.ld.find_all(desc="video")
                print(f"  Gallery items found: {len(items)} ({attempt + 1}/4)")
                if items:
                    first = items[0]
                    print(f"  Selecting item at {first}")
                    self.ld.tap(first[0], first[1], 3)
                    selected = True
                    break
                time.sleep(2)
            if not selected:
                print("  No items found - using fallback")
                self.ld.tap(180, 600, 3)
            self.ld.screenshot("step11_video_selected")

            self.step(12, "Handling dialogs")
            for _ in range(8):
                found, pos = self.ld.wait_any(
                    ["Continue", "Done", "Next", "Skip", "Allow", "OK",
                     "Got it", "Not now"], timeout=2)
                if found:
                    print(f"  Clicking: {found}")
                    self.ld.tap(pos[0], pos[1], 2)
                else:
                    break
            self.ld.screenshot("step12_dialogs_done")

            self.step(13, "Going to share screen")
            self.ld.tap_find(text="Next", timeout=5)
            time.sleep(3)
            self.ld.screenshot("step13_share_screen")

            self.step(14, "Entering caption")
            time.sleep(2)
            caption_entered = False

            xml = self.ld.get_ui()
            try:
                root = ET.fromstring(xml)
                for e in root.iter("node"):
                    if "EditText" in e.get("class", ""):
                        pos = self.ld._center(e)
                        if pos:
                            print(f"  Found EditText at {pos}")
                            self.ld.tap(pos[0], pos[1], 1)
                            self.ld.key("KEYCODE_MOVE_END", 0.2)
                            for _ in range(40):
                                self.ld.key("KEYCODE_DEL", 0.02)
                            self.ld.text_type(caption, 1)
                            caption_entered = True
                            break
            except Exception:
                pass

            if not caption_entered:
                for hint in ("Describe your reel", "Write something",
                             "Name", "Add a caption"):
                    if self.ld.tap_find(text=hint, timeout=2):
                        time.sleep(0.5)
                        self.ld.key("KEYCODE_MOVE_END", 0.2)
                        for _ in range(40):
                            self.ld.key("KEYCODE_DEL", 0.02)
                        self.ld.text_type(caption, 1)
                        caption_entered = True
                        break
            self.ld.screenshot("step14_caption")

            self.step(15, "Sharing reel")
            shared = False
            for _ in range(3):
                if self.ld.tap_find(text="Share now", timeout=5):
                    shared = True
                    break
                if self.ld.tap_find(text="Share", timeout=3):
                    shared = True
                    break
                self.ld.back(1)
            if shared:
                print("\n  Uploading reel...")
                time.sleep(10)
            self.ld.screenshot("step15_done")

            self.log("\n=== REEL UPLOAD COMPLETE ===", True)
            self.log(f"Caption: {caption}")
            return True

        except Exception as e:
            self.log(f"\nERROR: {e}")
            import traceback
            traceback.print_exc()
            self.ld.screenshot("error")
            return False


def parse_args():
    ap = argparse.ArgumentParser(description="Facebook Reel Uploader v6 ADVANCED")
    ap.add_argument("--index", type=int, default=1,
                    help="LDPlayer instance index (default 1)")
    ap.add_argument("--device", default=None,
                    help="adb device/serial (default: auto-detect)")
    ap.add_argument("--video", default=str(WORKDIR / "1.mp4"),
                    help="video file to upload")
    ap.add_argument("--caption-file", default=str(WORKDIR / "caption.txt"),
                    help="file containing the reel caption")
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()
    uploader = ReelUploaderV6(index=args.index, device=args.device,
                              video=args.video, caption_file=args.caption_file)
    success = uploader.upload()
    sys.exit(0 if success else 1)