"""Small Windows watchdog for the standalone BTC-LRA data collector."""
from __future__ import annotations

import json
import os
import subprocess
import time
import ctypes
from datetime import datetime, timezone
from pathlib import Path

POLL_SECONDS = 45
STALE_SECONDS = 30
TASK_LOG_MAX_BYTES = 2 * 1024 * 1024


def log(root: Path, message: str) -> None:
    path = root / 'runtime' / 'collector' / 'BTC_LRA_COLLECTOR.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('a', encoding='utf-8') as handle:
            handle.write(f'{datetime.now().astimezone().isoformat()} {message}\n')
        if path.stat().st_size > TASK_LOG_MAX_BYTES:
            data = path.read_bytes()
            path.write_bytes(data[-TASK_LOG_MAX_BYTES // 2:])
    except OSError:
        pass


def read_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding='utf-8').strip())
        return value if value > 0 else None
    except (OSError, ValueError):
        return None


def process_exists(pid: int | None) -> bool:
    if pid is None:
        return False
    if os.name == 'nt':
        access = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
        handle = ctypes.windll.kernel32.OpenProcess(access, False, int(pid))
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def latest_sample_age(path: Path) -> float | None:
    if not path.is_file():
        return None
    try:
        with path.open('rb') as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 65536))
            lines = handle.read().decode('utf-8', errors='replace').splitlines()
        for line in reversed(lines):
            try:
                row = json.loads(line)
                value = row.get('timestamp_utc') or row.get('timestamp_local') or row.get('timestamp')
                if not value:
                    continue
                timestamp = datetime.fromisoformat(str(value)).astimezone(timezone.utc)
                return max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds())
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    except OSError:
        return None
    return None


def start_collector(root: Path, reason: str, age: float | None) -> None:
    start_bat = root / 'START_BTC_LRA_COLLECTOR.bat'
    try:
        subprocess.Popen(
            ['cmd.exe', '/c', str(start_bat)],
            cwd=str(root),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            close_fds=True,
        )
        log(root, f'WATCHDOG_RESTART reason={reason} last_sample_age={age if age is not None else "--"}')
    except (OSError, ValueError) as exc:
        log(root, f'WATCHDOG_ERROR action=start_collector type={type(exc).__name__} error={exc}')


def acquire_watchdog_pid(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            existing = int(path.read_text(encoding='utf-8').strip())
            if process_exists(existing):
                return False
        except (OSError, ValueError):
            pass
        try:
            path.unlink()
        except OSError:
            pass
    try:
        path.write_text(str(os.getpid()), encoding='utf-8')
        return True
    except OSError:
        return False


def release_watchdog_pid(path: Path) -> None:
    try:
        if path.read_text(encoding='utf-8').strip() == str(os.getpid()):
            path.unlink()
    except (OSError, ValueError):
        pass


def main() -> None:
    root = Path(__file__).resolve().parent
    pid_path = root / 'runtime' / 'collector' / 'BTC_LRA_COLLECTOR.pid'
    watchdog_pid_path = root / 'runtime' / 'collector' / 'BTC_LRA_COLLECTOR_WATCHDOG.pid'
    oi_path = root / 'runtime' / 'collector' / 'BTC_LRA_OI_RAW.jsonl'
    if not acquire_watchdog_pid(watchdog_pid_path):
        log(root, 'WATCHDOG_ALREADY_RUNNING')
        return
    log(root, 'WATCHDOG_START')
    try:
        while True:
            pid = read_pid(pid_path)
            age = latest_sample_age(oi_path)
            if process_exists(pid) and age is not None and age <= STALE_SECONDS:
                time.sleep(POLL_SECONDS)
                continue
            pid_alive = process_exists(pid)
            if pid_alive:
                # A live collector with stale data is usually experiencing a
                # network/API outage. Starting another collector would create
                # duplicate Binance polling and make the outage worse.
                log(root, f'WATCHDOG_STALE process_alive=YES pid={pid} last_sample_age={age if age is not None else "--"}')
            else:
                start_collector(root, 'pid_missing', age)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        return
    finally:
        release_watchdog_pid(watchdog_pid_path)


if __name__ == '__main__':
    main()
