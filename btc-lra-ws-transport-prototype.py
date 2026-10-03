"""Diagnostic-only BTCUSDT USD-M WebSocket transport prototype.

This file is intentionally isolated from the production collector and monitor.
It writes only under runtime/ws and does not modify the existing JSONL feeds.
Requires the optional ``websockets`` package for live mode; ``--self-test``
uses only the standard library.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SYMBOL = 'BTCUSDT'
SYMBOL_WS = SYMBOL.lower()
REST_BASE = 'https://fapi.binance.com'
DEPTH_WS = f'wss://fstream.binance.com/public/ws/{SYMBOL_WS}@depth@100ms'
AGGTRADE_WS = f'wss://fstream.binance.com/market/ws/{SYMBOL_WS}@aggTrade'
WS_MAX_SESSION_SECONDS = 23 * 60 * 60 + 50 * 60
QUEUE_MAX = 20_000
RECONNECT_MAX_SECONDS = 60.0


def epoch_ms() -> int:
    return time.time_ns() // 1_000_000


def iso_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


class JsonlSink:
    """Single diagnostic writer used by the prototype's writer task."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.paths = {
            'DEPTH': self.root / 'BTC_LRA_DEPTH_WS.jsonl',
            'DEPTH_SNAPSHOT': self.root / 'BTC_LRA_DEPTH_WS_SNAPSHOTS.jsonl',
            'AGGTRADE': self.root / 'BTC_LRA_AGGTRADE_WS.jsonl',
            'AGGREGATE': self.root / 'BTC_LRA_AGGTRADE_WS_AGGREGATES.jsonl',
            'FINALIZED': self.root / 'BTC_LRA_AGGTRADE_WS_FINALIZED.jsonl',
            'HEALTH': self.root / 'BTC_LRA_WS_HEALTH.log',
        }

    def write(self, stream: str, row: dict[str, Any]) -> None:
        path = self.paths[stream]
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')

    def write_batch(self, stream: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        path = self.paths[stream]
        with path.open('a', encoding='utf-8') as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')

    def health(self, message: str) -> None:
        with self.paths['HEALTH'].open('a', encoding='utf-8') as handle:
            handle.write(f'{datetime.now(timezone.utc).isoformat()} {message}\n')


@dataclass
class FeedState:
    name: str
    last_event_ms: int | None = None
    connected_ms: int | None = None
    reconnect_count: int = 0
    gap_count: int = 0
    sync_epoch: int = 0
    state: str = 'DISCONNECTED'


@dataclass
class DepthBook:
    """Sequence-aware local book; no production depth research is called."""

    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    last_update_id: int | None = None
    sync_epoch: int = 0
    state: str = 'UNSYNCED'

    def discard(self, epoch: int) -> None:
        self.bids.clear()
        self.asks.clear()
        self.last_update_id = None
        self.sync_epoch = epoch
        self.state = 'UNSYNCED'

    def load_snapshot(self, payload: dict[str, Any], epoch: int) -> None:
        self.discard(epoch)
        self.bids = {float(price): float(qty) for price, qty in payload.get('bids', []) if float(qty) > 0}
        self.asks = {float(price): float(qty) for price, qty in payload.get('asks', []) if float(qty) > 0}
        self.last_update_id = int(payload['lastUpdateId'])
        self.state = 'SYNCING'

    def apply(self, payload: dict[str, Any], first_after_snapshot: bool = False) -> str:
        first_id = int(payload['U'])
        final_id = int(payload['u'])
        previous_id = payload.get('pu')
        if self.last_update_id is None:
            return 'UNSYNCED'
        if final_id <= self.last_update_id:
            return 'STALE'
        if first_after_snapshot:
            if not (first_id <= self.last_update_id + 1 <= final_id):
                if first_id > self.last_update_id + 1:
                    self.state = 'RESYNC'
                    return 'SEQUENCE_GAP'
                return 'WAIT'
        elif previous_id is not None and int(previous_id) != self.last_update_id:
            self.state = 'RESYNC'
            return 'SEQUENCE_GAP'
        elif previous_id is None and first_id != self.last_update_id + 1:
            self.state = 'RESYNC'
            return 'SEQUENCE_GAP'
        for price, quantity in payload.get('b', []):
            self._apply_level(self.bids, price, quantity)
        for price, quantity in payload.get('a', []):
            self._apply_level(self.asks, price, quantity)
        self.last_update_id = final_id
        self.state = 'LIVE'
        return 'APPLIED'

    @staticmethod
    def _apply_level(book: dict[float, float], price: str | float, quantity: str | float) -> None:
        price_value = float(price)
        quantity_value = float(quantity)
        if quantity_value == 0:
            book.pop(price_value, None)
        else:
            book[price_value] = quantity_value


@dataclass
class AggTradeAggregator:
    buckets: dict[tuple[int, int], dict[str, float]] = field(default_factory=dict)

    def add(self, trade: dict[str, Any], received_ms: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        trade_ms = int(trade['trade_ts_ms'])
        side = trade['aggressor_side']
        quantity = float(trade['quantity'])
        price = float(trade['price'])
        result: list[dict[str, Any]] = []
        for seconds in (5, 30, 60):
            bucket_ms = (trade_ms // (seconds * 1000)) * seconds * 1000
            bucket_key = (seconds, bucket_ms)
            bucket = self.buckets.setdefault(bucket_key, {'buy': 0.0, 'sell': 0.0, 'notional': 0.0, 'trades': 0.0})
            bucket['buy' if side == 'BUY' else 'sell'] += quantity
            bucket['notional'] += price * quantity
            bucket['trades'] += 1
            result.append({
                'stream': 'AGGREGATE',
                'bucket_seconds': seconds,
                'bucket_start_ms': bucket_ms,
                'bucket_start': iso_ms(bucket_ms),
                'taker_buy_btc': bucket['buy'],
                'taker_sell_btc': bucket['sell'],
                'total_volume_btc': bucket['buy'] + bucket['sell'],
                'net_aggression_btc': bucket['buy'] - bucket['sell'],
                'notional_usdt': bucket['notional'],
                'trade_count': int(bucket['trades']),
                'last_received_ts_ms': received_ms,
            })
        finalized: list[dict[str, Any]] = []
        for (bucket_seconds, bucket_start_ms), bucket in list(self.buckets.items()):
            bucket_end_ms = bucket_start_ms + bucket_seconds * 1000
            if bucket_end_ms <= trade_ms:
                finalized.append({
                    'stream': 'AGGREGATE_FINAL',
                    'bucket_seconds': bucket_seconds,
                    'bucket_start_ms': bucket_start_ms,
                    'bucket_end_ms': bucket_end_ms,
                    'bucket_start': iso_ms(bucket_start_ms),
                    'bucket_end': iso_ms(bucket_end_ms),
                    'taker_buy_btc': bucket['buy'],
                    'taker_sell_btc': bucket['sell'],
                    'total_volume_btc': bucket['buy'] + bucket['sell'],
                    'net_aggression_btc': bucket['buy'] - bucket['sell'],
                    'trade_count': int(bucket['trades']),
                    'notional_usdt': bucket['notional'],
                    'final': True,
                    'finalized_received_ts_ms': received_ms,
                })
                del self.buckets[(bucket_seconds, bucket_start_ms)]
        return result, finalized


def parse_agg_trade(payload: dict[str, Any], received_ms: int) -> dict[str, Any]:
    maker_is_buyer = bool(payload['m'])
    return {
        'stream': 'AGGTRADE',
        'event_ts_ms': int(payload['E']),
        'trade_ts_ms': int(payload['T']),
        'received_ts_ms': received_ms,
        'event_ts': iso_ms(int(payload['E'])),
        'trade_ts': iso_ms(int(payload['T'])),
        'price': float(payload['p']),
        'quantity': float(payload['q']),
        'buyer_is_maker': maker_is_buyer,
        'aggressor_side': 'SELL' if maker_is_buyer else 'BUY',
        'aggregate_trade_id': int(payload['a']),
    }


def parse_depth_event(payload: dict[str, Any], received_ms: int, sync_epoch: int) -> dict[str, Any]:
    return {
        'stream': 'DEPTH',
        'event_ts_ms': int(payload['E']),
        'transaction_ts_ms': int(payload.get('T', payload['E'])),
        'received_ts_ms': received_ms,
        'event_ts': iso_ms(int(payload['E'])),
        'U': int(payload['U']),
        'u': int(payload['u']),
        'pu': int(payload['pu']) if payload.get('pu') is not None else None,
        'bids_json': json.dumps(payload.get('b', []), separators=(',', ':')),
        'asks_json': json.dumps(payload.get('a', []), separators=(',', ':')),
        'sync_epoch': sync_epoch,
        'sequence_ok': None,
    }


def fetch_depth_snapshot(limit: int = 1000) -> dict[str, Any]:
    query = urllib.parse.urlencode({'symbol': SYMBOL, 'limit': limit})
    request = urllib.request.Request(
        f'{REST_BASE}/fapi/v1/depth?{query}',
        headers={'User-Agent': 'BTC-LRA-ws-transport-prototype/1.0'},
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode('utf-8'))


class TransportPrototype:
    def __init__(self, root: Path, depth_limit: int = 1000) -> None:
        self.sink = JsonlSink(root)
        self.depth_limit = depth_limit
        self.depth_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.depth_snapshot_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self.agg_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.depth = DepthBook()
        self.aggregator = AggTradeAggregator()
        self.health = {
            'DEPTH': FeedState('DEPTH'),
            'AGGTRADE': FeedState('AGGTRADE'),
        }
        self.stop = asyncio.Event()
        self.depth_resync = asyncio.Event()

    async def run(self) -> None:
        await asyncio.gather(
            self.writer_loop(),
            self.depth_loop(),
            self.aggtrade_loop(),
            self.health_loop(),
        )

    async def writer_loop(self) -> None:
        while not self.stop.is_set():
            depth_batch: list[dict[str, Any]] = []
            snapshot_batch: list[dict[str, Any]] = []
            agg_batch: list[dict[str, Any]] = []
            finalized_batch: list[dict[str, Any]] = []
            for queue, stream in ((self.depth_queue, 'DEPTH'), (self.depth_snapshot_queue, 'DEPTH_SNAPSHOT'), (self.agg_queue, 'AGGTRADE')):
                target = depth_batch if stream == 'DEPTH' else agg_batch
                if stream == 'DEPTH_SNAPSHOT':
                    target = snapshot_batch
                while len(target) < 500:
                    try:
                        target.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
            if not depth_batch and not snapshot_batch and not agg_batch:
                await asyncio.sleep(0.05)
                continue
            self.sink.write_batch('DEPTH', depth_batch)
            self.sink.write_batch('DEPTH_SNAPSHOT', snapshot_batch)
            self.sink.write_batch('AGGTRADE', agg_batch)
            aggregate_rows: list[dict[str, Any]] = []
            for row in agg_batch:
                snapshots, finalized = self.aggregator.add(row, int(row['received_ts_ms']))
                aggregate_rows.extend(snapshots)
                finalized_batch.extend(finalized)
            self.sink.write_batch('AGGREGATE', aggregate_rows)
            self.sink.write_batch('FINALIZED', finalized_batch)
            await asyncio.sleep(0)

    async def depth_loop(self) -> None:
        try:
            import websockets  # type: ignore
        except ImportError:
            self.sink.health('DEPTH_DISABLED reason=missing_dependency:websockets')
            return
        backoff = 1.0
        while not self.stop.is_set():
            snapshot_task: asyncio.Task[Any] | None = None
            try:
                self.health['DEPTH'].state = 'SYNCING'
                self.health['DEPTH'].reconnect_count += 1
                self.depth.sync_epoch += 1
                epoch = self.depth.sync_epoch
                snapshot_task = asyncio.create_task(asyncio.to_thread(fetch_depth_snapshot, self.depth_limit))
                async with websockets.connect(DEPTH_WS, ping_interval=120, ping_timeout=30, close_timeout=5) as socket:
                    self.sink.health(f'WS_DEPTH_CONNECTED epoch={epoch} url={DEPTH_WS}')
                    buffered: deque[dict[str, Any]] = deque()
                    snapshot: dict[str, Any] | None = None
                    while snapshot is None:
                        if snapshot_task.done():
                            snapshot = snapshot_task.result()
                            break
                        payload = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                        if isinstance(payload, dict) and payload.get('e') == 'depthUpdate':
                            buffered.append(payload)
                    self.depth.load_snapshot(snapshot, epoch)
                    snapshot_row = {
                        'stream': 'DEPTH_SNAPSHOT',
                        'snapshot_ts_ms': epoch_ms(),
                        'received_ts_ms': epoch_ms(),
                        'sync_epoch': epoch,
                        'lastUpdateId': int(snapshot['lastUpdateId']),
                        'bids_json': json.dumps(snapshot.get('bids', []), separators=(',', ':')),
                        'asks_json': json.dumps(snapshot.get('asks', []), separators=(',', ':')),
                    }
                    try:
                        self.depth_snapshot_queue.put_nowait(snapshot_row)
                    except asyncio.QueueFull:
                        self.sink.health(f'WS_DEPTH_SNAPSHOT_QUEUE_OVERFLOW epoch={epoch}')
                        raise RuntimeError('depth snapshot queue overflow')
                    first_applied = False
                    for payload in buffered:
                        result = self.depth.apply(payload, first_after_snapshot=not first_applied)
                        if result in ('WAIT', 'STALE'):
                            continue
                        if result == 'SEQUENCE_GAP':
                            raise RuntimeError('depth sequence gap during initial sync')
                        if result == 'APPLIED':
                            first_applied = True
                            await self.enqueue_depth(payload, epoch, True)
                    self.health['DEPTH'].state = 'LIVE'
                    self.sink.health(f'WS_DEPTH_SYNCED epoch={epoch} last_update_id={self.depth.last_update_id}')
                    backoff = 1.0
                    connected_at = time.monotonic()
                    while time.monotonic() - connected_at < WS_MAX_SESSION_SECONDS:
                        payload = json.loads(await socket.recv())
                        if not isinstance(payload, dict) or payload.get('e') != 'depthUpdate':
                            continue
                        result = self.depth.apply(payload)
                        if result == 'SEQUENCE_GAP':
                            self.health['DEPTH'].gap_count += 1
                            self.sink.health(f'WS_DEPTH_SEQUENCE_GAP epoch={epoch} U={payload.get("U")} u={payload.get("u")} pu={payload.get("pu")}')
                            raise RuntimeError('depth sequence gap')
                        if result == 'APPLIED':
                            self.health['DEPTH'].last_event_ms = int(payload['E'])
                            await self.enqueue_depth(payload, epoch, True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health['DEPTH'].state = 'RESYNC'
                self.sink.health(f'WS_DEPTH_RESYNC reason={type(exc).__name__}:{exc}')
                await asyncio.sleep(backoff)
                backoff = min(RECONNECT_MAX_SECONDS, backoff * 2)
            finally:
                if snapshot_task is not None:
                    if not snapshot_task.done():
                        snapshot_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await snapshot_task

    async def enqueue_depth(self, payload: dict[str, Any], epoch: int, sequence_ok: bool) -> None:
        row = parse_depth_event(payload, epoch_ms(), epoch)
        row['sequence_ok'] = sequence_ok
        try:
            self.depth_queue.put_nowait(row)
        except asyncio.QueueFull:
            self.health['DEPTH'].gap_count += 1
            self.depth_resync.set()
            self.sink.health(f'WS_DEPTH_QUEUE_OVERFLOW epoch={epoch} action=RESYNC')
            raise RuntimeError('depth queue overflow')

    async def aggtrade_loop(self) -> None:
        try:
            import websockets  # type: ignore
        except ImportError:
            self.sink.health('AGG_CONNECTED state=DISABLED reason=missing_dependency:websockets')
            return
        backoff = 1.0
        while not self.stop.is_set():
            try:
                self.health['AGGTRADE'].state = 'CONNECTING'
                self.health['AGGTRADE'].reconnect_count += 1
                async with websockets.connect(AGGTRADE_WS, ping_interval=120, ping_timeout=30, close_timeout=5) as socket:
                    connected_at = time.monotonic()
                    self.health['AGGTRADE'].state = 'LIVE'
                    self.sink.health(f'WS_AGG_CONNECTED url={AGGTRADE_WS}')
                    backoff = 1.0
                    while time.monotonic() - connected_at < WS_MAX_SESSION_SECONDS:
                        payload = json.loads(await socket.recv())
                        if not isinstance(payload, dict) or payload.get('e') != 'aggTrade':
                            continue
                        row = parse_agg_trade(payload, epoch_ms())
                        self.health['AGGTRADE'].last_event_ms = row['event_ts_ms']
                        try:
                            self.agg_queue.put_nowait(row)
                        except asyncio.QueueFull:
                            self.health['AGGTRADE'].gap_count += 1
                            self.sink.health('WS_AGG_GAP reason=queue_overflow')
                            raise RuntimeError('aggTrade queue overflow')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health['AGGTRADE'].state = 'DEGRADED'
                self.sink.health(f'WS_AGG_DISCONNECTED reason={type(exc).__name__}:{exc}')
                await asyncio.sleep(backoff)
                backoff = min(RECONNECT_MAX_SECONDS, backoff * 2)

    async def health_loop(self) -> None:
        while not self.stop.is_set():
            now = epoch_ms()
            fields = []
            for state in self.health.values():
                age = None if state.last_event_ms is None else (now - state.last_event_ms) / 1000
                fields.append(f'{state.name.lower()}_age_sec={age if age is not None else "--"}')
                fields.append(f'{state.name.lower()}_state={state.state}')
                fields.append(f'{state.name.lower()}_reconnects={state.reconnect_count}')
                fields.append(f'{state.name.lower()}_gaps={state.gap_count}')
            self.sink.health('HEALTH ' + ' '.join(fields))
            await asyncio.sleep(30)


def self_test() -> None:
    payload = {'e': 'aggTrade', 'E': 1_000, 'T': 1_001, 'a': 7, 'p': '100.0', 'q': '2.5', 'm': True}
    trade = parse_agg_trade(payload, 1_100)
    assert trade['aggressor_side'] == 'SELL'
    assert trade['aggregate_trade_id'] == 7
    snapshots, finalized = AggTradeAggregator().add(trade, 1_100)
    assert snapshots and snapshots[0]['taker_sell_btc'] == 2.5

    aggregator = AggTradeAggregator()
    boundary_trade = dict(trade, trade_ts_ms=60_000, event_ts_ms=60_000)
    snapshots, _ = aggregator.add(boundary_trade, 60_100)
    by_timeframe = {row['bucket_seconds']: row for row in snapshots}
    assert set(by_timeframe) == {5, 30, 60}
    assert all(row['total_volume_btc'] == 2.5 and row['trade_count'] == 1 for row in by_timeframe.values())
    _, finalized = aggregator.add(dict(trade, trade_ts_ms=120_000, event_ts_ms=120_000), 120_100)
    assert {row['bucket_seconds'] for row in finalized} == {5, 30, 60}
    assert all(row['final'] is True for row in finalized)

    book = DepthBook()
    book.load_snapshot({'lastUpdateId': 10, 'bids': [['100', '1']], 'asks': [['101', '2']]}, 1)
    first = {'E': 1, 'U': 10, 'u': 11, 'pu': 9, 'b': [['100', '2']], 'a': []}
    assert book.apply(first, first_after_snapshot=True) == 'APPLIED'
    assert book.last_update_id == 11 and book.bids[100.0] == 2.0
    gap = {'E': 2, 'U': 13, 'u': 13, 'pu': 12, 'b': [], 'a': []}
    assert book.apply(gap) == 'SEQUENCE_GAP'
    assert book.state == 'RESYNC'
    assert DEPTH_WS.startswith('wss://fstream.binance.com/public/ws/')
    assert AGGTRADE_WS.startswith('wss://fstream.binance.com/market/ws/')
    stale = {'E': 0, 'U': 5, 'u': 9, 'pu': 4, 'b': [], 'a': []}
    assert book.apply(stale, first_after_snapshot=True) == 'STALE'
    applicable = {'E': 1, 'U': 11, 'u': 12, 'pu': 10, 'b': [], 'a': []}
    assert book.apply(applicable, first_after_snapshot=True) == 'APPLIED'
    assert book.apply(applicable) == 'STALE'
    jumped = {'E': 2, 'U': 15, 'u': 15, 'pu': 14, 'b': [], 'a': []}
    assert book.apply(jumped) == 'SEQUENCE_GAP'
    print('WS_TRANSPORT_SELF_TEST=PASS')
    print('AGGREGATOR_TIMEFRAME_ISOLATION=PASS')
    print('DEPTH_SEQUENCE_TEST=PASS')


async def main_async(args: argparse.Namespace) -> None:
    if args.self_test:
        self_test()
        return
    root = Path(args.output).resolve()
    prototype = TransportPrototype(root, args.depth_limit)
    try:
        await prototype.run()
    except KeyboardInterrupt:
        prototype.stop.set()


def main() -> None:
    parser = argparse.ArgumentParser(description='Diagnostic-only BTCUSDT USD-M WS transport prototype')
    parser.add_argument('--output', default='runtime/ws')
    parser.add_argument('--depth-limit', type=int, choices=(100, 500, 1000), default=1000)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == '__main__':
    main()
