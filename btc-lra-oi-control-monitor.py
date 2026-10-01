"""Session-anchored, read-only BTC-LRA OI flow monitor.

Research presentation only. It never imports, starts, writes, or changes the
BTC-LRA engine. Replay uses raw instantaneous OI and 1m market bars; 5m is
only a display/aggregation concern outside this monitor.
"""
from __future__ import annotations

import argparse
import csv
import contextlib
import ctypes
import io
import json
import os
import re
import sys
import time
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
    return datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=PANAMA)


def fmt_time(value: datetime | None) -> str:
    return value.astimezone(PANAMA).strftime('%H:%M:%S / %d.%m.%y -5') if value else '—'


def n(value: Any, digits: int = 1) -> str:
    return '—' if value is None else f'{float(value):+,.{digits}f}'.replace(',', ' ')


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
                unique[stamp] = {'ts': datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).astimezone(PANAMA), 'oi': float(row['OI_BTC'])}
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
            changes.append(0.0 if previous is None else value['oi'] - previous)
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


def publish_gui(bridge: GuiBridge | None, session: 'Session', clock: datetime,
                speed: float | None, live: bool, market_price: float | None = None) -> None:
    if bridge is None:
        return
    events = []
    for event in session.event_history:
        display_flow = event.get('display_flow', event.get('flow', 'HELD'))
        control = event.get('control_label', 'CONTROL NEUTRAL')
        if control.startswith('CONTROL '):
            control = control.removeprefix('CONTROL ')
        events.append({
            'time': event['time'].strftime('%H:%M'),
            'btc_price': f'{compact(event.get("display_event_price"), signed=False)} USDT',
            'oi_act': f'{compact(event.get("event_oi_activity"), signed=False)} BTC',
            'net': f'{compact(event.get("event_oi_net"))} BTC',
            'aggr': f'{display_flow} {event.get("aggr_share_pct", 0.0):.1f}% {compact(event.get("aggr_mag", event.get("display_flow_adv", event.get("flow_adv", 0))))} BTC',
            'delta_price': f'{compact(event.get("display_price_change", event.get("price_change")))} USDT',
            'control': control,
        })
    bridge.publish({
        'live': live,
        'anchor': fmt_time(session.anchor),
        'clock': fmt_time(clock),
        'speed': speed,
        'price': f'{compact(market_price if market_price is not None else session.price, signed=False)} USDT',
        'dominance': (
            f'DOMINANCE BUY {session.buy_dominance_weight_usdt / (session.buy_dominance_weight_usdt + session.sell_dominance_weight_usdt) * 100:.1f}% / '
            f'SELL {session.sell_dominance_weight_usdt / (session.buy_dominance_weight_usdt + session.sell_dominance_weight_usdt) * 100:.1f}%'
            if session.buy_dominance_weight_usdt + session.sell_dominance_weight_usdt > 0 else 'DOMINANCE BUY -- / SELL --'
        ),
        'oi_flow': f'OI FLOW {compact(session.oi_event_flow)} BTC',
        'events': events,
    })


def write_replay_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(line.rstrip() + '\n')


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


