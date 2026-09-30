"""Offline population, representation and restart-parity audit.

Reads existing evidence and uses the causal engine only in a no-I/O harness.
It never changes detector semantics or production output.
"""
from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import importlib.util
import json
import statistics
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
ENGINE_PATH = ROOT / "btc-lra-002.py"
HUMAN_EVENTS = {"BATTLE_STARTED", "BATTLE_RESOLUTION_CANDIDATE", "BATTLE_RESOLUTION_HOLDING", "PASSIVE_REJECTION_EXIT_WARNING", "OPPOSITE_CONTROL_CANDIDATE"}
SELECTED_EVENTS = {"TRANSFER_CANDIDATE", "BATTLE_RESOLUTION_CANDIDATE", "BATTLE_RESOLUTION_HOLDING", "TRANSFER_CHALLENGED", "OLD_SIDE_RESTORED", "PASSIVE_REJECTION_EXIT_WARNING", "OPPOSITE_CONTROL_CANDIDATE"}
CLUSTER_MAX_GAP_MINUTES = 5
BENCHMARK_LOCAL = [("2026-09-22 19:10", "2026-09-22 19:10"), ("2026-09-22 19:11", "2026-09-22 19:11"), ("2026-09-22 20:56", "2026-09-22 20:56"), ("2026-09-22 20:58", "2026-09-22 20:58"), ("2026-09-22 23:33", "2026-09-22 23:33"), ("2026-09-22 23:37", "2026-09-22 23:37"), ("2026-09-28 06:56", "2026-09-28 07:06"), ("2026-09-29 00:48", "2026-09-29 00:50")]


