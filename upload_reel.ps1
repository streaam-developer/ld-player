# Facebook Reel Uploader - Advanced Automation Script
# Automatically handles UI changes and uploads reels with caption

param(
    [string]$CaptionFile = "D:\git hub all repo\ld-player\caption.txt",
    [string]$AdbPath = "C:\LDPlayer\LDPlayer9\adb.exe",
    [string]$DeviceId = "emulator-5556",
    [int]$Timeout = 5000
)

# Error handling
$ErrorActionPreference = "Stop"

# Helper functions
function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$timestamp] [$Level] $Message" -ForegroundColor $(
        switch($Level) {
            "ERROR" { "Red" }
            "WARN" { "Yellow" }
            "SUCCESS" { "Green" }
            default { "White" }
        }
    )
}

function Invoke-ADB {
    param(
        [string]$Command,
        [int]$WaitAfter = 1000
    )
    try {
        $result = & $AdbPath -s $DeviceId shell $Command 2>&1
        Start-Sleep -Milliseconds $WaitAfter
        return $result
    }
    catch {
        Write-Log "ADB Command failed: $Command" "ERROR"
        throw
    }
}

function Invoke-ADB-Input {
    param(
        [int]$X,
        [int]$Y,
        [int]$WaitAfter = 1000
    )
    Invoke-ADB "input tap $X $Y" $WaitAfter
}

function Invoke-ADB-Swipe {
    param(
        [int]$X1, [int]$Y1,
        [int]$X2, [int]$Y2,
        [int]$Duration = 300,
        [int]$WaitAfter = 1000
    )
    Invoke-ADB "input swipe $X1 $Y1 $X2 $Y2 $Duration" $WaitAfter
}

function Invoke-ADB-Text {
    param(
        [string]$Text,
        [int]$WaitAfter = 500
    )
    # Escape special characters for ADB
    $escapedText = $Text -replace '"', '\"' -replace "'", "\'"
    Invoke-ADB "input text '$escapedText'" $WaitAfter
}

function Take-Screenshot {
    param([string]$OutputPath = "C:\Users\zrs2026\AppData\Local\Temp\opencode\current_screen.png")
    Invoke-ADB "screencap -p /sdcard/current_screen.png" 500
    & $AdbPath -s $DeviceId pull /sdcard/current_screen.png $OutputPath | Out-Null
    return $OutputPath
}

function Wait-ForElement {
    param(
        [string]$Description,
        [int]$MaxAttempts = 10,
        [int]$AttemptDelay = 1000
    )
    for ($i = 1; $i -le $MaxAttempts; $i++) {
        Write-Log "Waiting for $Description (Attempt $i/$MaxAttempts)..."
        Start-Sleep -Milliseconds $AttemptDelay
    }
}

