@echo off
echo ==========================================
echo   Facebook Reel Uploader v2 (Smart UI)
echo ==========================================
echo.
echo Reading caption from caption.txt:
type "D:\git hub all repo\ld-player\caption.txt"
echo.
echo.
echo Starting upload...
echo.
python "D:\git hub all repo\ld-player\upload_reel_v2.py"
echo.
if %ERRORLEVEL% EQU 0 (
    echo ==========================================
    echo   SUCCESS! Reel uploaded.
    echo ==========================================
) else (
    echo ==========================================
    echo   FAILED! Check errors above.
    echo ==========================================
)
pause
