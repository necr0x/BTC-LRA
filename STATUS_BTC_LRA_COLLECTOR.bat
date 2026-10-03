@echo off
set "ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%ROOT%runtime\collector\BTC_LRA_COLLECTOR.pid'; $h='%ROOT%runtime\collector\BTC_LRA_COLLECTOR.log'; if (Test-Path -LiteralPath $p) { $id=[int](Get-Content -LiteralPath $p); try { Get-Process -Id $id -ErrorAction Stop | Out-Null; Write-Output ('RUNNING pid='+$id) } catch { Write-Output 'STOPPED (stale PID)' } } else { Write-Output 'STOPPED' }; if (Test-Path -LiteralPath $h) { $last=Get-Content -LiteralPath $h -Tail 1; Write-Output ('LAST '+$last) }"
pause
exit /b 0
