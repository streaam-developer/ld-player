#!/usr/bin/env python3
"""
Facebook Reel Uploader v4 - Handles Overlapping Notifications
Deletes old media, imports fresh video, handles "Uploading reel" block
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

class SmartADB:
    def __init__(self, device="emulator-5556"):
        self.device = device
        self.adb = "C:\\LDPlayer\\LDPlayer9\\adb.exe"
        self.dir = Path("C:\\Users\\zrs2026\\AppData\\Local\\Temp\\opencode")
        self.dir.mkdir(exist_ok=True)
    
    def run(self, cmd, wait=1):
        try:
            r = subprocess.run([self.adb, "-s", self.device, "shell"] + cmd.split(),
                             capture_output=True, text=True, timeout=30)
            time.sleep(wait)
            return r.stdout
        except:
            return ""
    
    def tap(self, x, y, w=1):
        self.run(f"input tap {x} {y}", w)
    
    def swipe(self, x1, y1, x2, y2, d=300, w=1):
        self.run(f"input swipe {x1} {y1} {x2} {y2} {d}", w)
    
    def key(self, k, w=0.3):
        self.run(f"input keyevent {k}", w)
    
    def text_type(self, t, w=0.5):
        escaped = t.replace(" ", "%s")
        self.run(f"input text '{escaped}'", w)
    
    def back(self, w=1):
        self.run("input keyevent KEYCODE_BACK", w)
    
    def home(self, w=1):
        self.run("input keyevent KEYCODE_HOME", w)
    
    def get_ui(self):
        self.run("uiautomator dump /sdcard/ui.xml", 0.5)
        r = subprocess.run([self.adb, "-s", self.device, "shell", "cat", "/sdcard/ui.xml"],
                         capture_output=True, text=True)
        return r.stdout
    
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
    
    def scroll_down(self, times=1, fast=False):
        for _ in range(times):
            if fast:
                self.swipe(540, 1500, 540, 600, 200, 0.3)
            else:
                self.swipe(540, 1500, 540, 800, 300, 0.5)
    
    def scroll_up(self, times=1):
        for _ in range(times):
            self.swipe(540, 800, 540, 1500, 300, 0.5)
    
    def screenshot(self, name="shot"):
        ts = datetime.now().strftime("%H%M%S")
        p = self.dir / f"{name}_{ts}.png"
        self.run("screencap -p /sdcard/shot.png", 0.3)
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
    
    def clear_media(self):
        """Delete all old photos and videos"""
        print("  Clearing old media...")
        self.run("rm -rf /sdcard/DCIM/Camera/*", 1)
        self.run("rm -rf /sdcard/Pictures/*", 0.5)
        self.run("rm -rf /sdcard/Download/*.mp4", 0.5)
        self.run("rm -rf /sdcard/Movies/*", 0.5)
        print("  Old media deleted!")
    
    def import_video(self, video_path):
        """Import video to LDPlayer"""
        print(f"  Importing: {Path(video_path).name}")
        self.run(f"push \"{video_path}\" /sdcard/DCIM/Camera/reel_video.mp4", 5)
        # Scan media
        self.run("am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file:///sdcard/DCIM/Camera/reel_video.mp4", 2)
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
    
    def dismiss_upload_notification(self):
        """Try to dismiss upload notification"""
        # Try pressing back
        self.back(1)
        # Try tapping elsewhere
        self.tap(540, 400, 1)
        # Try scrolling up
        self.scroll_up(1)


class ReelUploaderV4:
    def __init__(self):
        self.a = SmartADB()
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
            self.log("\n=== Facebook Reel Uploader v4 ===\n", True)
            
            # Read caption
            caption = self.read_caption()
            
            # STEP 1: Clear old media and import new video
            self.step(1, "Preparing video")
            self.a.clear_media()
            
            video = self.find_random_video()
            if video:
                self.a.import_video(video)
            else:
                self.log("ERROR: No MP4 videos found!")
                return False
            
            # STEP 2: Open Facebook
            self.step(2, "Opening Facebook")
            self.a.run("am start -n com.facebook.katana/com.facebook.katana.LoginActivity", 4)
            
            # Go to home first
            self.a.home(1)
            time.sleep(1)
            self.a.run("am start -n com.facebook.katana/com.facebook.katana.LoginActivity", 3)
            
            # STEP 3: Wait for upload to complete if any
            self.step(3, "Checking for ongoing uploads")
            if self.a.is_on_screen("Uploading reel"):
                self.a.wait_for_upload_complete(30)
            
            self.a.screenshot("step3_home")
            
            # STEP 4: Go to Profile
            self.step(4, "Going to Profile")
            # Profile tab is rightmost (position 6)
            self.a.tap(990, 1840, 2)
            time.sleep(2)
            
            # Verify profile
            if not (self.a.is_on_screen("Add to story") or self.a.is_on_screen("Edit profile")):
                self.a.tap_find(desc="Profile", timeout=5)
                time.sleep(2)
            
            self.a.screenshot("step4_profile")
            
            # STEP 5: Click Reels tab
            self.step(5, "Clicking Reels tab")
            self.a.tap_find(text="Reels", timeout=5)
            time.sleep(2)
            
            # STEP 6: Handle "Uploading reel" notification
            self.step(6, "Handling upload notification")
            
            # Check if Create Reel is blocked
            create_pos = self.a.find(text="Create reel")
            upload_pos = self.a.find(text="Uploading reel")
            
            if upload_pos and create_pos:
                # Check if they overlap (upload notification is below create button)
                if abs(upload_pos[1] - create_pos[1]) < 200:
                    print("  Notification blocking Create Reel button!")
                    print("  Trying to dismiss...")
                    
                    # Wait for upload to complete
                    self.a.wait_for_upload_complete(60)
                    
                    # If still blocking, try to scroll
                    if self.a.is_on_screen("Uploading reel"):
                        self.a.scroll_down(2)
                        time.sleep(2)
            
            self.a.screenshot("step6_notification_handled")
            
            # STEP 7: Find and click Create Reel
            self.step(7, "Clicking Create Reel")
            
            # Scroll to find Create Reel button
            found = False
            for i in range(8):
                if self.a.tap_find(text="Create reel", timeout=2):
                    found = True
                    break
                print(f"  Scrolling to find Create reel... ({i+1}/8)")
                self.a.scroll_down(1)
                time.sleep(1)
            
            if not found:
                # Try tapping in the area below "No reels yet"
                print("  Using coordinate fallback for Create Reel")
                self.a.tap(540, 1620, 3)
            
            time.sleep(3)
            self.a.screenshot("step7_create_reel")
            
            # STEP 8: Select VIDEO
            self.step(8, "Selecting video")
            time.sleep(2)
            
            # The video should be first item in gallery
            # Look for video thumbnail
            items = self.a.find_all(desc="photo")
            if not items:
                items = self.a.find_all(desc="video")
            
            if items:
                print(f"  Found {len(items)} items, selecting first")
                self.a.tap(items[0][0], items[0][1], 3)
            else:
                # Tap in gallery area (first item position)
                print("  Tapping gallery area (first item)")
                self.a.tap(180, 600, 3)
            
            self.a.screenshot("step8_video_selected")
            
            # STEP 9: Handle dialogs
            self.step(9, "Handling dialogs")
            for _ in range(8):
                found, pos = self.a.wait_any(
                    ["Continue", "Done", "Next", "Skip", "Allow", "OK", "Got it"],
                    timeout=2
                )
                if found:
                    print(f"  Clicking: {found}")
                    self.a.tap(pos[0], pos[1], 2)
                else:
                    break
            
            self.a.screenshot("step9_dialogs_done")
            
            # STEP 10: Go to share screen
            self.step(10, "Going to share screen")
            self.a.tap_find(text="Next", timeout=5)
            time.sleep(3)
            
            self.a.screenshot("step10_share_screen")
            
            # STEP 11: Enter caption
            self.step(11, "Entering caption")
            time.sleep(2)
            
            caption_entered = False
            
            # Find caption field
            xml = self.a.get_ui()
            try:
                root = ET.fromstring(xml)
                for e in root.iter('node'):
                    cls = e.get('class', '')
                    if 'EditText' in cls:
                        pos = self.a._center(e)
                        if pos:
                            print(f"  Found caption field at {pos}")
                            self.a.tap(pos[0], pos[1], 1)
                            self.a.key("KEYCODE_MOVE_END", 0.2)
                            for _ in range(30):
                                self.a.key("KEYCODE_DEL", 0.02)
                            self.a.text_type(caption, 1)
                            caption_entered = True
                            break
            except:
                pass
            
            if not caption_entered:
                # Try text hints
                hints = ["Describe your reel", "Write something", "Caption", "Add caption"]
                for hint in hints:
                    if self.a.tap_find(text=hint, timeout=2):
                        time.sleep(0.5)
                        self.a.key("KEYCODE_MOVE_END", 0.2)
                        for _ in range(30):
                            self.a.key("KEYCODE_DEL", 0.02)
                        self.a.text_type(caption, 1)
                        caption_entered = True
                        break
            
            if not caption_entered:
                # Coordinate fallback
                print("  Using coordinate fallback")
                self.a.tap(600, 350, 1)
                self.a.key("KEYCODE_MOVE_END", 0.2)
                for _ in range(30):
                    self.a.key("KEYCODE_DEL", 0.02)
                self.a.text_type(caption, 1)
            
            self.a.screenshot("step11_caption")
            
            # STEP 12: Share
            self.step(12, "Sharing reel")
            shared = False
            if self.a.tap_find(text="Share now", timeout=5):
                shared = True
            elif self.a.tap_find(text="Share", timeout=3):
                shared = True
            
            if shared:
                print("\n  Uploading reel...")
                time.sleep(10)
            
            self.a.screenshot("step12_done")
            
            self.log("\n=== REEL UPLOAD COMPLETE ===", True)
            self.log(f"Caption: {caption}")
            
            return True
            
        except Exception as e:
            self.log(f"\nERROR: {e}")
            import traceback
            traceback.print_exc()
            self.a.screenshot("error")
            return False


if __name__ == "__main__":
    uploader = ReelUploaderV4()
    success = uploader.upload()
    sys.exit(0 if success else 1)
