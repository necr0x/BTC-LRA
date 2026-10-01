"""Read-only Russian live monitor for an existing BTC-LRA-002 runtime.

This file is deliberately a presentation layer.  It opens engine files only for
reading, keeps byte offsets in memory, and never imports or starts the engine.
"""
from __future__ import annotations

import argparse
import codecs
import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PANAMA = timezone(timedelta(hours=-5))
MAX_HISTORY_BYTES = 16 * 1024 * 1024
OI_WINDOW_MS = 60_000
ACTIVE_RELEASE_STATUSES = {"ACTIVE", "WATCHING", "OPEN"}


def configure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def number(value: Any, digits: int = 2) -> str:
    if value is None or isinstance(value, bool):
        return "—"
    try:
        return f"{float(value):,.{digits}f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def ts_ms(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def clock(value: Any = None) -> str:
    stamp = ts_ms(value) if value is not None else int(time.time() * 1000)
    if stamp is None:
        return "??:??:??"
    return datetime.fromtimestamp(stamp / 1000, tz=PANAMA).strftime("%H:%M:%S / %d.%m.%y -5")


def direction(record: dict[str, Any]) -> str | None:
    for key in ("direction", "side", "candidate_side", "old_side", "new_side"):
        value = record.get(key)
        if value in ("BUY", "SELL"):
            return value
    return None


def display_direction(record: dict[str, Any], fallback: str = "") -> str:
    return direction(record) or fallback or "сторона не указана"


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        return None


