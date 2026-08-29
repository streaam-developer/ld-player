@echo off
color 0A
echo.
echo  ================================================
echo   Facebook Reel Uploader v6 - FINAL
echo   - Deletes ALL media aggressively
echo   - Imports only 1.mp4
echo   - Forces gallery refresh
echo  ================================================
echo.
echo  Video file: 1.mp4
echo  -------------------------------
if exist "D:\git hub all repo\ld-player\1.mp4" (
    echo   [OK] 1.mp4 found
) else (
    echo   [ERROR] 1.mp4 NOT found!
    echo   Place 1.mp4 in: D:\git hub all repo\ld-player\
    pause
    exit /b 1
)
echo.
echo  Caption file:
type "D:\git hub all repo\ld-player\caption.txt"
echo.
echo  -------------------------------
echo.
echo  Starting in 3 seconds...
timeout /t 3 >nul
echo.
python "D:\git hub all repo\ld-player\upload_reel_v6.py"
echo.
echo  ================================================
if %ERRORLEVEL% EQU 0 (
    echo   SUCCESS! 1.mp4 uploaded with caption.
) else (
    echo   FAILED! Check errors above.
)
echo  ================================================
pause
