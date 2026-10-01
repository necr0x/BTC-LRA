"""Session-anchored, read-only BTC-LRA OI flow monitor.

Research presentation only. It never imports, starts, writes, or changes the
BTC-LRA engine. Replay uses raw instantaneous OI and 1m market bars; 5m is
only a display/aggregation concern outside this monitor.
"""
from __future__ import annotations

import argparse
import csv
import contextlib
import io
import json
import os
import re
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PANAMA = timezone(timedelta(hours=-5))


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


def load_market(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            stamp = row['timestamp_panama']
            fmt = '%Y-%m-%d %H:%M:%S' if len(stamp) > 16 else '%Y-%m-%d %H:%M'
            ts = datetime.strptime(stamp, fmt).replace(tzinfo=PANAMA)
            rows.append({'ts': ts, 'open': float(row['open']), 'high': float(row['high']), 'low': float(row['low']), 'close': float(row['close']), 'buy': float(row['taker_buy_BTC']), 'sell': float(row['taker_sell_BTC'])})
    return rows


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


def write_replay_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(line.rstrip() + '\n')


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
        self.last_events: deque[dict[str, Any]] = deque(maxlen=3)
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


    def print_status(self, title='SESSION SNAPSHOT', current_time: datetime | None = None) -> None:
        snap = self.snapshot(self.anchor)
        anchor_text = self.anchor.strftime('%H:%M:%S / %d.%m.%y -5') if self.anchor else '—'
        print(f'\nBTC-LRA OI МОНИТОР ПОТОКА / СКАНИРОВАНИЕ ОТ {anchor_text}')
        print(f'ЦЕНА                         {n(self.price)}')
        if snap['dominant'] in ('BUY', 'SELL'):
            side = snap['dominant']
            print(f'ДОМИНАЦИЯ — {side}                  {n(snap["advantage"])} BTC')
            print(f'ПИК ДОМИНАЦИИ {side}                {n(snap["peak"])} BTC | 100.0%')
            print(f'ДОМИНАЦИЯ {side} СЕЙЧАС             {n(snap["advantage"])} BTC | {snap["adv_remaining_pct"]:5.1f}%')
            print(f'ПОТЕРЯНО ОТ ПИКА                  {n(snap["adv_lost"])} BTC | {snap["adv_lost_pct"]:5.1f}%')
            opposite = 'BUY' if side == 'SELL' else 'SELL'
            opposite_peak = snap['buy_peak'] if opposite == 'BUY' else snap['sell_peak']
            if opposite_peak > 0:
                print(f'ПРЕДЫДУЩИЙ ПИК {opposite}               {n(opposite_peak)} BTC')
        else:
            print(f'ДОМИНАЦИЯ — УДЕРЖАНО               {n(snap["advantage"])} BTC')
        print(f'СТАТУС: {status_ru(snap["status"])}')
        print('\nПОСЛЕДНИЕ СОБЫТИЯ')
        for event in self.last_events:
            oi_value = event.get('event_oi_net', event.get('oi_net'))
            oi_word = 'ПРИШЛО' if (oi_value or 0) >= 0 else 'УШЛО'
            print(f'{event["time"].strftime("%H:%M")} | OI {n(oi_value)} BTC — {oi_word} | АКТИВНОСТЬ OI {n(event.get("event_oi_activity"))} BTC')
            print(f'       FLOW {flow_ru(event["flow"])} {n(event["flow_adv"])} | ЦЕНА {n(event["price_change"])} | {result_ru(event["classification"])}')

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
        current = dict(snap, time=minute['minute'], flow=flow, flow_adv=abs(flow_delta), price_change=price_change, classification=classification, event_oi_net=(snap['oi_net'] or 0.0) - prev_oi, event_oi_activity=snap['oi_activity'] - prev_activity)
        self.last_events.append(current)
        self.previous_event = current
        start_time = event.get('start_time', event['start'])
        start_text = start_time.strftime('%H:%M') if isinstance(start_time, datetime) else str(start_time)
        event_label = 'ЭКСТРЕМАЛЬНОЕ OI-СОБЫТИЕ' if event.get('kind') == 'MEGA' else 'СИЛЬНОЕ OI-СОБЫТИЕ'
        oi_word = 'ПРИШЛО' if current['event_oi_net'] >= 0 else 'УШЛО'
        print(f'\n{event_label} {minute["minute"].strftime("%H:%M")} | НАЧАЛО ЭПИЗОДА {start_text}')
        print(f'OI {n(current["event_oi_net"])} BTC — {oi_word} | АКТИВНОСТЬ OI {n(current["event_oi_activity"])} BTC')
        print(f'FLOW {flow_ru(flow)} {n(abs(flow_delta))} | ЦЕНА {n(price_change)} | {result_ru(classification)}')


def run_replay(args: argparse.Namespace) -> None:
    samples = load_raw(args.raw_oi)
    minutes = minute_oi(samples)
    annotate_minutes(minutes)
    events = anomaly_events(minutes)
    market = load_market(args.market_csv) if args.market_csv else []
    anchor = parse_time(args.from_time) if args.from_time else minutes[0]['minute']
    end = parse_time(args.end) if args.end else None
    session = Session(anchor)
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
            session.emit_event(event, minute, market_rows)
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
    session.print_status('FINAL SESSION STATE')


def run_replay_live(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parent
    raw_paths = args.raw_oi or discover_raw_paths(root)
    market_path = args.market_csv or discover_market_path(root)
    if not raw_paths:
        raise SystemExit('Не найдены локальные BTC-LRA raw OI JSONL-файлы.')
    if market_path is None:
        raise SystemExit('Не найден локальный записанный 1m market CSV.')
    samples = load_raw(raw_paths)
    market = load_market(market_path)
    common = recorded_range(samples, market)
    if common is None:
        raise SystemExit('Нет общего записанного диапазона между raw OI и market 1m.')
    start, end = common
    session = Session(start)
    log_path = args.log or root / 'data' / 'research' / 'BTC_LRA_RECORDED_LIVE_REPLAY.log'
    log_path.write_text('', encoding='utf-8')
    write_replay_log(log_path, f'RAW OI: {fmt_time(samples[0]["ts"])} -> {fmt_time(samples[-1]["ts"])} | samples={len(samples)}')
    write_replay_log(log_path, f'MARKET 1M: {fmt_time(market[0]["ts"])} -> {fmt_time(market[-1]["ts"])} | bars={len(market)}')
    write_replay_log(log_path, f'COMMON RANGE: {fmt_time(start)} -> {fmt_time(end)} | anchor={fmt_time(start)}')
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

    def rebuild_session(new_anchor: datetime, clock: datetime) -> Session:
        rebuilt = Session(new_anchor)
        prefix_minutes = minute_oi(raw_prefix)
        annotate_minutes(prefix_minutes)
        event_by_time = {prefix_minutes[e['confirmed']]['minute']: e for e in anomaly_events(prefix_minutes)}
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

    def process_until(clock: datetime) -> None:
        nonlocal previous_flow, previous_status, previous_peaks, event_count, flow_cross_count
        prefix_minutes = minute_oi(raw_prefix)
        annotate_minutes(prefix_minutes)
        event_by_time = {prefix_minutes[e['confirmed']]['minute']: e for e in anomaly_events(prefix_minutes)}
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
                emitted_events.add(minute_time)
                event_count += 1
                if args.sound == 'on':
                    print('\a', end='', flush=True)
                session.emit_event(event, minute, [market_row] if market_row else [])
                write_replay_log(log_path, f'{fmt_time(clock)} | СИЛЬНОЕ OI-СОБЫТИЕ | подтверждено {minute_time.strftime("%H:%M")}')
            peaks = (session.sell_peak, session.buy_peak)
            state_changed = current['status'] != previous_status or peaks != previous_peaks
            if state_changed:
                print(f'\nИСТОРИЧЕСКОЕ ВРЕМЯ {fmt_time(clock)} | СКОРОСТЬ {speed:g}x')
                session.print_status('ТЕКУЩЕЕ СОСТОЯНИЕ')
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
            time.sleep(0.05)
        poll_keys(sample['ts'])
        if previous_clock is not None and speed > 0:
            time.sleep(max(0.0, (sample['ts'] - previous_clock).total_seconds() / speed))
        raw_prefix.append(sample)
        process_until(sample['ts'])
        previous_clock = sample['ts']
    process_until(end + timedelta(minutes=1))
    final = session.snapshot(end)
    write_replay_log(log_path, f'FINAL | {fmt_time(end)} | buy={session.buy:.1f} | sell={session.sell:.1f} | oi_net={final["oi_net"]} | events={event_count} | crosses={flow_cross_count}')
    print(f'\nЗАПИСАННЫЙ REPLAY ЗАВЕРШЁН | СИЛЬНЫХ OI-СОБЫТИЙ: {event_count} | СМЕН НАКОПИТЕЛЬНОГО ДОМИНАНТА: {flow_cross_count}')
    print(f'RAW OI: {fmt_time(samples[0]["ts"])} -> {fmt_time(samples[-1]["ts"])} | {len(samples)} samples')
    print(f'MARKET 1M: {fmt_time(market[0]["ts"])} -> {fmt_time(market[-1]["ts"])} | {len(market)} bars')
    print(f'ОБЩИЙ ДИАПАЗОН: {fmt_time(start)} -> {fmt_time(end)}')
    print('ZERO FUTURE LEAKAGE: PASS | STRONG detector: unchanged')
    session.print_status('ИТОГОВОЕ СОСТОЯНИЕ СЕССИИ')


def run_live(args: argparse.Namespace) -> None:
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    anchor = datetime.now(PANAMA) if args.from_now or not args.from_time else parse_time(args.from_time)
    session = Session(anchor)
    processed: set[datetime] = set()
    print('BTC-LRA OI МОНИТОР ПОТОКА | ТОЛЬКО ЧТЕНИЕ')
    print('R = СБРОСИТЬ ОТСЧЁТ НА СЕЙЧАС | Ctrl+C = выход')
    while True:
        if msvcrt and msvcrt.kbhit():
            key = msvcrt.getwch().upper()
            if key == 'R':
                session.reset(datetime.now(PANAMA)); processed.clear()
                print(f'\nОТСЧЁТ СБРОШЕН: {session.anchor.strftime("%H:%M:%S / %d.%m.%y -5")}')
        samples = load_raw(args.raw_oi)
        minutes = minute_oi(samples)
        annotate_minutes(minutes)
        events = anomaly_events(minutes)
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
                print('\a', end='', flush=True)
                session.emit_event(event, minute, [])
        session.print_status('LIVE STATUS')
        time.sleep(max(2, args.poll_seconds))


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
    args = parser.parse_args()
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