def load_engine():
    spec = importlib.util.spec_from_file_location("btc_lra_002_audit_engine", ENGINE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def iter_jsonl(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def event_range(row: dict[str, Any]):
    bounds = row.get("bounds")
    if isinstance(bounds, list) and len(bounds) == 2 and all(isinstance(x, (int, float)) for x in bounds):
        return float(bounds[0]), float(bounds[1])
    price = row.get("price")
    return (float(price), float(price)) if isinstance(price, (int, float)) else None


def direction(row: dict[str, Any]):
    for key in ("direction", "side", "candidate_side", "old_side", "new_side"):
        if row.get(key) in ("BUY", "SELL"):
            return row[key]
    return None


def local_minute(text: str) -> int:
    # Benchmark labels are Panama local time (UTC-05), used only after audit.
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp() * 1000) + 5 * 60 * 60 * 1000


def percentile(values: list[int], fraction: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return float(values[min(len(values) - 1, round((len(values) - 1) * fraction))])


def population_audit(root: Path):
    counts = Counter(); ids = defaultdict(lambda: {"zone": set(), "battle": set(), "release": set()})
    by_minute = defaultdict(lambda: {"events": 0, "types": Counter(), "zones": set(), "battles": set(), "releases": set(), "selected_count": 0, "selected_directions": set(), "selected_low": None, "selected_high": None, "selected_zone_ids": set(), "selected_battle_ids": set(), "selected_release_ids": set()})
    active_battles: set[str] = set(); active_releases: set[str] = set(); active_zones: set[str] = set(); returned_zones: set[str] = set(); benchmark_events = defaultdict(list)
    paths = [root / "BTC_LRA_002_EVENTS.jsonl", root / "BTC_LRA_002_BATTLES.jsonl", root / "BTC_LRA_002_RELEASES.jsonl"]
    benchmark_windows = [(f"{start}..{end}", local_minute(start), local_minute(end) + 59999) for start, end in BENCHMARK_LOCAL]
    for path in paths:
        for row in iter_jsonl(path):
            kind = row.get("event") or row.get("record_type")
            bid = row.get("battle_id"); rid = row.get("release_id"); zid = row.get("zone_id")
            if row.get("record_type") == "BATTLE_ARCHIVED" and bid:
                active_battles.discard(bid)
            elif row.get("record_type") == "RELEASE_STATUS" and rid and row.get("status") == "CHALLENGED":
                active_releases.discard(rid)
            if not row.get("event"):
                continue
            counts[kind] += 1
            if zid: ids[kind]["zone"].add(zid)
            if bid: ids[kind]["battle"].add(bid)
            if rid: ids[kind]["release"].add(rid)
            ts = row.get("time_ts")
            if not isinstance(ts, int):
                continue
            minute = ts // 60000; data = by_minute[minute]; data["events"] += 1; data["types"][kind] += 1
            if zid: data["zones"].add(zid)
            if bid: data["battles"].add(bid)
            if rid: data["releases"].add(rid)
            if kind == "BATTLE_STARTED" and bid: active_battles.add(bid)
            if kind == "BATTLE_RESOLUTION_HOLDING" and rid: active_releases.add(rid)
            if kind == "BALANCE_ACTIVE" and zid: active_zones.add(zid)
            if kind == "ZONE_DEPARTED" and zid: active_zones.discard(zid); returned_zones.add(zid)
            if kind in {"ZONE_FIRST_RETURN", "ZONE_RETESTED"} and zid: returned_zones.add(zid)
            data["active_battles"] = len(active_battles); data["active_releases"] = len(active_releases); data["interactable_zones"] = len(active_zones | returned_zones)
            if kind in SELECTED_EVENTS:
                data["selected_count"] += 1
                if direction(row): data["selected_directions"].add(direction(row))
                bounds = event_range(row)
                if bounds:
                    data["selected_low"] = bounds[0] if data["selected_low"] is None else min(data["selected_low"], bounds[0]); data["selected_high"] = bounds[1] if data["selected_high"] is None else max(data["selected_high"], bounds[1])
                if zid: data["selected_zone_ids"].add(zid)
                if bid: data["selected_battle_ids"].add(bid)
                if rid: data["selected_release_ids"].add(rid)
            for label, start_ts, end_ts in benchmark_windows:
                if start_ts <= ts <= end_ts:
                    benchmark_events[label].append({"event": kind, "time_ts": ts, "zone_id": zid, "battle_id": bid, "release_id": rid, "direction": direction(row)})
    metrics = [{"minute_ts": minute * 60000, "events": data["events"], "zones": len(data["zones"]), "interactable_zones": data.get("interactable_zones", 0), "battles": data.get("active_battles", 0), "releases": data.get("active_releases", 0)} for minute, data in sorted(by_minute.items())]
    distributions = {key: {"median": percentile([x[key] for x in metrics], .5), "p90": percentile([x[key] for x in metrics], .9), "p95": percentile([x[key] for x in metrics], .95), "p99": percentile([x[key] for x in metrics], .99), "max": max((x[key] for x in metrics), default=0)} for key in ("events", "zones", "interactable_zones", "battles", "releases")}
    total = sum(counts.values())
    event_types = {kind: {"count": count, "percent": count / max(1, total) * 100, "events_per_bar": count / 13810, "unique_zone_ids": len(ids[kind]["zone"]), "unique_battle_ids": len(ids[kind]["battle"]), "unique_release_ids": len(ids[kind]["release"])} for kind, count in counts.most_common()}
    top = {key: sorted(metrics, key=lambda x: x[key], reverse=True)[:20] for key in ("events", "zones", "interactable_zones", "battles", "releases")}
    summary = {"total_machine_events": total, "event_counts": dict(counts), "event_types": event_types, "bar_distributions": distributions, "top_20": top, "benchmark_events": dict(benchmark_events), "analysis_parameters": {"source": "existing JSONL only", "minute_bucket": "time_ts // 60000", "cluster_max_gap_minutes": CLUSTER_MAX_GAP_MINUTES, "benchmark_timezone": "America/Panama (UTC-05), regression only"}}
    return summary, metrics, by_minute


def physical_clusters(by_minute):
    clusters = []
    for minute in sorted(by_minute):
        data = by_minute[minute]
        human_count = sum(data["types"].get(x, 0) for x in HUMAN_EVENTS)
        if not data["selected_count"] and not human_count: continue
        dirs = data["selected_directions"]; ranges = [(data["selected_low"], data["selected_high"])] if data["selected_low"] is not None else []
        low = min((x[0] for x in ranges), default=None); high = max((x[1] for x in ranges), default=None); current = clusters[-1] if clusters else None
        compatible = bool(current and minute - current["end_minute"] <= CLUSTER_MAX_GAP_MINUTES and (current["direction"] == "MIXED" or not dirs or current["direction"] in dirs) and (current["low"] is None or low is None or high >= current["low"] and current["high"] >= low))
        if not compatible:
            current = {"physical_episode_id": f"PE-{len(clusters)+1:06d}", "start_minute": minute, "end_minute": minute, "direction": next(iter(dirs)) if len(dirs) == 1 else "MIXED", "low": low, "high": high, "raw_event_count": 0, "human_eligible_count": 0, "zone_ids": set(), "battle_ids": set(), "release_ids": set()}; clusters.append(current)
        current["end_minute"] = minute; current["low"] = low if current["low"] is None else current["low"] if low is None else min(current["low"], low); current["high"] = high if current["high"] is None else current["high"] if high is None else max(current["high"], high); current["raw_event_count"] += data["events"]; current["human_eligible_count"] += human_count
        current["zone_ids"].update(data["selected_zone_ids"]); current["battle_ids"].update(data["selected_battle_ids"]); current["release_ids"].update(data["selected_release_ids"])
    for row in clusters:
        row["zone_ids"] = sorted(row["zone_ids"]); row["battle_ids"] = sorted(row["battle_ids"]); row["release_ids"] = sorted(row["release_ids"]); row["zone_representations"] = len(row["zone_ids"]); row["battle_representations"] = len(row["battle_ids"]); row["release_representations"] = len(row["release_ids"])
    return clusters


SIGNATURE_KEYS = (
    "event", "time_ts", "observable_at_ts", "direction", "side",
    "zone_id", "battle_id", "release_id", "price", "base_price",
    "active_extreme", "latest_retained_extreme", "result_per_100_BTC",
    "old_side", "new_side", "candidate_side", "candidate_status",
    "status", "winner", "leader", "old_side_restored", "new_side_progress",
)
SEMANTIC_KEYS = tuple(key for key in SIGNATURE_KEYS if key not in {"zone_id", "battle_id", "release_id"})


def compact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): compact_value(value[key]) for key in sorted(value) if key not in {"events", "parent_context"}}
    if isinstance(value, list):
        return [compact_value(x) for x in value]
    return value


