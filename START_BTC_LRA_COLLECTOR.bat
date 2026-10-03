@echo off
set "ROOT=%~dp0"
set "PY="
if exist "%ROOT%.venv\Scripts\pythonw.exe" set "PY=%ROOT%.venv\Scripts\pythonw.exe"
if not defined PY for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY set "PY=pyw.exe"
start "" /b "%PY%" "%ROOT%btc-lra-data-collector.py" --poll-seconds 5 --history-hours 24 --depth-limit 100
exit /b 0
