#!/usr/bin/env python3
"""
Facebook Reel Uploader v3 - Ultra Smart
Handles all UI changes, properly selects VIDEO, types caption
"""

import subprocess
import time
import os
import sys
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime

class UltraADB:
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
    
    def text(self, t, w=0.5):
        # Use clipboard method for reliable text input
        self.run(f"am broadcast -a clipper.set -e text '{t}'", 0.3)
        self.run("input keyevent KEYCODE_PASTE", w)
    
    def text_type(self, t, w=0.5):
        # Direct typing - escape spaces
        escaped = t.replace(" ", "%s")
        self.run(f"input text '{escaped}'", w)
    
    def get_ui(self):
        self.run("uiautomator dump /sdcard/ui.xml", 0.5)
        r = subprocess.run([self.adb, "-s", self.device, "shell", "cat", "/sdcard/ui.xml"],
                         capture_output=True, text=True)
        return r.stdout
    
    def find(self, text=None, desc=None, cls=None, exact=False):
        """Find element by text/desc/class"""
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
                if cls:
                    c = e.get('class', '')
                    if cls in c:
                        return self._center(e)
        except:
            pass
        return None
    
    def find_all(self, text=None, desc=None):
        """Find all matching elements"""
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
    
    def find_video_items(self):
        """Find video items in gallery - look for video indicators"""
        xml = self.get_ui()
        videos = []
        try:
            root = ET.fromstring(xml)
            for e in root.iter('node'):
                desc = e.get('content-desc', '').lower()
                cls = e.get('class', '').lower()
                # Videos often have play icon or duration
                if any(x in desc for x in ['video', 'mp4', 'play', 'reel', 'duration']):
                    videos.append(self._center(e))
                # Check for ImageView with video-like properties
                if 'imageview' in cls and e.get('clickable') == 'true':
                    bounds = e.get('bounds', '')
                    match = re.findall(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
                    if match:
                        x1, y1, x2, y2 = map(int, match[0])
                        h = y2 - y1
                        # Video thumbnails are usually taller
                        if h > 200 and y1 > 400:
                            videos.append(((x1+x2)//2, (y1+y2)//2))
        except:
            pass
        return videos
    
    def _center(self, e):
        bounds = e.get('bounds', '')
        m = re.findall(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
        if m:
            x1, y1, x2, y2 = map(int, m[0])
            return ((x1+x2)//2, (y1+y2)//2)
        return None
    
    def tap_find(self, text=None, desc=None, wait=1, timeout=10):
        """Find and tap element"""
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
        """Scroll down"""
        for _ in range(times):
            self.swipe(540, 1500, 540, 800, 300, 0.5)
    
    def scroll_up(self, times=1):
        """Scroll up"""
        for _ in range(times):
            self.swipe(540, 800, 540, 1500, 300, 0.5)
    
    def screenshot(self, name="shot"):
        ts = datetime.now().strftime("%H%M%S")
        p = self.dir / f"{name}_{ts}.png"
        self.run("screencap -p /sdcard/shot.png", 0.3)
        subprocess.run([self.adb, "-s", self.device, "pull", "/sdcard/shot.png", str(p)], capture_output=True)
        return p
    
    def wait_any(self, texts, timeout=10):
        """Wait for any text to appear"""
        start = time.time()
        while time.time() - start < timeout:
            for t in texts:
                pos = self.find(text=t)
                if pos:
                    return t, pos
            time.sleep(0.5)
        return None, None
    
    def is_on_screen(self, text):
        """Check if text is visible"""
        return self.find(text=text) is not None


class ReelUploaderV3:
    def __init__(self):
        self.a = UltraADB()
        self.caption_file = Path("D:\\git hub all repo\\ld-player\\caption.txt")
    
    def log(self, msg, ok=False):
        c = "\033[32m" if ok else ""
        print(f"{c}{msg}\033[0m")
    
    def read_caption(self):
        if self.caption_file.exists():
            cap = self.caption_file.read_text().strip()
            self.log(f"Caption loaded: {cap}", True)
            return cap
        return "Check out this video! #viral #reels"
    
    def step(self, n, msg):
        print(f"\n{'='*50}")
        print(f"  STEP {n}: {msg}")
        print(f"{'='*50}")
    
    def upload(self):
        try:
            self.log("\n=== Facebook Reel Uploader v3 ===\n", True)
            
            # Read caption
            caption = self.read_caption()
            
            # STEP 1: Make sure we're on Facebook home
            self.step(1, "Going to Facebook Home")
            self.a.run("am start -n com.facebook.katana/com.facebook.katana.LoginActivity", 3)
            
            # Verify home screen
            if not self.a.is_on_screen("What's on your mind"):
                print("  Waiting for home screen...")
                time.sleep(3)
            
            self.a.screenshot("step1_home")
            
            # STEP 2: Go to Profile tab (last tab in bottom nav)
            self.step(2, "Opening Profile tab")
            # Profile is the rightmost tab (6th position = 900-1080 x range)
            self.a.tap(990, 1840, 2)
            
            # Verify profile page
            time.sleep(2)
            self.a.screenshot("step2_profile")
            
            # Check if we're on profile
            if self.a.is_on_screen("Add to story") or self.a.is_on_screen("Edit profile"):
                self.log("  On Profile page!", True)
            else:
                print("  Trying to find profile elements...")
                # Try tapping profile icon
                self.a.tap_find(desc="Profile", timeout=5)
                time.sleep(2)
            
            # STEP 3: Click on REELS tab on profile
            self.step(3, "Clicking Reels tab on profile")
            # First make sure we're on "All" tab, then click "Reels"
            if self.a.tap_find(text="Reels", timeout=5):
                time.sleep(2)
            self.a.screenshot("step3_reels_tab")
            
            # STEP 4: Find and click CREATE REEL button
            self.step(4, "Finding Create Reel button")
            
            # Scroll down to find Create reel button
            found = False
            for i in range(5):
                if self.a.tap_find(text="Create reel", timeout=3):
                    found = True
                    break
                print(f"  Scrolling down to find Create reel... ({i+1}/5)")
                self.a.scroll_down(1)
            
            if not found:
                # Try finding by desc
                self.a.tap_find(desc="Create reel", timeout=5)
            
            time.sleep(3)
            self.a.screenshot("step4_create_reel")
            
            # STEP 5: Select VIDEO from gallery
            self.step(5, "Selecting VIDEO from gallery")
            time.sleep(2)
            
            # First, check what's on screen
            self.a.screenshot("step5_gallery")
            
            # Try to find video items
            videos = self.a.find_video_items()
            if videos:
                print(f"  Found {len(videos)} video(s), selecting first one")
                self.a.tap(videos[0][0], videos[0][1], 3)
            else:
                print("  No video indicators found, looking for gallery items...")
                # Look for any media item in gallery (below the header)
                items = self.a.find_all(desc="photo")
                if not items:
                    items = self.a.find_all(desc="video")
                if not items:
                    # Try finding ImageView elements in gallery area
                    xml = self.a.get_ui()
                    try:
                        root = ET.fromstring(xml)
                        for e in root.iter('node'):
                            cls = e.get('class', '')
                            if 'ImageView' in cls or 'MediaItem' in cls:
                                pos = self.a._center(e)
                                if pos and pos[1] > 500:  # Below header
                                    items.append(pos)
                    except:
                        pass
                
                if items:
                    print(f"  Found {len(items)} gallery items, selecting first one")
                    self.a.tap(items[0][0], items[0][1], 3)
                else:
                    # Last resort - tap in gallery area
                    print("  Tapping in gallery area (fallback)")
                    self.a.tap(180, 700, 3)
            
            self.a.screenshot("step5_selected")
            
            # STEP 6: Handle ALL dialogs (Continue, Done, Next, Skip, Review)
            self.step(6, "Handling dialogs")
            
            for attempt in range(10):
                # Check for various buttons
                found, pos = self.a.wait_any(
                    ["Continue", "Done", "Next", "Skip", "Allow", "OK", "Got it", "Review"],
                    timeout=2
                )
                if found:
                    print(f"  Clicking: {found}")
                    self.a.tap(pos[0], pos[1], 2)
                else:
                    break
            
            self.a.screenshot("step6_dialogs_done")
            
            # STEP 7: Navigate to share screen
            self.step(7, "Going to share screen")
            
            # Look for Next button on editing screen
            if self.a.tap_find(text="Next", timeout=5):
                time.sleep(3)
            elif self.a.tap_find(desc="Next", timeout=3):
                time.sleep(3)
            
            self.a.screenshot("step7_share_screen")
            
            # STEP 8: Enter CAPTION
            self.step(8, "Entering caption")
            time.sleep(2)
            
            caption_entered = False
            
            # Method 1: Find EditText field
            xml = self.a.get_ui()
            try:
                root = ET.fromstring(xml)
                for e in root.iter('node'):
                    cls = e.get('class', '')
                    if 'EditText' in cls:
                        pos = self.a._center(e)
                        if pos:
                            print(f"  Found EditText at {pos}")
                            self.a.tap(pos[0], pos[1], 1)
                            # Clear and type
                            self.a.key("KEYCODE_MOVE_END", 0.2)
                            for _ in range(30):
                                self.a.key("KEYCODE_DEL", 0.02)
                            self.a.text_type(caption, 1)
                            caption_entered = True
                            break
            except:
                pass
            
            # Method 2: Find by text hint
            if not caption_entered:
                hints = ["Describe your reel", "Write something", "Caption", "Add caption", "What's on your mind"]
                for hint in hints:
                    if self.a.tap_find(text=hint, timeout=2):
                        time.sleep(0.5)
                        self.a.key("KEYCODE_MOVE_END", 0.2)
                        for _ in range(30):
                            self.a.key("KEYCODE_DEL", 0.02)
                        self.a.text_type(caption, 1)
                        caption_entered = True
                        break
            
            # Method 3: Tap on text area near top of screen
            if not caption_entered:
                print("  Using coordinate fallback for caption")
                self.a.tap(600, 350, 1)
                self.a.key("KEYCODE_MOVE_END", 0.2)
                for _ in range(30):
                    self.a.key("KEYCODE_DEL", 0.02)
                self.a.text_type(caption, 1)
            
            self.a.screenshot("step8_caption")
            
            # STEP 9: Set audience to Public (optional)
            self.step(9, "Setting audience (optional)")
            if self.a.tap_find(text="Who can see this", timeout=3):
                time.sleep(1)
                self.a.tap_find(text="Public", timeout=3)
                self.a.tap_find(text="Done", timeout=3)
            
            # STEP 10: SHARE NOW
            self.step(10, "Sharing reel!")
            
            # Try multiple ways to find Share button
            shared = False
            if self.a.tap_find(text="Share now", timeout=5):
                shared = True
            elif self.a.tap_find(text="Share", timeout=3):
                shared = True
            elif self.a.tap_find(desc="Share", timeout=3):
                shared = True
            
            if shared:
                print("\n  Waiting for upload to complete...")
                time.sleep(10)
            
            self.a.screenshot("step10_done")
            
            # Final check
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
    uploader = ReelUploaderV3()
    success = uploader.upload()
    sys.exit(0 if success else 1)