def state_before_event(engine: Any, record: dict[str, Any]) -> dict[str, Any]:
    state = engine.state
    zid = record.get("zone_id")
    bid = record.get("battle_id")
    rid = record.get("release_id")
    if not bid and zid:
        bid = state.get("active_battle_by_zone", {}).get(zid)
    if not rid and bid:
        battle = state.get("battles", {}).get(bid, {})
        rid = battle.get("release_id")
    tf_context = {
        tf: {
            "tail": compact_value(engine.tf_bars.get(tf, [])[-3:]),
            "current_bucket": compact_value(engine.current_buckets.get(tf)),
        }
        for tf in sorted(set(getattr(engine, "tf_bars", {})) | set(getattr(engine, "current_buckets", {})))
    }
    return {
        "bar_count": state.get("bar_count"),
        "last_processed_bar_ts": state.get("last_processed_bar_ts"),
        "cumulative_volume_BTC": engine.cumulative_volume_BTC,
        "zone_id": zid,
        "battle_id": bid,
        "release_id": rid,
        "zone": compact_value(state.get("zones", {}).get(zid)) if zid else None,
        "battle": compact_value(state.get("battles", {}).get(bid)) if bid else None,
        "release": compact_value(state.get("releases", {}).get(rid)) if rid else None,
        "active_zone_ids": sorted(engine.active_zone_ids),
        "return_zone_ids": sorted(engine.return_zone_ids),
        "active_zone_iteration_order": list(engine.active_zone_ids),
        "return_zone_iteration_order": list(engine.return_zone_ids),
        "candidate_zone_iteration_order": list(engine.active_zone_ids | engine.return_zone_ids),
        "active_release_ids": sorted(engine.active_release_ids),
        "active_battle_by_zone": dict(state.get("active_battle_by_zone", {})),
        "recent_bars": compact_value(state.get("recent_bars", [])[-5:]),
        "swing_candidates": compact_value(state.get("swing_candidates", [])[-16:]),
        "tf_context": tf_context,
        "oi_last": compact_value(engine.oi_samples[-1]) if engine.oi_samples else None,
        "processed_event_ids_tail": state.get("processed_event_ids", [])[-8:],
    }


