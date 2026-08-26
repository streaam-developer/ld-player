#!/usr/bin/env python3
"""
Facebook Reel Uploader v2 - Smart UI Automator Based
Automatically detects UI elements by text, handles all dialogs
"""

import subprocess
import time
import os
import sys
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime

class SmartADB:
    def __init__(self, device_id="emulator-5556"):
        self.device_id = device_id
        self.adb = "C:\\LDPlayer\\LDPlayer9\\adb.exe"
        self.screen_dir = Path("C:\\Users\\zrs2026\\AppData\\Local\\Temp\\opencode")
        self.screen_dir.mkdir(exist_ok=True)
        
    def cmd(self, command, wait=1):
        """Run shell command"""
        try:
            result = subprocess.run(
                [self.adb, "-s", self.device_id, "shell"] + command.split(),
                capture_output=True, text=True, timeout=30
            )
            time.sleep(wait)
            return result.stdout
        except Exception as e:
            print(f"  [ERROR] {e}")
            return ""
    
    def tap(self, x, y, wait=1):
        """Tap at coordinates"""
        self.cmd(f"input tap {x} {y}", wait)
    
    def text_input(self, text, wait=0.5):
        """Input text - handles special chars"""
        # Use base64 encoding for special characters
        import base64
        encoded = base64.b64encode(text.encode()).decode()
        self.cmd(f"am broadcast -a ADB_INPUT_TEXT --es msg '{encoded}'", wait)
    
    def text_input_simple(self, text, wait=0.5):
        """Simple text input"""
        escaped = text.replace(" ", "%s").replace("'", "\\'")
        self.cmd(f"input text '{escaped}'", wait)
    
    def key(self, keycode, wait=0.3):
        """Press key"""
        self.cmd(f"input keyevent {keycode}", wait)
    
    def get_ui(self):
        """Get UI hierarchy"""
        self.cmd("uiautomator dump /sdcard/ui.xml", 0.5)
        result = subprocess.run(
            [self.adb, "-s", self.device_id, "shell", "cat", "/sdcard/ui.xml"],
            capture_output=True, text=True
        )
        return result.stdout
    
    def find_by_text(self, text, exact=False):
        """Find element center by text"""
        xml_content = self.get_ui()
        try:
            root = ET.fromstring(xml_content)
            for elem in root.iter('node'):
                elem_text = elem.get('text', '')
                if exact:
                    if elem_text == text:
                        return self._get_center(elem)
                else:
                    if text.lower() in elem_text.lower():
                        return self._get_center(elem)
        except:
            pass
        return None
    
    def find_by_desc(self, desc):
        """Find element by content-description"""
        xml_content = self.get_ui()
        try:
            root = ET.fromstring(xml_content)
            for elem in root.iter('node'):
                if desc.lower() in elem.get('content-desc', '').lower():
                    return self._get_center(elem)
        except:
            pass
        return None
    
    def find_by_class_and_text(self, class_name, text):
        """Find by class and text"""
        xml_content = self.get_ui()
        try:
            root = ET.fromstring(xml_content)
            for elem in root.iter('node'):
                if class_name in elem.get('class', '') and text.lower() in elem.get('text', '').lower():
                    return self._get_center(elem)
        except:
            pass
        return None
    
    def _get_center(self, elem):
        """Get center coordinates from bounds"""
        bounds = elem.get('bounds', '')
        match = re.findall(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
        if match:
            x1, y1, x2, y2 = map(int, match[0])
            return ((x1 + x2) // 2, (y1 + y2) // 2)
        return None
    
    def tap_text(self, text, wait=1, timeout=10):
        """Find text and tap it"""
        start = time.time()
        while time.time() - start < timeout:
            pos = self.find_by_text(text)
            if pos:
                print(f"  Found '{text}' at {pos}")
                self.tap(pos[0], pos[1], wait)
                return True
            time.sleep(0.5)
        print(f"  [WARN] '{text}' not found in {timeout}s")
        return False
    
    def tap_desc(self, desc, wait=1, timeout=10):
        """Find desc and tap it"""
        start = time.time()
        while time.time() - start < timeout:
            pos = self.find_by_desc(desc)
            if pos:
                print(f"  Found desc '{desc}' at {pos}")
                self.tap(pos[0], pos[1], wait)
                return True
            time.sleep(0.5)
        print(f"  [WARN] desc '{desc}' not found in {timeout}s")
        return False
    
    def screenshot(self, name="screen"):
        """Take screenshot"""
        ts = datetime.now().strftime("%H%M%S")
        path = self.screen_dir / f"{name}_{ts}.png"
        self.cmd("screencap -p /sdcard/shot.png", 0.3)
        subprocess.run([self.adb, "-s", self.device_id, "pull", "/sdcard/shot.png", str(path)], 
                      capture_output=True)
        return path
    
    def wait_for_any(self, texts, timeout=15):
        """Wait for any of the texts to appear"""
        start = time.time()
        while time.time() - start < timeout:
            for text in texts:
                pos = self.find_by_text(text)
                if pos:
                    return text, pos
            time.sleep(0.5)
        return None, None


class ReelUploader:
    def __init__(self):
        self.adb = SmartADB()
        self.caption_file = Path("D:\\git hub all repo\\ld-player\\caption.txt")
        
    def log(self, msg, level="INFO"):
        colors = {"INFO": "", "OK": "\033[32m", "WARN": "\033[33m", "ERR": "\033[31m"}
        print(f"{colors.get(level, '')}[{level}] {msg}\033[0m")
    
    def read_caption(self):
        if self.caption_file.exists():
            cap = self.caption_file.read_text().strip()
            self.log(f"Caption: {cap}", "OK")
            return cap
        return "Check out this video! #viral #reels"
    
    def step(self, num, msg):
        print(f"\n{'='*50}")
        print(f"  STEP {num}: {msg}")
        print(f"{'='*50}")
    
    def upload(self):
        try:
            self.log("Starting Facebook Reel Uploader v2", "OK")
            
            # Read caption
            caption = self.read_caption()
            
            # Step 1: Open Facebook
            self.step(1, "Opening Facebook")
            self.adb.cmd("am start -n com.facebook.katana/com.facebook.katana.LoginActivity", 3)
            
            # Step 2: Go to Profile
            self.step(2, "Going to Profile tab")
            # Use UI to find profile tab
            if not self.adb.tap_desc("Profile", wait=2, timeout=5):
                # Fallback - try finding by icon
                self.adb.tap_desc("profile", wait=2, timeout=5)
            
            self.adb.screenshot("profile")
            
            # Step 3: Click on Reels tab
            self.step(3, "Clicking Reels tab")
            self.adb.tap_text("Reels", wait=2)
            
            # Step 4: Click Create Reel
            self.step(4, "Clicking Create Reel button")
            if not self.adb.tap_text("Create reel", wait=3, timeout=5):
                # Fallback - try the Reel button
                self.adb.tap_desc("Reel", wait=3)
            
            self.adb.screenshot("create_reel")
            
            # Step 5: Select VIDEO (not photo!)
            self.step(5, "Selecting VIDEO from gallery")
            time.sleep(2)
            
            # Get UI dump to find video items
            xml = self.adb.get_ui()
            try:
                root = ET.fromstring(xml)
                # Look for video thumbnails - they usually have video icon or specific class
                video_found = False
                for elem in root.iter('node'):
                    desc = elem.get('content-desc', '').lower()
                    # Check for video indicators
                    if 'video' in desc or 'mp4' in desc or 'reel' in desc:
                        pos = self.adb._get_center(elem)
                        if pos:
                            print(f"  Found VIDEO: {desc} at {pos}")
                            self.adb.tap(pos[0], pos[1], 3)
                            video_found = True
                            break
                
                if not video_found:
                    # Try first media item in gallery
                    self.log("Looking for gallery items...", "WARN")
                    # Gallery items are usually in a grid
                    for elem in root.iter('node'):
                        cls = elem.get('class', '')
                        if 'ImageView' in cls or 'MediaItem' in cls:
                            pos = self.adb._get_center(elem)
                            if pos and pos[1] > 400:  # Below header
                                print(f"  Trying gallery item at {pos}")
                                self.adb.tap(pos[0], pos[1], 3)
                                video_found = True
                                break
            except Exception as e:
                print(f"  Parse error: {e}")
            
            self.adb.screenshot("video_selected")
            
            # Step 6: Handle Review/Continue/Done dialogs
            self.step(6, "Handling dialogs")
            for i in range(8):
                found_text, pos = self.adb.wait_for_any(
                    ["Continue", "Done", "Next", "Skip", "Allow"], timeout=3
                )
                if found_text:
                    print(f"  Clicking: {found_text}")
                    self.adb.tap(pos[0], pos[1], 2)
                else:
                    break
            
            self.adb.screenshot("after_dialogs")
            
            # Step 7: Click Next to go to share screen
            self.step(7, "Clicking Next")
            self.adb.tap_text("Next", wait=3, timeout=5)
            
            self.adb.screenshot("share_screen")
            
            # Step 8: Find and fill caption
            self.step(8, "Entering caption")
            time.sleep(2)
            
            # Try multiple ways to find caption field
            caption_entered = False
            
            # Method 1: Find EditText
            xml = self.adb.get_ui()
            try:
                root = ET.fromstring(xml)
                for elem in root.iter('node'):
                    cls = elem.get('class', '')
                    text = elem.get('text', '')
                    hint = elem.get('content-desc', '').lower()
                    
                    # Look for caption/description input
                    if ('EditText' in cls or 
                        'describe' in hint or 
                        'caption' in hint or
                        'write something' in text.lower()):
                        pos = self.adb._get_center(elem)
                        if pos:
                            print(f"  Found caption field at {pos}")
                            self.adb.tap(pos[0], pos[1], 1)
                            
                            # Clear existing text
                            self.adb.key("KEYCODE_MOVE_END", 0.2)
                            for _ in range(50):
                                self.adb.key("KEYCODE_DEL", 0.02)
                            
                            # Input caption
                            self.adb.text_input_simple(caption, 1)
                            caption_entered = True
                            break
            except:
                pass
            
            # Method 2: Tap on "Describe your reel" text area
            if not caption_entered:
                if self.adb.tap_text("Describe your reel", wait=1, timeout=3):
                    time.sleep(0.5)
                    self.adb.key("KEYCODE_MOVE_END", 0.2)
                    for _ in range(50):
                        self.adb.key("KEYCODE_DEL", 0.02)
                    self.adb.text_input_simple(caption, 1)
                    caption_entered = True
            
            # Method 3: Try tapping on text area near top
            if not caption_entered:
                self.log("Trying coordinate fallback for caption", "WARN")
                self.adb.tap(600, 300, 1)
                self.adb.key("KEYCODE_MOVE_END", 0.2)
                for _ in range(50):
                    self.adb.key("KEYCODE_DEL", 0.02)
                self.adb.text_input_simple(caption, 1)
            
            self.adb.screenshot("caption_entered")
            
            # Step 9: Click Share now
            self.step(9, "Sharing reel")
            self.adb.tap_text("Share now", wait=5, timeout=10)
            
            time.sleep(5)
            self.adb.screenshot("upload_done")
            
            self.log("=== REEL UPLOADED SUCCESSFULLY ===", "OK")
            self.log(f"Caption: {caption}")
            return True
            
        except Exception as e:
            self.log(f"Error: {e}", "ERR")
            self.adb.screenshot("error")
            import traceback
            traceback.print_exc()
            return False


if __name__ == "__main__":
    uploader = ReelUploader()
    success = uploader.upload()
    sys.exit(0 if success else 1)
