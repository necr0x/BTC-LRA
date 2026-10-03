@echo off
setlocal
set "ROOT=%~dp0"
set "TASK=BTC_LRA_DATA_COLLECTOR"
set "PY="
if exist "%ROOT%.venv\Scripts\pythonw.exe" set "PY=%ROOT%.venv\Scripts\pythonw.exe"
if not defined PY for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY set "PY=pyw.exe"

schtasks /Delete /TN "%TASK%" /F >nul 2>&1
schtasks /Create /TN "%TASK%" /SC ONLOGON /DELAY 0000:15 /RL LIMITED /F /TR "\"%PY%\" \"%ROOT%btc-lra-collector-watchdog.py\"" >nul
if errorlevel 1 (
    echo AUTOSTART INSTALL FAILED
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $s=New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -MultipleInstances IgnoreNew; Set-ScheduledTask -TaskName '%TASK%' -Settings $s | Out-Null"
if errorlevel 1 (
    echo AUTOSTART SETTINGS FAILED
    pause
    exit /b 1
)
schtasks /Run /TN "%TASK%" >nul 2>&1
if errorlevel 1 (
    echo AUTOSTART START FAILED
    pause
    exit /b 1
)
echo AUTOSTART INSTALLED
echo TASK=%TASK%
pause
endlocal
