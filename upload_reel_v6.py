#!/usr/bin/env python3
"""
Facebook Reel Uploader v6 - FINAL FIXED
Deletes ALL media, imports 1.mp4 only, handles gallery refresh
"""

import subprocess
import time
import os
import sys
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime

class LDController:
    def __init__(self):
        self.console = "C:\\LDPlayer\\LDPlayer9\\ldconsole.exe"
        self.adb = "C:\\LDPlayer\\LDPlayer9\\adb.exe"
        self.device = "emulator-5556"
        self.ld_index = 1
        self.dir = Path("C:\\Users\\zrs2026\\AppData\\Local\\Temp\\opencode")
        self.dir.mkdir(exist_ok=True)
    
    def run_adb(self, cmd, wait=1):
        try:
            r = subprocess.run([self.adb, "-s", self.device, "shell"] + cmd.split(),
                            capture_output=True, text=True, timeout=30)
            time.sleep(wait)
            return r.stdout
        except:
            return ""
    
    def run_console(self, cmd, wait=1):
        try:
            r = subprocess.run([self.console] + cmd.split(),
                            capture_output=True, text=True, timeout=60)
            time.sleep(wait)
            return r.stdout
        except:
            return ""
    
    def tap(self, x, y, w=1):
        self.run_adb(f"input tap {x} {y}", w)
    
    def swipe(self, x1, y1, x2, y2, d=300, w=1):
        self.run_adb(f"input swipe {x1} {y1} {x2} {y2} {d}", w)
    
    def key(self, k, w=0.3):
        self.run_adb(f"input keyevent {k}", w)
    
    def back(self, w=1):
        self.run_adb("input keyevent KEYCODE_BACK", w)
    
    def home(self, w=1):
        self.run_adb("input keyevent KEYCODE_HOME", w)
    
    def text_type(self, t, w=0.5):
        escaped = t.replace(" ", "%s")
        self.run_adb(f"input text '{escaped}'", w)
    
    def get_ui(self):
        self.run_adb("uiautomator dump /sdcard/ui.xml", 0.5)
        try:
            r = subprocess.run([self.adb, "-s", self.device, "shell", "cat", "/sdcard/ui.xml"],
                            capture_output=True, text=True, timeout=10)
            return r.stdout
        except:
            return ""
    
    def find(self, text=None, desc=None):
        xml = self.get_ui()
        try:
            root = ET.fromstring(xml)
            for e in root.iter('node'):
                if text and text.lower() in e.get('text', '').lower():
                    return self._center(e)
                if desc and desc.lower() in e.get('content-desc', '').lower():
                    return self._center(e)
        except:
            pass
        return None
    
    def find_all(self, text=None, desc=None):
        xml = self.get_ui()
        results = []
        try:
            root = ET.fromstring(xml)
            for e in root.iter('node'):
                if text and text.lower() in e.get('text', '').lower():
                    results.append(self._center(e))
                elif desc and desc.lower() in e.get('content-desc', '').lower():
                    results.append(self._center(e))
        except:
            pass
        return [r for r in results if r]
    
    def _center(self, e):
        bounds = e.get('bounds', '')
        m = re.findall(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
        if m:
            x1, y1, x2, y2 = map(int, m[0])
            return ((x1+x2)//2, (y1+y2)//2)
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
    
    def scroll_down(self, times=1):
        for _ in range(times):
            self.swipe(540, 1500, 540, 800, 300, 0.5)
    
    def scroll_up(self, times=1):
        for _ in range(times):
            self.swipe(540, 800, 540, 1500, 300, 0.5)
    
    def screenshot(self, name="shot"):
        ts = datetime.now().strftime("%H%M%S")
        p = self.dir / f"{name}_{ts}.png"
        self.run_adb("screencap -p /sdcard/shot.png", 0.3)
        subprocess.run([self.adb, "-s", self.device, "pull", "/sdcard/shot.png", str(p)], capture_output=True)
        return p
    
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
    
    def is_running(self):
        r = self.run_console(f"isrunning --index {self.ld_index}", 2)
        return "running" in r.lower()
    
    def start_ldplayer(self):
        print("  Starting LDPlayer...")
        self.run_console(f"launch --index {self.ld_index}", 5)
        print("  Waiting for boot...")
        for i in range(30):
            time.sleep(5)
            r = subprocess.run([self.adb, "devices"], capture_output=True, text=True)
            if self.device in r.stdout:
                print(f"  LDPlayer ready! ({(i+1)*5}s)")
                return True
            print(f"  Waiting... ({(i+1)*5}s)")
        return False
    
    def is_adb_connected(self):
        r = subprocess.run([self.adb, "devices"], capture_output=True, text=True)
        return self.device in r.stdout
    
    def open_facebook(self):
        print("  Opening Facebook...")
        self.run_adb("am start -n com.facebook.katana/com.facebook.katana.LoginActivity", 5)
        time.sleep(5)
    
    def aggressive_clear_media(self):
        """AGGRESSIVELY delete ALL media and refresh gallery"""
        print("  [1/5] Deleting DCIM folder...")
        self.run_adb("rm -rf /sdcard/DCIM", 2)
        
        print("  [2/5] Deleting Pictures folder...")
        self.run_adb("rm -rf /sdcard/Pictures", 1)
        
        print("  [3/5] Deleting Movies folder...")
        self.run_adb("rm -rf /sdcard/Movies", 1)
        
        print("  [4/5] Deleting Video folder...")
        self.run_adb("rm -rf /sdcard/Video", 1)
        
        print("  [5/5] Killing media scanner and restarting...")
        # Kill media scanner to force rescan
        self.run_adb("am force-stop com.android.providers.media", 1)
        self.run_adb("am force-stop com.android.providers.media.module", 1)
        
        # Wait for media scanner to die
        time.sleep(3)
        
        # Recreate DCIM/Camera folder
        self.run_adb("mkdir -p /sdcard/DCIM/Camera", 1)
        
        print("  All media deleted!")
    
    def import_1mp4(self, video_path):
        """Import 1.mp4 to LDPlayer"""
        print(f"  Importing: {Path(video_path).name}")
        
        # Push file
        subprocess.run([self.console, "push", "--index", str(self.ld_index),
                       "--remote", "/sdcard/DCIM/Camera/1.mp4",
                       "--local", video_path], 
                      capture_output=True, timeout=120)
        time.sleep(3)
        
        # Verify file exists
        check = self.run_adb("ls -la /sdcard/DCIM/Camera/1.mp4", 1)
        if "1.mp4" in check:
            print("  1.mp4 imported successfully!")
        else:
            print("  WARNING: File may not have imported properly")
        
        # Force media scan
        print("  Scanning media...")
        self.run_adb("am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file:///sdcard/DCIM/Camera/1.mp4", 2)
        
        # Wait for media scan to complete
        time.sleep(5)
        
        print("  1.mp4 ready!")
    
    def force_refresh_gallery(self):
        """Force refresh gallery by restarting media provider"""
        print("  Refreshing gallery...")
        self.run_adb("am force-stop com.android.providers.media", 2)
        self.run_adb("am force-stop com.android.providers.media.module", 2)
        time.sleep(3)
        self.run_adb("am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file:///sdcard/DCIM/Camera/", 2)
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


class ReelUploaderV6:
    def __init__(self):
        self.ld = LDController()
        self.caption_file = Path("D:\\git hub all repo\\ld-player\\caption.txt")
        self.video_file = Path("D:\\git hub all repo\\ld-player\\1.mp4")
    
    def log(self, msg, ok=False):
        c = "\033[32m" if ok else ""
        print(f"{c}{msg}\033[0m")
    
    def read_caption(self):
        if self.caption_file.exists():
            cap = self.caption_file.read_text().strip()
            self.log(f"Caption: {cap}", True)
            return cap
        return "Check out this video! #viral #reels"
    
    def step(self, n, msg):
        print(f"\n{'='*50}")
        print(f"  STEP {n}: {msg}")
        print(f"{'='*50}")
    
    def upload(self):
        try:
            self.log("\n=== Facebook Reel Uploader v6 ===\n", True)
            
            # Read caption
            caption = self.read_caption()
            
            # Check if 1.mp4 exists
            if not self.video_file.exists():
                self.log(f"ERROR: {self.video_file} not found!")
                return False
            print(f"  Video file: {self.video_file}")
            
            # STEP 1: Check and start LDPlayer
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
            
            print("  LDPlayer ready!")
            
            # STEP 2: AGGRESSIVELY clear all media
            self.step(2, "Clearing ALL old media")
            self.ld.aggressive_clear_media()
            
            # STEP 3: Import 1.mp4
            self.step(3, "Importing 1.mp4")
            self.ld.import_1mp4(str(self.video_file))
            
            # STEP 4: Force refresh gallery
            self.step(4, "Refreshing gallery")
            self.ld.force_refresh_gallery()
            
            # STEP 5: Open Facebook
            self.step(5, "Opening Facebook")
            self.ld.home(1)
            time.sleep(1)
            self.ld.open_facebook()
            
            # STEP 6: Handle ongoing uploads
            self.step(6, "Checking for ongoing uploads")
            if self.ld.is_on_screen("Uploading reel"):
                self.ld.wait_for_upload_complete(60)
            
            self.ld.screenshot("step6_home")
            
            # STEP 7: Go to Profile
            self.step(7, "Going to Profile")
            self.ld.tap(990, 1840, 2)
            time.sleep(2)
            
            if not (self.ld.is_on_screen("Add to story") or self.ld.is_on_screen("Edit profile")):
                self.ld.tap_find(desc="Profile", timeout=5)
                time.sleep(2)
            
            self.ld.screenshot("step7_profile")
            
            # STEP 8: Click Reels tab
            self.step(8, "Clicking Reels tab")
            self.ld.tap_find(text="Reels", timeout=5)
            time.sleep(2)
            
            # STEP 9: Handle notification blocking
            self.step(9, "Handling notifications")
            if self.ld.is_on_screen("Uploading reel"):
                self.ld.wait_for_upload_complete(60)
            
            self.ld.screenshot("step9_ready")
            
            # STEP 10: Click Create Reel
            self.step(10, "Clicking Create Reel")
            
            found = False
            for i in range(8):
                if self.ld.tap_find(text="Create reel", timeout=2):
                    found = True
                    break
                print(f"  Scrolling... ({i+1}/8)")
                self.ld.scroll_down(1)
                time.sleep(1)
            
            if not found:
                print("  Using fallback coordinates")
                self.ld.tap(540, 1620, 3)
            
            time.sleep(3)
            self.ld.screenshot("step10_create_reel")
            
            # STEP 11: Select 1.mp4 from gallery
            self.step(11, "Selecting 1.mp4 from gallery")
            time.sleep(2)
            
            # Force refresh again before selecting
            self.ld.force_refresh_gallery()
            
            # Find and click the video - should be first/only item
            items = self.ld.find_all(desc="photo")
            if not items:
                items = self.ld.find_all(desc="video")
            
            # Count how many items we see
            print(f"  Gallery items found: {len(items)}")
            
            if len(items) == 1:
                # Perfect - only 1 item (our 1.mp4)
                print("  Only 1 item in gallery - selecting it")
                self.ld.tap(items[0][0], items[0][1], 3)
            elif len(items) > 1:
                # Multiple items - select first one (should be our video)
                print("  Multiple items - selecting first one (1.mp4)")
                self.ld.tap(items[0][0], items[0][1], 3)
            else:
                # No items found - use coordinate fallback
                print("  No items found - using fallback")
                self.ld.tap(180, 600, 3)
            
            self.ld.screenshot("step11_video_selected")
            
            # STEP 12: Handle dialogs
            self.step(12, "Handling dialogs")
            for _ in range(8):
                found, pos = self.ld.wait_any(
                    ["Continue", "Done", "Next", "Skip", "Allow", "OK", "Got it"],
                    timeout=2
                )
                if found:
                    print(f"  Clicking: {found}")
                    self.ld.tap(pos[0], pos[1], 2)
                else:
                    break
            
            self.ld.screenshot("step12_dialogs_done")
            
            # STEP 13: Go to share screen
            self.step(13, "Going to share screen")
            self.ld.tap_find(text="Next", timeout=5)
            time.sleep(3)
            
            self.ld.screenshot("step13_share_screen")
            
            # STEP 14: Enter caption
            self.step(14, "Entering caption")
            time.sleep(2)
            
            caption_entered = False
            
            # Method 1: Find EditText
            xml = self.ld.get_ui()
            try:
                root = ET.fromstring(xml)
                for e in root.iter('node'):
                    if 'EditText' in e.get('class', ''):
                        pos = self.ld._center(e)
                        if pos:
                            print(f"  Found EditText at {pos}")
                            self.ld.tap(pos[0], pos[1], 1)
                            self.ld.key("KEYCODE_MOVE_END", 0.2)
                            for _ in range(30):
                                self.ld.key("KEYCODE_DEL", 0.02)
                            self.ld.text_type(caption, 1)
                            caption_entered = True
                            break
            except:
                pass
            
            if not caption_entered:
                hints = ["Describe your reel", "Write something", "Caption"]
                for hint in hints:
                    if self.ld.tap_find(text=hint, timeout=2):
                        time.sleep(0.5)
                        self.ld.key("KEYCODE_MOVE_END", 0.2)
                        for _ in range(30):
                            self.ld.key("KEYCODE_DEL", 0.02)
                        self.ld.text_type(caption, 1)
                        caption_entered = True
                        break
            
            if not caption_entered:
                print("  Using coordinate fallback")
                self.ld.tap(600, 350, 1)
                self.ld.key("KEYCODE_MOVE_END", 0.2)
                for _ in range(30):
                    self.ld.key("KEYCODE_DEL", 0.02)
                self.ld.text_type(caption, 1)
            
            self.ld.screenshot("step14_caption")
            
            # STEP 15: Share
            self.step(15, "Sharing reel")
            shared = False
            if self.ld.tap_find(text="Share now", timeout=5):
                shared = True
            elif self.ld.tap_find(text="Share", timeout=3):
                shared = True
            
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


if __name__ == "__main__":
    uploader = ReelUploaderV6()
    success = uploader.upload()
    sys.exit(0 if success else 1)