def causal_signature(record: dict[str, Any], sequence: int) -> dict[str, Any]:
    return {"sequence_number": sequence, **{key: record.get(key) for key in SIGNATURE_KEYS}}


def signature_core(signature: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
    return [signature.get(key) for key in keys]


def attach_trace(engine: Any, signature_path: Path, capture_path: Path | None = None, capture_sequence: int | None = None, append: bool = False, initial_strict: str = "", initial_semantic: str = "") -> dict[str, Any]:
    handle = signature_path.open("a" if append else "w", encoding="utf-8", buffering=1024 * 1024)
    strict_digest = initial_strict
    semantic_digest = initial_semantic
    captured = False
    original_emit = engine.emit

    def traced_emit(kind: str, ts: int, payload: dict[str, Any], human: dict[str, Any] | None = None, machine_file: str = "events", observable_at_ts: int | None = None):
        nonlocal strict_digest, semantic_digest, captured
        before_count = engine.event_digest_count
        pre_state = None
        if capture_path and capture_sequence == before_count + 1 and not captured:
            pre_state = state_before_event(engine, {"event": kind, "time_ts": ts, "observable_at_ts": observable_at_ts, **payload})
        record = original_emit(kind, ts, payload, human=human, machine_file=machine_file, observable_at_ts=observable_at_ts)
        if engine.event_digest_count == before_count:
            return record
        sequence = engine.event_digest_count
        signature = causal_signature(record, sequence)
        strict_core = signature_core(signature, SIGNATURE_KEYS)
        semantic_core = signature_core(signature, SEMANTIC_KEYS)
        strict_digest = hashlib.sha256((strict_digest + "|" + json.dumps(strict_core, separators=(",", ":"), ensure_ascii=False)).encode()).hexdigest()
        semantic_digest = hashlib.sha256((semantic_digest + "|" + json.dumps(semantic_core, separators=(",", ":"), ensure_ascii=False)).encode()).hexdigest()
        handle.write(json.dumps({"signature": signature, "strict_core": strict_core, "semantic_core": semantic_core}, ensure_ascii=False, separators=(",", ":")) + "\n")
        if capture_path and capture_sequence == sequence and not captured:
            capture_path.write_text(json.dumps({"sequence_number": sequence, "signature": signature, "state_before_event": pre_state or state_before_event(engine, record)}, ensure_ascii=False, indent=2), encoding="utf-8")
            captured = True
        return record

    engine.emit = traced_emit
    return {"handle": handle, "strict": lambda: strict_digest, "semantic": lambda: semantic_digest, "captured": lambda: captured}


def finish_trace(trace: dict[str, Any]) -> dict[str, Any]:
    trace["handle"].flush(); trace["handle"].close()
    return {"strict_digest": trace["strict"](), "semantic_digest": trace["semantic"](), "captured": trace["captured"]()}


def read_signature(handle: Any) -> dict[str, Any] | None:
    line = handle.readline()
    if not line:
        return None
    return json.loads(line)


def classify_mismatch(left: dict[str, Any] | None, right: dict[str, Any] | None) -> str:
    if left is None or right is None:
        return "E. event missing on one branch"
    a = left["signature"]; b = right["signature"]
    if a.get("event") != b.get("event"):
        return "C. different event type"
    if a.get("time_ts") != b.get("time_ts") or a.get("observable_at_ts") != b.get("observable_at_ts"):
        return "D. different timestamp/order"
    if any(a.get(key) != b.get(key) for key in ("direction", "side")):
        return "B. same IDs but different direction/side"
    if any(a.get(key) != b.get(key) for key in ("zone_id", "battle_id", "release_id")):
        return "A. same event type/time but different IDs"
    return "F. same causal identity but payload/state field differs"


def compare_signatures(left_path: Path, right_path: Path) -> dict[str, Any]:
    first_strict = None
    first_semantic = None
    compared = 0
    with left_path.open("r", encoding="utf-8") as left, right_path.open("r", encoding="utf-8") as right:
        while True:
            a = read_signature(left); b = read_signature(right)
            if a is None and b is None:
                break
            compared += 1
            if first_strict is None and (a is None or b is None or a["strict_core"] != b["strict_core"]):
                first_strict = {"sequence_number": compared, "classification": classify_mismatch(a, b), "continuous": a, "restart": b}
            if first_semantic is None and (a is None or b is None or a["semantic_core"] != b["semantic_core"]):
                first_semantic = {"sequence_number": compared, "classification": classify_mismatch(a, b), "continuous": a, "restart": b}
            if first_strict is not None and first_semantic is not None:
                # Continue only until both streams have been consumed in a later pass would waste time;
                # the first mismatch is already fixed and missing-event cases are represented above.
                break
    return {"compared_until_first_both": compared, "first_strict_divergence": first_strict, "first_semantic_divergence": first_semantic}


def diagnostic_parity_case(module, bars, split, label):
    root = Path(tempfile.mkdtemp(prefix="btc002-divergence-"))
    continuous_signature = root / "continuous_signatures.jsonl"
    restart_signature = root / "restart_signatures.jsonl"
    def outputs():
        return {key: root / f"{key}.json" for key in ("state", "zones", "events", "battles", "releases", "oi", "human", "debug", "audit")}
    def make_engine(paths):
        engine = module.CausalEngine(outputs=paths, reset=True, human_enabled=False, persist_each_bar=False, telemetry="none")
        engine.write_jsonl = lambda path, value: None
        return engine
    continuous = make_engine(outputs())
    continuous_trace = attach_trace(continuous, continuous_signature)
    for bar in bars: continuous.process_closed_bar(dict(bar))
    continuous_trace_result = finish_trace(continuous_trace)
    del continuous; gc.collect()
    split_paths = outputs()
    first = make_engine(split_paths)
    restart_prefix_trace = attach_trace(first, restart_signature)
    for bar in bars[:split]: first.process_closed_bar(dict(bar))
    restart_prefix_result = finish_trace(restart_prefix_trace)
    snapshot = first.persistence_state()
    module.atomic_json(split_paths["state"], snapshot)
    module.atomic_json(split_paths["zones"], {"zones": list(snapshot["zones"].values())})
    del first; gc.collect()
    restarted = module.CausalEngine(outputs=split_paths, human_enabled=False, persist_each_bar=False, telemetry="none")
    restarted.write_jsonl = lambda path, value: None
    start = max(0, split - module.BOOTSTRAP_MINUTES - 1)
    for bar in bars[start:split]: restarted.rehydrate_bar(dict(bar))
    restart_trace = attach_trace(restarted, restart_signature, initial_strict=restart_prefix_result["strict_digest"], initial_semantic=restart_prefix_result["semantic_digest"], append=True)
    for bar in bars[split:]: restarted.process_closed_bar(dict(bar))
    restart_trace_result = finish_trace(restart_trace)
    comparison = compare_signatures(continuous_signature, restart_signature)
    result = {"label": label, "split": split, "bars": len(bars), "temp_root": str(root), "continuous_signatures": str(continuous_signature), "restart_signatures": str(restart_signature), "continuous": continuous_trace_result, "restart": restart_trace_result, "continuous_event_count": restarted.event_digest_count, "comparison": comparison}
    first_divergence = comparison.get("first_strict_divergence") or comparison.get("first_semantic_divergence")
    if first_divergence:
        seq = first_divergence["sequence_number"]
        # Rerun only the prefix needed to materialize state immediately before the first mismatch.
        def rerun_until(paths, sequence, bars_for_run, split_at, capture_path):
            engine = make_engine(paths)
            if split_at is None:
                trace = attach_trace(engine, paths["audit"], capture_path, sequence)
                for bar in bars_for_run:
                    engine.process_closed_bar(dict(bar))
            else:
                prefix_trace = attach_trace(engine, paths["audit"])
                for bar in bars_for_run[:split_at]: engine.process_closed_bar(dict(bar))
                prefix_result = finish_trace(prefix_trace)
                snapshot = engine.persistence_state(); module.atomic_json(paths["state"], snapshot); module.atomic_json(paths["zones"], {"zones": list(snapshot["zones"].values())})
                del engine; gc.collect()
                engine = module.CausalEngine(outputs=paths, human_enabled=False, persist_each_bar=False, telemetry="none"); engine.write_jsonl = lambda path, value: None
                start_at = max(0, split_at - module.BOOTSTRAP_MINUTES - 1)
                for bar in bars_for_run[start_at:split_at]: engine.rehydrate_bar(dict(bar))
                trace = attach_trace(engine, paths["audit"], capture_path, sequence, append=True, initial_strict=prefix_result["strict_digest"], initial_semantic=prefix_result["semantic_digest"])
                for bar in bars_for_run[split_at:]: engine.process_closed_bar(dict(bar))
            finish_trace(trace)
        cont_capture = root / "continuous_state_before_first_divergence.json"
        rest_capture = root / "restart_state_before_first_divergence.json"
        rerun_until(outputs(), seq, bars, None, cont_capture)
        rerun_until(outputs(), seq, bars, split, rest_capture)
        result["state_before_first_divergence"] = {"continuous": str(cont_capture), "restart": str(rest_capture)}
    return result


def split_boundary_case(module, bars, split, label):
    root = Path(tempfile.mkdtemp(prefix="btc002-split-state-"))
    paths = {key: root / f"{key}.json" for key in ("state", "zones", "events", "battles", "releases", "oi", "human", "debug", "audit")}
    continuous = module.CausalEngine(outputs=paths, reset=True, human_enabled=False, persist_each_bar=False, telemetry="none")
    continuous.write_jsonl = lambda path, value: None
    for bar in bars[:split]: continuous.process_closed_bar(dict(bar))
    continuous_state = state_before_event(continuous, {})
    snapshot = continuous.persistence_state()
    module.atomic_json(paths["state"], snapshot)
    module.atomic_json(paths["zones"], {"zones": list(snapshot["zones"].values())})
    del continuous; gc.collect()
    restarted = module.CausalEngine(outputs=paths, human_enabled=False, persist_each_bar=False, telemetry="none")
    restarted.write_jsonl = lambda path, value: None
    start = max(0, split - module.BOOTSTRAP_MINUTES - 1)
    for bar in bars[start:split]: restarted.rehydrate_bar(dict(bar))
    restart_state = state_before_event(restarted, {})
    result = {"label": label, "split": split, "bars": len(bars), "temp_root": str(root), "continuous_state": continuous_state, "restart_state": restart_state}
    (root / "split_boundary_state.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
    def make_engine():
        outputs = {key: Path(tempfile.mkdtemp(prefix="btc002-parity-")) / f"{key}.json" for key in ("state", "zones", "events", "battles", "releases", "oi", "human", "debug", "audit")}
        engine = module.CausalEngine(outputs=outputs, reset=True, human_enabled=False, persist_each_bar=False, telemetry="none"); engine.write_jsonl = lambda path, value: None
        return engine
    def run(engine, seq):
        for bar in seq: engine.process_closed_bar(dict(bar))
    continuous = make_engine(); run(continuous, bars); continuous_result = {"digest": continuous.digest(), "events": continuous.event_digest_count, "battles": continuous.profile["counts"]["battles_created"], "releases": continuous.profile["counts"]["releases_created"]}; del continuous; gc.collect()
    first = make_engine(); run(first, bars[:split]); snapshot = first.persistence_state(); module.atomic_json(first.outputs["state"], snapshot); module.atomic_json(first.outputs["zones"], {"zones": list(snapshot["zones"].values())}); outputs = first.outputs; del first; gc.collect()
    restarted = module.CausalEngine(outputs=outputs, human_enabled=False, persist_each_bar=False, telemetry="none"); restarted.write_jsonl = lambda path, value: None
    start = max(0, split - module.BOOTSTRAP_MINUTES - 1)
    for bar in bars[start:split]: restarted.rehydrate_bar(dict(bar))
    run(restarted, bars[split:]); same = continuous_result["digest"] == restarted.digest()
    return {"label": label, "split": split, "bars": len(bars), "parity": same, "continuous_digest": continuous_result["digest"], "restarted_digest": restarted.digest(), "continuous_event_count": continuous_result["events"], "restarted_event_count": restarted.event_digest_count, "continuous_battles": continuous_result["battles"], "restarted_battles": restarted.profile["counts"]["battles_created"], "continuous_releases": continuous_result["releases"], "restarted_releases": restarted.profile["counts"]["releases_created"], "first_divergent_event": None if same else "digest differs; no detector changes were made"}


def write_outputs(summary, metrics, clusters):
    result = {"population": summary, "physical_clusters": {"count": len(clusters), "raw_events": sum(x["raw_event_count"] for x in clusters), "raw_human_eligible": sum(x["human_eligible_count"] for x in clusters), "unique_physical_episodes_with_human": sum(bool(x["human_eligible_count"]) for x in clusters), "representations_per_episode": {"median": statistics.median([x["zone_representations"] for x in clusters]) if clusters else 0, "max": max((x["zone_representations"] for x in clusters), default=0)}}}
    (ROOT / "BTC_LRA_002_EVENT_POPULATION_SUMMARY.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with (ROOT / "BTC_LRA_002_BAR_CONCURRENCY.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["minute_ts", "events", "zones", "interactable_zones", "battles", "releases"]); writer.writeheader(); writer.writerows(metrics)
    with (ROOT / "BTC_LRA_002_PHYSICAL_EPISODES.jsonl").open("w", encoding="utf-8") as handle:
        for row in clusters: handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--csv", type=Path, default=ROOT / "BTC_LRA_MASTER_20260920_NOW_1M.csv"); parser.add_argument("--population", action="store_true"); parser.add_argument("--parity", action="store_true"); parser.add_argument("--diagnose-parity", action="store_true"); parser.add_argument("--split-state", action="store_true"); args = parser.parse_args()
    if not args.population and not args.parity and not args.diagnose_parity and not args.split_state: args.population = args.parity = True
    output = {}
    if args.population:
        summary, metrics, by_minute = population_audit(ROOT); clusters = physical_clusters(by_minute); write_outputs(summary, metrics, clusters); output["population"] = {"total_machine_events": summary["total_machine_events"], "top_event_types": list(summary["event_types"].items())[:10], "physical_episode_count": len(clusters), "raw_human_eligible": sum(x["human_eligible_count"] for x in clusters), "unique_physical_episodes_with_human": sum(bool(x["human_eligible_count"]) for x in clusters)}
    if args.parity:
        module = load_engine(); bars = module.replay_bars(args.csv); cases = [parity_case(module, bars[:9000], 7000, "0-9000 split 7000"), parity_case(module, bars, 11000, "0-13810 split 11000")]; (ROOT / "BTC_LRA_002_RESTART_PARITY.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8"); output["restart_parity"] = cases
    if args.diagnose_parity:
        module = load_engine(); bars = module.replay_bars(args.csv); case = diagnostic_parity_case(module, bars[:9000], 7000, "0-9000 split 7000"); (ROOT / "BTC_LRA_002_RESTART_DIVERGENCE.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8"); output["restart_divergence"] = case
    if args.split_state:
        module = load_engine(); bars = module.replay_bars(args.csv); case = split_boundary_case(module, bars[:7001], 7000, "split boundary 7000"); (ROOT / "BTC_LRA_002_SPLIT_STATE.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8"); output["split_state"] = case
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