def parse_line(raw: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def record_ts(record: dict[str, Any]) -> int:
    return ts_ms(record.get("time_ts") or record.get("sample_time_ts") or record.get("ts")) or 0


def price_of(record: dict[str, Any]) -> float | None:
    for key in ("current_price", "price", "close", "bar_close", "last_price", "base_price"):
        value = record.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    for nested in (record.get("bar"), record.get("push"), record.get("active_extreme")):
        if isinstance(nested, dict):
            result = price_of(nested)
            if result is not None:
                return result
    return None


def event_header(record: dict[str, Any], kind: str, fallback_price: float | None = None) -> str:
    price = record.get("new_extreme") if isinstance(record.get("new_extreme"), (int, float)) else price_of(record)
    price = price if isinstance(price, (int, float)) else fallback_price
    context = "HIGH" if kind in {"NEW_EXTREME_WITHOUT_RETENTION", "PASSIVE_REJECTION_EXIT_WARNING"} and direction(record) == "BUY" else "LOW" if kind in {"NEW_EXTREME_WITHOUT_RETENTION", "PASSIVE_REJECTION_EXIT_WARNING"} and direction(record) == "SELL" else "PRICE"
    return f"{context} {number(price, 2)} / {clock(record_ts(record))}"


def current_header(price: float | None, ts: int | None = None) -> str:
    return f"{number(price, 1)} / {clock(ts)}"


class TailFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.pending = b""

    def history(self, cutoff_ms: int) -> list[dict[str, Any]]:
        try:
            size = self.path.stat().st_size
            start = max(0, size - MAX_HISTORY_BYTES)
            with self.path.open("rb") as handle:
                handle.seek(start)
                data = handle.read()
            if start:
                data = data.split(b"\n", 1)[1] if b"\n" in data else b""
            rows = [row for line in data.splitlines() if (row := parse_line(line)) is not None]
            self.offset = size
            self.pending = b""
            return [row for row in rows if record_ts(row) >= cutoff_ms]
        except (FileNotFoundError, PermissionError, OSError):
            return []

    def read_new(self) -> list[dict[str, Any]]:
        try:
            size = self.path.stat().st_size
            if size < self.offset:
                self.offset = 0
                self.pending = b""
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
            self.offset += len(data)
            if not data:
                return []
            data = self.pending + data
            lines = data.split(b"\n")
            self.pending = lines.pop() or b""
            return [row for line in lines if (row := parse_line(line)) is not None]
        except (FileNotFoundError, PermissionError, OSError):
            return []


class LiveMonitor:
    def __init__(self, runtime_root: Path, history_minutes: int, heartbeat_seconds: int, no_heartbeat: bool) -> None:
        self.root = runtime_root
        self.history_minutes = max(0, history_minutes)
        self.heartbeat_seconds = max(5, heartbeat_seconds)
        self.no_heartbeat = no_heartbeat
        events = self.root / "events"
        state = self.root / "state"
        self.streams = {
            "events": TailFile(events / "BTC_LRA_002_EVENTS.jsonl"),
            "battles": TailFile(events / "BTC_LRA_002_BATTLES.jsonl"),
            "releases": TailFile(events / "BTC_LRA_002_RELEASES.jsonl"),
            "oi": TailFile(events / "BTC_LRA_002_OI_SAMPLES.jsonl"),
        }
        self.state_path = state / "BTC_LRA_002_STATE.json"
        self.zone_state_path = state / "BTC_LRA_002_ZONE_STATE.json"
        self.state: dict[str, Any] = {}
        self.zones: dict[str, dict[str, Any]] = {}
        self.battles: dict[str, dict[str, Any]] = {}
        self.releases: dict[str, dict[str, Any]] = {}
        self.battle_view: dict[str, tuple[Any, ...]] = {}
        self.battle_latest: dict[str, dict[str, Any]] = {}
        self.release_view: dict[str, Any] = {}
        self.oi: deque[dict[str, Any]] = deque(maxlen=900)
        self.latest_price: float | None = None
        self.price_by_ts: dict[int, float] = {}
        self.pending_transfer_groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.last_state_check = 0.0
        self.last_heartbeat = 0.0

    def load_state(self) -> None:
        state = read_json(self.state_path)
        zone_state = read_json(self.zone_state_path)
        if state is not None:
            self.state = state
            self.battles = {str(k): v for k, v in state.get("battles", {}).items() if isinstance(v, dict)}
            self.releases = {str(k): v for k, v in state.get("releases", {}).items() if isinstance(v, dict)}
            recent_bars = state.get("recent_bars", [])
            if recent_bars:
                self.latest_price = price_of(recent_bars[-1])
        if zone_state is not None:
            self.zones = {str(z.get("zone_id")): z for z in zone_state.get("zones", []) if isinstance(z, dict) and z.get("zone_id")}
        for key, value in self.state.get("zones", {}).items():
            if isinstance(value, dict):
                self.zones.setdefault(str(key), value)
        self.last_state_check = time.monotonic()

    def oi_delta(self, start_ts: int | None = None) -> tuple[float | None, float | None, str]:
        samples = list(self.oi)
        if not samples:
            return None, None, "нет данных"
        current = samples[-1].get("OI_BTC")
        if not isinstance(current, (int, float)):
            return None, None, "нет данных"
        if start_ts is None:
            start_value = samples[0].get("OI_BTC")
        else:
            before = [x for x in samples if (ts_ms(x.get("sample_time_ts")) or 0) <= start_ts]
            start_value = (before[-1] if before else samples[0]).get("OI_BTC")
        recent_cutoff = (ts_ms(samples[-1].get("sample_time_ts")) or 0) - OI_WINDOW_MS
        recent = [x for x in samples if (ts_ms(x.get("sample_time_ts")) or 0) <= recent_cutoff]
        recent_value = (recent[-1] if recent else samples[0]).get("OI_BTC")
        since_start = float(current - start_value) if isinstance(start_value, (int, float)) else None
        last_60 = float(current - recent_value) if isinstance(recent_value, (int, float)) else None
        return since_start, last_60, self.oi_direction(last_60)

    @staticmethod
    def oi_direction(delta: float | None) -> str:
        if delta is None:
            return "нет данных"
        rounded = round(delta, 1)
        if rounded == 0:
            return "СТАБИЛЕН"
        return "НАРАСТАЕТ" if rounded > 0 else "СОКРАЩАЕТСЯ"

    def oi_text(self, start_ts: int | None = None) -> list[str]:
        since_start, last_60, trend = self.oi_delta(start_ts)
        lines = []
        if start_ts is not None:
            lines.append(f"ОИ от начала борьбы {since_start:+.1f} BTC" if since_start is not None else "ОИ от начала борьбы нет данных")
        lines.append(f"ОИ за последние 60 сек {last_60:+.1f} BTC" if last_60 is not None else "ОИ за последние 60 сек нет данных")
        lines.append(f"ОИ {trend}")
        return lines

    @staticmethod
    def transfer_sides(record: dict[str, Any]) -> tuple[Any, Any]:
        evidence = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
        return record.get("side_a", evidence.get("side_a")), record.get("side_b", evidence.get("side_b"))

    def queue_transfer_candidate(self, record: dict[str, Any]) -> None:
        side_a, side_b = self.transfer_sides(record)
        timestamp = record_ts(record)
        explicit_price = price_of(record)
        price = explicit_price if explicit_price is not None else self.price_by_ts.get(timestamp, self.latest_price)
        if timestamp not in self.price_by_ts and isinstance(price, (int, float)):
            self.price_by_ts[timestamp] = float(price)
        price_key = round(float(price), 8) if isinstance(price, (int, float)) else None
        key = (timestamp, side_a, side_b, price_key)
        group = self.pending_transfer_groups.setdefault(key, {"time_ts": timestamp, "price": price, "side_a": side_a, "side_b": side_b, "records": []})
        group["records"].append(record)

    def event_price(self, record: dict[str, Any]) -> float | None:
        value = record.get("new_extreme")
        if isinstance(value, (int, float)):
            return float(value)
        return price_of(record) or self.latest_price

    def event_context(self, kind: str, record: dict[str, Any]) -> str:
        if kind in {"NEW_EXTREME_WITHOUT_RETENTION", "PASSIVE_REJECTION_EXIT_WARNING", "RETAINED_PUSH", "ORIGINAL_SIDE_RESTORED"}:
            side = direction(record)
            if side == "BUY":
                return "HIGH"
            if side == "SELL":
                return "LOW"
        return "PRICE"

    def position_text(self, price: float | None, zone: dict[str, Any] | None) -> str:
        low, high = (zone or {}).get("low"), (zone or {}).get("high")
        if not isinstance(price, (int, float)) or not isinstance(low, (int, float)) or not isinstance(high, (int, float)) or high <= low:
            return "ПОЛОЖЕНИЕ НЕДОСТУПНО"
        if price < low:
            return "НИЖЕ НИЖНЕЙ ГРАНИЦЫ"
        if price > high:
            return "ВЫШЕ ВЕРХНЕЙ ГРАНИЦЫ"
        position_pct = (price - low) / (high - low) * 100
        rounded_pct = round(position_pct)
        if rounded_pct == 50:
            return "СЕРЕДИНА ЗОНЫ"
        if position_pct < 50:
            return f"{rounded_pct}% ОТ НИЖНЕЙ ГРАНИЦЫ"
        return f"{100 - rounded_pct}% ДО ВЕРХНЕЙ ГРАНИЦЫ"

    def print_event_block(self, record: dict[str, Any], description: str, event_price: float | None = None,
                          context: str | None = None, extra: list[str] | None = None) -> None:
        price = event_price if event_price is not None else self.event_price(record)
        zone = self.zones.get(str(record.get("zone_id"))) if record.get("zone_id") else None
        if zone and isinstance(zone.get("low"), (int, float)) and isinstance(zone.get("high"), (int, float)):
            first = f"{zone.get('timeframe', '?')} / {number(zone['low'], 1)} – {number(zone['high'], 1)} / {clock(record_ts(record))}"
        else:
            first = f"— / — / {clock(record_ts(record))}"
        print()
        print(first)
        print(description)
        print(f"{number(price, 2)} {context or 'PRICE'} / {self.position_text(price, zone)}")
        if extra:
            print(*extra, sep="\n")

    def print_battle_metrics(self, record: dict[str, Any], current_price: float | None = None) -> None:
        battle_id = record.get("battle_id")
        battle = self.battles.get(str(battle_id), record)
        if battle_id and str(battle_id) in self.battle_latest:
            merged = dict(battle)
            merged.update(self.battle_latest[str(battle_id)])
            battle = merged
        display_price = current_price if isinstance(current_price, (int, float)) else price_of(record) or price_of(battle)
        print(f"Цена: {number(display_price, 1)}")
        print(f"Усилие BUY:  {number(battle.get('buy_effort'), 1)} BTC")
        print(f"Усилие SELL: {number(battle.get('sell_effort'), 1)} BTC")
        print(f"Результат BUY:  {number(battle.get('buy_result'), 1)} USD")
        print(f"Результат SELL: {number(battle.get('sell_result'), 1)} USD")
        print(f"Лидер: {battle.get('leader') or record.get('leader') or '—'}")
        print(f"Кандидат: {battle.get('candidate_side') or record.get('candidate_side') or '—'} ({battle.get('candidate_status') or record.get('candidate_status') or '—'})")
        print(*self.oi_text(ts_ms(battle.get("start_ts"))), sep="\n")

    def emit(self, record: dict[str, Any], historical: bool = False, quiet: bool = False) -> None:
        kind = record.get("event") or record.get("record_type")
        if not kind:
            return
        self.latest_price = price_of(record) or self.latest_price
        if kind == "BATTLE_BAR":
            battle_id = str(record.get("battle_id", ""))
            signature = tuple(record.get(key) for key in ("leader", "candidate_side", "candidate_status", "state"))
            previous = self.battle_view.get(battle_id)
            self.battle_view[battle_id] = signature
            self.battle_latest[battle_id] = record
            if quiet or previous is None or previous == signature:
                return
            description = (f"ЛИДЕР СМЕНИЛСЯ: {previous[0] or '—'} → {signature[0] or '—'}"
                           if previous[0] != signature[0]
                           else f"Состояние кандидата изменилось: {signature[1] or 'нет кандидата'} / {signature[2] or '—'}")
            self.print_event_block(record, description, price_of(record), "PRICE")
            self.print_battle_metrics(record)
            return
        if kind == "TRANSFER_CANDIDATE":
            if not quiet:
                self.queue_transfer_candidate(record)
            return
        if quiet:
            if kind == "RELEASE_STATUS":
                release_id = str(record.get("release_id", ""))
                self.release_view[release_id] = record.get("status") or record.get("new_status") or "—"
            return
        event_price = self.event_price(record)
        context = self.event_context(kind, record)
        if kind == "BATTLE_STARTED":
            self.print_event_block(record, f"НАЧАЛАСЬ БОРЬБА {display_direction(record)}", event_price, context)
            self.print_battle_metrics(record)
        elif kind == "BATTLE_RESOLUTION_CANDIDATE":
            self.print_event_block(record, f"ПОЯВИЛСЯ ПЕРЕВЕС {display_direction(record)}", event_price, context)
            self.print_battle_metrics(record)
        elif kind == "TRANSFER_CHALLENGED":
            self.print_event_block(record, "ПЕРЕДАЧА КОНТРОЛЯ ОСПОРЕНА", event_price, context)
        elif kind == "OLD_SIDE_RESTORED":
            self.print_event_block(record, "ПРЕЖНЯЯ СТОРОНА ВОССТАНОВИЛАСЬ", event_price, context)
        elif kind == "BATTLE_RESOLUTION_HOLDING":
            self.print_event_block(record, f"{display_direction(record)} ВЫИГРАЛ ЛОКАЛЬНУЮ БОРЬБУ", event_price, context)
            self.print_battle_metrics(record)
        elif kind == "BATTLE_ARCHIVED":
            self.print_event_block(record, f"БОРЬБА ЗАКРЫТА: статус={record.get('status') or record.get('state') or '—'}, причина={record.get('reason') or 'не указана'}", event_price, context)
        elif kind == "RETAINED_PUSH":
            self.print_event_block(record, f"ДВИЖЕНИЕ ПОСЛЕ ПОБЕДЫ ПРОДОЛЖАЕТСЯ ({display_direction(record)})", event_price, context)
        elif kind == "PULLBACK_OBSERVATION":
            self.print_event_block(record, "НАЧАЛСЯ ОТКАТ", event_price, context)
        elif kind == "ORIGINAL_SIDE_RESTORED":
            self.print_event_block(record, "ИСХОДНАЯ СТОРОНА ВОССТАНОВИЛА ДВИЖЕНИЕ", event_price, context)
        elif kind == "NEW_EXTREME_WITHOUT_RETENTION":
            self.print_event_block(record, "НОВЫЙ ЭКСТРЕМУМ НЕ УДЕРЖАН", event_price, context)
        elif kind == "PASSIVE_REJECTION_EXIT_WARNING":
            self.print_event_block(record, "ЭФФЕКТИВНОСТЬ УХУДШАЕТСЯ / ВОЗМОЖНОЕ ПОГЛОЩЕНИЕ", event_price, context)
        elif kind == "OPPOSITE_CONTROL_CANDIDATE":
            self.print_event_block(record, "ПРОТИВОПОЛОЖНАЯ СТОРОНА ПОЛУЧИЛА КОНТРОЛЬ — КАНДИДАТ", event_price, context)
        elif kind == "RELEASE_STATUS":
            release_id = str(record.get("release_id", ""))
            status = record.get("status") or record.get("new_status") or "—"
            if self.release_view.get(release_id) == status:
                return
            self.release_view[release_id] = status
            self.print_event_block(record, f"RELEASE: статус → {status}", event_price, context)
        elif kind == "RELEASE_ARCHIVED":
            self.print_event_block(record, f"RELEASE ЗАКРЫТ: причина={record.get('reason') or 'не указана'}", event_price, context)
        elif kind == "ATTEMPT_EPISODE_ARCHIVED":
            result = record.get("result") or record.get("status") or record.get("outcome") or "результат не указан"
            self.print_event_block(record, f"ПОПЫТКА ЗАВЕРШЕНА: {result}", event_price, context)

    def flush_transfer_candidates(self) -> None:
        for group in self.pending_transfer_groups.values():
            records = group["records"]
            if not records:
                continue
            tf_counts: dict[str, int] = {}
            lows: list[float] = []
            highs: list[float] = []
            for record in records:
                zone = self.zones.get(str(record.get("zone_id")), {})
                timeframe = zone.get("timeframe")
                if timeframe:
                    tf_counts[timeframe] = tf_counts.get(timeframe, 0) + 1
                if isinstance(zone.get("low"), (int, float)):
                    lows.append(float(zone["low"]))
                if isinstance(zone.get("high"), (int, float)):
                    highs.append(float(zone["high"]))
            order = {"5m": 0, "15m": 1, "1h": 2, "4h": 3}
            tf_text = " / ".join(f"{tf} ×{count}" for tf, count in sorted(tf_counts.items(), key=lambda item: order.get(item[0], 99)))
            price = group["price"]
            if len(records) == 1:
                zone = self.zones.get(str(records[0].get("zone_id")), {})
                first = f"{zone.get('timeframe', '?')} / {number(zone.get('low'), 1)} – {number(zone.get('high'), 1)} / {clock(group['time_ts'])}"
                position = self.position_text(price, zone)
            elif lows and highs:
                coverage = {"low": min(lows), "high": max(highs)}
                first = f"{tf_text or '?'} / {number(coverage['low'], 1)} – {number(coverage['high'], 1)} / {clock(group['time_ts'])}"
                position = self.position_text(price, coverage).replace("ГРАНИЦЫ", "ГРАНИЦЫ ОХВАТА")
            else:
                first = f"{tf_text or '?'} / — – — / {clock(group['time_ts'])}"
                position = "ПОЛОЖЕНИЕ НЕДОСТУПНО"
            print()
            print(first)
            print(f"ВОЗМОЖНАЯ ПЕРЕДАЧА КОНТРОЛЯ {group['side_a']} → {group['side_b']}")
            print(f"{number(price, 2)} PRICE / {position}")
            if len(records) > 1:
                print(f"Представлений {len(records)}")
                if tf_text:
                    print(f"TF {tf_text}")
                if lows and highs:
                    print(f"Охват зон {min(lows):.1f}–{max(highs):.1f}")
        self.pending_transfer_groups.clear()

    def handle_oi(self, record: dict[str, Any]) -> None:
        if not isinstance(record.get("OI_BTC"), (int, float)):
            return
        before = self.oi_delta()[2]
        self.oi.append(record)
        after = self.oi_delta()[2]
        if before != after and before != "нет данных":
            delta = self.oi_delta()[1]
            if delta is None:
                return
            if after == "СТАБИЛЕН":
                label = f"ОИ СТАБИЛЕН / изменение за 60 сек {delta:+.1f} BTC"
            else:
                label = f"ОИ {after} {delta:+.1f} BTC / 60 сек"
            print()
            print(f"{current_header(self.latest_price, record_ts(record))}\n{label}")

    def current_context_battle(self, active_battles: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not isinstance(self.latest_price, (int, float)):
            return None
        candidates = []
        for battle in active_battles:
            zone = self.zones.get(str(battle.get("zone_id")))
            low, high = (zone or {}).get("low"), (zone or {}).get("high")
            if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
                continue
            if low <= self.latest_price <= high:
                width = max(float(high - low), 0.0)
                candidates.append((width, -(battle.get("start_ts") or 0), battle))
        return min(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None

    def current_snapshot(self) -> None:
        active_ids = {str(value) for value in self.state.get("active_battle_by_zone", {}).values() if value}
        active_battles = [self.battles[key] for key in active_ids if key in self.battles]
        active_releases = [x for x in self.releases.values() if x.get("status") in ACTIVE_RELEASE_STATUSES]
        print(f"{current_header(self.latest_price)}\nТЕКУЩЕЕ СОСТОЯНИЕ")
        print(f"Активных представлений борьбы: {len(active_ids)}")
        print(f"Активных release-представлений: {len(active_releases)}")
        battle = self.current_context_battle(active_battles)
        if battle is not None:
            current_context = dict(battle)
            current_context["time_ts"] = int(time.time() * 1000)
            self.print_event_block(current_context, "ТЕКУЩАЯ БОРЬБА", self.latest_price, "PRICE")
            self.print_battle_metrics(battle, self.latest_price)
        else:
            print("Явной текущей борьбы у цены сейчас нет")

    def startup(self) -> None:
        self.load_state()
        cutoff = int(time.time() * 1000) - self.history_minutes * 60_000
        history: list[dict[str, Any]] = []
        for name, stream in self.streams.items():
            rows = stream.history(cutoff)
            if name == "oi":
                self.oi.extend(sorted(rows, key=record_ts))
            else:
                history.extend(rows)
        print(f"BTC-LRA-002 LIVE MONITOR | только чтение | runtime: {self.root}")
        print(f"История: последние {self.history_minutes} минут; engine не изменяется")
        for row in sorted(history, key=record_ts):
            self.emit(row, historical=True, quiet=True)
        print(f"Контекст последних {self.history_minutes} минут восстановлен без печати каждой строки.")
        self.current_snapshot()
        self.last_heartbeat = time.monotonic()

    def run(self) -> None:
        self.startup()
        while True:
            for name, stream in self.streams.items():
                rows = stream.read_new()
                if name == "oi":
                    for row in rows:
                        self.handle_oi(row)
                else:
                    for row in rows:
                        self.emit(row)
            self.flush_transfer_candidates()
            now = time.monotonic()
            if now - self.last_state_check >= 10:
                self.load_state()
            if not self.no_heartbeat and now - self.last_heartbeat >= self.heartbeat_seconds:
                self.current_snapshot()
                self.last_heartbeat = now
            time.sleep(1.0)


def main() -> None:
    configure_stdout()
    parser = argparse.ArgumentParser(description="Read-only Russian BTC-LRA-002 live monitor")
    parser.add_argument("--runtime-root", type=Path, default=Path(__file__).resolve().parent / "runtime")
    parser.add_argument("--history-minutes", type=int, default=60)
    parser.add_argument("--heartbeat-seconds", type=int, default=60)
    parser.add_argument("--no-heartbeat", action="store_true")
    args = parser.parse_args()
    LiveMonitor(args.runtime_root.resolve(), args.history_minutes, args.heartbeat_seconds, args.no_heartbeat).run()


if __name__ == "__main__":
    main()
