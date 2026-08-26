@echo off
color 0A
echo.
echo  ================================================
echo   Facebook Reel Uploader v3 - ULTRA SMART
echo  ================================================
echo.
echo  Reading caption from caption.txt:
echo  -------------------------------
type "D:\git hub all repo\ld-player\caption.txt"
echo.
echo  -------------------------------
echo.
echo  Starting upload in 3 seconds...
timeout /t 3 >nul
echo.
python "D:\git hub all repo\ld-player\upload_reel_v3.py"
echo.
echo  ================================================
if %ERRORLEVEL% EQU 0 (
    echo   SUCCESS! Reel uploaded with caption.
) else (
    echo   FAILED! Check errors above.
)
echo  ================================================
pause