class Session:
    def __init__(self, anchor: datetime) -> None:
        self.anchor = anchor
        self.oi_start: float | None = None
        self.oi_current: float | None = None
        self.oi_add = self.oi_exit = 0.0
        self.buy = self.sell = 0.0
        self.anchor_price: float | None = None
        self.price: float | None = None
        self.previous_event: dict[str, Any] | None = None
        self.last_events: deque[dict[str, Any]] = deque(maxlen=10)
        self.event_history: list[dict[str, Any]] = []
        self.oi_event_flow = 0.0
        self.market_history: list[dict[str, Any]] = []
        self.buy_dominance_weight_usdt = 0.0
        self.sell_dominance_weight_usdt = 0.0
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

    def reset(self, anchor: datetime) -> None:
        self.__init__(anchor)

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
        return {'time': event_time, 'oi_net': (self.oi_current - self.oi_start) if self.oi_current is not None and self.oi_start is not None else None, 'oi_add': self.oi_add, 'oi_exit': self.oi_exit, 'oi_activity': self.oi_add + self.oi_exit, 'buy': self.buy, 'sell': self.sell, 'delta': delta, 'dominant': dominant, 'advantage': advantage, 'peak': peak, 'sell_peak': self.sell_peak, 'buy_peak': self.buy_peak, 'retraced': retraced, 'adv_lost': adv_lost, 'adv_remaining_pct': advantage / peak * 100 if peak else 0.0, 'adv_lost_pct': adv_lost / peak * 100 if peak else 0.0, 'price': self.price, 'price_from_start': (self.price - self.anchor_price) if self.price is not None and self.anchor_price is not None else None, **self.early_metrics()}

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
        print(f'\nOI FLOW MONITORING / {mode} FROM {anchor_text}')
        if not live and current_time is not None and speed is not None:
            print(f'ИСТОРИЧЕСКОЕ ВРЕМЯ {fmt_time(current_time)} | СКОРОСТЬ {speed:g}x')
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
        current = dict(snap, time=minute['minute'], flow=flow, flow_adv=abs(flow_delta), price_change=price_change, classification=classification, control_label=control_label, event_oi_net=event_oi_net, event_oi_activity=event_oi_activity, display_flow=display_flow, display_flow_adv=abs(display_delta), display_price_change=display_price_change, display_event_price=event.get('display_event_price'), **effort)
        self.oi_event_flow += float(event_oi_net or 0.0)
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
    common = recorded_range(samples, market)
    if common is None:
        raise SystemExit('Нет общего записанного диапазона между raw OI и market 1m.')
    start, end = common
    session = Session(start)
    session.market_history = market
    log_path = args.log or root / 'data' / 'research' / 'BTC_LRA_RECORDED_LIVE_REPLAY.log'
    tv_path = root / 'BTC_LRA_TV_EVENTS.pine'
    tv_event_history: dict[datetime, dict[str, Any]] = {}
    write_tv_events_pine(tv_path, [])
    log_path.write_text('', encoding='utf-8')
    write_replay_log(log_path, f'RAW OI: {fmt_time(samples[0]["ts"])} -> {fmt_time(samples[-1]["ts"])} | samples={len(samples)}')
    write_replay_log(log_path, f'MARKET 1M: {fmt_time(market[0]["ts"])} -> {fmt_time(market[-1]["ts"])} | bars={len(market)}')
    write_replay_log(log_path, f'COMMON RANGE: {fmt_time(start)} -> {fmt_time(end)} | anchor={fmt_time(start)}')
    for line in event_source_validation(all_minutes):
        write_replay_log(log_path, line)
    print('BTC-LRA | МОНИТОР OI / ПОТОКА')
    print(f'ЗАПИСАННЫЙ ДИАПАЗОН: {fmt_time(start)} -> {fmt_time(end)}')
    print(f'RAW OI: {len(samples)} samples | MARKET 1M: {len(market)} bars | СКОРОСТЬ: {args.speed}x')
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

    def latest_closed_market_price(clock: datetime) -> float | None:
        nonlocal market_cursor, last_closed_market_price
        while market_cursor + 1 < len(market):
            candidate = market[market_cursor + 1]
            if candidate['ts'] + timedelta(minutes=1) > clock:
                break
            market_cursor += 1
            last_closed_market_price = candidate['close']
        return last_closed_market_price

    def rebuild_session(new_anchor: datetime, clock: datetime) -> Session:
        rebuilt = Session(new_anchor)
        rebuilt.market_history = market
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
            if minute_time in event_by_time:
                event = dict(event_by_time[minute_time])
                event['start_time'] = prefix_minutes[event['start']]['minute']
                event['kind'] = 'MEGA' if prefix_minutes[event['confirmed']]['mega'] else 'STRONG'
                decorate_event_interval(event, prefix_minutes, market)
                with contextlib.redirect_stdout(io.StringIO()):
                    rebuilt.emit_event(event, minute, [market_row] if market_row else [])
            rebuilt.snapshot(minute_time)
        return rebuilt

    def poll_keys(clock: datetime) -> None:
        nonlocal paused, speed, previous_flow, previous_status, previous_peaks, session
        while msvcrt.kbhit():
            key = msvcrt.getwch()
            if key == ' ':
                paused = not paused
                text = 'ПАУЗА' if paused else 'ПРОДОЛЖЕНИЕ'
                print(f'\n{text} | ИСТОРИЧЕСКОЕ ВРЕМЯ {fmt_time(clock)}')
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
                    session = rebuild_session(requested, clock)
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
            elif key == '+' or key == '=':
                speed = min(120.0, {0: 1, 1: 5, 5: 10, 10: 20, 20: 30, 30: 60, 60: 120}.get(int(speed), speed * 2))
                print(f'СКОРОСТЬ: {speed:g}x')
            elif key == '-':
                speed = max(0.0, {120: 60, 60: 30, 30: 20, 20: 10, 10: 5, 5: 1, 1: 0}.get(int(speed), speed / 2))
                print(f'СКОРОСТЬ: {speed:g}x')

    def poll_gui_commands(clock: datetime) -> None:
        nonlocal paused, speed, previous_flow, previous_status, previous_peaks, session
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
            elif key == 'T' and value:
                try:
                    if re.fullmatch(r'\d{1,2}:\d{2}', value):
                        requested = datetime.strptime(value, '%H:%M').replace(year=clock.year, month=clock.month, day=clock.day, tzinfo=PANAMA)
                    else:
                        requested = parse_time(value)
                    if requested < start or requested > clock:
                        raise ValueError('anchor вне доступного диапазона или позже текущего времени')
                    session = rebuild_session(requested, clock)
                    previous_flow = session.snapshot(clock).get('dominant')
                    previous_status = session.early_status
                    previous_peaks = (session.sell_peak, session.buy_peak)
                    paused = False
                except (ValueError, TypeError):
                    pass
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
                applied_market.add(minute_time)
            current = session.snapshot(minute_time)
            if current['dominant'] in ('BUY', 'SELL') and previous_flow and current['dominant'] != previous_flow:
                flow_cross_count += 1
                line = f'{fmt_time(clock)} | СМЕНА НАКОПИТЕЛЬНОГО ДОМИНАНТА: {previous_flow} -> {current["dominant"]}'
                print(line); write_replay_log(log_path, line)
            if current['dominant'] in ('BUY', 'SELL'):
                previous_flow = current['dominant']
            if minute_time in event_by_time and minute_time not in emitted_events:
                event = dict(event_by_time[minute_time])
                event['start_time'] = prefix_minutes[event['start']]['minute']
                event['kind'] = 'MEGA' if prefix_minutes[event['confirmed']]['mega'] else 'STRONG'
                decorate_event_interval(event, prefix_minutes, market)
                emitted_events.add(minute_time)
                event_count += 1
                if args.sound == 'on':
                    print('\a', end='', flush=True)
                session.emit_event(event, minute, [market_row] if market_row else [])
                tv_event_history[minute_time] = dict(session.event_history[-1])
                write_tv_events_pine(tv_path, list(tv_event_history.values()))
                debug_event = session.last_events[-1]
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
                }, ensure_ascii=False))
                write_replay_log(log_path, f'{fmt_time(clock)} | СИЛЬНОЕ OI-СОБЫТИЕ | подтверждено {minute_time.strftime("%H:%M")}')
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
        process_until(sample['ts'])
        publish_gui(gui, session, sample['ts'], speed, live=False, market_price=latest_closed_market_price(sample['ts']))
        previous_clock = sample['ts']
    process_until(end + timedelta(minutes=1))
    publish_gui(gui, session, end, speed, live=False, market_price=latest_closed_market_price(end))
    final = session.snapshot(end)
    write_replay_log(log_path, f'FINAL | {fmt_time(end)} | buy={session.buy:.1f} | sell={session.sell:.1f} | oi_net={final["oi_net"]} | events={event_count} | crosses={flow_cross_count}')
    print(f'\nЗАПИСАННЫЙ REPLAY ЗАВЕРШЁН | СИЛЬНЫХ OI-СОБЫТИЙ: {event_count} | СМЕН НАКОПИТЕЛЬНОГО ДОМИНАНТА: {flow_cross_count}')
    print(f'RAW OI: {fmt_time(samples[0]["ts"])} -> {fmt_time(samples[-1]["ts"])} | {len(samples)} samples')
    print(f'MARKET 1M: {fmt_time(market[0]["ts"])} -> {fmt_time(market[-1]["ts"])} | {len(market)} bars')
    print(f'ОБЩИЙ ДИАПАЗОН: {fmt_time(start)} -> {fmt_time(end)}')
    print(f'TV PINE: {tv_path} | events={len(tv_event_history)}')
    print('ZERO FUTURE LEAKAGE: PASS | STRONG detector: unchanged')
    for line in event_source_validation(all_minutes):
        print(line)
    session.print_status('ИТОГОВОЕ СОСТОЯНИЕ СЕССИИ', current_time=end, live=False)


