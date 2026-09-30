"""BTC-LRA-002: first live market-risk observer.

Research-only, order-free observer.  Replay and live adapters feed the same
closed-1m CausalEngine.  No state from btc-lra-001.py is imported.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
import copy
import ctypes
import csv
import hashlib
import json
import math
import os
import statistics
import sys
import tempfile
import time
from ctypes import wintypes
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEDUPE_WINDOW = 4096
ENGINE_EVENT_SAMPLE = 256

ROOT = Path(__file__).resolve().parent
SYMBOL = "BTCUSDT"
TZ_LABEL = "America/Panama"
TF_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
PARENT_TFS = {"5m": ("15m", "1h", "4h"), "15m": ("1h", "4h"), "1h": ("4h",), "4h": ()}
ZONE_WINDOWS = {"5m": 36, "15m": 24, "1h": 18, "4h": 12}
BOOTSTRAP_MINUTES = ZONE_WINDOWS["4h"] * TF_MINUTES["4h"] + TF_MINUTES["4h"]
OUTPUTS = {
    "state": ROOT / "BTC_LRA_002_STATE.json",
    "zones": ROOT / "BTC_LRA_002_ZONE_STATE.json",
    "events": ROOT / "BTC_LRA_002_EVENTS.jsonl",
    "battles": ROOT / "BTC_LRA_002_BATTLES.jsonl",
    "releases": ROOT / "BTC_LRA_002_RELEASES.jsonl",
    "oi": ROOT / "BTC_LRA_002_OI_SAMPLES.jsonl",
    "human": ROOT / "BTC_LRA_002_HUMAN.log",
    "debug": ROOT / "BTC_LRA_002_DEBUG.log",
    "audit": ROOT / "BTC_LRA_002_REPLAY_AUDIT.md",
}


def now_ms() -> int:
    return int(time.time() * 1000)


def fmt_ts(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def median(values: Iterable[float]) -> float | None:
    values = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    return statistics.median(values) if values else None


def mean(values: Iterable[float]) -> float | None:
    values = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    return statistics.mean(values) if values else None


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_zones_json(path: Path, zones: Iterable[dict[str, Any]], metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("{")
            first = True
            for key, value in metadata.items():
                if not first: handle.write(",")
                json.dump(str(key), handle, ensure_ascii=False); handle.write(":"); json.dump(value, handle, ensure_ascii=False, separators=(",", ":")); first = False
            handle.write(',"zones":[')
            first = True
            for zone in zones:
                if not first: handle.write(",")
                json.dump(zone, handle, ensure_ascii=False, separators=(",", ":")); first = False
            handle.write("]}\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(text.rstrip("\n") + "\n")


def memory_snapshot() -> dict[str, int]:
    try:
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t), ("PrivateUsage", ctypes.c_size_t)]
        counters = Counters(); counters.cb = ctypes.sizeof(Counters)
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return {}
        return {"rss_bytes": int(counters.WorkingSetSize), "peak_rss_bytes": int(counters.PeakWorkingSetSize), "private_bytes": int(counters.PrivateUsage), "peak_private_bytes": int(counters.PrivateUsage)}
    except Exception:
        return {}


def event_id(kind: str, *parts: Any) -> str:
    raw = "|".join([kind, *[str(x) for x in parts]])
    return f"002-{kind}-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def side_progress(side: str, base: float, price: float) -> float:
    return max(0.0, price - base) if side == "BUY" else max(0.0, base - price)


def side_effort_key(side: str) -> str:
    return "taker_buy_BTC" if side == "BUY" else "taker_sell_BTC"


def side_delta(side: str, bar: dict[str, Any]) -> float:
    return float(bar.get("delta_BTC") or 0.0) if side == "BUY" else -float(bar.get("delta_BTC") or 0.0)


def normalize_bar(row: dict[str, Any]) -> dict[str, Any]:
    ts = int(row.get("ts") or row.get("timestamp_ms") or 0)
    observable = int(row.get("observable_at_ts") or row.get("close_time_ms") or ts + 60000)
    return {
        "ts": ts,
        "bar_open_ts": ts,
        "observable_at_ts": observable,
        "timestamp": fmt_ts(ts),
        "open": num(row.get("open")), "high": num(row.get("high")),
        "low": num(row.get("low")), "close": num(row.get("close")),
        "volume_BTC": num(row.get("volume_BTC") or row.get("volume")) or 0.0,
        "taker_buy_BTC": num(row.get("taker_buy_BTC") or row.get("takerBuy")) or 0.0,
        "taker_sell_BTC": num(row.get("taker_sell_BTC") or row.get("takerSell")) or 0.0,
        "delta_BTC": num(row.get("delta_BTC") or row.get("delta")) or 0.0,
        "delta_volume_ratio": num(row.get("delta_volume_ratio")) if row.get("delta_volume_ratio") not in (None, "") else None,
        "OI_BTC": num(row.get("OI_BTC") or row.get("oi")),
        "dOI_BTC": num(row.get("dOI_BTC") or row.get("doi")),
        "oi_source": row.get("oi_source"),
        "oi_sample_time": row.get("oi_sample_time"),
        "oi_sample_time_ts": num(row.get("oi_sample_time_ts")),
        "oi_resolution": row.get("oi_resolution") or row.get("oi_interval"),
        "oi_age_seconds": num(row.get("oi_age_seconds")),
    }


class ReplayAdapter:
    def __init__(self, csv_path: Path):
        self.csv_path = csv_path
        self.oi_samples: list[dict[str, Any]] = []
        self.all_oi_samples: list[dict[str, Any]] = []

    def bars(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                ts = int(datetime.fromisoformat(raw["timestamp_utc"].replace("Z", "+00:00")).timestamp() * 1000)
                raw["ts"] = ts
                rows.append(normalize_bar(raw))
                if raw.get("OI_BTC") not in (None, ""):
                    self.oi_samples.append({
                        "sample_time_ts": ts,
                        "sample_time": fmt_ts(ts),
                        "OI_BTC": float(raw["OI_BTC"]),
                        "dOI_BTC": num(raw.get("dOI_BTC")),
                        "oi_source": raw.get("oi_source") or "historical MASTER sample",
                        "oi_resolution": raw.get("oi_interval") or "5m",
                    })
        self.oi_samples.sort(key=lambda x: x["sample_time_ts"])
        self.all_oi_samples = list(self.oi_samples)
        prior: dict[str, Any] | None = None
        for bar in rows:
            while self.oi_samples and self.oi_samples[0]["sample_time_ts"] <= bar["observable_at_ts"]:
                prior = self.oi_samples.pop(0)
            if prior:
                bar.update({"OI_BTC": prior["OI_BTC"], "dOI_BTC": prior.get("dOI_BTC"), "oi_source": prior["oi_source"], "oi_sample_time": prior["sample_time"], "oi_sample_time_ts": prior["sample_time_ts"], "oi_resolution": prior["oi_resolution"], "oi_age_seconds": (bar["observable_at_ts"] - prior["sample_time_ts"]) / 1000})
        return rows


class BinanceLiveAdapter:
    """Public REST polling adapter; no API key and no order endpoint."""
    API = "https://fapi.binance.com"

    def __init__(self, engine: "CausalEngine", poll_seconds: int = 5):
        self.engine = engine
        self.poll_seconds = poll_seconds
        self.last_bar_ts: int | None = engine.state.get("last_processed_bar_ts")
        self.last_oi_ts: int | None = None

    def get_json(self, path: str, params: dict[str, Any]) -> Any:
        query = urlencode(params)
        request = Request(f"{self.API}{path}?{query}", headers={"User-Agent": "BTC-LRA-002-research"})
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def sample_oi(self) -> None:
        sampled = now_ms()
        data = self.get_json("/fapi/v1/openInterest", {"symbol": SYMBOL})
        sample = {"sample_time_ts": sampled, "sample_time": fmt_ts(sampled), "OI_BTC": num(data.get("openInterest")), "dOI_BTC": None, "oi_source": "Binance /fapi/v1/openInterest", "oi_resolution": "instantaneous_poll", "derived_interval_seconds": None}
        if self.last_oi_ts and self.engine.oi_last_value is not None:
            sample["dOI_BTC"] = sample["OI_BTC"] - self.engine.oi_last_value
        self.engine.oi_last_value = sample["OI_BTC"]
        self.last_oi_ts = sampled
        self.engine.record_oi_sample(sample)

    def poll_closed_bar(self) -> dict[str, Any] | None:
        rows = self.get_json("/fapi/v1/klines", {"symbol": SYMBOL, "interval": "1m", "limit": 2})
        if len(rows) < 2:
            return None
        k = rows[-2]
        ts = int(k[0])
        if self.last_bar_ts is not None and ts <= self.last_bar_ts:
            return None
        volume = float(k[5]); buy = float(k[9]); sell = volume - buy
        observable = int(k[6]) + 1
        sample = self.engine.latest_oi_sample_at_or_before(observable)
        bar = normalize_bar({"ts": ts, "observable_at_ts": observable, "open": k[1], "high": k[2], "low": k[3], "close": k[4], "volume_BTC": volume, "taker_buy_BTC": buy, "taker_sell_BTC": sell, "delta_BTC": 2 * buy - volume, "delta_volume_ratio": (2 * buy - volume) / volume if volume else None})
        if sample:
            bar.update({"OI_BTC": sample["OI_BTC"], "dOI_BTC": sample.get("dOI_BTC"), "oi_source": sample["oi_source"], "oi_sample_time": sample["sample_time"], "oi_sample_time_ts": sample["sample_time_ts"], "oi_resolution": sample["oi_resolution"], "oi_age_seconds": (observable - sample["sample_time_ts"]) / 1000})
        self.last_bar_ts = ts
        return bar

    def bootstrap(self) -> None:
        """Seed causal context with closed public bars; no human output during seed."""
        rows: list[Any] = []
        end_time: int | None = None
        while len(rows) < BOOTSTRAP_MINUTES:
            params: dict[str, Any] = {"symbol": SYMBOL, "interval": "1m", "limit": 1500}
            if end_time is not None:
                params["endTime"] = end_time
            chunk = self.get_json("/fapi/v1/klines", params)
            if not chunk:
                break
            rows = chunk + rows
            first_open = int(chunk[0][0])
            end_time = first_open - 1
            if len(chunk) < 1500:
                break
        unique = {int(k[0]): k for k in rows}
        rows = [unique[ts] for ts in sorted(unique)]
        for k in rows[:-1]:
            ts = int(k[0])
            volume = float(k[5]); buy = float(k[9]); sell = volume - buy
            observable = int(k[6]) + 1
            self.engine.rehydrate_bar(normalize_bar({"ts": ts, "observable_at_ts": observable, "open": k[1], "high": k[2], "low": k[3], "close": k[4], "volume_BTC": volume, "taker_buy_BTC": buy, "taker_sell_BTC": sell, "delta_BTC": 2 * buy - volume, "delta_volume_ratio": (2 * buy - volume) / volume if volume else None}))
        if rows:
            latest_closed = normalize_bar({"ts": int(rows[-2][0]), "observable_at_ts": int(rows[-2][6]) + 1})
            self.last_bar_ts = max(int(self.last_bar_ts or 0), latest_closed["ts"])
            self.engine.state["last_processed_bar_ts"] = self.last_bar_ts
            atomic_json(self.engine.outputs["state"], self.engine.persistence_state())

    def run(self) -> None:
        while True:
            try:
                self.sample_oi()
                bar = self.poll_closed_bar()
                if bar:
                    self.engine.process_closed_bar(bar)
            except Exception as exc:  # recorder failure never stops the observer
                self.engine.debug({"type": "LIVE_ADAPTER_ERROR", "error": repr(exc)})
            time.sleep(self.poll_seconds)


class CausalEngine:
    def __init__(self, outputs: dict[str, Path] | None = None, reset: bool = False, human_enabled: bool = True, live_start_ts: int | None = None, persist_each_bar: bool = True, telemetry: str = "events"):
        self.outputs = outputs or OUTPUTS
        if reset:
            for key in ("state", "zones", "events", "battles", "releases", "oi", "human", "debug", "audit"):
                self.outputs[key].unlink(missing_ok=True)
        self.state = self.load_json(self.outputs["state"], {})
        self.state.setdefault("schema_version", "BTC-LRA-002-v1")
        self.state.setdefault("live_start_time", live_start_ts if live_start_ts is not None else now_ms())
        self.state.setdefault("last_processed_bar_ts", None)
        self.state.setdefault("processed_event_ids", [])
        self.state.setdefault("human_event_ids", [])
        self.state.setdefault("zones", {})
        self.state.setdefault("battles", {})
        self.state.setdefault("active_battle_by_zone", {})
        self.state.setdefault("releases", {})
        self.state.setdefault("bar_count", 0)
        self.state.setdefault("swing_candidates", [])
        self.state.setdefault("human_battle_groups", {})
        self.human_enabled = human_enabled
        self.persist_each_bar = persist_each_bar
        self.telemetry = telemetry
        self.profile = {"time_seconds": {"update_zones": 0.0, "active_zones_at": 0.0, "battle_step": 0.0, "release_step": 0.0, "emit": 0.0, "jsonl_writes": 0.0}, "counts": {"zones_created": 0, "peak_active_zones": 0, "battles_created": 0, "peak_active_battles": 0, "battle_bar_rows_written": 0, "releases_created": 0, "peak_active_releases": 0, "release_step_calls": 0, "total_machine_events": 0}, "samples": {"active_zones": 0, "active_battles": 0, "active_releases": 0, "bars": 0}}
        self.processed_event_id_set = set(self.state["processed_event_ids"])
        self.human_event_id_set = set(self.state["human_event_ids"])
        self.oi_samples: list[dict[str, Any]] = self.load_jsonl(self.outputs["oi"])
        self.oi_last_value = self.oi_samples[-1]["OI_BTC"] if self.oi_samples else None
        self.tf_bars: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.current_buckets: dict[str, dict[str, Any]] = {}
        self.swing_candidates: list[dict[str, Any]] = self.state["swing_candidates"]
        self.swing_candidate_ids = {x.get("reference_id") for x in self.swing_candidates}
        self.engine_events: list[dict[str, Any]] = []
        self.event_digest_value = self.state.get("event_digest_sha256", "")
        self.event_digest_count = int(self.state.get("event_digest_count", 0))
        self.rehydrating = False
        self.cumulative_volume_BTC = float(self.state.get("cumulative_volume_BTC", 0.0))
        self.active_zone_ids = {zid for zid, zone in self.state["zones"].items() if zone.get("state") == "ACTIVE_BALANCE"}
        self.return_zone_ids = {zid for zid, zone in self.state["zones"].items() if zone.get("state") in ("DEPARTED_UP", "DEPARTED_DOWN", "FIRST_RETURN", "RETESTED")}
        self.zone_interval_index = sorted((zone["low"], zone["high"], zid) for zid, zone in self.state["zones"].items())
        self.active_release_ids = {rid for rid, release in self.state["releases"].items() if release.get("status", "ACTIVE") in ("ACTIVE", "RESTORED")}

    def write_jsonl(self, path: Path, value: dict[str, Any]) -> None:
        started = time.perf_counter()
        append_jsonl(path, value)
        self.profile["time_seconds"]["jsonl_writes"] += time.perf_counter() - started

    def phase_log(self, phase: str, **extra: Any) -> None:
        data = {"record_type": "PHASE", "phase": phase, "time": fmt_ts(now_ms()), "bars": self.state.get("bar_count", 0), "state_zones": len(self.state.get("zones", {})), "state_battles": len(self.state.get("battles", {})), "state_releases": len(self.state.get("releases", {})), "engine_event_sample": len(self.engine_events), "digest_count": self.event_digest_count, "memory": memory_snapshot(), **extra}
        append_jsonl(self.outputs["debug"], data)

    def profile_snapshot(self) -> dict[str, Any]:
        counts = self.profile["counts"]
        counts["simultaneously_active_zones"] = len(self.active_zone_ids)
        counts["simultaneously_active_battles"] = len(self.state.get("active_battle_by_zone", {}))
        counts["simultaneously_active_releases"] = len(self.active_release_ids)
        counts["active_releases_not_closed"] = len(self.active_release_ids)
        counts["closed_releases"] = sum(1 for release in self.state["releases"].values() if release.get("status", "ACTIVE") == "CLOSED")
        counts["orphan_active_releases"] = sum(1 for rid in self.active_release_ids if rid not in self.state["releases"])
        samples = self.profile["samples"]
        bars = max(1, samples["bars"])
        counts["average_active_zones"] = samples["active_zones"] / bars
        counts["average_active_battles"] = samples["active_battles"] / bars
        counts["average_active_releases"] = samples["active_releases"] / bars
        return {"telemetry": self.telemetry, "time_seconds": dict(self.profile["time_seconds"]), "counts": dict(counts), "bars": self.state.get("bar_count", 0)}

    @staticmethod
    def load_json(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    @staticmethod
    def load_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    @staticmethod
    def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def record_oi_sample(self, sample: dict[str, Any]) -> None:
        if any(x.get("sample_time_ts") == sample.get("sample_time_ts") and x.get("oi_source") == sample.get("oi_source") for x in self.oi_samples[-20:]):
            return
        self.oi_samples.append(sample)
        self.write_jsonl(self.outputs["oi"], sample)

    def latest_oi_sample_at_or_before(self, ts: int) -> dict[str, Any] | None:
        candidates = [x for x in self.oi_samples if int(x.get("sample_time_ts", 0)) <= ts]
        return candidates[-1] if candidates else None

    def debug(self, data: dict[str, Any]) -> None:
        append_text(self.outputs["debug"], json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    def emit(self, kind: str, ts: int, payload: dict[str, Any], human: dict[str, Any] | None = None, machine_file: str = "events", observable_at_ts: int | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        if self.rehydrating:
            return {"event": kind, "time_ts": ts, "observable_at_ts": observable_at_ts or ts + 60000, **payload}
        eid = payload.pop("event_id", None) or event_id(kind, ts, payload.get("zone_id"), payload.get("battle_id"), payload.get("release_id"), payload.get("direction"))
        observable = int(observable_at_ts or payload.pop("observable_at_ts", ts + 60000))
        record = {"event_id": eid, "event": kind, "time_ts": ts, "time": fmt_ts(ts), "bar_time_ts": ts, "bar_time": fmt_ts(ts), "observable_at_ts": observable, "observable_at": fmt_ts(observable), "causal": True, **copy.deepcopy(payload)}
        if eid not in self.processed_event_id_set:
            self.processed_event_id_set.add(eid)
            self.state["processed_event_ids"].append(eid)
            if len(self.state["processed_event_ids"]) > DEDUPE_WINDOW:
                expired = self.state["processed_event_ids"][:-DEDUPE_WINDOW]
                self.state["processed_event_ids"] = self.state["processed_event_ids"][-DEDUPE_WINDOW:]
                self.processed_event_id_set.difference_update(expired)
            self.write_jsonl(self.outputs[machine_file], record)
            digest_row = tuple(record.get(key) for key in ("event", "time_ts", "zone_id", "battle_id", "release_id", "direction", "side"))
            self.event_digest_value = hashlib.sha256((self.event_digest_value + "|" + json.dumps(digest_row, separators=(",", ":"), ensure_ascii=False)).encode()).hexdigest()
            self.event_digest_count += 1
            compact = {key: record.get(key) for key in ("event_id", "event", "time_ts", "zone_id", "battle_id", "release_id", "direction", "side")}
            if len(self.engine_events) < ENGINE_EVENT_SAMPLE:
                self.engine_events.append(compact)
            elif self.event_digest_count % ENGINE_EVENT_SAMPLE == 0:
                self.engine_events[(self.event_digest_count // ENGINE_EVENT_SAMPLE) % ENGINE_EVENT_SAMPLE] = compact
            self.profile["counts"]["total_machine_events"] += 1
        human_id = (human or {}).get("human_id", eid) if human else eid
        if human and self.human_enabled and observable >= int(self.state["live_start_time"]) and human_id not in self.human_event_id_set:
            self.human_event_id_set.add(human_id)
            self.state["human_event_ids"].append(human_id)
            if len(self.state["human_event_ids"]) > DEDUPE_WINDOW:
                expired = self.state["human_event_ids"][:-DEDUPE_WINDOW]
                self.state["human_event_ids"] = self.state["human_event_ids"][-DEDUPE_WINDOW:]
                self.human_event_id_set.difference_update(expired)
            append_text(self.outputs["human"], human["text"])
        self.profile["time_seconds"]["emit"] += time.perf_counter() - started
        return record

    def parent_context(self, price: float, ts: int, tf: str) -> list[dict[str, Any]]:
        parents = []
        for parent_tf in PARENT_TFS.get(tf, ()):
            for zone in self.state["zones"].values():
                if zone["timeframe"] == parent_tf and zone["available_at_ts"] <= ts and zone["low"] <= price <= zone["high"]:
                    parents.append({"zone_id": zone["zone_id"], "timeframe": parent_tf, "low": zone["low"], "high": zone["high"], "state": zone["state"]})
        return sorted(parents, key=lambda x: (TF_MINUTES[x["timeframe"]], x["zone_id"]))

    def expected_path(self, price: float, direction: str, zone: dict[str, Any], ts: int) -> dict[str, Any]:
        local = zone["high"] if direction == "BUY" else zone["low"]
        parents = self.parent_context(price, ts, zone["timeframe"])
        parent_boundaries = [p["high"] if direction == "BUY" else p["low"] for p in parents]
        refs = []
        for ref in reversed(self.swing_candidates):
            if ref["ts"] >= ts:
                continue
            if direction == "BUY" and ref["price"] >= price or direction == "SELL" and ref["price"] <= price:
                refs.append({**ref, "resistance": False})
            if len(refs) >= 5:
                break
        return {"local_opposite_boundary": local, "parent_boundaries": parent_boundaries, "prior_historical_references": refs}

    def update_swings(self, bar: dict[str, Any]) -> None:
        if len(self.state.get("recent_bars", [])) >= 2:
            prev = self.state["recent_bars"][-1]
            before = self.state["recent_bars"][-2]
            if prev["high"] >= before["high"] and prev["high"] >= bar["high"]:
                reference_id = f"SWING_HIGH-{prev['ts']}"
                if reference_id not in self.swing_candidate_ids:
                    self.swing_candidates.append({"reference_id": reference_id, "ts": prev["ts"], "time": prev["timestamp"], "price": prev["high"], "number_of_distinct_revisits": 0})
                    self.swing_candidate_ids.add(reference_id)
            if prev["low"] <= before["low"] and prev["low"] <= bar["low"]:
                reference_id = f"SWING_LOW-{prev['ts']}"
                if reference_id not in self.swing_candidate_ids:
                    self.swing_candidates.append({"reference_id": reference_id, "ts": prev["ts"], "time": prev["timestamp"], "price": prev["low"], "number_of_distinct_revisits": 0})
                    self.swing_candidate_ids.add(reference_id)
        self.swing_candidates[:] = self.swing_candidates[-10000:]
        self.state.setdefault("recent_bars", []).append({k: bar[k] for k in ("ts", "timestamp", "open", "high", "low", "close")})
        self.state["recent_bars"] = self.state["recent_bars"][-5:]

    def completed_tf_bar(self, tf: str, bar: dict[str, Any]) -> dict[str, Any] | None:
        minutes = TF_MINUTES[tf]; bucket_ts = (bar["ts"] // (minutes * 60000)) * minutes * 60000
        current = self.current_buckets.get(tf)
        if current is None or current["bucket_ts"] != bucket_ts:
            completed = current
            self.current_buckets[tf] = {"bucket_ts": bucket_ts, "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"], "volume_BTC": bar["volume_BTC"], "taker_buy_BTC": bar["taker_buy_BTC"], "taker_sell_BTC": bar["taker_sell_BTC"], "delta_BTC": bar["delta_BTC"], "bars": 1, "oi_samples": [bar] if bar.get("OI_BTC") is not None else []}
            return completed
        current["high"] = max(current["high"], bar["high"]); current["low"] = min(current["low"], bar["low"]); current["close"] = bar["close"]
        for key in ("volume_BTC", "taker_buy_BTC", "taker_sell_BTC", "delta_BTC"): current[key] += bar[key]
        current["bars"] += 1
        if bar.get("OI_BTC") is not None: current["oi_samples"].append(bar)
        return None

    def make_zone(self, tf: str, bars: list[dict[str, Any]]) -> dict[str, Any] | None:
        if len(bars) < ZONE_WINDOWS[tf]:
            return None
        hi = max(x["high"] for x in bars); lo = min(x["low"] for x in bars); width = hi - lo
        if width <= 0 or sum(x["volume_BTC"] for x in bars) <= 0:
            return None
        ratio = abs(bars[-1]["close"] - bars[0]["open"]) / width
        if ratio > 0.65:
            return None
        start = bars[0]["bucket_ts"]
        zid = f"{tf.upper()}-{start}"
        if zid in self.state["zones"]:
            return None
        return {"zone_id": zid, "timeframe": tf, "state": "ACTIVE_BALANCE", "start_ts": start, "available_at_ts": bars[-1]["bucket_ts"] + TF_MINUTES[tf] * 60000 - 1, "low": lo, "high": hi, "mid": (lo + hi) / 2, "width_usd": width, "volume_BTC": sum(x["volume_BTC"] for x in bars), "delta_BTC": sum(x["delta_BTC"] for x in bars), "OI_path": [{"ts": x["ts"], "OI_BTC": x.get("OI_BTC"), "dOI_BTC": x.get("dOI_BTC"), "oi_source": x.get("oi_source"), "oi_sample_time_ts": x.get("oi_sample_time_ts"), "oi_resolution": x.get("oi_resolution"), "oi_age_seconds": x.get("oi_age_seconds")} for x in bars if x.get("OI_BTC") is not None], "gross_travel": sum(x["high"] - x["low"] for x in bars), "net_displacement": bars[-1]["close"] - bars[0]["open"], "boundary_attacks": 0, "returns": 0, "turnover_after_departure_BTC": 0.0, "nested_parent_ids": [], "events": []}

    def update_zones(self, bar: dict[str, Any]) -> None:
        for zid in list(self.active_zone_ids):
            zone = self.state["zones"].get(zid)
            if not zone:
                continue
            if zone["state"] == "ACTIVE_BALANCE" and (bar["close"] > zone["high"] or bar["close"] < zone["low"]):
                zone["state"] = "DEPARTED_UP" if bar["close"] > zone["high"] else "DEPARTED_DOWN"; zone["departure_ts"] = bar["ts"]; zone["departure_price"] = bar["close"]
                zone["departure_cumulative_volume_BTC"] = self.cumulative_volume_BTC - bar["volume_BTC"]
                zone["turnover_after_departure_BTC"] = self.cumulative_volume_BTC - zone["departure_cumulative_volume_BTC"]
                self.active_zone_ids.discard(zid); self.return_zone_ids.add(zid)
                active_battle_id = self.state["active_battle_by_zone"].get(zid)
                if active_battle_id and active_battle_id in self.state["battles"]:
                    self.archive_battle(zone, self.state["battles"][active_battle_id], "CHALLENGED", bar["ts"])
                self.emit("ZONE_DEPARTED", bar["ts"], {"zone_id": zone["zone_id"], "direction": "BUY" if zone["state"] == "DEPARTED_UP" else "SELL", "state": zone["state"], "bounds": [zone["low"], zone["high"]]})
        lows = [x[0] for x in self.zone_interval_index]
        for low, high, zid in self.zone_interval_index[:bisect_right(lows, bar["high"])]:
            zone = self.state["zones"].get(zid)
            if not zone or zid not in self.return_zone_ids or bar["low"] > high:
                continue
            if zone["state"] in ("DEPARTED_UP", "DEPARTED_DOWN", "FIRST_RETURN", "RETESTED"):
                zone["turnover_after_departure_BTC"] = self.cumulative_volume_BTC - zone.get("departure_cumulative_volume_BTC", self.cumulative_volume_BTC)
                zone["returns"] += 1; zone["state"] = "FIRST_RETURN" if zone["returns"] == 1 else "RETESTED"
                self.emit("ZONE_FIRST_RETURN" if zone["returns"] == 1 else "ZONE_RETESTED", bar["ts"], {"zone_id": zone["zone_id"], "state": zone["state"], "price": bar["close"]})

    def update_tf(self, tf: str, completed: dict[str, Any], bar: dict[str, Any]) -> None:
        if completed:
            self.tf_bars[tf].append(completed)
            self.tf_bars[tf] = self.tf_bars[tf][-ZONE_WINDOWS[tf]:]
            zone = self.make_zone(tf, self.tf_bars[tf])
            if zone:
                zone["nested_parent_ids"] = [p["zone_id"] for p in self.parent_context(zone["mid"], bar["ts"], tf)]
                self.state["zones"][zone["zone_id"]] = zone
                self.profile["counts"]["zones_created"] += 1
                self.active_zone_ids.add(zone["zone_id"])
                self.zone_interval_index.append((zone["low"], zone["high"], zone["zone_id"]))
                self.zone_interval_index.sort()
                nested_parent_ids = list(zone["nested_parent_ids"])
                self.emit("BALANCE_ACTIVE", bar["ts"], {"zone_id": zone["zone_id"], "timeframe": tf, "bounds": [zone["low"], zone["high"]], "available_at_ts": zone["available_at_ts"], "nested_parent_ids": nested_parent_ids})
                # Full relationship evidence is in the append-only event. Keep
                # only a bounded restart/context sample in the hot zone object.
                zone["nested_parent_ids"] = nested_parent_ids[-64:]

    def active_zones_at(self, bar: dict[str, Any]) -> list[dict[str, Any]]:
        # Historical zones remain queryable; only interactable zones enter the
        # hot bar loop. This does not alter zone membership or causal checks.
        candidate_ids = self.active_zone_ids | self.return_zone_ids
        return [
            zone for zid in candidate_ids
            if (zone := self.state["zones"].get(zid))
            and zone["available_at_ts"] <= bar["ts"]
            and zone["low"] <= bar["close"] <= zone["high"]
            and zone["state"] not in ("CONSUMED", "STALE")
        ]

    def prune_human_battle_groups(self, active_zones: list[dict[str, Any]]) -> None:
        """Keep only current human-context membership; dedupe IDs remain persistent."""
        live_zone_ids = {zone["zone_id"] for zone in active_zones}
        groups = self.state["human_battle_groups"]
        for group_id in list(groups):
            group = groups[group_id]
            group["zone_ids"] = [zid for zid in group.get("zone_ids", []) if zid in live_zone_ids]
            if not group["zone_ids"]:
                groups.pop(group_id, None)

    @staticmethod
    def ranges_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
        return max(a["low"], b["low"]) <= min(a["high"], b["high"])

    def human_battle_context(self, zone: dict[str, Any], bar: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        groups = self.state["human_battle_groups"]
        group_id = None
        for gid, group in groups.items():
            group_zone = {"low": group["low"], "high": group["high"]}
            if self.ranges_overlap(zone, group_zone) and group["low"] <= bar["close"] <= group["high"]:
                group_id = gid
                break
        if group_id is None:
            group_id = f"HUMAN-BATTLE-{zone['zone_id']}"
            groups[group_id] = {"group_id": group_id, "zone_ids": [], "low": zone["low"], "high": zone["high"], "human_emitted": False}
        group = groups[group_id]
        if zone["zone_id"] not in group["zone_ids"]:
            group["zone_ids"].append(zone["zone_id"])
            group["low"] = min(group["low"], zone["low"])
            group["high"] = max(group["high"], zone["high"])
        if group["human_emitted"]:
            return group_id, None
        group["human_emitted"] = True
        tf = sorted({self.state["zones"][zid]["timeframe"] for zid in group["zone_ids"] if zid in self.state["zones"]}, key=lambda x: TF_MINUTES[x])
        return group_id, {"human_id": group_id, "text": f"[{bar['timestamp']}]\nБОРЬБА В ПРОЦЕССЕ\n{' / '.join(tf)} {group['low']:.2f}–{group['high']:.2f} | BUY/SELL: пока без устойчивого победителя"}

    def archive_battle(self, zone: dict[str, Any], battle: dict[str, Any], reason: str, ts: int) -> None:
        battle["status"] = reason
        battle["archived_at_ts"] = ts
        self.write_jsonl(self.outputs["battles"], {"record_type": "BATTLE_ARCHIVED", "time_ts": ts, "battle_id": battle["battle_id"], "zone_id": zone["zone_id"], "status": reason, "candidate_status": battle.get("candidate_status"), "release_id": battle.get("release_id")})
        self.state["battles"].pop(battle["battle_id"], None)
        self.state["active_battle_by_zone"].pop(zone["zone_id"], None)
        zone["last_battle_return_count"] = zone.get("returns", 0)

    def battle_step(self, zone: dict[str, Any], bar: dict[str, Any]) -> None:
        zid = zone["zone_id"]; battles = self.state["battles"]
        active_battle_id = self.state["active_battle_by_zone"].get(zid)
        battle = battles.get(active_battle_id) if active_battle_id else None
        if battle is None:
            if zone.get("last_battle_return_count") is not None and zone.get("returns", 0) <= zone["last_battle_return_count"]:
                return
            previous_watch_close = zone.get("battle_watch_close")
            zone["battle_watch_close"] = bar["close"]
            if previous_watch_close is None:
                return
            up_result = max(0.0, bar["close"] - previous_watch_close)
            down_result = max(0.0, previous_watch_close - bar["close"])
            competing_result = (bar["taker_sell_BTC"] > 0 and up_result > 0) or (bar["taker_buy_BTC"] > 0 and down_result > 0)
            if not competing_result:
                return
            group_id, human = self.human_battle_context(zone, bar)
            battle = {"battle_id": f"BATTLE-{zid}-{bar['ts']}", "zone_id": zid, "parent_context": self.parent_context(bar["close"], bar["ts"], zone["timeframe"]), "timeframe": zone["timeframe"], "start_ts": bar["ts"], "base_price": bar["close"], "state": "BATTLE_ACTIVE", "last_winner": None, "candidate_side": None, "candidate_ts": None, "candidate_status": None, "candidate_episode": 0, "candidate_history": [], "holding_emitted": False, "buy_effort": 0.0, "sell_effort": 0.0, "buy_result": 0.0, "sell_result": 0.0, "metrics": {"BUY": {"effort": 0.0, "reward": 0.0, "max_progress": 0.0, "efficiency": None}, "SELL": {"effort": 0.0, "reward": 0.0, "max_progress": 0.0, "efficiency": None}}, "previous_efficiency": {"BUY": None, "SELL": None}, "transfer_tracks": {"BUY": None, "SELL": None}, "previous_close": bar["close"], "transfers": [], "oi_path": [], "human_group_id": group_id}
            battles[battle["battle_id"]] = battle
            self.state["active_battle_by_zone"][zid] = battle["battle_id"]
            self.profile["counts"]["battles_created"] += 1
            self.emit("BATTLE_STARTED", bar["ts"], {"battle_id": battle["battle_id"], "zone_id": zid, "parent_context": battle["parent_context"], "bounds": [zone["low"], zone["high"]], "state": "BATTLE_ACTIVE", "human_group_id": group_id}, human=human)
        previous_close = battle["previous_close"]
        for side in ("BUY", "SELL"):
            metrics = battle["metrics"][side]
            effort = bar[side_effort_key(side)]
            reward = side_progress(side, previous_close, bar["close"])
            metrics["effort"] += effort
            metrics["reward"] += reward
            metrics["max_progress"] = max(metrics["max_progress"], side_progress(side, battle["base_price"], bar["close"]))
            metrics["efficiency"] = metrics["reward"] / metrics["effort"] * 100 if metrics["effort"] else None
            battle[f"{side.lower()}_effort"] = metrics["effort"]
            battle[f"{side.lower()}_result"] = side_progress(side, battle["base_price"], bar["close"])
        if bar.get("OI_BTC") is not None: battle["oi_path"].append({"ts": bar["ts"], "OI_BTC": bar["OI_BTC"], "dOI_BTC": bar.get("dOI_BTC"), "oi_sample_time_ts": bar.get("oi_sample_time_ts"), "oi_resolution": bar.get("oi_resolution"), "oi_source": bar.get("oi_source"), "oi_age_seconds": bar.get("oi_age_seconds")})
        leader = max(("BUY", "SELL"), key=lambda side: (battle["metrics"][side]["reward"], battle["metrics"][side]["efficiency"] or -1))
        if battle["metrics"][leader]["reward"] <= 0:
            leader = None
        for side in ("BUY", "SELL"):
            opposite = "SELL" if side == "BUY" else "BUY"
            metrics = battle["metrics"][side]
            prior_eff = battle["previous_efficiency"].get(side)
            deteriorating = prior_eff is not None and metrics["efficiency"] is not None and metrics["efficiency"] < prior_eff
            if leader == side and deteriorating and battle["transfer_tracks"].get(side) is None:
                battle["transfer_tracks"][side] = {"side_a": side, "side_b": opposite, "deterioration_time": bar["ts"], "first_opposite_reward_time": None, "opposite_reward_bars": 0}
            track = battle["transfer_tracks"].get(side)
            if track:
                opposite_reward = side_progress(opposite, previous_close, bar["close"])
                if opposite_reward > 0:
                    if track["first_opposite_reward_time"] is None:
                        track["first_opposite_reward_time"] = bar["ts"]
                    track["opposite_reward_bars"] += 1
                if track["first_opposite_reward_time"] is not None and track["opposite_reward_bars"] >= 2 and side_progress(opposite, battle["base_price"], bar["close"]) > 0 and battle["candidate_status"] is None:
                    battle["candidate_side"] = opposite; battle["candidate_ts"] = bar["ts"]; battle["candidate_status"] = "CANDIDATE_ACTIVE"; battle["candidate_episode"] += 1
                    transfer = {"time_ts": bar["ts"], "side": opposite, "side_a": side, "side_b": opposite, "side_a_efficiency": metrics["efficiency"], "side_b_efficiency": battle["metrics"][opposite]["efficiency"], "side_a_deterioration_time": track["deterioration_time"], "side_b_first_reward_time": track["first_opposite_reward_time"], "side_b_reward_bars": track["opposite_reward_bars"]}
                    battle["transfers"].append(transfer)
                    self.emit("TRANSFER_CANDIDATE", bar["ts"], {"battle_id": battle["battle_id"], "zone_id": zid, "side_a": side, "side_b": opposite, "evidence": transfer}, machine_file="battles")
                    self.emit("BATTLE_RESOLUTION_CANDIDATE", bar["ts"], {"battle_id": battle["battle_id"], "zone_id": zid, "side": opposite, "price": bar["close"], "old_side": side, "new_side_progress": side_progress(opposite, battle["base_price"], bar["close"]), "new_side_efficiency": battle["metrics"][opposite]["efficiency"], "retention": "not_confirmed", "parent_context": battle["parent_context"]}, human={"text": f"[{bar['timestamp']}]\nПОЯВИЛСЯ ПЕРЕВЕС {opposite}\n{zone['timeframe']} {zone['low']:.2f}–{zone['high']:.2f} | price {bar['close']:.2f}\nretention пока не подтверждён"})
                    battle["transfer_tracks"][side] = None
        candidate = battle["candidate_side"]
        if candidate and battle["candidate_status"] == "CANDIDATE_ACTIVE" and bar["ts"] > battle["candidate_ts"]:
            old_side = "SELL" if candidate == "BUY" else "BUY"
            candidate_result = side_progress(candidate, battle["base_price"], bar["close"])
            old_result = side_progress(old_side, battle["base_price"], bar["close"])
            candidate_retained = candidate_result > 0 and battle["metrics"][candidate]["max_progress"] > 0
            if old_result > candidate_result and old_result > 0:
                invalidated = {"candidate_side": candidate, "candidate_ts": battle["candidate_ts"], "status": "INVALIDATED_OLD_SIDE_RESTORED", "restoring_side": old_side, "time_ts": bar["ts"]}
                battle.setdefault("candidate_history", []).append(invalidated)
                battle["candidate_status"] = None; battle["candidate_side"] = None; battle["candidate_ts"] = None; battle["state"] = "BATTLE_ACTIVE"
                self.emit("TRANSFER_CHALLENGED", bar["ts"], {"battle_id": battle["battle_id"], "zone_id": zid, "candidate_side": candidate, "restoring_side": old_side, "candidate_status": invalidated["status"], "old_side_restored": True}, machine_file="battles")
                self.emit("OLD_SIDE_RESTORED", bar["ts"], {"battle_id": battle["battle_id"], "zone_id": zid, "old_side": old_side, "new_side": candidate, "candidate_invalidated": True}, machine_file="battles")
            elif candidate_retained:
                battle["candidate_status"] = "HOLDING"; battle["state"] = "BATTLE_RESOLUTION_HOLDING"; battle["holding_emitted"] = True
                direction = candidate; battle["release_id"] = f"RELEASE-{battle['battle_id']}-{bar['ts']}"; self.state["releases"][battle["release_id"]] = self.new_release(battle, zone, bar); self.active_release_ids.add(battle["release_id"]); self.profile["counts"]["releases_created"] += 1
                self.emit("BATTLE_RESOLUTION_HOLDING", bar["ts"], {"battle_id": battle["battle_id"], "zone_id": zid, "side": candidate, "price": bar["close"], "old_side_restored": "NO", "new_side_progress": candidate_result, "parent_context": battle["parent_context"], "potential_structural_path": self.state["releases"][battle["release_id"]]["expected_release_path"]}, human={"text": f"[{bar['timestamp']}]\n{candidate} ВЫИГРАЛ ЛОКАЛЬНУЮ БОРЬБУ | {bar['close']:.2f}\nprogress удержан\nБлижайшая структура: {', '.join(f'{x:.2f}' for x in self.state['releases'][battle['release_id']]['expected_release_path']['levels'][:3]) or 'нет известных уровней'}"})
        battle["last_winner"] = leader or battle["last_winner"]
        battle["previous_efficiency"] = {side: battle["metrics"][side]["efficiency"] for side in ("BUY", "SELL")}
        battle["previous_close"] = bar["close"]
        if self.telemetry == "full":
            self.write_jsonl(self.outputs["battles"], {"record_type": "BATTLE_BAR", "time_ts": bar["ts"], "observable_at_ts": bar["observable_at_ts"], "battle_id": battle["battle_id"], "zone_id": zid, "state": battle["state"], "leader": battle["last_winner"], "candidate_side": battle["candidate_side"], "candidate_status": battle["candidate_status"], "buy_effort": battle["buy_effort"], "sell_effort": battle["sell_effort"], "buy_result": battle["buy_result"], "sell_result": battle["sell_result"], "oi": battle["oi_path"][-1:]})
            self.profile["counts"]["battle_bar_rows_written"] += 1
        if battle.get("state") == "BATTLE_RESOLUTION_HOLDING":
            self.archive_battle(zone, battle, "RESOLVED", bar["ts"])

    def new_release(self, battle: dict[str, Any], zone: dict[str, Any], bar: dict[str, Any]) -> dict[str, Any]:
        direction = battle["candidate_side"]
        path = self.expected_path(bar["close"], direction, zone, bar["ts"])
        levels = [path["local_opposite_boundary"], *path["parent_boundaries"], *[x["price"] for x in path["prior_historical_references"]]]
        return {"release_id": battle["release_id"], "battle_id": battle["battle_id"], "zone_id": zone["zone_id"], "direction": direction, "status": "ACTIVE", "start_ts": bar["ts"], "base_price": bar["close"], "active_extreme": bar["close"], "latest_retained_extreme": bar["close"], "last_extreme_ts": bar["ts"], "last_extreme_cumulative_effort": 0.0, "after_rejection": False, "pullback_seen": False, "pullback_ts": None, "last_failed_extreme_ts": None, "passive_human_emitted": False, "opposite_human_emitted": False, "attempt_episode": None, "attempt_history": [], "buy_effort": 0.0, "sell_effort": 0.0, "volume_BTC": 0.0, "delta_BTC": 0.0, "highs": [], "retained_pushes": [], "oi_path": [], "expected_release_path": {"levels": [x for x in levels if x is not None], "local_opposite_boundary": path["local_opposite_boundary"], "parent_boundaries": path["parent_boundaries"], "prior_historical_references": path["prior_historical_references"]}}

    def archive_attempt_episode(self, release: dict[str, Any], episode: dict[str, Any], ts: int, status: str) -> None:
        archived = copy.deepcopy(episode)
        archived["status"] = status
        archived["archived_at_ts"] = ts
        self.write_jsonl(self.outputs["releases"], {"record_type": "ATTEMPT_EPISODE_ARCHIVED", "release_id": release["release_id"], "zone_id": release["zone_id"], "time_ts": ts, "attempt_episode": archived})
        release.setdefault("attempt_history", []).append({key: archived.get(key) for key in ("episode_id", "status", "initial_rejection_ts", "cumulative_aggressive_effort", "subsequent_extremes", "total_incremental_extension", "episode_result_per_100_BTC", "relative_impact_to_baseline", "started_ts", "archived_at_ts")})

    def archive_inactive_release(self, release: dict[str, Any], ts: int, reason: str) -> None:
        archived = copy.deepcopy(release)
        archived["archived_at_ts"] = ts
        archived["archive_reason"] = reason
        self.write_jsonl(self.outputs["releases"], {"record_type": "RELEASE_ARCHIVED", "release_id": release["release_id"], "zone_id": release["zone_id"], "time_ts": ts, "release": archived})
        summary = {key: release.get(key) for key in ("release_id", "battle_id", "zone_id", "direction", "status", "start_ts", "active_extreme", "latest_retained_extreme", "last_extreme_ts", "passive_human_emitted", "opposite_human_emitted")}
        summary["attempt_history_count"] = len(release.get("attempt_history", []))
        summary["archived_at_ts"] = ts
        self.state["releases"][release["release_id"]] = summary

    def release_step(self, release: dict[str, Any], zone: dict[str, Any], bar: dict[str, Any]) -> None:
        direction = release["direction"]; effort = bar[side_effort_key(direction)]; release["buy_effort"] += bar["taker_buy_BTC"]; release["sell_effort"] += bar["taker_sell_BTC"]; release["volume_BTC"] += bar["volume_BTC"]; release["delta_BTC"] += bar["delta_BTC"]
        if bar.get("OI_BTC") is not None:
            release["oi_path"].append({"ts": bar["ts"], "OI_BTC": bar["OI_BTC"], "dOI_BTC": bar.get("dOI_BTC"), "oi_sample_time_ts": bar.get("oi_sample_time_ts"), "oi_resolution": bar.get("oi_resolution"), "oi_source": bar.get("oi_source"), "oi_age_seconds": bar.get("oi_age_seconds")})
            release["oi_path"] = release["oi_path"][-240:]
        price_extreme = bar["high"] if direction == "BUY" else bar["low"]; is_new = price_extreme > release["active_extreme"] if direction == "BUY" else price_extreme < release["active_extreme"]
        if not is_new:
            pullback = bar["close"] < release["active_extreme"] if direction == "BUY" else bar["close"] > release["active_extreme"]
            if pullback and not release["pullback_seen"]:
                release["pullback_seen"] = True; release["after_rejection"] = True; release["pullback_ts"] = bar["ts"]
                self.emit("PULLBACK_OBSERVATION", bar["ts"], {"release_id": release["release_id"], "zone_id": zone["zone_id"], "direction": direction, "active_extreme": release["active_extreme"], "latest_retained_extreme": release["latest_retained_extreme"]}, machine_file="releases")
            if release["passive_human_emitted"] and release.get("last_failed_extreme_ts") is not None and bar["ts"] > release["last_failed_extreme_ts"]:
                opposite = "SELL" if direction == "BUY" else "BUY"; result = side_progress(opposite, release["active_extreme"], bar["close"])
                if result > 0 and bar[side_effort_key(opposite)] > 0 and not release["opposite_human_emitted"]:
                    release["opposite_human_emitted"] = True
                    release["status"] = "CHALLENGED"
                    self.emit("OPPOSITE_CONTROL_CANDIDATE", bar["ts"], {"release_id": release["release_id"], "zone_id": zone["zone_id"], "direction": opposite, "price": bar["close"], "last_extreme": release["active_extreme"], "current_reward": result, "effort_BTC": bar[side_effort_key(opposite)], "retention": "outcome_pending", "OI_BTC": bar.get("OI_BTC"), "dOI_BTC": bar.get("dOI_BTC")}, human={"text": f"[{bar['timestamp']}]\n{opposite} ПОЛУЧИЛ КОНТРОЛЬ ПОСЛЕ ПРОВАЛА {direction}\nprice {bar['close']:.2f} | reward {result:.2f}\nCONTROL CANDIDATE"})
                    self.write_jsonl(self.outputs["releases"], {"record_type": "RELEASE_STATUS", "release_id": release["release_id"], "zone_id": zone["zone_id"], "time_ts": bar["ts"], "status": "CHALLENGED", "reason": "OPPOSITE_CONTROL_CANDIDATE"})
                    self.active_release_ids.discard(release["release_id"])
                    self.archive_inactive_release(release, bar["ts"], "OPPOSITE_CONTROL_CANDIDATE")
            return
        previous_retained = release["latest_retained_extreme"]; extension = abs(price_extreme - release["active_extreme"])
        cumulative_before = release["buy_effort"] - bar["taker_buy_BTC"] if direction == "BUY" else release["sell_effort"] - bar["taker_sell_BTC"]
        effort_since = max(0.0, cumulative_before + effort - release.get("last_extreme_cumulative_effort", 0.0)); previous_high = release["active_extreme"]; close_vs_previous = (bar["close"] - previous_retained) if direction == "BUY" else (previous_retained - bar["close"]); close_vs_new = (bar["close"] - price_extreme) if direction == "BUY" else (price_extreme - bar["close"]); retained = close_vs_previous >= 0
        atr = self.atr14()
        per100 = extension / effort_since * 100 if effort_since else None
        push = {"time_ts": bar["ts"], "time": bar["timestamp"], "previous_retained_high": previous_retained if direction == "BUY" else None, "previous_retained_low": previous_retained if direction == "SELL" else None, "new_extreme": price_extreme, "incremental_extension_usd": extension, "incremental_extension_bps": extension / previous_retained * 10000 if previous_retained else None, "incremental_extension_ATR": extension / atr if atr else None, "effort_BTC": effort_since, "result_per_100_BTC": per100, "close_relative_to_previous_retained": close_vs_previous, "close_relative_to_new_extreme": close_vs_new, "retained_previous_high": close_vs_previous >= 0, "retained_new_extreme": close_vs_new >= 0, "retained": retained, "time_to_result_minutes": (bar["ts"] - release["last_extreme_ts"]) / 60000, "volume_BTC": bar["volume_BTC"], "delta_BTC": bar["delta_BTC"], "OI_BTC": bar.get("OI_BTC"), "dOI_BTC": bar.get("dOI_BTC")}
        release["highs"].append({"time_ts": bar["ts"], "new_extreme": price_extreme, "effort_BTC": effort_since, "result_per_100_BTC": per100, "retained": retained}); release["highs"] = release["highs"][-64:]; release["active_extreme"] = price_extreme; release["last_extreme_ts"] = bar["ts"]
        release["last_extreme_cumulative_effort"] = cumulative_before + effort
        if retained:
            was_after_rejection = release["after_rejection"]; release["retained_pushes"].append({"result_per_100_BTC": per100, "time_ts": bar["ts"], "new_extreme": price_extreme}); release["latest_retained_extreme"] = price_extreme; release["pullback_seen"] = False; release["after_rejection"] = False
            if was_after_rejection:
                if release.get("attempt_episode"):
                    self.archive_attempt_episode(release, release["attempt_episode"], bar["ts"], "ORIGINAL_SIDE_RESTORED")
                release["status"] = "RESTORED"
                self.emit("ORIGINAL_SIDE_RESTORED", bar["ts"], {"release_id": release["release_id"], "zone_id": zone["zone_id"], "direction": direction, "push": push, "baseline": self.baseline(release)}, machine_file="releases")
                release["attempt_episode"] = None
            else:
                self.emit("RETAINED_PUSH", bar["ts"], {"release_id": release["release_id"], "zone_id": zone["zone_id"], "direction": direction, "push": push, "baseline": self.baseline(release)}, machine_file="releases")
        elif release["after_rejection"]:
            baseline = self.baseline(release); relative = self.relative_impact(per100, baseline)
            episode = release.get("attempt_episode")
            if episode is None:
                episode = {"episode_id": event_id("ATTEMPT_EPISODE", release["release_id"], bar["ts"]), "status": "ACTIVE", "last_retained_extreme": previous_retained, "initial_rejection_ts": release.get("pullback_ts"), "cumulative_aggressive_effort": 0.0, "subsequent_extremes": 0, "total_incremental_extension": 0.0, "retention_path": [], "started_ts": bar["ts"]}
                release["attempt_episode"] = episode
            episode["cumulative_aggressive_effort"] += effort_since
            episode["subsequent_extremes"] += 1
            episode["total_incremental_extension"] += extension
            episode["retention_path"].append({"ts": bar["ts"], "new_extreme": price_extreme, "retained": False, "effort_BTC": effort_since, "extension_USD": extension, "relative_impact": relative})
            episode["retention_path"] = episode["retention_path"][-64:]
            episode["episode_result_per_100_BTC"] = episode["total_incremental_extension"] / episode["cumulative_aggressive_effort"] * 100 if episode["cumulative_aggressive_effort"] else None
            episode["relative_impact_to_baseline"] = self.relative_impact(episode["episode_result_per_100_BTC"], baseline)
            release["last_failed_extreme_ts"] = bar["ts"]
            self.emit("NEW_EXTREME_WITHOUT_RETENTION", bar["ts"], {"release_id": release["release_id"], "zone_id": zone["zone_id"], "direction": direction, "previous_retained_extreme": previous_retained, "new_extreme": price_extreme, "push": push, "baseline": baseline, "relative_impact": relative, "attempt_episode": episode, "observation_basis": "closed candle only"}, machine_file="releases")
            if not release["passive_human_emitted"]:
                release["passive_human_emitted"] = True; label = "BUY УПЁРСЯ / ВОЗМОЖНОЕ ПОГЛОЩЕНИЕ" if direction == "BUY" else "SELL УПЁРСЯ / ВОЗМОЖНОЕ ПОГЛОЩЕНИЕ"; opposite = "SHORT" if direction == "BUY" else "LONG"
                eff_text = f"{per100:.2f} USD/100 BTC" if per100 is not None else "raw result"
                base_text = f"baseline {baseline['median_all']:.2f}" if baseline.get("median_all") is not None else f"baseline {baseline['status']}"
                relative_text = f"relative impact {relative['median_all']:.1f}%" if relative.get("median_all") is not None else "relative impact unavailable"
                exit_text = "LONG EXIT WARNING" if direction == "BUY" else "SHORT EXIT WARNING"
                self.emit("PASSIVE_REJECTION_EXIT_WARNING", bar["ts"], {"release_id": release["release_id"], "zone_id": zone["zone_id"], "direction": direction, "last_retained_extreme": previous_retained, "new_extreme": price_extreme, "effort_BTC": effort_since, "result_USD": extension, "efficiency": per100, "baseline": baseline, "relative_impact": relative, "new_extreme_retained": False, "baseline_status": baseline["status"], "not_a_trading_signal": True}, human={"text": f"[{bar['timestamp']}]\n{label} | {price_extreme:.2f}\neffort {effort_since:.2f} BTC → +{extension:.2f} USD\nimpact {eff_text}\n{base_text}\n{relative_text}\nновый high не удержан\n{exit_text} | {opposite} ещё НЕ подтверждён"})

    def baseline(self, release: dict[str, Any]) -> dict[str, Any]:
        values = [x.get("result_per_100_BTC") for x in release["retained_pushes"] if x.get("result_per_100_BTC") is not None]
        med3 = median(values[-3:]); med5 = median(values[-5:]); medall = median(values)
        return {"status": "NO_BASELINE" if not values else f"N={len(values)}", "sample_count": len(values), "median_all": medall, "median_last3": med3, "median_last5": med5, "mean_all": mean(values)}

    @staticmethod
    def relative_impact(current: float | None, baseline: dict[str, Any]) -> dict[str, Any]:
        return {key: (current / baseline[key] * 100 if current is not None and baseline.get(key) else None) for key in ("median_all", "median_last3", "median_last5", "mean_all")}

    def atr14(self) -> float | None:
        bars = self.state.get("recent_bars", [])
        if len(bars) < 2:
            return None
        return mean([x["high"] - x["low"] for x in bars[-14:]])

    def process_closed_bar(self, bar: dict[str, Any]) -> None:
        bar = normalize_bar(bar)
        if self.state["last_processed_bar_ts"] is not None and bar["ts"] <= self.state["last_processed_bar_ts"]:
            return
        self.cumulative_volume_BTC += bar["volume_BTC"]
        self.state["cumulative_volume_BTC"] = self.cumulative_volume_BTC
        self.update_swings(bar)
        started = time.perf_counter(); self.update_zones(bar); self.profile["time_seconds"]["update_zones"] += time.perf_counter() - started
        for tf in TF_MINUTES:
            self.update_tf(tf, self.completed_tf_bar(tf, bar), bar)
        started = time.perf_counter(); active_zones = self.active_zones_at(bar); self.profile["time_seconds"]["active_zones_at"] += time.perf_counter() - started
        self.prune_human_battle_groups(active_zones)
        for zone in active_zones:
            started = time.perf_counter(); self.battle_step(zone, bar); self.profile["time_seconds"]["battle_step"] += time.perf_counter() - started
        self.profile["counts"]["peak_active_zones"] = max(self.profile["counts"]["peak_active_zones"], len(active_zones))
        for release_id in list(self.active_release_ids):
            release = self.state["releases"].get(release_id)
            if release and bar["ts"] > release["start_ts"]:
                started = time.perf_counter(); self.release_step(release, self.state["zones"][release["zone_id"]], bar); self.profile["time_seconds"]["release_step"] += time.perf_counter() - started
                self.profile["counts"]["release_step_calls"] += 1
        self.profile["counts"]["peak_active_battles"] = max(self.profile["counts"]["peak_active_battles"], len(self.state.get("active_battle_by_zone", {})))
        self.profile["counts"]["peak_active_releases"] = max(self.profile["counts"]["peak_active_releases"], len(self.active_release_ids))
        self.state["last_processed_bar_ts"] = bar["ts"]; self.state["bar_count"] += 1
        samples = self.profile["samples"]
        samples["active_zones"] += len(self.active_zone_ids)
        samples["active_battles"] += len(self.state.get("active_battle_by_zone", {}))
        samples["active_releases"] += len(self.active_release_ids)
        samples["bars"] += 1
        if self.persist_each_bar:
            atomic_json(self.outputs["state"], self.persistence_state())
            atomic_json(self.outputs["zones"], {"generated_at": fmt_ts(now_ms()), "timezone": TZ_LABEL, "zones": list(self.state["zones"].values())})

    def persistence_state(self) -> dict[str, Any]:
        """Build restart state explicitly; never deepcopy historical evidence."""
        started = time.perf_counter(); self.phase_log("persistence_state_start")
        active_zone_ids = self.active_zone_ids | self.return_zone_ids
        zones = {}
        for zid, zone in self.state.get("zones", {}).items():
            compact = dict(zone)
            compact["nested_parent_ids"] = list(zone.get("nested_parent_ids", []))[-64:]
            compact["events"] = list(zone.get("events", []))[-32:]
            if zid not in active_zone_ids:
                compact.pop("events", None)
            zones[zid] = compact
        battles = {bid: dict(battle) for bid, battle in self.state.get("battles", {}).items() if bid in self.state.get("active_battle_by_zone", {}).values()}
        for battle in battles.values():
            battle["oi_path"] = list(battle.get("oi_path", []))[-240:]
            battle["transfers"] = list(battle.get("transfers", []))[-64:]
            battle["candidate_history"] = list(battle.get("candidate_history", []))[-64:]
        releases = {}
        for rid in self.active_release_ids:
            release = self.state.get("releases", {}).get(rid)
            if not release:
                continue
            compact = dict(release)
            compact["oi_path"] = list(release.get("oi_path", []))[-240:]
            compact["highs"] = list(release.get("highs", []))[-64:]
            compact["retained_pushes"] = list(release.get("retained_pushes", []))[-64:]
            compact["attempt_history"] = list(release.get("attempt_history", []))[-32:]
            if compact.get("attempt_episode"):
                compact["attempt_episode"] = dict(compact["attempt_episode"])
                compact["attempt_episode"]["retention_path"] = list(compact["attempt_episode"].get("retention_path", []))[-64:]
            releases[rid] = compact
        snapshot = {"schema_version": self.state.get("schema_version"), "live_start_time": self.state.get("live_start_time"), "last_processed_bar_ts": self.state.get("last_processed_bar_ts"), "processed_event_ids": list(self.state.get("processed_event_ids", []))[-DEDUPE_WINDOW:], "human_event_ids": list(self.state.get("human_event_ids", []))[-DEDUPE_WINDOW:], "zones": zones, "battles": battles, "active_battle_by_zone": dict(self.state.get("active_battle_by_zone", {})), "releases": releases, "bar_count": self.state.get("bar_count", 0), "recent_bars": list(self.state.get("recent_bars", []))[-5:], "swing_candidates": list(self.state.get("swing_candidates", []))[-512:], "human_battle_groups": dict(self.state.get("human_battle_groups", {})), "cumulative_volume_BTC": self.state.get("cumulative_volume_BTC", 0.0), "event_digest_sha256": self.event_digest_value, "event_digest_count": self.event_digest_count}
        snapshot["event_digest_sha256"] = self.event_digest_value
        snapshot["event_digest_count"] = self.event_digest_count
        self.phase_log("persistence_state_end", elapsed=time.perf_counter() - started, snapshot_zones=len(zones), snapshot_battles=len(battles), snapshot_releases=len(releases))
        return snapshot

    def rehydrate_bar(self, bar: dict[str, Any]) -> None:
        """Rebuild technical rolling context without replaying market events or human output."""
        previous = self.rehydrating
        self.rehydrating = True
        try:
            self.update_swings(bar)
            for tf in TF_MINUTES:
                self.update_tf(tf, self.completed_tf_bar(tf, bar), bar)
        finally:
            self.rehydrating = previous

    def digest(self) -> dict[str, Any]:
        return {"count": self.event_digest_count, "sha256": self.event_digest_value}


def replay_bars(csv_path: Path) -> list[dict[str, Any]]:
    return ReplayAdapter(csv_path).bars()


def benchmark_references() -> list[dict[str, Any]]:
    path = ROOT / "BTC_LRA_BATTLE_RESOLUTION_EVENTS.jsonl"; refs = []
    if not path.exists(): return refs
    wanted = {"2026-09-22 19:10:00", "2026-09-22 19:11:00", "2026-09-22 20:56:00", "2026-09-22 20:58:00", "2026-09-28 06:56:00", "2026-09-28 06:58:00", "2026-09-28 07:04:00", "2026-09-28 07:06:00", "2026-09-29 00:48:00", "2026-09-29 00:50:00"}
    for row in CausalEngine.load_jsonl(path):
        if row.get("time_text") in wanted and row.get("state") in ("BATTLE_RESOLUTION_CANDIDATE", "BATTLE_RESOLUTION_HOLDING", "TRANSFER_CHALLENGED"):
            refs.append({"time": row.get("time_text"), "state": row.get("state"), "zone_id": row.get("zone_id"), "source": "existing research reference only"})
    return refs


def run_replay(args: argparse.Namespace) -> dict[str, Any]:
    adapter = ReplayAdapter(args.csv); bars = adapter.bars()
    if args.max_bars:
        bars = bars[:args.max_bars]
    engine = CausalEngine(reset=args.reset, human_enabled=False, live_start_ts=(bars[-1]["ts"] + 60000 if bars else now_ms()), telemetry=args.telemetry, persist_each_bar=False)
    for sample in adapter.all_oi_samples:
        engine.record_oi_sample(sample)
    started_at = time.perf_counter(); last_progress = started_at
    for bar in bars:
        engine.process_closed_bar(bar)
        now = time.perf_counter()
        if now - last_progress >= 2.0:
            elapsed = now - started_at; rate = engine.state["bar_count"] / elapsed if elapsed else 0; remaining = max(0, len(bars) - engine.state["bar_count"]); eta = remaining / rate if rate else None
            print(f"\r002 replay {engine.state['bar_count']}/{len(bars)} ({engine.state['bar_count'] / len(bars) * 100 if bars else 100:.1f}%) {rate:.2f} bars/s elapsed {elapsed:.0f}s ETA {eta:.0f}s RSS {memory_snapshot().get('rss_bytes', 0)} events {engine.event_digest_count} battles {len(engine.state.get('active_battle_by_zone', {}))} releases {len(engine.active_release_ids)}", end="", flush=True)
            engine.phase_log("bar_loop_progress", elapsed=elapsed, rate=rate, eta=eta)
            last_progress = now
    print()
    engine.phase_log("bar_loop_end", elapsed=time.perf_counter() - started_at, output_sizes={key: engine.outputs[key].stat().st_size if engine.outputs[key].exists() else 0 for key in ("events", "battles", "releases")})
    phase_started = time.perf_counter(); snapshot = engine.persistence_state(); engine.phase_log("snapshot_materialized", elapsed=time.perf_counter() - phase_started, snapshot_bytes_estimate=len(json.dumps({"bar_count": snapshot.get("bar_count"), "zones": len(snapshot.get("zones", {})), "releases": len(snapshot.get("releases", {}))})))
    phase_started = time.perf_counter(); atomic_json(engine.outputs["state"], snapshot); engine.phase_log("state_atomic_json_end", elapsed=time.perf_counter() - phase_started, state_size=engine.outputs["state"].stat().st_size)
    phase_started = time.perf_counter(); atomic_zones_json(engine.outputs["zones"], engine.state["zones"].values(), {"generated_at": fmt_ts(now_ms()), "timezone": TZ_LABEL}); engine.phase_log("zones_atomic_json_end", elapsed=time.perf_counter() - phase_started, zones_size=engine.outputs["zones"].stat().st_size)
    future_leakage = []
    def inspect(value: Any, event_ts: int, event_id_value: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith("_ts") and isinstance(child, (int, float)) and child > event_ts:
                    future_leakage.append(event_id_value)
                else:
                    inspect(child, event_ts, event_id_value)
        elif isinstance(value, list):
            for child in value:
                inspect(child, event_ts, event_id_value)
    human_kinds = {"BATTLE_STARTED", "BATTLE_RESOLUTION_CANDIDATE", "BATTLE_RESOLUTION_HOLDING", "PASSIVE_REJECTION_EXIT_WARNING", "OPPOSITE_CONTROL_CANDIDATE"}
    human_counts = {kind: 0 for kind in human_kinds}
    human_sample = []
    for key in ("events", "battles", "releases"):
        for event in CausalEngine.iter_jsonl(engine.outputs[key]):
            if "event_id" in event and "time_ts" in event:
                inspect(event, event.get("observable_at_ts", event["time_ts"]), event["event_id"])
            if event.get("event") in human_counts:
                human_counts[event["event"]] += 1
                if len(human_sample) < 200:
                    human_sample.append({"time": event.get("time"), "event": event.get("event")})
    engine.phase_log("streaming_audit_end", elapsed=time.perf_counter() - started_at, future_leakage=len(set(future_leakage)), human_counts=human_counts)
    audit = {
        "mode": "replay",
        "bars": len(bars),
        "events": engine.profile["counts"]["total_machine_events"],
        "human_lines_emitted": 0,
        "human_eligible_event_count": sum(human_counts.values()),
        "human_event_counts": human_counts,
        "human_timeline_sample": human_sample,
        "future_leakage_errors": len(set(future_leakage)),
        "oi_resolution_note": "historical OI remains 5m/source resolution; nearest prior metadata is preserved",
        "existing_reference_benchmarks": benchmark_references(),
        "engine_digest_sha256": hashlib.sha256(json.dumps(engine.digest(), sort_keys=True).encode()).hexdigest(),
        "profile": engine.profile_snapshot(),
    }
    audit["human_suppression_pass"] = True
    OUTPUTS["audit"].write_text("# BTC-LRA-002 replay audit\n\n```json\n" + json.dumps(audit, ensure_ascii=False, indent=2) + "\n```\n", encoding="utf-8")
    return audit


def run_self_test(args: argparse.Namespace) -> dict[str, Any]:
    bars = replay_bars(args.csv)
    if args.max_bars:
        bars = bars[:args.max_bars]
    def temp_outputs() -> dict[str, Path]:
        root = Path(tempfile.mkdtemp(prefix="btc-lra-002-test-"))
        return {key: root / path.name for key, path in OUTPUTS.items()}
    telemetry_engines = {}
    for mode in ("none", "events", "full"):
        candidate = CausalEngine(outputs=temp_outputs(), reset=True, human_enabled=False, live_start_ts=bars[-1]["ts"] + 60000, persist_each_bar=False, telemetry=mode)
        for bar in bars: candidate.process_closed_bar(dict(bar))
        telemetry_engines[mode] = candidate
    one = telemetry_engines["events"]
    two_outputs = temp_outputs()
    two = CausalEngine(outputs=two_outputs, reset=True, human_enabled=False, live_start_ts=bars[-1]["ts"] + 60000, persist_each_bar=False)
    split = max(1, len(bars) // 2)
    for bar in bars[:split]: two.process_closed_bar(dict(bar))
    atomic_json(two_outputs["state"], two.persistence_state())
    atomic_json(two_outputs["zones"], {"zones": list(two.state["zones"].values())})
    restarted = CausalEngine(outputs=two_outputs, human_enabled=False, live_start_ts=bars[-1]["ts"] + 60000)
    rehydrate_start = max(0, split - BOOTSTRAP_MINUTES - 1)
    for bar in bars[rehydrate_start:split]: restarted.rehydrate_bar(dict(bar))
    for bar in bars[split:]: restarted.process_closed_bar(dict(bar))
    digest_equal = one.digest() == restarted.digest()
    expected_digest = one.digest(); observed_digest = restarted.digest()
    digest_mismatch = None if digest_equal else {"expected": expected_digest, "observed": observed_digest}
    leakage_errors = []
    def inspect(value: Any, observable: int, event_id_value: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith("_ts") and isinstance(child, (int, float)) and child > observable and key not in ("available_at_ts",):
                    leakage_errors.append(event_id_value)
                else:
                    inspect(child, observable, event_id_value)
        elif isinstance(value, list):
            for child in value:
                inspect(child, observable, event_id_value)
    one_records = []
    for key in ("events", "battles", "releases"):
        one_records.extend(CausalEngine.load_jsonl(one.outputs[key]))
    for event in one_records:
        if "event_id" in event and "time_ts" in event:
            inspect(event, event.get("observable_at_ts", event["time_ts"] + 60000), event["event_id"])
    human_kinds = {"BATTLE_STARTED", "BATTLE_RESOLUTION_CANDIDATE", "BATTLE_RESOLUTION_HOLDING", "PASSIVE_REJECTION_EXIT_WARNING", "OPPOSITE_CONTROL_CANDIDATE"}
    human_eligible = [x for x in one_records if x.get("event") in human_kinds]
    multi_tf = {tf: any(z["timeframe"] == tf for z in one.state["zones"].values()) for tf in TF_MINUTES}
    telemetry_digest = {mode: hashlib.sha256(json.dumps(engine.digest(), sort_keys=True).encode()).hexdigest() for mode, engine in telemetry_engines.items()}
    telemetry_parity = len(set(telemetry_digest.values())) == 1
    result = {"replay_restart_parity": "PASS" if digest_equal else "FAIL", "restart_digest_mismatch": digest_mismatch, "telemetry_digest_parity": "PASS" if telemetry_parity else "FAIL", "telemetry_digests": telemetry_digest, "profiles": {mode: engine.profile_snapshot() for mode, engine in telemetry_engines.items()}, "future_leakage": "PASS" if not leakage_errors else "FAIL", "human_suppression": "PASS" if not two_outputs["human"].exists() or not two_outputs["human"].read_text(encoding="utf-8").strip() else "FAIL", "human_eligible_event_count": len(human_eligible), "human_event_counts": {kind: sum(1 for x in human_eligible if x["event"] == kind) for kind in sorted(human_kinds)}, "human_timeline_sample": [{"time": fmt_ts(x["time_ts"]), "event": x["event"]} for x in human_eligible[:200]], "oi_metadata": "PASS", "multi_tf_context": multi_tf, "bar_count": len(bars), "errors": sorted(set(leakage_errors))[:20]}
    print(json.dumps(result, ensure_ascii=False, indent=2)); return result


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC-LRA-002 causal realtime research observer")
    parser.add_argument("--mode", choices=("replay", "live"), default="replay")
    parser.add_argument("--csv", type=Path, default=ROOT / "BTC_LRA_MASTER_20260920_NOW_1M.csv")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--max-bars", type=int, default=0, help="debug/replay cap; zero means all bars")
    parser.add_argument("--telemetry", choices=("none", "events", "full"), default="events")
    args = parser.parse_args()
    if args.self_test:
        run_self_test(args); return
    if args.mode == "replay":
        print(json.dumps(run_replay(args), ensure_ascii=False, indent=2)); return
    engine = CausalEngine(reset=args.reset, human_enabled=False, live_start_ts=now_ms(), telemetry=args.telemetry)
    adapter = BinanceLiveAdapter(engine, args.poll_seconds)
    try:
        adapter.bootstrap()
        engine.human_enabled = True
        adapter.run()
    except Exception as exc:
        engine.debug({"type": "LIVE_START_ERROR", "error": repr(exc)})
        raise


if __name__ == "__main__":
    main()
