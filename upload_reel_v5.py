#!/usr/bin/env python3
"""
Facebook Reel Uploader v5 - FINAL
Starts LDPlayer, Opens Facebook, Handles everything
"""

import subprocess
import time
import os
import sys
import re
import random
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime

class LDPlayerController:
    def __init__(self):
        self.console = "C:\\LDPlayer\\LDPlayer9\\ldconsole.exe"
        self.adb = "C:\\LDPlayer\\LDPlayer9\\adb.exe"
        self.device = "emulator-5556"
        self.ldplayer_index = 1  # LDPlayer-1
        self.dir = Path("C:\\Users\\zrs2026\\AppData\\Local\\Temp\\opencode")
        self.dir.mkdir(exist_ok=True)
    
    def run_console(self, cmd, wait=1):
        """Run LDPlayer console command"""
        try:
            r = subprocess.run([self.console] + cmd.split(), 
                            capture_output=True, text=True, timeout=60)
            time.sleep(wait)
            return r.stdout
        except Exception as e:
            print(f"  Console error: {e}")
            return ""
    
    def run_adb(self, cmd, wait=1):
        """Run ADB command"""
        try:
            r = subprocess.run([self.adb, "-s", self.device, "shell"] + cmd.split(),
                            capture_output=True, text=True, timeout=30)
            time.sleep(wait)
            return r.stdout
        except:
            return ""
    
    def is_running(self):
        """Check if LDPlayer is running"""
        r = self.run_console(f"isrunning --index {self.ldplayer_index}", 2)
        return "running" in r.lower()
    
    def start_ldplayer(self):
        """Start LDPlayer"""
        print("  Starting LDPlayer...")
        self.run_console(f"launch --index {self.ldplayer_index}", 5)
        
        # Wait for ADB to be ready
        print("  Waiting for LDPlayer to boot...")
        for i in range(30):
            time.sleep(5)
            r = subprocess.run([self.adb, "devices"], capture_output=True, text=True)
            if self.device in r.stdout:
                print(f"  LDPlayer ready! (took ~{(i+1)*5}s)")
                return True
            print(f"  Waiting... ({(i+1)*5}s)")
        
        print("  LDPlayer boot timeout!")
        return False
    
    def is_adb_connected(self):
        """Check if ADB device is connected"""
        r = subprocess.run([self.adb, "devices"], capture_output=True, text=True)
        return self.device in r.stdout
    
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
    
    def find(self, text=None, desc=None, exact=False):
        xml = self.get_ui()
        try:
            root = ET.fromstring(xml)
            for e in root.iter('node'):
                if text:
                    t = e.get('text', '')
                    if exact:
                        if t != text: continue
                    else:
                        if text.lower() not in t.lower(): continue
                    return self._center(e)
                if desc:
                    d = e.get('content-desc', '')
                    if desc.lower() in d.lower():
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
    
    def open_facebook(self):
        """Open Facebook app"""
        print("  Opening Facebook...")
        self.run_adb("am start -n com.facebook.katana/com.facebook.katana.LoginActivity", 5)
        time.sleep(3)
        
        # Check if Facebook opened
        if self.is_on_screen("What's on your mind") or self.is_on_screen("facebook"):
            print("  Facebook opened!")
            return True
        
        # Try again
        print("  Retrying Facebook...")
        self.run_adb("am start -n com.facebook.katana/com.facebook.katana.LoginActivity", 5)
        time.sleep(5)
        return True
    
    def clear_media(self):
        """Delete all old photos and videos"""
        print("  Clearing old media...")
        self.run_adb("rm -rf /sdcard/DCIM/Camera/*", 1)
        self.run_adb("rm -rf /sdcard/Pictures/*", 0.5)
        print("  Old media deleted!")
    
    def import_video(self, video_path):
        """Import video to LDPlayer"""
        print(f"  Importing: {Path(video_path).name}")
        # Use push command with index
        subprocess.run([self.console, "push", "--index", str(self.ldplayer_index),
                       "--remote", "/sdcard/DCIM/Camera/reel_video.mp4",
                       "--local", video_path], 
                      capture_output=True, timeout=60)
        time.sleep(3)
        
        # Scan media
        self.run_adb("am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file:///sdcard/DCIM/Camera/reel_video.mp4", 2)
        print("  Video imported!")
    
    def wait_for_upload_complete(self, max_wait=60):
        """Wait for 'Uploading reel...' to disappear"""
        print("  Waiting for previous upload to complete...")
        start = time.time()
        while time.time() - start < max_wait:
            if not self.is_on_screen("Uploading reel"):
                print("  Upload completed!")
                return True
            print(f"  Still uploading... ({int(time.time()-start)}s)")
            time.sleep(3)
        print("  Upload timeout - proceeding anyway")
        return False


