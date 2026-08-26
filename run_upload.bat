@echo off
echo ========================================
echo Facebook Reel Uploader - Advanced Script
echo ========================================
echo.

REM Check if caption.txt exists
if not exist "D:\git hub all repo\ld-player\caption.txt" (
    echo ERROR: caption.txt not found!
    echo Please create caption.txt with your reel caption.
    pause
    exit /b 1
)

REM Display caption
echo Caption to use:
type "D:\git hub all repo\ld-player\caption.txt"
echo.
echo.

REM Run the PowerShell script
echo Starting reel upload...
powershell -ExecutionPolicy Bypass -File "D:\git hub all repo\ld-player\upload_reel.ps1"

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ========================================
    echo Reel uploaded successfully!
    echo ========================================
) else (
    echo.
    echo ========================================
    echo Upload failed. Check logs above.
    echo ========================================
)

pause
