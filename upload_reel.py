#!/usr/bin/env python3
"""
Facebook Reel Uploader - Advanced Python Script
Uses UI Automator for robust UI automation
Handles UI changes dynamically
"""

import subprocess
import time
import os
import sys
import re
from pathlib import Path
from datetime import datetime
import xml.etree.ElementTree as ET

class ADBController:
    def __init__(self, device_id="emulator-5556", adb_path="C:\\LDPlayer\\LDPlayer9\\adb.exe"):
        self.device_id = device_id
        self.adb_path = adb_path
        self.screenshot_dir = Path("C:\\Users\\zrs2026\\AppData\\Local\\Temp\\opencode")
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        
    def run_command(self, command, wait_after=1):
        """Run ADB command"""
        try:
            cmd = [self.adb_path, "-s", self.device_id, "shell"] + command.split()
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            time.sleep(wait_after)
            return result.stdout.strip()
        except Exception as e:
            print(f"ADB Error: {e}")
            return ""
    
    def tap(self, x, y, wait_after=1):
        """Tap on coordinates"""
        self.run_command(f"input tap {x} {y}", wait_after)
        
    def swipe(self, x1, y1, x2, y2, duration=300, wait_after=1):
        """Swipe gesture"""
        self.run_command(f"input swipe {x1} {y1} {x2} {y2} {duration}", wait_after)
        
    def input_text(self, text, wait_after=0.5):
        """Input text"""
        escaped = text.replace("'", "\\'").replace('"', '\\"')
        self.run_command(f"input text '{escaped}'", wait_after)
        
    def press_key(self, key, wait_after=0.5):
        """Press hardware key"""
        self.run_command(f"input keyevent {key}", wait_after)
        
    def screenshot(self, name="screen"):
        """Take screenshot and pull to PC"""
        timestamp = datetime.now().strftime("%H%M%S")
        filename = f"{name}_{timestamp}.png"
        local_path = self.screenshot_dir / filename
        
        self.run_command("screencap -p /sdcard/screenshot.png", 0.5)
        subprocess.run([self.adb_path, "-s", self.device_id, 
                       "pull", "/sdcard/screenshot.png", str(local_path)], 
                      capture_output=True)
        return local_path
    
    def get_ui_dump(self):
        """Get UI hierarchy dump for element detection"""
        self.run_command("uiautomator dump /sdcard/ui_dump.xml", 1)
        result = subprocess.run([self.adb_path, "-s", self.device_id, 
                                "shell", "cat", "/sdcard/ui_dump.xml"],
                               capture_output=True, text=True)
        return result.stdout
    
    def find_element_by_text(self, text, dump=None):
        """Find element by text content"""
        if dump is None:
            dump = self.get_ui_dump()
        
        # Parse XML and search for text
        try:
            root = ET.fromstring(dump)
            for elem in root.iter('node'):
                if elem.get('text', '').lower() == text.lower():
                    bounds = elem.get('bounds', '')
                    match = re.findall(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
                    if match:
                        x1, y1, x2, y2 = map(int, match[0])
                        return ((x1 + x2) // 2, (y1 + y2) // 2)
        except:
            pass
        return None
    
    def find_element_by_resource(self, resource_id, dump=None):
        """Find element by resource ID"""
        if dump is None:
            dump = self.get_ui_dump()
        
        try:
            root = ET.fromstring(dump)
            for elem in root.iter('node'):
                if resource_id in elem.get('resource-id', ''):
                    bounds = elem.get('bounds', '')
                    match = re.findall(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
                    if match:
                        x1, y1, x2, y2 = map(int, match[0])
                        return ((x1 + x2) // 2, (y1 + y2) // 2)
        except:
            pass
        return None
    
    def wait_and_tap(self, text, timeout=10):
        """Wait for element and tap it"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            pos = self.find_element_by_text(text)
            if pos:
                self.tap(pos[0], pos[1])
                return True
            time.sleep(0.5)
        return False


class FacebookReelUploader:
    def __init__(self):
        self.adb = ADBController()
        self.caption_file = Path("D:\\git hub all repo\\ld-player\\caption.txt")
        
    def log(self, message, level="INFO"):
        """Log message with timestamp"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        colors = {
            "INFO": "\033[37m",
            "SUCCESS": "\033[32m",
            "WARN": "\033[33m",
            "ERROR": "\033[31m"
        }
        print(f"{colors.get(level, '')}[{timestamp}] [{level}] {message}\033[0m")
        
    def read_caption(self):
        """Read caption from file"""
        if self.caption_file.exists():
            caption = self.caption_file.read_text().strip()
            self.log(f"Caption loaded: {caption}", "SUCCESS")
            return caption
        else:
            self.log("Caption file not found, using default", "WARN")
            return "Check out this video! #viral #trending #reels"
    
    def verify_device(self):
        """Verify device connection"""
        result = subprocess.run([self.adb.adb_path, "devices"], 
                              capture_output=True, text=True)
        if self.adb.device_id in result.stdout:
            self.log("Device connected", "SUCCESS")
            return True
        self.log("Device not connected", "ERROR")
        return False
    
    def navigate_to_facebook(self):
        """Navigate to Facebook and prepare for upload"""
        self.log("Opening Facebook...")
        self.adb.run_command("am start -n com.facebook.katana/com.facebook.katana.LoginActivity", 3)
        
        # Wait for Facebook to load
        time.sleep(3)
        
        # Take screenshot to verify
        self.adb.screenshot("facebook_open")
        
    def go_to_profile(self):
        """Navigate to profile tab"""
        self.log("Going to profile...")
        # Profile tab is typically at bottom right
        self.adb.tap(980, 1840, 2)
        
    def go_to_reels_tab(self):
        """Click on Reels tab"""
        self.log("Clicking Reels tab...")
        self.adb.tap(500, 120, 1.5)
        
    def click_create_reel(self):
        """Click Create Reel button"""
        self.log("Clicking Create Reel...")
        self.adb.tap(540, 1620, 3)
        
    def select_video(self):
        """Select video from gallery"""
        self.log("Selecting video...")
        # Try to find video in gallery
        self.adb.tap(180, 600, 2)
        
    def handle_audience_screens(self):
        """Handle audience/review screens dynamically"""
        self.log("Handling audience screens...")
        
        for attempt in range(5):
            # Try to find and click Continue/Done buttons
            dump = self.adb.get_ui_dump()
            
            # Look for Continue button
            pos = self.adb.find_element_by_text("Continue", dump)
            if pos:
                self.log(f"Found Continue button at {pos}")
                self.adb.tap(pos[0], pos[1], 1.5)
                continue
            
            # Look for Done button
            pos = self.adb.find_element_by_text("Done", dump)
            if pos:
                self.log(f"Found Done button at {pos}")
                self.adb.tap(pos[0], pos[1], 1.5)
                continue
            
            # Look for Next button
            pos = self.adb.find_element_by_text("Next", dump)
            if pos:
                self.log(f"Found Next button at {pos}")
                self.adb.tap(pos[0], pos[1], 1.5)
                continue
            
            time.sleep(1)
    
    def enter_caption(self, caption):
        """Enter caption text"""
        self.log("Entering caption...")
        
        # Click on description field
        dump = self.adb.get_ui_dump()
        pos = self.adb.find_element_by_text("Describe your reel", dump)
        if pos:
            self.adb.tap(pos[0], pos[1], 1)
        else:
            # Fallback to approximate coordinates
            self.adb.tap(600, 300, 1)
        
        # Clear existing text
        self.adb.press_key("KEYCODE_MOVE_END", 0.3)
        for _ in range(20):
            self.adb.press_key("KEYCODE_DEL", 0.05)
        
        # Enter new caption
        self.adb.input_text(caption, 1)
        
    def set_audience_public(self):
        """Set audience to Public for more reach"""
        self.log("Setting audience to Public...")
        
        # Click on "Who can see this?"
        self.adb.tap(540, 780, 1)
        
        # Select Public
        self.adb.tap(540, 1200, 1)
        
        # Click Done
        self.adb.tap(540, 1840, 1)
    
    def share_reel(self):
        """Click Share now button"""
        self.log("Sharing reel...")
        
        # Try to find Share button
        dump = self.adb.get_ui_dump()
        pos = self.adb.find_element_by_text("Share now", dump)
        if pos:
            self.adb.tap(pos[0], pos[1], 5)
        else:
            # Fallback
            self.adb.tap(540, 1800, 5)
    
    def upload(self):
        """Main upload process"""
        try:
            self.log("=== Facebook Reel Uploader Started ===", "SUCCESS")
            
            # Step 1: Read caption
            caption = self.read_caption()
            
            # Step 2: Verify device
            if not self.verify_device():
                return False
            
            # Step 3: Navigate to Facebook
            self.navigate_to_facebook()
            
            # Step 4: Go to profile
            self.go_to_profile()
            
            # Step 5: Go to Reels tab
            self.go_to_reels_tab()
            
            # Step 6: Click Create Reel
            self.click_create_reel()
            
            # Step 7: Select video
            self.select_video()
            
            # Step 8: Handle audience screens
            self.handle_audience_screens()
            
            # Step 9: Enter caption
            self.enter_caption(caption)
            
            # Step 10: Set audience (optional)
            # self.set_audience_public()
            
            # Step 11: Share reel
            self.share_reel()
            
            # Final verification
            time.sleep(3)
            self.adb.screenshot("upload_complete")
            
            self.log("=== Reel Upload Complete ===", "SUCCESS")
            self.log(f"Caption: {caption}")
            return True
            
        except Exception as e:
            self.log(f"Error: {e}", "ERROR")
            self.adb.screenshot("error")
            return False


if __name__ == "__main__":
    uploader = FacebookReelUploader()
    success = uploader.upload()
    sys.exit(0 if success else 1)
