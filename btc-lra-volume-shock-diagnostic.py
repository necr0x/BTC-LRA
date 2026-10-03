"""Diagnostic-only VOLUME_SHOCK research layer.

This module reads the isolated WebSocket prototype files under ``runtime/ws``.
It does not import, modify, or feed the production detector, monitor, P1/P2,
CONTROL, or F/E/R calculations.  Rarity is causal: the current observation is
scored against prior observations only.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

WINDOWS = (5, 15, 30, 60)
RARITY_LEVELS = (90.0, 95.0, 97.5, 99.0)
DEPTH_BPS = (5, 10, 25)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding='utf-8') as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')


def iso_ms(ts_ms: int | None) -> str | None:
    if ts_ms is None:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def finite_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


class CausalDistribution:
    def __init__(self) -> None:
        self.values: list[float] = []

    def score_then_add(self, value: float) -> tuple[float | None, float | None, int]:
        if not self.values:
            result = (None, None, 0)
        else:
            ordered = sorted(self.values)
            rank = sum(1 for prior in ordered if prior <= value)
            percentile = 100.0 * rank / len(ordered)
            result = (percentile, max(0.0, 1.0 - percentile / 100.0), len(ordered))
        self.values.append(value)
        return result


class DepthTimeline:
    """Causal synchronized-book cursor built from prototype snapshot/delta rows."""

    def __init__(self, snapshots: list[dict[str, Any]], deltas: list[dict[str, Any]]) -> None:
        self.events: list[tuple[int, int, dict[str, Any]]] = []
        snapshot_times: dict[str, int] = {}
        for row in snapshots:
            ts = int(row.get('snapshot_ts_ms') or row.get('received_ts_ms') or 0)
            epoch_key = str(row.get('sync_epoch'))
            snapshot_times[epoch_key] = ts
            self.events.append((ts, 0, row))
        for row in deltas:
            ts = int(row.get('event_ts_ms') or row.get('received_ts_ms') or 0)
            # Buffered updates can have exchange event timestamps earlier than
            # the REST snapshot receive time. They must be replayed after the
            # snapshot, not before it, within the same synchronization epoch.
            snapshot_ts = snapshot_times.get(str(row.get('sync_epoch')))
            if snapshot_ts is not None:
                ts = max(ts, snapshot_ts)
            self.events.append((ts, 1, row))
        self.events.sort(key=lambda item: (item[0], item[1]))
        self.index = 0
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.epoch: int | None = None
        self.last_ts: int | None = None

    @staticmethod
    def _levels(value: Any) -> list[list[Any]]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return []
        return value if isinstance(value, list) else []

    @staticmethod
    def _apply(book: dict[float, float], levels: Any) -> None:
        for level in DepthTimeline._levels(levels):
            if not isinstance(level, list) or len(level) < 2:
                continue
            price = finite_float(level[0])
            quantity = finite_float(level[1])
            if price <= 0:
                continue
            if quantity <= 0:
                book.pop(price, None)
            else:
                book[price] = quantity

    def advance_to(self, before_ts_ms: int) -> bool:
        changed = False
        while self.index < len(self.events) and self.events[self.index][0] < before_ts_ms:
            ts, kind, row = self.events[self.index]
            if kind == 0:
                self.bids.clear()
                self.asks.clear()
                self._apply(self.bids, row.get('bids_json'))
                self._apply(self.asks, row.get('asks_json'))
                self.epoch = row.get('sync_epoch')
            elif self.epoch is None or row.get('sync_epoch') == self.epoch:
                self._apply(self.bids, row.get('bids_json'))
                self._apply(self.asks, row.get('asks_json'))
            self.last_ts = ts
            self.index += 1
            changed = True
        return changed and bool(self.bids) and bool(self.asks)

    def visible_depth(self, bps: int) -> dict[str, float] | None:
        if not self.bids or not self.asks:
            return None
        best_bid = max(self.bids)
        best_ask = min(self.asks)
        mid = (best_bid + best_ask) / 2.0
        band = mid * bps / 10_000.0
        bid = sum(qty for price, qty in self.bids.items() if price >= mid - band)
        ask = sum(qty for price, qty in self.asks.items() if price <= mid + band)
        return {'bid': bid, 'ask': ask, 'mid': mid}


@dataclass
class WindowState:
    trades: deque[dict[str, Any]]


def aggregate_window(trades: Iterable[dict[str, Any]]) -> dict[str, float]:
    rows = list(trades)
    quantities = [finite_float(row.get('quantity')) for row in rows]
    buy = sum(q for row, q in zip(rows, quantities) if row.get('aggressor_side') == 'BUY')
    sell = sum(q for row, q in zip(rows, quantities) if row.get('aggressor_side') == 'SELL')
    total = buy + sell
    top = sorted(quantities, reverse=True)
    top_trade_mass = sum(top[:5])
    return {
        'total_volume_btc': total,
        'trade_count': float(len(rows)),
        'trade_rate': float(len(rows)),
        'taker_buy_btc': buy,
        'taker_sell_btc': sell,
        'aggr_delta_btc': buy - sell,
        'aggr_share': abs(buy - sell) / total if total else 0.0,
        'largest_aggtrade_btc': max(quantities, default=0.0),
        'top_trade_mass_btc': top_trade_mass,
        'top_trade_mass_share': top_trade_mass / total if total else 0.0,
    }


def parse_oi(path: Path) -> list[tuple[int, float]]:
    result: list[tuple[int, float]] = []
    for row in load_jsonl(path):
        raw_ts = row.get('server_ts_ms') or row.get('timestamp_ms')
        if raw_ts is None:
            text = row.get('timestamp_utc') or row.get('server_time_utc')
            if text:
                try:
                    raw_ts = int(datetime.fromisoformat(str(text).replace('Z', '+00:00')).timestamp() * 1000)
                except ValueError:
                    continue
        if raw_ts is None:
            continue
        value = row.get('oi_btc') or row.get('open_interest_btc') or row.get('oi')
        if value is not None:
            result.append((int(raw_ts), finite_float(value)))
    return sorted(result)


class VolumeShockAnalyzer:
    def __init__(self, root: Path, oi_path: Path | None = None) -> None:
        self.root = root
        self.trades = sorted(load_jsonl(root / 'BTC_LRA_AGGTRADE_WS.jsonl'), key=lambda r: int(r.get('trade_ts_ms') or 0))
        self.forceorders = sorted(load_jsonl(root / 'BTC_LRA_FORCEORDER_WS.jsonl'), key=lambda r: int(r.get('event_ts_ms') or 0))
        depth_snapshots = load_jsonl(root / 'BTC_LRA_DEPTH_WS_SNAPSHOTS.jsonl')
        depth_deltas = load_jsonl(root / 'BTC_LRA_DEPTH_WS.jsonl')
        self.depth_by_window = {
            window: DepthTimeline(depth_snapshots, depth_deltas)
            for window in WINDOWS
        }
        self.oi = parse_oi(oi_path) if oi_path else []
        self.oi_index = 0
        self.oi_value: float | None = None
        self.distributions: dict[tuple[int, str], CausalDistribution] = {
            (window, metric): CausalDistribution()
            for window in WINDOWS
            for metric in ('total_volume_btc', 'trade_rate', 'buy_turnover', 'sell_turnover', 'total_turnover')
        }

    def _oi_at(self, ts_ms: int) -> float | None:
        while self.oi_index < len(self.oi) and self.oi[self.oi_index][0] <= ts_ms:
            self.oi_value = self.oi[self.oi_index][1]
            self.oi_index += 1
        return self.oi_value

    def _force_metrics(self, start_ms: int, end_ms: int) -> dict[str, float]:
        rows = [row for row in self.forceorders if start_ms <= int(row.get('event_ts_ms') or 0) <= end_ms]
        return {
            'long_liquidation_btc': sum(finite_float(r.get('quantity')) for r in rows if r.get('liquidation_side') == 'LONG'),
            'short_liquidation_btc': sum(finite_float(r.get('quantity')) for r in rows if r.get('liquidation_side') == 'SHORT'),
            'liquidation_notional_usdt': sum(finite_float(r.get('notional_usdt')) for r in rows),
            'liquidation_count': float(len(rows)),
        }

    def run(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        states = {window: WindowState(deque()) for window in WINDOWS}
        features: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        for trade in self.trades:
            now = int(trade.get('trade_ts_ms') or trade.get('event_ts_ms') or 0)
            for state in states.values():
                state.trades.append(trade)
            row_values: dict[str, Any] = {'ts_ms': now, 'ts': iso_ms(now), 'stream': 'VOLUME_SHOCK_FEATURES'}
            any_tail = False
            for window, state in states.items():
                cutoff = now - window * 1000
                while state.trades and int(state.trades[0].get('trade_ts_ms') or 0) < cutoff:
                    state.trades.popleft()
                metrics = aggregate_window(state.trades)
                depth_values: dict[str, Any] = {}
                self.depth_by_window[window].advance_to(cutoff)
                depth_cursor = self.depth_by_window[window]
                for bps in DEPTH_BPS:
                    visible = depth_cursor.visible_depth(bps)
                    depth_values[f'bid_depth_{bps}bps'] = visible['bid'] if visible else None
                    depth_values[f'ask_depth_{bps}bps'] = visible['ask'] if visible else None
                ask = depth_values.get('ask_depth_10bps')
                bid = depth_values.get('bid_depth_10bps')
                metrics['buy_turnover'] = metrics['taker_buy_btc'] / ask if ask else 0.0
                metrics['sell_turnover'] = metrics['taker_sell_btc'] / bid if bid else 0.0
                visible_total = (ask or 0.0) + (bid or 0.0)
                metrics['total_turnover'] = metrics['total_volume_btc'] / visible_total if visible_total else 0.0
                rarity: dict[str, Any] = {}
                for metric in ('total_volume_btc', 'trade_rate', 'buy_turnover', 'sell_turnover', 'total_turnover'):
                    percentile, tail, reference_count = self.distributions[(window, metric)].score_then_add(metrics[metric])
                    rarity[f'{metric}_percentile'] = percentile
                    rarity[f'{metric}_tail_probability'] = tail
                    rarity[f'{metric}_reference_count'] = reference_count
                    if percentile is not None and any(percentile >= level for level in RARITY_LEVELS):
                        any_tail = True
                prefix = f'w{window}_'
                for key, value in {**metrics, **depth_values, **rarity}.items():
                    row_values[prefix + key] = value
            row_values.update(self._force_metrics(now - 60_000, now))
            current_oi = self._oi_at(now)
            row_values['oi_btc'] = current_oi
            row_values['event_type'] = 'VOLUME_SHOCK' if any_tail else None
            features.append(row_values)
            if any_tail:
                events.append({
                    'event_type': 'VOLUME_SHOCK',
                    'event_ts_ms': now,
                    'event_ts': iso_ms(now),
                    'candidate_rarity_levels': list(RARITY_LEVELS),
                    'trigger_metrics': [
                        metric for metric in ('total_volume_btc', 'trade_rate', 'buy_turnover', 'sell_turnover', 'total_turnover')
                        if any(row_values.get(f'w{window}_{metric}_percentile') is not None and row_values[f'w{window}_{metric}_percentile'] >= level for window in WINDOWS for level in RARITY_LEVELS)
                    ],
                    'descriptive_label': None,
                })
        return features, events


def self_test() -> None:
    trades = [
        {'trade_ts_ms': 1_000, 'aggressor_side': 'BUY', 'quantity': 2.0},
        {'trade_ts_ms': 2_000, 'aggressor_side': 'SELL', 'quantity': 1.0},
    ]
    metrics = aggregate_window(trades)
    assert metrics['total_volume_btc'] == 3.0
    assert metrics['taker_buy_btc'] == 2.0 and metrics['taker_sell_btc'] == 1.0
    dist = CausalDistribution()
    assert dist.score_then_add(10.0)[2] == 0
    assert dist.score_then_add(20.0)[2] == 1
    depth = DepthTimeline(
        [{'snapshot_ts_ms': 100, 'sync_epoch': 1, 'bids_json': '[[100, 2]]', 'asks_json': '[[100.01, 3]]'}],
        [{'event_ts_ms': 50, 'sync_epoch': 1, 'bids_json': '[[100, 1]]', 'asks_json': '[]'}],
    )
    assert depth.advance_to(101)
    visible = depth.visible_depth(10)
    assert visible and visible['bid'] == 1.0 and visible['ask'] == 3.0
    print('VOLUME_SHOCK_SELF_TEST=PASS')


def write_focus_report(root: Path, features: list[dict[str, Any]], events: list[dict[str, Any]], focus_text: str) -> None:
    try:
        focus = datetime.fromisoformat(focus_text).replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit('--focus must be ISO local/UTC datetime, e.g. 2026-10-03T10:30:00')
    focus_ms = int(focus.timestamp() * 1000)
    selected = [row for row in features if focus_ms - 300_000 <= int(row['ts_ms']) <= focus_ms + 300_000]
    selected_events = [row for row in events if focus_ms - 300_000 <= int(row['event_ts_ms']) <= focus_ms + 300_000]
    lines = [
        '# VOLUME_SHOCK focus report', '',
        f'Focus: {focus.isoformat()}',
        'Diagnostic-only; no production labels or thresholds were applied.', '',
        f'Feature rows in ±5m: {len(selected)}',
        f'VOLUME_SHOCK rows in ±5m: {len(selected_events)}', '',
        '| time | event | w5 volume | w5 trades | w5 buy turnover | w5 sell turnover | liq BTC | OI |',
        '|---|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for row in selected:
        lines.append('| {0} | {1} | {2:.4f} | {3:.0f} | {4:.6f} | {5:.6f} | {6:.4f} | {7} |'.format(
            row['ts'], row.get('event_type') or '', row.get('w5_total_volume_btc', 0.0), row.get('w5_trade_count', 0.0),
            row.get('w5_buy_turnover', 0.0), row.get('w5_sell_turnover', 0.0),
            row.get('long_liquidation_btc', 0.0) + row.get('short_liquidation_btc', 0.0), row.get('oi_btc'),
        ))
    (root / 'BTC_LRA_VOLUME_SHOCK_FOCUS_REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description='Diagnostic-only BTCUSDT VOLUME_SHOCK analyzer')
    parser.add_argument('--input', default='runtime/ws')
    parser.add_argument('--oi', default='runtime/collector/BTC_LRA_OI_RAW.jsonl')
    parser.add_argument('--output', default='runtime/ws')
    parser.add_argument('--focus')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    analyzer = VolumeShockAnalyzer(Path(args.input), Path(args.oi))
    features, events = analyzer.run()
    output = Path(args.output)
    write_jsonl(output / 'BTC_LRA_VOLUME_SHOCK_FEATURES.jsonl', features)
    write_jsonl(output / 'BTC_LRA_VOLUME_SHOCK_EVENTS.jsonl', events)
    if args.focus:
        write_focus_report(output, features, events, args.focus)
    print(f'VOLUME_SHOCK_FEATURES={len(features)} EVENTS={len(events)}')


if __name__ == '__main__':
    main()