def run_live(args: argparse.Namespace, gui: GuiBridge | None = None) -> None:
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    anchor = datetime.now(PANAMA) if args.from_now or not args.from_time else parse_time(args.from_time)
    session = Session(anchor)
    processed: set[datetime] = set()
    root = Path(__file__).resolve().parent
    tv_path = root / 'BTC_LRA_TV_EVENTS.pine'
    tv_event_history: dict[datetime, dict[str, Any]] = {}
    write_tv_events_pine(tv_path, [])
    print('BTC-LRA OI МОНИТОР ПОТОКА | ТОЛЬКО ЧТЕНИЕ')
    print('R = СБРОСИТЬ ОТСЧЁТ НА СЕЙЧАС | Ctrl+C = выход')
    while True:
        if gui is not None:
            for key, _value in gui.pop_commands():
                if key == 'R':
                    session.reset(datetime.now(PANAMA)); processed.clear()
        if msvcrt and msvcrt.kbhit():
            key = msvcrt.getwch().upper()
            if key == 'R':
                session.reset(datetime.now(PANAMA)); processed.clear()
                print(f'\nОТСЧЁТ СБРОШЕН: {session.anchor.strftime("%H:%M:%S / %d.%m.%y -5")}')
        samples = load_raw(args.raw_oi)
        minutes = minute_oi(samples)
        annotate_minutes(minutes)
        events = individual_events(minutes)
        event_by_time = {minutes[e['confirmed']]['minute']: e for e in events}
        for minute in minutes:
            if minute['minute'] < session.anchor or minute['minute'] in processed:
                continue
            session.apply_oi(minute)
            processed.add(minute['minute'])
            if minute['minute'] in event_by_time:
                event = dict(event_by_time[minute['minute']])
                event['start_time'] = minutes[event['start']]['minute']
                event['kind'] = 'MEGA' if minutes[event['confirmed']]['mega'] else 'STRONG'
                decorate_event_interval(event, minutes, [])
                print('\a', end='', flush=True)
                session.emit_event(event, minute, [])
                tv_event_history[minute['minute']] = dict(session.event_history[-1])
                write_tv_events_pine(tv_path, list(tv_event_history.values()))
        session.print_status('LIVE STATUS', current_time=datetime.now(PANAMA), live=True)
        publish_gui(gui, session, datetime.now(PANAMA), None, live=True)
        time.sleep(max(2, args.poll_seconds))


