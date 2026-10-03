@echo off
set "ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%ROOT%runtime\collector\BTC_LRA_COLLECTOR.pid'; if (Test-Path -LiteralPath $p) { $id=[int](Get-Content -LiteralPath $p); try { Stop-Process -Id $id -ErrorAction Stop; Write-Output ('STOPPED pid='+$id) } catch { Write-Output 'STOPPED (stale PID)' } } else { Write-Output 'STOPPED' }"
exit /b 0
