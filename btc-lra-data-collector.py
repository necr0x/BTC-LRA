"""Standalone bounded BTCUSDT Futures telemetry collector."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PANAMA = timezone(timedelta(hours=-5))
SYMBOL = 'BTCUSDT'
MAX_HEALTH_BYTES = 4 * 1024 * 1024


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_local(value: datetime) -> str:
    return value.astimezone(PANAMA).isoformat()


def api_get(path: str, params: dict[str, object]) -> object:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f'https://fapi.binance.com{path}?{query}',
        headers={'User-Agent': 'BTC-LRA-data-collector/1.0'},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode('utf-8'))


def parse_timestamp(payload: dict[str, object]) -> datetime | None:
    for key in ('timestamp_utc', 'timestamp_local', 'timestamp'):
        value = payload.get(key)
        if value:
            try:
                return datetime.fromisoformat(str(value)).astimezone(timezone.utc)
            except ValueError:
                continue
    return None


def read_jsonl(path: Path, hours: float) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    cutoff = now_utc() - timedelta(hours=hours)
    rows: dict[str, dict[str, object]] = {}
    try:
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    timestamp = parse_timestamp(row)
                    if timestamp is not None and timestamp >= cutoff:
                        rows[timestamp.isoformat()] = row
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
    except OSError:
        return []
    return [rows[key] for key in sorted(rows)]


def atomic_write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix=f'{path.stem}.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def append_jsonl(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')


class Collector:
    def __init__(self, root: Path, history_hours: float, poll_seconds: float, depth_limit: int) -> None:
        self.root = root
        self.history_hours = max(24.0, history_hours)
        self.poll_seconds = max(1.0, poll_seconds)
        self.depth_limit = depth_limit
        self.directory = root / 'runtime' / 'collector'
        self.depth_directory = self.directory / 'depth'
        self.oi_path = self.directory / 'BTC_LRA_OI_RAW.jsonl'
        self.market_path = self.directory / 'BTC_LRA_MARKET_RAW.jsonl'
        self.depth_summary_path = self.directory / 'BTC_LRA_DEPTH_SUMMARY.jsonl'
        legacy_oi_path = root / 'runtime' / 'monitor' / 'BTC_LRA_MONITOR_RAW_OI_12H.jsonl'
        legacy_market_path = self.directory / 'BTC_LRA_MARKET_12H.jsonl'
        legacy_depth_summary_path = self.directory / 'BTC_LRA_DEPTH_SUMMARY_12H.jsonl'
        self.health_path = self.directory / 'BTC_LRA_COLLECTOR.log'
        self.pid_path = self.directory / 'BTC_LRA_COLLECTOR.pid'
        current_oi_rows = read_jsonl(self.oi_path, history_hours)
        legacy_oi_rows = read_jsonl(legacy_oi_path, history_hours)
        if legacy_oi_path.is_file():
            if legacy_oi_rows:
                self.oi_rows, legacy_used, legacy_overlap_skipped = self._splice_oi_rows(
                    current_oi_rows, legacy_oi_rows, history_hours,
                )
                atomic_write_jsonl(self.oi_path, self.oi_rows)
                oldest = parse_timestamp(self.oi_rows[0]) if self.oi_rows else None
                newest = parse_timestamp(self.oi_rows[-1]) if self.oi_rows else None
                span = ((newest - oldest).total_seconds() / 3600.0) if oldest and newest else 0.0
                self.log(
                    f'LEGACY_OI_BOOTSTRAP legacy_total={len(legacy_oi_rows)} '
                    f'legacy_used={legacy_used} legacy_overlap_skipped={legacy_overlap_skipped} '
                    f'collector_existing={len(current_oi_rows)} combined={len(self.oi_rows)} '
                    f'oldest={oldest.isoformat() if oldest else "--"} '
                    f'newest={newest.isoformat() if newest else "--"} span_hours={span:.2f}'
                )
            else:
                self.oi_rows = current_oi_rows
                self.log('LEGACY_OI_BOOTSTRAP_FAILED reason=no_valid_samples')
        else:
            self.oi_rows = current_oi_rows
        self.market_rows = read_jsonl(self.market_path if self.market_path.is_file() else legacy_market_path, history_hours)
        self.depth_summary_rows = read_jsonl(self.depth_summary_path if self.depth_summary_path.is_file() else legacy_depth_summary_path, history_hours)
        self.previous_oi = float(self.oi_rows[-1]['oi_btc']) if self.oi_rows and self.oi_rows[-1].get('oi_btc') is not None else None
        self.previous_oi_time = parse_timestamp(self.oi_rows[-1]) if self.oi_rows else None
        self.stop_requested = False
        self.last_heartbeat = 0.0
        self.last_compact = 0.0

    @staticmethod
    def _splice_oi_rows(current: list[dict[str, object]], legacy: list[dict[str, object]],
                        history_hours: float) -> tuple[list[dict[str, object]], int, int]:
        cutoff = now_utc() - timedelta(hours=history_hours)
        current_times = [parse_timestamp(row) for row in current]
        current_times = [stamp for stamp in current_times if stamp is not None]
        collector_first = min(current_times) if current_times else None
        legacy_before = [
            row for row in legacy
            if (timestamp := parse_timestamp(row)) is not None
            and (collector_first is None or timestamp < collector_first)
        ]
        overlap_skipped = len(legacy) - len(legacy_before) if collector_first is not None else 0
        source_rows = [*legacy_before, *current]
        merged: dict[str, tuple[datetime, dict[str, object]]] = {}
        for row in source_rows:
            timestamp = parse_timestamp(row)
            if timestamp is None or timestamp < cutoff:
                continue
            oi = row.get('oi_btc')
            if oi is None:
                continue
            merged[timestamp.isoformat()] = (timestamp, {'oi_btc': float(oi)})
        ordered = [merged[key] for key in sorted(merged)]
        rebuilt: list[dict[str, object]] = []
        previous_timestamp: datetime | None = None
        previous_oi: float | None = None
        for timestamp, values in ordered:
            dt_sec = (timestamp - previous_timestamp).total_seconds() if previous_timestamp else None
            doi = float(values['oi_btc']) - previous_oi if previous_oi is not None else None
            rebuilt.append({
                'timestamp_local': iso_local(timestamp),
                'timestamp_utc': timestamp.astimezone(timezone.utc).isoformat(),
                'timestamp': iso_local(timestamp),
                'server_time_utc': timestamp.astimezone(timezone.utc).isoformat(),
                'oi_btc': float(values['oi_btc']),
                'dOI_btc': doi,
                'dt_sec': dt_sec,
                'gap_break': bool(dt_sec is not None and dt_sec > 15.0),
            })
            previous_timestamp = timestamp
            previous_oi = float(values['oi_btc'])
        return rebuilt, len(legacy_before), overlap_skipped

    def log(self, message: str) -> None:
        self.health_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.health_path.open('a', encoding='utf-8') as handle:
                handle.write(f'{datetime.now(PANAMA).isoformat()} {message}\n')
            if self.health_path.stat().st_size > MAX_HEALTH_BYTES:
                data = self.health_path.read_bytes()
                self.health_path.write_bytes(data[-MAX_HEALTH_BYTES // 2:])
        except OSError:
            pass

    def acquire_pid(self) -> bool:
        self.pid_path.parent.mkdir(parents=True, exist_ok=True)
        if self.pid_path.is_file():
            try:
                existing = int(self.pid_path.read_text(encoding='utf-8').strip())
                if self.process_exists(existing):
                    self.log(f'ALREADY_RUNNING pid={existing}')
                    return False
            except (OSError, ValueError):
                pass
            try:
                self.pid_path.unlink()
            except OSError:
                pass
        self.pid_path.write_text(str(os.getpid()), encoding='utf-8')
        return True

    @staticmethod
    def process_exists(pid: int) -> bool:
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

    def release_pid(self) -> None:
        try:
            if self.pid_path.read_text(encoding='utf-8').strip() == str(os.getpid()):
                self.pid_path.unlink()
        except (OSError, ValueError):
            pass

    def compact(self) -> None:
        cutoff = now_utc() - timedelta(hours=self.history_hours)
        self.oi_rows = [row for row in self.oi_rows if (parse_timestamp(row) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
        self.market_rows = [row for row in self.market_rows if (parse_timestamp(row) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
        self.depth_summary_rows = [row for row in self.depth_summary_rows if (parse_timestamp(row) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
        atomic_write_jsonl(self.oi_path, self.oi_rows)
        atomic_write_jsonl(self.market_path, self.market_rows)
        atomic_write_jsonl(self.depth_summary_path, self.depth_summary_rows)
        self.prune_depth_files(cutoff)
        self.last_compact = time.monotonic()
        self.log(f'COMPACT oi={len(self.oi_rows)} market={len(self.market_rows)} depth_summary={len(self.depth_summary_rows)}')

    def prune_depth_files(self, cutoff: datetime) -> None:
        if not self.depth_directory.is_dir():
            return
        cutoff_hour = cutoff.replace(minute=0, second=0, microsecond=0)
        for path in self.depth_directory.glob('BTC_LRA_DEPTH_*.jsonl'):
            try:
                stamp = datetime.strptime(path.stem.removeprefix('BTC_LRA_DEPTH_'), '%Y%m%d_%H').replace(tzinfo=PANAMA).astimezone(timezone.utc)
                if stamp + timedelta(hours=1) < cutoff_hour:
                    path.unlink()
            except (OSError, ValueError):
                continue

    def collect_oi(self, poll_time: datetime) -> None:
        payload = api_get('/fapi/v1/openInterest', {'symbol': SYMBOL})
        server_time = datetime.fromtimestamp(float(payload['time']) / 1000, tz=timezone.utc) if payload.get('time') else poll_time
        oi = float(payload['openInterest'])
        dt_sec = (server_time - self.previous_oi_time).total_seconds() if self.previous_oi_time else None
        doi = oi - self.previous_oi if self.previous_oi is not None else None
        gap_break = dt_sec is not None and dt_sec > 15.0
        row = {
            'timestamp_local': iso_local(poll_time), 'timestamp_utc': server_time.isoformat(),
            'timestamp': iso_local(poll_time), 'server_time_utc': server_time.isoformat(),
            'oi_btc': oi, 'dOI_btc': doi, 'dt_sec': dt_sec, 'gap_break': gap_break,
        }
        append_jsonl(self.oi_path, row)
        self.oi_rows.append(row)
        self.previous_oi, self.previous_oi_time = oi, server_time
        if gap_break:
            self.log(f'GAP dt_sec={dt_sec:.3f} timestamp={server_time.isoformat()}')

    def collect_market(self, poll_time: datetime) -> None:
        payload = api_get('/fapi/v1/klines', {'symbol': SYMBOL, 'interval': '1m', 'limit': 2})
        row = payload[-1]
        snapshot = {
            'timestamp_local': iso_local(poll_time), 'timestamp_utc': poll_time.isoformat(),
            'minute_open_time': datetime.fromtimestamp(float(row[0]) / 1000, tz=timezone.utc).astimezone(PANAMA).isoformat(),
            'open': float(row[1]), 'high': float(row[2]), 'low': float(row[3]), 'current_close': float(row[4]),
            'volume': float(row[5]), 'taker_buy_base': float(row[9]), 'taker_sell_base': float(row[5]) - float(row[9]),
            'quote_volume': float(row[7]), 'taker_buy_quote': float(row[10]),
            'taker_sell_quote': float(row[7]) - float(row[10]),
        }
        append_jsonl(self.market_path, snapshot)
        self.market_rows.append(snapshot)

    def collect_depth(self, poll_time: datetime) -> None:
        payload = api_get('/fapi/v1/depth', {'symbol': SYMBOL, 'limit': self.depth_limit})
        bids = [[float(level[0]), float(level[1])] for level in payload.get('bids', [])]
        asks = [[float(level[0]), float(level[1])] for level in payload.get('asks', [])]
        raw = {'timestamp_local': iso_local(poll_time), 'timestamp_utc': poll_time.isoformat(), 'lastUpdateId': payload.get('lastUpdateId'), 'bids': bids, 'asks': asks}
        hour_name = poll_time.astimezone(PANAMA).strftime('BTC_LRA_DEPTH_%Y%m%d_%H.jsonl')
        append_jsonl(self.depth_directory / hour_name, raw)
        if not bids or not asks:
            return
        best_bid, best_ask = bids[0][0], asks[0][0]
        mid = (best_bid + best_ask) / 2
        bid_qty = sum(level[1] for level in bids)
        ask_qty = sum(level[1] for level in asks)
        bid_notional = sum(price * qty for price, qty in bids)
        ask_notional = sum(price * qty for price, qty in asks)
        summary = {
            'timestamp_local': iso_local(poll_time), 'timestamp_utc': poll_time.isoformat(),
            'best_bid': best_bid, 'best_ask': best_ask, 'mid': mid,
            'spread_usdt': best_ask - best_bid, 'spread_bps': (best_ask - best_bid) / mid * 10000 if mid else None,
            'total_bid_qty_in_snapshot': bid_qty, 'total_ask_qty_in_snapshot': ask_qty,
            'total_bid_notional': bid_notional, 'total_ask_notional': ask_notional,
            'bid_ask_notional_ratio': bid_notional / ask_notional if ask_notional else None,
            'top_bid_level_qty': bids[0][1], 'top_ask_level_qty': asks[0][1],
            'largest_bid_level_price': max(bids, key=lambda level: level[1])[0],
            'largest_bid_level_qty': max(bids, key=lambda level: level[1])[1],
            'largest_ask_level_price': max(asks, key=lambda level: level[1])[0],
            'largest_ask_level_qty': max(asks, key=lambda level: level[1])[1],
        }
        append_jsonl(self.depth_summary_path, summary)
        self.depth_summary_rows.append(summary)

    def heartbeat(self) -> None:
        now = time.monotonic()
        if now - self.last_heartbeat < 300:
            return
        self.last_heartbeat = now
        latest_oi = parse_timestamp(self.oi_rows[-1]) if self.oi_rows else None
        latest_market = parse_timestamp(self.market_rows[-1]) if self.market_rows else None
        latest_depth = parse_timestamp(self.depth_summary_rows[-1]) if self.depth_summary_rows else None
        current = datetime.now(timezone.utc)
        self.log(
            f'HEARTBEAT time={iso_local(current)} oi_samples_24h={len(self.oi_rows)} '
            f'market_samples_24h={len(self.market_rows)} depth_snapshots_24h={len(self.depth_summary_rows)} '
            f'last_oi_age_sec={(current - latest_oi).total_seconds() if latest_oi else "--"} '
            f'last_market_age_sec={(current - latest_market).total_seconds() if latest_market else "--"} '
            f'last_depth_age_sec={(current - latest_depth).total_seconds() if latest_depth else "--"}'
        )

    def run(self) -> None:
        if not self.acquire_pid():
            return
        self.log(f'START pid={os.getpid()} poll_seconds={self.poll_seconds:g} depth_limit={self.depth_limit}')
        if self.oi_rows or self.market_rows or self.depth_summary_rows:
            self.log(f'RESTORE oi={len(self.oi_rows)} market={len(self.market_rows)} depth_summary={len(self.depth_summary_rows)}')
        self.compact()

        def stop(_signum: int, _frame: object) -> None:
            self.stop_requested = True

        signal.signal(signal.SIGINT, stop)
        if hasattr(signal, 'SIGTERM'):
            signal.signal(signal.SIGTERM, stop)
        next_tick = time.monotonic()
        try:
            while not self.stop_requested:
                poll_time = now_utc()
                try:
                    self.collect_oi(poll_time)
                except Exception as exc:
                    self.log(f'API ERROR endpoint=openInterest type={type(exc).__name__} error={exc}')
                try:
                    self.collect_market(poll_time)
                except Exception as exc:
                    self.log(f'API ERROR endpoint=klines type={type(exc).__name__} error={exc}')
                try:
                    self.collect_depth(poll_time)
                except Exception as exc:
                    self.log(f'DEPTH ERROR type={type(exc).__name__} error={exc}')
                self.heartbeat()
                if time.monotonic() - self.last_compact >= 15 * 60:
                    self.compact()
                next_tick += self.poll_seconds
                time.sleep(max(0.0, next_tick - time.monotonic()))
        finally:
            self.compact()
            self.log('STOP')
            self.release_pid()


def main() -> None:
    parser = argparse.ArgumentParser(description='Standalone BTC-LRA public Binance data collector')
    parser.add_argument('--poll-seconds', type=float, default=5.0)
    parser.add_argument('--history-hours', type=float, default=24.0)
    parser.add_argument('--depth-limit', type=int, choices=(50, 100, 500), default=100)
    args = parser.parse_args()
    Collector(Path(__file__).resolve().parent, args.history_hours, args.poll_seconds, args.depth_limit).run()


if __name__ == '__main__':
    main()
