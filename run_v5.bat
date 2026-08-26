@echo off
color 0A
echo.
echo  ================================================
echo   Facebook Reel Uploader v5 - AUTO START
echo   - Starts LDPlayer automatically
echo   - Opens Facebook
echo   - Uploads reel with caption
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
python "D:\git hub all repo\ld-player\upload_reel_v5.py"
echo.
echo  ================================================
if %ERRORLEVEL% EQU 0 (
    echo   SUCCESS! Reel uploaded with caption.
) else (
    echo   FAILED! Check errors above.
)
echo  ================================================
pause
