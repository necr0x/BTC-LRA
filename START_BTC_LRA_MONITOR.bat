@echo off
set "ROOT=%~dp0"
set "PY="
if exist "%ROOT%.venv\Scripts\pythonw.exe" set "PY=%ROOT%.venv\Scripts\pythonw.exe"
if not defined PY if exist "%LOCALAPPDATA%\Python\pythoncore-3.14-64\pythonw.exe" set "PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\pythonw.exe"
if not defined PY for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY set "PY=pyw.exe"
set "SOUND_FILE=%ROOT%runtime\monitor\BTC_LRA_EVENT.wav"

if exist "%SOUND_FILE%" (
    start "" /b "%PY%" "%ROOT%btc-lra-oi-control-monitor.py" --mode live --from-now --poll-seconds 5 --sound on --sound-file "%SOUND_FILE%" --gui --log "%ROOT%data\research\BTC_LRA_LIVE_MONITOR.log"
) else if "%~1"=="" (
    start "" /b "%PY%" "%ROOT%btc-lra-oi-control-monitor.py" --mode live --from-now --poll-seconds 5 --sound on --gui --log "%ROOT%data\research\BTC_LRA_LIVE_MONITOR.log"
) else (
    start "" /b "%PY%" "%ROOT%btc-lra-oi-control-monitor.py" --mode live --from-now --poll-seconds 5 --sound on --sound-file "%~1" --gui --log "%ROOT%data\research\BTC_LRA_LIVE_MONITOR.log"
)
exit /b 0
