"""Session-anchored, read-only BTC-LRA OI control monitor.

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
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PANAMA = timezone(timedelta(hours=-5))


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
        self.sell_effort_btc = self.sell - self.low_sell_effort
        self.buy_effort_btc = self.buy - self.high_buy_effort
        self.last_low_extension = 0.0
        self.last_high_extension = 0.0
        if self.last_low is None:
            self.last_low = row['low']
            self.low_sell_effort = self.sell
        elif row['low'] < self.last_low:
            self.last_low_extension = self.last_low - row['low']
            self.sell_effort_btc = max(0.0, self.sell - self.low_sell_effort)
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
            self.buy_effort_btc = max(0.0, self.buy - self.high_buy_effort)
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
            'last_low': self.last_low,
            'current_low': self.last_low,
            'low_extension': self.last_low_extension,
            'sell_effort_btc': self.sell_effort_btc,
            'low_extension_per_100': self.last_low_extension / self.sell_effort_btc * 100 if self.sell_effort_btc > 0 else 0.0,
            'last_high': self.last_high,
            'current_high': self.last_high,
            'high_extension': self.last_high_extension,
            'buy_effort_btc': self.buy_effort_btc,
            'high_extension_per_100': self.last_high_extension / self.buy_effort_btc * 100 if self.buy_effort_btc > 0 else 0.0,
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
        return {'time': event_time, 'oi_net': (self.oi_current - self.oi_start) if self.oi_current is not None and self.oi_start is not None else None, 'oi_add': self.oi_add, 'oi_exit': self.oi_exit, 'oi_activity': self.oi_add + self.oi_exit, 'buy': self.buy, 'sell': self.sell, 'delta': delta, 'dominant': dominant, 'advantage': advantage, 'peak': peak, 'sell_peak': self.sell_peak, 'buy_peak': self.buy_peak, 'retraced': retraced, 'price': self.price, 'price_from_start': (self.price - self.anchor_price) if self.price is not None and self.anchor_price is not None else None, **self.early_metrics()}

    def print_status(self, title='SESSION SNAPSHOT') -> None:
        snap = self.snapshot(self.anchor)
        print(f'\nBTC-LRA OI CONTROL MONITOR\n{title}\nSESSION FROM: {self.anchor.strftime("%H:%M") if self.anchor else "—"}')
        print(f'PRICE              {n(self.price)}')
        print(f'OI NET             {n(snap["oi_net"])} BTC\nOI ADD              {n(self.oi_add)} BTC\nOI EXIT             {n(self.oi_exit)} BTC\nOI ACTIVITY         {n(snap["oi_activity"])} BTC')
        print(f'CUM BUY             {n(self.buy)} BTC\nCUM SELL            {n(self.sell)} BTC\nCUM DOMINANCE       {snap["dominant"]} {n(snap["advantage"])} BTC')
        print(f'SELL PEAK ADV       {n(self.sell_peak)} BTC\nBUY PEAK ADV        {n(self.buy_peak)} BTC\nCURRENT RETRACED    {snap["retraced"]:.1f}%\nPRICE FROM START    {n(snap["price_from_start"])} USD')
        print(f'SELL ADV NOW        {n(snap["sell_adv_now"])} BTC | RETRACED {snap["sell_adv_retraced"]:.1f}%')
        print(f'BUY ADV NOW         {n(snap["buy_adv_now"])} BTC | RETRACED {snap["buy_adv_retraced"]:.1f}%')
        print(f'LAST LOW            {n(snap["last_low"])} | CURRENT LOW {n(snap["current_low"])} | LOW EXTENSION {n(snap["low_extension"])}')
        print(f'SELL EFFORT         {n(snap["sell_effort_btc"])} BTC | LOW / 100 BTC {n(snap["low_extension_per_100"])} USD')
        print(f'LAST HIGH           {n(snap["last_high"])} | CURRENT HIGH {n(snap["current_high"])} | HIGH EXTENSION {n(snap["high_extension"])}')
        print(f'BUY EFFORT          {n(snap["buy_effort_btc"])} BTC | HIGH / 100 BTC {n(snap["high_extension_per_100"])} USD')
        print(f'STATUS: {snap["status"]}')
        print('\nLAST EVENTS')
        for event in self.last_events:
            print(f'{event["time"].strftime("%H:%M")} | OI {n(event["oi_net"])} | FLOW {event["flow"]} {n(event["flow_adv"])} | PRICE {n(event["price_change"])} | RESULT CONTROL {event["classification"]}')

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
        print(f'\nRAW OI EVENT CONFIRMED AT {minute["minute"].strftime("%H:%M")} | EPISODE START {start_text}')
        print(f'SINCE PREVIOUS EVENT: OI NET {n((snap["oi_net"] or 0)-prev_oi)} | BUY {n(snap["buy"]-prev_buy)} | SELL {n(snap["sell"]-prev_sell)} | FLOW {flow} {n(abs(flow_delta))} | PRICE {n(price_change)}')
        print(f'SINCE SESSION ANCHOR: OI NET {n(snap["oi_net"])} | ADD {n(snap["oi_add"])} | EXIT {n(snap["oi_exit"])} | BUY {n(snap["buy"])} | SELL {n(snap["sell"])} | CUM FLOW DOMINANT {snap["dominant"]} {n(snap["advantage"])} | RESULT CONTROL {classification}')
        print(f'EXTREMUM STATE: {snap["status"]} | SELL ADV NOW {n(snap["sell_adv_now"])} / RETRACED {snap["sell_adv_retraced"]:.1f}% | LOW EXT / 100 SELL {n(snap["low_extension_per_100"])} | HIGH EXT / 100 BUY {n(snap["high_extension_per_100"])}')


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
        if current['dominant'] in ('BUY', 'SELL') and previous_control and current['dominant'] != previous_control:
            flow_crosses.append((minute['minute'].strftime('%H:%M'), previous_control, current['dominant']))
            print(f'\nCUM FLOW CROSS: {previous_control} -> {current["dominant"]} at {minute["minute"].strftime("%H:%M")}')
        if current['dominant'] in ('BUY', 'SELL'):
            previous_control = current['dominant']
        if minute['minute'] in event_by_time:
            event = dict(event_by_time[minute['minute']])
            event['start_time'] = minutes[event['start']]['minute']
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
            print(f'\nCHECKPOINT {row["time"]}')
            print(f'CUM BUY {n(row["buy"])} | CUM SELL {n(row["sell"])} | CUM FLOW DOMINANT {row["dominant"]} | CUM ADV {n(row["advantage"])}')
            print(f'PEAK ADV {n(row["peak"])} | ADV NOW {n(row["advantage"])} | ADV RETRACED {row["retraced"]:.1f}% | PRICE FROM ANCHOR {n(row["price_from_start"])}')
            print(f'OI NET {n(row["oi_net"])} | OI ADD {n(row["oi_add"])} | OI EXIT {n(row["oi_exit"])} | OI ACTIVITY {n(row["oi_activity"])}')
            print(f'SINCE PREVIOUS CHECKPOINT: BUY {n(interval_buy)} | SELL {n(interval_sell)} | FLOW {interval_flow} {n(abs(interval_delta))} | PRICE {n(interval_price)} | RESULT CONTROL {result}')
            print(f'EXTREMUM STATUS: {row["status"]} | SELL PEAK {n(row["sell_peak"])} | SELL NOW {n(row["sell_adv_now"])} | SELL RETRACED {row["sell_adv_retraced"]:.1f}% | LOW EXT / 100 SELL {n(row["low_extension_per_100"])}')
            print(f'BUY PEAK {n(row["buy_peak"])} | BUY NOW {n(row["buy_adv_now"])} | BUY RETRACED {row["buy_adv_retraced"]:.1f}% | HIGH EXT / 100 BUY {n(row["high_extension_per_100"])}')
            previous_checkpoint = current
    visible_events = [event for event in events if anchor <= minutes[event['confirmed']]['minute'] and (end is None or minutes[event['confirmed']]['minute'] <= end)]
    print(f'\nREPLAY COMPLETE | raw samples={len(samples)} | minute aggregates={len(minutes)} | valid STRONG episodes={len(visible_events)}')
    print('\nCUM FLOW CROSSES:')
    for at, old, new in flow_crosses:
        print(f'{at} | {old} -> {new}')
    print('\nRESULT CONTROL CHANGES:')
    for at, old, new in result_changes:
        print(f'{at} | {old} -> {new}')
    print('\nCHRONOLOGICAL CHECKPOINT TABLE:')
    print('TIME | CUM FLOW | CUM ADV | PRICE FROM ANCHOR | INTERVAL FLOW | INTERVAL PRICE | RESULT CONTROL')
    for row in checkpoint_rows:
        print(f'{row["time"]} | {row["dominant"]} {n(row["advantage"])} | {n(row["advantage"])} | {n(row["price_from_start"])} | {row["interval_flow"]} {n(abs(row["interval_buy"]-row["interval_sell"]))} | {n(row["interval_price"])} | {row["result"]}')
    session.print_status('FINAL SESSION STATE')


def run_live(args: argparse.Namespace) -> None:
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    anchor = datetime.now(PANAMA) if args.from_now or not args.from_time else parse_time(args.from_time)
    session = Session(anchor)
    processed: set[datetime] = set()
    print('BTC-LRA OI CONTROL MONITOR | READ-ONLY LIVE')
    print('R = RESET SESSION ANCHOR TO NOW | Ctrl+C = exit')
    while True:
        if msvcrt and msvcrt.kbhit():
            key = msvcrt.getwch().upper()
            if key == 'R':
                session.reset(datetime.now(PANAMA)); processed.clear()
                print(f'\nSESSION ANCHOR RESET: {session.anchor.strftime("%H:%M:%S / %d.%m.%y -5")}')
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
                print('\a', end='', flush=True)
                session.emit_event(event, minute, [])
        session.print_status('LIVE STATUS')
        time.sleep(max(2, args.poll_seconds))


def main() -> None:
    parser = argparse.ArgumentParser(description='Session-anchored read-only BTC-LRA OI control monitor')
    parser.add_argument('--mode', choices=('replay', 'historical', 'live'), default='replay')
    parser.add_argument('--from', dest='from_time')
    parser.add_argument('--from-now', action='store_true')
    parser.add_argument('--end')
    parser.add_argument('--raw-oi', type=Path, action='append', required=True)
    parser.add_argument('--market-csv', type=Path)
    parser.add_argument('--snapshot-time', dest='snapshot_times', action='append')
    parser.add_argument('--poll-seconds', type=int, default=5)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.mode in ('replay', 'historical'):
        if args.report:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                run_replay(args)
            text = buffer.getvalue()
            print(text, end='')
            args.report.write_text(text, encoding='utf-8')
        else:
            run_replay(args)
    else:
        run_live(args)


if __name__ == '__main__':
    main()