class ReelUploaderV5:
    def __init__(self):
        self.ld = LDPlayerController()
        self.caption_file = Path("D:\\git hub all repo\\ld-player\\caption.txt")
        self.videos_folder = Path("D:\\git hub all repo\\ld-player")
    
    def log(self, msg, ok=False):
        c = "\033[32m" if ok else ""
        print(f"{c}{msg}\033[0m")
    
    def read_caption(self):
        if self.caption_file.exists():
            cap = self.caption_file.read_text().strip()
            self.log(f"Caption: {cap}", True)
            return cap
        return "Check out this video! #viral #reels"
    
    def find_random_video(self):
        """Find random MP4 video from videos folder"""
        videos = list(self.videos_folder.glob("*.mp4"))
        if videos:
            chosen = random.choice(videos)
            print(f"  Selected video: {chosen.name}")
            return str(chosen)
        return None
    
    def step(self, n, msg):
        print(f"\n{'='*50}")
        print(f"  STEP {n}: {msg}")
        print(f"{'='*50}")
    
    def upload(self):
        try:
            self.log("\n=== Facebook Reel Uploader v5 ===\n", True)
            
            # Read caption
            caption = self.read_caption()
            
            # STEP 1: Check and start LDPlayer
            self.step(1, "Checking LDPlayer")
            
            if not self.ld.is_running():
                print("  LDPlayer is not running!")
                if not self.ld.start_ldplayer():
                    self.log("ERROR: Could not start LDPlayer!")
                    return False
            else:
                print("  LDPlayer is already running")
            
            # Check ADB connection
            if not self.ld.is_adb_connected():
                print("  Waiting for ADB...")
                time.sleep(10)
                if not self.ld.is_adb_connected():
                    self.log("ERROR: ADB not connected!")
                    return False
            
            print("  LDPlayer ready!")
            
            # STEP 2: Prepare video
            self.step(2, "Preparing video")
            self.ld.clear_media()
            
            video = self.find_random_video()
            if video:
                self.ld.import_video(video)
            else:
                self.log("ERROR: No MP4 videos found!")
                return False
            
            # STEP 3: Open Facebook
            self.step(3, "Opening Facebook")
            self.ld.open_facebook()
            
            # STEP 4: Wait for any ongoing upload
            self.step(4, "Checking for ongoing uploads")
            if self.ld.is_on_screen("Uploading reel"):
                self.ld.wait_for_upload_complete(60)
            
            self.ld.screenshot("step4_home")
            
            # STEP 5: Go to Profile
            self.step(5, "Going to Profile")
            # Profile tab is rightmost (position 6)
            self.ld.tap(990, 1840, 2)
            time.sleep(2)
            
            # Verify profile
            if not (self.ld.is_on_screen("Add to story") or self.ld.is_on_screen("Edit profile")):
                self.ld.tap_find(desc="Profile", timeout=5)
                time.sleep(2)
            
            self.ld.screenshot("step5_profile")
            
            # STEP 6: Click Reels tab
            self.step(6, "Clicking Reels tab")
            self.ld.tap_find(text="Reels", timeout=5)
            time.sleep(2)
            
            # STEP 7: Handle "Uploading reel" notification
            self.step(7, "Handling upload notification")
            
            # Check if Create Reel is blocked
            create_pos = self.ld.find(text="Create reel")
            upload_pos = self.ld.find(text="Uploading reel")
            
            if upload_pos and create_pos:
                # Check if they overlap
                if abs(upload_pos[1] - create_pos[1]) < 200:
                    print("  Notification blocking Create Reel!")
                    self.ld.wait_for_upload_complete(60)
            
            self.ld.screenshot("step7_handled")
            
            # STEP 8: Find and click Create Reel
            self.step(8, "Clicking Create Reel")
            
            # Scroll to find Create Reel button
            found = False
            for i in range(8):
                if self.ld.tap_find(text="Create reel", timeout=2):
                    found = True
                    break
                print(f"  Scrolling to find Create reel... ({i+1}/8)")
                self.ld.scroll_down(1)
                time.sleep(1)
            
            if not found:
                print("  Using coordinate fallback")
                self.ld.tap(540, 1620, 3)
            
            time.sleep(3)
            self.ld.screenshot("step8_create_reel")
            
            # STEP 9: Select VIDEO
            self.step(9, "Selecting video")
            time.sleep(2)
            
            # The video should be first item in gallery
            items = self.ld.find_all(desc="photo")
            if not items:
                items = self.ld.find_all(desc="video")
            
            if items:
                print(f"  Found {len(items)} items, selecting first")
                self.ld.tap(items[0][0], items[0][1], 3)
            else:
                print("  Tapping gallery area (first item)")
                self.ld.tap(180, 600, 3)
            
            self.ld.screenshot("step9_video_selected")
            
            # STEP 10: Handle dialogs
            self.step(10, "Handling dialogs")
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
            
            self.ld.screenshot("step10_dialogs_done")
            
            # STEP 11: Go to share screen
            self.step(11, "Going to share screen")
            self.ld.tap_find(text="Next", timeout=5)
            time.sleep(3)
            
            self.ld.screenshot("step11_share_screen")
            
            # STEP 12: Enter caption
            self.step(12, "Entering caption")
            time.sleep(2)
            
            caption_entered = False
            
            # Find caption field
            xml = self.ld.get_ui()
            try:
                root = ET.fromstring(xml)
                for e in root.iter('node'):
                    cls = e.get('class', '')
                    if 'EditText' in cls:
                        pos = self.ld._center(e)
                        if pos:
                            print(f"  Found caption field at {pos}")
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
                hints = ["Describe your reel", "Write something", "Caption", "Add caption"]
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
            
            self.ld.screenshot("step12_caption")
            
            # STEP 13: Share
            self.step(13, "Sharing reel")
            shared = False
            if self.ld.tap_find(text="Share now", timeout=5):
                shared = True
            elif self.ld.tap_find(text="Share", timeout=3):
                shared = True
            
            if shared:
                print("\n  Uploading reel...")
                time.sleep(10)
            
            self.ld.screenshot("step13_done")
            
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
    uploader = ReelUploaderV5()
    success = uploader.upload()
    sys.exit(0 if success else 1)
