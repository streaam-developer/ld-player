@echo off
color 0A
echo.
echo  ================================================
echo   Facebook Reel Uploader v4 - FINAL VERSION
echo   - Deletes old media
echo   - Imports random video
echo   - Handles "Uploading reel" blocking
echo  ================================================
echo.
echo  Caption file:
echo  -------------------------------
type "D:\git hub all repo\ld-player\caption.txt"
echo.
echo  -------------------------------
echo.
echo  Starting in 3 seconds...
timeout /t 3 >nul
echo.
python "D:\git hub all repo\ld-player\upload_reel_v4.py"
echo.
echo  ================================================
if %ERRORLEVEL% EQU 0 (
    echo   SUCCESS! Reel uploaded with caption.
) else (
    echo   FAILED! Check errors above.
)
echo  ================================================
pause