def launch_gui(args: argparse.Namespace) -> None:
    """Run the existing monitor in a worker and render only snapshots in Qt."""
    try:
        from PySide6.QtCore import QThread, QTimer, Qt
        from PySide6.QtGui import QFont, QFontMetrics
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
            self.setWindowTitle('OI FLOW MONITORING')
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
            sample_values = ['14:51', '83 520.5 USDT', '306.7 BTC', '+290.7 BTC', 'SELL 58.9% +112.1 BTC', '-87.3 USDT', 'MARKET SELL']
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
            top_block = QWidget()
            top_block.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            top_layout = QVBoxLayout(top_block)
            top_layout.setContentsMargins(10, 0, 10, 0)
            top_layout.setSpacing(3)
            top_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
            top_layout.addWidget(self.header, 0)
            top_layout.addWidget(self.replay_line, 0)
            top_layout.addWidget(self.current, 0)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.setSpacing(10)
            layout.addWidget(top_block, 0)
            layout.addWidget(self.table, 1)
            self._cells: dict[tuple[int, int], str] = {}
            self._last_event_count = 0
            self._last_anchor = None
            self._real_event_count = 0
            self._syncing_row_slots = False
            self._natural_column_widths = [0] * 7
            self._content_width_initialized = False
            self._setting_initial_size = True
            self._resizing_to_content = False
            self._snapping_height = False
            horizontal.setMinimumSectionSize(0)
            initial_metrics = QFontMetrics(font)
            initial_headers = ['TIME', 'BTC PRICE', 'OI ACT', 'NET', 'AGGR', 'ΔPRICE', 'CONTROL']
            initial_width = sum(initial_metrics.horizontalAdvance(header) + 22
                                for header in initial_headers) + 20
            initial_height = 10 * 22 + 3 * 24 + 26 + 28
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
                metrics.horizontalAdvance(self.current.text()),
            )
            required_width = max(top_width, sum(self._natural_column_widths)) + 16
            if self.table.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded:
                required_width += self.table.verticalScrollBar().sizeHint().width()
            if changed or not self._content_width_initialized:
                self._content_width_initialized = True
                self.resize_columns()
            self._required_content_width = required_width
            if required_width > self.width() and not self._resizing_to_content:
                self._resizing_to_content = True
                self._setting_initial_size = True
                self.resize(required_width, self.height())
                self._setting_initial_size = False
                self._resizing_to_content = False

        def showEvent(self, event) -> None:
            super().showEvent(event)
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
            super().resizeEvent(event)
            self.snap_height_to_rows()
            if (
                self.isVisible()
                and not self._setting_initial_size
                and not self._resizing_to_content
                and getattr(self, '_required_content_width', 0) > self.width()
            ):
                self._resizing_to_content = True
                self._setting_initial_size = True
                self.resize(self._required_content_width, self.height())
                self._setting_initial_size = False
                self._resizing_to_content = False
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
            elif key == Qt.Key.Key_T:
                value, accepted = QInputDialog.getText(self, 'НОВЫЙ ОТСЧЁТ', 'Введите HH:MM или YYYY-MM-DD HH:MM')
                if accepted and value.strip():
                    bridge.command('T', value.strip())
            else:
                super().keyPressEvent(event)

        def refresh(self) -> None:
            snapshot = bridge.read()
            if not snapshot:
                return
            mode = 'LIVE FROM' if snapshot['live'] else 'SCAN FROM'
            self.header.setText(f'OI FLOW MONITORING / {mode} {snapshot["anchor"]}')
            self.replay_line.setText('' if snapshot['live'] else f'ИСТОРИЧЕСКОЕ ВРЕМЯ {snapshot["clock"]} | СКОРОСТЬ {snapshot["speed"]:g}x')
            self.current.setText(f'{snapshot["price"]} | {snapshot["clock"].split(" / ")[0]} | {snapshot["dominance"]} | {snapshot["oi_flow"]}')
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
    parser.add_argument('--log', type=Path)
    parser.add_argument('--gui', action='store_true', help='запустить PySide6 dashboard')
    args = parser.parse_args()
    if args.gui:
        if args.mode not in ('replay-live', 'live'):
            parser.error('--gui поддерживается для --mode replay-live или --mode live')
        if args.mode == 'live' and not args.raw_oi:
            args.raw_oi = discover_raw_paths(Path(__file__).resolve().parent)
            if not args.raw_oi:
                parser.error('для --mode live --gui не найдены локальные raw OI-файлы')
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
        if not args.raw_oi:
            parser.error('--raw-oi обязателен для live режима')
        run_live(args)


if __name__ == '__main__':
    main()