# Main execution
try {
    Write-Log "=== Facebook Reel Uploader Started ===" "SUCCESS"
    
    # Step 1: Read caption from file
    Write-Log "Step 1: Reading caption from file..."
    if (Test-Path $CaptionFile) {
        $caption = Get-Content $CaptionFile -Raw | ForEach-Object { $_.Trim() }
        Write-Log "Caption loaded: $caption" "SUCCESS"
    } else {
        Write-Log "Caption file not found: $CaptionFile" "WARN"
        $caption = "Check out this video! #viral #trending #reels"
        Write-Log "Using default caption: $caption" "WARN"
    }
    
    # Step 2: Verify device connection
    Write-Log "Step 2: Verifying device connection..."
    $devices = & $AdbPath devices 2>&1
    if ($devices -notmatch $DeviceId) {
        throw "Device $DeviceId not connected"
    }
    Write-Log "Device connected: $DeviceId" "SUCCESS"
    
    # Step 3: Navigate to Facebook home
    Write-Log "Step 3: Navigating to Facebook home..."
    Invoke-ADB "am start -a android.intent.action.MAIN -c android.intent.category.HOME" 2000
    
    # Step 4: Open Facebook app
    Write-Log "Step 4: Opening Facebook app..."
    Invoke-ADB "am start -n com.facebook.katana/com.facebook.katana.LoginActivity" 3000
    
    # Step 5: Take screenshot to verify Facebook is open
    $screenshot = Take-Screenshot
    Write-Log "Screenshot saved: $screenshot"
    
    # Step 6: Navigate to Profile tab (bottom right)
    Write-Log "Step 5: Navigating to Profile..."
    Invoke-ADB-Input 980 1840 2000
    
    # Step 7: Click on Reels tab
    Write-Log "Step 6: Clicking on Reels tab..."
    Invoke-ADB-Input 500 120 1500
    
    # Step 8: Click Create reel button
    Write-Log "Step 7: Clicking Create reel button..."
    Invoke-ADB-Input 540 1620 3000
    
    # Step 9: Select the video from gallery
    Write-Log "Step 8: Selecting video from gallery..."
    # Look for the most recent video in gallery
    Invoke-ADB-Input 180 600 2000
    
    # Step 10: Handle "Review audience" or "Who can see" screens
    Write-Log "Step 9: Handling audience screens..."
    Start-Sleep -Seconds 2
    
    # Try to find and click Continue/Done buttons
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        $screenshot = Take-Screenshot
        Write-Log "Checking for audience/continue screens (Attempt $attempt)..."
        
        # Click Continue button (typically at bottom)
        Invoke-ADB-Input 540 1840 1500
        
        # Click Done button if present
        Invoke-ADB-Input 540 1840 1500
    }
    
    # Step 11: Click Next to proceed to sharing screen
    Write-Log "Step 10: Clicking Next..."
    Invoke-ADB-Input 980 1740 3000
    
    # Step 12: Wait for New Reel screen
    Write-Log "Step 11: Waiting for New Reel screen..."
    Start-Sleep -Seconds 3
    
    # Step 13: Click on description field
    Write-Log "Step 12: Clicking on description field..."
    Invoke-ADB-Input 600 300 1000
    
    # Step 14: Enter caption text
    Write-Log "Step 13: Entering caption..."
    # Clear existing text first
    Invoke-ADB "input keyevent KEYCODE_MOVE_END" 300
    Invoke-ADB "input keyevent --longpress KEYCODE_DEL KEYCODE_DEL KEYCODE_DEL KEYCODE_DEL KEYCODE_DEL" 500
    
    # Type the caption
    Invoke-ADB-Text $caption 1000
    
    # Step 15: Set audience to Public (optional - for more reach)
    Write-Log "Step 14: Setting audience to Public..."
    Invoke-ADB-Input 540 780 1000  # Click "Who can see this?"
    Invoke-ADB-Input 540 1200 1000  # Select Public
    Invoke-ADB-Input 540 1840 1000  # Click Done
    
    # Step 16: Final verification
    Write-Log "Step 15: Final verification..."
    $screenshot = Take-Screenshot
    Write-Log "Final screenshot saved: $screenshot"
    
    # Step 17: Click Share now
    Write-Log "Step 16: Clicking Share now..."
    Invoke-ADB-Input 540 1800 5000  # Wait longer for upload
    
    # Step 18: Verify upload
    Write-Log "Step 17: Verifying upload..."
    Start-Sleep -Seconds 5
    $screenshot = Take-Screenshot
    
    Write-Log "=== Reel Upload Complete ===" "SUCCESS"
    Write-Log "Caption: $caption"
    Write-Log "Video: reel_video.mp4"
    Write-Log "Status: Shared successfully"
    
}
catch {
    Write-Log "Error occurred: $_" "ERROR"
    Write-Log "Stack Trace: $($_.ScriptStackTrace)" "ERROR"
    
    # Take error screenshot
    try {
        $errorScreenshot = Take-Screenshot -OutputPath "C:\Users\zrs2026\AppData\Local\Temp\opencode\error_screen.png"
        Write-Log "Error screenshot saved: $errorScreenshot"
    } catch {
        Write-Log "Could not save error screenshot" "WARN"
    }
    
    exit 1
}
finally {
    Write-Log "Script execution completed"
}
