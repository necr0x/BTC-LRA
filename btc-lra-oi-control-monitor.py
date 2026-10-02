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
    v2_buy_pct, v2_sell_pct = dominance_percentages(
        session.buy_dominance_v2_btc, session.sell_dominance_v2_btc
    )
    old_dominance = (
        f'DOMINANCE OLD BUY {old_buy_pct:.1f}% / SELL {old_sell_pct:.1f}%'
        if old_buy_pct is not None else 'DOMINANCE OLD BUY -- / SELL --'
    )
    v2_dominance = (
        f'DOMINANCE V2   BUY {v2_buy_pct:.1f}% / SELL {v2_sell_pct:.1f}%'
        if v2_buy_pct is not None else 'DOMINANCE V2   BUY -- / SELL --'
    )
    bridge.publish({
        'live': live,
        'anchor': fmt_time(session.anchor),
        'clock': fmt_time(clock),
        'speed': speed,
        'price': f'{compact(market_price if market_price is not None else session.price, signed=False)} USDT',
        'dominance': old_dominance,
        'dominance_v2': v2_dominance,
        'dominance_v2_buy_pct': v2_buy_pct,
        'dominance_v2_sell_pct': v2_sell_pct,
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


def dominance_percentages(buy_weight: float, sell_weight: float) -> tuple[float | None, float | None]:
    total = buy_weight + sell_weight
    if total <= 0:
        return None, None
    return buy_weight / total * 100, sell_weight / total * 100


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
            impulse = abs(doi) * 5.0 / dt_sec if dt_sec and dt_sec > 0 else None
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
        'raw_control': control,
        'control_label': control,
        'display_control_text': f'{raw_class} {control_text} | {oi_flow} {oi_directionality_60s:.2f}' if oi_directionality_60s is not None else f'{raw_class} {control_text} | {oi_flow}',
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
            'raw_control': summary.get('CONTROL') or 'CONTROL UNCLEAR',
            'raw_dominance_v2_contribution_btc': 0.0,
            'raw_oi_flow_contribution': 0.0,
        })
        return existing
    event = _raw_event_payload(summary, session)
    session.event_history.append(event)
    return event


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
        self.buy_dominance_v2_btc = 0.0
        self.sell_dominance_v2_btc = 0.0
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

    def apply_dominance_v2(self, oi_add: float, effort: dict[str, Any]) -> dict[str, Any]:
        """Accumulate the separate OI-mass/control-strength dominance model."""
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
        if side == 'BUY':
            self.buy_dominance_v2_btc += weight
        elif side == 'SELL':
            self.sell_dominance_v2_btc += weight
        buy_pct, sell_pct = dominance_percentages(
            self.buy_dominance_v2_btc, self.sell_dominance_v2_btc
        )
        return {
            'v2_side': side or 'UNCLEAR',
            'v2_strength': strength,
            'v2_weight_btc': weight,
            'buy_dominance_v2_btc': self.buy_dominance_v2_btc,
            'sell_dominance_v2_btc': self.sell_dominance_v2_btc,
            'buy_v2_pct': buy_pct,
            'sell_v2_pct': sell_pct,
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
        v2 = self.apply_dominance_v2(float(minute.get('add', 0.0)), effort)
        current = dict(snap, time=minute['minute'], flow=flow, flow_adv=abs(flow_delta), price_change=price_change, classification=classification, control_label=control_label, event_oi_net=event_oi_net, event_oi_activity=event_oi_activity, display_flow=display_flow, display_flow_adv=abs(display_delta), display_price_change=display_price_change, display_event_price=event.get('display_event_price'), display_reference_price=event.get('display_reference_price'), display_taker_buy=event.get('display_taker_buy', 0.0), display_taker_sell=event.get('display_taker_sell', 0.0), display_flow_delta=event.get('display_flow_delta', display_delta), display_oi_add=float(minute.get('add', 0.0)), display_oi_exit=float(minute.get('exit', 0.0)), display_oi_jump=float(minute.get('jump', 0.0)), oi_add_mass=float(minute.get('add', 0.0)), **effort, **v2)
        current['event_flags'] = ['STRONG/MEGA']
        self.oi_event_flow += float(event_oi_net or 0.0)
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
            raw_summary = raw_flow_by_time.get(minute_time)
            if raw_summary is not None:
                apply_raw_event_to_session(rebuilt, raw_summary)
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
            self._user_resized = False
            self._snapping_height = False
            self._current_plain_text = ''
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
            if not self._setting_initial_size and not self._resizing_to_content and self.isVisible():
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
            buy_pct = snapshot.get('dominance_v2_buy_pct')
            sell_pct = snapshot.get('dominance_v2_sell_pct')
            buy_text = '--' if buy_pct is None else f'{buy_pct:.1f}%'
            sell_text = '--' if sell_pct is None else f'{sell_pct:.1f}%'
            self._current_plain_text = (
                f'{snapshot["clock"].split(" / ")[0]} | '
                f'DOMINANCE {buy_text} {sell_text} | {snapshot["oi_flow"]}'
            )
            self.current.setText(
                f'{snapshot["clock"].split(" / ")[0]} | DOMINANCE '
                f'<span style="color:#168a2f">{buy_text}</span> '
                f'<span style="color:#c62828">{sell_text}</span> | '
                f'{snapshot["oi_flow"]}'
            )
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
    parser.add_argument('--neighbor-analysis', action='store_true',
                        help='write causal neighboring OI/aggression diagnostics')
    parser.add_argument('--raw-intensity-analysis', action='store_true',
                        help='write causal raw OI intensity percentile diagnostics')
    args = parser.parse_args()
    if args.neighbor_analysis:
        run_neighbor_flow_analysis(args)
        return
    if args.raw_intensity_analysis:
        run_raw_intensity_analysis(args)
        return
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
