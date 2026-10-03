@echo off
setlocal
set "TASK=BTC_LRA_DATA_COLLECTOR"
schtasks /Delete /TN "%TASK%" /F >nul 2>&1
if errorlevel 1 (
    echo AUTOSTART REMOVE FAILED OR TASK NOT FOUND
    pause
    exit /b 1
)
echo AUTOSTART REMOVED
echo TASK=%TASK%
pause
endlocal
