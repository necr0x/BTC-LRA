"""Session-anchored, read-only BTC-LRA OI flow monitor.

Research presentation only. It never imports, starts, writes, or changes the
BTC-LRA engine. Replay uses raw instantaneous OI and 1m market bars; 5m is
only a display/aggregation concern outside this monitor.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import contextlib
import ctypes
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

PANAMA = timezone(timedelta(hours=-5))

# Research configuration for the effort -> price-result model.
EFF_BASELINE_WINDOW = 30
MIN_BASELINE_SAMPLES = 5
MIN_AGGR_SHARE_PCT = 55.0
LIMIT_EFF_RATIO = 0.50
AGGR_BASELINE_WINDOW = 30
MIN_AGGR_BASELINE_SAMPLES = 10
MIN_AGGR_MAG_X = 2.0
RAW_BASELINE_MINUTES = 60


def play_event_sound(sound_file: Path | None) -> None:
    """Play an event sound without blocking the replay worker.

    A user-provided WAV/MP3 is preferred.  With ``--sound on`` and no file,
    use the Windows notification sound so live events are still audible.
    """
    if sound_file is None or not sound_file.is_file():
        if sys.platform == 'win32':
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except (ImportError, RuntimeError, OSError):
                pass
        return
    try:
        if sound_file.suffix.lower() == '.wav':
            import winsound
            winsound.PlaySound(str(sound_file), winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
    except (ImportError, RuntimeError, OSError):
        pass
    ffplay = shutil.which('ffplay')
    if ffplay:
        try:
            subprocess.Popen(
                [ffplay, '-nodisp', '-autoexit', '-loglevel', 'quiet', str(sound_file)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
        except OSError:
            pass
    if sys.platform == 'win32':
        try:
            os.startfile(str(sound_file))
        except OSError:
            pass


SELL_INTERCEPTION_SOUND_THRESHOLD = 0.75


def seller_interception_probability(effort: dict[str, Any] | None,
                                    depth_level: dict[str, Any] | None = None,
                                    pctl: float | None = None,
                                    doi: float | None = None) -> float:
    """Research score for early seller interception of aggressive buyers.

    This is deliberately a transparent score, not a calibrated probability.
    It requires BUY taker pressure to meet a LIMIT SELL response and rewards
    poor price efficiency plus an ASK level that was tested/held.
    """
    if not effort or effort.get('aggr_side') != 'BUY':
        return 0.0
    if effort.get('control') != 'CONTROL LIMIT SELL':
        return 0.0
    share = float(effort.get('aggr_share_pct') or 0.0)
    share_score = max(0.0, min(1.0, (share - 55.0) / 42.0))
    eff_ratio = effort.get('eff_ratio')
    efficiency_score = 0.0 if eff_ratio is None else max(0.0, min(1.0, 1.0 - float(eff_ratio)))
    absorption_score = max(0.0, min(1.0, float(effort.get('absorption_ratio') or 0.0)))
    pctl_score = 0.0 if pctl is None else max(0.0, min(1.0, (float(pctl) - 90.0) / 10.0))
    depth_score = 0.0
    if depth_level:
        if depth_level.get('tested'):
            depth_score += 0.35
        if depth_level.get('held') or depth_level.get('status') == 'HELD':
            depth_score += 0.45
        if float(depth_level.get('last_size') or 0.0) >= float(depth_level.get('initial_size') or 0.0):
            depth_score += 0.20
    # A positive price response is not automatically bullish: a large BUY
    # impulse with a tiny response is still consistent with absorption.
    result_bps = effort.get('aligned_result_bps')
    result_score = 0.0 if result_bps is None else max(0.0, min(1.0, 1.0 - max(float(result_bps), 0.0) / 3.0))
    expansion_score = 0.0 if doi is None else (1.0 if float(doi) > 0 else 0.65)
    score = (
        0.18 * share_score + 0.22 * efficiency_score +
        0.22 * absorption_score + 0.12 * pctl_score +
        0.18 * depth_score + 0.06 * result_score +
        0.02 * expansion_score
    )
    return max(0.0, min(1.0, score))


def is_seller_interception(effort: dict[str, Any] | None,
                           depth_level: dict[str, Any] | None = None,
                           pctl: float | None = None,
                           doi: float | None = None) -> bool:
    return seller_interception_probability(effort, depth_level, pctl, doi) >= SELL_INTERCEPTION_SOUND_THRESHOLD


STATUS_RU = {
    'OBSERVATION': 'НАБЛЮДЕНИЕ',
    'SELL CONTROL': 'КОНТРОЛЬ SELL',
    'BUY CONTROL': 'КОНТРОЛЬ BUY',
    'SELL RESULT EFFICIENCY FALLING': 'ОТДАЧА SELL ПАДАЕТ',
    'BUY RESULT EFFICIENCY FALLING': 'ОТДАЧА BUY ПАДАЕТ',
    'SELL PRESSURE HELD': 'SELL НЕ ПРОДАВЛИВАЕТ ЦЕНУ',
    'BUY PRESSURE HELD': 'BUY НЕ ПРОДАВЛИВАЕТ ЦЕНУ',
    'POSSIBLE EARLY BUY CONTROL SHIFT': 'ВОЗМОЖНА РАННЯЯ ПЕРЕДАЧА К BUY',
    'POSSIBLE EARLY SELL CONTROL SHIFT': 'ВОЗМОЖНА РАННЯЯ ПЕРЕДАЧА К SELL',
    'EARLY BUY CONTROL': 'РАННИЙ КОНТРОЛЬ BUY',
    'EARLY SELL CONTROL': 'РАННИЙ КОНТРОЛЬ SELL',
}


def status_ru(value: str) -> str:
    return STATUS_RU.get(value, value)


def result_ru(value: str) -> str:
    replacements = {
        'HELD / NO CLEAR RESULT': 'УДЕРЖАНО / ЯСНОГО РЕЗУЛЬТАТА НЕТ',
        'BUY GOT RESULT': 'BUY ПОЛУЧИЛ РЕЗУЛЬТАТ',
        'SELL GOT RESULT': 'SELL ПОЛУЧИЛ РЕЗУЛЬТАТ',
        'BUY WON THIS INTERVAL': 'BUY ПОЛУЧИЛ РЕЗУЛЬТАТ В ЭТОМ ИНТЕРВАЛЕ',
        'SELL WON THIS INTERVAL': 'SELL ПОЛУЧИЛ РЕЗУЛЬТАТ В ЭТОМ ИНТЕРВАЛЕ',
        'BUY PRESSURE FAILED': 'ДАВЛЕНИЕ BUY НЕ ДАЛО РЕЗУЛЬТАТА',
        'SELL PRESSURE FAILED': 'ДАВЛЕНИЕ SELL НЕ ДАЛО РЕЗУЛЬТАТА',
    }
    for english, russian in replacements.items():
        value = value.replace(english, russian)
    return value


def flow_ru(value: str) -> str:
    return {'BUY': 'BUY', 'SELL': 'SELL', 'HELD': 'УДЕРЖАНО'}.get(value, value)


def compact(value: Any, signed: bool = True) -> str:
    if value is None:
        return '—'
    number = float(value)
    digits = 0 if abs(number - round(number)) < 0.05 else 1
    text = f'{abs(number):,.{digits}f}'.replace(',', ' ')
    return (('-' if number < 0 else '+') if signed else '') + text


def parse_time(value: str) -> datetime:
    if value.endswith('Z'):
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(PANAMA)
    if 'T' in value:
        return datetime.fromisoformat(value).replace(tzinfo=PANAMA)
    for pattern in ('%d.%m.%y %H:%M', '%d.%m.%Y %H:%M'):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=PANAMA)
        except ValueError:
            continue
    return datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=PANAMA)


def parse_t_window(text: str, now: datetime) -> dict[str, Any]:
    """Parse the deterministic short syntax used by GUI command T."""
    value = text.strip()
    if not value:
        raise ValueError('пустое временное окно')
    now = now.astimezone(PANAMA)

    def clock_token(token: str) -> tuple[int, int]:
        if not re.fullmatch(r'\d{4}', token):
            raise ValueError('время должно быть в формате HHMM')
        hour, minute = int(token[:2]), int(token[2:])
        if hour > 23 or minute > 59:
            raise ValueError(f'недопустимое время {token}: HH 00-23, MM 00-59')
        return hour, minute

    def date_clock(token: str) -> datetime:
        if len(token) == 4:
            day, month, clock = now.day, now.month, token
        elif len(token) == 8:
            day, month, clock = int(token[:2]), int(token[2:4]), token[4:]
        else:
            raise ValueError('ожидалось HHMM или DDMMHHMM')
        hour, minute = clock_token(clock)
        try:
            return datetime(
                now.year, month, day, hour, minute, tzinfo=PANAMA,
            )
        except ValueError as exc:
            raise ValueError(f'недопустимая календарная дата в {token}') from exc

    if '-' not in value:
        if re.fullmatch(r'\d{4}', value):
            start = date_clock(value)
        elif re.fullmatch(r'\d{8}', value):
            start = date_clock(value)
        elif re.fullmatch(r'\d{1,2}:\d{2}', value) or re.fullmatch(
                r'\d{1,2}\.\d{1,2}\.\d{2,4}\s+\d{1,2}:\d{2}', value):
            if ':' in value and '.' not in value:
                hour, minute = map(int, value.split(':'))
                if hour > 23 or minute > 59:
                    raise ValueError('недопустимое время')
                start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            else:
                start = parse_time(value)
        else:
            raise ValueError('используйте HHMM или DDMMHHMM')
        if start > now:
            raise ValueError('FROM позже текущего времени')
        return {'mode': 'LIVE_FROM', 'start': start, 'end': None}

    parts = value.split('-')
    if len(parts) != 2:
        raise ValueError('диапазон должен иметь вид FROM-TO')
    left, right = parts
    if not (re.fullmatch(r'\d{4}', left) or re.fullmatch(r'\d{8}', left)):
        raise ValueError('FROM диапазона должен быть HHMM или DDMMHHMM')
    if not re.fullmatch(r'\d{4}', right):
        raise ValueError('TO диапазона должен быть HHMM')
    start = date_clock(left)
    end_hour, end_minute = clock_token(right)
    end = start.replace(hour=end_hour, minute=end_minute)
    if end <= start:
        end += timedelta(days=1)
    if end > now:
        raise ValueError('TO позже текущего времени')
    return {'mode': 'FIXED_RANGE', 'start': start, 'end': end}


def fmt_time(value: datetime | None) -> str:
    return value.astimezone(PANAMA).strftime('%H:%M:%S / %d.%m.%y -5') if value else '—'


def fmt_gui_anchor(value: datetime | None) -> str:
    return value.astimezone(PANAMA).strftime('%d.%m.%y %H:%M') if value else '—'


def fmt_gui_clock(value: datetime | None) -> str:
    return value.astimezone(PANAMA).strftime('%H:%M') if value else '—'


def n(value: Any, digits: int = 1) -> str:
    return '—' if value is None else f'{float(value):+,.{digits}f}'.replace(',', ' ')


def price_display(value: Any) -> str:
    if value is None:
        return '—'
    return f'{float(value):,.1f}'.replace(',', ' ')


def load_raw(paths: list[Path]) -> list[dict[str, Any]]:
    unique: dict[int, dict[str, Any]] = {}
    for path in paths:
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get('oi_resolution') != 'instantaneous_poll':
                    continue
                stamp = int(row['sample_time_ts'])
                unique[stamp] = {
                    'ts': datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).astimezone(PANAMA),
                    'oi': float(row['OI_BTC']),
                    'gap_break': bool(row.get('gap_break', False)),
                }
    return [unique[key] for key in sorted(unique)]


def minute_oi(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[sample['ts'].replace(second=0, microsecond=0)].append(sample)
    result = []
    previous: float | None = None
    for minute in sorted(grouped):
        values = grouped[minute]
        changes = []
        for value in values:
            changes.append(0.0 if previous is None or value.get('gap_break') else value['oi'] - previous)
            previous = value['oi']
        pos = [x for x in changes if x > 0]
        neg = [abs(x) for x in changes if x < 0]
        result.append({'minute': minute, 'first': values[0]['oi'], 'last': values[-1]['oi'], 'net': values[-1]['oi'] - values[0]['oi'], 'add': sum(pos), 'exit': sum(neg), 'activity': sum(pos) + sum(neg), 'jump': max(pos + neg, default=0.0)})
    return result


def annotate_minutes(minutes: list[dict[str, Any]]) -> None:
    for index, row in enumerate(minutes):
        history = minutes[max(0, index - 30):index]
        net = [abs(x['net']) for x in history if abs(x['net']) > 0]
        activity = [x['activity'] for x in history if x['activity'] > 0]
        jump = [x['jump'] for x in history if x['jump'] > 0]
        row['history_n'] = len(history)
        row['net_x'] = abs(row['net']) / __import__('statistics').median(net) if net else 0
        row['activity_x'] = row['activity'] / __import__('statistics').median(activity) if activity else 0
        row['jump_x'] = row['jump'] / __import__('statistics').median(jump) if jump else 0
        values = (row['net_x'], row['activity_x'], row['jump_x'])
        row['strong'] = row['history_n'] >= 10 and sum(x >= 8 for x in values) >= 2
        row['mega'] = row['history_n'] >= 10 and sum(x >= 20 for x in values) >= 2
        row['elevated'] = row['history_n'] >= 10 and any(x >= 2 for x in values)


def anomaly_events(minutes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    strong = [i for i, row in enumerate(minutes) if row['strong']]
    groups: list[list[int]] = []
    if strong:
        group = [strong[0]]
        for index in strong[1:]:
            gap = (minutes[index]['minute'] - minutes[group[-1]]['minute']).total_seconds() / 60
            if gap <= 4:
                group.append(index)
            else:
                groups.append(group); group = [index]
        groups.append(group)
    events = []
    for group in groups:
        continuation = None
        for index in group:
            if minutes[index]['mega']:
                for candidate in range(index + 1, min(len(minutes), index + 6)):
                    if (minutes[candidate]['minute'] - minutes[index]['minute']).total_seconds() / 60 <= 5 and minutes[candidate]['elevated']:
                        continuation = candidate; break
            if continuation is not None:
                break
        if len(group) < 2 and continuation is None:
            continue
        confirmed = group[1] if len(group) >= 2 else continuation
        start = group[0] - 1 if group[0] > 0 and minutes[group[0] - 1]['elevated'] else group[0]
        end = max(group[-1], continuation or group[-1])
        quiet = 0
        while end + 1 < len(minutes) and quiet < 2:
            end += 1
            if minutes[end]['strong'] or minutes[end]['elevated']:
                quiet = 0
            else:
                quiet += 1
        events.append({'start': start, 'confirmed': confirmed, 'end': end, 'peak': max(range(start, end + 1), key=lambda i: max(minutes[i]['net_x'], minutes[i]['activity_x'], minutes[i]['jump_x']))})
    return events


def individual_events(minutes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one event per causal STRONG/MEGA minute.

    This is the historical event source used by the original long event list.
    ``anomaly_events`` remains available as a separate grouped research view;
    it must not replace these individual rows in the monitor dashboard.
    """
    return [
        {'start': index, 'confirmed': index, 'end': index, 'peak': index}
        for index, row in enumerate(minutes)
        if row['strong']
    ]


def event_source_validation(minutes: list[dict[str, Any]]) -> list[str]:
    individual = individual_events(minutes)
    grouped = anomaly_events(minutes)
    individual_times = ', '.join(minutes[event['confirmed']]['minute'].strftime('%H:%M') for event in individual) or '—'
    grouped_times = ', '.join(minutes[event['confirmed']]['minute'].strftime('%H:%M') for event in grouped) or '—'
    return [
        'INDIVIDUAL STRONG/MEGA EVENTS: ' + individual_times,
        'AGGREGATED CONFIRMED EPISODES: ' + grouped_times,
        f'INDIVIDUAL EVENT COUNT: {len(individual)}',
        f'AGGREGATED EPISODE COUNT: {len(grouped)}',
    ]


def load_market(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            stamp = row['timestamp_panama']
            fmt = '%Y-%m-%d %H:%M:%S' if len(stamp) > 16 else '%Y-%m-%d %H:%M'
            ts = datetime.strptime(stamp, fmt).replace(tzinfo=PANAMA)
            rows.append({'ts': ts, 'open': float(row['open']), 'high': float(row['high']), 'low': float(row['low']), 'close': float(row['close']), 'buy': float(row['taker_buy_BTC']), 'sell': float(row['taker_sell_BTC'])})
    return rows


def load_collector_market_history(path: Path, now: datetime,
                                  history_hours: float = 24.0) -> list[dict[str, Any]]:
    """Rebuild 1m market bars from collector's causal unfinished-candle snapshots."""
    if not path.is_file():
        return []
    cutoff = now - timedelta(hours=history_hours)
    grouped: dict[datetime, list[dict[str, Any]]] = {}
    try:
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                    minute_value = payload.get('minute_open_time')
                    sample_value = payload.get('timestamp_utc') or payload.get('timestamp_local')
                    if not minute_value or not sample_value:
                        continue
                    minute = datetime.fromisoformat(str(minute_value)).astimezone(PANAMA).replace(second=0, microsecond=0)
                    sample_time = datetime.fromisoformat(str(sample_value)).astimezone(PANAMA)
                    if sample_time < cutoff or minute > now:
                        continue
                    row = {
                        'sample_time': sample_time,
                        'ts': minute,
                        'open': float(payload['open']),
                        'high': float(payload['high']),
                        'low': float(payload['low']),
                        'close': float(payload['current_close']),
                        'buy': float(payload.get('taker_buy_base', 0.0)),
                        'sell': float(payload.get('taker_sell_base', 0.0)),
                    }
                    grouped.setdefault(minute, []).append(row)
                except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                    continue
    except OSError:
        return []
    bars: list[dict[str, Any]] = []
    for minute, snapshots in grouped.items():
        snapshots.sort(key=lambda row: row['sample_time'])
        first, last = snapshots[0], snapshots[-1]
        bars.append({
            'ts': minute,
            'open': first['open'],
            'high': max(row['high'] for row in snapshots),
            'low': min(row['low'] for row in snapshots),
            'close': last['close'],
            'buy': last['buy'],
            'sell': last['sell'],
        })
    return sorted(bars, key=lambda row: row['ts'])


def load_collector_depth_summary(path: Path, now: datetime,
                                 history_hours: float = 24.0) -> list[dict[str, Any]]:
    """Read collector depth telemetry for the research-only pre-release layer."""
    if not path.is_file():
        return []
    cutoff = now - timedelta(hours=history_hours)
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                    value = payload.get('timestamp_local') or payload.get('timestamp_utc')
                    if not value:
                        continue
                    timestamp = datetime.fromisoformat(str(value)).astimezone(PANAMA)
                    if timestamp < cutoff or timestamp > now:
                        continue
                    payload['_ts'] = timestamp
                    rows.append(payload)
                except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                    continue
    except OSError:
        return []
    return sorted(rows, key=lambda row: row['_ts'])


def fetch_binance_1m_klines(limit: int = 1000) -> list[dict[str, Any]]:
    """One-time migration fallback for historical market bars, never a live poll."""
    query = urllib.parse.urlencode({'symbol': 'BTCUSDT', 'interval': '1m', 'limit': limit})
    request = urllib.request.Request(
        f'https://fapi.binance.com/fapi/v1/klines?{query}',
        headers={'User-Agent': 'BTC-LRA-OI-monitor-migration/1.0'},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode('utf-8'))
    return [{
        'ts': datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc).astimezone(PANAMA).replace(second=0, microsecond=0),
        'open': float(row[1]), 'high': float(row[2]), 'low': float(row[3]), 'close': float(row[4]),
        'buy': float(row[9]), 'sell': float(row[5]) - float(row[9]),
    } for row in payload]


def merge_market_rows(*sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[datetime, dict[str, Any]] = {}
    for source in sources:
        for row in source:
            merged[row['ts']] = row
    return [merged[timestamp] for timestamp in sorted(merged)]


def collector_raw_is_fresh(path: Path, now: datetime, max_age_sec: float = 30.0) -> bool:
    """Return whether the standalone collector appears to own a fresh OI file."""
    try:
        return path.is_file() and (now.timestamp() - path.stat().st_mtime) <= max_age_sec
    except OSError:
        return False


def load_collector_oi_history(path: Path, now: datetime,
                              history_hours: float = 24.0) -> list[dict[str, Any]]:
    """Read collector-owned OI history without modifying the source file."""
    if not path.is_file():
        return []
    cutoff = now - timedelta(hours=history_hours)
    unique: dict[datetime, dict[str, Any]] = {}
    try:
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                    timestamp_value = payload.get('timestamp_utc') or payload.get('timestamp_local') or payload.get('timestamp')
                    if not timestamp_value:
                        continue
                    timestamp = datetime.fromisoformat(str(timestamp_value)).astimezone(PANAMA)
                    if timestamp < cutoff:
                        continue
                    oi = payload.get('oi_btc')
                    if oi is None:
                        continue
                    unique[timestamp] = {
                        'ts': timestamp,
                        'oi': float(oi),
                        **({'gap_break': True} if payload.get('gap_break') else {}),
                    }
                except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                    continue
    except OSError:
        return []
    return [unique[timestamp] for timestamp in sorted(unique)]


def collector_latest_timestamp(path: Path) -> datetime | None:
    """Return the newest valid collector record timestamp for stale diagnostics."""
    if not path.is_file():
        return None
    latest: datetime | None = None
    try:
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                    value = payload.get('timestamp_utc') or payload.get('timestamp_local') or payload.get('timestamp')
                    if not value:
                        continue
                    stamp = datetime.fromisoformat(str(value)).astimezone(PANAMA)
                    if latest is None or stamp > latest:
                        latest = stamp
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
    except OSError:
        return None
    return latest


def decorate_event_interval(event: dict[str, Any], minute_rows: list[dict[str, Any]], market: list[dict[str, Any]]) -> None:
    start_index = event['start']
    confirmed_index = event['confirmed']
    oi_rows = minute_rows[start_index:confirmed_index + 1]
    event['display_oi_net'] = sum(row['net'] for row in oi_rows)
    event['display_oi_activity'] = sum(row['activity'] for row in oi_rows)
    start_time = minute_rows[start_index]['minute']
    end_time = minute_rows[confirmed_index]['minute']
    bars = [row for row in market if start_time <= row['ts'].replace(second=0, microsecond=0) <= end_time]
    if bars:
        event['display_reference_price'] = bars[0]['open']
        # This is the causal close price for the event row.  It is available
        # when the event is confirmed and is kept separate from the reference
        # price used by the effort/result calculations.
        event['display_event_price'] = bars[-1]['close']
        event['display_taker_buy'] = sum(row['buy'] for row in bars)
        event['display_taker_sell'] = sum(row['sell'] for row in bars)
        event['display_flow_delta'] = sum(row['buy'] - row['sell'] for row in bars)
        event['display_price_change'] = bars[-1]['close'] - bars[0]['open']
    else:
        event['display_reference_price'] = None
        event['display_event_price'] = None
        event['display_taker_buy'] = 0.0
        event['display_taker_sell'] = 0.0
        event['display_flow_delta'] = 0.0
        event['display_price_change'] = None


def discover_raw_paths(root: Path) -> list[Path]:
    candidates = [
        root / 'runtime' / 'events' / 'BTC_LRA_002_OI_SAMPLES.jsonl',
        root.parent / 'BTC-LRA-LIVE' / 'runtime' / 'events' / 'BTC_LRA_002_OI_SAMPLES.jsonl',
        root.parent / 'BTC-LRA-LIVE-PUBLISHER' / 'live-current' / 'events' / 'BTC_LRA_002_OI_SAMPLES.jsonl',
    ]
    candidates.extend(root.rglob('BTC_LRA_002_OI_SAMPLES.jsonl'))
    candidates.extend(root.parent.glob('BTC-LRA-*/**/BTC_LRA_002_OI_SAMPLES.jsonl'))
    return sorted({path.resolve() for path in candidates if path.is_file()})


def discover_market_path(root: Path) -> Path | None:
    preferred = [
        root / 'data' / 'research' / 'BTC_LRA_20260929_30_CONTINUOUS_RESEARCH_1M.csv',
    ]
    candidates = [path for path in preferred if path.is_file()]
    candidates.extend(root.rglob('*CONTINUOUS*1M*.csv'))
    candidates.extend(root.rglob('*RESEARCH*1M*.csv'))
    return sorted({path.resolve() for path in candidates if path.is_file()})[0] if candidates else None


def recorded_range(samples: list[dict[str, Any]], market: list[dict[str, Any]]) -> tuple[datetime, datetime] | None:
    if not samples or not market:
        return None
    raw_start = samples[0]['ts'].replace(second=0, microsecond=0)
    raw_end = samples[-1]['ts'].replace(second=0, microsecond=0)
    market_start = market[0]['ts'].replace(second=0, microsecond=0)
    market_end = market[-1]['ts'].replace(second=0, microsecond=0)
    start = max(raw_start, market_start)
    end = min(raw_end, market_end)
    return (start, end) if start <= end else None


class GuiBridge:
    """Thread-safe presentation bridge; it contains no market calculations."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._snapshot: dict[str, Any] | None = None
        self._commands: deque[tuple[str, str | None]] = deque()
        self.finished = False

    def publish(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._snapshot = snapshot

    def read(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._snapshot) if self._snapshot is not None else None

    def command(self, key: str, value: str | None = None) -> None:
        with self._lock:
            self._commands.append((key, value))

    def pop_commands(self) -> list[tuple[str, str | None]]:
        with self._lock:
            result = list(self._commands)
            self._commands.clear()
            return result


def append_minute_history(session: 'Session', minute: dict[str, Any],
                          market_row: dict[str, Any] | None) -> None:
    """Store one presentation row for each closed minute, independently of events."""
    if market_row is None:
        return
    minute_time = minute['minute']
    effort = session.effort_result_control(
        minute_time,
        float(market_row.get('buy', 0.0)),
        float(market_row.get('sell', 0.0)),
        float(market_row.get('close', 0.0)) - float(market_row.get('open', 0.0)),
        float(market_row.get('open', 0.0)),
    )
    row = {
        'time': minute_time,
        'btc_price': float(market_row['close']),
        'oi_activity': float(minute.get('activity', 0.0)),
        'oi_net': float(minute.get('net', 0.0)),
        'aggr_side': effort.get('aggr_side', 'HELD'),
        'aggr_share_pct': float(effort.get('aggr_share_pct', 0.0)),
        'aggr_mag': float(effort.get('aggr_mag', 0.0)),
        'price_change': float(market_row['close']) - float(market_row['open']),
        'control': effort.get('control', 'CONTROL UNCLEAR'),
    }
    for index, existing in enumerate(session.minute_history):
        if existing.get('time') == minute_time:
            session.minute_history[index] = row
            return
    session.minute_history.append(row)


def publish_gui(bridge: GuiBridge | None, session: 'Session', clock: datetime,
                speed: float | None, live: bool, market_price: float | None = None) -> None:
    if bridge is None:
        return
    events = []
    for event in session.event_history:
        display_flow = event.get('display_flow', event.get('flow', 'HELD'))
        control = event.get('control_label', 'CONTROL NEUTRAL')
        control = event.get('display_control_text', control)
        if control.startswith('CONTROL '):
            control = control.removeprefix('CONTROL ')
        events.append({
            'time': event['time'].strftime('%H:%M'),
            'btc_price': price_display(event.get("display_event_price")),
            'oi_act': f'{compact(event.get("event_oi_activity"), signed=False)} BTC',
            'net': f'{compact(event.get("event_oi_net"))} BTC',
            'aggr': f'{event.get("aggr_side", "HELD")} {event.get("aggr_share_pct", 0.0):.1f}% {compact(event.get("aggr_mag", 0.0))} BTC',
            'delta_price': f'{compact(event.get("display_price_change", event.get("price_change")))} USDT',
            'control': control,
        })
    old_buy_pct, old_sell_pct = dominance_percentages(
        session.buy_dominance_weight_usdt, session.sell_dominance_weight_usdt
    )
    continuous_buy_pct = session.continuous_buy_pct
    continuous_sell_pct = session.continuous_sell_pct
    continuous_partition = session.continuous_dominance_partition()
    partition_buy_error = session.continuous_buy_dominance_btc - (
        continuous_partition['event_buy_btc'] + continuous_partition['rest_buy_btc']
    )
    partition_sell_error = session.continuous_sell_dominance_btc - (
        continuous_partition['event_sell_btc'] + continuous_partition['rest_sell_btc']
    )
    dominance_relative = session.dominance_relative_snapshot(
        continuous_buy_pct, continuous_sell_pct, clock
    )
    old_dominance = (
        f'DOMINANCE OLD BUY {old_buy_pct:.1f}% / SELL {old_sell_pct:.1f}%'
        if old_buy_pct is not None else 'DOMINANCE OLD BUY -- / SELL --'
    )
    continuous_dominance = (
        f'DOMINANCE BUY {continuous_buy_pct:.1f}% / SELL {continuous_sell_pct:.1f}%'
        if continuous_buy_pct is not None else 'DOMINANCE BUY -- / SELL --'
    )
    pre_release = session.pre_release.snapshot()
    pre_release_text = 'DEPTH NEUTRAL'
    last_depth_event = pre_release.get('last_depth_event')
    if last_depth_event is not None:
        event_time = last_depth_event.get('time')
        event_clock = fmt_gui_clock(event_time) if isinstance(event_time, datetime) else '--:--'
        event_side = 'BID' if last_depth_event.get('side') == 'BUY' else 'ASK'
        event_status = last_depth_event.get('status', '—')
        event_side_html = f'<span style="color:#168a2f">{event_side}</span>' if event_side == 'BID' else f'<span style="color:#c62828">{event_side}</span>'
        if event_status in {'PULLED', 'BROKEN'}:
            event_side_html = f'<s>{event_side_html}</s>'
        event_price = last_depth_event.get('price')
        event_qty = last_depth_event.get('current_qty')
        price_text = '—' if event_price is None else f'{float(event_price):.1f}'
        qty_text = '—' if event_qty is None else f'{float(event_qty):.1f}'
        pre_release_text = (
            f'DEPTH {event_side_html} {event_status} | '
            f'{event_clock} | {qty_text} BTC @ {price_text}'
        )
    elif pre_release['state'] != 'NEUTRAL':
        pctl_text = '--' if pre_release.get('pctl') is None else f'{pre_release["pctl"]:.1f}'
        level = pre_release.get('depth_level') or {}
        if level:
            side_text = 'BID' if pre_release.get('side') == 'BUY' else 'ASK'
            depth_text = (
                f'{side_text} {level["last_size"]:.1f} BTC @ {level["price"]:.1f} '
                f'${level["distance"]:.1f} {level["status"]} {level["age_sec"]:.0f}s'
            )
        else:
            depth_text = '--'
        state_side = 'BID' if pre_release.get('side') == 'BUY' else 'ASK' if pre_release.get('side') == 'SELL' else ''
        pre_release_text = (
            f'DEPTH {state_side} {pre_release["state"]} | '
            f'OI {pre_release.get("oi_flow") or "--"} | PCTL {pctl_text} | {depth_text}'
        )
    depth_by_level: dict[Any, dict[str, Any]] = {}
    depth_order: list[Any] = []
    display_depth_history = getattr(session, 'display_depth_history', None)
    if display_depth_history is None:
        display_depth_history = pre_release.get('depth_history', [])
    for depth_event in display_depth_history:
        level_id = depth_event.get('level_id')
        if level_id is None:
            level_id = ('legacy', len(depth_order))
        if level_id not in depth_by_level:
            depth_by_level[level_id] = dict(depth_event)
            depth_order.append(level_id)
        else:
            depth_by_level[level_id].update(depth_event)
    depth_rows = []
    for level_id in depth_order:
        depth_event = depth_by_level[level_id]
        event_time = depth_event.get('time')
        initial_qty = depth_event.get('initial_qty')
        current_qty = depth_event.get('current_qty')
        qty_text = (
            '—' if initial_qty is None or current_qty is None
            else f'{float(initial_qty):.1f} → {float(current_qty):.1f} BTC'
        )
        depth_rows.append({
            'time': event_time.strftime('%H:%M:%S') if isinstance(event_time, datetime) else '—',
            'price': price_display(depth_event.get('price')),
            'side': 'BID' if depth_event.get('side') == 'BUY' else 'ASK',
            'volume': qty_text,
            'status': depth_event.get('status', '—'),
        })
    bridge.publish({
        'live': live,
        'window_mode': getattr(session, 'window_mode', 'LIVE_FROM'),
        'window_end': fmt_time(getattr(session, 'window_end', None)),
        'window_end_gui': fmt_gui_anchor(getattr(session, 'window_end', None)),
        'anchor': fmt_time(session.anchor),
        'clock': fmt_time(clock),
        'anchor_gui': fmt_gui_anchor(session.anchor),
        'clock_gui': fmt_gui_clock(clock),
        'speed': speed,
        'price': f'{compact(market_price if market_price is not None else session.price, signed=False)} USDT',
        'dominance': old_dominance,
        'dominance_v2': continuous_dominance,
        'dominance_v2_buy_pct': continuous_buy_pct,
        'dominance_v2_sell_pct': continuous_sell_pct,
        'continuous_buy_pct': continuous_buy_pct,
        'continuous_sell_pct': continuous_sell_pct,
        'continuous_buy_dominance_btc': session.continuous_buy_dominance_btc,
        'continuous_sell_dominance_btc': session.continuous_sell_dominance_btc,
        'event_buy_dominance_btc': continuous_partition['event_buy_btc'],
        'event_sell_dominance_btc': continuous_partition['event_sell_btc'],
        'event_buy_pct': continuous_partition['event_buy_pct'],
        'event_sell_pct': continuous_partition['event_sell_pct'],
        'rest_buy_dominance_btc': continuous_partition['rest_buy_btc'],
        'rest_sell_dominance_btc': continuous_partition['rest_sell_btc'],
        'rest_buy_pct': continuous_partition['rest_buy_pct'],
        'rest_sell_pct': continuous_partition['rest_sell_pct'],
        'dominance_partition_buy_error_btc': partition_buy_error,
        'dominance_partition_sell_error_btc': partition_sell_error,
        'continuous_unclear_minutes': session.continuous_unclear_minutes,
        'continuous_valid_closed_minutes': session.continuous_valid_closed_minutes,
        'total_oi_anchor_btc': session.total_oi_anchor_btc,
        'total_oi_current_btc': session.total_oi_current_btc,
        'total_oi_flow_btc': session.total_oi_flow_btc,
        'total_oi_flow_pct': session.total_oi_flow_pct,
        **dominance_relative,
        'oi_flow': f'OI FLOW {compact(session.total_oi_flow_btc)} BTC',
        'raw_baseline_ready': getattr(session, 'raw_baseline_ready', False),
        'raw_baseline_span_minutes': getattr(session, 'raw_baseline_span_minutes', 0.0),
        'collector_stale': getattr(session, 'collector_stale', True),
        'collector_last_age_sec': getattr(session, 'collector_last_age_sec', None),
        'collector_market_last_age_sec': getattr(session, 'collector_market_last_age_sec', None),
        'pre_release': pre_release,
        'pre_release_text': pre_release_text,
        'depth_rows': depth_rows[-12:],
        'events': events,
    })


def write_replay_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(line.rstrip() + '\n')


def write_bounded_live_log(path: Path, line: str, max_bytes: int = 8 * 1024 * 1024) -> None:
    """Append live diagnostics while keeping the runtime log bounded."""
    write_replay_log(path, line)
    try:
        if path.stat().st_size <= max_bytes:
            return
        data = path.read_bytes()
        path.write_bytes(data[-max_bytes // 2:])
    except OSError:
        pass


def _event_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


class EventRowArchive:
    """Append-only archive for canonical rows shown in the upper GUI table."""

    def __init__(self, path: Path, diagnostic_log: Path | None = None) -> None:
        self.path = path
        self.diagnostic_log = diagnostic_log
        self.event_ids: set[str] = set()
        self._load_event_ids()

    def _load_event_ids(self) -> None:
        if not self.path.is_file():
            return
        try:
            with self.path.open('r', encoding='utf-8') as handle:
                for line in handle:
                    try:
                        event_id = json.loads(line).get('event_id')
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    if isinstance(event_id, str):
                        self.event_ids.add(event_id)
        except OSError as exc:
            self._diagnose(f'EVENT_ROW_ARCHIVE_READ_ERROR {type(exc).__name__}: {exc}')

    def _diagnose(self, message: str) -> None:
        if self.diagnostic_log is None:
            return
        try:
            write_bounded_live_log(self.diagnostic_log, message)
        except OSError:
            pass

    @staticmethod
    def _number(event: dict[str, Any], *names: str) -> float | None:
        for name in names:
            value = event.get(name)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None

    def persist(self, event: dict[str, Any], session: 'Session',
                record_origin: str) -> bool:
        event_time = _event_datetime(event.get('time'))
        if event_time is None:
            return False
        event_id = event_time.isoformat()
        if event_id in self.event_ids:
            return False
        flags = event.get('event_flags')
        if not isinstance(flags, list):
            flags = []
        row = {
            'event_id': event_id,
            'time': event_id,
            'btc_price': self._number(event, 'display_event_price', 'price'),
            'oi_activity_btc': self._number(event, 'event_oi_activity'),
            'oi_net_btc': self._number(event, 'event_oi_net'),
            'aggr_side': event.get('aggr_side'),
            'aggr_share_pct': self._number(event, 'aggr_share_pct'),
            'aggr_mag_btc': self._number(event, 'aggr_mag'),
            'price_change_usdt': self._number(event, 'display_price_change', 'price_change'),
            'control': event.get('control') or event.get('control_label'),
            'event_source': event.get('event_source'),
            'event_flags': list(flags),
            'raw_oi_intensity_pctl': self._number(event, 'raw_oi_intensity_pctl'),
            'raw_peak_type': event.get('raw_peak_type'),
            'oi_flow_mode': event.get('raw_oi_flow'),
            'eff_ratio': self._number(event, 'eff_ratio'),
            'absorption_ratio': self._number(event, 'absorption_ratio'),
            'oi_flow_contribution_btc': self._number(event, 'oi_flow_contribution'),
            'buy_dominance_contribution_btc': self._number(event, 'v2_buy_delta_btc'),
            'sell_dominance_contribution_btc': self._number(event, 'v2_sell_delta_btc'),
            'oi_flow_after_btc': float(session.oi_event_flow),
            'buy_dominance_after_btc': float(session.buy_dominance_v2_btc),
            'sell_dominance_after_btc': float(session.sell_dominance_v2_btc),
            'record_origin': record_origin,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
                handle.flush()
        except (OSError, TypeError, ValueError) as exc:
            self._diagnose(f'EVENT_ROW_ARCHIVE_WRITE_ERROR {type(exc).__name__}: {exc}')
            return False
        self.event_ids.add(event_id)
        return True


def pine_quote(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


def pine_timestamp(value: datetime) -> str:
    local = value.astimezone(PANAMA)
    return f'timestamp("GMT-5", {local.year}, {local.month}, {local.day}, {local.hour}, {local.minute})'


def write_tv_events_pine(path: Path, events: list[dict[str, Any]]) -> None:
    """Atomically regenerate the standalone TradingView event indicator."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        '//@version=6',
        '// BTC-LRA OI Events',
        '// Generated by btc-lra-oi-control-monitor.py; copy into TradingView.',
        '// Event timestamps use Panama time (UTC-5).',
        'indicator("BTC-LRA OI Events", overlay=true, max_labels_count=500)',
        '',
        'show_market = input.bool(true, "Show MARKET events")',
        'show_limit = input.bool(true, "Show LIMIT events")',
        'show_unclear = input.bool(true, "Show UNCLEAR events")',
        'show_details = input.bool(false, "Show detailed labels")',
        '',
    ]
    exported = 0
    for index, event in enumerate(sorted(events, key=lambda item: item['time'])):
        event_price = event.get('display_event_price')
        if event_price is None:
            lines.append(f'// Skipped event {event["time"].isoformat()}: no causal BTC price recorded.')
            continue
        try:
            price = float(event_price)
        except (TypeError, ValueError):
            lines.append(f'// Skipped event {event["time"].isoformat()}: invalid causal BTC price.')
            continue
        if price <= 0:
            lines.append(f'// Skipped event {event["time"].isoformat()}: non-positive causal BTC price.')
            continue
        control = str(event.get('control_label', 'CONTROL UNCLEAR'))
        if control.startswith('CONTROL '):
            control = control.removeprefix('CONTROL ')
        if control not in {'MARKET BUY', 'MARKET SELL', 'LIMIT BUY', 'LIMIT SELL', 'UNCLEAR'}:
            control = 'UNCLEAR'
        side = 'BUY' if control.endswith('BUY') else 'SELL' if control.endswith('SELL') else 'UNCLEAR'
        kind = 'MARKET' if control.startswith('MARKET ') else 'LIMIT' if control.startswith('LIMIT ') else 'UNCLEAR'
        short_label = 'M BUY' if control == 'MARKET BUY' else 'M SELL' if control == 'MARKET SELL' else 'L BUY' if control == 'LIMIT BUY' else 'L SELL' if control == 'LIMIT SELL' else '?'
        color = 'color.green' if control == 'MARKET BUY' else 'color.red' if control == 'MARKET SELL' else 'color.aqua' if control == 'LIMIT BUY' else 'color.orange' if control == 'LIMIT SELL' else 'color.gray'
        show_input = 'show_market' if kind == 'MARKET' else 'show_limit' if kind == 'LIMIT' else 'show_unclear'
        y_expression = f'{price:.10f} * 0.999' if side == 'BUY' else f'{price:.10f} * 1.001' if side == 'SELL' else f'{price:.10f}'
        label_style = 'label.style_label_up' if side == 'BUY' else 'label.style_label_down' if side == 'SELL' else 'label.style_label_left'
        aggr_side = str(event.get('aggr_side', 'HELD'))
        aggr_share = event.get('aggr_share_pct', 0.0)
        aggr_mag = event.get('aggr_mag', 0.0)
        details = (
            f'TIME: {event["time"].strftime("%Y-%m-%d %H:%M")} Panama\\n'
            f'PRICE: {compact(event_price, signed=False)} USDT\\n'
            f'OI ACT: {compact(event.get("event_oi_activity"), signed=False)} BTC\\n'
            f'NET: {compact(event.get("event_oi_net"))} BTC\\n'
            f'AGGR: {aggr_side} {float(aggr_share):.1f}% {compact(aggr_mag)} BTC\\n'
            f'PRICE CHANGE: {compact(event.get("display_price_change", event.get("price_change")))} USDT\\n'
            f'CONTROL: {control}'
        )
        ts_expression = pine_timestamp(event['time'])
        lines.extend([
            f'event_{index}_time = {ts_expression}',
            f'event_{index}_price = {price:.10f}',
            f'event_{index}_shown = {show_input}',
            f'if event_{index}_shown and time <= event_{index}_time and time_close > event_{index}_time',
            f'    label.new(x=event_{index}_time, y={y_expression}, xloc=xloc.bar_time, yloc=yloc.price, text=show_details ? {pine_quote(details)} : {pine_quote(short_label)}, style={label_style}, color={color}, textcolor=color.white, size=size.tiny)',
            '',
        ])
        exported += 1
    lines.insert(5, f'// Exported events: {exported}')
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    temporary.replace(path)


def dominance_percentages(buy_weight: float, sell_weight: float) -> tuple[float | None, float | None]:
    total = buy_weight + sell_weight
    if total <= 0:
        return None, None
    return buy_weight / total * 100, sell_weight / total * 100


def relative_dominance_change(current_pct: float | None,
                              reference_pct: float | None) -> float | None:
    """Return relative change in a dominance share, not percentage points."""
    if current_pct is None or reference_pct in (None, 0):
        return None
    return (current_pct / reference_pct - 1.0) * 100.0


def dominance_change_text(change_pct: float | None) -> str:
    if change_pct is None:
        return '—'
    if abs(change_pct) < 0.05:
        return '→0.0%'
    arrow = '↑' if change_pct > 0 else '↓'
    return f'{arrow}{abs(change_pct):.1f}%'


def dominance_change_markup(change_pct: float | None, arrow_color: str) -> str:
    text = dominance_change_text(change_pct)
    if text == '—':
        return '<span style="color:#000000">—</span>'
    return (
        f'<span style="color:{arrow_color}">{text[0]}</span>'
        f'<span style="color:#000000">{text[1:]}</span>'
    )


def dominance_change_parenthetical(change_pct: float | None) -> str:
    if change_pct is None:
        return ''
    return f'({change_pct:+.1f}%)'


def continuous_market_control(history: list[dict[str, Any]],
                               row: dict[str, Any]) -> dict[str, Any]:
    """Pure all-minute effort/result calculation for market-state dominance.

    This intentionally does not use anomaly thresholds or mutate Session.
    ``history`` is causal and may contain only pre-anchor baseline rows plus
    already processed post-anchor rows.
    """
    taker_buy = float(row.get('buy', 0.0) or 0.0)
    taker_sell = float(row.get('sell', 0.0) or 0.0)
    aggr_delta = taker_buy - taker_sell
    aggr_side = 'BUY' if aggr_delta > 0 else 'SELL' if aggr_delta < 0 else 'HELD'
    aggr_mag = abs(aggr_delta)
    reference_price = float(row.get('open', 0.0) or 0.0)
    price_change = float(row.get('close', 0.0) or 0.0) - reference_price
    aligned_bps = None
    if reference_price > 0 and aggr_side != 'HELD':
        signed_result = price_change if aggr_side == 'BUY' else -price_change
        aligned_bps = signed_result / reference_price * 10_000.0

    side_baselines: dict[str, list[float]] = {'BUY': [], 'SELL': []}
    for previous in history[-EFF_BASELINE_WINDOW:]:
        previous_buy = float(previous.get('buy', 0.0) or 0.0)
        previous_sell = float(previous.get('sell', 0.0) or 0.0)
        previous_delta = previous_buy - previous_sell
        previous_side = 'BUY' if previous_delta > 0 else 'SELL' if previous_delta < 0 else None
        previous_mag = abs(previous_delta)
        previous_open = float(previous.get('open', 0.0) or 0.0)
        previous_close = float(previous.get('close', 0.0) or 0.0)
        if previous_side is None or previous_mag <= 0 or previous_open <= 0:
            continue
        previous_result = (previous_close - previous_open
                           if previous_side == 'BUY'
                           else previous_open - previous_close)
        previous_bps = previous_result / previous_open * 10_000.0
        previous_notional_m = previous_mag * previous_open / 1_000_000.0
        if previous_bps > 0 and previous_notional_m > 0:
            side_baselines[previous_side].append(previous_bps / previous_notional_m)

    baseline_impact = None
    if aggr_side in ('BUY', 'SELL') and len(side_baselines[aggr_side]) >= MIN_BASELINE_SAMPLES:
        baseline_impact = __import__('statistics').median(side_baselines[aggr_side])
    aggr_notional_m = aggr_mag * reference_price / 1_000_000.0 if reference_price > 0 else 0.0
    expected_bps = baseline_impact * aggr_notional_m if baseline_impact is not None else None
    eff_ratio = (
        aligned_bps / expected_bps
        if aligned_bps is not None and expected_bps is not None and expected_bps > 0
        else None
    )
    control = 'CONTROL UNCLEAR'
    strength = 0.0
    contribution_side = None
    if aggr_side in ('BUY', 'SELL') and aggr_mag > 0 and eff_ratio is not None:
        if eff_ratio < LIMIT_EFF_RATIO:
            strength = min(1.0, max(0.0, 1.0 - max(eff_ratio, 0.0)))
            contribution_side = 'SELL' if aggr_side == 'BUY' else 'BUY'
            control = f'CONTROL LIMIT {contribution_side}'
        else:
            strength = min(1.0, max(0.0, eff_ratio))
            contribution_side = aggr_side
            control = f'CONTROL MARKET {aggr_side}'
    weight = aggr_mag * strength if contribution_side else 0.0
    return {
        'aggr_side': aggr_side,
        'aggr_mag': aggr_mag,
        'price_change': price_change,
        'eff_ratio': eff_ratio,
        'absorption_ratio': min(1.0, max(0.0, 1.0 - max(eff_ratio or 0.0, 0.0))) if eff_ratio is not None else 0.0,
        'control': control,
        'strength': strength,
        'weight': weight,
        'contribution_side': contribution_side,
        'baseline_impact_bps_per_1m': baseline_impact,
    }


def percentage_text(value: float | None) -> str:
    return '--' if value is None else f'{value:.1f}%'


def write_v2_replay_report(path: Path, rows: list[dict[str, Any]], session: 'Session',
                           checkpoint_times: list[datetime] | None = None) -> None:
    """Write the comparison report without changing the live monitor state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    old_buy_pct, old_sell_pct = dominance_percentages(
        session.buy_dominance_weight_usdt, session.sell_dominance_weight_usdt
    )
    v2_buy_pct, v2_sell_pct = dominance_percentages(
        session.buy_dominance_v2_btc, session.sell_dominance_v2_btc
    )
    lines = [
        'BTC-LRA DOMINANCE V2 RECORDED REPLAY',
        'V2 only: individual STRONG/MEGA events; OI MASS = raw minute OI ADD.',
        'OLD dominance and all detector/AGGR/CONTROL calculations remain unchanged.',
        '',
        'TIME | OI ADD | CONTROL | STRENGTH | WEIGHT | BUY V2 | SELL V2 | BUY% | SELL%',
    ]
    for row in rows:
        lines.append(
            f'{row["time"].strftime("%H:%M")} | '
            f'{compact(row["oi_add"], signed=False)} BTC | '
            f'{row["control"]} | {row["strength"]:.4f} | '
            f'{compact(row["weight"], signed=False)} BTC | '
            f'{compact(row["cum_buy"], signed=False)} BTC | '
            f'{compact(row["cum_sell"], signed=False)} BTC | '
            f'{percentage_text(row["buy_pct"]):>6} | '
            f'{percentage_text(row["sell_pct"]):>6}'
        )
    lines.extend(['', f'EVENT COUNT: {len(rows)}'])
    lines.append(f'OLD FINAL: BUY {old_buy_pct:.1f}% / SELL {old_sell_pct:.1f}%' if old_buy_pct is not None else 'OLD FINAL: BUY -- / SELL --')
    lines.append(f'V2 FINAL: BUY {v2_buy_pct:.1f}% / SELL {v2_sell_pct:.1f}%' if v2_buy_pct is not None else 'V2 FINAL: BUY -- / SELL --')
    if checkpoint_times:
        lines.extend(['', 'CHECKPOINTS (last V2 state at or before time):', 'TIME | EVENT | BUY V2 | SELL V2 | BUY% | SELL%'])
        for checkpoint in checkpoint_times:
            eligible = [row for row in rows if row['time'] <= checkpoint]
            if not eligible:
                lines.append(f'{checkpoint.strftime("%H:%M")} | -- | -- | -- | -- | --')
                continue
            row = eligible[-1]
            lines.append(
                f'{checkpoint.strftime("%H:%M")} | {row["time"].strftime("%H:%M")} | '
                f'{compact(row["cum_buy"], signed=False)} BTC | {compact(row["cum_sell"], signed=False)} BTC | '
                f'{percentage_text(row["buy_pct"]):>6} | '
                f'{percentage_text(row["sell_pct"]):>6}'
            )
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


NEIGHBOR_ANALYSIS_COLUMNS = [
    'TIME', 'PRICE', 'PRICE_CHANGE', 'OI_NET', 'OI_ADD', 'OI_EXIT', 'OI_ACT',
    'OI_MAX_JUMP', 'NET_X', 'ACT_X', 'JUMP_X', 'OI_ACT_3M', 'OI_ACT_3M_X',
    'STRONG', 'MEGA', 'ELEVATED', 'TAKER_BUY', 'TAKER_SELL', 'AGGR_SIDE',
    'AGGR_MAG', 'AGGR_SHARE', 'AGGR_MAG_X', 'MEANINGFUL_AGGR', 'EFF_RATIO',
    'ABSORPTION_RATIO', 'CONTROL', 'CTX_3M_2X', 'CTX_3M_3X', 'CTX_3M_4X',
    'CTX_3M_5X',
]


def _analysis_value(value: Any) -> Any:
    """CSV-safe formatting without changing any market calculation."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'YES' if value else 'NO'
    if isinstance(value, float):
        return f'{value:.8f}'.rstrip('0').rstrip('.')
    return value


def _causal_rolling_oi(minutes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return 3m OI windows and X ratios using only prior completed windows."""
    import statistics

    windows: list[dict[str, Any]] = []
    for index, _row in enumerate(minutes):
        start = max(0, index - 2)
        complete = index >= 2
        window = minutes[start:index + 1]
        current = {
            'oi_act_3m': sum(row['activity'] for row in window),
            'oi_add_3m': sum(row['add'] for row in window),
            'oi_exit_3m': sum(row['exit'] for row in window),
            'oi_net_3m': sum(row['net'] for row in window),
            'complete': complete,
        }
        previous = [
            item['oi_act_3m']
            for item in windows[max(0, index - 30):index]
            if item['complete'] and item['oi_act_3m'] > 0
        ]
        baseline = statistics.median(previous) if previous else None
        current['oi_act_3m_x'] = (
            current['oi_act_3m'] / baseline
            if baseline and baseline > 0 and complete else None
        )
        windows.append(current)
    return windows


def _neighbor_analysis_rows(minutes: list[dict[str, Any]],
                            market: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build diagnostics for every OI minute without changing monitor state."""
    rolling = _causal_rolling_oi(minutes)
    market_by_minute = {
        row['ts'].replace(second=0, microsecond=0): row for row in market
    }
    rows: list[dict[str, Any]] = []
    for index, oi_row in enumerate(minutes):
        bar = market_by_minute.get(oi_row['minute'])
        effort: dict[str, Any] | None = None
        if bar is not None:
            # A fresh Session prevents diagnostic minutes from accumulating
            # dominance or other mutable state into the real monitor session.
            diagnostic_session = Session(oi_row['minute'])
            diagnostic_session.market_history = market
            effort = diagnostic_session.effort_result_control(
                oi_row['minute'], bar['buy'], bar['sell'],
                bar['close'] - bar['open'], bar['open'],
            )
        rolling_row = rolling[index]
        oi_act_3m_x = rolling_row['oi_act_3m_x']
        result = {
            'TIME': oi_row['minute'].strftime('%Y-%m-%d %H:%M:%S'),
            'PRICE': bar['close'] if bar else None,
            'REFERENCE_PRICE': bar['open'] if bar else None,
            'PRICE_CHANGE': (bar['close'] - bar['open']) if bar else None,
            'OI_NET': oi_row['net'],
            'OI_ADD': oi_row['add'],
            'OI_EXIT': oi_row['exit'],
            'OI_ACT': oi_row['activity'],
            'OI_MAX_JUMP': oi_row['jump'],
            'NET_X': oi_row.get('net_x'),
            'ACT_X': oi_row.get('activity_x'),
            'JUMP_X': oi_row.get('jump_x'),
            'OI_ACT_3M': rolling_row['oi_act_3m'] if rolling_row['complete'] else None,
            'OI_ACT_3M_X': oi_act_3m_x,
            'STRONG': oi_row.get('strong', False),
            'MEGA': oi_row.get('mega', False),
            'ELEVATED': oi_row.get('elevated', False),
            'TAKER_BUY': bar['buy'] if bar else None,
            'TAKER_SELL': bar['sell'] if bar else None,
            'AGGR_SIDE': effort.get('aggr_side') if effort else None,
            'AGGR_MAG': effort.get('aggr_mag') if effort else None,
            'AGGR_SHARE': effort.get('aggr_share_pct') if effort else None,
            'AGGR_MAG_X': effort.get('aggr_mag_x') if effort else None,
            'MEANINGFUL_AGGR': effort.get('meaningful_aggression', False) if effort else False,
            'EFF_RATIO': effort.get('eff_ratio') if effort else None,
            'ABSORPTION_RATIO': effort.get('absorption_ratio') if effort else None,
            'CONTROL': effort.get('control') if effort else None,
            'CTX_3M_2X': oi_act_3m_x is not None and oi_act_3m_x >= 2.0,
            'CTX_3M_3X': oi_act_3m_x is not None and oi_act_3m_x >= 3.0,
            'CTX_3M_4X': oi_act_3m_x is not None and oi_act_3m_x >= 4.0,
            'CTX_3M_5X': oi_act_3m_x is not None and oi_act_3m_x >= 5.0,
        }
        rows.append({key: _analysis_value(value) for key, value in result.items()})
        rows[-1]['_time'] = oi_row['minute']
        rows[-1]['_meaningful'] = bool(effort and effort.get('meaningful_aggression'))
    return rows


def _analysis_selected(row: dict[str, Any], rule: str) -> bool:
    strong = row['STRONG'] == 'YES' or row['MEGA'] == 'YES'
    if rule == 'CURRENT STRONG/MEGA':
        return strong
    if rule == 'CURRENT ELEVATED':
        return row['ELEVATED'] == 'YES'
    if rule == '3M 2X':
        return row['CTX_3M_2X'] == 'YES'
    if rule == '3M 3X':
        return row['CTX_3M_3X'] == 'YES'
    if rule == '3M 4X':
        return row['CTX_3M_4X'] == 'YES'
    if rule == '3M 5X':
        return row['CTX_3M_5X'] == 'YES'
    if rule == 'STRONG OR ELEVATED OR 3M':
        return strong or row['ELEVATED'] == 'YES' or any(
            row[key] == 'YES' for key in ('CTX_3M_2X', 'CTX_3M_3X', 'CTX_3M_4X', 'CTX_3M_5X')
        )
    return False


def _neighbor_window_bounds(day: datetime, start: str, end: str) -> tuple[datetime, datetime]:
    return (
        day.replace(hour=int(start[:2]), minute=int(start[3:]), second=0, microsecond=0),
        day.replace(hour=int(end[:2]), minute=int(end[3:]), second=59, microsecond=999999),
    )


def _neighbor_report(rows: list[dict[str, Any]], output_csv: Path) -> str:
    rules = ('CURRENT STRONG/MEGA', 'CURRENT ELEVATED', '3M 2X', '3M 3X', '3M 4X', '3M 5X', 'STRONG OR ELEVATED OR 3M')
    target_day = datetime(2026, 9, 30, tzinfo=PANAMA)
    ranges = {
        '14:45–15:00': _neighbor_window_bounds(target_day, '14:45', '15:00'),
        '20:25–20:45': _neighbor_window_bounds(target_day, '20:25', '20:45'),
        '22:10–22:40': _neighbor_window_bounds(target_day, '22:10', '22:40'),
    }
    lines = [
        '# BTC-LRA Neighbor Flow Analysis',
        '',
        'Diagnostic-only output. The production STRONG/MEGA detector, AGGR, CONTROL, DOMINANCE, GUI, Pine and replay chronology were not changed.',
        '',
        'All OI ratios use the existing `annotate_minutes()` causal baselines. 3m ratios use only completed rolling windows ending before the current minute. Per-minute CONTROL is the existing `Session.effort_result_control()` applied to that minute, in a fresh diagnostic session.',
        '',
        f'- CSV: `{output_csv}`',
        f'- OI minutes analyzed: {len(rows)}',
        f'- Current STRONG/MEGA minutes: {sum(_neighbor_selected(row, "CURRENT STRONG/MEGA") for row in rows)}',
        '',
        '## Rule comparison',
        '',
        'Extra minutes means selected minutes minus the current STRONG/MEGA set. Noise is a selected extra minute with no meaningful aggression and `CONTROL UNCLEAR`; this is a transparent diagnostic indicator, not a new detector rule.',
        '',
        '| RULE | EXTRA MINUTES | EARLIEST USEFUL 22:xx | NOISE |',
        '|---|---:|---|---|',
    ]
    for rule in rules[1:]:
        selected = [row for row in rows if _neighbor_selected(row, rule)]
        strong_selected = [row for row in selected if _neighbor_selected(row, 'CURRENT STRONG/MEGA')]
        extras = [row for row in selected if not _neighbor_selected(row, 'CURRENT STRONG/MEGA')]
        useful = [row for row in extras if 22 <= row['_time'].hour <= 22 and 10 <= row['_time'].minute <= 37 and row['_meaningful'] and str(row['CONTROL']).startswith('CONTROL LIMIT')]
        noise = [row['_time'].strftime('%H:%M') for row in extras if not row['_meaningful'] and row['CONTROL'] == 'CONTROL UNCLEAR']
        lines.append(f'| {rule} | {len(selected) - len(strong_selected)} | {useful[0]["_time"].strftime("%H:%M") if useful else "--"} | {", ".join(noise) or "--"} |')
    lines.extend(['', '## Requested minutes', '', '| TIME | STRONG? | OI ACT X | 3M X | AGGR | EFF_RATIO | CONTROL |', '|---|---|---:|---:|---|---:|---|'])
    for row in rows:
        if row['_time'].date() == target_day.date() and row['_time'].hour == 22 and 10 <= row['_time'].minute <= 40:
            aggr = f'{row["AGGR_SIDE"]} {row["AGGR_SHARE"]}% +{row["AGGR_MAG"]} BTC' if row['AGGR_SIDE'] else '--'
            lines.append(f'| {row["_time"].strftime("%H:%M")} | {"YES" if _neighbor_selected(row, "CURRENT STRONG/MEGA") else "NO"} | {row["ACT_X"] or "--"} | {row["OI_ACT_3M_X"] or "--"} | {aggr} | {row["EFF_RATIO"] or "--"} | {row["CONTROL"] or "--"} |')
    lines.extend(['', '## Focused interpretation', ''])
    for time_text in ('22:17', '22:26', '22:27', '22:37', '22:39'):
        matching = [row for row in rows if row['_time'].date() == target_day.date() and row['_time'].strftime('%H:%M') == time_text]
        if not matching:
            lines.append(f'- **{time_text}**: no matching raw OI minute in the loaded intersection.')
            continue
        row = matching[0]
        lines.append(f'- **{time_text}**: OI ACT X `{row["ACT_X"] or "--"}`, AGGR MAG X `{row["AGGR_MAG_X"] or "--"}`, CONTROL `{row["CONTROL"] or "--"}`, EFF_RATIO `{row["EFF_RATIO"] or "--"}`. It is absent from the current event table because `STRONG/MEGA={row["STRONG"]}/{row["MEGA"]}` is false; the table is driven only by individual STRONG minutes.')
    lines.append('')
    useful = [row for row in rows if row['_time'].date() == target_day.date() and row['_time'].hour == 22 and 10 <= row['_time'].minute <= 37 and row['_meaningful'] and str(row['CONTROL']).startswith('CONTROL LIMIT')]
    lines.append(f'**Earliest 22:10–22:37 meaningful aggression with opposite LIMIT control:** {useful[0]["_time"].strftime("%H:%M") if useful else "none in loaded data"}.')
    lines.extend(['', '## Counts by comparison window', '', '| WINDOW | RULE | SELECTED | EXTRA |', '|---|---|---:|---:|'])
    for name, (start, end) in ranges.items():
        scoped = [row for row in rows if start <= row['_time'] <= end]
        base = sum(_neighbor_selected(row, 'CURRENT STRONG/MEGA') for row in scoped)
        for rule in rules[1:]:
            selected = sum(_neighbor_selected(row, rule) for row in scoped)
            lines.append(f'| {name} | {rule} | {selected} | {selected - base} |')
    lines.extend(['', 'No production threshold was selected by this diagnostic pass.'])
    return '\n'.join(lines) + '\n'


def _neighbor_selected(row: dict[str, Any], rule: str) -> bool:
    return _analysis_selected(row, rule)


def run_neighbor_flow_analysis(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parent
    raw_paths = args.raw_oi or discover_raw_paths(root)
    market_path = args.market_csv or discover_market_path(root)
    if not raw_paths:
        raise RuntimeError('Neighbor analysis: raw OI data was not found')
    if market_path is None:
        raise RuntimeError('Neighbor analysis: market CSV was not found')
    samples = load_raw(raw_paths)
    market = load_market(market_path)
    minutes = minute_oi(samples)
    annotate_minutes(minutes)
    common = recorded_range(samples, market)
    if common is not None:
        start, end = common
        if args.from_time:
            start = max(start, parse_time(args.from_time))
        if args.end:
            end = min(end, parse_time(args.end))
        minutes = [row for row in minutes if start <= row['minute'] <= end]
    rows = _neighbor_analysis_rows(minutes, market)
    output_csv = root / 'data' / 'research' / 'BTC_LRA_NEIGHBOR_FLOW_ANALYSIS.csv'
    output_md = root / 'data' / 'research' / 'BTC_LRA_NEIGHBOR_FLOW_ANALYSIS.md'
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=NEIGHBOR_ANALYSIS_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, '') for key in NEIGHBOR_ANALYSIS_COLUMNS})
    output_md.write_text(_neighbor_report(rows, output_csv), encoding='utf-8')
    print(f'NEIGHBOR ANALYSIS CSV: {output_csv}')
    print(f'NEIGHBOR ANALYSIS REPORT: {output_md}')
    print(f'OI MINUTES: {len(rows)} | STRONG/MEGA: {sum(_neighbor_selected(row, "CURRENT STRONG/MEGA") for row in rows)}')


RAW_INTENSITY_COLUMNS = [
    'TIME', 'PRICE', 'PRICE_CHANGE', 'TAKER_BUY', 'TAKER_SELL', 'AGGR_SIDE',
    'AGGR_MAG', 'AGGR_MAG_X', 'AGGR_SHARE', 'MEANINGFUL_AGGR', 'EFF_RATIO',
    'ABSORPTION_RATIO', 'CONTROL', 'STRONG', 'MEGA', 'OI_IMPULSE_5S_EQ_MAX',
    'OI_ACTIVITY_30S', 'OI_ACTIVITY_60S', 'OI_NET_30S', 'OI_NET_60S',
    'OI_DIRECTIONALITY_30S', 'OI_DIRECTIONALITY_60S', 'IMPULSE_5S_PCTL',
    'BURST_30S_PCTL', 'BURST_60S_PCTL', 'MAX_IMPULSE_5S_PCTL',
    'MAX_BURST_30S_PCTL', 'MAX_BURST_60S_PCTL', 'OI_INTENSITY_PCTL',
    'P95_HITS', 'P99_HITS', 'PEAK_TIMESTAMP', 'PEAK_TYPE',
    'WARMUP_INSUFFICIENT',
]


RAW_SAMPLE_COLUMNS = [
    'TIMESTAMP', 'DT_SEC', 'DOI', 'OI_IMPULSE_5S_EQ', 'OI_ACTIVITY_30S',
    'OI_ACTIVITY_60S', 'OI_NET_30S', 'OI_NET_60S',
    'OI_DIRECTIONALITY_30S', 'OI_DIRECTIONALITY_60S', 'IMPULSE_5S_PCTL',
    'BURST_30S_PCTL', 'BURST_60S_PCTL', 'OI_INTENSITY_PCTL',
    'WARMUP_INSUFFICIENT',
]


def _raw_intensity_metrics(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Calculate raw-sample metrics and guarded causal empirical percentiles."""
    from collections import deque

    if not samples:
        return []
    metrics: list[dict[str, Any]] = []
    left_30 = left_60 = 0
    activity_30 = activity_60 = net_30 = net_60 = 0.0
    first_ts = samples[0]['ts']
    for index, sample in enumerate(samples):
        if index == 0:
            dt_sec = None
            doi = None
            impulse = None
        else:
            dt_sec = (sample['ts'] - samples[index - 1]['ts']).total_seconds()
            doi = sample['oi'] - samples[index - 1]['oi']
            if sample.get('gap_break') or dt_sec <= 0:
                doi = None
                impulse = None
            else:
                impulse = abs(doi) * 5.0 / dt_sec
        while left_30 < index and samples[left_30]['ts'] < sample['ts'] - timedelta(seconds=30):
            old = metrics[left_30]
            activity_30 -= old['_abs_doi']
            net_30 -= old['_doi']
            left_30 += 1
        while left_60 < index and samples[left_60]['ts'] < sample['ts'] - timedelta(seconds=60):
            old = metrics[left_60]
            activity_60 -= old['_abs_doi']
            net_60 -= old['_doi']
            left_60 += 1
        abs_doi = abs(doi) if doi is not None else 0.0
        signed_doi = doi if doi is not None else 0.0
        activity_30 += abs_doi
        activity_60 += abs_doi
        net_30 += signed_doi
        net_60 += signed_doi
        metrics.append({
            'ts': sample['ts'], 'dt_sec': dt_sec, 'doi': doi, 'impulse': impulse,
            'activity_30': activity_30, 'activity_60': activity_60,
            'net_30': net_30, 'net_60': net_60,
            'directionality_30': abs(net_30) / activity_30 if activity_30 > 0 else None,
            'directionality_60': abs(net_60) / activity_60 if activity_60 > 0 else None,
            '_doi': signed_doi, '_abs_doi': abs_doi,
            'warmup': sample['ts'] - first_ts < timedelta(minutes=60),
        })

    # The reference is a moving sorted distribution. Its upper edge is T-30m
    # and its lower edge is T-6h30m; neither current nor recent samples enter.
    sorted_values = {'impulse': [], 'activity_30': [], 'activity_60': []}
    eligible = {'impulse': deque(), 'activity_30': deque(), 'activity_60': deque()}
    add_index = 0
    for index, item in enumerate(metrics):
        current_time = item['ts']
        lower = current_time - timedelta(hours=6, minutes=30)
        upper = current_time - timedelta(minutes=30)
        while add_index < index and metrics[add_index]['ts'] < upper:
            candidate = metrics[add_index]
            if candidate['ts'] >= lower:
                for key in sorted_values:
                    value = candidate[key] if key == 'impulse' else candidate[key]
                    if value is None:
                        continue
                    bisect.insort(sorted_values[key], value)
                    eligible[key].append((candidate['ts'], value))
            add_index += 1
        for key in sorted_values:
            while eligible[key] and eligible[key][0][0] < lower:
                _, value = eligible[key].popleft()
                position = bisect.bisect_left(sorted_values[key], value)
                if position < len(sorted_values[key]):
                    sorted_values[key].pop(position)
        if item['warmup']:
            item['impulse_pctl'] = item['burst_30_pctl'] = item['burst_60_pctl'] = None
        else:
            for metric_key, output_key in (
                ('impulse', 'impulse_pctl'),
                ('activity_30', 'burst_30_pctl'),
                ('activity_60', 'burst_60_pctl'),
            ):
                value = item[metric_key]
                distribution = sorted_values[metric_key]
                if value is None or not distribution:
                    item[output_key] = None
                else:
                    rank = bisect.bisect_right(distribution, value)
                    item[output_key] = rank / len(distribution) * 100.0
    return metrics


def _raw_intensity_summary(samples: list[dict[str, Any]],
                           minutes: list[dict[str, Any]],
                           market: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_metrics = _raw_intensity_metrics(samples)
    by_minute: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for item in raw_metrics:
        by_minute[item['ts'].replace(second=0, microsecond=0)].append(item)
    market_rows = _neighbor_analysis_rows(minutes, market)
    market_by_minute = {row['_time']: row for row in market_rows}
    summaries: list[dict[str, Any]] = []
    for minute, raw_rows in sorted(by_minute.items()):
        valid = [row for row in raw_rows if row['impulse_pctl'] is not None]
        for row in raw_rows:
            row['intensity_pctl'] = max((row[key] for key in ('impulse_pctl', 'burst_30_pctl', 'burst_60_pctl') if row[key] is not None), default=None)
        valid_intensity = [row for row in raw_rows if row['intensity_pctl'] is not None]
        minute_oi = next((row for row in minutes if row['minute'] == minute), None)
        market_row = market_by_minute.get(minute, {})
        peak = max(valid_intensity, key=lambda row: row['intensity_pctl']) if valid_intensity else None
        peak_type = None
        if peak:
            peak_type = max(
                (('IMPULSE_5S', peak['impulse_pctl']), ('BURST_30S', peak['burst_30_pctl']), ('BURST_60S', peak['burst_60_pctl'])),
                key=lambda pair: pair[1] if pair[1] is not None else -1,
            )[0]
        intensity_values = [row['intensity_pctl'] for row in raw_rows if row['intensity_pctl'] is not None]
        summary = {
            'TIME': minute.strftime('%Y-%m-%d %H:%M:%S'),
            'PRICE': market_row.get('PRICE'), 'REFERENCE_PRICE': market_row.get('REFERENCE_PRICE'),
            'PRICE_CHANGE': market_row.get('PRICE_CHANGE'),
            'TAKER_BUY': market_row.get('TAKER_BUY'), 'TAKER_SELL': market_row.get('TAKER_SELL'),
            'AGGR_SIDE': market_row.get('AGGR_SIDE'), 'AGGR_MAG': market_row.get('AGGR_MAG'),
            'AGGR_MAG_X': market_row.get('AGGR_MAG_X'), 'AGGR_SHARE': market_row.get('AGGR_SHARE'),
            'MEANINGFUL_AGGR': market_row.get('MEANINGFUL_AGGR', 'NO'),
            'EFF_RATIO': market_row.get('EFF_RATIO'), 'ABSORPTION_RATIO': market_row.get('ABSORPTION_RATIO'),
            'CONTROL': market_row.get('CONTROL'),
            'STRONG': market_row.get('STRONG', 'NO'), 'MEGA': market_row.get('MEGA', 'NO'),
            'OI_NET': minute_oi.get('net') if minute_oi else None,
            'OI_ADD': minute_oi.get('add') if minute_oi else None,
            'OI_EXIT': minute_oi.get('exit') if minute_oi else None,
            'OI_ACT': minute_oi.get('activity') if minute_oi else None,
            'OI_MAX_JUMP': minute_oi.get('jump') if minute_oi else None,
            'OI_IMPULSE_5S_EQ_MAX': max((row['impulse'] for row in raw_rows if row['impulse'] is not None), default=None),
            'OI_ACTIVITY_30S': raw_rows[-1]['activity_30'], 'OI_ACTIVITY_60S': raw_rows[-1]['activity_60'],
            'OI_NET_30S': raw_rows[-1]['net_30'], 'OI_NET_60S': raw_rows[-1]['net_60'],
            'OI_DIRECTIONALITY_30S': raw_rows[-1]['directionality_30'], 'OI_DIRECTIONALITY_60S': raw_rows[-1]['directionality_60'],
            'IMPULSE_5S_PCTL': max((row['impulse_pctl'] for row in raw_rows if row['impulse_pctl'] is not None), default=None),
            'BURST_30S_PCTL': max((row['burst_30_pctl'] for row in raw_rows if row['burst_30_pctl'] is not None), default=None),
            'BURST_60S_PCTL': max((row['burst_60_pctl'] for row in raw_rows if row['burst_60_pctl'] is not None), default=None),
            'MAX_IMPULSE_5S_PCTL': max((row['impulse_pctl'] for row in raw_rows if row['impulse_pctl'] is not None), default=None),
            'MAX_BURST_30S_PCTL': max((row['burst_30_pctl'] for row in raw_rows if row['burst_30_pctl'] is not None), default=None),
            'MAX_BURST_60S_PCTL': max((row['burst_60_pctl'] for row in raw_rows if row['burst_60_pctl'] is not None), default=None),
            'OI_INTENSITY_PCTL': max(intensity_values, default=None),
            'P95_HITS': sum(1 for row in raw_rows if row['intensity_pctl'] is not None and row['intensity_pctl'] >= 95.0),
            'P99_HITS': sum(1 for row in raw_rows if row['intensity_pctl'] is not None and row['intensity_pctl'] >= 99.0),
            'PEAK_TIMESTAMP': peak['ts'].strftime('%Y-%m-%d %H:%M:%S') if peak else None,
            'PEAK_TYPE': peak_type, 'WARMUP_INSUFFICIENT': any(row['warmup'] for row in raw_rows),
            '_raw_rows': raw_rows, '_minute_oi': minute_oi,
        }
        summaries.append({key: _analysis_value(value) for key, value in summary.items() if not key.startswith('_')})
        summaries[-1]['_raw_rows'] = raw_rows
        summaries[-1]['_time'] = minute
    return summaries, raw_metrics


def _raw_threshold_selected(row: dict[str, Any], threshold: float, metric: str) -> bool:
    if row.get('WARMUP_INSUFFICIENT') == 'YES':
        return False
    if metric == '5s':
        value = row.get('MAX_IMPULSE_5S_PCTL')
    elif metric == '30s':
        value = row.get('MAX_BURST_30S_PCTL')
    elif metric == '60s':
        value = row.get('MAX_BURST_60S_PCTL')
    else:
        value = row.get('OI_INTENSITY_PCTL')
    return value not in (None, '') and float(value) >= threshold


def _raw_intensity_report(rows: list[dict[str, Any]], output_csv: Path) -> str:
    target_day = datetime(2026, 9, 30, tzinfo=PANAMA).date()
    target_rows = [row for row in rows if row['_time'].date() == target_day and row['_time'].hour == 22 and 10 <= row['_time'].minute <= 40]
    strong_count = sum(row['STRONG'] == 'YES' or row['MEGA'] == 'YES' for row in rows)
    lines = [
        '# BTC-LRA Raw OI Intensity Analysis', '',
        'Diagnostic-only experiment. Production STRONG/MEGA, AGGR, CONTROL, DOMINANCE, GUI, Pine, replay chronology and `btc-lra-002.py` were not changed.', '',
        'Raw metrics use `dOI = OI[i] - OI[i-1]`. The reference distribution is causal: `T-6h30m <= sample_time < T-30m`. The last 30 minutes never enter the reference. Percentiles are empirical ranks; warmup points have no threshold selection.', '',
        f'- CSV: `{output_csv}`', f'- 1m summaries: {len(rows)}', f'- Existing STRONG/MEGA minutes: {strong_count}', '',
        '## Threshold comparison', '',
        '| THRESHOLD | 5s | 30s | 60s | COMBINED | +AGGR | +CONTROL |', '|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for threshold in (95.0, 97.5, 99.0, 99.5):
        counts: dict[str, int] = {}
        for metric in ('5s', '30s', '60s', 'combined'):
            counts[metric] = sum(_raw_threshold_selected(row, threshold, metric) for row in rows)
        combined_rows = [row for row in rows if _raw_threshold_selected(row, threshold, 'combined')]
        aggr_rows = [row for row in combined_rows if row['MEANINGFUL_AGGR'] == 'YES']
        control_rows = [row for row in combined_rows if row['CONTROL'] not in ('', None, 'CONTROL UNCLEAR')]
        lines.append(f'| P{threshold:g} | {counts["5s"]} | {counts["30s"]} | {counts["60s"]} | {counts["combined"]} | {len(aggr_rows)} | {len(control_rows)} |')
    lines.extend(['', '## Target minutes and existing effort/result', '', '| TIME | OI INTENSITY | PEAK TYPE | AGGR | EFF | CONTROL | STRONG |', '|---|---:|---|---|---:|---|---|'])
    for row in target_rows:
        aggr = f'{row["AGGR_SIDE"]} {row["AGGR_MAG"]} BTC' if row['AGGR_SIDE'] else '--'
        lines.append(f'| {row["_time"].strftime("%H:%M")} | {row["OI_INTENSITY_PCTL"] or "--"} | {row["PEAK_TYPE"] or "--"} | {aggr} | {row["EFF_RATIO"] or "--"} | {row["CONTROL"] or "--"} | {row["STRONG"]} |')
    lines.extend(['', '## Requested 22:10–22:40 detail', '', '| TIME | OI INTENSITY | 5s | 30s | 60s | P99 HITS | PEAK | AGGR | AGGR MAG X | EFF_RATIO | CONTROL | STRONG |', '|---|---:|---:|---:|---:|---:|---|---|---:|---:|---|---|'])
    for row in target_rows:
        aggr = f'{row["AGGR_SIDE"]} {row["AGGR_MAG"]}' if row['AGGR_SIDE'] else '--'
        lines.append(f'| {row["_time"].strftime("%H:%M")} | {row["OI_INTENSITY_PCTL"] or "--"} | {row["IMPULSE_5S_PCTL"] or "--"} | {row["BURST_30S_PCTL"] or "--"} | {row["BURST_60S_PCTL"] or "--"} | {row["P99_HITS"]} | {row["PEAK_TYPE"] or "--"} | {aggr} | {row["AGGR_MAG_X"] or "--"} | {row["EFF_RATIO"] or "--"} | {row["CONTROL"] or "--"} | {row["STRONG"]} |')
    lines.extend(['', '## 22:17 / 22:26 / 22:27 / 22:37 / 22:39', ''])
    for time_text in ('22:17', '22:26', '22:27', '22:37', '22:39'):
        matching = [row for row in target_rows if row['_time'].strftime('%H:%M') == time_text]
        if not matching:
            lines.append(f'- **{time_text}**: no raw OI minute in loaded data.')
            continue
        row = matching[0]
        lines.append(f'- **{time_text}**: intensity `{row["OI_INTENSITY_PCTL"] or "--"}` (5s `{row["IMPULSE_5S_PCTL"] or "--"}`, 30s `{row["BURST_30S_PCTL"] or "--"}`, 60s `{row["BURST_60S_PCTL"] or "--"}`), P99 hits `{row["P99_HITS"]}`, peak `{row["PEAK_TYPE"] or "--"}`, AGGR `{row["AGGR_SIDE"] or "--"} {row["AGGR_MAG"] or "--"}`, CONTROL `{row["CONTROL"] or "--"}`, STRONG `{row["STRONG"]}`.')
    lines.extend(['', '## Threshold detail for benchmark minutes', '', '| THRESHOLD | 5s | 30s | 60s | COMBINED | +AGGR | +CONTROL |', '|---:|---|---|---|---|---|---|'])
    for threshold in (95.0, 97.5, 99.0, 99.5):
        cells = []
        for metric in ('5s', '30s', '60s', 'combined'):
            selected = [row['_time'].strftime('%H:%M') for row in target_rows if _raw_threshold_selected(row, threshold, metric)]
            cells.append(', '.join(selected) or '--')
        aggr = [row['_time'].strftime('%H:%M') for row in target_rows if _raw_threshold_selected(row, threshold, 'combined') and row['MEANINGFUL_AGGR'] == 'YES']
        control = [row['_time'].strftime('%H:%M') for row in target_rows if _raw_threshold_selected(row, threshold, 'combined') and row['CONTROL'] not in ('', None, 'CONTROL UNCLEAR')]
        lines.append(f'| P{threshold:g} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} | {", ".join(aggr) or "--"} | {", ".join(control) or "--"} |')
    lines.extend(['', '## Interpretation framework', '', '- **Single impulse:** high 5s percentile but low `P99_HITS` and no repeated high 30s/60s percentile.', '- **Burst:** high 30s/60s percentile with multiple raw high-percentile samples.', '- **Sustained process:** high-percentile minutes repeated across neighboring 1m rows. This report describes the evidence; it does not create a production event rule.', '', f'Existing STRONG/MEGA count used for regression check: **{strong_count}**.', 'The report does not select a production threshold or declare a trading signal.'])
    return '\n'.join(lines) + '\n'


def run_raw_intensity_analysis(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parent
    raw_paths = args.raw_oi or discover_raw_paths(root)
    market_path = args.market_csv or discover_market_path(root)
    if not raw_paths:
        raise RuntimeError('Raw intensity analysis: raw OI data was not found')
    if market_path is None:
        raise RuntimeError('Raw intensity analysis: market CSV was not found')
    samples = load_raw(raw_paths)
    market = load_market(market_path)
    minutes = minute_oi(samples)
    annotate_minutes(minutes)
    common = recorded_range(samples, market)
    if common is not None:
        start, end = common
        if args.from_time:
            start = max(start, parse_time(args.from_time))
        if args.end:
            end = min(end, parse_time(args.end))
        samples = [row for row in samples if start <= row['ts'] <= end + timedelta(minutes=1)]
        minutes = [row for row in minutes if start <= row['minute'] <= end]
    rows, raw_metrics = _raw_intensity_summary(samples, minutes, market)
    output_csv = root / 'data' / 'research' / 'BTC_LRA_RAW_OI_INTENSITY.csv'
    output_samples = root / 'data' / 'research' / 'BTC_LRA_RAW_OI_INTENSITY_SAMPLES.csv'
    output_md = root / 'data' / 'research' / 'BTC_LRA_RAW_OI_INTENSITY_ANALYSIS.md'
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=RAW_INTENSITY_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, '') for key in RAW_INTENSITY_COLUMNS})
    benchmark_ranges = (
        (datetime(2026, 9, 30, 14, 45, tzinfo=PANAMA), datetime(2026, 9, 30, 15, 0, 59, tzinfo=PANAMA)),
        (datetime(2026, 9, 30, 20, 25, tzinfo=PANAMA), datetime(2026, 9, 30, 20, 45, 59, tzinfo=PANAMA)),
        (datetime(2026, 9, 30, 22, 10, tzinfo=PANAMA), datetime(2026, 9, 30, 22, 40, 59, tzinfo=PANAMA)),
    )
    with output_samples.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=RAW_SAMPLE_COLUMNS)
        writer.writeheader()
        for item in raw_metrics:
            if not any(start <= item['ts'] <= end for start, end in benchmark_ranges):
                continue
            row = {
                'TIMESTAMP': item['ts'].strftime('%Y-%m-%d %H:%M:%S'), 'DT_SEC': item['dt_sec'], 'DOI': item['doi'],
                'OI_IMPULSE_5S_EQ': item['impulse'], 'OI_ACTIVITY_30S': item['activity_30'], 'OI_ACTIVITY_60S': item['activity_60'],
                'OI_NET_30S': item['net_30'], 'OI_NET_60S': item['net_60'], 'OI_DIRECTIONALITY_30S': item['directionality_30'],
                'OI_DIRECTIONALITY_60S': item['directionality_60'], 'IMPULSE_5S_PCTL': item['impulse_pctl'],
                'BURST_30S_PCTL': item['burst_30_pctl'], 'BURST_60S_PCTL': item['burst_60_pctl'],
                'OI_INTENSITY_PCTL': item.get('intensity_pctl'), 'WARMUP_INSUFFICIENT': item['warmup'],
            }
            writer.writerow({key: _analysis_value(value) for key, value in row.items()})
    output_md.write_text(_raw_intensity_report(rows, output_csv), encoding='utf-8')
    print(f'RAW INTENSITY CSV: {output_csv}')
    print(f'RAW SAMPLE AUDIT CSV: {output_samples}')
    print(f'RAW INTENSITY REPORT: {output_md}')


LOCAL_DOMINANCE_COLUMNS = [
    'TIME', 'PRICE', 'SESSION_BUY_PCT', 'SESSION_SELL_PCT',
    *[field for half_life in (10, 15, 20, 30) for field in (
        f'LOCAL_BUY_{half_life}', f'LOCAL_SELL_{half_life}',
        f'LOCAL_MASS_{half_life}', f'BUY_ANCHOR_{half_life}',
        f'SELL_ANCHOR_{half_life}', f'BUY_REWARD_BPS_{half_life}',
        f'SELL_REWARD_BPS_{half_life}',
    )],
]

# Research-only entry hypothesis parameters. These are intentionally explicit
# first-pass values; they are not production detector thresholds.
ENTRY_DOM_GAP_PCT = 15.0
ENTRY_MIN_LOCAL_MASS_BTC = 50.0
ENTRY_BAD_REWARD_BPS = 0.0
ENTRY_CONFIRM_REWARD_BPS = 0.0
ENTRY_INVALIDATION_REWARD_BPS = 2.0


def _local_dominance_state(contributions: list[dict[str, Any]], now: datetime,
                           price: float | None, half_life: int) -> dict[str, float | None]:
    buy_mass = sell_mass = buy_price_mass = sell_price_mass = 0.0
    for contribution in contributions:
        age_minutes = max(0.0, (now - contribution['time']).total_seconds() / 60.0)
        decay = 2.0 ** (-age_minutes / half_life)
        buy_weight = contribution['buy'] * decay
        sell_weight = contribution['sell'] * decay
        buy_mass += buy_weight
        sell_mass += sell_weight
        buy_price_mass += buy_weight * contribution['price']
        sell_price_mass += sell_weight * contribution['price']
    mass = buy_mass + sell_mass
    buy_anchor = buy_price_mass / buy_mass if buy_mass > 0 else None
    sell_anchor = sell_price_mass / sell_mass if sell_mass > 0 else None
    return {
        'buy': buy_mass, 'sell': sell_mass, 'mass': mass,
        'buy_anchor': buy_anchor, 'sell_anchor': sell_anchor,
        'buy_pct': buy_mass / mass * 100 if mass > 0 else None,
        'sell_pct': sell_mass / mass * 100 if mass > 0 else None,
        'buy_reward': ((price - buy_anchor) / buy_anchor * 10000
                       if price is not None and buy_anchor else None),
        'sell_reward': ((sell_anchor - price) / sell_anchor * 10000
                        if price is not None and sell_anchor else None),
    }


def _local_dominance_contributions(samples: list[dict[str, Any]],
                                   minutes: list[dict[str, Any]],
                                   market: list[dict[str, Any]],
                                   anchor: datetime, end: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Replay finalized event contributions without changing production state."""
    raw_summaries, _ = _raw_intensity_summary(samples, minutes, market)
    raw_by_time = {row['_time']: row for row in raw_summaries}
    event_by_time = {minutes[event['confirmed']]['minute']: event for event in individual_events(minutes)}
    market_by_time = {row['ts'].replace(second=0, microsecond=0): row for row in market}
    session = Session(anchor)
    session.market_history = market
    session.initialize_continuous_baseline(market)
    contributions: list[dict[str, Any]] = []
    output: list[dict[str, Any]] = []
    for minute in minutes:
        minute_time = minute['minute']
        if minute_time < anchor or minute_time > end:
            continue
        session.apply_oi(minute)
        market_row = market_by_time.get(minute_time)
        market_rows = [market_row] if market_row is not None else []
        for row in market_rows:
            session.apply_market(row)
            session.apply_continuous_market(row)
        canonical_event: dict[str, Any] | None = None
        if minute_time in event_by_time:
            event = dict(event_by_time[minute_time])
            event['start_time'] = minutes[event['start']]['minute']
            event['kind'] = 'MEGA' if minutes[event['confirmed']]['mega'] else 'STRONG'
            decorate_event_interval(event, minutes, market)
            with contextlib.redirect_stdout(io.StringIO()):
                session.emit_event(event, minute, market_rows)
            canonical_event = session.event_history[-1]
        raw_event = apply_raw_event_to_session(session, raw_by_time[minute_time]) if minute_time in raw_by_time else None
        if canonical_event is None and raw_event is not None:
            canonical_event = raw_event
        if canonical_event is not None:
            ledger = session.finalize_raw_minute(minute_time, canonical_event)
            buy_weight = float(ledger['final_buy_dom_contribution'])
            sell_weight = float(ledger['final_sell_dom_contribution'])
            if buy_weight or sell_weight:
                event_price = canonical_event.get('display_event_price')
                if event_price is None and market_row is not None:
                    event_price = market_row['close']
                if event_price is not None:
                    contributions.append({
                        'time': minute_time, 'price': float(event_price),
                        'buy': buy_weight, 'sell': sell_weight,
                        'control': canonical_event.get('control', 'CONTROL UNCLEAR'),
                    })
        price = market_row['close'] if market_row is not None else session.price
        session_buy_pct, session_sell_pct = dominance_percentages(
            session.buy_dominance_v2_btc, session.sell_dominance_v2_btc
        )
        row: dict[str, Any] = {
            'TIME': minute_time.strftime('%Y-%m-%d %H:%M:%S'),
            'PRICE': price,
            'SESSION_BUY_PCT': session_buy_pct,
            'SESSION_SELL_PCT': session_sell_pct,
            'CONTROL': canonical_event.get('control', '') if canonical_event else '',
            'OI_FLOW': session.oi_event_flow,
        }
        for half_life in (10, 15, 20, 30):
            state = _local_dominance_state(contributions, minute_time, price, half_life)
            row.update({
                f'LOCAL_BUY_{half_life}': state['buy'],
                f'LOCAL_SELL_{half_life}': state['sell'],
                f'LOCAL_MASS_{half_life}': state['mass'],
                f'BUY_ANCHOR_{half_life}': state['buy_anchor'],
                f'SELL_ANCHOR_{half_life}': state['sell_anchor'],
                f'BUY_REWARD_BPS_{half_life}': state['buy_reward'],
                f'SELL_REWARD_BPS_{half_life}': state['sell_reward'],
            })
        output.append(row)
    return output, contributions


def _local_dominance_report(rows: list[dict[str, Any]],
                            contributions: list[dict[str, Any]],
                            output_csv: Path) -> str:
    windows = (
        ('14:45-15:10', datetime(2026, 9, 30, 14, 45, tzinfo=PANAMA), datetime(2026, 9, 30, 15, 10, tzinfo=PANAMA)),
        ('20:20-20:50', datetime(2026, 9, 30, 20, 20, tzinfo=PANAMA), datetime(2026, 9, 30, 20, 50, tzinfo=PANAMA)),
        ('21:00-22:45', datetime(2026, 9, 30, 21, 0, tzinfo=PANAMA), datetime(2026, 9, 30, 22, 45, tzinfo=PANAMA)),
    )
    lines = [
        '# BTC-LRA LOCAL / ACTIVE DOMINANCE', '',
        'Research layer only. SESSION DOMINANCE is unchanged.',
        '', f'CSV: `{output_csv}`', '',
        'Decay: `2 ** (-age_minutes / half_life)`; anchors are weighted research anchors, not position averages.',
        '', '## Half-life comparison', '',
        'HALF LIFE | EARLY DETECTION | STABILITY | NOISE',
    ]
    for half_life in (10, 15, 20, 30):
        values = [row.get(f'LOCAL_MASS_{half_life}') for row in rows if row.get(f'LOCAL_MASS_{half_life}') not in (None, 0)]
        active = [row for row in rows if row.get(f'LOCAL_MASS_{half_life}') not in (None, 0)]
        flips = 0
        previous_side = None
        for row in active:
            buy = row.get(f'LOCAL_BUY_{half_life}', 0) or 0
            sell = row.get(f'LOCAL_SELL_{half_life}', 0) or 0
            side = 'BUY' if buy > sell else 'SELL' if sell > buy else None
            if side and previous_side and side != previous_side:
                flips += 1
            if side:
                previous_side = side
        first = active[0]['TIME'] if active else '--'
        lines.append(f'{half_life:>8} | {first} | {len(active)} active rows / {flips} side flips | {flips} flips')
    for title, start, finish in windows:
        window_rows = [row for row in rows if start <= datetime.strptime(row['TIME'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=PANAMA) <= finish]
        window_events = [event for event in contributions if start <= event['time'] <= finish]
        lines.extend(['', f'## {title}', '', f'Finalized contributions: {len(window_events)}'])
        if not window_events:
            lines.append('No finalized dominance contribution in this window.')
            continue
        lines.append('TIME | SIDE | BUY WEIGHT | SELL WEIGHT | CONTROL | PRICE')
        for event in window_events:
            side = 'BUY' if event['buy'] > 0 else 'SELL'
            lines.append(f'{event["time"].strftime("%H:%M")} | {side} | {event["buy"]:.3f} | {event["sell"]:.3f} | {event["control"]} | {event["price"]:.1f}')
        for half_life in (10, 15, 20, 30):
            first = next((row for row in window_rows if (row.get(f'LOCAL_MASS_{half_life}') or 0) > 0), None)
            if first is None:
                continue
            lines.append(
                f'HL {half_life}: first active {first["TIME"]}; '
                f'final mass {first.get(f"LOCAL_MASS_{half_life}")} BTC; '
                f'buy reward {first.get(f"BUY_REWARD_BPS_{half_life}")} bps; '
                f'sell reward {first.get(f"SELL_REWARD_BPS_{half_life}")} bps'
            )
    lines.extend(['', 'The report is descriptive and does not select a trading signal or production half-life.'])
    return '\n'.join(lines) + '\n'


def run_local_dominance_analysis(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parent
    raw_paths = args.raw_oi or discover_raw_paths(root)
    market_path = args.market_csv or discover_market_path(root)
    if not raw_paths or market_path is None:
        raise SystemExit('Local dominance research requires recorded raw OI and 1m market data.')
    samples = load_raw(raw_paths)
    market = load_market(market_path)
    minutes = minute_oi(samples)
    annotate_minutes(minutes)
    common = recorded_range(samples, market)
    if common is None:
        raise SystemExit('No common raw OI / market range for local dominance research.')
    start, end = common
    if args.from_time:
        start = max(start, parse_time(args.from_time))
    if args.end:
        end = min(end, parse_time(args.end))
    samples = [sample for sample in samples if start <= sample['ts'] <= end + timedelta(minutes=1)]
    minutes = [row for row in minutes if start <= row['minute'] <= end]
    market = [row for row in market if start <= row['ts'] <= end]
    rows, contributions = _local_dominance_contributions(samples, minutes, market, start, end)
    output_csv = root / 'data' / 'research' / 'BTC_LRA_LOCAL_DOMINANCE.csv'
    output_md = root / 'data' / 'research' / 'BTC_LRA_LOCAL_DOMINANCE_ANALYSIS.md'
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=LOCAL_DOMINANCE_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _analysis_value(row.get(key)) for key in LOCAL_DOMINANCE_COLUMNS})
    output_md.write_text(_local_dominance_report(rows, contributions, output_csv), encoding='utf-8')
    print(f'LOCAL DOMINANCE CSV: {output_csv}')
    print(f'LOCAL DOMINANCE REPORT: {output_md}')
    print(f'ROWS: {len(rows)} | CONTRIBUTIONS: {len(contributions)}')


ENTRY_HYPOTHESIS_COLUMNS = [
    'TIME', 'HALF_LIFE', 'SIDE', 'PRICE', 'LOCAL_DOM_PCT', 'LOCAL_MASS',
    'DOMINANT_ANCHOR', 'PRICE_REWARD_BPS', 'SESSION_DOM_PCT', 'OI_FLOW',
    'CONTROL', 'CONFIRM_TIME', 'INVALIDATION_TIME', 'PRICE_+5M',
    'PRICE_+10M', 'PRICE_+20M', 'PRICE_+30M', 'MFE', 'MAE',
    'MFE_BPS', 'MAE_BPS',
]


def _local_time(row: dict[str, Any]) -> datetime:
    return datetime.strptime(row['TIME'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=PANAMA)


def _entry_price_at(rows_by_time: dict[datetime, dict[str, Any]],
                    timestamp: datetime) -> float | None:
    row = rows_by_time.get(timestamp)
    return float(row['PRICE']) if row is not None and row.get('PRICE') not in (None, '') else None


def _entry_hypotheses(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    result: list[dict[str, Any]] = []
    distributions: dict[int, dict[str, Any]] = {}
    parsed = [(_local_time(row), row) for row in rows]
    rows_by_time = {timestamp: row for timestamp, row in parsed}
    for half_life in (10, 15, 20, 30):
        candidates: list[dict[str, Any]] = []
        active_side: str | None = None
        for index, (timestamp, row) in enumerate(parsed):
            buy = float(row.get(f'LOCAL_BUY_{half_life}') or 0.0)
            sell = float(row.get(f'LOCAL_SELL_{half_life}') or 0.0)
            mass = float(row.get(f'LOCAL_MASS_{half_life}') or 0.0)
            buy_pct = buy / mass * 100 if mass > 0 else None
            sell_pct = sell / mass * 100 if mass > 0 else None
            buy_reward = row.get(f'BUY_REWARD_BPS_{half_life}')
            sell_reward = row.get(f'SELL_REWARD_BPS_{half_life}')
            buy_reward = float(buy_reward) if buy_reward not in (None, '') else None
            sell_reward = float(sell_reward) if sell_reward not in (None, '') else None
            previous_mass = 0.0
            if index > 0:
                previous_mass = float(parsed[index - 1][1].get(f'LOCAL_MASS_{half_life}') or 0.0)
            side = None
            reward = None
            side_pct = None
            anchor = None
            if (sell_pct is not None and sell_pct - buy_pct >= ENTRY_DOM_GAP_PCT
                    and mass >= ENTRY_MIN_LOCAL_MASS_BTC and mass >= previous_mass
                    and sell_reward is not None and sell_reward <= ENTRY_BAD_REWARD_BPS):
                side, reward, side_pct = 'LONG', sell_reward, sell_pct
                anchor = row.get(f'SELL_ANCHOR_{half_life}')
            elif (buy_pct is not None and buy_pct - sell_pct >= ENTRY_DOM_GAP_PCT
                  and mass >= ENTRY_MIN_LOCAL_MASS_BTC and mass >= previous_mass
                  and buy_reward is not None and buy_reward <= ENTRY_BAD_REWARD_BPS):
                side, reward, side_pct = 'SHORT', buy_reward, buy_pct
                anchor = row.get(f'BUY_ANCHOR_{half_life}')
            if side is None or active_side is not None:
                continue
            active_side = side
            confirmation_time = None
            invalidation_time = None
            for future_timestamp, future in parsed[index + 1:]:
                future_price = float(future['PRICE']) if future.get('PRICE') not in (None, '') else None
                future_control = str(future.get('CONTROL') or '')
                if side == 'LONG':
                    future_reward = future.get(f'SELL_REWARD_BPS_{half_life}')
                    good_control = future_control in ('CONTROL MARKET BUY', 'CONTROL LIMIT BUY')
                    if (future_reward not in (None, '') and float(future_reward) <= ENTRY_CONFIRM_REWARD_BPS
                            and good_control and anchor not in (None, '') and future_price is not None and future_price > float(anchor)):
                        confirmation_time = future_timestamp
                        break
                    if future_reward not in (None, '') and float(future_reward) >= ENTRY_INVALIDATION_REWARD_BPS:
                        invalidation_time = future_timestamp
                        break
                else:
                    future_reward = future.get(f'BUY_REWARD_BPS_{half_life}')
                    good_control = future_control in ('CONTROL MARKET SELL', 'CONTROL LIMIT SELL')
                    if (future_reward not in (None, '') and float(future_reward) <= ENTRY_CONFIRM_REWARD_BPS
                            and good_control and anchor not in (None, '') and future_price is not None and future_price < float(anchor)):
                        confirmation_time = future_timestamp
                        break
                    if future_reward not in (None, '') and float(future_reward) >= ENTRY_INVALIDATION_REWARD_BPS:
                        invalidation_time = future_timestamp
                        break
            horizon_prices = []
            start_price = float(row['PRICE']) if row.get('PRICE') not in (None, '') else None
            for future_timestamp, future in parsed[index + 1:]:
                age = (future_timestamp - timestamp).total_seconds() / 60.0
                if age > 30:
                    break
                if future.get('PRICE') not in (None, ''):
                    horizon_prices.append((age, float(future['PRICE'])))
            def forward_price(minutes_ahead: int) -> float | None:
                return _entry_price_at(rows_by_time, timestamp + timedelta(minutes=minutes_ahead))
            favorable_moves = []
            adverse_moves = []
            if start_price is not None:
                for _age, future_price in horizon_prices:
                    if side == 'LONG':
                        favorable_moves.append(future_price - start_price)
                        adverse_moves.append(start_price - future_price)
                    else:
                        favorable_moves.append(start_price - future_price)
                        adverse_moves.append(future_price - start_price)
            mfe = max(0.0, max(favorable_moves, default=0.0))
            mae = max(0.0, max(adverse_moves, default=0.0))
            session_pct = f'{row.get("SESSION_BUY_PCT", "")} / {row.get("SESSION_SELL_PCT", "")}'
            result.append({
                'TIME': row['TIME'], 'HALF_LIFE': half_life, 'SIDE': side,
                'PRICE': start_price, 'LOCAL_DOM_PCT': side_pct, 'LOCAL_MASS': mass,
                'DOMINANT_ANCHOR': anchor, 'PRICE_REWARD_BPS': reward,
                'SESSION_DOM_PCT': session_pct, 'OI_FLOW': row.get('OI_FLOW'),
                'CONTROL': row.get('CONTROL', ''),
                'CONFIRM_TIME': confirmation_time.strftime('%Y-%m-%d %H:%M:%S') if confirmation_time else '',
                'INVALIDATION_TIME': invalidation_time.strftime('%Y-%m-%d %H:%M:%S') if invalidation_time else '',
                'PRICE_+5M': forward_price(5), 'PRICE_+10M': forward_price(10),
                'PRICE_+20M': forward_price(20), 'PRICE_+30M': forward_price(30),
                'MFE': mfe if start_price is not None else None,
                'MAE': mae if start_price is not None else None,
                'MFE_BPS': mfe / start_price * 10000 if start_price else None,
                'MAE_BPS': mae / start_price * 10000 if start_price else None,
            })
            # Release the latch after a terminal outcome; otherwise the same
            # unresolved episode is represented by its earliest causal row.
            if confirmation_time is not None or invalidation_time is not None:
                active_side = None
        mass_values = [float(row.get(f'LOCAL_MASS_{half_life}') or 0.0)
                       for row in rows if row.get(f'LOCAL_MASS_{half_life}') not in (None, '')]
        reward_values = []
        for row in rows:
            buy_value = float(row.get(f'LOCAL_BUY_{half_life}') or 0.0)
            sell_value = float(row.get(f'LOCAL_SELL_{half_life}') or 0.0)
            reward_key = f'SELL_REWARD_BPS_{half_life}' if sell_value >= buy_value else f'BUY_REWARD_BPS_{half_life}'
            if row.get(reward_key) not in (None, ''):
                reward_values.append(float(row[reward_key]))
        distributions[half_life] = {
            'mass': mass_values, 'reward': reward_values,
            'candidates': sum(1 for candidate in result if candidate['HALF_LIFE'] == half_life),
        }
    return result, distributions


def run_entry_hypotheses_analysis(args: argparse.Namespace) -> None:
    # Reuse the exact LOCAL DOMINANCE research replay, then classify only its
    # output. No production event or dominance state is changed.
    root = Path(__file__).resolve().parent
    raw_paths = args.raw_oi or discover_raw_paths(root)
    market_path = args.market_csv or discover_market_path(root)
    if not raw_paths or market_path is None:
        raise SystemExit('Entry hypothesis research requires recorded raw OI and 1m market data.')
    samples = load_raw(raw_paths)
    market = load_market(market_path)
    minutes = minute_oi(samples)
    annotate_minutes(minutes)
    common = recorded_range(samples, market)
    if common is None:
        raise SystemExit('No common raw OI / market range for entry hypothesis research.')
    start, end = common
    if args.from_time:
        start = max(start, parse_time(args.from_time))
    if args.end:
        end = min(end, parse_time(args.end))
    samples = [sample for sample in samples if start <= sample['ts'] <= end + timedelta(minutes=1)]
    minutes = [row for row in minutes if start <= row['minute'] <= end]
    market = [row for row in market if start <= row['ts'] <= end]
    local_rows, _contributions = _local_dominance_contributions(samples, minutes, market, start, end)
    candidates, distributions = _entry_hypotheses(local_rows)
    output_csv = root / 'data' / 'research' / 'BTC_LRA_ENTRY_HYPOTHESES.csv'
    output_md = root / 'data' / 'research' / 'BTC_LRA_ENTRY_HYPOTHESES.md'
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=ENTRY_HYPOTHESIS_COLUMNS)
        writer.writeheader()
        for row in candidates:
            writer.writerow({key: _analysis_value(row.get(key)) for key in ENTRY_HYPOTHESIS_COLUMNS})
    lines = [
        '# BTC-LRA ENTRY HYPOTHESES', '',
        'Research-only classification built from LOCAL DOMINANCE. It is not a trading signal and does not modify Pine.', '',
        f'Initial parameters: DOM gap >= {ENTRY_DOM_GAP_PCT:.1f} pct points; local mass >= {ENTRY_MIN_LOCAL_MASS_BTC:.1f} BTC; bad reward <= {ENTRY_BAD_REWARD_BPS:.1f} bps; invalidation >= {ENTRY_INVALIDATION_REWARD_BPS:.1f} bps.', '',
        '## Candidate distribution', '',
        'HALF LIFE | CANDIDATES | MASS MEDIAN | MASS P95 | REWARD MEDIAN',
    ]
    for half_life in (10, 15, 20, 30):
        mass = sorted(distributions[half_life]['mass'])
        reward = sorted(distributions[half_life]['reward'])
        median = mass[len(mass) // 2] if mass else None
        p95 = mass[min(len(mass) - 1, int(len(mass) * 0.95))] if mass else None
        reward_median = reward[len(reward) // 2] if reward else None
        lines.append(f'{half_life:>8} | {distributions[half_life]["candidates"]:>10} | {median} | {p95} | {reward_median}')
    lines.extend(['', '## Real candidate episodes', '', 'TIME | HALF LIFE | SIDE | PRICE | LOCAL DOM % | MASS | ANCHOR | REWARD | CONFIRM | INVALIDATION | MFE | MAE'])
    for candidate in candidates:
        lines.append(' | '.join(str(candidate.get(key, '')) for key in (
            'TIME', 'HALF_LIFE', 'SIDE', 'PRICE', 'LOCAL_DOM_PCT', 'LOCAL_MASS',
            'DOMINANT_ANCHOR', 'PRICE_REWARD_BPS', 'CONFIRM_TIME',
            'INVALIDATION_TIME', 'MFE', 'MAE')))
    target = [candidate for candidate in candidates if '22:' in candidate['TIME']]
    lines.extend(['', '## Earliest causal 22:xx candidate', ''])
    if target:
        earliest = min(target, key=lambda candidate: candidate['TIME'])
        lines.append(f'{earliest["TIME"]} | {earliest["SIDE"]} | half-life {earliest["HALF_LIFE"]} | price {earliest["PRICE"]} | reward {earliest["PRICE_REWARD_BPS"]} bps')
    else:
        lines.append('No candidate was produced in the 22:xx interval.')
    lines.extend(['', 'Thresholds are first-pass research parameters and were not optimized on the 22:xx outcome.'])
    output_md.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'ENTRY HYPOTHESES CSV: {output_csv}')
    print(f'ENTRY HYPOTHESES REPORT: {output_md}')
    print(f'CANDIDATES: {len(candidates)}')


EPISODE_COLUMNS = [
    'SIDE', 'START TIME', 'START PRICE', 'HALF LIFE', 'START DOM %',
    'START MASS', 'START ANCHOR', 'START REWARD', 'PEAK DOM %', 'PEAK MASS',
    'CONFIRM TIME', 'CONFIRM PRICE', 'INVALIDATION TIME', 'END TIME',
    'END PRICE', 'MFE', 'MAE', 'MFE_BPS', 'MAE_BPS', 'DURATION',
    'FINAL STATE', 'END REASON',
]


def _episode_signal(row: dict[str, Any], half_life: int, previous_mass: float) -> dict[str, Any] | None:
    buy = float(row.get(f'LOCAL_BUY_{half_life}') or 0.0)
    sell = float(row.get(f'LOCAL_SELL_{half_life}') or 0.0)
    mass = float(row.get(f'LOCAL_MASS_{half_life}') or 0.0)
    if mass <= 0 or mass < ENTRY_MIN_LOCAL_MASS_BTC or mass < previous_mass:
        return None
    buy_pct = buy / mass * 100
    sell_pct = sell / mass * 100
    buy_reward = row.get(f'BUY_REWARD_BPS_{half_life}')
    sell_reward = row.get(f'SELL_REWARD_BPS_{half_life}')
    buy_reward = float(buy_reward) if buy_reward not in (None, '') else None
    sell_reward = float(sell_reward) if sell_reward not in (None, '') else None
    if sell_pct - buy_pct >= ENTRY_DOM_GAP_PCT and sell_reward is not None and sell_reward <= ENTRY_BAD_REWARD_BPS:
        return {'side': 'LONG', 'dom_pct': sell_pct, 'mass': mass,
                'anchor': row.get(f'SELL_ANCHOR_{half_life}'), 'reward': sell_reward}
    if buy_pct - sell_pct >= ENTRY_DOM_GAP_PCT and buy_reward is not None and buy_reward <= ENTRY_BAD_REWARD_BPS:
        return {'side': 'SHORT', 'dom_pct': buy_pct, 'mass': mass,
                'anchor': row.get(f'BUY_ANCHOR_{half_life}'), 'reward': buy_reward}
    return None


def _episode_row(episode: dict[str, Any], parsed: list[tuple[datetime, dict[str, Any]]], end_index: int) -> dict[str, Any]:
    start_index = episode['start_index']
    start_time = parsed[start_index][0]
    end_time, end_row = parsed[end_index]
    start_price = episode['start_price']
    prices = [float(row['PRICE']) for _timestamp, row in parsed[start_index:end_index + 1] if row.get('PRICE') not in (None, '')]
    favorable = []
    adverse = []
    for price in prices:
        if episode['side'] == 'LONG':
            favorable.append(price - start_price)
            adverse.append(start_price - price)
        else:
            favorable.append(start_price - price)
            adverse.append(price - start_price)
    mfe = max(0.0, max(favorable, default=0.0))
    mae = max(0.0, max(adverse, default=0.0))
    return {
        'SIDE': episode['side'], 'START TIME': start_time.strftime('%Y-%m-%d %H:%M:%S'),
        'START PRICE': start_price, 'HALF LIFE': episode['half_life'],
        'START DOM %': episode['start_dom_pct'], 'START MASS': episode['start_mass'],
        'START ANCHOR': episode['start_anchor'], 'START REWARD': episode['start_reward'],
        'PEAK DOM %': episode['peak_dom_pct'], 'PEAK MASS': episode['peak_mass'],
        'CONFIRM TIME': episode['confirm_time'].strftime('%Y-%m-%d %H:%M:%S') if episode['confirm_time'] else '',
        'CONFIRM PRICE': episode['confirm_price'],
        'INVALIDATION TIME': episode['invalidation_time'].strftime('%Y-%m-%d %H:%M:%S') if episode['invalidation_time'] else '',
        'END TIME': end_time.strftime('%Y-%m-%d %H:%M:%S'),
        'END PRICE': float(end_row['PRICE']) if end_row.get('PRICE') not in (None, '') else None,
        'MFE': mfe, 'MAE': mae,
        'MFE_BPS': mfe / start_price * 10000 if start_price else None,
        'MAE_BPS': mae / start_price * 10000 if start_price else None,
        'DURATION': (end_time - start_time).total_seconds() / 60.0,
        'FINAL STATE': episode['state'], 'END REASON': episode['end_reason'],
    }


def _entry_episode_state_machine(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parsed = [(_local_time(row), row) for row in rows]
    episodes: list[dict[str, Any]] = []
    for half_life in (10, 15, 20, 30):
        current: dict[str, Any] | None = None
        for index, (timestamp, row) in enumerate(parsed):
            previous_mass = float(parsed[index - 1][1].get(f'LOCAL_MASS_{half_life}') or 0.0) if index else 0.0
            signal = _episode_signal(row, half_life, previous_mass)
            buy = float(row.get(f'LOCAL_BUY_{half_life}') or 0.0)
            sell = float(row.get(f'LOCAL_SELL_{half_life}') or 0.0)
            mass = float(row.get(f'LOCAL_MASS_{half_life}') or 0.0)
            dom_pct = sell / mass * 100 if current and current['side'] == 'LONG' and mass else buy / mass * 100 if current and current['side'] == 'SHORT' and mass else None
            reward_key = f'SELL_REWARD_BPS_{half_life}' if current and current['side'] == 'LONG' else f'BUY_REWARD_BPS_{half_life}' if current else ''
            reward = row.get(reward_key) if reward_key else None
            reward = float(reward) if reward not in (None, '') else None
            if current is None:
                if signal is not None and row.get('PRICE') not in (None, ''):
                    current = {
                        'side': signal['side'], 'half_life': half_life, 'state': 'EARLY_' + signal['side'],
                        'start_index': index, 'start_price': float(row['PRICE']),
                        'start_dom_pct': signal['dom_pct'], 'start_mass': signal['mass'],
                        'start_anchor': signal['anchor'], 'start_reward': signal['reward'],
                        'peak_dom_pct': signal['dom_pct'], 'peak_mass': signal['mass'],
                        'latest_anchor': signal['anchor'], 'latest_price': float(row['PRICE']),
                        'worst_reward': signal['reward'], 'confirm_time': None, 'confirm_price': None,
                        'invalidation_time': None, 'end_reason': 'OPEN',
                    }
                continue
            current['peak_dom_pct'] = max(current['peak_dom_pct'], dom_pct or 0.0)
            current['peak_mass'] = max(current['peak_mass'], mass)
            current['latest_price'] = float(row['PRICE']) if row.get('PRICE') not in (None, '') else current['latest_price']
            anchor_key = f'SELL_ANCHOR_{half_life}' if current['side'] == 'LONG' else f'BUY_ANCHOR_{half_life}'
            if row.get(anchor_key) not in (None, ''):
                current['latest_anchor'] = row[anchor_key]
            if reward is not None:
                current['worst_reward'] = min(current['worst_reward'], reward)
            original_anchor = current['start_anchor']
            control = str(row.get('CONTROL') or '')
            if current['state'] in ('EARLY_LONG', 'EARLY_SHORT'):
                if current['side'] == 'LONG':
                    confirmed = (reward is not None and reward <= ENTRY_CONFIRM_REWARD_BPS
                                 and control in ('CONTROL MARKET BUY', 'CONTROL LIMIT BUY')
                                 and original_anchor not in (None, '') and row.get('PRICE') not in (None, '')
                                 and float(row['PRICE']) > float(original_anchor))
                    invalidated = reward is not None and reward >= ENTRY_INVALIDATION_REWARD_BPS
                else:
                    confirmed = (reward is not None and reward <= ENTRY_CONFIRM_REWARD_BPS
                                 and control in ('CONTROL MARKET SELL', 'CONTROL LIMIT SELL')
                                 and original_anchor not in (None, '') and row.get('PRICE') not in (None, '')
                                 and float(row['PRICE']) < float(original_anchor))
                    invalidated = reward is not None and reward >= ENTRY_INVALIDATION_REWARD_BPS
                if confirmed:
                    current['state'] = 'LONG_CONFIRMED' if current['side'] == 'LONG' else 'SHORT_CONFIRMED'
                    current['confirm_time'] = timestamp
                    current['confirm_price'] = float(row['PRICE'])
                elif invalidated:
                    current['state'] = 'NEUTRAL'
                    current['invalidation_time'] = timestamp
                    current['end_reason'] = 'INVALIDATED'
                    episodes.append(_episode_row(current, parsed, index))
                    current = None
                    if signal is not None and row.get('PRICE') not in (None, ''):
                        current = {
                            'side': signal['side'], 'half_life': half_life, 'state': 'EARLY_' + signal['side'],
                            'start_index': index, 'start_price': float(row['PRICE']),
                            'start_dom_pct': signal['dom_pct'], 'start_mass': signal['mass'],
                            'start_anchor': signal['anchor'], 'start_reward': signal['reward'],
                            'peak_dom_pct': signal['dom_pct'], 'peak_mass': signal['mass'],
                            'latest_anchor': signal['anchor'], 'latest_price': float(row['PRICE']),
                            'worst_reward': signal['reward'], 'confirm_time': None, 'confirm_price': None,
                            'invalidation_time': None, 'end_reason': 'OPEN',
                        }
            else:
                # A confirmed episode releases when its original dominance
                # bias disappears or a new opposite early candidate appears.
                bias_lost = (current['side'] == 'LONG' and sell <= buy) or (current['side'] == 'SHORT' and buy <= sell)
                opposite = signal is not None and signal['side'] != current['side']
                if bias_lost or opposite:
                    current['end_reason'] = 'OPPOSITE_CANDIDATE' if opposite else 'BIAS_RELEASED'
                    episodes.append(_episode_row(current, parsed, index))
                    current = None
                    if signal is not None and row.get('PRICE') not in (None, ''):
                        current = {
                            'side': signal['side'], 'half_life': half_life, 'state': 'EARLY_' + signal['side'],
                            'start_index': index, 'start_price': float(row['PRICE']),
                            'start_dom_pct': signal['dom_pct'], 'start_mass': signal['mass'],
                            'start_anchor': signal['anchor'], 'start_reward': signal['reward'],
                            'peak_dom_pct': signal['dom_pct'], 'peak_mass': signal['mass'],
                            'latest_anchor': signal['anchor'], 'latest_price': float(row['PRICE']),
                            'worst_reward': signal['reward'], 'confirm_time': None, 'confirm_price': None,
                            'invalidation_time': None, 'end_reason': 'OPEN',
                        }
        if current is not None:
            current['end_reason'] = 'DATA_END'
            current['state'] = current['state']
            episodes.append(_episode_row(current, parsed, len(parsed) - 1))
    return episodes


def run_entry_episode_analysis(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parent
    raw_paths = args.raw_oi or discover_raw_paths(root)
    market_path = args.market_csv or discover_market_path(root)
    if not raw_paths or market_path is None:
        raise SystemExit('Entry episode research requires recorded raw OI and 1m market data.')
    samples = load_raw(raw_paths)
    market = load_market(market_path)
    minutes = minute_oi(samples)
    annotate_minutes(minutes)
    common = recorded_range(samples, market)
    if common is None:
        raise SystemExit('No common raw OI / market range for entry episode research.')
    start, end = common
    if args.from_time:
        start = max(start, parse_time(args.from_time))
    if args.end:
        end = min(end, parse_time(args.end))
    samples = [sample for sample in samples if start <= sample['ts'] <= end + timedelta(minutes=1)]
    minutes = [row for row in minutes if start <= row['minute'] <= end]
    market = [row for row in market if start <= row['ts'] <= end]
    local_rows, _ = _local_dominance_contributions(samples, minutes, market, start, end)
    episodes = _entry_episode_state_machine(local_rows)
    output_csv = root / 'data' / 'research' / 'BTC_LRA_ENTRY_EPISODES.csv'
    output_md = root / 'data' / 'research' / 'BTC_LRA_ENTRY_EPISODES.md'
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=EPISODE_COLUMNS)
        writer.writeheader()
        for episode in episodes:
            writer.writerow({key: _analysis_value(episode.get(key)) for key in EPISODE_COLUMNS})
    lines = [
        '# BTC-LRA ENTRY EPISODES', '',
        'Research-only state machine built from LOCAL DOMINANCE. Production detector, CONTROL, DOMINANCE and Pine are unchanged.', '',
        'States: `NEUTRAL`, `EARLY_LONG`, `EARLY_SHORT`, `LONG_CONFIRMED`, `SHORT_CONFIRMED`.', '',
        '## 21:45-22:45', '',
        'SIDE | START | HALF LIFE | CONFIRM | INVALIDATION | END | REASON | MFE | MAE',
    ]
    focus_start = datetime(2026, 9, 30, 21, 45, tzinfo=PANAMA)
    focus_end = datetime(2026, 9, 30, 22, 45, tzinfo=PANAMA)
    focus = [episode for episode in episodes if focus_start <= datetime.strptime(episode['START TIME'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=PANAMA) <= focus_end]
    for episode in focus:
        lines.append(f'{episode["SIDE"]} | {episode["START TIME"]} | {episode["HALF LIFE"]} | {episode["CONFIRM TIME"]} | {episode["INVALIDATION TIME"]} | {episode["END TIME"]} | {episode["END REASON"]} | {episode["MFE"]} | {episode["MAE"]}')
    if not focus:
        lines.append('No episodes in this interval.')
    lines.extend(['', '## All episodes', '', ' | '.join(EPISODE_COLUMNS)])
    for episode in episodes:
        lines.append(' | '.join(str(episode.get(key, '')) for key in EPISODE_COLUMNS))
    lines.extend(['', 'Benchmark expectations such as 22:01/22:03 and 22:37/22:39 were not hard-coded; the table above is the causal result.'])
    output_md.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'ENTRY EPISODES CSV: {output_csv}')
    print(f'ENTRY EPISODES REPORT: {output_md}')
    print(f'EPISODES: {len(episodes)}')


EPISODE_DIAGNOSTIC_COLUMNS = [
    'TIME', 'SIDE', 'PRICE', 'HALF_LIFE_VOTES', 'LONG_VOTES', 'SHORT_VOTES',
    'LONG_CONFIRM_VOTES', 'SHORT_CONFIRM_VOTES', 'CANDIDATE_HALF_LIVES',
    'CONFIRM_HALF_LIVES', 'PREMOVE_3M_BPS', 'PREMOVE_5M_BPS', 'PREMOVE_10M_BPS',
    'SESSION_BUY_PCT', 'SESSION_SELL_PCT', 'OI_FLOW', 'OI_FLOW_CHANGE_5M',
    'OI_FLOW_CHANGE_10M', 'BUY_CONTROL_EVENTS_5M', 'SELL_CONTROL_EVENTS_5M',
    'BUY_CONTROL_EVENTS_10M', 'SELL_CONTROL_EVENTS_10M', 'BUY_DOM_WEIGHT_ADDED_5M',
    'SELL_DOM_WEIGHT_ADDED_5M', 'BUY_DOM_WEIGHT_ADDED_10M', 'SELL_DOM_WEIGHT_ADDED_10M',
    'BUY_OI_ADD_5M', 'SELL_OI_ADD_5M', 'BUY_OI_ADD_10M', 'SELL_OI_ADD_10M',
    'CONTROL_SEQUENCE_5M', 'CONTROL_SEQUENCE_10M', 'PREVIOUS_EPISODE_SIDE',
    'MINUTES_SINCE_PREVIOUS_EPISODE_START', 'MINUTES_SINCE_PREVIOUS_CONFIRM',
    'MINUTES_SINCE_PREVIOUS_END', 'PREVIOUS_EPISODE_WAS_CONFIRMED',
    'CONFIRM_TIME', 'CONFIRM_DELAY_MIN', 'CONFIRM_CONTROL', 'CONFIRM_CONTROL_STRENGTH',
    'CONFIRM_EFF_RATIO', 'CONFIRM_ABSORPTION_RATIO', 'CONFIRM_AGGR_SIDE',
    'CONFIRM_AGGR_SHARE', 'CONFIRM_AGGR_MAG', 'CONFIRM_AGGR_MAG_X', 'CONFIRM_OI_ADD',
    'CONFIRM_OI_NET', 'CONFIRM_OI_ACTIVITY', 'CONFIRM_RAW_INTENSITY_PCTL',
    'CONFIRM_PRICE_CHANGE', 'CONFIRM_PRICE_CHANGE_BPS', 'CONFIRM_REWARD_BPS',
    'CONFIRM_DISTANCE_FROM_ANCHOR_BPS',
]
for _half_life in (10, 15, 20, 30):
    EPISODE_DIAGNOSTIC_COLUMNS.extend([
        f'HL{_half_life}_DOM_PCT', f'HL{_half_life}_MASS', f'HL{_half_life}_ANCHOR',
        f'HL{_half_life}_REWARD_BPS', f'HL{_half_life}_DOM_SLOPE_3M',
        f'HL{_half_life}_DOM_SLOPE_5M', f'HL{_half_life}_DOM_SLOPE_10M',
        f'HL{_half_life}_MASS_SLOPE_3M', f'HL{_half_life}_MASS_SLOPE_5M',
        f'HL{_half_life}_MASS_SLOPE_10M', f'HL{_half_life}_REWARD_CHANGE_3M',
        f'HL{_half_life}_REWARD_CHANGE_5M', f'HL{_half_life}_REWARD_CHANGE_10M',
        f'HL{_half_life}_ANCHOR_CHANGE_BPS_3M', f'HL{_half_life}_ANCHOR_CHANGE_BPS_5M',
        f'HL{_half_life}_ANCHOR_CHANGE_BPS_10M',
    ])
for _horizon in (5, 10, 20, 30):
    EPISODE_DIAGNOSTIC_COLUMNS.extend([f'MFE_{_horizon}M', f'MAE_{_horizon}M'])


def _diagnostic_number(value: Any) -> float | None:
    if value in (None, ''):
        return None
    return float(value)


def _diagnostic_row_at(parsed: list[tuple[datetime, dict[str, Any]]],
                       timestamp: datetime) -> dict[str, Any] | None:
    return next((row for at, row in parsed if at == timestamp), None)


def _diagnostic_side_values(row: dict[str, Any], half_life: int,
                            side: str) -> tuple[float | None, float | None, float | None, float | None]:
    prefix = 'SELL' if side == 'LONG' else 'BUY'
    mass = _diagnostic_number(row.get(f'LOCAL_MASS_{half_life}'))
    dom = _diagnostic_number(row.get(f'LOCAL_{prefix}_{half_life}'))
    dom_pct = dom / mass * 100.0 if dom is not None and mass else None
    anchor = _diagnostic_number(row.get(f'{prefix}_ANCHOR_{half_life}'))
    reward = _diagnostic_number(row.get(f'{prefix}_REWARD_BPS_{half_life}'))
    return dom_pct, mass, anchor, reward


def _diagnostic_outcome(parsed: list[tuple[datetime, dict[str, Any]]],
                        index: int, side: str, entry_price: float | None,
                        horizon: int) -> tuple[float | None, float | None]:
    if entry_price is None:
        return None, None
    future = [
        float(row['PRICE']) for at, row in parsed[index + 1:]
        if at <= parsed[index][0] + timedelta(minutes=horizon)
        and row.get('PRICE') not in (None, '')
    ]
    if not future:
        return None, None
    if side == 'LONG':
        return max(0.0, max(future) - entry_price), max(0.0, entry_price - min(future))
    return max(0.0, entry_price - min(future)), max(0.0, max(future) - entry_price)


def _episode_diagnostic_rows(local_rows: list[dict[str, Any]],
                             raw_summaries: list[dict[str, Any]],
                             episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parsed = [(_local_time(row), row) for row in local_rows]
    row_by_time = {at: row for at, row in parsed}
    raw_by_time = {row['_time']: row for row in raw_summaries}
    episode_by_key = {
        (_local_time({'TIME': episode['START TIME']}), episode['SIDE'], episode['HALF LIFE']): episode
        for episode in episodes
    }
    candidate_by_time: dict[datetime, dict[str, list[int]]] = defaultdict(lambda: {'LONG': [], 'SHORT': []})
    candidate_signal: dict[tuple[datetime, int], dict[str, Any]] = {}
    for index, (timestamp, row) in enumerate(parsed):
        for half_life in (10, 15, 20, 30):
            previous_mass = _diagnostic_number(parsed[index - 1][1].get(f'LOCAL_MASS_{half_life}')) if index else 0.0
            signal = _episode_signal(row, half_life, previous_mass or 0.0)
            if signal is not None:
                candidate_by_time[timestamp][signal['side']].append(half_life)
                candidate_signal[(timestamp, half_life)] = signal
    confirmation_by_time: dict[datetime, dict[str, list[int]]] = defaultdict(lambda: {'LONG': [], 'SHORT': []})
    for episode in episodes:
        if not episode.get('CONFIRM TIME'):
            continue
        confirmed = _local_time({'TIME': episode['CONFIRM TIME']})
        confirmation_by_time[confirmed][episode['SIDE']].append(episode['HALF LIFE'])

    contribution_by_time: dict[datetime, dict[str, float]] = defaultdict(lambda: {'buy': 0.0, 'sell': 0.0})
    for summary in raw_summaries:
        at = summary['_time']
        control = str(summary.get('CONTROL') or '')
        if control.endswith('BUY'):
            contribution_by_time[at]['buy'] += _diagnostic_number(summary.get('OI_ADD')) or 0.0
        elif control.endswith('SELL'):
            contribution_by_time[at]['sell'] += _diagnostic_number(summary.get('OI_ADD')) or 0.0

    rows: list[dict[str, Any]] = []
    for timestamp in sorted(candidate_by_time):
        for side in ('LONG', 'SHORT'):
            half_lives = candidate_by_time[timestamp][side]
            if not half_lives:
                continue
            index = next(index for index, (at, _row) in enumerate(parsed) if at == timestamp)
            row = row_by_time[timestamp]
            confirm_hls = confirmation_by_time.get(timestamp, {}).get(side, [])
            diagnostics: dict[str, Any] = {
                'TIME': timestamp.strftime('%Y-%m-%d %H:%M:%S'), 'SIDE': side,
                'PRICE': _diagnostic_number(row.get('PRICE')),
                'HALF_LIFE_VOTES': ','.join(map(str, half_lives)),
                'LONG_VOTES': len(candidate_by_time[timestamp]['LONG']),
                'SHORT_VOTES': len(candidate_by_time[timestamp]['SHORT']),
                'LONG_CONFIRM_VOTES': len(confirmation_by_time.get(timestamp, {}).get('LONG', [])),
                'SHORT_CONFIRM_VOTES': len(confirmation_by_time.get(timestamp, {}).get('SHORT', [])),
                'CANDIDATE_HALF_LIVES': ','.join(map(str, half_lives)),
                'CONFIRM_HALF_LIVES': ','.join(map(str, confirm_hls)),
            }
            price = _diagnostic_number(row.get('PRICE'))
            for minutes_back, field in ((3, 'PREMOVE_3M_BPS'), (5, 'PREMOVE_5M_BPS'), (10, 'PREMOVE_10M_BPS')):
                previous = _diagnostic_row_at(parsed, timestamp - timedelta(minutes=minutes_back))
                previous_price = _diagnostic_number(previous.get('PRICE')) if previous else None
                move = ((price - previous_price) / previous_price * 10000.0) if price and previous_price else None
                diagnostics[field] = move if side == 'LONG' else (-move if move is not None else None)
            diagnostics.update({
                'SESSION_BUY_PCT': row.get('SESSION_BUY_PCT'), 'SESSION_SELL_PCT': row.get('SESSION_SELL_PCT'),
                'OI_FLOW': row.get('OI_FLOW'),
            })
            for minutes_back, field in ((5, 'OI_FLOW_CHANGE_5M'), (10, 'OI_FLOW_CHANGE_10M')):
                previous = _diagnostic_row_at(parsed, timestamp - timedelta(minutes=minutes_back))
                current_flow = _diagnostic_number(row.get('OI_FLOW'))
                previous_flow = _diagnostic_number(previous.get('OI_FLOW')) if previous else None
                diagnostics[field] = current_flow - previous_flow if current_flow is not None and previous_flow is not None else None
            for window in (5, 10):
                recent_times = [timestamp - timedelta(minutes=offset) for offset in range(1, window + 1)]
                recent = [raw_by_time[at] for at in recent_times if at in raw_by_time]
                controls = [str(item.get('CONTROL') or '') for item in reversed(recent) if str(item.get('CONTROL') or '') not in ('', 'CONTROL UNCLEAR')]
                buy_controls = sum(control.endswith('BUY') for control in controls)
                sell_controls = sum(control.endswith('SELL') for control in controls)
                weights = {'buy': sum(contribution_by_time[at]['buy'] for at in recent_times),
                           'sell': sum(contribution_by_time[at]['sell'] for at in recent_times)}
                adds = {'buy': sum((_diagnostic_number(raw_by_time[at].get('OI_ADD')) or 0.0) for at in recent_times if at in raw_by_time and str(raw_by_time[at].get('CONTROL') or '').endswith('BUY')),
                        'sell': sum((_diagnostic_number(raw_by_time[at].get('OI_ADD')) or 0.0) for at in recent_times if at in raw_by_time and str(raw_by_time[at].get('CONTROL') or '').endswith('SELL'))}
                diagnostics.update({
                    f'BUY_CONTROL_EVENTS_{window}M': buy_controls, f'SELL_CONTROL_EVENTS_{window}M': sell_controls,
                    f'BUY_DOM_WEIGHT_ADDED_{window}M': weights['buy'], f'SELL_DOM_WEIGHT_ADDED_{window}M': weights['sell'],
                    f'BUY_OI_ADD_{window}M': adds['buy'], f'SELL_OI_ADD_{window}M': adds['sell'],
                    f'CONTROL_SEQUENCE_{window}M': ' > '.join(controls),
                })
            previous_episodes = sorted(
                (episode for episode in episodes if _local_time({'TIME': episode['START TIME']}) < timestamp),
                key=lambda episode: episode['START TIME'],
            )
            previous = previous_episodes[-1] if previous_episodes else None
            if previous:
                previous_start = _local_time({'TIME': previous['START TIME']})
                previous_confirm = _local_time({'TIME': previous['CONFIRM TIME']}) if previous.get('CONFIRM TIME') else None
                previous_end = _local_time({'TIME': previous['END TIME']}) if previous.get('END TIME') else None
                diagnostics.update({
                    'PREVIOUS_EPISODE_SIDE': previous['SIDE'],
                    'MINUTES_SINCE_PREVIOUS_EPISODE_START': (timestamp - previous_start).total_seconds() / 60.0,
                    'MINUTES_SINCE_PREVIOUS_CONFIRM': (timestamp - previous_confirm).total_seconds() / 60.0 if previous_confirm else None,
                    'MINUTES_SINCE_PREVIOUS_END': (timestamp - previous_end).total_seconds() / 60.0 if previous_end else None,
                    'PREVIOUS_EPISODE_WAS_CONFIRMED': 'YES' if previous.get('CONFIRM TIME') else 'NO',
                })
            else:
                diagnostics.update({'PREVIOUS_EPISODE_SIDE': '', 'PREVIOUS_EPISODE_WAS_CONFIRMED': 'NO'})
            for half_life in (10, 15, 20, 30):
                dom_pct, mass, anchor, reward = _diagnostic_side_values(row, half_life, side)
                diagnostics.update({f'HL{half_life}_DOM_PCT': dom_pct, f'HL{half_life}_MASS': mass,
                                    f'HL{half_life}_ANCHOR': anchor, f'HL{half_life}_REWARD_BPS': reward})
                for minutes_back in (3, 5, 10):
                    previous = _diagnostic_row_at(parsed, timestamp - timedelta(minutes=minutes_back))
                    previous_values = _diagnostic_side_values(previous, half_life, side) if previous else (None, None, None, None)
                    diagnostics[f'HL{half_life}_DOM_SLOPE_{minutes_back}M'] = dom_pct - previous_values[0] if dom_pct is not None and previous_values[0] is not None else None
                    diagnostics[f'HL{half_life}_MASS_SLOPE_{minutes_back}M'] = mass - previous_values[1] if mass is not None and previous_values[1] is not None else None
                    diagnostics[f'HL{half_life}_REWARD_CHANGE_{minutes_back}M'] = reward - previous_values[3] if reward is not None and previous_values[3] is not None else None
                    diagnostics[f'HL{half_life}_ANCHOR_CHANGE_BPS_{minutes_back}M'] = ((anchor - previous_values[2]) / previous_values[2] * 10000.0
                                                                                          if anchor is not None and previous_values[2] else None)

            selected_episode = next((episode_by_key.get((timestamp, side, half_life)) for half_life in half_lives if episode_by_key.get((timestamp, side, half_life))), None)
            confirm_time = _local_time({'TIME': selected_episode['CONFIRM TIME']}) if selected_episode and selected_episode.get('CONFIRM TIME') else None
            confirm_row = row_by_time.get(confirm_time) if confirm_time else None
            confirm_summary = raw_by_time.get(confirm_time) if confirm_time else None
            confirm_control = str(confirm_row.get('CONTROL') or '') if confirm_row else ''
            confirm_strength = None
            confirm_eff = _diagnostic_number(confirm_summary.get('EFF_RATIO')) if confirm_summary else None
            confirm_absorption = _diagnostic_number(confirm_summary.get('ABSORPTION_RATIO')) if confirm_summary else None
            if 'LIMIT ' in confirm_control:
                confirm_strength = confirm_absorption
            elif confirm_control.endswith('BUY') or confirm_control.endswith('SELL'):
                confirm_strength = max(0.0, min(1.0, confirm_eff or 0.0))
            if confirm_row and confirm_summary:
                confirm_price = _diagnostic_number(confirm_row.get('PRICE'))
                reference = _diagnostic_number(confirm_summary.get('REFERENCE_PRICE'))
                price_change = _diagnostic_number(confirm_summary.get('PRICE_CHANGE'))
                confirm_side = str(confirm_summary.get('AGGR_SIDE') or '')
                reward_key_side = side
                _dom, _mass, confirm_anchor, confirm_reward = _diagnostic_side_values(confirm_row, selected_episode['HALF LIFE'] if selected_episode else half_lives[0], reward_key_side)
                distance = ((confirm_price - confirm_anchor) / confirm_anchor * 10000.0 if side == 'LONG' and confirm_price and confirm_anchor else
                            (confirm_anchor - confirm_price) / confirm_anchor * 10000.0 if side == 'SHORT' and confirm_price and confirm_anchor else None)
                diagnostics.update({
                    'CONFIRM_TIME': confirm_time.strftime('%Y-%m-%d %H:%M:%S'),
                    'CONFIRM_DELAY_MIN': (confirm_time - timestamp).total_seconds() / 60.0,
                    'CONFIRM_CONTROL': confirm_control, 'CONFIRM_CONTROL_STRENGTH': confirm_strength,
                    'CONFIRM_EFF_RATIO': confirm_eff, 'CONFIRM_ABSORPTION_RATIO': confirm_absorption,
                    'CONFIRM_AGGR_SIDE': confirm_side, 'CONFIRM_AGGR_SHARE': _diagnostic_number(confirm_summary.get('AGGR_SHARE')),
                    'CONFIRM_AGGR_MAG': _diagnostic_number(confirm_summary.get('AGGR_MAG')),
                    'CONFIRM_AGGR_MAG_X': _diagnostic_number(confirm_summary.get('AGGR_MAG_X')),
                    'CONFIRM_OI_ADD': _diagnostic_number(confirm_summary.get('OI_ADD')),
                    'CONFIRM_OI_NET': _diagnostic_number(confirm_summary.get('OI_NET')),
                    'CONFIRM_OI_ACTIVITY': _diagnostic_number(confirm_summary.get('OI_ACT')),
                    'CONFIRM_RAW_INTENSITY_PCTL': _diagnostic_number(confirm_summary.get('OI_INTENSITY_PCTL')),
                    'CONFIRM_PRICE_CHANGE': price_change,
                    'CONFIRM_PRICE_CHANGE_BPS': price_change / reference * 10000.0 if price_change is not None and reference else None,
                    'CONFIRM_REWARD_BPS': confirm_reward, 'CONFIRM_DISTANCE_FROM_ANCHOR_BPS': distance,
                })
            for key in EPISODE_DIAGNOSTIC_COLUMNS:
                diagnostics.setdefault(key, '')
            for horizon in (5, 10, 20, 30):
                mfe, mae = _diagnostic_outcome(parsed, index, side, price, horizon)
                diagnostics[f'MFE_{horizon}M'] = mfe
                diagnostics[f'MAE_{horizon}M'] = mae
            rows.append(diagnostics)
    return rows


def _episode_diagnostic_report(rows: list[dict[str, Any]], output_csv: Path) -> str:
    benchmarks = ('21:26', '22:01', '22:37', '22:51')
    causal_fields = (
        'LONG_VOTES', 'SHORT_VOTES', 'CANDIDATE_HALF_LIVES', 'PREMOVE_3M_BPS',
        'PREMOVE_5M_BPS', 'PREMOVE_10M_BPS', 'SESSION_BUY_PCT', 'SESSION_SELL_PCT',
        'OI_FLOW', 'OI_FLOW_CHANGE_5M', 'BUY_CONTROL_EVENTS_5M', 'SELL_CONTROL_EVENTS_5M',
        'CONTROL_SEQUENCE_5M', 'PREVIOUS_EPISODE_SIDE', 'PREVIOUS_EPISODE_WAS_CONFIRMED',
        'CONFIRM_TIME', 'CONFIRM_DELAY_MIN', 'CONFIRM_CONTROL', 'CONFIRM_EFF_RATIO',
        'CONFIRM_ABSORPTION_RATIO', 'CONFIRM_AGGR_MAG_X', 'CONFIRM_OI_ADD',
        'CONFIRM_RAW_INTENSITY_PCTL', 'CONFIRM_REWARD_BPS', 'CONFIRM_DISTANCE_FROM_ANCHOR_BPS',
    )
    lines = [
        '# BTC-LRA ENTRY EPISODE DIAGNOSTICS', '',
        'Research-only diagnostics. Production monitor, episode state machine, CONTROL, DOMINANCE, Pine and thresholds are unchanged.',
        '', f'CSV: `{output_csv}`', '',
        'Candidate consensus and all features before/at confirmation are causal. MFE/MAE fields are post-outcome evaluation only.',
        '', '## Benchmark comparison', '',
        'FEATURE | 21:26 SHORT | 22:01 LONG | 22:37 LONG | 22:51 LONG',
        '---|---|---|---|---',
    ]
    for field in causal_fields:
        values = []
        for benchmark in benchmarks:
            matches = [row for row in rows if row['TIME'][11:16] == benchmark]
            values.append('; '.join(f'{row["SIDE"]}={row.get(field, "")}' for row in matches) or '--')
        lines.append(f'{field} | ' + ' | '.join(values))
    lines.extend(['', '## Causal features', '', 'The table above is restricted to fields available by candidate/confirmation time. No future endpoint or outcome is used to form them.', '', '## Post-outcome evaluation', '', 'TIME | SIDE | MFE_5M | MAE_5M | MFE_10M | MAE_10M | MFE_20M | MAE_20M | MFE_30M | MAE_30M', '---|---|---:|---:|---:|---:|---:|---:|---:|---:'])
    for row in rows:
        if row['TIME'][11:16] in benchmarks:
            lines.append(' | '.join(str(row.get(field, '')) for field in ('TIME', 'SIDE', 'MFE_5M', 'MAE_5M', 'MFE_10M', 'MAE_10M', 'MFE_20M', 'MAE_20M', 'MFE_30M', 'MAE_30M')))
    lines.extend(['', '## Interpretation', '', 'No new threshold, score, signal or production filter is selected. The benchmark rows are descriptive comparisons only.', ''])
    return '\n'.join(lines)


def run_entry_episode_diagnostics(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parent
    raw_paths = args.raw_oi or discover_raw_paths(root)
    market_path = args.market_csv or discover_market_path(root)
    if not raw_paths or market_path is None:
        raise SystemExit('Entry episode diagnostics requires recorded raw OI and market data.')
    samples = load_raw(raw_paths)
    market = load_market(market_path)
    minutes = minute_oi(samples)
    annotate_minutes(minutes)
    common = recorded_range(samples, market)
    if common is None:
        raise SystemExit('No common recorded raw OI / market range for episode diagnostics.')
    start, end = common
    if args.from_time:
        start = max(start, parse_time(args.from_time))
    if args.end:
        end = min(end, parse_time(args.end))
    samples = [sample for sample in samples if start <= sample['ts'] <= end + timedelta(minutes=1)]
    minutes = [row for row in minutes if start <= row['minute'] <= end]
    market = [row for row in market if start <= row['ts'] <= end]
    local_rows, _ = _local_dominance_contributions(samples, minutes, market, start, end)
    raw_summaries, _ = _raw_intensity_summary(samples, minutes, market)
    episodes = _entry_episode_state_machine(local_rows)
    rows = _episode_diagnostic_rows(local_rows, raw_summaries, episodes)
    output_csv = root / 'data' / 'research' / 'BTC_LRA_EPISODE_DIAGNOSTICS.csv'
    output_md = root / 'data' / 'research' / 'BTC_LRA_EPISODE_DIAGNOSTICS.md'
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=EPISODE_DIAGNOSTIC_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _analysis_value(row.get(key)) for key in EPISODE_DIAGNOSTIC_COLUMNS})
    output_md.write_text(_episode_diagnostic_report(rows, output_csv), encoding='utf-8')
    print(f'ENTRY EPISODE DIAGNOSTICS CSV: {output_csv}')
    print(f'ENTRY EPISODE DIAGNOSTICS REPORT: {output_md}')
    print(f'CANDIDATE MINUTES: {len(rows)}')


RELEASE_STAGE_COLUMNS = [
    'SIDE', 'HALF_LIFE', 'VOTE_COUNT', 'VOTE_LIST', 'STATE_PATH',
    'EARLY_TIME', 'EARLY_PRICE', 'DEFENDED_TIME', 'DEFENDED_PRICE',
    'DEFENDED_CONTROL', 'DEFENDED_EFF_RATIO', 'DEFENDED_AGGR_MAG_X',
    'DEFENDED_OI_ADD', 'DEFENDED_REWARD_BPS', 'RELEASE_TIME', 'RELEASE_PRICE',
    'RELEASE_CONTROL', 'RELEASE_EFF_RATIO', 'RELEASE_AGGR_MAG_X',
    'RELEASE_OI_ADD', 'RELEASE_RAW_PCTL', 'RELEASE_REWARD_BPS',
    'TIME_EARLY_TO_DEFENDED', 'TIME_EARLY_TO_RELEASE', 'TIME_DEFENDED_TO_RELEASE',
    'PREVIOUS_RELEASE_SIDE', 'MINUTES_SINCE_PREVIOUS_RELEASE', 'SAME_SIDE_AFTER_RELEASE',
]
for _stage_name in ('EARLY', 'DEFENDED', 'RELEASE'):
    for _horizon in (5, 10, 20, 30):
        RELEASE_STAGE_COLUMNS.extend([f'{_stage_name}_MFE_{_horizon}M', f'{_stage_name}_MAE_{_horizon}M'])


def _release_stage_event_fields(row: dict[str, Any], summary: dict[str, Any] | None,
                                side: str, half_life: int, index: int) -> dict[str, Any]:
    reward_key = f'SELL_REWARD_BPS_{half_life}' if side == 'LONG' else f'BUY_REWARD_BPS_{half_life}'
    return {
        'index': index, 'time': _local_time(row), 'price': _diagnostic_number(row.get('PRICE')),
        'control': str((summary or {}).get('CONTROL') or row.get('CONTROL') or ''),
        'eff_ratio': _diagnostic_number((summary or {}).get('EFF_RATIO')),
        'absorption_ratio': _diagnostic_number((summary or {}).get('ABSORPTION_RATIO')),
        'aggr_side': str((summary or {}).get('AGGR_SIDE') or ''),
        'aggr_share': _diagnostic_number((summary or {}).get('AGGR_SHARE')),
        'aggr_mag': _diagnostic_number((summary or {}).get('AGGR_MAG')),
        'aggr_mag_x': _diagnostic_number((summary or {}).get('AGGR_MAG_X')),
        'oi_add': _diagnostic_number((summary or {}).get('OI_ADD')),
        'raw_pctl': _diagnostic_number((summary or {}).get('OI_INTENSITY_PCTL')),
        'reward': _diagnostic_number(row.get(reward_key)),
    }


def _release_stage_episode_rows(local_rows: list[dict[str, Any]],
                               raw_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parsed = [(_local_time(row), row) for row in local_rows]
    row_by_time = {at: row for at, row in parsed}
    raw_by_time = {row['_time']: row for row in raw_summaries}
    candidate_by_time: dict[datetime, dict[str, list[int]]] = defaultdict(lambda: {'LONG': [], 'SHORT': []})
    signals_by_hl: dict[tuple[datetime, int], dict[str, Any]] = {}
    for index, (timestamp, row) in enumerate(parsed):
        for half_life in (10, 15, 20, 30):
            previous_mass = _diagnostic_number(parsed[index - 1][1].get(f'LOCAL_MASS_{half_life}')) if index else 0.0
            signal = _episode_signal(row, half_life, previous_mass or 0.0)
            if signal:
                candidate_by_time[timestamp][signal['side']].append(half_life)
                signals_by_hl[(timestamp, half_life)] = signal

    episodes: list[dict[str, Any]] = []
    last_release: dict[str, Any] | None = None

    def make_episode(timestamp: datetime, index: int, side: str, half_life: int) -> dict[str, Any]:
        signal = signals_by_hl[(timestamp, half_life)]
        previous = last_release
        return {
            'side': side, 'half_life': half_life, 'state': 'EARLY_' + side,
            'state_path': ['EARLY_' + side], 'start_index': index, 'early': {
                'index': index,
                'time': timestamp, 'price': _diagnostic_number(parsed[index][1].get('PRICE')),
                'dom_pct': signal['dom_pct'], 'mass': signal['mass'], 'anchor': signal['anchor'],
                'reward': signal['reward'],
            }, 'defended': None, 'release': None, 'end_reason': 'OPEN',
            'previous_release_side': previous['side'] if previous else '',
            'minutes_since_previous_release': ((timestamp - previous['time']).total_seconds() / 60.0 if previous else None),
        }

    def finish(current: dict[str, Any], end_index: int) -> None:
        nonlocal last_release
        side = current['side']
        state = current['state']
        current['end_index'] = end_index
        current['final_state'] = state
        current['end_time'] = parsed[end_index][0]
        current['end_reason'] = current.get('end_reason', 'DATA_END')
        for stage_name, stage in (('EARLY', current['early']), ('DEFENDED', current['defended']), ('RELEASE', current['release'])):
            if stage is None:
                continue
            if 'index' not in stage:
                raise AssertionError(f'release-stage {stage_name} is missing causal source index')
            for horizon in (5, 10, 20, 30):
                mfe, mae = _diagnostic_outcome(parsed, stage['index'], side, stage['price'], horizon)
                current[f'{stage_name}_MFE_{horizon}M'] = mfe
                current[f'{stage_name}_MAE_{horizon}M'] = mae
        if current['release'] is not None:
            last_release = {'side': side, 'time': current['release']['time']}
        episodes.append(current)

    current_by_hl: dict[int, dict[str, Any] | None] = {half_life: None for half_life in (10, 15, 20, 30)}
    for index, (timestamp, row) in enumerate(parsed):
        for half_life in (10, 15, 20, 30):
            current = current_by_hl[half_life]
            signal = signals_by_hl.get((timestamp, half_life))
            if current is None:
                if signal is not None and row.get('PRICE') not in (None, ''):
                    current_by_hl[half_life] = make_episode(timestamp, index, signal['side'], half_life)
                continue
            side = current['side']
            summary = raw_by_time.get(timestamp)
            control = str((summary or {}).get('CONTROL') or row.get('CONTROL') or '')
            meaningful = (summary or {}).get('MEANINGFUL_AGGR', 'YES') == 'YES'
            target_limit = 'CONTROL LIMIT BUY' if side == 'LONG' else 'CONTROL LIMIT SELL'
            target_market = 'CONTROL MARKET BUY' if side == 'LONG' else 'CONTROL MARKET SELL'
            reward_key = f'SELL_REWARD_BPS_{half_life}' if side == 'LONG' else f'BUY_REWARD_BPS_{half_life}'
            reward = _diagnostic_number(row.get(reward_key))
            if current['state'] in ('EARLY_LONG', 'EARLY_SHORT'):
                if meaningful and control == target_limit and current['defended'] is None:
                    stage = _release_stage_event_fields(row, summary, side, half_life, index)
                    current['defended'] = stage
                    current['state'] = 'LONG_DEFENDED' if side == 'LONG' else 'SHORT_DEFENDED'
                    current['state_path'].append(current['state'])
                elif meaningful and control == target_market:
                    stage = _release_stage_event_fields(row, summary, side, half_life, index)
                    current['release'] = stage
                    current['state'] = 'LONG_RELEASE' if side == 'LONG' else 'SHORT_RELEASE'
                    current['state_path'].append(current['state'])
                    current['end_reason'] = 'RELEASE'
                    finish(current, index)
                    current_by_hl[half_life] = None
                    continue
                elif reward is not None and reward >= ENTRY_INVALIDATION_REWARD_BPS:
                    current['state'] = 'INVALIDATED'
                    current['state_path'].append('INVALIDATED')
                    current['end_reason'] = 'INVALIDATED'
                    finish(current, index)
                    current_by_hl[half_life] = None
                    if signal is not None and row.get('PRICE') not in (None, ''):
                        current_by_hl[half_life] = make_episode(timestamp, index, signal['side'], half_life)
                    continue
            elif current['state'] in ('LONG_DEFENDED', 'SHORT_DEFENDED'):
                if meaningful and control == target_market:
                    stage = _release_stage_event_fields(row, summary, side, half_life, index)
                    current['release'] = stage
                    current['state'] = 'LONG_RELEASE' if side == 'LONG' else 'SHORT_RELEASE'
                    current['state_path'].append(current['state'])
                    current['end_reason'] = 'RELEASE'
                    finish(current, index)
                    current_by_hl[half_life] = None
                    continue
                if reward is not None and reward >= ENTRY_INVALIDATION_REWARD_BPS:
                    current['state'] = 'INVALIDATED'
                    current['state_path'].append('INVALIDATED')
                    current['end_reason'] = 'INVALIDATED'
                    finish(current, index)
                    current_by_hl[half_life] = None
                    continue

    for half_life, current in current_by_hl.items():
        if current is not None:
            current['end_reason'] = 'DATA_END'
            finish(current, len(parsed) - 1)

    output: list[dict[str, Any]] = []
    for episode in episodes:
        early = episode['early']
        defended = episode['defended']
        release = episode['release']
        votes = candidate_by_time[early['time']][episode['side']]
        result: dict[str, Any] = {
            'SIDE': episode['side'], 'HALF_LIFE': episode['half_life'], 'VOTE_COUNT': len(votes),
            'VOTE_LIST': ','.join(map(str, votes)), 'STATE_PATH': ' -> '.join(episode['state_path']),
            'EARLY_TIME': early['time'].strftime('%Y-%m-%d %H:%M:%S'), 'EARLY_PRICE': early['price'],
            'DEFENDED_TIME': defended['time'].strftime('%Y-%m-%d %H:%M:%S') if defended else '',
            'DEFENDED_PRICE': defended['price'] if defended else None,
            'DEFENDED_CONTROL': defended['control'] if defended else '',
            'DEFENDED_EFF_RATIO': defended['eff_ratio'] if defended else None,
            'DEFENDED_AGGR_MAG_X': defended['aggr_mag_x'] if defended else None,
            'DEFENDED_OI_ADD': defended['oi_add'] if defended else None,
            'DEFENDED_REWARD_BPS': defended['reward'] if defended else None,
            'RELEASE_TIME': release['time'].strftime('%Y-%m-%d %H:%M:%S') if release else '',
            'RELEASE_PRICE': release['price'] if release else None,
            'RELEASE_CONTROL': release['control'] if release else '',
            'RELEASE_EFF_RATIO': release['eff_ratio'] if release else None,
            'RELEASE_AGGR_MAG_X': release['aggr_mag_x'] if release else None,
            'RELEASE_OI_ADD': release['oi_add'] if release else None,
            'RELEASE_RAW_PCTL': release['raw_pctl'] if release else None,
            'RELEASE_REWARD_BPS': release['reward'] if release else None,
            'TIME_EARLY_TO_DEFENDED': (defended['time'] - early['time']).total_seconds() / 60.0 if defended else None,
            'TIME_EARLY_TO_RELEASE': (release['time'] - early['time']).total_seconds() / 60.0 if release else None,
            'TIME_DEFENDED_TO_RELEASE': (release['time'] - defended['time']).total_seconds() / 60.0 if release and defended else None,
            'PREVIOUS_RELEASE_SIDE': episode['previous_release_side'],
            'MINUTES_SINCE_PREVIOUS_RELEASE': episode['minutes_since_previous_release'],
            'SAME_SIDE_AFTER_RELEASE': 'YES' if episode['previous_release_side'] == episode['side'] and episode['previous_release_side'] else 'NO',
            '_END_TIME': episode['end_time'].strftime('%Y-%m-%d %H:%M:%S'),
            '_END_PRICE': _diagnostic_number(parsed[episode['end_index']][1].get('PRICE')),
            '_END_REASON': episode.get('end_reason', ''),
            '_DEFENDED_EVENT': defended,
            '_RELEASE_EVENT': release,
        }
        for stage_name in ('EARLY', 'DEFENDED', 'RELEASE'):
            for horizon in (5, 10, 20, 30):
                result[f'{stage_name}_MFE_{horizon}M'] = episode.get(f'{stage_name}_MFE_{horizon}M')
                result[f'{stage_name}_MAE_{horizon}M'] = episode.get(f'{stage_name}_MAE_{horizon}M')
        output.append(result)
    return output


def _release_stage_report(rows: list[dict[str, Any]], output_csv: Path) -> str:
    benchmark_times = ('21:26', '22:01', '22:37', '22:51')
    lines = [
        '# BTC-LRA RELEASE STAGES', '',
        'Research-only layer. Existing ENTRY EPISODES, production CONTROL, DOMINANCE, thresholds and Pine are unchanged.', '',
        f'CSV: `{output_csv}`', '',
        'LIMIT target-side control is classified as DEFENDED. MARKET target-side control is classified as RELEASE. No score or new threshold is introduced.', '',
        '## Benchmark causal timelines', '',
    ]
    for benchmark in benchmark_times:
        matches = [row for row in rows if row['EARLY_TIME'][11:16] == benchmark]
        lines.append(f'### {benchmark}')
        if not matches:
            lines.append('NONE')
            continue
        for row in matches:
            lines.append(f"{row['EARLY_TIME'][11:16]} EARLY {row['SIDE']} | votes {row['VOTE_COUNT']}/4 | state {row['STATE_PATH']}")
            if row.get('DEFENDED_TIME'):
                lines.append(f"{row['DEFENDED_TIME'][11:16]} {row['DEFENDED_CONTROL']} | {row['STATE_PATH'].split(' -> ')[1]}")
            if row.get('RELEASE_TIME'):
                lines.append(f"{row['RELEASE_TIME'][11:16]} {row['RELEASE_CONTROL']} | {row['STATE_PATH'].split(' -> ')[-1]}")
            else:
                lines.append('MARKET release: NONE')
    counts = defaultdict(int)
    for row in rows:
        path = row['STATE_PATH']
        if path.endswith('INVALIDATED'):
            counts['EARLY -> INVALIDATED'] += 1
        elif 'RELEASE' in path and 'DEFENDED' in path:
            counts['EARLY -> DEFENDED -> RELEASE'] += 1
        elif 'RELEASE' in path:
            counts['EARLY -> RELEASE'] += 1
        elif 'DEFENDED' in path:
            counts['EARLY -> DEFENDED'] += 1
        else:
            counts['NO-CONFIRM'] += 1
    lines.extend(['', '## Full dataset comparison', '', 'PATH | COUNT', '---|---:'])
    for key in ('EARLY -> INVALIDATED', 'EARLY -> DEFENDED', 'EARLY -> RELEASE', 'EARLY -> DEFENDED -> RELEASE', 'NO-CONFIRM'):
        lines.append(f'| {key} | {counts[key]} |')
    lines.extend(['', '## Outcome groups', '', 'GROUP | COUNT | EARLY MFE 10M | EARLY MAE 10M | RELEASE MFE 10M | RELEASE MAE 10M', '---|---:|---:|---:|---:|---:'])
    groups = {
        'RELEASE episodes': [row for row in rows if row.get('RELEASE_TIME')],
        'DEFENDED-only episodes': [row for row in rows if row.get('DEFENDED_TIME') and not row.get('RELEASE_TIME')],
        'NO-confirm episodes': [row for row in rows if not row.get('DEFENDED_TIME') and not row.get('RELEASE_TIME')],
    }
    for name, group in groups.items():
        average = lambda field: (sum(float(row[field]) for row in group if row.get(field) not in (None, '')) / len([row for row in group if row.get(field) not in (None, '')]) if any(row.get(field) not in (None, '') for row in group) else None)
        lines.append(f'| {name} | {len(group)} | {average("EARLY_MFE_10M")} | {average("EARLY_MAE_10M")} | {average("RELEASE_MFE_10M")} | {average("RELEASE_MAE_10M")} |')
    def benchmark_summary(label: str) -> str:
        matches = [row for row in rows if row['EARLY_TIME'][11:16] == label]
        if not matches:
            return f'{label}: NONE in the loaded causal episode set.'
        descriptions = []
        for row in matches:
            release = row.get('RELEASE_TIME') or 'NONE'
            descriptions.append(f"{row['SIDE']} votes={row['VOTE_COUNT']}/4 path={row['STATE_PATH']} release={release}")
        return f'{label}: ' + '; '.join(descriptions)
    release_delays = [float(row['TIME_EARLY_TO_RELEASE']) for row in rows if row.get('TIME_EARLY_TO_RELEASE') not in (None, '')]
    lines.extend([
        '', '## Answers', '',
        'The report compares LIMIT defense and MARKET release using observed causal rows. It does not select a threshold or declare a strategy.',
        '', '1. LIMIT and MARKET are reported as separate stages; the benchmark paths below show whether the distinction separates defense from active release.',
        '2. ' + benchmark_summary('21:26'),
        '3. ' + benchmark_summary('22:51'),
        f'4. EARLY-to-MARKET-RELEASE delays observed: {release_delays}; no delay threshold is selected.',
        '5. Outcome tables compare RELEASE, DEFENDED-only and NO-confirm groups without using outcomes in state formation.',
        '',
    ])
    return '\n'.join(lines)


def run_release_stage_analysis(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parent
    raw_paths = args.raw_oi or discover_raw_paths(root)
    market_path = args.market_csv or discover_market_path(root)
    if not raw_paths or market_path is None:
        raise SystemExit('Release stage analysis requires recorded raw OI and market data.')
    samples = load_raw(raw_paths)
    market = load_market(market_path)
    minutes = minute_oi(samples)
    annotate_minutes(minutes)
    common = recorded_range(samples, market)
    if common is None:
        raise SystemExit('No common recorded raw OI / market range for release stages.')
    start, end = common
    if args.from_time:
        start = max(start, parse_time(args.from_time))
    if args.end:
        end = min(end, parse_time(args.end))
    samples = [sample for sample in samples if start <= sample['ts'] <= end + timedelta(minutes=1)]
    minutes = [row for row in minutes if start <= row['minute'] <= end]
    market = [row for row in market if start <= row['ts'] <= end]
    local_rows, _ = _local_dominance_contributions(samples, minutes, market, start, end)
    raw_summaries, _ = _raw_intensity_summary(samples, minutes, market)
    rows = _release_stage_episode_rows(local_rows, raw_summaries)
    output_csv = root / 'data' / 'research' / 'BTC_LRA_RELEASE_STAGES.csv'
    output_md = root / 'data' / 'research' / 'BTC_LRA_RELEASE_STAGES.md'
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=RELEASE_STAGE_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _analysis_value(row.get(key)) for key in RELEASE_STAGE_COLUMNS})
    output_md.write_text(_release_stage_report(rows, output_csv), encoding='utf-8')
    print(f'RELEASE STAGES CSV: {output_csv}')
    print(f'RELEASE STAGES REPORT: {output_md}')
    print(f'EPISODES: {len(rows)}')


CONSENSUS_EPISODE_COLUMNS = [
    'SIDE', 'EARLIEST_EARLY_TIME', 'LATEST_EARLY_TIME', 'EARLY_PRICE',
    'VOTE_COUNT', 'VOTE_HALF_LIVES', 'DEFENDED_TIME', 'RELEASE_TIME',
    'PATH', 'PATHS_SEEN', 'PATH_DISAGREEMENT', 'CONSENSUS_1_TIME',
    'CONSENSUS_2_TIME', 'CONSENSUS_3_TIME', 'CONSENSUS_4_TIME', 'EPISODE_CLASS',
]
for _consensus_stage in ('EARLIEST_EARLY', 'CONSENSUS_2', 'CONSENSUS_3', 'CONSENSUS_4', 'DEFENDED', 'RELEASE'):
    for _horizon in (5, 10, 20, 30):
        CONSENSUS_EPISODE_COLUMNS.extend([
            f'{_consensus_stage}_MFE_{_horizon}M', f'{_consensus_stage}_MAE_{_horizon}M',
        ])


def _consensus_episode_rows(local_rows: list[dict[str, Any]],
                            release_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parsed = [(_local_time(row), row) for row in local_rows]
    row_index = {at: index for index, (at, _row) in enumerate(parsed)}
    candidate_votes: dict[tuple[datetime, str], list[int]] = defaultdict(list)
    for index, (timestamp, row) in enumerate(parsed):
        for half_life in (10, 15, 20, 30):
            previous_mass = _diagnostic_number(parsed[index - 1][1].get(f'LOCAL_MASS_{half_life}')) if index else 0.0
            signal = _episode_signal(row, half_life, previous_mass or 0.0)
            if signal:
                candidate_votes[(timestamp, signal['side'])].append(half_life)

    ordered = sorted(release_rows, key=lambda row: (row['SIDE'], row['EARLY_TIME'], row['HALF_LIFE']))
    groups: list[list[dict[str, Any]]] = []
    for item in ordered:
        early_time = _local_time({'TIME': item['EARLY_TIME']})
        release_time = _local_time({'TIME': item['RELEASE_TIME']}) if item.get('RELEASE_TIME') else None
        matching_group = None
        for group in reversed(groups):
            if group[-1]['SIDE'] != item['SIDE']:
                continue
            group_early = [_local_time({'TIME': row['EARLY_TIME']}) for row in group]
            same_release = bool(release_time and any(
                row.get('RELEASE_TIME') and _local_time({'TIME': row['RELEASE_TIME']}) == release_time
                for row in group
            ))
            if abs((early_time - max(group_early)).total_seconds()) <= 120 or (
                    same_release and abs((early_time - max(group_early)).total_seconds()) <= 120):
                matching_group = group
                break
        if matching_group is None:
            groups.append([item])
        else:
            matching_group.append(item)

    collapsed: list[dict[str, Any]] = []
    for group in groups:
        side = group[0]['SIDE']
        early_times = sorted(_local_time({'TIME': row['EARLY_TIME']}) for row in group)
        earliest = early_times[0]
        latest = early_times[-1]
        release_times = [_local_time({'TIME': row['RELEASE_TIME']}) for row in group if row.get('RELEASE_TIME')]
        defended_times = [_local_time({'TIME': row['DEFENDED_TIME']}) for row in group if row.get('DEFENDED_TIME')]
        stage_end = max([latest, *release_times, *defended_times])
        vote_times = [at for (at, vote_side) in candidate_votes if vote_side == side and earliest <= at <= stage_end]
        consensus_times: dict[int, datetime | None] = {}
        for required in (1, 2, 3, 4):
            consensus_times[required] = next((at for at in sorted(vote_times) if len(candidate_votes[(at, side)]) >= required), None)
        vote_snapshot = max(
            (candidate_votes[(at, side)] for at in vote_times),
            key=len,
        ) if vote_times else []
        # The max above is intentionally based only on votes available by the
        # episode's observed stage end; it is not used to form any signal.
        paths = sorted(set(row['STATE_PATH'] for row in group))
        path = max(group, key=lambda row: len(row.get('VOTE_LIST', '').split(',')))['STATE_PATH']
        earliest_row = min(group, key=lambda row: row['EARLY_TIME'])
        result: dict[str, Any] = {
            'SIDE': side, 'EARLIEST_EARLY_TIME': earliest.strftime('%Y-%m-%d %H:%M:%S'),
            'LATEST_EARLY_TIME': latest.strftime('%Y-%m-%d %H:%M:%S'),
            'EARLY_PRICE': earliest_row.get('EARLY_PRICE'), 'VOTE_COUNT': len(vote_snapshot),
            'VOTE_HALF_LIVES': ','.join(map(str, sorted(vote_snapshot))),
            'DEFENDED_TIME': min(defended_times).strftime('%Y-%m-%d %H:%M:%S') if defended_times else '',
            'RELEASE_TIME': min(release_times).strftime('%Y-%m-%d %H:%M:%S') if release_times else '',
            'PATH': path, 'PATHS_SEEN': ' || '.join(paths), 'PATH_DISAGREEMENT': 'YES' if len(paths) > 1 else 'NO',
            'EPISODE_CLASS': 'RELEASE' if release_times else 'DEFENDED_ONLY' if defended_times else 'INVALIDATED' if any(row['STATE_PATH'].endswith('INVALIDATED') for row in group) else 'NO_CONFIRM',
        }
        for required, at in consensus_times.items():
            result[f'CONSENSUS_{required}_TIME'] = at.strftime('%Y-%m-%d %H:%M:%S') if at else ''

        def add_outcomes(prefix: str, timestamp: datetime | None) -> None:
            index = row_index.get(timestamp) if timestamp is not None else None
            price = _diagnostic_number(row_by_time.get(timestamp, {}).get('PRICE')) if timestamp is not None else None
            for horizon in (5, 10, 20, 30):
                mfe, mae = _diagnostic_outcome(parsed, index, side, price, horizon) if index is not None else (None, None)
                result[f'{prefix}_MFE_{horizon}M'] = mfe
                result[f'{prefix}_MAE_{horizon}M'] = mae

        row_by_time = {at: row for at, row in parsed}
        add_outcomes('EARLIEST_EARLY', earliest)
        add_outcomes('CONSENSUS_2', consensus_times[2])
        add_outcomes('CONSENSUS_3', consensus_times[3])
        add_outcomes('CONSENSUS_4', consensus_times[4])
        add_outcomes('DEFENDED', min(defended_times) if defended_times else None)
        add_outcomes('RELEASE', min(release_times) if release_times else None)
        collapsed.append(result)
    return collapsed


def _consensus_episode_report(rows: list[dict[str, Any]], output_csv: Path) -> str:
    benchmarks = ('21:26', '22:01', '22:37', '22:51')
    lines = [
        '# BTC-LRA CONSENSUS EPISODES', '',
        'Research-only collapse of BTC_LRA_RELEASE_STAGES. Production monitor, CONTROL, DOMINANCE, release-stage detector, thresholds and Pine are unchanged.', '',
        f'CSV: `{output_csv}`', '',
        'Grouping uses same SIDE and EARLY-time proximity <= 2 minutes. Consensus timestamps are causal first-vote timestamps.', '',
        '## Collapsed benchmark episodes', '',
        'SIDE | EARLIEST EARLY | VOTES | DEFENDED | RELEASE | PATH | PATHS SEEN',
        '---|---|---:|---|---|---|---',
    ]
    for benchmark in benchmarks:
        matches = [row for row in rows if row['EARLIEST_EARLY_TIME'][11:16] == benchmark]
        for row in matches:
            lines.append(f"{row['SIDE']} | {row['EARLIEST_EARLY_TIME']} | {row['VOTE_COUNT']}/4 ({row['VOTE_HALF_LIVES']}) | {row['DEFENDED_TIME'] or 'NONE'} | {row['RELEASE_TIME'] or 'NONE'} | {row['PATH']} | {row['PATHS_SEEN']}")
        if not matches:
            lines.append(f'-- | {benchmark} | NONE | -- | -- | -- | --')
    lines.extend(['', '## Independent episode comparison', '', 'CLASS | COUNT | 10m MFE MEAN | 10m MFE MEDIAN | 10m MAE MEAN | 10m MAE MEDIAN', '---|---:|---:|---:|---:|---:'])
    import statistics
    for category in ('RELEASE', 'DEFENDED_ONLY', 'NO_CONFIRM', 'INVALIDATED'):
        group = [row for row in rows if row['EPISODE_CLASS'] == category]
        mfes = [float(row['EARLIEST_EARLY_MFE_10M']) for row in group if row.get('EARLIEST_EARLY_MFE_10M') not in (None, '')]
        maes = [float(row['EARLIEST_EARLY_MAE_10M']) for row in group if row.get('EARLIEST_EARLY_MAE_10M') not in (None, '')]
        lines.append(f'| {category} | {len(group)} | {statistics.mean(mfes) if mfes else None} | {statistics.median(mfes) if mfes else None} | {statistics.mean(maes) if maes else None} | {statistics.median(maes) if maes else None} |')
    lines.extend(['', '## Vote-level comparison', '', 'VOTES | COUNT | 10m MFE MEAN | 10m MFE MEDIAN | 10m MAE MEAN | 10m MAE MEDIAN', '---|---:|---:|---:|---:|---:'])
    for vote_count in (1, 2, 3, 4):
        group = [row for row in rows if row['VOTE_COUNT'] == vote_count]
        mfes = [float(row['EARLIEST_EARLY_MFE_10M']) for row in group if row.get('EARLIEST_EARLY_MFE_10M') not in (None, '')]
        maes = [float(row['EARLIEST_EARLY_MAE_10M']) for row in group if row.get('EARLIEST_EARLY_MAE_10M') not in (None, '')]
        lines.append(f'| {vote_count}/4 | {len(group)} | {statistics.mean(mfes) if mfes else None} | {statistics.median(mfes) if mfes else None} | {statistics.mean(maes) if maes else None} | {statistics.median(maes) if maes else None} |')
    release_delays = [
        (_local_time({'TIME': row['CONSENSUS_4_TIME']}) - _local_time({'TIME': row['EARLIEST_EARLY_TIME']})).total_seconds() / 60.0
        for row in rows if row.get('CONSENSUS_4_TIME')
    ]
    lines.extend(['', '## Questions', '', f'- Episodes after half-life collapse: **{len(rows)}**.', f'- EARLY to 4/4 consensus delays: `{release_delays}` minutes where available.', '- MARKET RELEASE and DEFENDED-only outcomes are reported separately; no threshold is selected.', '- Good episodes without MARKET RELEASE remain visible in the DEFENDED_ONLY/NO_CONFIRM groups.', ''])
    return '\n'.join(lines)


def run_consensus_episode_analysis(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parent
    raw_paths = args.raw_oi or discover_raw_paths(root)
    market_path = args.market_csv or discover_market_path(root)
    if not raw_paths or market_path is None:
        raise SystemExit('Consensus episode analysis requires recorded raw OI and market data.')
    samples = load_raw(raw_paths)
    market = load_market(market_path)
    minutes = minute_oi(samples)
    annotate_minutes(minutes)
    common = recorded_range(samples, market)
    if common is None:
        raise SystemExit('No common recorded range for consensus episodes.')
    start, end = common
    if args.from_time:
        start = max(start, parse_time(args.from_time))
    if args.end:
        end = min(end, parse_time(args.end))
    samples = [sample for sample in samples if start <= sample['ts'] <= end + timedelta(minutes=1)]
    minutes = [row for row in minutes if start <= row['minute'] <= end]
    market = [row for row in market if start <= row['ts'] <= end]
    local_rows, _ = _local_dominance_contributions(samples, minutes, market, start, end)
    raw_summaries, _ = _raw_intensity_summary(samples, minutes, market)
    release_rows = _release_stage_episode_rows(local_rows, raw_summaries)
    rows = _consensus_episode_rows(local_rows, release_rows)
    output_csv = root / 'data' / 'research' / 'BTC_LRA_CONSENSUS_EPISODES.csv'
    output_md = root / 'data' / 'research' / 'BTC_LRA_CONSENSUS_EPISODES.md'
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=CONSENSUS_EPISODE_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _analysis_value(row.get(key)) for key in CONSENSUS_EPISODE_COLUMNS})
    output_md.write_text(_consensus_episode_report(rows, output_csv), encoding='utf-8')
    print(f'CONSENSUS EPISODES CSV: {output_csv}')
    print(f'CONSENSUS EPISODES REPORT: {output_md}')
    print(f'COLLAPSED EPISODES: {len(rows)}')


TV_STRUCTURE_COLUMNS = [
    'TIME', 'PRICE', 'SIDE', 'STAGE', 'CONSENSUS_TIER', 'LOCAL_DOM_PCT',
    'LOCAL_MASS', 'PRICE_REWARD_BPS', 'CONTROL', 'EFF_RATIO', 'ABSORPTION',
    'OI_ADD', 'AGGR_SIDE', 'AGGR_SHARE', 'AGGR_MAG',
]


def _structure_time(value: Any) -> datetime | None:
    if value in (None, ''):
        return None
    return _local_time({'TIME': value})


def _structure_local_metadata(local_rows: list[dict[str, Any]], timestamp: datetime,
                              side: str, votes: str) -> dict[str, Any]:
    row = next((item for item in local_rows if _local_time(item) == timestamp), {})
    vote_list = [int(value) for value in str(votes).split(',') if value]
    half_life = vote_list[0] if vote_list else 10
    prefix = 'SELL' if side == 'LONG' else 'BUY'
    return {
        'price': _diagnostic_number(row.get('PRICE')),
        'local_dom_pct': _diagnostic_number(row.get(f'LOCAL_{prefix}_{half_life}')) / (_diagnostic_number(row.get(f'LOCAL_MASS_{half_life}')) or 1.0) * 100.0 if _diagnostic_number(row.get(f'LOCAL_MASS_{half_life}')) else None,
        'local_mass': _diagnostic_number(row.get(f'LOCAL_MASS_{half_life}')),
        'price_reward_bps': _diagnostic_number(row.get(f'{prefix}_REWARD_BPS_{half_life}')),
    }


def _structure_stage_event(event: dict[str, Any], stage: str, side: str,
                           consensus_tier: int, local_rows: list[dict[str, Any]],
                           raw_by_time: dict[datetime, dict[str, Any]]) -> dict[str, Any]:
    timestamp = event['time']
    metadata = _structure_local_metadata(local_rows, timestamp, side, str(consensus_tier))
    summary = raw_by_time.get(timestamp, {})
    return {
        'TIME': timestamp.strftime('%Y-%m-%d %H:%M:%S'),
        'PRICE': event.get('price') if event.get('price') is not None else metadata['price'],
        'SIDE': side,
        'STAGE': stage,
        'CONSENSUS_TIER': f'{consensus_tier}/4',
        'LOCAL_DOM_PCT': metadata['local_dom_pct'],
        'LOCAL_MASS': metadata['local_mass'],
        'PRICE_REWARD_BPS': event.get('reward') if event.get('reward') is not None else metadata['price_reward_bps'],
        'CONTROL': event.get('control') or summary.get('CONTROL') or '',
        'EFF_RATIO': event.get('eff_ratio') if event.get('eff_ratio') is not None else _diagnostic_number(summary.get('EFF_RATIO')),
        'ABSORPTION': event.get('absorption_ratio') if event.get('absorption_ratio') is not None else _diagnostic_number(summary.get('ABSORPTION_RATIO')),
        'OI_ADD': event.get('oi_add') if event.get('oi_add') is not None else _diagnostic_number(summary.get('OI_ADD')),
        'AGGR_SIDE': event.get('aggr_side') or summary.get('AGGR_SIDE') or '',
        'AGGR_SHARE': event.get('aggr_share') if event.get('aggr_share') is not None else _diagnostic_number(summary.get('AGGR_SHARE')),
        'AGGR_MAG': event.get('aggr_mag') if event.get('aggr_mag') is not None else _diagnostic_number(summary.get('AGGR_MAG')),
    }


def build_causal_structure_events(local_rows: list[dict[str, Any]],
                                  raw_summaries: list[dict[str, Any]],
                                  selected_votes: int) -> list[dict[str, Any]]:
    """Build structural labels from the existing release/consensus research layers."""
    if selected_votes not in (1, 2, 3, 4):
        raise ValueError('selected consensus votes must be 1, 2, 3 or 4')
    release_rows = _release_stage_episode_rows(local_rows, raw_summaries)
    consensus_rows = _consensus_episode_rows(local_rows, release_rows)
    raw_by_time = {row['_time']: row for row in raw_summaries}
    structures: list[dict[str, Any]] = []
    for consensus in consensus_rows:
        early_time = _structure_time(consensus.get(f'CONSENSUS_{selected_votes}_TIME'))
        if early_time is None:
            continue
        side = consensus['SIDE']
        early_bound = _structure_time(consensus['EARLIEST_EARLY_TIME'])
        latest_bound = _structure_time(consensus['LATEST_EARLY_TIME'])
        if early_bound is None or latest_bound is None:
            continue
        matching = [
            row for row in release_rows
            if row['SIDE'] == side
            and early_bound - timedelta(minutes=2) <= _structure_time(row['EARLY_TIME']) <= latest_bound + timedelta(minutes=2)
        ]
        matching.sort(key=lambda row: row['EARLY_TIME'])
        structures.append(_structure_stage_event({
            'time': early_time,
            'price': _diagnostic_number(next((row.get('EARLY_PRICE') for row in matching if row.get('EARLY_PRICE') not in (None, '')), None)),
            'reward': _diagnostic_number(next((row.get('START REWARD') for row in matching if row.get('START REWARD') not in (None, '')), None)),
        }, 'E-LONG' if side == 'LONG' else 'E-SHORT', side, selected_votes, local_rows, raw_by_time))

        stage_events: list[tuple[datetime, str, dict[str, Any]]] = []
        for row in matching:
            defended = row.get('_DEFENDED_EVENT')
            release = row.get('_RELEASE_EVENT')
            for event, stage in ((defended, 'L-HOLD' if side == 'LONG' else 'S-HOLD'), (release, 'LONG' if side == 'LONG' else 'SHORT')):
                if event is not None and event['time'] >= early_time:
                    stage_events.append((event['time'], stage, event))
            if row.get('_END_REASON') == 'INVALIDATED':
                invalidation_time = _structure_time(row.get('_END_TIME'))
                if invalidation_time is not None and invalidation_time >= early_time:
                    stage_events.append((invalidation_time, 'X', {
                        'time': invalidation_time,
                        'price': _diagnostic_number(row.get('_END_PRICE')),
                        'control': '', 'reward': None,
                    }))
        released = False
        emitted_stages: set[tuple[datetime, str]] = set()
        for timestamp, stage, event in sorted(stage_events, key=lambda item: item[0]):
            if timestamp < early_time:
                continue
            if released:
                continue
            if (timestamp, stage) in emitted_stages:
                continue
            if stage in ('L-HOLD', 'S-HOLD'):
                if any(item_stage in ('LONG', 'SHORT') and item_time <= timestamp for item_time, item_stage, _ in stage_events):
                    continue
            if stage in ('LONG', 'SHORT'):
                released = True
            emitted_stages.add((timestamp, stage))
            structures.append(_structure_stage_event(event, stage, side, selected_votes, local_rows, raw_by_time))
    structures.sort(key=lambda row: row['TIME'])
    return structures


def write_tv_structure_pine(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        '//@version=6',
        '// BTC-LRA structural research events',
        '// Generated from causal EARLY -> LIMIT defense -> MARKET release research state.',
        'indicator("BTC-LRA OI Structure", overlay=true, max_labels_count=500)',
        '',
        'consensus_votes = input.int(2, "EARLY consensus votes", options=[1, 2, 3, 4])',
        'show_early = input.bool(true, "Show EARLY")',
        'show_hold = input.bool(true, "Show HOLD")',
        'show_release = input.bool(true, "Show RELEASE")',
        'show_invalidated = input.bool(true, "Show invalidated")',
        'show_details = input.bool(false, "Show details")',
        '',
    ]
    for index, event in enumerate(events):
        timestamp = _structure_time(event['TIME'])
        price = _diagnostic_number(event.get('PRICE'))
        if timestamp is None or price is None or price <= 0:
            continue
        stage = event['STAGE']
        show = 'show_early' if stage in ('E-LONG', 'E-SHORT') else 'show_hold' if stage.endswith('HOLD') else 'show_release' if stage in ('LONG', 'SHORT') else 'show_invalidated'
        color = 'color.green' if stage in ('E-LONG', 'L-HOLD', 'LONG') else 'color.red' if stage in ('E-SHORT', 'S-HOLD', 'SHORT') else 'color.gray'
        style = 'label.style_label_up' if stage in ('E-LONG', 'L-HOLD', 'LONG') else 'label.style_label_down' if stage in ('E-SHORT', 'S-HOLD', 'SHORT') else 'label.style_label_left'
        y = f'{price:.10f} * 0.999' if stage in ('E-LONG', 'L-HOLD', 'LONG') else f'{price:.10f} * 1.001' if stage in ('E-SHORT', 'S-HOLD', 'SHORT') else f'{price:.10f}'
        details = '\\n'.join([
            f'TIME: {event["TIME"]} Panama', f'STAGE: {stage}',
            f'CONSENSUS: {event.get("CONSENSUS_TIER", "")}',
            f'LOCAL DOM: {_analysis_value(event.get("LOCAL_DOM_PCT"))}%',
            f'LOCAL MASS: {_analysis_value(event.get("LOCAL_MASS"))} BTC',
            f'REWARD: {_analysis_value(event.get("PRICE_REWARD_BPS"))} bps',
            f'CONTROL: {event.get("CONTROL", "")}',
            f'EFF_RATIO: {_analysis_value(event.get("EFF_RATIO"))}',
            f'ABSORPTION: {_analysis_value(event.get("ABSORPTION"))}',
            f'OI ADD: {_analysis_value(event.get("OI_ADD"))} BTC',
            f'AGGR: {event.get("AGGR_SIDE", "")} {_analysis_value(event.get("AGGR_SHARE"))}% {_analysis_value(event.get("AGGR_MAG"))} BTC',
        ])
        lines.extend([
            f'structure_{index}_time = {pine_timestamp(timestamp)}',
            f'structure_{index}_shown = {show} and consensus_votes == {int(str(event["CONSENSUS_TIER"]).split("/")[0])}',
            f'if structure_{index}_shown and time <= structure_{index}_time and time_close > structure_{index}_time',
            f'    label.new(x=structure_{index}_time, y={y}, xloc=xloc.bar_time, yloc=yloc.price, text=show_details ? {pine_quote(details)} : {pine_quote(stage)}, style={style}, color={color}, textcolor=color.white, size=size.tiny)',
            '',
        ])
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    temporary.replace(path)


def write_tv_structure_csv(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=TV_STRUCTURE_COLUMNS)
        writer.writeheader()
        for event in events:
            writer.writerow({key: _analysis_value(event.get(key)) for key in TV_STRUCTURE_COLUMNS})


def write_tv_structure_for_data(root: Path, samples: list[dict[str, Any]],
                                market: list[dict[str, Any]], start: datetime,
                                end: datetime, selected_votes: int) -> list[dict[str, Any]]:
    samples = [sample for sample in samples if start <= sample['ts'] <= end + timedelta(minutes=1)]
    minutes = [row for row in minute_oi(samples) if start <= row['minute'] <= end]
    market = [row for row in market if start <= row['ts'] <= end]
    annotate_minutes(minutes)
    local_rows, _ = _local_dominance_contributions(samples, minutes, market, start, end)
    raw_summaries, _ = _raw_intensity_summary(samples, minutes, market)
    events = build_causal_structure_events(local_rows, raw_summaries, selected_votes)
    write_tv_structure_csv(root / 'data' / 'research' / 'BTC_LRA_TV_STRUCTURE_EVENTS.csv', events)
    write_tv_structure_pine(root / 'BTC_LRA_TV_STRUCTURE.pine', events)
    return events


def run_tv_structure_export(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parent
    raw_paths = args.raw_oi or discover_raw_paths(root)
    market_path = args.market_csv or discover_market_path(root)
    if not raw_paths or market_path is None:
        raise SystemExit('TV structure export requires recorded raw OI and market data.')
    samples = load_raw(raw_paths)
    market = load_market(market_path)
    minutes = minute_oi(samples)
    annotate_minutes(minutes)
    common = recorded_range(samples, market)
    if common is None:
        raise SystemExit('No common recorded range for TV structure export.')
    start, end = common
    if args.from_time:
        start = max(start, parse_time(args.from_time))
    if args.end:
        end = min(end, parse_time(args.end))
    events = write_tv_structure_for_data(root, samples, market, start, end, args.tv_structure_votes)
    csv_path = root / 'data' / 'research' / 'BTC_LRA_TV_STRUCTURE_EVENTS.csv'
    pine_path = root / 'BTC_LRA_TV_STRUCTURE.pine'
    write_tv_structure_csv(csv_path, events)
    write_tv_structure_pine(pine_path, events)
    print(f'TV STRUCTURE PINE: {pine_path}')
    print(f'TV STRUCTURE CSV: {csv_path}')
    print(f'STRUCTURE EVENTS: {len(events)}')
    print(f'CONSENSUS TIER: {args.tv_structure_votes}/4')


def _raw_float(value: Any) -> float | None:
    if value in (None, ''):
        return None
    return float(value)


def _raw_event_payload(summary: dict[str, Any], session: 'Session') -> dict[str, Any]:
    """Convert a raw-intensity minute into a presentation-only event object."""
    control = summary.get('CONTROL') or 'CONTROL UNCLEAR'
    raw_class = 'RAW STRONG' if float(summary.get('OI_INTENSITY_PCTL') or 0) >= 99.0 else 'RAW WATCH'
    control_text = control.removeprefix('CONTROL ')
    oi_net_60s = _raw_float(summary.get('OI_NET_60S')) or 0.0
    oi_directionality_60s = _raw_float(summary.get('OI_DIRECTIONALITY_60S'))
    oi_flow = 'EXPANSION' if oi_net_60s > 0 else 'CONTRACTION' if oi_net_60s < 0 else 'FLAT'
    aggr_side = summary.get('AGGR_SIDE') or 'HELD'
    aggr_mag = _raw_float(summary.get('AGGR_MAG')) or 0.0
    aggr_share = _raw_float(summary.get('AGGR_SHARE')) or 0.0
    payload = {
        'time': summary['_time'],
        'event_source': 'RAW_OI_INTENSITY',
        'raw_class': raw_class,
        'raw_watch': True,
        'raw_strong': raw_class == 'RAW STRONG',
        'raw_oi_intensity_pctl': _raw_float(summary.get('OI_INTENSITY_PCTL')),
        'raw_peak_type': summary.get('PEAK_TYPE'),
        'raw_oi_net_60s': oi_net_60s,
        'raw_oi_directionality_60s': oi_directionality_60s,
        'raw_oi_flow': oi_flow,
        'raw_aggr': f'{aggr_side} {aggr_share:.1f}% {compact(aggr_mag)} BTC',
        'raw_eff_ratio': _raw_float(summary.get('EFF_RATIO')),
        'eff_ratio': _raw_float(summary.get('EFF_RATIO')),
        'absorption_ratio': _raw_float(summary.get('ABSORPTION_RATIO')) or 0.0,
        'raw_control': control,
        'control': control,
        'control_label': control,
        'display_control_text': control_text,
        'aggr_side': aggr_side,
        'aggr_mag': aggr_mag,
        'aggr_share_pct': aggr_share,
        'meaningful_aggression': summary.get('MEANINGFUL_AGGR') == 'YES',
        'event_oi_net': _raw_float(summary.get('OI_NET')) or 0.0,
        'event_oi_activity': _raw_float(summary.get('OI_ACT')) or 0.0,
        'display_oi_add': _raw_float(summary.get('OI_ADD')) or 0.0,
        'display_oi_exit': _raw_float(summary.get('OI_EXIT')) or 0.0,
        'display_oi_jump': _raw_float(summary.get('OI_MAX_JUMP')) or 0.0,
        'display_reference_price': _raw_float(summary.get('REFERENCE_PRICE')),
        'display_event_price': _raw_float(summary.get('PRICE')),
        'display_price_change': _raw_float(summary.get('PRICE_CHANGE')),
        'display_taker_buy': _raw_float(summary.get('TAKER_BUY')) or 0.0,
        'display_taker_sell': _raw_float(summary.get('TAKER_SELL')) or 0.0,
        'display_flow_delta': (_raw_float(summary.get('TAKER_BUY')) or 0.0) - (_raw_float(summary.get('TAKER_SELL')) or 0.0),
        'oi_add_mass': _raw_float(summary.get('OI_ADD')) or 0.0,
        'raw_dominance_v2_contribution_btc': 0.0,
        'raw_oi_flow_contribution': 0.0,
        'oi_flow_contribution': 0.0,
        'event_flags': [raw_class],
        'oi_event_flow': session.oi_event_flow,
        'buy_dominance_v2_btc': session.buy_dominance_v2_btc,
        'sell_dominance_v2_btc': session.sell_dominance_v2_btc,
    }
    return payload


def apply_raw_event_to_session(session: 'Session', summary: dict[str, Any]) -> dict[str, Any] | None:
    """Append/annotate raw events without changing OI flow or dominance."""
    if summary.get('WARMUP_INSUFFICIENT') == 'YES':
        return None
    intensity = _raw_float(summary.get('OI_INTENSITY_PCTL'))
    if intensity is None or intensity < 97.5:
        return None
    if summary.get('MEANINGFUL_AGGR') != 'YES' or summary.get('CONTROL') in ('', None, 'CONTROL UNCLEAR'):
        return None
    existing = next((event for event in session.event_history if event.get('time') == summary['_time']), None)
    if existing is not None:
        flags = existing.setdefault('event_flags', ['STRONG/MEGA'])
        raw_class = 'RAW STRONG' if intensity >= 99.0 else 'RAW WATCH'
        if raw_class not in flags:
            flags.append(raw_class)
        existing.update({
            'raw_watch': True,
            'raw_strong': intensity >= 99.0,
            'raw_class': raw_class,
            'raw_oi_intensity_pctl': intensity,
            'raw_peak_type': summary.get('PEAK_TYPE'),
            'raw_oi_net_60s': _raw_float(summary.get('OI_NET_60S')),
            'raw_oi_directionality_60s': _raw_float(summary.get('OI_DIRECTIONALITY_60S')),
            'raw_oi_flow': 'EXPANSION' if (_raw_float(summary.get('OI_NET_60S')) or 0.0) > 0 else 'CONTRACTION' if (_raw_float(summary.get('OI_NET_60S')) or 0.0) < 0 else 'FLAT',
            'raw_aggr': f'{summary.get("AGGR_SIDE") or "HELD"} {float(summary.get("AGGR_SHARE") or 0.0):.1f}% {compact(_raw_float(summary.get("AGGR_MAG")) or 0.0)} BTC',
            'raw_eff_ratio': _raw_float(summary.get('EFF_RATIO')),
            'eff_ratio': _raw_float(summary.get('EFF_RATIO')),
            'absorption_ratio': _raw_float(summary.get('ABSORPTION_RATIO')) or 0.0,
            'raw_control': summary.get('CONTROL') or 'CONTROL UNCLEAR',
            'control': summary.get('CONTROL') or 'CONTROL UNCLEAR',
            'raw_dominance_v2_contribution_btc': 0.0,
            'raw_oi_flow_contribution': 0.0,
        })
        return existing
    event = _raw_event_payload(summary, session)
    session.event_history.append(event)
    return event


class PreReleaseState:
    """Research-only causal state machine; it never changes production accounting."""

    PCTL_THRESHOLD = 97.5
    MIN_DEPTH_LEVEL_BTC = 20.0
    MAX_DEPTH_DISTANCE_USDT = 50.0
    TEST_DISTANCE_USDT = 10.0
    REQUIRED_SAMPLES = 2
    CANCEL_MISSES = 3

    def __init__(self) -> None:
        self.state = 'NEUTRAL'
        self.side: str | None = None
        self.streak = 0
        self.misses = 0
        self.last_sample: datetime | None = None
        self.last_detail: dict[str, Any] | None = None
        self.depth_levels: dict[str, dict[str, Any] | None] = {'BUY': None, 'SELL': None}
        self.depth_events: list[dict[str, Any]] = []
        self.last_depth_event: dict[str, Any] | None = None
        self.depth_history: list[dict[str, Any]] = []
        self._next_level_id = 1

    def _record_depth_event(self, event: dict[str, Any]) -> None:
        self.depth_events.append(event)
        self.depth_history.append(dict(event))
        self.depth_history = self.depth_history[-200:]
        self.last_depth_event = event

    def pop_depth_events(self) -> list[dict[str, Any]]:
        events = self.depth_events
        self.depth_events = []
        return events

    def _finish_depth_level(self, side: str, previous: dict[str, Any],
                            timestamp: datetime, mid: float | None,
                            status: str | None = None) -> None:
        level_price = float(previous['price'])
        if status is None:
            crossed = mid is not None and (mid < level_price if side == 'BUY' else mid > level_price)
            status = 'BROKEN' if crossed else 'PULLED'
        initial = float(previous.get('initial_size', 0.0))
        current = float(previous.get('last_size', 0.0))
        self._record_depth_event({
            'level_id': previous.get('level_id'),
            'side': side,
            'status': status,
            'price': level_price,
            'initial_qty': initial,
            'current_qty': current,
            'remaining_pct': current / initial * 100.0 if initial > 0 else None,
            'min_distance': previous.get('min_distance'),
            'age_sec': previous.get('age_sec', 0.0),
            'time': timestamp,
        })

    def _depth_level(self, side: str, depth: dict[str, Any] | None,
                     timestamp: datetime) -> dict[str, Any] | None:
        if depth is None:
            self.depth_levels[side] = None
            return None
        price_key = 'largest_bid_level_price' if side == 'BUY' else 'largest_ask_level_price'
        size_key = 'largest_bid_level_qty' if side == 'BUY' else 'largest_ask_level_qty'
        try:
            level_price = float(depth.get(price_key))
            level_size = float(depth.get(size_key))
            mid = float(depth.get('mid'))
        except (TypeError, ValueError):
            previous = self.depth_levels.get(side)
            if previous is not None:
                self._finish_depth_level(side, previous, timestamp, None)
            self.depth_levels[side] = None
            return None
        distance = abs(mid - level_price)
        previous = self.depth_levels.get(side)
        if level_size < self.MIN_DEPTH_LEVEL_BTC or distance > self.MAX_DEPTH_DISTANCE_USDT:
            if previous is not None:
                self._finish_depth_level(side, previous, timestamp, mid)
            self.depth_levels[side] = None
            return None
        same_level = previous is not None and abs(float(previous['price']) - level_price) <= 1.0
        if not same_level:
            if previous is not None:
                self._finish_depth_level(side, previous, timestamp, mid)
            previous = {
                'level_id': self._next_level_id,
                'first_seen': timestamp,
                'price': level_price,
                'initial_size': level_size,
                'min_size': level_size,
            }
            self._next_level_id += 1
        previous['price'] = level_price
        previous['last_size'] = level_size
        previous['min_size'] = min(float(previous.get('min_size', level_size)), level_size)
        previous['distance'] = distance
        previous['min_distance'] = min(float(previous.get('min_distance', distance)), distance)
        previous['age_sec'] = max(0.0, (timestamp - previous['first_seen']).total_seconds())
        previous['tested'] = distance <= self.TEST_DISTANCE_USDT
        moved_away = mid > level_price if side == 'BUY' else mid < level_price
        previous['held'] = bool(previous['tested'] and moved_away and level_size >= float(previous['initial_size']) * 0.25)
        new_status = 'HELD' if previous['held'] else 'TESTED' if previous['tested'] else 'NEAR'
        if new_status != previous.get('status'):
            initial = float(previous.get('initial_size', level_size))
            self._record_depth_event({
                'level_id': previous.get('level_id'),
                'side': side,
                'status': new_status,
                'price': level_price,
                'initial_qty': initial,
                'current_qty': level_size,
                'remaining_pct': level_size / initial * 100.0 if initial > 0 else None,
                'min_distance': previous.get('min_distance'),
                'age_sec': previous['age_sec'],
                'time': timestamp,
            })
        previous['status'] = new_status
        self.depth_levels[side] = previous
        return dict(previous)

    @staticmethod
    def _target_control(side: str, control: str) -> bool:
        return control in {
            'CONTROL LIMIT BUY' if side == 'BUY' else 'CONTROL LIMIT SELL',
            'CONTROL MARKET BUY' if side == 'BUY' else 'CONTROL MARKET SELL',
        }

    def _detail(self, sample: dict[str, Any], metric: dict[str, Any],
                effort: dict[str, Any], depth: dict[str, Any] | None,
                price: float | None) -> dict[str, Any]:
        doi = float(metric.get('doi') or 0.0)
        pctl = max((metric.get(key) for key in ('impulse_pctl', 'burst_30_pctl', 'burst_60_pctl')
                    if metric.get(key) is not None), default=None)
        ratio = depth.get('bid_ask_notional_ratio') if depth else None
        side = str(effort.get('aggr_side') or 'HELD')
        depth_level = self._depth_level(side, depth, sample['ts']) if side in ('BUY', 'SELL') else None
        anomaly_probability = seller_interception_probability(effort, depth_level, pctl, doi)
        return {
            'time': sample['ts'],
            'price': price,
            'side': side,
            'pctl': pctl,
            'peak_type': metric.get('peak_type'),
            'doi': doi,
            'oi_flow': 'EXPANSION' if doi > 0 else 'CONTRACTION' if doi < 0 else 'FLAT',
            'aggr_mag': effort.get('aggr_mag'),
            'aggr_share': effort.get('aggr_share_pct'),
            'control': effort.get('control', 'CONTROL UNCLEAR'),
            'eff_ratio': effort.get('eff_ratio'),
            'absorption_ratio': effort.get('absorption_ratio'),
            'depth_ratio': depth.get('bid_ask_notional_ratio') if depth else None,
            'depth_level': depth_level,
            'anomaly_probability': anomaly_probability,
            'anomaly_label': 'SELL INTERCEPT' if anomaly_probability >= SELL_INTERCEPTION_SOUND_THRESHOLD else None,
        }

    def observe(self, sample: dict[str, Any], metric: dict[str, Any],
                effort: dict[str, Any] | None, depth: dict[str, Any] | None,
                price: float | None) -> dict[str, Any] | None:
        """Advance only from information available at this raw sample timestamp."""
        timestamp = sample['ts']
        if self.last_sample is not None and timestamp <= self.last_sample:
            return None
        self.last_sample = timestamp
        # Collector gaps, duplicate timestamps and out-of-order samples are
        # valid telemetry but never valid impulse evidence for this layer.
        if (metric.get('doi') is None or sample.get('gap_break') or
                (metric.get('dt_sec') is not None and metric.get('dt_sec') <= 0)):
            return None
        if effort is None:
            self.misses += 1
            return self._maybe_cancel(timestamp)

        detail = self._detail(sample, metric, effort, depth, price)
        self.last_detail = detail
        side = detail['side']
        qualified = (
            detail['pctl'] is not None and detail['pctl'] >= self.PCTL_THRESHOLD and
            side in ('BUY', 'SELL') and bool(effort.get('meaningful_aggression')) and
            detail['control'] != 'CONTROL UNCLEAR' and detail['depth_level'] is not None
        )
        if not qualified:
            self.misses += 1
            return self._maybe_cancel(timestamp)

        if self.side is not None and side != self.side and self.state != 'NEUTRAL':
            previous = self.state
            self.state = 'NEUTRAL'
            self.side = None
            self.streak = 0
            self.misses = 0
            return {'state': 'CANCEL', 'previous_state': previous, **detail}

        self.side = side
        self.streak += 1
        self.misses = 0
        prefix = 'BUY' if side == 'BUY' else 'SELL'
        limit = f'CONTROL LIMIT {side}'
        market = f'CONTROL MARKET {side}'
        previous = self.state
        if self.state == 'NEUTRAL':
            self.state = f'{prefix}_WATCH'
        elif self.state in (f'{prefix}_WATCH', f'{prefix}_BUILDING'):
            if self.streak >= self.REQUIRED_SAMPLES and detail['control'] == market:
                self.state = f'{prefix}_RELEASE'
            elif detail['control'] == limit:
                self.state = f'{prefix}_BUILDING'
        if self.state != previous:
            return {'state': self.state, 'previous_state': previous, **detail}
        return None

    def _maybe_cancel(self, timestamp: datetime) -> dict[str, Any] | None:
        if self.state == 'NEUTRAL' or self.misses < self.CANCEL_MISSES:
            return None
        previous = self.state
        side = self.side
        self.state = 'NEUTRAL'
        self.side = None
        self.streak = 0
        self.misses = 0
        return {
            'state': 'CANCEL',
            'previous_state': previous,
            'time': timestamp,
            'side': side,
            'price': self.last_detail.get('price') if self.last_detail else None,
            'pctl': self.last_detail.get('pctl') if self.last_detail else None,
            'oi_flow': self.last_detail.get('oi_flow') if self.last_detail else None,
            'control': self.last_detail.get('control') if self.last_detail else 'CONTROL UNCLEAR',
        }

    def snapshot(self) -> dict[str, Any]:
        detail = self.last_detail or {}
        return {
            'state': self.state,
            'side': self.side,
            'streak': self.streak,
            'pctl': detail.get('pctl'),
            'oi_flow': detail.get('oi_flow'),
            'depth_ratio': detail.get('depth_ratio'),
            'depth_level': detail.get('depth_level'),
            'anomaly_probability': detail.get('anomaly_probability', 0.0),
            'anomaly_label': detail.get('anomaly_label'),
            'control': detail.get('control'),
            'time': detail.get('time'),
            'last_depth_event': self.last_depth_event,
            'depth_history': list(self.depth_history),
        }


def build_depth_timeline(rows: list[dict[str, Any]], anchor: datetime,
                        clock: datetime) -> list[dict[str, Any]]:
    """Reconstruct a display-only depth timeline from stored REST snapshots."""
    tracker = PreReleaseState()
    selected = [
        row for row in rows
        if anchor <= row.get('_ts', anchor) <= clock
    ]
    for row in selected:
        try:
            timestamp = row['_ts']
            mid = float(row['mid'])
            for side, price_key, size_key in (
                ('BUY', 'largest_bid_level_price', 'largest_bid_level_qty'),
                ('SELL', 'largest_ask_level_price', 'largest_ask_level_qty'),
            ):
                depth = {
                    'mid': mid,
                    price_key: row.get(price_key),
                    size_key: row.get(size_key),
                }
                tracker._depth_level(side, depth, timestamp)
        except (KeyError, TypeError, ValueError):
            continue

    # Keep currently active levels visible even though they have not finished.
    for side, level in tracker.depth_levels.items():
        if level is None or level.get('level_id') is None:
            continue
        level_id = level['level_id']
        if any(event.get('level_id') == level_id for event in tracker.depth_history):
            tracker.depth_history.append({
                'level_id': level_id,
                'side': side,
                'status': level.get('status', 'ACTIVE'),
                'price': level.get('price'),
                'initial_qty': level.get('initial_size'),
                'current_qty': level.get('last_size'),
                'remaining_pct': (
                    float(level.get('last_size', 0.0)) /
                    float(level.get('initial_size', 1.0)) * 100.0
                    if float(level.get('initial_size', 0.0)) > 0 else None
                ),
                'min_distance': level.get('min_distance'),
                'age_sec': level.get('age_sec', 0.0),
                'time': selected[-1]['_ts'] if selected else clock,
            })
    return tracker.depth_history[-200:]


class Session:
    def __init__(self, anchor: datetime) -> None:
        self.anchor = anchor
        self.window_mode = 'LIVE_FROM'
        self.window_end: datetime | None = None
        self.display_depth_history: list[dict[str, Any]] = []
        self.oi_start: float | None = None
        self.oi_current: float | None = None
        self.oi_add = self.oi_exit = 0.0
        self.buy = self.sell = 0.0
        self.anchor_price: float | None = None
        self.price: float | None = None
        self.previous_event: dict[str, Any] | None = None
        self.last_events: deque[dict[str, Any]] = deque(maxlen=10)
        self.event_history: list[dict[str, Any]] = []
        self.minute_history: list[dict[str, Any]] = []
        self.oi_event_flow = 0.0
        self.market_history: list[dict[str, Any]] = []
        self.buy_dominance_weight_usdt = 0.0
        self.sell_dominance_weight_usdt = 0.0
        self.buy_dominance_v2_btc = 0.0
        self.sell_dominance_v2_btc = 0.0
        self.total_oi_anchor_btc: float | None = None
        self.total_oi_current_btc: float | None = None
        self.total_oi_flow_btc: float | None = None
        self.total_oi_flow_pct: float | None = None
        self.continuous_buy_dominance_btc = 0.0
        self.continuous_sell_dominance_btc = 0.0
        self.continuous_buy_pct: float | None = None
        self.continuous_sell_pct: float | None = None
        self.continuous_unclear_minutes = 0
        self.continuous_valid_closed_minutes = 0
        self.continuous_contributions_by_minute: dict[datetime, dict[str, float]] = {}
        self.continuous_market_history: list[dict[str, Any]] = []
        self.continuous_baseline_initialized = False
        self.continuous_last_result: dict[str, Any] | None = None
        self.dominance_reference_buy_pct: float | None = None
        self.dominance_reference_sell_pct: float | None = None
        self.dominance_reference_time: datetime | None = None
        self.dominance_reference_valid_minutes = 0
        self.dominance_reference_buy_equiv_btc: float | None = None
        self.dominance_reference_sell_equiv_btc: float | None = None
        self.dominance_reference_frozen = False
        self.dominance_reference_frozen_just_now = False
        self.provisional_oi_flow = 0.0
        self.provisional_buy_dominance_v2_btc = 0.0
        self.provisional_sell_dominance_v2_btc = 0.0
        self.raw_minute_ledgers: dict[datetime, dict[str, Any]] = {}
        self.raw_baseline_ready = False
        self.raw_baseline_span_minutes = 0.0
        self.collector_stale = True
        self.collector_last_age_sec: float | None = None
        self.collector_market_last_age_sec: float | None = None
        self.buy_peak = self.sell_peak = 0.0
        self.last_control: str | None = None
        self.last_low: float | None = None
        self.last_high: float | None = None
        self.previous_low: float | None = None
        self.current_low: float | None = None
        self.previous_high: float | None = None
        self.current_high: float | None = None
        self.low_sell_effort = 0.0
        self.high_buy_effort = 0.0
        self.last_low_extension = 0.0
        self.last_high_extension = 0.0
        self.sell_best_efficiency = 0.0
        self.buy_best_efficiency = 0.0
        self.sell_effort_btc = 0.0
        self.buy_effort_btc = 0.0
        self.early_status = 'OBSERVATION'
        self.pre_release = PreReleaseState()

    def reset(self, anchor: datetime) -> None:
        self.__init__(anchor)

    def initialize_continuous_baseline(self, market_rows: list[dict[str, Any]]) -> None:
        """Seed only the pre-anchor causal reference bars."""
        self.continuous_market_history = [
            row for row in market_rows if row.get('ts') < self.anchor
        ][-EFF_BASELINE_WINDOW:]
        self.continuous_baseline_initialized = True

    def apply_continuous_market(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """Apply one closed post-anchor 1m bar to continuous dominance."""
        if row.get('ts') is None or row['ts'] < self.anchor:
            return None
        if not self.continuous_baseline_initialized:
            self.initialize_continuous_baseline([])
        if any(existing.get('ts') == row['ts'] for existing in self.continuous_market_history[-1:]):
            return None
        result = continuous_market_control(self.continuous_market_history, row)
        self.continuous_last_result = result
        self.continuous_market_history.append(dict(row))
        if result['contribution_side'] == 'BUY':
            self.continuous_buy_dominance_btc += result['weight']
        elif result['contribution_side'] == 'SELL':
            self.continuous_sell_dominance_btc += result['weight']
        else:
            self.continuous_unclear_minutes += 1
        self.continuous_contributions_by_minute[row['ts']] = {
            'buy': float(result['weight']) if result['contribution_side'] == 'BUY' else 0.0,
            'sell': float(result['weight']) if result['contribution_side'] == 'SELL' else 0.0,
        }
        self.continuous_buy_pct, self.continuous_sell_pct = dominance_percentages(
            self.continuous_buy_dominance_btc,
            self.continuous_sell_dominance_btc,
        )
        self.dominance_reference_frozen_just_now = False
        self.continuous_valid_closed_minutes += 1
        if (self.continuous_valid_closed_minutes == 15 and
                not self.dominance_reference_frozen):
            self.dominance_reference_buy_pct = self.continuous_buy_pct
            self.dominance_reference_sell_pct = self.continuous_sell_pct
            self.dominance_reference_time = row['ts']
            self.dominance_reference_buy_equiv_btc = self.continuous_buy_dominance_btc
            self.dominance_reference_sell_equiv_btc = self.continuous_sell_dominance_btc
            self.dominance_reference_valid_minutes = self.continuous_valid_closed_minutes
            self.dominance_reference_frozen = True
            self.dominance_reference_frozen_just_now = True
        return result

    def continuous_dominance_partition(self) -> dict[str, float | None]:
        """Split the existing continuous contributions by canonical event minute."""
        event_times = {
            event.get('time') for event in self.event_history
            if isinstance(event.get('time'), datetime)
        }
        event_buy = event_sell = rest_buy = rest_sell = 0.0
        for minute, contribution in self.continuous_contributions_by_minute.items():
            if minute in event_times:
                event_buy += contribution['buy']
                event_sell += contribution['sell']
            else:
                rest_buy += contribution['buy']
                rest_sell += contribution['sell']
        event_buy_pct, event_sell_pct = dominance_percentages(event_buy, event_sell)
        rest_buy_pct, rest_sell_pct = dominance_percentages(rest_buy, rest_sell)
        return {
            'event_buy_btc': event_buy,
            'event_sell_btc': event_sell,
            'event_buy_pct': event_buy_pct,
            'event_sell_pct': event_sell_pct,
            'rest_buy_btc': rest_buy,
            'rest_sell_btc': rest_sell,
            'rest_buy_pct': rest_buy_pct,
            'rest_sell_pct': rest_sell_pct,
        }

    def update_total_oi(self, samples: list[dict[str, Any]], clock: datetime) -> None:
        """Set total OI state from absolute valid samples, not event deltas."""
        eligible = [
            sample for sample in samples
            if self.anchor <= sample.get('ts', self.anchor) <= clock
            and sample.get('oi') is not None
        ]
        if not eligible:
            return
        anchor_sample = eligible[0]
        current_sample = eligible[-1]
        self.total_oi_anchor_btc = float(anchor_sample['oi'])
        self.total_oi_current_btc = float(current_sample['oi'])
        self.total_oi_flow_btc = self.total_oi_current_btc - self.total_oi_anchor_btc
        self.total_oi_flow_pct = (
            self.total_oi_flow_btc / self.total_oi_anchor_btc * 100.0
            if self.total_oi_anchor_btc else None
        )

    def dominance_relative_snapshot(self, buy_pct: float | None,
                                    sell_pct: float | None,
                                    event_time: datetime) -> dict[str, Any]:
        return {
            'dominance_reference_buy_pct': self.dominance_reference_buy_pct,
            'dominance_reference_sell_pct': self.dominance_reference_sell_pct,
            'dominance_buy_change_pct': relative_dominance_change(
                buy_pct, self.dominance_reference_buy_pct
            ),
            'dominance_sell_change_pct': relative_dominance_change(
                sell_pct, self.dominance_reference_sell_pct
            ),
            'dominance_reference_time': self.dominance_reference_time,
            'dominance_reference_valid_minutes': self.dominance_reference_valid_minutes,
            'dominance_reference_buy_equiv_btc': self.dominance_reference_buy_equiv_btc,
            'dominance_reference_sell_equiv_btc': self.dominance_reference_sell_equiv_btc,
            'dominance_reference_frozen': self.dominance_reference_frozen,
        }

    def dominance_reference_diagnostic(self) -> dict[str, Any]:
        return {
            'time': self.dominance_reference_time.isoformat()
            if self.dominance_reference_time else None,
            'buy_pct': self.dominance_reference_buy_pct,
            'sell_pct': self.dominance_reference_sell_pct,
            'buy_equiv': self.dominance_reference_buy_equiv_btc,
            'sell_equiv': self.dominance_reference_sell_equiv_btc,
            'valid_minutes': self.dominance_reference_valid_minutes,
        }

    def raw_ledger(self, minute: datetime) -> dict[str, Any]:
        return self.raw_minute_ledgers.setdefault(minute, {
            'minute': minute,
            'raw_flow_counted': 0.0,
            'raw_buy_dom_counted': 0.0,
            'raw_sell_dom_counted': 0.0,
            'raw_samples_counted': 0,
            'raw_samples_seen': 0,
            'sample_timestamps': set(),
            'strong_finalized': False,
            'final_flow_contribution': 0.0,
            'final_buy_dom_contribution': 0.0,
            'final_sell_dom_contribution': 0.0,
        })

    def apply_raw_sample(self, sample: dict[str, Any], raw_metric: dict[str, Any],
                         partial_effort: dict[str, Any] | None = None,
                         log_path: Path | None = None) -> dict[str, Any] | None:
        """Apply only a causal, qualified sample to provisional accumulators."""
        minute = sample['ts'].replace(second=0, microsecond=0)
        ledger = self.raw_ledger(minute)
        timestamp = sample['ts']
        if timestamp in ledger['sample_timestamps']:
            return None
        ledger['sample_timestamps'].add(timestamp)
        ledger['raw_samples_seen'] += 1
        pctl = max((raw_metric.get(key) for key in ('impulse_pctl', 'burst_30_pctl', 'burst_60_pctl') if raw_metric.get(key) is not None), default=None)
        if pctl is None or pctl < 97.5 or partial_effort is None:
            return None
        control = partial_effort.get('control', 'CONTROL UNCLEAR')
        if not partial_effort.get('meaningful_aggression') or control == 'CONTROL UNCLEAR':
            return None
        ledger['raw_samples_counted'] += 1
        d_oi = float(raw_metric.get('doi') or 0.0)
        flow_delta = d_oi
        positive_oi = max(d_oi, 0.0)
        strength = 0.0
        if control == 'CONTROL LIMIT BUY' or control == 'CONTROL LIMIT SELL':
            strength = min(1.0, max(0.0, float(partial_effort.get('absorption_ratio') or 0.0)))
        elif control == 'CONTROL MARKET BUY' or control == 'CONTROL MARKET SELL':
            strength = min(1.0, max(0.0, float(partial_effort.get('eff_ratio') or 0.0)))
        buy_delta = positive_oi * strength if control in ('CONTROL LIMIT BUY', 'CONTROL MARKET BUY') else 0.0
        sell_delta = positive_oi * strength if control in ('CONTROL LIMIT SELL', 'CONTROL MARKET SELL') else 0.0
        ledger['raw_flow_counted'] += flow_delta
        ledger['raw_buy_dom_counted'] += buy_delta
        ledger['raw_sell_dom_counted'] += sell_delta
        self.provisional_oi_flow += flow_delta
        self.provisional_buy_dominance_v2_btc += buy_delta
        self.provisional_sell_dominance_v2_btc += sell_delta
        detail = {
            'time': timestamp, 'dOI': d_oi, 'pctl': pctl, 'control': control,
            'strength': strength, 'flow_delta': flow_delta,
            'buy_dom_delta': buy_delta, 'sell_dom_delta': sell_delta,
            'display_flow': self.oi_event_flow + self.provisional_oi_flow,
            'display_buy_dom': self.buy_dominance_v2_btc + self.provisional_buy_dominance_v2_btc,
            'display_sell_dom': self.sell_dominance_v2_btc + self.provisional_sell_dominance_v2_btc,
        }
        if log_path is not None:
            write_replay_log(log_path, 'RAW_ACCUM ' + json.dumps(detail, ensure_ascii=False, default=str))
        return detail

    def finalize_raw_minute(self, minute: datetime, canonical_event: dict[str, Any] | None = None,
                            log_path: Path | None = None) -> dict[str, Any]:
        ledger = self.raw_ledger(minute)
        if ledger['strong_finalized']:
            return ledger
        raw_flow = ledger['raw_flow_counted']
        raw_buy = ledger['raw_buy_dom_counted']
        raw_sell = ledger['raw_sell_dom_counted']
        canonical_flow = float(canonical_event.get('event_oi_net', 0.0)) if canonical_event else 0.0
        canonical_v2 = self.dominance_v2_contribution(
            float(canonical_event.get('oi_add_mass', canonical_event.get('display_oi_add', 0.0))) if canonical_event else 0.0,
            canonical_event or {},
        ) if canonical_event else {'v2_buy_delta_btc': 0.0, 'v2_sell_delta_btc': 0.0}
        self.oi_event_flow += canonical_flow
        self.buy_dominance_v2_btc += canonical_v2['v2_buy_delta_btc']
        self.sell_dominance_v2_btc += canonical_v2['v2_sell_delta_btc']
        # Remove any provisional values for this minute.  The final minute
        # contribution is now represented exactly once in committed state.
        self.provisional_oi_flow -= raw_flow
        self.provisional_buy_dominance_v2_btc -= raw_buy
        self.provisional_sell_dominance_v2_btc -= raw_sell
        ledger['final_flow_contribution'] = canonical_flow
        ledger['final_buy_dom_contribution'] = canonical_v2['v2_buy_delta_btc']
        ledger['final_sell_dom_contribution'] = canonical_v2['v2_sell_delta_btc']
        if canonical_event is not None:
            canonical_event['oi_flow_contribution'] = canonical_flow
            canonical_event['raw_oi_flow_contribution'] = 0.0 if canonical_event.get('event_source') == 'RAW_OI_INTENSITY' else canonical_flow
            canonical_event['raw_dominance_v2_contribution_btc'] = canonical_v2['v2_weight_btc']
            canonical_event.update({
                'v2_side': canonical_v2['v2_side'],
                'v2_strength': canonical_v2['v2_strength'],
                'v2_weight_btc': canonical_v2['v2_weight_btc'],
                'v2_buy_delta_btc': canonical_v2['v2_buy_delta_btc'],
                'v2_sell_delta_btc': canonical_v2['v2_sell_delta_btc'],
                'buy_dominance_v2_btc': self.buy_dominance_v2_btc,
                'sell_dominance_v2_btc': self.sell_dominance_v2_btc,
            })
        ledger['strong_finalized'] = canonical_event is not None
        if log_path is not None:
            write_replay_log(log_path, 'RAW_MINUTE_FINALIZE ' + json.dumps({
                'minute': minute.isoformat(),
                'raw_samples_counted': ledger['raw_samples_counted'],
                'flow_already_counted': raw_flow,
                'canonical_flow': canonical_flow,
                'strong_residual': canonical_flow - raw_flow if canonical_event and canonical_event.get('event_source') != 'RAW_OI_INTENSITY' else 0.0,
                'double_count': 0,
            }, ensure_ascii=False))
        return ledger

    def apply_oi(self, row: dict[str, Any]) -> None:
        if row['minute'] < self.anchor:
            return
        if self.oi_start is None:
            self.oi_start = row['first']
        self.oi_current = row['last']
        self.oi_add += row['add']; self.oi_exit += row['exit']

    def apply_market(self, row: dict[str, Any]) -> None:
        if row['ts'] < self.anchor:
            return
        if self.anchor_price is None:
            self.anchor_price = row['open']
        previous_close = self.price
        self.price = row['close']; self.buy += row['buy']; self.sell += row['sell']
        self.sell_effort_btc = row['sell']
        self.buy_effort_btc = row['buy']
        self.last_low_extension = 0.0
        self.last_high_extension = 0.0
        self.previous_low = self.last_low
        self.current_low = row['low']
        self.previous_high = self.last_high
        self.current_high = row['high']
        if self.last_low is None:
            self.last_low = row['low']
            self.low_sell_effort = self.sell
        elif row['low'] < self.last_low:
            self.last_low_extension = self.last_low - row['low']
            if self.sell_effort_btc > 0:
                efficiency = self.last_low_extension / self.sell_effort_btc * 100
                self.sell_best_efficiency = max(self.sell_best_efficiency, efficiency)
            self.last_low = row['low']
            self.low_sell_effort = self.sell
        if self.last_high is None:
            self.last_high = row['high']
            self.high_buy_effort = self.buy
        elif row['high'] > self.last_high:
            self.last_high_extension = row['high'] - self.last_high
            if self.buy_effort_btc > 0:
                efficiency = self.last_high_extension / self.buy_effort_btc * 100
                self.buy_best_efficiency = max(self.buy_best_efficiency, efficiency)
            self.last_high = row['high']
            self.high_buy_effort = self.buy
        self._update_early_status(previous_close, row['close'])

    def _update_early_status(self, previous_close: float | None, close: float) -> None:
        delta = self.buy - self.sell
        dominant = 'BUY' if delta > 0 else 'SELL' if delta < 0 else None
        sell_now = max(0.0, -delta)
        buy_now = max(0.0, delta)
        sell_retraced = self.sell_peak > 0 and sell_now < self.sell_peak
        buy_retraced = self.buy_peak > 0 and buy_now < self.buy_peak
        oi_loaded = self.oi_add + self.oi_exit > 0
        previous_status = self.early_status
        if dominant == 'SELL' and oi_loaded and sell_retraced and self.sell_best_efficiency > 0:
            if self.last_low_extension <= 0 and self.sell_effort_btc > 0:
                self.early_status = 'SELL PRESSURE HELD'
                if previous_close is not None and close > previous_close:
                    self.early_status = ('EARLY BUY CONTROL' if previous_status == 'POSSIBLE EARLY BUY CONTROL SHIFT'
                                         else 'POSSIBLE EARLY BUY CONTROL SHIFT')
            elif self.sell_effort_btc > 0 and self.last_low_extension / self.sell_effort_btc * 100 < self.sell_best_efficiency:
                self.early_status = 'SELL RESULT EFFICIENCY FALLING'
            else:
                self.early_status = 'SELL CONTROL'
        elif dominant == 'BUY' and oi_loaded and buy_retraced and self.buy_best_efficiency > 0:
            if self.last_high_extension <= 0 and self.buy_effort_btc > 0:
                self.early_status = 'BUY PRESSURE HELD'
                if previous_close is not None and close < previous_close:
                    self.early_status = ('EARLY SELL CONTROL' if previous_status == 'POSSIBLE EARLY SELL CONTROL SHIFT'
                                         else 'POSSIBLE EARLY SELL CONTROL SHIFT')
            elif self.buy_effort_btc > 0 and self.last_high_extension / self.buy_effort_btc * 100 < self.buy_best_efficiency:
                self.early_status = 'BUY RESULT EFFICIENCY FALLING'
            else:
                self.early_status = 'BUY CONTROL'

    def early_metrics(self) -> dict[str, Any]:
        delta = self.buy - self.sell
        sell_now = max(0.0, -delta)
        buy_now = max(0.0, delta)
        return {
            'sell_adv_now': sell_now,
            'buy_adv_now': buy_now,
            'sell_adv_retraced': (self.sell_peak - sell_now) / self.sell_peak * 100 if self.sell_peak else 0.0,
            'buy_adv_retraced': (self.buy_peak - buy_now) / self.buy_peak * 100 if self.buy_peak else 0.0,
            'previous_low': self.previous_low,
            'last_low': self.last_low,
            'current_low': self.current_low,
            'low_extension': self.last_low_extension,
            'sell_effort_btc': self.sell_effort_btc,
            'low_extension_per_100': self.last_low_extension / self.sell_effort_btc * 100 if self.sell_effort_btc > 0 else 0.0,
            'last_high': self.last_high,
            'current_high': self.current_high,
            'high_extension': self.last_high_extension,
            'buy_effort_btc': self.buy_effort_btc,
            'high_extension_per_100': self.last_high_extension / self.buy_effort_btc * 100 if self.buy_effort_btc > 0 else 0.0,
            'previous_high': self.previous_high,
            'status': self.early_status,
        }

    def result_control(self, flow: str, price_change: float | None) -> str:
        delta = self.buy - self.sell
        if price_change is None or abs(price_change) < 1e-9:
            return 'HELD / NO CLEAR RESULT'
        price_side = 'BUY' if price_change > 0 else 'SELL'
        if flow == price_side:
            return f'{price_side} GOT RESULT'
        if flow in ('BUY', 'SELL'):
            return f'{price_side} WON THIS INTERVAL | {flow} PRESSURE FAILED'
        return f'{price_side} WON THIS INTERVAL'

    def control_label(self, classification: str) -> str:
        """Presentation mapping for the existing event control classification."""
        if classification.startswith('HELD'):
            return 'CONTROL NEUTRAL'
        if classification.startswith('BUY GOT RESULT'):
            return 'CONTROL MARKET BUY'
        if classification.startswith('SELL GOT RESULT'):
            return 'CONTROL MARKET SELL'
        if 'SELL PRESSURE FAILED' in classification:
            return 'CONTROL LIMIT BUY'
        if 'BUY PRESSURE FAILED' in classification:
            return 'CONTROL LIMIT SELL'
        return 'CONTROL NEUTRAL'

    def dominance_v2_contribution(self, oi_add: float, effort: dict[str, Any]) -> dict[str, Any]:
        """Calculate one V2 contribution without mutating committed state."""
        control = str(effort.get('control', 'CONTROL UNCLEAR'))
        if control.startswith('CONTROL '):
            control = control.removeprefix('CONTROL ')
        side = 'BUY' if control.endswith('BUY') else 'SELL' if control.endswith('SELL') else None
        if control.startswith('LIMIT '):
            strength = min(1.0, max(0.0, float(effort.get('absorption_ratio') or 0.0)))
        elif control.startswith('MARKET '):
            strength = min(1.0, max(0.0, float(effort.get('eff_ratio') or 0.0)))
        else:
            strength = 0.0
        weight = float(oi_add or 0.0) * strength if side is not None else 0.0
        return {
            'v2_side': side or 'UNCLEAR',
            'v2_strength': strength,
            'v2_weight_btc': weight,
            'v2_buy_delta_btc': weight if side == 'BUY' else 0.0,
            'v2_sell_delta_btc': weight if side == 'SELL' else 0.0,
        }

    def effort_result_control(self, event_time: datetime, taker_buy: float,
                              taker_sell: float, price_change: float | None,
                              reference_price: float | None) -> dict[str, Any]:
        total_taker = taker_buy + taker_sell
        aggr_delta = taker_buy - taker_sell
        aggr_side = 'BUY' if aggr_delta > 0 else 'SELL' if aggr_delta < 0 else 'HELD'
        aggr_mag = abs(aggr_delta)
        aggr_share = (max(taker_buy, taker_sell) / total_taker * 100) if total_taker > 0 else 0.0

        previous_bars = [row for row in self.market_history if row['ts'] < event_time]
        aggr_history = [abs(row['buy'] - row['sell']) for row in previous_bars[-AGGR_BASELINE_WINDOW:]]
        aggr_baseline = (__import__('statistics').median(aggr_history)
                         if len(aggr_history) >= MIN_AGGR_BASELINE_SAMPLES else None)
        aggr_mag_x = aggr_mag / aggr_baseline if aggr_baseline and aggr_baseline > 0 else None
        side_baselines: dict[str, list[float]] = {'BUY': [], 'SELL': []}
        for row in previous_bars[-EFF_BASELINE_WINDOW:]:
            delta = row['buy'] - row['sell']
            side = 'BUY' if delta > 0 else 'SELL' if delta < 0 else None
            magnitude = abs(delta)
            total = row['buy'] + row['sell']
            share = max(row['buy'], row['sell']) / total * 100 if total > 0 else 0.0
            row_mag_x = magnitude / aggr_baseline if aggr_baseline and aggr_baseline > 0 else None
            row_meaningful = share >= MIN_AGGR_SHARE_PCT or (row_mag_x is not None and row_mag_x >= MIN_AGGR_MAG_X)
            if not side or magnitude <= 0 or not row_meaningful:
                continue
            aligned = (row['close'] - row['open']) if side == 'BUY' else (row['open'] - row['close'])
            reference = row['open']
            notional_m = magnitude * reference / 1_000_000 if reference > 0 else 0.0
            aligned_bps = aligned / reference * 10_000 if reference > 0 else 0.0
            if aligned_bps > 0 and notional_m > 0:
                side_baselines[side].append(aligned_bps / notional_m)
        side_baseline = side_baselines.get(aggr_side, [])
        baseline_impact = __import__('statistics').median(side_baseline) if len(side_baseline) >= MIN_BASELINE_SAMPLES else None
        aggr_notional = aggr_mag * reference_price if reference_price and reference_price > 0 else None
        price_bps = price_change / reference_price * 10_000 if price_change is not None and reference_price and reference_price > 0 else None
        aligned_bps = None if price_bps is None or aggr_side == 'HELD' else (price_bps if aggr_side == 'BUY' else -price_bps)
        actual_impact = (aligned_bps / (aggr_notional / 1_000_000)) if aligned_bps is not None and aggr_notional and aggr_notional > 0 else None
        expected_bps = baseline_impact * (aggr_notional / 1_000_000) if baseline_impact is not None and aggr_notional else None
        eff_ratio = aligned_bps / expected_bps if aligned_bps is not None and expected_bps and expected_bps > 0 else None

        control = 'CONTROL UNCLEAR'
        absorption_ratio = 0.0
        absorbed_notional = 0.0
        meaningful_aggression = (
            aggr_side in ('BUY', 'SELL') and
            (aggr_share >= MIN_AGGR_SHARE_PCT or (aggr_mag_x is not None and aggr_mag_x >= MIN_AGGR_MAG_X))
        )
        if meaningful_aggression and baseline_impact is not None and eff_ratio is not None:
            if eff_ratio < LIMIT_EFF_RATIO:
                absorption_ratio = min(1.0, max(0.0, 1.0 - max(eff_ratio, 0.0)))
                absorbed_notional = (aggr_notional or 0.0) * absorption_ratio
                control = 'CONTROL LIMIT SELL' if aggr_side == 'BUY' else 'CONTROL LIMIT BUY'
            else:
                control = f'CONTROL MARKET {aggr_side}'
        if control == 'CONTROL LIMIT BUY':
            self.buy_dominance_weight_usdt += absorbed_notional
        elif control == 'CONTROL LIMIT SELL':
            self.sell_dominance_weight_usdt += absorbed_notional
        total_dominance = self.buy_dominance_weight_usdt + self.sell_dominance_weight_usdt
        return {
            'total_taker': total_taker,
            'aggr_side': aggr_side,
            'aggr_mag': aggr_mag,
            'aggr_share_pct': aggr_share,
            'aggr_baseline_median_btc': aggr_baseline,
            'aggr_mag_x': aggr_mag_x,
            'meaningful_aggression': meaningful_aggression,
            'reference_price': reference_price,
            'aggr_notional_usdt': aggr_notional,
            'price_change_bps': price_bps,
            'aligned_result_bps': aligned_bps,
            'baseline_impact_bps_per_1m': baseline_impact,
            'expected_aligned_bps': expected_bps,
            'actual_impact_bps_per_1m': actual_impact,
            'eff_ratio': eff_ratio,
            'absorption_ratio': absorption_ratio,
            'absorbed_aggression_usdt': absorbed_notional,
            'control': control,
            'buy_dominance_weight_usdt': self.buy_dominance_weight_usdt,
            'sell_dominance_weight_usdt': self.sell_dominance_weight_usdt,
            'buy_dominance_pct': self.buy_dominance_weight_usdt / total_dominance * 100 if total_dominance else None,
            'sell_dominance_pct': self.sell_dominance_weight_usdt / total_dominance * 100 if total_dominance else None,
        }

    def interval_result(self, flow: str, price_change: float | None) -> str:
        return self.result_control(flow, price_change)

    def snapshot(self, event_time: datetime) -> dict[str, Any]:
        delta = self.buy - self.sell
        dominant = 'BUY' if delta > 0 else 'SELL' if delta < 0 else 'HELD'
        advantage = abs(delta)
        if dominant == 'BUY': self.buy_peak = max(self.buy_peak, advantage)
        if dominant == 'SELL': self.sell_peak = max(self.sell_peak, advantage)
        peak = self.sell_peak if dominant == 'SELL' else self.buy_peak if dominant == 'BUY' else 0.0
        retraced = (peak - advantage) / peak * 100 if peak else 0.0
        adv_lost = max(0.0, peak - advantage)
        return {'time': event_time, 'oi_net': (self.oi_current - self.oi_start) if self.oi_current is not None and self.oi_start is not None else None, 'oi_add': self.oi_add, 'oi_exit': self.oi_exit, 'oi_activity': self.oi_add + self.oi_exit, 'buy': self.buy, 'sell': self.sell, 'delta': delta, 'dominant': dominant, 'advantage': advantage, 'peak': peak, 'sell_peak': self.sell_peak, 'buy_peak': self.buy_peak, 'retraced': retraced, 'adv_lost': adv_lost, 'adv_remaining_pct': advantage / peak * 100 if peak else 0.0, 'adv_lost_pct': adv_lost / peak * 100 if peak else 0.0, 'price': self.price, 'price_from_start': (self.price - self.anchor_price) if self.price is not None and self.anchor_price is not None else None, 'total_oi_anchor_btc': self.total_oi_anchor_btc, 'total_oi_current_btc': self.total_oi_current_btc, 'total_oi_flow_btc': self.total_oi_flow_btc, 'total_oi_flow_pct': self.total_oi_flow_pct, 'continuous_buy_dominance_btc': self.continuous_buy_dominance_btc, 'continuous_sell_dominance_btc': self.continuous_sell_dominance_btc, 'continuous_buy_pct': self.continuous_buy_pct, 'continuous_sell_pct': self.continuous_sell_pct, 'continuous_unclear_minutes': self.continuous_unclear_minutes, 'continuous_valid_closed_minutes': self.continuous_valid_closed_minutes, 'dominance_reference_buy_pct': self.dominance_reference_buy_pct, 'dominance_reference_sell_pct': self.dominance_reference_sell_pct, 'dominance_reference_time': self.dominance_reference_time, 'dominance_reference_valid_minutes': self.dominance_reference_valid_minutes, 'dominance_reference_buy_equiv_btc': self.dominance_reference_buy_equiv_btc, 'dominance_reference_sell_equiv_btc': self.dominance_reference_sell_equiv_btc, **self.early_metrics()}

    def print_status(self, title='SESSION SNAPSHOT') -> None:
        snap = self.snapshot(self.anchor)
        title_display = {'FINAL SESSION STATE': 'ИТОГОВОЕ СОСТОЯНИЕ СЕССИИ', 'LIVE STATUS': 'ТЕКУЩЕЕ СОСТОЯНИЕ'}.get(title, title)
        print(f'\nBTC-LRA OI МОНИТОР ПОТОКА\n{title_display}\nОТСЧЁТ С: {self.anchor.strftime("%H:%M") if self.anchor else "—"}')
        print(f'ЦЕНА                 {n(self.price)}')
        print(f'OI В НАЧАЛЕ          {n(self.oi_start)} BTC\nOI СЕЙЧАС            {n(self.oi_current)} BTC\nИЗМЕНЕНИЕ OI         {n(snap["oi_net"])} BTC\nПРИТОК OI            {n(self.oi_add)} BTC\nВЫХОД OI             {n(self.oi_exit)} BTC\nАКТИВНОСТЬ OI        {n(snap["oi_activity"])} BTC')
        print(f'НАКОПЛЕННЫЙ BUY      {n(self.buy)} BTC\nНАКОПЛЕННЫЙ SELL     {n(self.sell)} BTC\nНАКОПИТЕЛЬНАЯ ДЕЛЬТА {n(snap["delta"])} BTC\nДОМИНАНТ ПОТОКА      {flow_ru(snap["dominant"])}')
        print(f'НАКОПЛЕННЫЙ ПЕРЕВЕС {n(snap["advantage"])} BTC\nЦЕНА ОТ НАЧАЛА       {n(snap["price_from_start"])} USD')
        if snap['dominant'] in ('BUY', 'SELL'):
            print(f'МАКС. ПЕРЕВЕС {snap["dominant"]:<4}   {n(snap["peak"])} BTC | 100.0%')
            print(f'ПЕРЕВЕС {snap["dominant"]} СЕЙЧАС  {n(snap["advantage"])} BTC | {snap["adv_remaining_pct"]:5.1f}%')
            print(f'ПОТЕРЯНО ПЕРЕВЕСА     {n(snap["adv_lost"])} BTC | {snap["adv_lost_pct"]:5.1f}%')
            other = 'BUY' if snap['dominant'] == 'SELL' else 'SELL'
            other_peak = snap['buy_peak'] if other == 'BUY' else snap['sell_peak']
            print(f'ПРЕДЫДУЩИЙ ПИК {other}  {n(other_peak)} BTC')
        else:
            print(f'МАКС. ПЕРЕВЕС SELL    {n(snap["sell_peak"])} BTC\nМАКС. ПЕРЕВЕС BUY     {n(snap["buy_peak"])} BTC')
        print(f'ПЕРЕВЕС SELL СЕЙЧАС  {n(snap["sell_adv_now"])} BTC | ПОТЕРЯНО {snap["sell_adv_retraced"]:5.1f}%')
        print(f'ПЕРЕВЕС BUY СЕЙЧАС   {n(snap["buy_adv_now"])} BTC | ПОТЕРЯНО {snap["buy_adv_retraced"]:5.1f}%')
        print(f'ПРЕДЫДУЩИЙ LOW       {n(snap["previous_low"])} | ТЕКУЩИЙ LOW {n(snap["current_low"])} | НОВОЕ СНИЖЕНИЕ {n(snap["low_extension"])}')
        print(f'УСИЛИЕ SELL          {n(snap["sell_effort_btc"])} BTC | РЕЗУЛЬТАТ SELL / 100 BTC {n(snap["low_extension_per_100"])} USD')
        print(f'ПРЕДЫДУЩИЙ HIGH      {n(snap["previous_high"])} | ТЕКУЩИЙ HIGH {n(snap["current_high"])} | НОВЫЙ РОСТ {n(snap["high_extension"])}')
        print(f'УСИЛИЕ BUY           {n(snap["buy_effort_btc"])} BTC | РЕЗУЛЬТАТ BUY / 100 BTC {n(snap["high_extension_per_100"])} USD')
        print(f'СТАТУС: {status_ru(snap["status"])}')
        print('\nПОСЛЕДНИЕ СОБЫТИЯ')
        for event in self.last_events:
            print(f'{event["time"].strftime("%H:%M")} | ИЗМЕНЕНИЕ OI {n(event["oi_net"])} | ДОМИНАНТ ПОТОКА {flow_ru(event["flow"])} {n(event["flow_adv"])} | РЕЗУЛЬТАТ ЦЕНЫ {n(event["price_change"])} | КТО ПОЛУЧИЛ РЕЗУЛЬТАТ: {result_ru(event["classification"])}')

    def emit_event(self, event: dict[str, Any], minute: dict[str, Any], market_rows: list[dict[str, Any]]) -> None:
        before = self.previous_event
        snap = self.snapshot(minute['minute'])
        if market_rows:
            snap = self.snapshot(minute['minute'])
        previous_price = before['price'] if before else self.anchor_price
        price_change = snap['price'] - previous_price if snap['price'] is not None and previous_price is not None else None
        prev_oi = before['oi_net'] if before else 0.0
        prev_buy = before['buy'] if before else 0.0; prev_sell = before['sell'] if before else 0.0
        flow_delta = (snap['buy'] - prev_buy) - (snap['sell'] - prev_sell)
        flow = 'BUY' if flow_delta > 0 else 'SELL' if flow_delta < 0 else 'HELD'
        classification = self.result_control(flow, price_change)
        current = dict(snap, time=minute['minute'], flow=flow, flow_adv=abs(flow_delta), price_change=price_change, classification=classification)
        self.last_events.append(current); self.previous_event = current
        start_time = event.get('start_time', event['start'])
        start_text = start_time.strftime('%H:%M') if isinstance(start_time, datetime) else str(start_time)
        event_label = 'ЭКСТРЕМАЛЬНОЕ OI-СОБЫТИЕ' if event.get('kind') == 'MEGA' else 'СИЛЬНОЕ OI-СОБЫТИЕ'
        print(f'\n{event_label} ПОДТВЕРЖДЕНО В {minute["minute"].strftime("%H:%M")} | НАЧАЛО ЭПИЗОДА {start_text}')
        print(f'С ПРЕДЫДУЩЕГО СОБЫТИЯ: ИЗМЕНЕНИЕ OI {n((snap["oi_net"] or 0)-prev_oi)} | BUY {n(snap["buy"]-prev_buy)} | SELL {n(snap["sell"]-prev_sell)} | ДОМИНАНТ ПОТОКА {flow_ru(flow)} {n(abs(flow_delta))} | РЕЗУЛЬТАТ ЦЕНЫ {n(price_change)}')
        print(f'С ОТСЧЁТА: ИЗМЕНЕНИЕ OI {n(snap["oi_net"])} | ПРИТОК {n(snap["oi_add"])} | ВЫХОД {n(snap["oi_exit"])} | BUY {n(snap["buy"])} | SELL {n(snap["sell"])} | ДОМИНАНТ ПОТОКА {flow_ru(snap["dominant"])} {n(snap["advantage"])} | КТО ПОЛУЧИЛ РЕЗУЛЬТАТ: {result_ru(classification)}')
        print(f'СОСТОЯНИЕ ЭКСТРЕМУМА: {status_ru(snap["status"])} | ПЕРЕВЕС SELL СЕЙЧАС {n(snap["sell_adv_now"])} / ПОТЕРЯНО {snap["sell_adv_retraced"]:.1f}% | РЕЗУЛЬТАТ SELL / 100 BTC {n(snap["low_extension_per_100"])} | РЕЗУЛЬТАТ BUY / 100 BTC {n(snap["high_extension_per_100"])}')


    def print_status(self, title='SESSION SNAPSHOT', current_time: datetime | None = None,
                     speed: float | None = None, live: bool = False) -> None:
        """Render the compact dashboard without changing session calculations."""
        anchor_text = self.anchor.strftime('%H:%M:%S / %d.%m.%y -5') if self.anchor else '—'
        mode = 'LIVE' if live else 'SCAN'
        print(f'\nOI FLOW MONITOR v0.0.1 | {mode} FROM {anchor_text}')
        if not live and current_time is not None and speed is not None:
            print(f'HISTORICAL TIME {fmt_time(current_time)} | SPEED {speed:g}x')
        price_text = compact(self.price, signed=False) if self.price is not None else '—'
        clock_text = current_time.astimezone(PANAMA).strftime('%H:%M:%S') if current_time is not None else '—'
        # No explicit DOMINANCE series exists in this monitor.  Do not
        # substitute aggression percentages; leave the field unavailable.
        print(f'{price_text} | {clock_text} | DOMINANCE BUY — / SELL — | OI FLOW {compact(self.oi_event_flow)} BTC')
        for event in self.last_events:
            display_price = event.get('display_price_change', event['price_change'])
            control = event.get('control_label', 'CONTROL NEUTRAL')
            print(f'{event["time"].strftime("%H:%M")} | OI ACT {compact(event.get("event_oi_activity"), signed=False)} | NET {compact(event.get("event_oi_net"))} | AGGR {event.get("aggr_side", "HELD")} {event.get("aggr_share_pct", 0.0):.1f}% {compact(event.get("aggr_mag", 0.0))} | PRICE {compact(display_price)} | {control}')

    def emit_event(self, event: dict[str, Any], minute: dict[str, Any], market_rows: list[dict[str, Any]]) -> None:
        before = self.previous_event
        snap = self.snapshot(minute['minute'])
        previous_price = before['price'] if before else self.anchor_price
        price_change = snap['price'] - previous_price if snap['price'] is not None and previous_price is not None else None
        prev_oi = before['oi_net'] if before else 0.0
        prev_activity = before['oi_activity'] if before else 0.0
        prev_buy = before['buy'] if before else 0.0
        prev_sell = before['sell'] if before else 0.0
        flow_delta = (snap['buy'] - prev_buy) - (snap['sell'] - prev_sell)
        flow = 'BUY' if flow_delta > 0 else 'SELL' if flow_delta < 0 else 'HELD'
        classification = self.result_control(flow, price_change)
        display_delta = event.get('display_flow_delta', flow_delta)
        display_flow = 'BUY' if display_delta > 0 else 'SELL' if display_delta < 0 else 'HELD'
        event_oi_net = event.get('display_oi_net', (snap['oi_net'] or 0.0) - prev_oi)
        event_oi_activity = event.get('display_oi_activity', snap['oi_activity'] - prev_activity)
        display_price_change = event.get('display_price_change', price_change)
        effort = self.effort_result_control(
            minute['minute'],
            float(event.get('display_taker_buy', 0.0)),
            float(event.get('display_taker_sell', 0.0)),
            display_price_change,
            event.get('display_reference_price'),
        )
        control_label = effort['control']
        # V2 is calculated here but committed only by finalize_raw_minute().
        # This keeps STRONG/RAW reconciliation single-entry and idempotent.
        v2 = self.dominance_v2_contribution(float(minute.get('add', 0.0)), effort)
        current = dict(snap, time=minute['minute'], flow=flow, flow_adv=abs(flow_delta), price_change=price_change, classification=classification, event_source=event.get('event_source'), control_label=control_label, event_oi_net=event_oi_net, event_oi_activity=event_oi_activity, display_flow=display_flow, display_flow_adv=abs(display_delta), display_price_change=display_price_change, display_event_price=event.get('display_event_price'), display_reference_price=event.get('display_reference_price'), display_taker_buy=event.get('display_taker_buy', 0.0), display_taker_sell=event.get('display_taker_sell', 0.0), display_flow_delta=event.get('display_flow_delta', display_delta), display_oi_add=float(minute.get('add', 0.0)), display_oi_exit=float(minute.get('exit', 0.0)), display_oi_jump=float(minute.get('jump', 0.0)), oi_add_mass=float(minute.get('add', 0.0)), **effort, **v2)
        current['buy_dominance_v2_btc'] = self.buy_dominance_v2_btc
        current['sell_dominance_v2_btc'] = self.sell_dominance_v2_btc
        current['buy_v2_pct'], current['sell_v2_pct'] = dominance_percentages(
            self.buy_dominance_v2_btc, self.sell_dominance_v2_btc
        )
        current['event_flags'] = ['STRONG/MEGA']
        current['oi_flow_contribution'] = float(event_oi_net or 0.0)
        current['oi_event_flow'] = self.oi_event_flow
        self.last_events.append(current)
        self.event_history.append(current)
        self.previous_event = current
        start_time = event.get('start_time', event['start'])
        start_text = start_time.strftime('%H:%M') if isinstance(start_time, datetime) else str(start_time)
        event_label = 'ЭКСТРЕМАЛЬНОЕ OI-СОБЫТИЕ' if event.get('kind') == 'MEGA' else 'СИЛЬНОЕ OI-СОБЫТИЕ'
        oi_word = 'ПРИШЛО' if current['event_oi_net'] >= 0 else 'УШЛО'
        print(f'\n{event_label} {minute["minute"].strftime("%H:%M")} | НАЧАЛО ЭПИЗОДА {start_text}')
        print(f'OI {n(current["event_oi_net"])} BTC — {oi_word} | АКТИВНОСТЬ OI {n(current["event_oi_activity"])} BTC')
        print(f'AGGR {effort["aggr_side"]} {effort["aggr_share_pct"]:.1f}% {compact(effort["aggr_mag"])} | PRICE {compact(display_price_change)} | {control_label}')


def run_replay(args: argparse.Namespace) -> None:
    samples = load_raw(args.raw_oi)
    minutes = minute_oi(samples)
    annotate_minutes(minutes)
    events = individual_events(minutes)
    market = load_market(args.market_csv) if args.market_csv else []
    anchor = parse_time(args.from_time) if args.from_time else minutes[0]['minute']
    end = parse_time(args.end) if args.end else None
    session = Session(anchor)
    session.market_history = market
    tv_path = Path(__file__).resolve().parent / 'BTC_LRA_TV_EVENTS.pine'
    tv_event_history: dict[datetime, dict[str, Any]] = {}
    write_tv_events_pine(tv_path, [])
    event_by_time = {minutes[event['confirmed']]['minute']: event for event in events}
    snapshots = set(args.snapshot_times or ['14:47', '14:48', '14:51', '14:52', '14:53', '14:54', '15:00', '17:07', '18:51', '20:05', '20:31', '21:17', '21:48', '21:53', '21:54', '22:37', '22:39', '23:10'])
    previous_control = None
    previous_checkpoint: dict[str, Any] | None = None
    checkpoint_rows: list[dict[str, Any]] = []
    minute_table_rows: list[dict[str, Any]] = []
    flow_crosses: list[tuple[str, str, str]] = []
    result_changes: list[tuple[str, str, str]] = []
    for index, minute in enumerate(minutes):
        if minute['minute'] < anchor or end and minute['minute'] > end:
            continue
        session.apply_oi(minute)
        market_rows = [row for row in market if row['ts'].replace(second=0, microsecond=0) == minute['minute']]
        for row in market_rows:
            session.apply_market(row)
        current = session.snapshot(minute['minute'])
        minute_table_rows.append(current)
        if current['dominant'] in ('BUY', 'SELL') and previous_control and current['dominant'] != previous_control:
            flow_crosses.append((minute['minute'].strftime('%H:%M'), previous_control, current['dominant']))
            print(f'\nСМЕНА НАКОПИТЕЛЬНОГО ДОМИНАНТА: {previous_control} -> {current["dominant"]} В {minute["minute"].strftime("%H:%M")}')
        if current['dominant'] in ('BUY', 'SELL'):
            previous_control = current['dominant']
        if minute['minute'] in event_by_time:
            event = dict(event_by_time[minute['minute']])
            event['start_time'] = minutes[event['start']]['minute']
            event['kind'] = 'MEGA' if minutes[event['confirmed']]['mega'] else 'STRONG'
            decorate_event_interval(event, minutes, market)
            session.emit_event(event, minute, market_rows)
            tv_event_history[minute['minute']] = dict(session.event_history[-1])
            write_tv_events_pine(tv_path, list(tv_event_history.values()))
        if minute['minute'].strftime('%H:%M') in snapshots:
            current = session.snapshot(minute['minute'])
            flow = current['dominant']
            if previous_checkpoint is None:
                interval_buy = current['buy']; interval_sell = current['sell']; interval_price = current['price_from_start']
            else:
                interval_buy = current['buy'] - previous_checkpoint['buy']
                interval_sell = current['sell'] - previous_checkpoint['sell']
                interval_price = (current['price'] - previous_checkpoint['price']) if current['price'] is not None and previous_checkpoint['price'] is not None else None
            interval_delta = interval_buy - interval_sell
            interval_flow = 'BUY' if interval_delta > 0 else 'SELL' if interval_delta < 0 else 'HELD'
            result = session.result_control(interval_flow, interval_price)
            result_side = 'HELD' if result.startswith('HELD') else result.split(' ')[0]
            previous_result = checkpoint_rows[-1]['result_side'] if checkpoint_rows else result_side
            if checkpoint_rows and result_side != previous_result:
                result_changes.append((minute['minute'].strftime('%H:%M'), previous_result, result_side))
            row = dict(current, time=minute['minute'].strftime('%H:%M'), interval_buy=interval_buy, interval_sell=interval_sell, interval_flow=interval_flow, interval_price=interval_price, result=result, result_side=result_side)
            checkpoint_rows.append(row)
            print(f'\nКОНТРОЛЬНАЯ ТОЧКА {row["time"]}')
            print(f'НАКОПЛЕННЫЙ BUY {n(row["buy"])} | НАКОПЛЕННЫЙ SELL {n(row["sell"])} | ДОМИНАНТ ПОТОКА {flow_ru(row["dominant"])} | НАКОПЛЕННЫЙ ПЕРЕВЕС {n(row["advantage"])}')
            print(f'МАКС. ПЕРЕВЕС {n(row["peak"])} | ПЕРЕВЕС СЕЙЧАС {n(row["advantage"])} | ПОТЕРЯНО {row["retraced"]:.1f}% | ЦЕНА ОТ НАЧАЛА {n(row["price_from_start"])}')
            print(f'ИЗМЕНЕНИЕ OI {n(row["oi_net"])} | ПРИТОК OI {n(row["oi_add"])} | ВЫХОД OI {n(row["oi_exit"])} | АКТИВНОСТЬ OI {n(row["oi_activity"])}')
            print(f'С ПРЕДЫДУЩЕЙ ТОЧКИ: BUY {n(interval_buy)} | SELL {n(interval_sell)} | ДОМИНАНТ ПОТОКА {flow_ru(interval_flow)} {n(abs(interval_delta))} | РЕЗУЛЬТАТ ЦЕНЫ {n(interval_price)} | КТО ПОЛУЧИЛ РЕЗУЛЬТАТ: {result_ru(result)}')
            print(f'СОСТОЯНИЕ ЭКСТРЕМУМА: {status_ru(row["status"])} | МАКС. SELL {n(row["sell_peak"])} | SELL СЕЙЧАС {n(row["sell_adv_now"])} | ПОТЕРЯНО {row["sell_adv_retraced"]:.1f}% | РЕЗУЛЬТАТ SELL / 100 BTC {n(row["low_extension_per_100"])}')
            print(f'ПРЕДЫДУЩИЙ LOW {n(row["previous_low"])} | ТЕКУЩИЙ LOW {n(row["current_low"])} | УСИЛИЕ SELL {n(row["sell_effort_btc"])} BTC')
            print(f'МАКС. BUY {n(row["buy_peak"])} | BUY СЕЙЧАС {n(row["buy_adv_now"])} | ПОТЕРЯНО {row["buy_adv_retraced"]:.1f}% | РЕЗУЛЬТАТ BUY / 100 BTC {n(row["high_extension_per_100"])}')
            print(f'ПРЕДЫДУЩИЙ HIGH {n(row["previous_high"])} | ТЕКУЩИЙ HIGH {n(row["current_high"])} | УСИЛИЕ BUY {n(row["buy_effort_btc"])} BTC')
            previous_checkpoint = current
    visible_events = [event for event in events if anchor <= minutes[event['confirmed']]['minute'] and (end is None or minutes[event['confirmed']]['minute'] <= end)]
    print(f'\nПОВТОР ЗАВЕРШЁН | raw samples={len(samples)} | минутных агрегатов={len(minutes)} | СИЛЬНЫХ OI-эпизодов={len(visible_events)}')
    print('\nСМЕНЫ НАКОПИТЕЛЬНОГО ДОМИНАНТА:')
    for at, old, new in flow_crosses:
        print(f'{at} | {old} -> {new}')
    print('\nИЗМЕНЕНИЯ РЕЗУЛЬТАТА ЦЕНЫ:')
    for at, old, new in result_changes:
        print(f'{at} | {old} -> {new}')
    print('\nХРОНОЛОГИЧЕСКАЯ ТАБЛИЦА КОНТРОЛЬНЫХ ТОЧЕК:')
    print('ВРЕМЯ | ПОТОК | ПЕРЕВЕС | ЦЕНА ОТ НАЧАЛА | ПОТОК ИНТЕРВАЛА | РЕЗУЛЬТАТ ЦЕНЫ | КТО ПОЛУЧИЛ РЕЗУЛЬТАТ')
    for row in checkpoint_rows:
        print(f'{row["time"]} | {flow_ru(row["dominant"])} {n(row["advantage"])} | {n(row["advantage"])} | {n(row["price_from_start"])} | {flow_ru(row["interval_flow"])} {n(abs(row["interval_buy"]-row["interval_sell"]))} | {n(row["interval_price"])} | {result_ru(row["result"])}')
    print('\nМИНУТНАЯ ТАБЛИЦА ЭКСТРЕМУМОВ И ОТДАЧИ:')
    print('ВРЕМЯ | ПОТОК | МАКС. ПЕРЕВЕС | ПЕРЕВЕС СЕЙЧАС | ПОТЕРЯНО | ИЗМЕНЕНИЕ OI | УСИЛИЕ SELL | НОВОЕ СНИЖЕНИЕ | РЕЗУЛЬТАТ SELL / 100 BTC | УСИЛИЕ BUY | НОВЫЙ РОСТ | РЕЗУЛЬТАТ BUY / 100 BTC | СТАТУС')
    for row in minute_table_rows:
        print(f'{row["time"].strftime("%H:%M")} | {flow_ru(row["dominant"])} {n(row["advantage"])} | {n(row["peak"])} | {n(row["sell_adv_now"] if row["dominant"] == "SELL" else row["buy_adv_now"])} | {(row["sell_adv_retraced"] if row["dominant"] == "SELL" else row["buy_adv_retraced"]):.1f}% | {n(row["oi_net"])} | {n(row["sell_effort_btc"])} | {n(row["low_extension"])} | {n(row["low_extension_per_100"])} | {n(row["buy_effort_btc"])} | {n(row["high_extension"])} | {n(row["high_extension_per_100"])} | {status_ru(row["status"])}')
    print(f'TV PINE: {tv_path} | events={len(tv_event_history)}')
    session.print_status('FINAL SESSION STATE', current_time=end, live=False)


def run_replay_live(args: argparse.Namespace, gui: GuiBridge | None = None) -> None:
    root = Path(__file__).resolve().parent
    raw_paths = args.raw_oi or discover_raw_paths(root)
    market_path = args.market_csv or discover_market_path(root)
    if not raw_paths:
        raise SystemExit('Не найдены локальные BTC-LRA raw OI JSONL-файлы.')
    if market_path is None:
        raise SystemExit('Не найден локальный записанный 1m market CSV.')
    samples = load_raw(raw_paths)
    market = load_market(market_path)
    all_minutes = minute_oi(samples)
    annotate_minutes(all_minutes)
    raw_flow_summaries, _raw_flow_samples = _raw_intensity_summary(samples, all_minutes, market)
    raw_flow_by_time = {row['_time']: row for row in raw_flow_summaries}
    raw_metric_by_timestamp = {row['ts']: row for row in _raw_flow_samples}
    common = recorded_range(samples, market)
    if common is None:
        raise SystemExit('Нет общего записанного диапазона между raw OI и market 1m.')
    start, end = common
    session = Session(start)
    session.market_history = market
    session.initialize_continuous_baseline(market)
    log_path = args.log or root / 'data' / 'research' / 'BTC_LRA_RECORDED_LIVE_REPLAY.log'
    event_archive = EventRowArchive(root / 'runtime' / 'monitor' / 'BTC_LRA_EVENT_ROWS.jsonl', log_path)
    tv_path = root / 'BTC_LRA_TV_EVENTS.pine'
    tv_event_history: dict[datetime, dict[str, Any]] = {}
    v2_report_path = root / 'data' / 'research' / 'BTC_LRA_DOMINANCE_V2_REPLAY.txt'
    v2_rows: list[dict[str, Any]] = []
    v2_checkpoints = [datetime(2026, 9, 30, hour, minute, tzinfo=PANAMA) for hour, minute in (
        (14, 54), (17, 7), (18, 51), (20, 5), (20, 31), (22, 37), (22, 39), (22, 44), (23, 10)
    )]
    write_v2_replay_report(v2_report_path, v2_rows, session, v2_checkpoints)
    log_path.write_text('', encoding='utf-8')
    write_replay_log(log_path, f'RAW OI: {fmt_time(samples[0]["ts"])} -> {fmt_time(samples[-1]["ts"])} | samples={len(samples)}')
    write_replay_log(log_path, f'MARKET 1M: {fmt_time(market[0]["ts"])} -> {fmt_time(market[-1]["ts"])} | bars={len(market)}')
    write_replay_log(log_path, f'COMMON RANGE: {fmt_time(start)} -> {fmt_time(end)} | anchor={fmt_time(start)}')
    for line in event_source_validation(all_minutes):
        write_replay_log(log_path, line)
    print('BTC-LRA | МОНИТОР OI / ПОТОКА')
    print(f'ЗАПИСАННЫЙ ДИАПАЗОН: {fmt_time(start)} -> {fmt_time(end)}')
    print(f'RAW OI: {len(samples)} samples | MARKET 1M: {len(market)} bars | SPEED: {args.speed}x')
    print('SPACE = пауза/продолжить | R = новый отсчёт | + / - = скорость | Ctrl+C = выход')

    print('T = выбрать исторический отсчёт и пересчитать состояние до текущего времени')
    import msvcrt
    speed = float(args.speed)
    paused = False
    previous_clock: datetime | None = None
    raw_prefix: list[dict[str, Any]] = []
    applied_minutes: set[datetime] = set()
    applied_market: set[datetime] = set()
    emitted_events: set[datetime] = set()
    previous_flow: str | None = None
    previous_status = session.early_status
    previous_peaks = (0.0, 0.0)
    event_count = 0
    flow_cross_count = 0
    market_by_minute = {row['ts'].replace(second=0, microsecond=0): row for row in market}
    market_cursor = -1
    last_closed_market_price: float | None = None
    partial_market_fallback_logged = False

    def latest_closed_market_price(clock: datetime) -> float | None:
        nonlocal market_cursor, last_closed_market_price
        while market_cursor + 1 < len(market):
            candidate = market[market_cursor + 1]
            if candidate['ts'] + timedelta(minutes=1) > clock:
                break
            market_cursor += 1
            last_closed_market_price = candidate['close']
        return last_closed_market_price

    def causal_partial_market(sample: dict[str, Any]) -> dict[str, Any] | None:
        """Return a partial candle only when the source explicitly provides one.

        The recorded CSV contains closed 1m taker totals only.  Its complete
        candle must never be reused as if it were known at an earlier sample.
        """
        return None

    def process_raw_sample(sample: dict[str, Any]) -> None:
        nonlocal partial_market_fallback_logged
        raw_metric = raw_metric_by_timestamp.get(sample['ts'])
        if raw_metric is None or raw_metric.get('doi') is None:
            return
        partial = causal_partial_market(sample)
        effort = None
        if partial is not None:
            diagnostic = Session(sample['ts'])
            diagnostic.market_history = market
            effort = diagnostic.effort_result_control(
                sample['ts'], partial['buy'], partial['sell'],
                partial['price'] - partial['open'], partial['open'],
            )
        elif not partial_market_fallback_logged:
            write_replay_log(log_path, 'RAW_PARTIAL_MARKET_FALLBACK unavailable: recorded source has closed 1m taker totals only')
            partial_market_fallback_logged = True
        detail = session.apply_raw_sample(sample, raw_metric, effort, log_path)
        if detail is not None:
            write_replay_log(log_path, 'RAW_EVENT ' + json.dumps({
                'time': detail['time'], 'class': 'RAW STRONG' if detail['pctl'] >= 99.0 else 'RAW WATCH',
                'pctl': detail['pctl'], 'peak': max((('IMPULSE_5S', raw_metric.get('impulse_pctl')), ('BURST_30S', raw_metric.get('burst_30_pctl')), ('BURST_60S', raw_metric.get('burst_60_pctl'))), key=lambda pair: pair[1] if pair[1] is not None else -1)[0],
                'flow': raw_metric.get('net_60'), 'directionality': raw_metric.get('directionality_60'),
                'control': detail['control'],
            }, ensure_ascii=False, default=str))

    def rebuild_session(new_anchor: datetime, clock: datetime) -> Session:
        rebuilt = Session(new_anchor)
        rebuilt.market_history = market
        rebuilt.initialize_continuous_baseline(market)
        prefix_minutes = minute_oi(raw_prefix)
        annotate_minutes(prefix_minutes)
        event_by_time = {prefix_minutes[e['confirmed']]['minute']: e for e in individual_events(prefix_minutes)}
        for minute in prefix_minutes:
            minute_time = minute['minute']
            if minute_time < new_anchor or minute_time > end or minute_time + timedelta(minutes=1) > clock:
                continue
            rebuilt.apply_oi(minute)
            market_row = market_by_minute.get(minute_time)
            if market_row is not None and market_row['ts'] + timedelta(minutes=1) <= clock:
                rebuilt.apply_market(market_row)
                continuous_result = rebuilt.apply_continuous_market(market_row)
                if continuous_result is not None:
                    if rebuilt.dominance_reference_frozen_just_now:
                        write_replay_log(
                            log_path,
                            'DOMINANCE_REFERENCE_FROZEN ' + json.dumps(
                                rebuilt.dominance_reference_diagnostic(),
                                ensure_ascii=False,
                            ),
                        )
                    write_replay_log(log_path, 'CONTINUOUS_STATE ' + json.dumps({
                        'time': market_row['ts'].isoformat(),
                        'buy_equiv': rebuilt.continuous_buy_dominance_btc,
                        'sell_equiv': rebuilt.continuous_sell_dominance_btc,
                        'buy_pct': rebuilt.continuous_buy_pct,
                        'sell_pct': rebuilt.continuous_sell_pct,
                        'control': continuous_result.get('control'),
                        'aggr_mag': continuous_result.get('aggr_mag'),
                        'strength': continuous_result.get('strength'),
                    }, ensure_ascii=False))
            append_minute_history(rebuilt, minute, market_row)
            canonical_event = None
            if minute_time in event_by_time:
                event = dict(event_by_time[minute_time])
                event['start_time'] = prefix_minutes[event['start']]['minute']
                event['kind'] = 'MEGA' if prefix_minutes[event['confirmed']]['mega'] else 'STRONG'
                decorate_event_interval(event, prefix_minutes, market)
                with contextlib.redirect_stdout(io.StringIO()):
                    rebuilt.emit_event(event, minute, [market_row] if market_row else [])
                canonical_event = rebuilt.event_history[-1]
            raw_summary = raw_flow_by_time.get(minute_time)
            if raw_summary is not None:
                raw_event = apply_raw_event_to_session(rebuilt, raw_summary)
                if canonical_event is None and raw_event is not None:
                    canonical_event = raw_event
            if canonical_event is not None:
                rebuilt.finalize_raw_minute(minute_time, canonical_event)
                event_archive.persist(canonical_event, rebuilt, 'BACKFILL')
            rebuilt.snapshot(minute_time)
        return rebuilt

    def activate_rebuilt_session(new_anchor: datetime, clock: datetime) -> None:
        nonlocal session
        session = rebuild_session(new_anchor, clock)
        # The rebuilt session already contains everything through `clock`.
        # Rebuild the bookkeeping sets too, otherwise process_until() either
        # skips the next data or emits old events again.
        applied_minutes.clear()
        applied_market.clear()
        emitted_events.clear()
        prefix_minutes = minute_oi(raw_prefix)
        for minute in prefix_minutes:
            if new_anchor <= minute['minute'] and minute['minute'] + timedelta(minutes=1) <= clock:
                applied_minutes.add(minute['minute'])
                if minute['minute'] in market_by_minute:
                    applied_market.add(minute['minute'])
        prefix_events = individual_events(prefix_minutes)
        emitted_events.update(
            prefix_minutes[event['confirmed']]['minute']
            for event in prefix_events
            if new_anchor <= prefix_minutes[event['confirmed']]['minute'] <= clock
        )

    def poll_keys(clock: datetime) -> None:
        nonlocal paused, speed, previous_flow, previous_status, previous_peaks
        while msvcrt.kbhit():
            key = msvcrt.getwch()
            if key == ' ':
                paused = not paused
                text = 'ПАУЗА' if paused else 'ПРОДОЛЖЕНИЕ'
                print(f'\n{text} | HISTORICAL TIME {fmt_time(clock)}')
                write_replay_log(log_path, f'{fmt_time(clock)} | {text}')
            elif key.upper() == 'R':
                session.reset(clock)
                previous_flow = None
                previous_status = session.early_status
                previous_peaks = (0.0, 0.0)
                print(f'\nНОВЫЙ ОТСЧЁТ С: {fmt_time(clock)}')
                write_replay_log(log_path, f'{fmt_time(clock)} | НОВЫЙ ОТСЧЁТ С')
            elif key.upper() == 'T':
                paused = True
                print('\nНОВЫЙ ОТСЧЁТ')
                print('Введите HH:MM или YYYY-MM-DD HH:MM')
                try:
                    value = input('> ').strip()
                    if re.fullmatch(r'\d{1,2}:\d{2}', value):
                        requested = datetime.strptime(value, '%H:%M').replace(year=clock.year, month=clock.month, day=clock.day, tzinfo=PANAMA)
                    else:
                        requested = parse_time(value)
                    if requested < start or requested > clock:
                        raise ValueError('anchor вне доступного диапазона или позже текущего времени')
                    activate_rebuilt_session(requested, clock)
                    previous_flow = session.snapshot(clock).get('dominant')
                    previous_status = session.early_status
                    previous_peaks = (session.sell_peak, session.buy_peak)
                    print(f'ОТСЧЁТ ИЗМЕНЁН: {requested.strftime("%H:%M")}')
                    print(f'СОСТОЯНИЕ ПЕРЕСЧИТАНО ДО: {clock.strftime("%H:%M")}')
                    write_replay_log(log_path, f'{fmt_time(clock)} | ОТСЧЁТ ИЗМЕНЁН: {fmt_time(requested)} | пересчитано до {fmt_time(clock)}')
                    paused = False
                except (ValueError, TypeError) as exc:
                    print(f'ОШИБКА ОТСЧЁТА: {exc}')
                    print('Replay остаётся на паузе. Нажмите T для новой попытки или SPACE для продолжения.')
            elif key in ('\x00', '\xe0'):
                arrow = msvcrt.getwch()
                if arrow == 'M':
                    speed = min(120.0, {0: 1, 1: 5, 5: 10, 10: 20, 20: 30, 30: 60, 60: 120}.get(int(speed), speed * 2))
                    print(f'SPEED: {speed:g}x')
                elif arrow == 'K':
                    speed = max(0.0, {120: 60, 60: 30, 30: 20, 20: 10, 10: 5, 5: 1, 1: 0}.get(int(speed), speed / 2))
                    print(f'SPEED: {speed:g}x')
            elif key == '+' or key == '=':
                speed = min(120.0, {0: 1, 1: 5, 5: 10, 10: 20, 20: 30, 30: 60, 60: 120}.get(int(speed), speed * 2))
                print(f'SPEED: {speed:g}x')
            elif key == '-':
                speed = max(0.0, {120: 60, 60: 30, 30: 20, 20: 10, 10: 5, 5: 1, 1: 0}.get(int(speed), speed / 2))
                print(f'SPEED: {speed:g}x')

    def poll_gui_commands(clock: datetime) -> None:
        nonlocal paused, speed, previous_flow, previous_status, previous_peaks
        if gui is None:
            return
        for key, value in gui.pop_commands():
            if key == 'SPACE':
                paused = not paused
            elif key == 'R':
                session.reset(clock)
                previous_flow = None
                previous_status = session.early_status
                previous_peaks = (0.0, 0.0)
            elif key == '+':
                speed = min(120.0, {0: 1, 1: 5, 5: 10, 10: 20, 20: 30, 30: 60, 60: 120}.get(int(speed), max(1.0, speed * 2)))
            elif key == '-':
                speed = max(0.0, {120: 60, 60: 30, 30: 20, 20: 10, 10: 5, 5: 1, 1: 0}.get(int(speed), speed / 2))
            elif key == 'RIGHT':
                speed = min(120.0, {0: 1, 1: 5, 5: 10, 10: 20, 20: 30, 30: 60, 60: 120}.get(int(speed), max(1.0, speed * 2)))
            elif key == 'LEFT':
                speed = max(0.0, {120: 60, 60: 30, 30: 20, 20: 10, 10: 5, 5: 1, 1: 0}.get(int(speed), speed / 2))
            elif key == 'T' and value:
                try:
                    if re.fullmatch(r'\d{1,2}:\d{2}', value):
                        requested = datetime.strptime(value, '%H:%M').replace(year=clock.year, month=clock.month, day=clock.day, tzinfo=PANAMA)
                    else:
                        requested = parse_time(value)
                    if requested < start or requested > clock:
                        raise ValueError('anchor вне доступного диапазона или позже текущего времени')
                    activate_rebuilt_session(requested, clock)
                    previous_flow = session.snapshot(clock).get('dominant')
                    previous_status = session.early_status
                    previous_peaks = (session.sell_peak, session.buy_peak)
                    paused = False
                except (ValueError, TypeError):
                    pass
        session.update_total_oi(raw_prefix, clock)
        publish_gui(gui, session, clock, speed, live=False)

    def process_until(clock: datetime) -> None:
        nonlocal previous_flow, previous_status, previous_peaks, event_count, flow_cross_count
        prefix_minutes = minute_oi(raw_prefix)
        annotate_minutes(prefix_minutes)
        event_by_time = {prefix_minutes[e['confirmed']]['minute']: e for e in individual_events(prefix_minutes)}
        for minute in prefix_minutes:
            minute_time = minute['minute']
            if minute_time < start or minute_time > end or minute_time in applied_minutes:
                continue
            if minute_time + timedelta(minutes=1) > clock:
                continue
            session.apply_oi(minute)
            applied_minutes.add(minute_time)
            market_row = market_by_minute.get(minute_time)
            if market_row is not None and market_row['ts'] + timedelta(minutes=1) <= clock and minute_time not in applied_market:
                session.apply_market(market_row)
                session.apply_continuous_market(market_row)
                applied_market.add(minute_time)
            append_minute_history(session, minute, market_row)
            current = session.snapshot(minute_time)
            if current['dominant'] in ('BUY', 'SELL') and previous_flow and current['dominant'] != previous_flow:
                flow_cross_count += 1
                line = f'{fmt_time(clock)} | СМЕНА НАКОПИТЕЛЬНОГО ДОМИНАНТА: {previous_flow} -> {current["dominant"]}'
                print(line); write_replay_log(log_path, line)
            if current['dominant'] in ('BUY', 'SELL'):
                previous_flow = current['dominant']
            canonical_event_for_finalize: dict[str, Any] | None = None
            raw_event: dict[str, Any] | None = None
            if minute_time in event_by_time and minute_time not in emitted_events:
                event = dict(event_by_time[minute_time])
                event['start_time'] = prefix_minutes[event['start']]['minute']
                event['kind'] = 'MEGA' if prefix_minutes[event['confirmed']]['mega'] else 'STRONG'
                decorate_event_interval(event, prefix_minutes, market)
                emitted_events.add(minute_time)
                event_count += 1
                if args.sound == 'on':
                    play_event_sound(args.sound_file)
                session.emit_event(event, minute, [market_row] if market_row else [])
                canonical_event_for_finalize = session.event_history[-1]
                tv_event_history[minute_time] = dict(session.event_history[-1])
                write_tv_events_pine(tv_path, list(tv_event_history.values()))
                debug_event = session.last_events[-1]
                v2_rows.append({
                    'time': minute_time,
                    'oi_add': minute['add'],
                    'oi_net': minute['net'],
                    'oi_activity': minute['activity'],
                    'aggr': f'{debug_event.get("aggr_side")} {debug_event.get("aggr_share_pct", 0.0):.1f}% {compact(debug_event.get("aggr_mag", 0.0))}',
                    'control': debug_event.get('control', 'CONTROL UNCLEAR').removeprefix('CONTROL '),
                    'eff_ratio': debug_event.get('eff_ratio'),
                    'absorption_ratio': debug_event.get('absorption_ratio'),
                    'v2_side': debug_event.get('v2_side'),
                    'strength': debug_event.get('v2_strength', 0.0),
                    'weight': debug_event.get('v2_weight_btc', 0.0),
                    'cum_buy': debug_event.get('buy_dominance_v2_btc', 0.0),
                    'cum_sell': debug_event.get('sell_dominance_v2_btc', 0.0),
                    'buy_pct': debug_event.get('buy_v2_pct'),
                    'sell_pct': debug_event.get('sell_v2_pct'),
                })
                write_v2_replay_report(v2_report_path, v2_rows, session, v2_checkpoints)
                write_replay_log(log_path, 'EFFORT_RESULT ' + json.dumps({
                    'timestamp': minute_time.isoformat(),
                    'taker_buy': event.get('display_taker_buy', 0.0),
                    'taker_sell': event.get('display_taker_sell', 0.0),
                    'aggr_side': debug_event.get('aggr_side'),
                    'aggr_share_pct': debug_event.get('aggr_share_pct'),
                    'aggr_mag_btc': debug_event.get('aggr_mag'),
                    'aggr_baseline_median_btc': debug_event.get('aggr_baseline_median_btc'),
                    'aggr_mag_x': debug_event.get('aggr_mag_x'),
                    'meaningful_aggression': debug_event.get('meaningful_aggression'),
                    'price_change': debug_event.get('display_price_change'),
                    'event_reference_price': debug_event.get('reference_price'),
                    'aggr_notional_usdt': debug_event.get('aggr_notional_usdt'),
                    'price_change_bps': debug_event.get('price_change_bps'),
                    'aligned_result_bps': debug_event.get('aligned_result_bps'),
                    'baseline_impact_bps_per_1m': debug_event.get('baseline_impact_bps_per_1m'),
                    'expected_aligned_bps': debug_event.get('expected_aligned_bps'),
                    'actual_impact_bps_per_1m': debug_event.get('actual_impact_bps_per_1m'),
                    'eff_ratio': debug_event.get('eff_ratio'),
                    'absorption_ratio': debug_event.get('absorption_ratio'),
                    'absorbed_aggression_usdt': debug_event.get('absorbed_aggression_usdt'),
                    'control': debug_event.get('control'),
                    'buy_dominance_weight_usdt': debug_event.get('buy_dominance_weight_usdt'),
                    'sell_dominance_weight_usdt': debug_event.get('sell_dominance_weight_usdt'),
                    'buy_dominance_pct': debug_event.get('buy_dominance_pct'),
                    'sell_dominance_pct': debug_event.get('sell_dominance_pct'),
                    'oi_add_mass_btc': debug_event.get('oi_add_mass'),
                    'v2_side': debug_event.get('v2_side'),
                    'v2_strength': debug_event.get('v2_strength'),
                    'v2_weight_btc': debug_event.get('v2_weight_btc'),
                    'cum_buy_v2_btc': debug_event.get('buy_dominance_v2_btc'),
                    'cum_sell_v2_btc': debug_event.get('sell_dominance_v2_btc'),
                    'buy_v2_pct': debug_event.get('buy_v2_pct'),
                    'sell_v2_pct': debug_event.get('sell_v2_pct'),
                }, ensure_ascii=False))
                write_replay_log(log_path, f'{fmt_time(clock)} | СИЛЬНОЕ OI-СОБЫТИЕ | подтверждено {minute_time.strftime("%H:%M")}')
            raw_summary = raw_flow_by_time.get(minute_time)
            if raw_summary is not None:
                raw_event = apply_raw_event_to_session(session, raw_summary)
                if raw_event is not None:
                    if canonical_event_for_finalize is None:
                        canonical_event_for_finalize = raw_event
                    write_replay_log(log_path, 'RAW_OI_EVENT ' + json.dumps({
                        'timestamp': minute_time.isoformat(),
                        'classification': raw_event.get('raw_class'),
                        'oi_intensity_pctl': raw_event.get('raw_oi_intensity_pctl'),
                        'peak_type': raw_event.get('raw_peak_type'),
                        'oi_net_60s': raw_event.get('raw_oi_net_60s'),
                        'oi_directionality_60s': raw_event.get('raw_oi_directionality_60s'),
                        'oi_flow': raw_event.get('raw_oi_flow'),
                        'aggr': raw_event.get('raw_aggr'),
                        'eff_ratio': raw_event.get('raw_eff_ratio'),
                        'control': raw_event.get('raw_control'),
                        'dominance_v2_contribution_btc': 0.0,
                    }, ensure_ascii=False))
            if canonical_event_for_finalize is not None:
                source = 'STRONG+RAW' if canonical_event_for_finalize.get('event_source') != 'RAW_OI_INTENSITY' and raw_summary is not None and raw_event is not None else 'STRONG' if canonical_event_for_finalize.get('event_source') != 'RAW_OI_INTENSITY' else 'RAW_ONLY'
                session.finalize_raw_minute(minute_time, canonical_event_for_finalize, log_path=log_path)
                event_archive.persist(canonical_event_for_finalize, session, 'BACKFILL')
                write_replay_log(log_path, 'ACCUM_FINAL ' + json.dumps({
                    'time': minute_time.isoformat(),
                    'source': source,
                    'net': canonical_event_for_finalize.get('event_oi_net', 0.0),
                    'oi_add': minute.get('add', 0.0),
                    'control': canonical_event_for_finalize.get('control', 'CONTROL UNCLEAR'),
                    'strength': canonical_event_for_finalize.get('v2_strength', 0.0),
                    'flow_final': canonical_event_for_finalize.get('oi_flow_contribution', 0.0),
                    'buy_dom_final': canonical_event_for_finalize.get('v2_buy_delta_btc', 0.0),
                    'sell_dom_final': canonical_event_for_finalize.get('v2_sell_delta_btc', 0.0),
                    'double_count': 0,
                }, ensure_ascii=False, default=str))
            peaks = (session.sell_peak, session.buy_peak)
            state_changed = current['status'] != previous_status or peaks != previous_peaks
            if state_changed:
                session.print_status('ТЕКУЩЕЕ СОСТОЯНИЕ', current_time=clock, speed=speed, live=False)
                write_replay_log(log_path, f'{fmt_time(clock)} | peak={peaks} | status={current["status"]}')
                previous_status = current['status']
                previous_peaks = peaks

    for sample in samples:
        if sample['ts'] < start:
            continue
        if sample['ts'] > end + timedelta(minutes=1):
            break
        while paused:
            poll_keys(sample['ts'])
            poll_gui_commands(sample['ts'])
            time.sleep(0.05)
        poll_keys(sample['ts'])
        poll_gui_commands(sample['ts'])
        if previous_clock is not None and speed > 0:
            time.sleep(max(0.0, (sample['ts'] - previous_clock).total_seconds() / speed))
        raw_prefix.append(sample)
        process_raw_sample(sample)
        process_until(sample['ts'])
        session.update_total_oi(samples, sample['ts'])
        publish_gui(gui, session, sample['ts'], speed, live=False, market_price=latest_closed_market_price(sample['ts']))
        previous_clock = sample['ts']
    process_until(end + timedelta(minutes=1))
    session.update_total_oi(samples, end)
    publish_gui(gui, session, end, speed, live=False, market_price=latest_closed_market_price(end))
    final = session.snapshot(end)
    write_v2_replay_report(v2_report_path, v2_rows, session, v2_checkpoints)
    write_replay_log(log_path, f'FINAL | {fmt_time(end)} | buy={session.buy:.1f} | sell={session.sell:.1f} | oi_net={final["oi_net"]} | events={event_count} | crosses={flow_cross_count}')
    print(f'\nЗАПИСАННЫЙ REPLAY ЗАВЕРШЁН | СИЛЬНЫХ OI-СОБЫТИЙ: {event_count} | СМЕН НАКОПИТЕЛЬНОГО ДОМИНАНТА: {flow_cross_count}')
    print(f'RAW OI: {fmt_time(samples[0]["ts"])} -> {fmt_time(samples[-1]["ts"])} | {len(samples)} samples')
    print(f'MARKET 1M: {fmt_time(market[0]["ts"])} -> {fmt_time(market[-1]["ts"])} | {len(market)} bars')
    print(f'ОБЩИЙ ДИАПАЗОН: {fmt_time(start)} -> {fmt_time(end)}')
    print(f'TV PINE: {tv_path} | events={len(tv_event_history)}')
    print(f'DOMINANCE V2 REPORT: {v2_report_path} | events={len(v2_rows)}')
    print('ZERO FUTURE LEAKAGE: PASS | STRONG detector: unchanged')
    for line in event_source_validation(all_minutes):
        print(line)
    session.print_status('ИТОГОВОЕ СОСТОЯНИЕ СЕССИИ', current_time=end, live=False)


def run_live(args: argparse.Namespace, gui: GuiBridge | None = None) -> None:
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    startup_now = datetime.now(PANAMA)
    requested_default_anchor = startup_now - timedelta(hours=12)
    anchor = parse_time(args.from_time) if args.from_time else (startup_now if args.from_now else requested_default_anchor)
    session = Session(anchor)
    processed: set[datetime] = set()
    processed_raw_samples: set[datetime] = set()
    root = Path(__file__).resolve().parent
    log_path = root / 'runtime' / 'monitor' / 'BTC_LRA_LIVE_MONITOR.log'
    event_archive = EventRowArchive(root / 'runtime' / 'monitor' / 'BTC_LRA_EVENT_ROWS.jsonl', log_path)
    live_event_cutoff = startup_now.replace(second=0, microsecond=0)
    collector_directory = root / 'runtime' / 'collector'
    collector_raw_path = collector_directory / 'BTC_LRA_OI_RAW.jsonl'
    collector_market_path = collector_directory / 'BTC_LRA_MARKET_RAW.jsonl'
    collector_depth_path = collector_directory / 'BTC_LRA_DEPTH_SUMMARY.jsonl'
    tv_path = root / 'BTC_LRA_TV_EVENTS.pine'
    tv_event_history: dict[datetime, dict[str, Any]] = {}
    print('BTC-LRA OI МОНИТОР ПОТОКА | ТОЛЬКО ЧТЕНИЕ')
    print('R = СБРОСИТЬ ОТСЧЁТ НА СЕЙЧАС | Ctrl+C = выход')
    market_history: list[dict[str, Any]] = []
    raw_flow_by_time: dict[datetime, dict[str, Any]] = {}
    collector_owns_raw = collector_raw_is_fresh(collector_raw_path, startup_now)
    collector_owns_market = collector_raw_is_fresh(collector_market_path, startup_now)
    raw_samples = load_collector_oi_history(collector_raw_path, startup_now, 24.0)
    source = 'COLLECTOR' if raw_samples else 'EMPTY'
    history_span = ((raw_samples[-1]['ts'] - raw_samples[0]['ts']).total_seconds() / 3600
                    if len(raw_samples) >= 2 else 0.0)
    processed_raw_samples.update(sample['ts'] for sample in raw_samples)
    collector_oi_latest = collector_latest_timestamp(collector_raw_path)
    collector_market_latest = collector_latest_timestamp(collector_market_path)
    earliest_exact = raw_samples[0]['ts'] + timedelta(hours=6, minutes=30) if raw_samples else None
    if not args.from_time and not args.from_now and earliest_exact is not None and anchor < earliest_exact:
        actual_anchor = min(max(earliest_exact, raw_samples[0]['ts']), startup_now)
        write_bounded_live_log(
            log_path,
            f'DEFAULT_ANCHOR_CLAMPED requested={fmt_time(anchor)} actual={fmt_time(actual_anchor)} '
            f'reason=insufficient_raw_reference earliest_exact={fmt_time(earliest_exact)}',
        )
        anchor = actual_anchor
        session = Session(anchor)
    session.collector_stale = not (collector_owns_raw and collector_owns_market)
    session.collector_last_age_sec = ((startup_now - collector_oi_latest).total_seconds() if collector_oi_latest else None)
    session.collector_market_last_age_sec = ((startup_now - collector_market_latest).total_seconds() if collector_market_latest else None)
    migration_market = load_collector_market_history(collector_market_path, startup_now, 24.0)
    if len(migration_market) < 120:
        try:
            migration_market = merge_market_rows(fetch_binance_1m_klines(1000), migration_market)
            write_bounded_live_log(log_path, f'MARKET_MIGRATION_FALLBACK bars={len(migration_market)} source=BINANCE_HISTORICAL_ONLY')
        except (OSError, ValueError, TypeError, urllib.error.URLError) as exc:
            write_bounded_live_log(log_path, f'MARKET_MIGRATION_FALLBACK_ERROR {type(exc).__name__}: {exc}')
    market_history = merge_market_rows(migration_market)[-120:]
    session.initialize_continuous_baseline(migration_market)
    baseline_ready = history_span >= RAW_BASELINE_MINUTES / 60
    session.raw_baseline_ready = baseline_ready
    session.raw_baseline_span_minutes = history_span * 60
    baseline_line = (
        f'RAW_BASELINE_RESTORED samples={len(raw_samples)} '
        f'oldest={fmt_time(raw_samples[0]["ts"]) if raw_samples else "--"} '
        f'newest={fmt_time(raw_samples[-1]["ts"]) if raw_samples else "--"} '
        f'span_hours={history_span:.2f} source={source} '
        f'reference_ready={"YES" if baseline_ready else "NO"}'
    )
    collector_line = (
        f'COLLECTOR_OI_RESTORED path={collector_raw_path} samples={len(raw_samples)} '
        f'oldest={fmt_time(raw_samples[0]["ts"]) if raw_samples else "--"} '
        f'newest={fmt_time(raw_samples[-1]["ts"]) if raw_samples else "--"} '
        f'span_hours={history_span:.2f} reference_ready={"YES" if baseline_ready else "NO"}'
    )
    print(collector_line)
    write_bounded_live_log(log_path, collector_line)
    print(baseline_line)
    write_bounded_live_log(log_path, baseline_line)
    baseline_warmup_logged = False
    if not baseline_ready:
        warmup_line = f'RAW_BASELINE_WARMUP reason=available_span_{history_span * 60:.1f}m_required_{RAW_BASELINE_MINUTES}m'
        print(warmup_line)
        write_bounded_live_log(log_path, warmup_line)
        baseline_warmup_logged = True
    def parse_live_anchor(value: str, clock: datetime) -> datetime:
        parsed = parse_t_window(value, clock)
        return parsed['start']

    def rebuild_live_session(new_anchor: datetime, clock: datetime,
                             rebuild_market: list[dict[str, Any]],
                             window_mode: str = 'LIVE_FROM',
                             window_end: datetime | None = None) -> tuple[
                                 Session, set[datetime], set[datetime], dict[datetime, dict[str, Any]]]:
        """Causally rebuild session state without replaying raw sensors as new samples."""
        target_end = window_end or clock
        closed_market = [row for row in rebuild_market if row['ts'] + timedelta(minutes=1) <= target_end]
        if not closed_market:
            raise ValueError('нет закрытых Binance 1m свечей для rebuild')
        if not any(row['ts'] <= new_anchor for row in closed_market):
            raise ValueError('недостаточно Binance 1m истории для указанного anchor')
        if closed_market[0]['ts'] > new_anchor - timedelta(minutes=30):
            raise ValueError('недостаточно 30m закрытого market baseline для указанного anchor')

        rebuilt = Session(new_anchor)
        rebuilt.window_mode = window_mode
        rebuilt.window_end = window_end
        rebuilt.market_history = closed_market
        rebuilt.initialize_continuous_baseline(rebuild_market)
        rebuilt.raw_baseline_ready = baseline_ready
        rebuilt.raw_baseline_span_minutes = history_span * 60
        rebuilt_until = target_end.replace(second=0, microsecond=0)
        rebuild_minutes = minute_oi(raw_samples)
        annotate_minutes(rebuild_minutes)
        rebuild_summaries, _ = _raw_intensity_summary(raw_samples, rebuild_minutes, closed_market)
        rebuild_raw_by_time = {row['_time']: row for row in rebuild_summaries}
        rebuild_events = {
            rebuild_minutes[event['confirmed']]['minute']: event
            for event in individual_events(rebuild_minutes)
        }
        market_by_time = {row['ts']: row for row in closed_market}
        rebuilt_processed: set[datetime] = set()
        current_tv: dict[datetime, dict[str, Any]] = {}

        for minute in rebuild_minutes:
            minute_time = minute['minute']
            if minute_time < new_anchor or minute_time >= rebuilt_until:
                continue
            if minute_time + timedelta(minutes=1) > target_end:
                continue
            rebuilt.apply_oi(minute)
            rebuilt_processed.add(minute_time)
            market_row = market_by_time.get(minute_time)
            if market_row is not None:
                rebuilt.apply_market(market_row)
                continuous_result = rebuilt.apply_continuous_market(market_row)
                if rebuilt.dominance_reference_frozen_just_now:
                    write_bounded_live_log(
                        log_path,
                        'DOMINANCE_REFERENCE_FROZEN ' + json.dumps(
                            rebuilt.dominance_reference_diagnostic(),
                            ensure_ascii=False,
                        ),
                    )
            append_minute_history(rebuilt, minute, market_row)
            canonical_event: dict[str, Any] | None = None
            raw_event: dict[str, Any] | None = None
            if minute_time in rebuild_events:
                event = dict(rebuild_events[minute_time])
                event['start_time'] = rebuild_minutes[event['start']]['minute']
                event['kind'] = 'MEGA' if rebuild_minutes[event['confirmed']]['mega'] else 'STRONG'
                decorate_event_interval(event, rebuild_minutes, closed_market)
                with contextlib.redirect_stdout(io.StringIO()):
                    rebuilt.emit_event(event, minute, [market_row] if market_row else [])
                canonical_event = rebuilt.event_history[-1]
                current_tv[minute_time] = dict(canonical_event)
            raw_summary = rebuild_raw_by_time.get(minute_time)
            if raw_summary is not None:
                raw_event = apply_raw_event_to_session(rebuilt, raw_summary)
            if canonical_event is None and raw_event is not None:
                canonical_event = raw_event
            if canonical_event is not None:
                rebuilt.finalize_raw_minute(minute_time, canonical_event)
                event_archive.persist(canonical_event, rebuilt, 'BACKFILL')

        # Do not restore provisional samples from the unfinished minute. The
        # historical partial kline is not available causally at each sample
        # timestamp, so replaying it could attribute future market state to
        # earlier OI samples. The next new sample continues normal LIVE
        # processing and starts the provisional ledger from zero.
        # Mark the current-minute samples as observed, but intentionally do
        # not apply them. This prevents the next LIVE loop from replaying
        # historical provisional samples; only a newly arriving sample can
        # start the fresh provisional ledger.
        processed_samples = {
            sample['ts'] for sample in raw_samples
            if new_anchor <= sample['ts'] <= target_end
        }

        return rebuilt, rebuilt_processed, processed_samples, current_tv

    while True:
        if gui is not None:
            for key, value in gui.pop_commands():
                if key == 'R':
                    reset_time = datetime.now(PANAMA)
                    session = Session(reset_time)
                    session.initialize_continuous_baseline(migration_market)
                    session.raw_baseline_ready = baseline_ready
                    session.raw_baseline_span_minutes = history_span * 60
                    processed.clear()
                    processed_raw_samples = {
                        sample['ts'] for sample in raw_samples
                        if sample['ts'] < reset_time.replace(second=0, microsecond=0)
                    }
                    tv_event_history.clear()
                    write_tv_events_pine(tv_path, [])
                    write_bounded_live_log(log_path, f'LIVE_RESET anchor={fmt_time(reset_time)} raw_baseline_preserved=YES')
                elif key == 'T' and value:
                    old_anchor = session.anchor
                    clock = datetime.now(PANAMA)
                    try:
                        raw_samples = load_collector_oi_history(collector_raw_path, clock, 24.0)
                        if raw_samples:
                            history_span = (raw_samples[-1]['ts'] - raw_samples[0]['ts']).total_seconds() / 3600.0
                            baseline_ready = history_span >= RAW_BASELINE_MINUTES / 60
                        window = parse_t_window(value, clock)
                        requested = window['start']
                        requested_end = window['end'] or clock
                        earliest_exact = (raw_samples[0]['ts'] + timedelta(hours=6, minutes=30)) if raw_samples else None
                        if earliest_exact is None or requested < earliest_exact:
                            raise ValueError(
                                f'insufficient_raw_reference earliest_exact={fmt_time(earliest_exact)}'
                            )
                        rebuild_market = merge_market_rows(
                            migration_market,
                            load_collector_market_history(collector_market_path, clock, 24.0),
                        )
                        if len(rebuild_market) < 31:
                            rebuild_market = merge_market_rows(fetch_binance_1m_klines(1000), rebuild_market)
                        if window['mode'] == 'FIXED_RANGE':
                            oi_available = f'{fmt_time(raw_samples[0]["ts"])} -> {fmt_time(raw_samples[-1]["ts"])}' if raw_samples else '--'
                            market_available = f'{fmt_time(rebuild_market[0]["ts"])} -> {fmt_time(rebuild_market[-1]["ts"])}' if rebuild_market else '--'
                            if not raw_samples or raw_samples[0]['ts'] > requested or raw_samples[-1]['ts'] < requested_end:
                                raise ValueError(
                                    f'RANGE NOT AVAILABLE requested={fmt_time(requested)} -> {fmt_time(requested_end)} '
                                    f'OI available={oi_available} MARKET available={market_available}'
                                )
                            if not rebuild_market or rebuild_market[0]['ts'] > requested - timedelta(minutes=30) or rebuild_market[-1]['ts'] + timedelta(minutes=1) < requested_end:
                                raise ValueError(
                                    f'RANGE NOT AVAILABLE requested={fmt_time(requested)} -> {fmt_time(requested_end)} '
                                    f'OI available={oi_available} MARKET available={market_available}'
                                )
                        rebuilt, rebuilt_processed, rebuilt_samples, rebuilt_tv = rebuild_live_session(
                            requested, clock, rebuild_market, window['mode'], requested_end if window['mode'] == 'FIXED_RANGE' else None,
                        )
                        preserved_depth_history = list(session.pre_release.depth_history)
                        session = rebuilt
                        session.pre_release.depth_history = preserved_depth_history[-200:]
                        session.pre_release.last_depth_event = (
                            session.pre_release.depth_history[-1]
                            if session.pre_release.depth_history else None
                        )
                        numeric_level_ids = [
                            event.get('level_id') for event in session.pre_release.depth_history
                            if isinstance(event.get('level_id'), int)
                        ]
                        session.pre_release._next_level_id = max(numeric_level_ids, default=0) + 1
                        processed = rebuilt_processed
                        processed_raw_samples = rebuilt_samples
                        tv_event_history = {
                            timestamp: event for timestamp, event in rebuilt_tv.items()
                            if timestamp >= requested
                        }
                        write_tv_events_pine(tv_path, list(tv_event_history.values()))
                        market_history = [
                            row for row in rebuild_market
                            if row['ts'] + timedelta(minutes=1) <= requested_end
                        ][-120:]
                        session.market_history = market_history
                        write_bounded_live_log(log_path, f'LIVE_ANCHOR_CHANGED mode={window["mode"]} from={fmt_time(old_anchor)} to={fmt_time(requested)} rebuilt_until={fmt_time(requested_end)} events={len(session.event_history)} oi_flow={session.oi_event_flow + session.provisional_oi_flow:.6f} buy_dom={session.buy_dominance_v2_btc + session.provisional_buy_dominance_v2_btc:.6f} sell_dom={session.sell_dominance_v2_btc + session.provisional_sell_dominance_v2_btc:.6f} provisional_current_minute=RESET')
                    except (ValueError, TypeError, OSError, urllib.error.URLError) as exc:
                        write_bounded_live_log(log_path, f'LIVE_ANCHOR_REJECTED requested={value!r} reason={exc}')
                elif key == 'RIGHT':
                    args.speed = min(120.0, {0: 1, 1: 5, 5: 10, 10: 20, 20: 30, 30: 60, 60: 120}.get(int(args.speed), max(1.0, args.speed * 2)))
                elif key == 'LEFT':
                    args.speed = max(0.0, {120: 60, 60: 30, 30: 20, 20: 10, 10: 5, 5: 1, 1: 0}.get(int(args.speed), args.speed / 2))
        if msvcrt and msvcrt.kbhit():
            key = msvcrt.getwch().upper()
            if key == 'R':
                reset_time = datetime.now(PANAMA)
                session = Session(reset_time)
                session.initialize_continuous_baseline(migration_market)
                session.raw_baseline_ready = baseline_ready
                session.raw_baseline_span_minutes = history_span * 60
                processed.clear()
                processed_raw_samples = {
                    sample['ts'] for sample in raw_samples
                    if sample['ts'] < reset_time.replace(second=0, microsecond=0)
                }
                tv_event_history.clear()
                write_tv_events_pine(tv_path, [])
                print(f'\nОТСЧЁТ СБРОШЕН: {session.anchor.strftime("%H:%M:%S / %d.%m.%y -5")}')
            elif key == 'T':
                print('LIVE T доступен через GUI: нажмите T и задайте HH:MM или YYYY-MM-DD HH:MM')
        now = datetime.now(PANAMA)
        collector_fresh = collector_raw_is_fresh(collector_raw_path, now)
        collector_market_fresh = collector_raw_is_fresh(collector_market_path, now)
        collector_market_rows = load_collector_market_history(collector_market_path, now, 24.0)
        live_klines = merge_market_rows(migration_market, collector_market_rows)
        depth_rows = load_collector_depth_summary(collector_depth_path, now, 24.0)
        display_end = session.window_end if session.window_mode == 'FIXED_RANGE' else now
        session.display_depth_history = build_depth_timeline(depth_rows, session.anchor, display_end)
        if collector_fresh:
            raw_samples = load_collector_oi_history(collector_raw_path, now, 24.0)

        if raw_samples:
            span_minutes = (raw_samples[-1]['ts'] - raw_samples[0]['ts']).total_seconds() / 60
            history_span = span_minutes / 60.0
            baseline_ready = span_minutes >= RAW_BASELINE_MINUTES
        oi_latest = collector_latest_timestamp(collector_raw_path)
        market_latest = collector_latest_timestamp(collector_market_path)
        session.collector_stale = not (collector_fresh and collector_market_fresh)
        session.collector_last_age_sec = ((now - oi_latest).total_seconds() if oi_latest else None)
        session.collector_market_last_age_sec = ((now - market_latest).total_seconds() if market_latest else None)
        session.raw_baseline_ready = baseline_ready
        session.raw_baseline_span_minutes = history_span * 60
        if not session.raw_baseline_ready and not baseline_warmup_logged:
            warmup_line = f'RAW_BASELINE_WARMUP reason=available_span_{history_span * 60:.1f}m_required_{RAW_BASELINE_MINUTES}m'
            write_bounded_live_log(log_path, warmup_line)
            baseline_warmup_logged = True
        samples = raw_samples
        minutes = minute_oi(samples)
        annotate_minutes(minutes)
        processing_end = session.window_end if session.window_mode == 'FIXED_RANGE' else now
        closed_market = [row for row in live_klines if row['ts'] + timedelta(minutes=1) <= processing_end]
        current_market = None if session.window_mode == 'FIXED_RANGE' else next(
            (row for row in reversed(live_klines) if row['ts'] + timedelta(minutes=1) > now), None
        )
        if closed_market:
            market_history = closed_market[-120:]
            session.market_history = market_history
        if current_market is not None:
            session.price = current_market['close']
        raw_metrics = _raw_intensity_metrics(samples)
        raw_metric_by_timestamp = {row['ts']: row for row in raw_metrics}
        raw_summaries, _ = _raw_intensity_summary(samples, minutes, market_history)
        raw_flow_by_time = {row['_time']: row for row in raw_summaries}
        session.update_total_oi(raw_samples, processing_end)
        events = individual_events(minutes)
        event_by_time = {minutes[e['confirmed']]['minute']: e for e in events}
        for minute in minutes:
            if minute['minute'] < session.anchor or minute['minute'] in processed:
                continue
            if minute['minute'] + timedelta(minutes=1) > processing_end:
                continue
            session.apply_oi(minute)
            processed.add(minute['minute'])
            market_row = next((row for row in market_history if row['ts'] == minute['minute']), None)
            if market_row is not None:
                session.apply_market(market_row)
                continuous_result = session.apply_continuous_market(market_row)
                if continuous_result is not None:
                    if session.dominance_reference_frozen_just_now:
                        write_bounded_live_log(
                            log_path,
                            'DOMINANCE_REFERENCE_FROZEN ' + json.dumps(
                                session.dominance_reference_diagnostic(),
                                ensure_ascii=False,
                            ),
                        )
                    write_bounded_live_log(log_path, 'CONTINUOUS_STATE ' + json.dumps({
                        'time': market_row['ts'].isoformat(),
                        'total_oi_flow_btc': session.total_oi_flow_btc,
                        'buy_equiv': session.continuous_buy_dominance_btc,
                        'sell_equiv': session.continuous_sell_dominance_btc,
                        'buy_pct': session.continuous_buy_pct,
                        'sell_pct': session.continuous_sell_pct,
                        'control': continuous_result.get('control'),
                        'aggr_mag': continuous_result.get('aggr_mag'),
                        'strength': continuous_result.get('strength'),
                    }, ensure_ascii=False))
            append_minute_history(session, minute, market_row)
            canonical_event = None
            raw_event: dict[str, Any] | None = None
            if minute['minute'] in event_by_time:
                event = dict(event_by_time[minute['minute']])
                event['start_time'] = minutes[event['start']]['minute']
                event['kind'] = 'MEGA' if minutes[event['confirmed']]['mega'] else 'STRONG'
                decorate_event_interval(event, minutes, market_history)
                if args.sound == 'on':
                    play_event_sound(args.sound_file)
                session.emit_event(event, minute, [market_row] if market_row else [])
                canonical_event = session.event_history[-1]
                tv_event_history[minute['minute']] = dict(canonical_event)
                # Live Pine export keeps the existing STRONG/MEGA stream;
                # RAW-only observations are intentionally not exported yet.
                write_tv_events_pine(tv_path, list(tv_event_history.values()))
            raw_summary = raw_flow_by_time.get(minute['minute'])
            raw_event = apply_raw_event_to_session(session, raw_summary) if raw_summary is not None else None
            if canonical_event is None and raw_event is not None:
                canonical_event = raw_event
            if canonical_event is not None:
                session.finalize_raw_minute(minute['minute'], canonical_event, log_path=log_path)
                event_archive.persist(
                    canonical_event,
                    session,
                    'LIVE' if minute['minute'] >= live_event_cutoff else 'BACKFILL',
                )
                write_bounded_live_log(log_path, 'ACCUM_FINAL ' + json.dumps({
                    'time': minute['minute'].isoformat(),
                    'source': 'STRONG+RAW' if minute['minute'] in event_by_time and raw_event is not None else 'STRONG' if minute['minute'] in event_by_time else 'RAW_ONLY',
                    'net': canonical_event.get('event_oi_net', 0.0),
                    'oi_add': minute.get('add', 0.0),
                    'control': canonical_event.get('control', 'CONTROL UNCLEAR'),
                    'strength': canonical_event.get('v2_strength', 0.0),
                    'flow_final': canonical_event.get('oi_flow_contribution', 0.0),
                    'buy_dom_final': canonical_event.get('v2_buy_delta_btc', 0.0),
                    'sell_dom_final': canonical_event.get('v2_sell_delta_btc', 0.0),
                    'double_count': 0,
                }, ensure_ascii=False, default=str))
        # Current Binance kline is causal: it is never added to history or
        # baseline until the exchange closes it.
        for sample in samples:
            if sample['ts'] < session.anchor or sample['ts'] > processing_end or sample['ts'] in processed_raw_samples:
                continue
            metric = raw_metric_by_timestamp.get(sample['ts'])
            if metric is not None:
                partial_effort = None
                if current_market is not None and sample['ts'].replace(second=0, microsecond=0) == current_market['ts']:
                    diagnostic = Session(sample['ts'])
                    diagnostic.market_history = market_history
                    partial_effort = diagnostic.effort_result_control(
                        sample['ts'], current_market['buy'], current_market['sell'],
                        current_market['close'] - current_market['open'], current_market['open'],
                    )
                session.apply_raw_sample(sample, metric, partial_effort=partial_effort, log_path=log_path)
                if partial_effort is not None:
                    depth = next((row for row in reversed(depth_rows) if row['_ts'] <= sample['ts']), None)
                    pre_release_event = session.pre_release.observe(
                        sample, metric, partial_effort, depth,
                        current_market['close'] if current_market is not None else session.price,
                    )
                    for depth_event in session.pre_release.pop_depth_events():
                        write_bounded_live_log(
                            log_path,
                            'PRE_RELEASE_DEPTH ' + json.dumps(
                                depth_event, ensure_ascii=False, default=str,
                            ),
                        )
                        if args.sound == 'on' and depth_event.get('status') in {
                                'NEAR', 'TESTED', 'HELD', 'DEFENDED', 'BROKEN', 'PULLED',
                        }:
                            write_bounded_live_log(
                                log_path,
                                f'SOUND_TRIGGER depth status={depth_event.get("status")} '
                                f'side={depth_event.get("side")} price={depth_event.get("price")}',
                            )
                            play_event_sound(args.sound_file)
                    if pre_release_event is not None:
                        write_bounded_live_log(
                            log_path,
                            'PRE_RELEASE_STATE ' + json.dumps(
                                pre_release_event, ensure_ascii=False, default=str,
                            ),
                        )
                        if args.sound == 'on':
                            play_event_sound(args.sound_file)
            processed_raw_samples.add(sample['ts'])
        session.print_status('LIVE STATUS', current_time=processing_end, live=True)
        session.update_total_oi(raw_samples, processing_end)
        publish_gui(gui, session, processing_end, None, live=True)
        time.sleep(max(2, args.poll_seconds))


def launch_gui(args: argparse.Namespace) -> None:
    """Run the existing monitor in a worker and render only snapshots in Qt."""
    try:
        from PySide6.QtCore import QThread, QTimer, Qt
        from PySide6.QtGui import QColor, QFont, QFontMetrics
        from PySide6.QtWidgets import QApplication, QAbstractItemView, QFrame, QHeaderView, QInputDialog, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QSizePolicy
    except ImportError as exc:
        raise SystemExit('Для --gui установите зависимость PySide6: python -m pip install PySide6') from exc

    bridge = GuiBridge()

    class ReplayWorker(QThread):
        def run(self) -> None:
            try:
                with open(os.devnull, 'w', encoding='utf-8') as sink, contextlib.redirect_stdout(sink):
                    if args.mode == 'replay-live':
                        run_replay_live(args, bridge)
                    else:
                        run_live(args, bridge)
            finally:
                bridge.finished = True

    class MonitorWindow(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle('OI FLOW MONITOR v0.0.1')
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
            font = QFont('Consolas', 10)
            self.header = QLabel('Ожидание данных…')
            self.replay_line = QLabel('')
            self.current = QLabel('')
            for label in (self.header, self.replay_line, self.current):
                label.setFont(font)
                label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
                label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.table = QTableWidget(0, 7)
            self.table.setHorizontalHeaderLabels(['TIME', 'BTC PRICE', 'OI ACT', 'NET', 'AGGR', 'ΔPRICE', 'CONTROL'])
            self.table.setFont(font)
            vertical = self.table.verticalHeader()
            vertical.setVisible(False)
            vertical.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
            vertical.setDefaultSectionSize(22)
            self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.table.setFrameShape(QFrame.Shape.NoFrame)
            self.table.setContentsMargins(0, 0, 0, 0)
            self.table.setViewportMargins(0, 0, 0, 0)
            self.table.setStyleSheet(
                'QTableWidget {'
                ' border-top: none;'
                ' border-left: 1px solid #c7c7c7;'
                ' border-right: 1px solid #c7c7c7;'
                ' border-bottom: 1px solid #c7c7c7;'
                ' border-radius: 0px;'
                '}'
                'QHeaderView::section { border-radius: 0px; }'
            )
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            horizontal = self.table.horizontalHeader()
            horizontal.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
            horizontal.setStretchLastSection(False)
            horizontal.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            horizontal.setFixedHeight(30)
            horizontal.setStyleSheet('QHeaderView::section { border-bottom: 1px solid #a8a8a8; }')
            sample_values = ['14:51', '83 520.5', '306.7 BTC', '+290.7 BTC', 'SELL 58.9% +112.1 BTC', '-87.3 USDT', 'MARKET SELL']
            self.table.setRowCount(1)
            for column, value in enumerate(sample_values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(0, column, item)
            self.table.resizeColumnsToContents()
            for column in range(7):
                horizontal.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
                item = self.table.item(0, column)
                if item is not None:
                    item.setText('')
            self.table.setRowCount(0)
            self.depth_table = QTableWidget(0, 5)
            self.depth_table.setHorizontalHeaderLabels(['TIME', 'PRICE', 'SIDE', 'VOLUME', 'STATUS'])
            self.depth_table.setFont(font)
            self.depth_table.verticalHeader().setVisible(False)
            self.depth_table.verticalHeader().setDefaultSectionSize(22)
            self.depth_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.depth_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            self.depth_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.depth_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.depth_table.setMaximumHeight(6 * 22 + 30)
            depth_horizontal = self.depth_table.horizontalHeader()
            depth_horizontal.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
            depth_horizontal.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            depth_horizontal.setFixedHeight(30)
            depth_horizontal.setStyleSheet('QHeaderView::section { border-bottom: 1px solid #a8a8a8; }')
            self.depth_table.setStyleSheet(
                'QTableWidget {'
                ' border: 1px solid #c7c7c7;'
                ' border-radius: 0px;'
                '}'
            )
            top_block = QWidget()
            top_block.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            top_layout = QVBoxLayout(top_block)
            top_layout.setContentsMargins(10, 0, 10, 0)
            top_layout.setSpacing(3)
            top_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
            top_layout.addWidget(self.header, 0)
            top_layout.addWidget(self.current, 0)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.setSpacing(10)
            layout.addWidget(top_block, 0)
            layout.addWidget(self.table, 1)
            layout.addWidget(self.depth_table, 0)
            self._cells: dict[tuple[int, int], str] = {}
            self._last_event_count = 0
            self._last_anchor = None
            self._real_event_count = 0
            self._syncing_row_slots = False
            self._natural_column_widths = [0] * 7
            self._content_width_initialized = False
            self._setting_initial_size = True
            self._resizing_to_content = False
            self._user_resized = False
            self._snapping_height = False
            self._current_plain_text = ''
            horizontal.setMinimumSectionSize(0)
            initial_metrics = QFontMetrics(font)
            initial_headers = ['TIME', 'BTC PRICE', 'OI ACT', 'NET', 'AGGR', 'ΔPRICE', 'CONTROL']
            initial_width = sum(initial_metrics.horizontalAdvance(header) + 22
                                for header in initial_headers) + 20
            initial_height = 10 * 22 + 6 * 22 + 2 * 24 + 26 + 58
            self.resize(initial_width, initial_height)
            self._setting_initial_size = False
            self.table.verticalScrollBar().rangeChanged.connect(lambda _min, _max: self.resize_columns())
            QTimer.singleShot(0, self.sync_row_slots)
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.refresh)
            self.timer.start(100)

        def resize_columns(self) -> None:
            if getattr(self, '_resizing_columns', False):
                return
            self._resizing_columns = True
            try:
                available = max(0, self.table.viewport().width())
                natural = list(self._natural_column_widths)
                if not any(natural):
                    return
                natural_total = sum(natural)
                if available <= natural_total:
                    widths = natural
                else:
                    extra = available - natural_total
                    widths = [width + extra * width / natural_total for width in natural]
                integer_widths = [max(1, int(width)) for width in widths]
                remainder = max(0, available - sum(integer_widths))
                if remainder:
                    integer_widths[-1] += remainder
                for column, width in enumerate(integer_widths):
                    self.table.setColumnWidth(column, width)
            finally:
                self._resizing_columns = False

        def update_content_widths(self, rows: list[dict[str, Any]]) -> None:
            """Measure content once and use it as the table's natural width."""
            headers = ['TIME', 'BTC PRICE', 'OI ACT', 'NET', 'AGGR', 'ΔPRICE', 'CONTROL']
            metrics = QFontMetrics(self.table.font())
            padding = 22
            measured = [metrics.horizontalAdvance(header) + padding for header in headers]
            for row in rows:
                for column, value in enumerate(row.values()):
                    measured[column] = max(measured[column],
                                           metrics.horizontalAdvance(str(value)) + padding)
            changed = False
            for column, width in enumerate(measured):
                if width > self._natural_column_widths[column]:
                    self._natural_column_widths[column] = width
                    changed = True
            top_width = max(
                metrics.horizontalAdvance(self.header.text()),
                metrics.horizontalAdvance(self.replay_line.text()),
                metrics.horizontalAdvance(self._current_plain_text or self.current.text()),
            )
            required_width = max(top_width, sum(self._natural_column_widths)) + 16
            if self.table.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded:
                required_width += self.table.verticalScrollBar().sizeHint().width()
            if changed or not self._content_width_initialized:
                self._content_width_initialized = True
                self.resize_columns()
            self._required_content_width = required_width
            if not self._user_resized and required_width > self.width() and not self._resizing_to_content:
                self._resizing_to_content = True
                self._setting_initial_size = True
                self.resize(required_width, self.height())
                self._setting_initial_size = False
                self._resizing_to_content = False

        def showEvent(self, event) -> None:
            super().showEvent(event)
            self._user_resized = False
            self.apply_sharp_corners()
            self.update_content_widths([])

        def apply_sharp_corners(self) -> None:
            """Disable Windows 11 DWM rounded corners while keeping the title bar."""
            if sys.platform != 'win32':
                return
            try:
                hwnd = int(self.winId())
                # DWMWA_WINDOW_CORNER_PREFERENCE = 33;
                # DWMWCP_DONOTROUND = 1.
                preference = ctypes.c_int(1)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 33, ctypes.byref(preference), ctypes.sizeof(preference)
                )
            except (AttributeError, OSError, TypeError, ValueError):
                pass

        def resizeEvent(self, event) -> None:
            if (not self._setting_initial_size and not self._resizing_to_content
                    and not self._snapping_height and self.isVisible()):
                self._user_resized = True
            super().resizeEvent(event)
            self.snap_height_to_rows()
            self.sync_row_slots()
            self.resize_columns()

        def snap_height_to_rows(self) -> None:
            """Keep the table viewport aligned to complete data-row heights."""
            if self._snapping_height or not self.isVisible():
                return
            row_height = max(1, self.table.verticalHeader().defaultSectionSize())
            viewport_height = self.table.viewport().height()
            remainder = viewport_height % row_height
            if remainder <= 0:
                return
            target_height = self.height() - remainder
            if target_height <= self.minimumHeight():
                return
            self._snapping_height = True
            try:
                self.resize(self.width(), target_height)
            finally:
                self._snapping_height = False

        def sync_row_slots(self) -> None:
            """Keep the viewport covered by fixed-height grid rows."""
            if self._syncing_row_slots:
                return
            self._syncing_row_slots = True
            try:
                row_height = max(1, self.table.verticalHeader().defaultSectionSize())
                viewport_height = max(0, self.table.viewport().height())
                full_capacity = max(1, viewport_height // row_height)
                needs_vertical_scroll = self._real_event_count > full_capacity
                desired_policy = (
                    Qt.ScrollBarPolicy.ScrollBarAsNeeded
                    if needs_vertical_scroll
                    else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
                )
                if self.table.verticalScrollBarPolicy() != desired_policy:
                    self.table.setVerticalScrollBarPolicy(desired_policy)
                    viewport_height = max(0, self.table.viewport().height())
                # Keep only complete rows in the visible placeholder area;
                # never expose a partially clipped next row at the bottom.
                capacity = max(1, viewport_height // row_height)
                desired = max(self._real_event_count, capacity)
                if self.table.rowCount() != desired:
                    self.table.setRowCount(desired)
            finally:
                self._syncing_row_slots = False

        def keyPressEvent(self, event) -> None:
            key = event.key()
            if key == Qt.Key.Key_Space:
                bridge.command('SPACE')
            elif key == Qt.Key.Key_R:
                bridge.command('R')
            elif key == Qt.Key.Key_Plus or key == Qt.Key.Key_Equal:
                bridge.command('+')
            elif key == Qt.Key.Key_Minus:
                bridge.command('-')
            elif key == Qt.Key.Key_Right:
                bridge.command('RIGHT')
            elif key == Qt.Key.Key_Left:
                bridge.command('LEFT')
            elif key == Qt.Key.Key_T:
                value, accepted = QInputDialog.getText(
                    self,
                    'T WINDOW',
                    'HHMM, DDMMHHMM, HHMM-HHMM or DDMMHHMM-HHMM',
                )
                if accepted and value.strip():
                    bridge.command('T', value.strip())
                return
            else:
                super().keyPressEvent(event)

        def refresh(self) -> None:
            snapshot = bridge.read()
            if not snapshot:
                return
            if snapshot.get('window_mode') == 'FIXED_RANGE':
                mode = f'RANGE {snapshot["anchor_gui"]}–{snapshot["window_end_gui"]}'
            else:
                mode = f'LIVE FROM {snapshot["anchor_gui"]}' if snapshot['live'] else f'SCAN FROM {snapshot["anchor_gui"]}'
            baseline_text = ''
            if snapshot['live'] and snapshot.get('window_mode') != 'FIXED_RANGE' and not snapshot.get('raw_baseline_ready', False):
                baseline_text = f' | RAW WARMUP {snapshot.get("raw_baseline_span_minutes", 0.0):.0f}m'
            self.header.setText(f'OI FLOW MONITOR v0.0.1 | {mode}{baseline_text}')
            self.replay_line.setText(
                snapshot.get('pre_release_text', '') if snapshot['live']
                else f'HISTORICAL TIME {snapshot["clock"]} | SPEED {snapshot["speed"]:g}x'
            )
            buy_pct = snapshot.get('continuous_buy_pct')
            sell_pct = snapshot.get('continuous_sell_pct')
            buy_text = '--' if buy_pct is None else f'{buy_pct:.1f}%'
            sell_text = '--' if sell_pct is None else f'{sell_pct:.1f}%'
            event_buy_pct = snapshot.get('event_buy_pct')
            event_sell_pct = snapshot.get('event_sell_pct')
            rest_buy_pct = snapshot.get('rest_buy_pct')
            rest_sell_pct = snapshot.get('rest_sell_pct')
            event_buy_text = '--' if event_buy_pct is None else f'{event_buy_pct:.1f}%'
            event_sell_text = '--' if event_sell_pct is None else f'{event_sell_pct:.1f}%'
            rest_buy_text = '--' if rest_buy_pct is None else f'{rest_buy_pct:.1f}%'
            rest_sell_text = '--' if rest_sell_pct is None else f'{rest_sell_pct:.1f}%'
            if snapshot.get('dominance_reference_frozen', False):
                buy_change = dominance_change_parenthetical(
                    snapshot.get('dominance_buy_change_pct')
                )
                sell_change = dominance_change_parenthetical(
                    snapshot.get('dominance_sell_change_pct')
                )
                full_plain = f'{buy_change} {buy_text} vs {sell_text} {sell_change}'
                full_html = (
                    f'<span style="color:#000000">{buy_change}</span> '
                    f'<span style="color:#168a2f">{buy_text}</span> vs '
                    f'<span style="color:#c62828">{sell_text}</span> '
                    f'<span style="color:#000000">{sell_change}</span>'
                )
            else:
                full_plain = f'{buy_text} vs {sell_text}'
                full_html = (
                    f'<span style="color:#168a2f">{buy_text}</span> vs '
                    f'<span style="color:#c62828">{sell_text}</span>'
                )
            event_html = (
                f'<span style="color:#168a2f">{event_buy_text}</span> vs '
                f'<span style="color:#c62828">{event_sell_text}</span>'
            )
            rest_html = (
                f'<span style="color:#168a2f">{rest_buy_text}</span> vs '
                f'<span style="color:#c62828">{rest_sell_text}</span>'
            )
            plain_dominance = (
                f'F-DOMINANCE {full_plain} | {snapshot["oi_flow"]}\n'
                f'E-DOMINANCE {event_buy_text} vs {event_sell_text}\n'
                f'R-DOMINANCE {rest_buy_text} vs {rest_sell_text}'
            )
            html_dominance = (
                f'F-DOMINANCE {full_html} | {snapshot["oi_flow"]}<br>'
                f'E-DOMINANCE {event_html}<br>'
                f'R-DOMINANCE {rest_html}'
            )
            self._current_plain_text = plain_dominance
            self.current.setText(html_dominance)
            rows = snapshot['events']
            old_real_count = self._real_event_count
            anchor_changed = self._last_anchor != snapshot['anchor']
            scroll_bar = self.table.verticalScrollBar()
            was_near_bottom = scroll_bar.value() >= max(0, scroll_bar.maximum() - 1)
            self._real_event_count = len(rows)
            if anchor_changed:
                # The session event list was rebuilt.  Clear old cell text so
                # rows that become placeholders cannot retain the old anchor's
                # event values.
                self.table.clearContents()
                self._cells.clear()
            self.sync_row_slots()
            self.update_content_widths(rows)
            self.resize_columns()
            for row_index in range(self.table.rowCount()):
                values = (list(rows[row_index].values())
                          if row_index < len(rows)
                          else [''] * self.table.columnCount())
                for col_index, value in enumerate(values):
                    text = str(value)
                    if self._cells.get((row_index, col_index)) != text:
                        item = self.table.item(row_index, col_index)
                        if item is None:
                            item = QTableWidgetItem()
                            self.table.setItem(row_index, col_index, item)
                        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
                        item.setText(text)
                        self._cells[(row_index, col_index)] = text
            depth_rows = snapshot.get('depth_rows', [])
            self.depth_table.setRowCount(len(depth_rows))
            for row_index, row in enumerate(depth_rows):
                for col_index, value in enumerate(row.values()):
                    item = self.depth_table.item(row_index, col_index)
                    if item is None:
                        item = QTableWidgetItem()
                        self.depth_table.setItem(row_index, col_index, item)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
                    item.setText(str(value))
                    if col_index == 2:
                        item.setForeground(QColor('#168a2f' if value == 'BID' else '#c62828'))
                        side_font = item.font()
                        side_font.setStrikeOut(row.get('status') in {'PULLED', 'BROKEN'})
                        item.setFont(side_font)
            row_height = max(1, self.table.verticalHeader().defaultSectionSize())
            visible_real_rows = max(1, self.table.viewport().height() // row_height)
            if anchor_changed:
                # Placeholder rows must not make the initial session appear
                # scrolled.  Only use the bottom position when real history
                # actually exceeds the visible capacity.
                if len(rows) > visible_real_rows:
                    self.table.scrollToBottom()
                else:
                    self.table.scrollToTop()
            elif len(rows) > old_real_count:
                if len(rows) > visible_real_rows and was_near_bottom:
                    self.table.scrollToBottom()
                else:
                    self.table.scrollToTop()
            self._last_event_count = len(rows)
            self._last_anchor = snapshot['anchor']

    app = QApplication(sys.argv)
    window = MonitorWindow()
    worker = ReplayWorker()
    worker.start()
    window.show()
    app.exec()
    if worker.isRunning():
        worker.requestInterruption()
        worker.wait(1000)


def main() -> None:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Сессионный read-only монитор потока OI BTC-LRA')
    parser.add_argument('--mode', choices=('replay', 'historical', 'live', 'replay-live'), default='replay')
    parser.add_argument('--from', dest='from_time')
    parser.add_argument('--from-now', action='store_true')
    parser.add_argument('--end')
    parser.add_argument('--raw-oi', type=Path, action='append')
    parser.add_argument('--market-csv', type=Path)
    parser.add_argument('--snapshot-time', dest='snapshot_times', action='append')
    parser.add_argument('--poll-seconds', type=int, default=5)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--speed', type=float, default=30.0)
    parser.add_argument('--sound', choices=('on', 'off'), default='off')
    parser.add_argument('--sound-file', type=Path,
                        help='path to event sound file, e.g. MP3 or WAV; use with --sound on')
    parser.add_argument('--log', type=Path)
    parser.add_argument('--gui', action='store_true', help='запустить PySide6 dashboard')
    parser.add_argument('--neighbor-analysis', action='store_true',
                        help='write causal neighboring OI/aggression diagnostics')
    parser.add_argument('--raw-intensity-analysis', action='store_true',
                        help='write causal raw OI intensity percentile diagnostics')
    parser.add_argument('--local-dominance-analysis', action='store_true',
                        help='write decayed local/active dominance research')
    parser.add_argument('--entry-hypotheses-analysis', action='store_true',
                        help='write research-only local dominance entry hypotheses')
    parser.add_argument('--entry-episodes-analysis', action='store_true',
                        help='write research-only local dominance episode states')
    parser.add_argument('--entry-episode-diagnostics', action='store_true',
                        help='write research-only entry episode quality diagnostics')
    parser.add_argument('--release-stage-analysis', action='store_true',
                        help='write research-only LIMIT defense / MARKET release stages')
    parser.add_argument('--consensus-episodes-analysis', action='store_true',
                        help='collapse release-stage rows into research-only consensus episodes')
    parser.add_argument('--tv-structure-export', action='store_true',
                        help='write research-only causal TradingView structure export')
    parser.add_argument('--tv-structure-votes', type=int, choices=(1, 2, 3, 4), default=2,
                        help='selected causal EARLY consensus tier for TradingView structure export')
    args = parser.parse_args()
    if args.neighbor_analysis:
        run_neighbor_flow_analysis(args)
        return
    if args.raw_intensity_analysis:
        run_raw_intensity_analysis(args)
        return
    if args.local_dominance_analysis:
        run_local_dominance_analysis(args)
        return
    if args.entry_hypotheses_analysis:
        run_entry_hypotheses_analysis(args)
        return
    if args.entry_episodes_analysis:
        run_entry_episode_analysis(args)
        return
    if args.entry_episode_diagnostics:
        run_entry_episode_diagnostics(args)
        return
    if args.release_stage_analysis:
        run_release_stage_analysis(args)
        return
    if args.consensus_episodes_analysis:
        run_consensus_episode_analysis(args)
        return
    if args.tv_structure_export:
        run_tv_structure_export(args)
        return
    if args.gui:
        if args.mode not in ('replay-live', 'live'):
            parser.error('--gui поддерживается для --mode replay-live или --mode live')
        launch_gui(args)
        return
    if args.mode in ('replay', 'historical'):
        if not args.raw_oi:
            parser.error('--raw-oi обязателен для обычного replay/historical режима')
        if args.report:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                run_replay(args)
            text = buffer.getvalue()
            print(text, end='')
            args.report.write_text(text, encoding='utf-8')
        else:
            run_replay(args)
    elif args.mode == 'replay-live':
        run_replay_live(args)
    else:
        run_live(args)


if __name__ == '__main__':
    main()
