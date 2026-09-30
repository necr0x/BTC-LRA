"""
BTC-LRA-001 (pressure-continuation research revision)

Исследовательская версия поверх btc-lra-001.py.

001 остаётся baseline. CASE1 и CASE3 запускаются из 001 без изменений.
Изменён только pressure-слой:

    PRESSURE
        -> PRESSURE_BREAKOUT_WATCH
            -> PRESSURE_CONTINUATION
            -> PRESSURE_FAILURE
            -> NO_RESOLUTION

Ни один класс не является торговой командой.
Новые метрики собираются для historical replay/post-analysis, а не для
подгонки порогов под один эпизод.
"""

import importlib.util
import copy
import json
import math
import re
import statistics
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path


ROOT = Path(__file__).resolve().parent
BASELINE_PATH = (
    ROOT / "archive" / "code" / "script-versions"
    / "2026-09-25_21-06-16_pressure-continuation"
    / "btc-lra-001.py"
)


def configure_console_encoding():
    """Keep UTF-8 Russian output readable in Windows terminals."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


configure_console_encoding()


def load_baseline():
    spec = importlib.util.spec_from_file_location(
        "btc_lra_001_baseline",
        BASELINE_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось загрузить baseline: {BASELINE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASELINE = load_baseline()

# Архивный baseline сохраняет пути относительно своей папки. Рабочий запуск
# должен писать в общий лог и lock-файл текущей рабочей версии, а не в архив.
RUNTIME_LOG_ROOT = ROOT / "runtime" / "logs"
RESEARCH_ROOT = ROOT / "research"
BASELINE.LOG_FILE = RUNTIME_LOG_ROOT / "BTC_LRA_RESEARCH_LOG.txt"
BASELINE.LOCK_FILE = RUNTIME_LOG_ROOT / "BTC_LRA_RESEARCH_LOG.lock"

SYMBOL = BASELINE.SYMBOL
BASE = BASELINE.BASE
LOG_FILE = BASELINE.LOG_FILE
LOCK_FILE = BASELINE.LOCK_FILE

POLL_SECONDS = 5
OUTCOME_HORIZONS = (5, 15, 30, 60)
RETENTION_HORIZONS = (1, 3, 5)
EPISODE_JSONL = RESEARCH_ROOT / "pressure" / "BTC_LRA_PRESSURE_EPISODES.jsonl"
QUALITY_JSONL = RESEARCH_ROOT / "pressure" / "BTC_LRA_EVENT_QUALITY.jsonl"
TEST_ENTRY_JSONL = RESEARCH_ROOT / "legacy" / "BTC_LRA_TEST_ENTRIES.jsonl"
MARKET_EPISODE_JSONL = RESEARCH_ROOT / "legacy" / "BTC_LRA_MARKET_EPISODES.jsonl"
POSITION_STATE_JSONL = RESEARCH_ROOT / "legacy" / "BTC_LRA_POSITION_STATE.jsonl"
ACTIVE_MOVE_HEALTH_JSONL = RESEARCH_ROOT / "pressure" / "BTC_LRA_ACTIVE_MOVE_HEALTH.jsonl"
PRE_BREAKOUT_BATTLE_JSONL = RESEARCH_ROOT / "pressure" / "BTC_LRA_PRE_BREAKOUT_BATTLE.jsonl"
MOVE_ORIGIN_JSONL = RESEARCH_ROOT / "move-origin" / "BTC_LRA_MOVE_ORIGIN_TEST.jsonl"
EARLY_REVERSAL_JSONL = RESEARCH_ROOT / "move-origin" / "BTC_LRA_EARLY_REVERSAL_TEST.jsonl"
PRESSURE_POST_WATCH_TELEMETRY_JSONL = RESEARCH_ROOT / "pressure" / "BTC_LRA_PRESSURE_POST_WATCH_TELEMETRY.jsonl"
CASE1_DEBUG_LOG = RESEARCH_ROOT / "pressure" / "BTC_LRA_IMPULSE_EXHAUSTION_DEBUG.log"
CASE1_INTERNAL_JSONL = RESEARCH_ROOT / "pressure" / "BTC_LRA_IMPULSE_EXHAUSTION_CASE1.jsonl"
CASE3_DEBUG_LOG = RESEARCH_ROOT / "pressure" / "BTC_LRA_COUNTERATTACK_DEBUG.log"
CASE3_INTERNAL_JSONL = RESEARCH_ROOT / "pressure" / "BTC_LRA_COUNTERATTACK_CASE3.jsonl"
DIRECTIONAL_STATE_JSON = RESEARCH_ROOT / "move-origin" / "BTC_LRA_DIRECTIONAL_HYPOTHESIS_STATE.json"
DIRECTIONAL_HISTORY_JSONL = RESEARCH_ROOT / "move-origin" / "BTC_LRA_DIRECTIONAL_HYPOTHESIS.jsonl"
DIRECTIONAL_DEBUG_LOG = RESEARCH_ROOT / "move-origin" / "BTC_LRA_DIRECTIONAL_HYPOTHESIS_DEBUG.log"

# Research-only lifecycle values. They describe observations; they are not
# trading filters and are intentionally not optimized on one day of data.
ENTRY_OUTCOME_HORIZONS = (1, 3, 5, 10, 15, 30, 60)
VIRTUAL_SCHEMES = (
    (1.0, 1.0),
    (1.5, 1.0),
    (2.0, 1.0),
    (2.0, 1.5),
)
ACCEPTANCE_OUTSIDE_CLOSES = 2
QUALITY_SEQUENCE_GAP_SEC = 15 * 60
MARKET_EPISODE_GAP_SEC = 20 * 60
PRE_BREAKOUT_BATTLE_WINDOWS = (10, 20, 30)

# MOVE_ORIGIN_TEST is an independent research-only layer.  These are
# structural/relative research settings, not optimized trading thresholds.
MOVE_ORIGIN_OUTCOME_HORIZONS = (1, 3, 5, 15, 30, 60)
MOVE_ORIGIN_LOOKBACK_BARS = getattr(BASELINE, "PS_LOOKBACK", 60)
MOVE_ORIGIN_SEQUENCE_GAP_SEC = QUALITY_SEQUENCE_GAP_SEC
MOVE_ORIGIN_RETENTION_DROP_RATIO = 0.5
MOVE_ORIGIN_MIN_REWARD_ATR = getattr(
    BASELINE,
    "PS_MIN_FAILURE_REWARD_ATR",
    0.22,
)

# MOVE_TRANSITION_TEST reuses the existing descriptive MOVE_ORIGIN metrics and
# thresholds.  It has independent state, output, and dataset; no new market
# threshold is introduced here.
EARLY_REVERSAL_OUTCOME_HORIZONS = (1, 3, 5, 15, 30, 60)


# Coordinator-level directional hypothesis.  This is deliberately separate
# from every detector: source modules only publish completed source events.
_directional_lock = threading.RLock()
_directional_test_mode = False
_directional_state = {
    "state": "INACTIVE",
    "control": "NONE",
    "challenger": None,
    "challenge_start_time": None,
    "challenge_start_price": None,
    "hypothesis_id": None,
    "source_module": None,
    "source_event_id": None,
    "source_event_type": None,
    "source_time": None,
    "source_price": None,
    "evidence": [],
    "previous_broken_control": None,
    "previous_break_time": None,
    "previous_break_price": None,
    "last_event_time": None,
    "processed_source_ids": [],
    "next_hypothesis_number": 1,
}


def _directional_identity(event):
    source_id = event.get("source_event_id")
    if source_id and event.get("source_module") != "CASE3":
        return "|".join(str(event.get(key) or "") for key in (
            "source_module", "source_event_type", "source_event_id",
        ))
    return "|".join(str(event.get(key) or "") for key in (
        "source_module", "source_event_type", "classification", "event_time",
        "timeframe", "original_side", "counter_side", "price",
    ))


def _directional_load_state():
    if not DIRECTIONAL_STATE_JSON.exists():
        return
    try:
        saved = json.loads(DIRECTIONAL_STATE_JSON.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            _directional_state.update(saved)
    except (OSError, ValueError, TypeError):
        pass


def _directional_save_state():
    if _directional_test_mode:
        return
    temporary = DIRECTIONAL_STATE_JSON.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(_directional_state, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(DIRECTIONAL_STATE_JSON)


def _directional_write(record, debug=False):
    if _directional_test_mode:
        return
    target = DIRECTIONAL_DEBUG_LOG if debug else DIRECTIONAL_HISTORY_JSONL
    try:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        print(f"DIRECTIONAL HYPOTHESIS WRITE ERROR: {exc}", flush=True)


def _directional_stamp(event_time):
    try:
        return BASELINE.datetime.fromtimestamp(float(event_time), BASELINE.TZ).strftime("%d.%m.%y %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return BASELINE.now_local().strftime("%d.%m.%y %H:%M:%S")


def _directional_human(event_time, title, body):
    if _directional_test_mode:
        return
    block = f"[{_directional_stamp(event_time)}] {title}\n{body.rstrip()}\n\n"
    try:
        with BASELINE._lock(LOCK_FILE):
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(block)
    except OSError as exc:
        print(f"DIRECTIONAL HUMAN LOG ERROR: {exc}", flush=True)


def _directional_normalize(source_module, source_event_type, source_event_id,
                            event_time, price, evidence_side, **fields):
    try:
        event_time = float(event_time)
        if event_time > 100000000000:
            event_time /= 1000.0
    except (TypeError, ValueError):
        event_time = None
    return {
        "source_module": source_module,
        "source_event_type": source_event_type,
        "source_event_id": str(source_event_id or ""),
        "event_time": float(event_time) if event_time is not None else None,
        "price": price,
        "evidence_side": evidence_side,
        "available_at_event": fields.pop("available_at_event", True),
        **fields,
    }


def _directional_ingest_case1(payload):
    if not isinstance(payload, dict):
        return None
    candidate = payload.get("opposite_side")
    return _directional_normalize(
        "CASE1", "CASE1_FINAL_REVERSAL",
        payload.get("episode_id"), payload.get("final_event_time") or payload.get("failure_time"),
        payload.get("final_event_price") or payload.get("shock_price"), candidate,
        original_side=payload.get("shock_side"),
        counter_side=candidate, candidate_side=candidate, classification=payload.get("causal_path"),
        timeframe=payload.get("tf"),
        available_at_event=payload.get("available_at_event", True),
    )


def _directional_ingest_case3(payload):
    if not isinstance(payload, dict):
        return None
    original = "BUY" if payload.get("original_direction") == "UP" else "SELL"
    counter = "SELL" if original == "BUY" else "BUY"
    classification = payload.get("final_classification")
    terminated = original if classification in {"MOVE_TERMINATION", "REVERSAL_CANDIDATE"} else None
    candidate = counter if classification == "REVERSAL_CANDIDATE" else None
    evidence_side = original if classification in {"REBOUND_ONLY", "MOVE_TERMINATION"} else counter
    return _directional_normalize(
        "CASE3", "CASE3_FINAL_EVENT", payload.get("case3_episode_id"),
        payload.get("final_event_time"), payload.get("final_event_price"), evidence_side,
        original_side=original, counter_side=counter, terminated_side=terminated,
        candidate_side=candidate, classification=classification,
        timeframe=payload.get("tf"),
        old_side_response=payload.get("old_side_response"),
        first_counter_result=payload.get("first_counter_result"),
        second_counter_result=payload.get("second_counter_result"),
        attacks=payload.get("attacks"),
        OI_context=payload.get("OI_context"),
        available_at_event=True,
    )


def _directional_ingest_mot(signal):
    if not isinstance(signal, dict):
        return None
    direction = signal.get("direction")
    side = "BUY" if direction == "LONG" else "SELL" if direction == "SHORT" else None
    return _directional_normalize(
        "MOVE_ORIGIN", "TEST_ENTRY_CANDIDATE", signal.get("signal_id"),
        signal.get("timestamp_epoch") or signal.get("timestamp"), signal.get("price"), side,
        candidate_side=side,
        timeframe=signal.get("tf"),
        available_at_event=signal.get("evidence_available_at_signal", True),
    )


def _directional_transition(event, action, before, after, reason, human=None):
    record = {
        "record_type": "DIRECTIONAL_HYPOTHESIS_TRANSITION",
        "hypothesis_id": _directional_state.get("hypothesis_id"),
        "control": _directional_state.get("control"),
        "hypothesis_state": _directional_state.get("state"),
        "event": event,
        "transition": action,
        "state_before": before,
        "state_after": after,
        "reason": reason,
        "available_at_transition": event.get("available_at_event", True),
        "processed_at": time.time(),
    }
    _directional_state.setdefault("evidence", []).append({
        "source_module": event.get("source_module"),
        "source_event_id": event.get("source_event_id"),
        "source_event_type": event.get("source_event_type"),
        "event_time": event.get("event_time"),
        "action": action,
        "reason": reason,
        "classification": event.get("classification"),
        "original_side": event.get("original_side"),
        "counter_side": event.get("counter_side"),
        "terminated_side": event.get("terminated_side"),
        "candidate_side": event.get("candidate_side"),
        "old_side_response": event.get("old_side_response"),
        "first_counter_result": event.get("first_counter_result"),
        "second_counter_result": event.get("second_counter_result"),
    })
    _directional_write(record)
    _directional_write(record, debug=True)
    if human:
        _directional_human(event.get("event_time"), human[0], human[1])


def _directional_process(event):
    if not event or not event.get("evidence_side") or event.get("event_time") is None:
        return
    identity = _directional_identity(event)
    with _directional_lock:
        if identity in _directional_state.setdefault("processed_source_ids", []):
            return
        last_time = _directional_state.get("last_event_time")
        if last_time is not None and event["event_time"] < last_time:
            _directional_state["processed_source_ids"].append(identity)
            _directional_write({"event": event, "classification": "LATE_HISTORICAL_EVIDENCE"}, debug=True)
            _directional_save_state()
            return
        _directional_state["processed_source_ids"].append(identity)
        before = {"state": _directional_state.get("state"), "control": _directional_state.get("control"), "challenger": _directional_state.get("challenger")}
        state = _directional_state["state"]
        control = _directional_state["control"]
        side = event["evidence_side"]
        classification = event.get("classification")
        action = "OBSERVE_ONLY"
        reason = "source event observed without sufficient coordinator semantics"
        human = None

        is_case3 = event["source_module"] == "CASE3"
        if state == "INACTIVE":
            if event["source_module"] == "CASE1" or (event["source_module"] == "CASE3" and classification == "REVERSAL_CANDIDATE"):
                _directional_state.update({"state": "ACTIVE", "control": side, "challenger": None,
                    "hypothesis_id": f"DH{_directional_state.get('next_hypothesis_number', 1):06d}",
                    "next_hypothesis_number": _directional_state.get("next_hypothesis_number", 1) + 1,
                    "source_module": event["source_module"], "source_event_id": event["source_event_id"],
                    "source_event_type": event["source_event_type"], "source_time": event["event_time"],
                    "source_price": event.get("price"), "evidence": []})
                action = "SET_CONTROL"
                reason = "completed reversal evidence established a provisional control"
                previous = _directional_state.get("previous_broken_control")
                if previous and previous != side:
                    human = (f"ВОЗМОЖНАЯ СМЕНА {previous} → {side} | {event.get('price')}",
                             f"{side} получил подтверждение после провала {previous}.")
                else:
                    human = (f"ВОЗМОЖНАЯ ТВХ {side} | {event.get('price')}",
                             f"{side} получил подтверждённый результат.")
        elif state == "ACTIVE":
            # CASE3 has explicit causal semantics.  Interpret them before
            # applying the generic same/opposite-side relationship.
            if is_case3 and classification == "MOVE_TERMINATION":
                if event.get("terminated_side") == control:
                    _directional_state.update({"state": "CHALLENGED", "challenger": event.get("counter_side"),
                        "challenge_start_time": event["event_time"], "challenge_start_price": event.get("price")})
                    action = "CHALLENGE_CONTROL"
                    reason = "CASE3 MOVE_TERMINATION challenges matching terminated_side"
                else:
                    action = "OBSERVE_ONLY"
                    reason = "CASE3 MOVE_TERMINATION refers to another terminated_side"
            elif is_case3 and classification == "REBOUND_ONLY":
                if event.get("original_side") == control:
                    action = "SUPPORT_CONTROL"
                    reason = "CASE3 REBOUND_ONLY reports restoration of the active original side"
                else:
                    action = "OBSERVE_ONLY"
                    reason = "CASE3 REBOUND_ONLY refers to another original side"
            elif is_case3 and classification == "REVERSAL_CANDIDATE":
                if event.get("candidate_side") == control:
                    action = "SUPPORT_CONTROL"
                    reason = "CASE3 candidate_side supports the active control"
                else:
                    _directional_state.update({"state": "CHALLENGED", "challenger": event.get("candidate_side"),
                        "challenge_start_time": event["event_time"], "challenge_start_price": event.get("price")})
                    action = "CHALLENGE_CONTROL"
                    reason = "CASE3 candidate_side challenges the active control"
            elif side == control:
                action = "SUPPORT_CONTROL"
                reason = "same-side evidence supports the active control"
                if event["source_module"] == "MOVE_ORIGIN":
                    human = (f"ВОЗМОЖНАЯ ТВХ {side} | {event.get('price')}", f"{side} повторно получил результат, контроль сохраняется.")
            else:
                _directional_state.update({"state": "CHALLENGED", "challenger": side,
                    "challenge_start_time": event["event_time"], "challenge_start_price": event.get("price")})
                action = "CHALLENGE_CONTROL"
                reason = "opposite evidence challenges active control"
        else:  # CHALLENGED
            if is_case3 and classification == "REBOUND_ONLY" and event.get("original_side") == control:
                _directional_state.update({"state": "ACTIVE", "challenger": None, "challenge_start_time": None, "challenge_start_price": None})
                action = "OLD_SIDE_RESTORED"
                reason = "CASE3 REBOUND_ONLY provides explicit old-side restoration evidence"
            elif is_case3 and classification == "REVERSAL_CANDIDATE" and event.get("candidate_side") == control and event.get("terminated_side") != control:
                _directional_state.update({"state": "ACTIVE", "challenger": None, "challenge_start_time": None, "challenge_start_price": None})
                action = "OLD_SIDE_RESTORED"
                reason = "CASE3 candidate_side provides stronger same-side restoration evidence"
            elif event["source_module"] == "CASE1" and side == control:
                _directional_state.update({"state": "ACTIVE", "challenger": None, "challenge_start_time": None, "challenge_start_price": None})
                action = "OLD_SIDE_RESTORED"
                reason = "CASE1 final evidence restores the challenged control"
            elif is_case3 and classification in {"MOVE_TERMINATION", "REVERSAL_CANDIDATE"} and event.get("terminated_side") == control and event.get("counter_side") == _directional_state.get("challenger"):
                _directional_state.update({"state": "INACTIVE", "control": "NONE", "challenger": None,
                    "previous_broken_control": control, "previous_break_time": event["event_time"], "previous_break_price": event.get("price")})
                action = "CONTROL_BROKEN"
                reason = "CASE3 causal evidence confirms failure of challenged terminated_side"
                human = (f"{control}-КОНТРОЛЬ СЛОМАН | {event.get('price')}", f"{control} не смог восстановить результат после встречного {side}.")
            else:
                action = "CHALLENGE_UNRESOLVED"
                reason = "no existing source event proves restoration or break"

        after = {"state": _directional_state.get("state"), "control": _directional_state.get("control"), "challenger": _directional_state.get("challenger")}
        _directional_transition(event, action, before, after, reason, human)
        _directional_state["last_event_time"] = event["event_time"]
        _directional_save_state()


def _directional_process_test(event):
    """Run a synthetic hypothesis event with zero production side effects."""
    global _directional_test_mode
    snapshot = copy.deepcopy(_directional_state)
    _directional_test_mode = True
    try:
        _directional_process(event)
    finally:
        _directional_state.clear()
        _directional_state.update(snapshot)
        _directional_test_mode = False


def _directional_hypothesis_event(source_module, payload):
    if source_module == "CASE1":
        _directional_process(_directional_ingest_case1(payload))
    elif source_module == "CASE3":
        _directional_process(_directional_ingest_case3(payload))


_directional_load_state()
try:
    _directional_save_state()
    DIRECTIONAL_HISTORY_JSONL.touch(exist_ok=True)
    DIRECTIONAL_DEBUG_LOG.touch(exist_ok=True)
except OSError:
    pass

MOVE_TRANSITION_STARTUP_TEXT = (
    "Модуль «Переход движения» исследует ухудшение исходного движения "
    "и первый встречный результат. Это исследовательские наблюдения, "
    "а не торговые входы. Стадии: предупреждение остановки, первый "
    "встречный результат, кандидат направления и подтверждение направления."
)
MOVE_ORIGIN_STARTUP_TEXT = (
    "Модуль «Происхождение движения» исследует ранний кандидат по смене "
    "соотношения усилия → результата. Это только исследовательские "
    "наблюдения, не торговые приказы."
)

COORDINATOR_STARTUP_TEXT = (
    "- \u041c\u043e\u0434\u0443\u043b\u044c \u00ab\u0418\u043c\u043f\u0443\u043b\u044c\u0441 \u0438 \u0438\u0441\u0442\u043e\u0449\u0435\u043d\u0438\u0435\u00bb \u0441\u043b\u0435\u0434\u0438\u0442 \u0437\u0430 \u0441\u0438\u043b\u044c\u043d\u043e\u0439 \u0430\u0433\u0440\u0435\u0441\u0441\u0438\u0435\u0439, \u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u0435\u043c \u0438 \u0435\u0451 \u0438\u0441\u0442\u043e\u0449\u0435\u043d\u0438\u0435\u043c.\n\n"
    "- \u041c\u043e\u0434\u0443\u043b\u044c \u00ab\u0412\u0441\u0442\u0440\u0435\u0447\u043d\u0430\u044f \u0430\u0442\u0430\u043a\u0430\u00bb \u043f\u0440\u043e\u0432\u0435\u0440\u044f\u0435\u0442, \u043f\u043e\u043b\u0443\u0447\u0430\u0435\u0442 \u043b\u0438 \u043f\u0440\u043e\u0442\u0438\u0432\u043e\u043f\u043e\u043b\u043e\u0436\u043d\u0430\u044f \u0441\u0442\u043e\u0440\u043e\u043d\u0430 \u0440\u0435\u0430\u043b\u044c\u043d\u044b\u0439 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442.\n\n"
    "- \u041c\u043e\u0434\u0443\u043b\u044c \u00ab\u0414\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u0438 \u0441\u0432\u0438\u043f\u00bb \u0438\u0441\u0441\u043b\u0435\u0434\u0443\u0435\u0442 \u043f\u043e\u0441\u043b\u0435\u0434\u043e\u0432\u0430\u0442\u0435\u043b\u044c\u043d\u043e\u0435 \u0434\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u0438 \u0444\u0438\u043d\u0430\u043b\u044c\u043d\u0443\u044e \u043f\u043e\u043f\u044b\u0442\u043a\u0443 \u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u044f.\n\n"
    "- \u041c\u043e\u0434\u0443\u043b\u044c \u00ab\u041f\u0440\u043e\u0438\u0441\u0445\u043e\u0436\u0434\u0435\u043d\u0438\u0435 \u0434\u0432\u0438\u0436\u0435\u043d\u0438\u044f\u00bb \u0438\u0441\u0441\u043b\u0435\u0434\u0443\u0435\u0442 \u0440\u0430\u043d\u043d\u0438\u0439 \u043a\u0430\u043d\u0434\u0438\u0434\u0430\u0442 \u043f\u043e \u0441\u043c\u0435\u043d\u0435 \u0441\u043e\u043e\u0442\u043d\u043e\u0448\u0435\u043d\u0438\u044f \u0443\u0441\u0438\u043b\u0438\u044f \u2192 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u0430."
)

# Существующие исследовательские пороги 001 не меняем.
WATCH_TTL_SEC = BASELINE.PS_SWEEP_TTL_SEC
BREAKOUT_ATR = BASELINE.PS_MIN_SWEEP_ATR

_state_lock = threading.Lock()
_baseline_lock_original = BASELINE._lock
_baseline_lock_thread_guard = threading.RLock()


def _thread_safe_baseline_lock(lock_path, timeout=5.0):
    """Serialize shared-log locking within this process on Windows.

    The baseline lock is cross-process aware, but msvcrt.locking can raise
    OSError(22) when several in-process engine threads unlock the same byte at
    once.  This wrapper only protects file I/O; it does not alter detector
    state, thresholds, or datasets.
    """
    original_context = _baseline_lock_original(lock_path, timeout)

    class _GuardedLock:
        def __init__(self):
            self.os_lock_acquired = False

        def __enter__(self):
            _baseline_lock_thread_guard.acquire()
            try:
                try:
                    original_context.__enter__()
                    self.os_lock_acquired = True
                except OSError as error:
                    # Windows can report ERROR_INVALID_PARAMETER when the
                    # shared byte lock is contended by another in-process
                    # writer.  The RLock above still serializes this process;
                    # continue with append-only I/O instead of killing the
                    # 5-second research loop.
                    if getattr(error, "winerror", None) != 87 and getattr(error, "errno", None) != 22:
                        raise
                return self
            except Exception:
                _baseline_lock_thread_guard.release()
                raise

        def __exit__(self, exc_type, exc, tb):
            try:
                if not self.os_lock_acquired:
                    return False
                try:
                    return original_context.__exit__(exc_type, exc, tb)
                except OSError as error:
                    if getattr(error, "winerror", None) == 87 or getattr(error, "errno", None) == 22:
                        return True
                    raise
            finally:
                _baseline_lock_thread_guard.release()

    return _GuardedLock()


BASELINE._lock = _thread_safe_baseline_lock
_episodes = []
_active_watch = None
_pressure_post_watch_windows = []
_pressure_context = None
_last_context_key = None
_last_price = None
_episode_number = 0

_quality_lock = threading.Lock()
_quality_event_number = 0
_test_entry_number = 0
_market_episode_number = 0
_exit_anchor_number = 0
_battle_number = 0
_quality_records = []
_test_entries = []
_exit_anchors = []
_market_episodes = []
_case3_legs = []
_case3_counters = []
_case1_exhaustions = []
_active_move_health = []
_active_move_number = 0
_processed_log_events = set()
_position_last_sample = 0.0
_position_state = None
_last_oi = None
_last_atr = None
_oi_history = deque(maxlen=5000)

# MOVE_ORIGIN_TEST state is deliberately separate from all existing detector
# state and datasets.  The first live call seeds context but never backfills
# a signal from already closed historical bars.
_move_origin_signal_number = 0
_move_origin_sequence_number = 0
_move_origin_signals = []
_move_origin_loaded = False
_move_origin_seeded = False
_move_origin_last_closed_bar_ts = None
_move_origin_last_oi = None
_move_origin_history = deque(maxlen=max(MOVE_ORIGIN_LOOKBACK_BARS * 2, 120))
_move_origin_states = {
    "LONG": {"stage": "WAIT_EFFECTIVE_ATTACK", "sequence_id": None, "stages": {}, "transitions": []},
    "SHORT": {"stage": "WAIT_EFFECTIVE_ATTACK", "sequence_id": None, "stages": {}, "transitions": []},
}

# Independent experimental early-reversal layer.  It never reads or mutates
# MOVE_ORIGIN_TEST state and only advances on newly closed 1m bars.
_early_reversal_signal_number = 0
_early_reversal_sequence_number = 0
_early_reversal_signals = []
_early_reversal_loaded = False
_early_reversal_seeded = False
_early_reversal_last_closed_bar_ts = None
_early_reversal_last_oi = None
_early_reversal_states = {
    "LONG": {"stage": "WAIT_OLD_EFFECTIVE", "sequence_id": None, "stages": {}, "transitions": []},
    "SHORT": {"stage": "WAIT_OLD_EFFECTIVE", "sequence_id": None, "stages": {}, "transitions": []},
}
_early_reversal_direct_states = {
    "LONG": {"stage": "WAIT_OLD_STRONG_FINAL_IMPULSE", "sequence_id": None, "stages": {}, "transitions": []},
    "SHORT": {"stage": "WAIT_OLD_STRONG_FINAL_IMPULSE", "sequence_id": None, "stages": {}, "transitions": []},
}

# Descriptive research layer above EARLY_REVERSAL_TEST.  It observes the
# existing ERT events; it does not alter their detector or emit human alerts.
EFFORT_RESULT_BATTLE_JSONL = RESEARCH_ROOT / "effort-transfer" / "BTC_LRA_EFFORT_RESULT_BATTLE.jsonl"
EFFORT_RESULT_BATTLE_DEBUG = RESEARCH_ROOT / "effort-transfer" / "BTC_LRA_EFFORT_RESULT_BATTLE_DEBUG.log"
BATTLE_IDLE_TIMEOUT_SEC = 15 * 60
_battle_episode_number = 0
_battle_active = None
_battle_seed = None


def _battle_side_from_signal(signal):
    return "BUY" if signal.get("direction") == "LONG" else "SELL"


def _battle_float(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _battle_attack_from_signal(signal):
    side = _battle_side_from_signal(signal)
    volume = _battle_float(signal.get("opposite_volume"))
    delta = _battle_float(signal.get("opposite_delta"))
    buy_volume = max(0.0, (volume + delta) / 2.0)
    sell_volume = max(0.0, (volume - delta) / 2.0)
    aggressive_volume = buy_volume if side == "BUY" else sell_volume
    extension = _battle_float(signal.get("opposite_price_progress"))
    retained = _battle_float(signal.get("opposite_retained_reward"))
    retention = signal.get("opposite_retained_fraction")
    if retention is None:
        retention = retained / extension if extension > 0 else 0.0
    return {
        "signal_id": signal.get("signal_id"),
        "timestamp": signal.get("timestamp"),
        "timestamp_epoch": _battle_float(signal.get("timestamp_epoch")),
        "direction": signal.get("direction"),
        "old_side": signal.get("old_side"),
        "new_side": side,
        "causal_branch": signal.get("causal_branch"),
        "price": signal.get("price"),
        "volume": volume,
        "total_volume": volume,
        "aggressive_volume": aggressive_volume,
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "delta": delta,
        "abs_delta": abs(delta),
        "delta_share": abs(delta) / volume if volume else 0.0,
        "atr": signal.get("atr"),
        "directional_extension": extension,
        "price_result": extension,
        "retained_result": retained,
        "retention_fraction": _battle_float(retention),
        "result_per_100BTC_volume": extension / aggressive_volume * 100 if aggressive_volume else 0.0,
        "retained_result_per_100BTC_volume": retained / aggressive_volume * 100 if aggressive_volume else 0.0,
        "result_per_abs_delta": extension / abs(delta) if delta else 0.0,
        "retained_result_per_abs_delta": retained / abs(delta) if delta else 0.0,
        "relative_volume": signal.get("relative_volume"),
        "oi": signal.get("current_oi"),
        "dOI": signal.get("oi_change"),
        "candle_progress": signal.get("candle_progress"),
        "available_at_event": signal.get("evidence_available_at_signal", {}),
    }


def _battle_write(record, debug=False):
    path = EFFORT_RESULT_BATTLE_DEBUG if debug else EFFORT_RESULT_BATTLE_JSONL
    try:
        if debug:
            line = json.dumps(record, ensure_ascii=False, default=str)
        else:
            line = json.dumps(record, ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception as exc:
        print(f"BATTLE DATA ERROR: {type(exc).__name__}: {exc}", flush=True)


def _battle_metrics(episode):
    result = {}
    for side in ("BUY", "SELL"):
        attacks = [item for item in episode["attacks"] if item["new_side"] == side]
        volume = sum(_battle_float(item.get("aggressive_volume")) for item in attacks)
        delta = sum(_battle_float(item.get("delta")) for item in attacks)
        raw_result = sum(_battle_float(item.get("price_result")) for item in attacks)
        retained = sum(_battle_float(item.get("retained_result")) for item in attacks)
        result[side] = {
            "attack_count": len(attacks),
            "cumulative_aggressive_volume": volume,
            "cumulative_delta": delta,
            "cumulative_price_result": raw_result,
            "cumulative_retained_result": retained,
            "result_per_100BTC": raw_result / volume * 100 if volume else 0.0,
            "retained_result_per_100BTC": retained / volume * 100 if volume else 0.0,
            "efficiency_evolution": [
                {
                    "timestamp": item.get("timestamp"),
                    "retained_result": item.get("retained_result"),
                    "retention_fraction": item.get("retention_fraction"),
                    "retained_result_per_100BTC": item.get("retained_result_per_100BTC_volume"),
                }
                for item in attacks
            ],
        }
    return result


def _battle_snapshot(episode):
    snapshot = dict(episode)
    snapshot["buy_metrics"] = _battle_metrics(episode)["BUY"]
    snapshot["sell_metrics"] = _battle_metrics(episode)["SELL"]
    return snapshot


def _battle_finalize(episode, state, reason, timestamp_epoch):
    episode["final_state"] = state
    episode["end_time"] = fmt_ts(timestamp_epoch)
    episode["end_time_epoch"] = timestamp_epoch
    episode["close_reason"] = reason
    episode["metrics_at_close"] = _battle_metrics(episode)
    _battle_write({"record_type": "BATTLE_EPISODE", **_battle_snapshot(episode)})


def _battle_expire_if_needed(timestamp_epoch):
    global _battle_active, _battle_seed
    current = _battle_active or _battle_seed
    if not current:
        return
    last = _battle_float(current.get("last_event_time_epoch"))
    if last and timestamp_epoch - last > BATTLE_IDLE_TIMEOUT_SEC:
        _battle_finalize(current, "EXPIRED", "NO_RELATED_ERT_WITHIN_TIMEOUT", timestamp_epoch)
        _battle_active = None
        _battle_seed = None


def _battle_add_bar(bar, oi_meta, atr):
    timestamp_epoch = _battle_float(bar.get("ts")) / 1000.0
    for episode in tuple(item for item in (_battle_active, _battle_seed) if item):
        if timestamp_epoch <= _battle_float(episode.get("last_bar_time_epoch")):
            continue
        episode["bars"].append({
            "timestamp": fmt_ts(timestamp_epoch),
            "timestamp_epoch": timestamp_epoch,
            "open": bar.get("open"), "high": bar.get("high"),
            "low": bar.get("low"), "close": bar.get("close"),
            "volume": bar.get("volume"), "delta": bar.get("delta"),
            "price_change": _battle_float(bar.get("close")) - _battle_float(bar.get("open")),
            "atr": atr,
            "oi": oi_meta.get("current_oi"), "dOI": oi_meta.get("oi_change"),
        })
        episode["last_bar_time_epoch"] = timestamp_epoch


def _battle_classify_advantage(episode):
    attacks = episode["attacks"]
    if len(attacks) < 3:
        return None, "INSUFFICIENT_SEQUENCE"
    latest = attacks[-1]
    previous = attacks[-2]
    before_previous = attacks[-3]
    if latest["new_side"] != before_previous["new_side"] or latest["new_side"] == previous["new_side"]:
        return None, "NO_A_B_A_SEQUENCE"
    side = latest["new_side"]
    opposite = previous["new_side"]
    latest_retained = _battle_float(latest.get("retained_result"))
    opposite_retained = _battle_float(previous.get("retained_result"))
    latest_eff = _battle_float(latest.get("retained_result_per_100BTC_volume"))
    opposite_eff = _battle_float(previous.get("retained_result_per_100BTC_volume"))
    strictly_better = (
        latest_eff > opposite_eff and latest_retained >= opposite_retained
    ) or (
        latest_retained > opposite_retained and latest_eff >= opposite_eff
    )
    if latest_retained > 0 and strictly_better:
        return side, "SIDE_A_RESULT_AFTER_SIDE_B_RESPONSE"
    return None, "COMPARABLE_OR_WEAKER_RESULT"


def _battle_on_bar(bar, oi_meta, atr):
    _battle_expire_if_needed(_battle_float(bar.get("ts")) / 1000.0)
    if _battle_active or _battle_seed:
        _battle_add_bar(bar, oi_meta, atr)


def _battle_on_ert(signal, bar=None, oi_meta=None, atr=None):
    global _battle_episode_number, _battle_active, _battle_seed
    timestamp_epoch = _battle_float(signal.get("timestamp_epoch"))
    _battle_expire_if_needed(timestamp_epoch)
    attack = _battle_attack_from_signal(signal)
    current = _battle_active or _battle_seed
    if current is None:
        _battle_episode_number += 1
        current = {
            "record_type": "BATTLE_EPISODE_STATE",
            "battle_episode_id": f"BATTLE{_battle_episode_number:06d}",
            "start_time": signal.get("timestamp"),
            "start_time_epoch": timestamp_epoch,
            "end_time": None,
            "seed_side": attack["new_side"],
            "all_ERT_event_ids": [], "attacks": [], "bars": [],
            "reattack_sequence": [], "state_transitions": [],
            "state": "BATTLE_SEED", "final_state": None,
            "advantage_side": None, "advantage_timestamp": None,
            "reason_components": [], "available_at_advantage": None,
            "last_event_time_epoch": timestamp_epoch,
            "last_bar_time_epoch": timestamp_epoch - 1,
        }
        _battle_seed = current
        _battle_write({"record_type": "BATTLE_STATE", "event": "BATTLE_SEED", **_battle_snapshot(current)}, debug=True)
    current["all_ERT_event_ids"].append(signal.get("signal_id"))
    current["attacks"].append(attack)
    current["last_event_time_epoch"] = timestamp_epoch
    if bar is not None:
        _battle_add_bar(current, oi_meta or {}, atr)
    current["last_bar_time_epoch"] = max(current.get("last_bar_time_epoch", 0), timestamp_epoch)
    current["metrics"] = _battle_metrics(current)
    if len(current["attacks"]) >= 2 and current["state"] == "BATTLE_SEED":
        current["state"] = "BATTLE_ACTIVE"
        current["state_transitions"].append({"timestamp": signal.get("timestamp"), "state": "BATTLE_ACTIVE"})
    if len(current["attacks"]) >= 2:
        previous = current["attacks"][-2]
        response = "REATTACK_WITH_REWARD" if attack["retained_result"] > 0 else "REATTACK_WITHOUT_REWARD"
        if attack["retained_result"] > 0 and attack["retention_fraction"] < 0.5:
            response = "REATTACK_WITH_POOR_RETENTION"
        current["reattack_sequence"].append({
            "response_side": attack["new_side"], "after_side": previous["new_side"],
            "timestamp": attack["timestamp"], "classification": response,
            "effort": attack["aggressive_volume"], "result": attack["price_result"],
            "retained_result": attack["retained_result"],
        })
    advantage, reason = _battle_classify_advantage(current)
    if advantage:
        current["advantage_side"] = advantage
        current["advantage_timestamp"] = attack["timestamp"]
        current["state"] = f"{advantage}_ADVANTAGE"
        current["reason_components"] = [reason, "retained_result_positive", "response_side_failed_to_exceed_result"]
        current["available_at_advantage"] = {
            "signal_id": signal.get("signal_id"), "timestamp": signal.get("timestamp"),
            "no_future_data_used": True, "metrics": _battle_metrics(current),
        }
    _battle_write({"record_type": "BATTLE_EVENT", "event": "ERT_ATTACHED", "battle_episode_id": current["battle_episode_id"], "attack": attack, "state": current["state"], "metrics": _battle_metrics(current)}, debug=True)
    if _battle_seed is current and len(current["attacks"]) >= 2:
        _battle_active = current
        _battle_seed = None


def now_ts():
    return time.time()


def fmt_ts(ts):
    return BASELINE.datetime.fromtimestamp(
        ts,
        BASELINE.timezone.utc,
    ).astimezone(BASELINE.TZ).strftime("%Y-%m-%d %H:%M:%S")


def side_sign(side):
    return 1.0 if side == "UP" else -1.0


def side_direction(side):
    return "LONG" if side == "UP" else "SHORT"


def side_word(side):
    return "покупатели" if side == "UP" else "продавцы"


def oriented_progress(side, start, value):
    return side_sign(side) * (value - start)


def current_price_from_bar(bar):
    return float(bar["close"])


def log_event(kind, body):
    BASELINE.coordinator_log(kind, body)


def _write_pressure_human_line(event_time, side, price):
    """Write only the compact PRESSURE_DETECTED human observation.

    PRESSURE runs in its own worker thread.  It must not depend on the
    coordinator thread having already installed its temporary log adapter.
    This is presentation routing only; the detector and research records are
    unchanged.
    """
    stamp = BASELINE.datetime.fromtimestamp(
        float(event_time), BASELINE.TZ
    ).strftime("%d.%m.%y %H:%M:%S")
    label = "ДАВЛЕНИЕ ПОКУПАТЕЛЕЙ" if side == "UP" else "ДАВЛЕНИЕ ПРОДАВЦОВ"
    block = f"[{stamp}] {label} | {float(price):.1f}\n\n"
    try:
        with BASELINE._lock(LOCK_FILE):
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(block)
    except OSError as exc:
        print(f"PRESSURE HUMAN LOG ERROR: {exc}", flush=True)


def attack_record(
    bar,
    pressure_side,
    atr,
    reference,
    oi_change=None,
    oi_change_pct=None,
):
    """Сохраняет одну атаку без вывода о стороне позиции."""
    if bar["delta"] > 0:
        attack_side = "UP"
        progress = max(0.0, bar["high"] - bar["open"])
        retained = max(0.0, bar["close"] - bar["open"])
    elif bar["delta"] < 0:
        attack_side = "DOWN"
        progress = max(0.0, bar["open"] - bar["low"])
        retained = max(0.0, bar["open"] - bar["close"])
    else:
        attack_side = "FLAT"
        progress = 0.0
        retained = 0.0

    abs_delta = abs(bar["delta"])
    extension = max(
        0.0,
        oriented_progress(
            pressure_side,
            reference,
            bar["high"] if pressure_side == "UP" else bar["low"],
        ),
    )
    pressure_retained = max(
        0.0,
        oriented_progress(pressure_side, reference, bar["close"]),
    )
    return {
        "time": fmt_ts(bar["ts"] / 1000.0),
        "timestamp": bar["ts"] / 1000.0,
        "direction": attack_side,
        "delta": bar["delta"],
        "volume": bar["volume"],
        "delta_volume_ratio": abs(bar["delta"]) / bar["volume"] if bar["volume"] else 0.0,
        "directional_progress": progress,
        "directional_progress_atr": progress / atr if atr else 0.0,
        "efficiency": progress / atr / abs_delta if atr and abs_delta else 0.0,
        "efficiency_per_100_delta": progress / atr * 100.0 / abs_delta if atr and abs_delta else 0.0,
        "extension_reward": extension,
        "retained_reward": pressure_retained,
        "retained_at_close": pressure_retained,
        "oi_change": oi_change,
        "dOI": oi_change,
        "dOI_pct": oi_change_pct,
        "oi_source": "live_monitor_5m" if oi_change is not None else None,
    }


def append_jsonl(kind, episode, extra=None):
    """Append-only research dataset; never used as a signal."""
    record = {
        "recorded_at": fmt_ts(now_ts()),
        "event": kind,
        "episode_id": episode["id"],
        "class": episode.get("class"),
        "pressure_direction": episode.get("side"),
        "direction": episode.get("direction"),
        "pressure_detected_time": episode.get("pressure_detected_time"),
        "pressure_reference_price": episode.get("pressure_reference_price"),
        "pressure_attack_count": episode.get("pressure_attack_count"),
        "pressure_cumulative_delta": episode.get("pressure_cumulative_delta"),
        "pressure_total_volume": episode.get("pressure_total_volume"),
        "pressure_delta_volume_ratio": episode.get("pressure_delta_volume_ratio"),
        "oi_context": episode.get("oi_context"),
        "market_episode_id": episode.get("market_episode_id"),
        "quality_context": episode.get("quality_context"),
        "latest_extreme": episode.get("latest_extreme"),
        "counter_events": episode.get("counter_events", []),
        "attacks": episode.get("attacks", []),
    }
    if extra:
        record.update(extra)
    with BASELINE._lock(LOCK_FILE):
        with EPISODE_JSONL.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def append_dataset(path, record):
    """Append one machine-readable research record under the shared lock."""
    with BASELINE._lock(LOCK_FILE):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _pressure_post_watch_side_metrics(side, attempt_price, bar):
    """Symmetric, bar-close-only side metrics for post-WATCH research."""
    volume = as_float(bar.get("volume"), 0.0) or 0.0
    delta = as_float(bar.get("delta"), 0.0) or 0.0
    buy_effort = max(0.0, (volume + delta) / 2.0)
    sell_effort = max(0.0, (volume - delta) / 2.0)
    effort = buy_effort if side == "BUY" else sell_effort
    if side == "BUY":
        extension = max(0.0, as_float(bar.get("high"), attempt_price) - attempt_price)
        retained = max(0.0, as_float(bar.get("close"), attempt_price) - attempt_price)
    else:
        extension = max(0.0, attempt_price - as_float(bar.get("low"), attempt_price))
        retained = max(0.0, attempt_price - as_float(bar.get("close"), attempt_price))
    return {
        "side": side,
        "effort_volume": effort,
        "aggressive_volume": effort,
        "delta": delta if side == "BUY" else -delta,
        "extension_reward": extension,
        "price_reward": extension,
        "retained_reward": retained,
        "retention_fraction": retained / extension if extension > 0 else 0.0,
        "result_per_100BTC_effort": extension / effort * 100.0 if effort else 0.0,
        "retained_result_per_100BTC_effort": retained / effort * 100.0 if effort else 0.0,
    }


def _pressure_post_watch_state(window):
    if window.get("failure_candidate"):
        return "PRESSURE_FAILURE_CANDIDATE"
    if window.get("acceptance"):
        return "PRESSURE_ACCEPTANCE"
    if window.get("continuation_candidate"):
        return "PRESSURE_CONTINUATION_CANDIDATE"
    return "PRESSURE_BREAKOUT_WATCH"


def _pressure_post_watch_record(
    window,
    bar,
    oi,
    doi,
    doi_pct,
    record_type,
    writer=None,
    window_status="ACTIVE",
):
    pressure_side = window["pressure_side"]
    opposite_side = "SELL" if pressure_side == "BUY" else "BUY"
    attempt_price = window["attempt_price"]
    same = _pressure_post_watch_side_metrics(pressure_side, attempt_price, bar)
    opposite = _pressure_post_watch_side_metrics(opposite_side, attempt_price, bar)

    previous_pressure_max = window["max_pressure_progress"]
    previous_counter_max = window["max_counter_progress"]
    pressure_progress = same["extension_reward"]
    counter_progress = opposite["extension_reward"]
    same["incremental_reward"] = max(0.0, pressure_progress - previous_pressure_max)
    opposite["incremental_reward"] = max(0.0, counter_progress - previous_counter_max)

    high = as_float(bar.get("high"), attempt_price)
    low = as_float(bar.get("low"), attempt_price)
    if pressure_side == "BUY":
        pressure_new_extreme = high > window["pressure_extreme"]
        counter_new_extreme = low < window["counter_extreme"]
    else:
        pressure_new_extreme = low < window["pressure_extreme"]
        counter_new_extreme = high > window["counter_extreme"]

    if pressure_new_extreme:
        window["pressure_extreme"] = high if pressure_side == "BUY" else low
    if counter_new_extreme:
        window["counter_extreme"] = low if pressure_side == "BUY" else high
    window["max_pressure_progress"] = max(previous_pressure_max, pressure_progress)
    window["max_counter_progress"] = max(previous_counter_max, counter_progress)

    record = {
        "record_type": record_type,
        "window_status": window_status,
        "window_completed": window_status == "COMPLETED",
        "recorded_at": fmt_ts(now_ts()),
        "event_time": fmt_ts(bar["ts"] / 1000.0),
        "event_timestamp": bar["ts"] / 1000.0,
        "available_at_event": True,
        "episode_id": window["episode_id"],
        "watch_event_time": window["watch_event_time"],
        "watch_event_timestamp": window["watch_event_timestamp"],
        "watch_processed_at": window["watch_processed_at"],
        "watch_attempt_price": attempt_price,
        "pressure_side": pressure_side,
        "opposite_side": opposite_side,
        "ohlc": {
            "open": bar.get("open"),
            "high": bar.get("high"),
            "low": bar.get("low"),
            "close": bar.get("close"),
        },
        "volume": bar.get("volume"),
        "delta": bar.get("delta"),
        "oi": oi,
        "dOI": doi,
        "dOI_pct": doi_pct,
        "same_side": same,
        "opposite_side_metrics": opposite,
        "new_pressure_extreme": pressure_new_extreme,
        "new_opposite_extreme": counter_new_extreme,
        "distance_from_watch_attempt_price": as_float(bar.get("close"), attempt_price) - attempt_price,
        "absolute_distance_from_watch_attempt_price": abs(as_float(bar.get("close"), attempt_price) - attempt_price),
        "max_pressure_progress": window["max_pressure_progress"],
        "max_counter_progress": window["max_counter_progress"],
        "existing_pressure_state": _pressure_post_watch_state(window),
        "existing_pressure_event": _pressure_post_watch_state(window),
    }
    (writer or append_dataset)(PRESSURE_POST_WATCH_TELEMETRY_JSONL, record)


def _start_pressure_post_watch_window(watch, windows=None, writer=None):
    bar = watch["bars"][0]
    pressure_side = "BUY" if watch["side"] == "UP" else "SELL"
    window = {
        "episode_id": watch["episode"]["id"],
        "pressure_side": pressure_side,
        "opposite_side": "SELL" if pressure_side == "BUY" else "BUY",
        "watch_event_time": fmt_ts(watch["attempt_bar_time"]),
        "watch_event_timestamp": watch["attempt_bar_time"],
        "watch_processed_at": fmt_ts(watch["attempt_time"]),
        "attempt_price": watch["attempt_price"],
        "attempt_bar_timestamp": bar["ts"],
        # Bar timestamps are milliseconds; keep the observation-window
        # boundary in the same unit as closed-bar ``ts`` values.
        "window_end_timestamp": bar["ts"] + 10 * 60 * 1000,
        "last_recorded_bar_timestamp": None,
        "pressure_extreme": watch["attempt_price"],
        "counter_extreme": watch["attempt_price"],
        "max_pressure_progress": 0.0,
        "max_counter_progress": 0.0,
        "continuation_candidate": False,
        "failure_candidate": False,
        "acceptance": False,
        "completed": False,
        "seen_bar_timestamps": set(),
    }
    (windows if windows is not None else _pressure_post_watch_windows).append(window)
    _pressure_post_watch_record(
        window,
        bar,
        watch.get("oi_at_attempt"),
        None,
        None,
        "PRESSURE_POST_WATCH_BASELINE",
        writer=writer,
    )
    return window


def _record_pressure_post_watch_bars(
    closed,
    oi,
    doi,
    doi_pct,
    windows=None,
    writer=None,
):
    """Record only newly closed bars; never feeds data back into pressure logic."""
    if not closed:
        return
    active_windows = windows if windows is not None else _pressure_post_watch_windows
    for window in list(active_windows):
        if window.get("completed"):
            continue
        start = window["attempt_bar_timestamp"]
        end = window["window_end_timestamp"]
        for bar in sorted(closed, key=lambda item: item.get("ts", 0)):
            ts = bar.get("ts")
            if ts is None or ts <= start or ts > end:
                continue
            if ts in window["seen_bar_timestamps"]:
                continue
            if not bar.get("closed", False):
                continue
            is_final = ts >= end
            _pressure_post_watch_record(
                window,
                bar,
                oi,
                doi,
                doi_pct,
                "PRESSURE_POST_WATCH_BAR",
                writer=writer,
                window_status="COMPLETED" if is_final else "ACTIVE",
            )
            window["seen_bar_timestamps"].add(ts)
            window["last_recorded_bar_timestamp"] = ts
            if is_final:
                window["completed"] = True
                break
        if window.get("completed") and window in active_windows:
            active_windows.remove(window)


def as_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def quality_aggression(abs_delta, delta_z=None, delta_share=None, volume_ratio=None):
    dz = abs(as_float(delta_z, 0.0))
    share = abs(as_float(delta_share, 0.0))
    volume = as_float(volume_ratio, 0.0)
    amount = abs(as_float(abs_delta, 0.0))
    if dz >= 4.0 or share >= 0.75 or amount >= 150.0:
        return "EXTREME"
    if dz >= 2.0 or share >= 0.50 or volume >= 2.0 or amount >= 75.0:
        return "HIGH"
    if dz > 0.0 or share >= 0.20 or amount >= 20.0:
        return "NORMAL"
    return "LOW"


def quality_reward(extension_atr):
    value = as_float(extension_atr, 0.0)
    if value <= 0.0:
        return "NONE"
    if value < 0.25:
        return "WEAK"
    if value < 0.70:
        return "MEANINGFUL"
    return "STRONG"


def quality_retention(retained_fraction):
    value = as_float(retained_fraction, 0.0)
    if value <= 0.0:
        return "FAILED"
    if value < 0.50:
        return "WEAK"
    return "HELD"


def quality_context(
    *,
    abs_delta=0.0,
    delta_z=None,
    delta_share=None,
    volume_ratio=None,
    extension_atr=0.0,
    retained_fraction=None,
    sequence="ISOLATED",
    efficiency_trend="MIXED",
    active_leg="EFFECTIVE",
    oi_state="APPROX_STABLE",
    structure="NONE",
):
    return {
        "AGGRESSION": quality_aggression(
            abs_delta,
            delta_z,
            delta_share,
            volume_ratio,
        ),
        "PRICE_REWARD": quality_reward(extension_atr),
        "RETENTION": quality_retention(retained_fraction),
        "SEQUENCE": sequence,
        "EFFICIENCY_TREND": efficiency_trend,
        "ACTIVE_LEG": active_leg,
        "OI_STATE": oi_state,
        "STRUCTURE_CONFIRMATION": structure,
    }


def market_episode_id(ts, direction, event_type):
    """Group correlated observations without deleting raw events."""
    global _market_episode_number
    ts = as_float(ts, now_ts())
    with _quality_lock:
        for episode in reversed(_market_episodes):
            if ts - episode["last_time"] > MARKET_EPISODE_GAP_SEC:
                break
            if episode["direction"] == direction:
                episode["last_time"] = max(episode["last_time"], ts)
                episode["event_types"].append(event_type)
                return episode["id"]
        _market_episode_number += 1
        episode = {
            "id": f"M{_market_episode_number:04d}",
            "start_time": ts,
            "last_time": ts,
            "direction": direction,
            "event_types": [event_type],
        }
        _market_episodes.append(episode)
    append_dataset(
        MARKET_EPISODE_JSONL,
        {
            "event": "MARKET_EPISODE_OPENED",
            "market_episode_id": episode["id"],
            "start_time": ts,
            "direction": direction,
            "event_type": event_type,
        },
    )
    return episode["id"]


def record_quality_event(event_type, ts, direction, payload, context=None):
    global _quality_event_number
    _quality_event_number += 1
    market_id = market_episode_id(ts, direction, event_type)
    record = {
        "event_id": f"Q{_quality_event_number:06d}",
        "event": event_type,
        "timestamp": ts,
        "direction": direction,
        "market_episode_id": market_id,
        "quality_context": context or {},
        **payload,
    }
    _quality_records.append(record)
    append_dataset(QUALITY_JSONL, record)
    return record


def create_episode(side, context, detected_time, detected_price):
    global _episode_number
    _episode_number += 1
    boundary = context["hi"] if side == "UP" else context["lo"]
    episode = {
        "id": f"P{_episode_number:04d}",
        "class": "UNRESOLVED",
        "side": side,
        "direction": side_direction(side),
        "pressure_detected_time": detected_time,
        "pressure_detected_price": detected_price,
        "pressure_window_start": context["window_start"],
        "pressure_reference_price": boundary,
        "pressure_attack_count": context["tests"],
        "pressure_cumulative_delta": context["cum_delta"],
        "pressure_total_volume": context["total_volume"],
        "pressure_delta_volume_ratio": context["delta_dom"],
        "pressure_atr": context["atr"],
        "oi_context": context.get("oi_context"),
        "market_episode_id": market_episode_id(
            detected_time,
            side_direction(side),
            "PRESSURE_DETECTED",
        ),
        "quality_context": quality_context(
            abs_delta=abs(context["cum_delta"]),
            delta_share=context["delta_dom"],
            structure="NONE",
        ),
        "latest_extreme": boundary,
        "attacks": [],
        "retention": {},
        "watch": None,
        "continuation_time": None,
        "active_leg_time": None,
        "counter_events": [],
        "_counter_seen_bar_ts": [],
        "counter_start_bar_ts": None,
        "anchors": {
            "PRESSURE_DETECTED": {
                "time": detected_time,
                "price": detected_price,
                "atr": context["atr"],
            }
        },
        "outcomes_written": set(),
    }
    episode["attacks"] = [
        attack_record(
            bar,
            side,
            context["atr"],
            boundary,
        )
        for bar in context.get("pressure_bars", [])
    ]
    _episodes.append(episode)
    append_jsonl(
        "PRESSURE_DETECTED",
        episode,
        {"pressure_detected_price": detected_price},
    )
    return episode


def context_body(context, detected_price):
    return (
        f"Pressure side={context['side']} | "
        f"pressure_detected_price={detected_price:.1f}\n"
        f"pressure_window_start={fmt_ts(context['window_start'])}\n"
        f"reference_price={context['hi'] if context['side'] == 'UP' else context['lo']:.1f}\n"
        f"attempts={context['tests']} | "
        f"cumulative_delta={context['cum_delta']:+.1f} BTC | "
        f"total_volume={context['total_volume']:.1f} BTC | "
        f"delta/volume={context['delta_dom']:+.1%}\n"
        f"OI source={context.get('oi_source', 'unknown')} | "
        f"OI interval={context.get('oi_interval', 'unknown')} | "
        f"OI change={context.get('oi_change_text', 'unknown')}\n"
        "Это наблюдение, НЕ вход."
    )


def log_pressure_detected(episode, context):
    _write_pressure_human_line(
        episode["pressure_detected_time"],
        context["side"],
        episode["pressure_detected_price"],
    )
    record_quality_event(
        "PRESSURE_DETECTED",
        episode["pressure_detected_time"],
        side_direction(context["side"]),
        {
            "episode_id": episode["id"],
            "pressure_attack_count": context["tests"],
            "pressure_cumulative_delta": context["cum_delta"],
            "pressure_total_volume": context["total_volume"],
            "pressure_delta_volume_ratio": context["delta_dom"],
            "oi_change": context.get("oi_context"),
            "oi_change_pct": context.get("oi_context_pct"),
        },
        episode.get("quality_context"),
    )


def make_context(closed, side, oi_change, oi_change_pct):
    context = BASELINE._ps_context(closed)
    if context is None or context["side"] != side:
        return None
    context = dict(context)
    context["window_start"] = closed[-BASELINE.PS_PRESSURE_BARS]["ts"] / 1000.0
    context["pressure_bars"] = closed[-BASELINE.PS_PRESSURE_BARS:]
    context["total_volume"] = sum(
        b["volume"] for b in closed[-BASELINE.PS_PRESSURE_BARS:]
    )
    context["oi_context"] = oi_change
    context["oi_context_pct"] = oi_change_pct
    context["oi_source"] = "live_monitor:/futures/data/openInterestHist"
    context["oi_interval"] = "5m"
    context["oi_change_text"] = (
        f"{oi_change:+.3f} BTC ({oi_change_pct:+.3f}%)"
        if oi_change is not None and oi_change_pct is not None
        else "unavailable"
    )
    return context


def _battle_attempt(bar, side, atrv):
    delta = as_float(bar.get("delta"), 0.0)
    volume = as_float(bar.get("volume"), 0.0)
    if side == "BUY":
        extension = max(0.0, as_float(bar.get("high"), 0.0) - as_float(bar.get("open"), 0.0))
        retained = max(0.0, as_float(bar.get("close"), 0.0) - as_float(bar.get("open"), 0.0))
        active = delta > 0
    else:
        extension = max(0.0, as_float(bar.get("open"), 0.0) - as_float(bar.get("low"), 0.0))
        retained = max(0.0, as_float(bar.get("open"), 0.0) - as_float(bar.get("close"), 0.0))
        active = delta < 0
    return {
        "bar_ts": bar.get("ts"),
        "timestamp": as_float(bar.get("ts"), 0.0) / 1000.0,
        "delta": delta,
        "abs_delta": abs(delta),
        "volume": volume,
        "active_side": active,
        "extension_reward": extension,
        "extension_reward_ATR": extension / atrv if atrv else 0.0,
        "retained_reward": retained,
        "retained_reward_ATR": retained / atrv if atrv else 0.0,
        "retained_fraction": retained / extension if extension else 0.0,
        "efficiency_delta": extension / abs(delta) if delta else 0.0,
        "efficiency_volume": extension / volume if volume else 0.0,
        "strong_attempt_existing_reference": abs(delta) >= BASELINE.PS_MIN_POST_COUNTER_DELTA,
    }


def _battle_side_summary(bars, side, atrv):
    attempts = []
    first_open = None
    extreme = None
    total_delta = 0.0
    total_volume = 0.0
    for bar in bars:
        attempt = _battle_attempt(bar, side, atrv)
        if not attempt["active_side"]:
            continue
        attempts.append(attempt)
        total_delta += attempt["abs_delta"]
        total_volume += attempt["volume"]
        if first_open is None:
            first_open = as_float(bar.get("open"), 0.0)
            extreme = as_float(bar.get("high" if side == "BUY" else "low"), first_open)
        elif side == "BUY":
            extreme = max(extreme, as_float(bar.get("high"), extreme))
        else:
            extreme = min(extreme, as_float(bar.get("low"), extreme))

    sequence_extension = 0.0
    sequence_retained = 0.0
    if first_open is not None and extreme is not None:
        sequence_extension = (
            max(0.0, extreme - first_open)
            if side == "BUY"
            else max(0.0, first_open - extreme)
        )
        last_close = as_float(bars[-1].get("close"), first_open) if bars else first_open
        sequence_retained = (
            max(0.0, last_close - first_open)
            if side == "BUY"
            else max(0.0, first_open - last_close)
        )

    split = max(1, len(attempts) // 2) if attempts else 0
    first_half = attempts[:split] if attempts else []
    second_half = attempts[split:] if attempts else []
    first_eff = statistics.median([x["efficiency_delta"] for x in first_half]) if first_half else None
    second_eff = statistics.median([x["efficiency_delta"] for x in second_half]) if second_half else None
    return {
        "attempt_count": len(attempts),
        "strong_attempt_count_existing_reference": sum(
            1 for item in attempts if item["strong_attempt_existing_reference"]
        ),
        "cumulative_delta": sum(item["delta"] for item in attempts),
        "cumulative_abs_delta": total_delta,
        "total_volume": total_volume,
        "sequence_extension_reward_ATR": sequence_extension / atrv if atrv else 0.0,
        "sequence_retained_reward_ATR": sequence_retained / atrv if atrv else 0.0,
        "sequence_retained_fraction": (
            sequence_retained / sequence_extension if sequence_extension else 0.0
        ),
        "efficiency_delta_sequence": (
            sequence_extension / total_delta if total_delta else 0.0
        ),
        "efficiency_volume_sequence": (
            sequence_extension / total_volume if total_volume else 0.0
        ),
        "efficiency_first_half_median": first_eff,
        "efficiency_second_half_median": second_eff,
        "efficiency_change_second_minus_first": (
            second_eff - first_eff
            if first_eff is not None and second_eff is not None else None
        ),
        "attempts": attempts,
    }


def _battle_reward_transition(bars, atrv):
    history = {"BUY": [], "SELL": []}
    for bar in bars:
        for side in ("BUY", "SELL"):
            attempt = _battle_attempt(bar, side, atrv)
            if not attempt["active_side"]:
                continue
            opposite = "SELL" if side == "BUY" else "BUY"
            if attempt["extension_reward_ATR"] > 0 and history[opposite] and all(
                item["extension_reward_ATR"] <= 0 for item in history[opposite]
            ):
                return {
                    "reward_side": side,
                    "after_opponent": opposite,
                    "timestamp": attempt["timestamp"],
                }
            history[side].append(attempt)
    return None


def _battle_oi_summary(anchor_time, minutes):
    start = anchor_time - minutes * 60
    samples = [
        {"timestamp": ts, "oi": oi}
        for ts, oi in _oi_history
        if start <= ts <= anchor_time
    ]
    if not samples:
        return {
            "source": "live_monitor_oi_samples",
            "interval": "5s_poll",
            "available": False,
            "coverage_seconds": 0.0,
        }
    first = samples[0]["oi"]
    last = samples[-1]["oi"]
    peak = max(item["oi"] for item in samples)
    return {
        "source": "live_monitor_oi_samples",
        "interval": "5s_poll",
        "available": True,
        "coverage_seconds": max(0.0, samples[-1]["timestamp"] - samples[0]["timestamp"]),
        "sample_count": len(samples),
        "oi_start": first,
        "oi_end": last,
        "oi_change": last - first,
        "oi_change_pct": 100.0 * (last - first) / first if first else None,
        "oi_peak": peak,
        "oi_destruction_from_peak": last - peak,
    }


def record_pre_breakout_battle(
    episode,
    closed,
    event_type,
    anchor_time,
    anchor_price,
    anchor_bar_ts,
    side,
    atrv,
    oi_context=None,
    oi_context_pct=None,
):
    """Persist raw pre-event 1m flow; descriptive only, no signal/filter."""
    global _battle_number
    _battle_number += 1
    anchor_bar_ts = as_float(anchor_bar_ts, None)
    eligible = [
        bar for bar in closed
        if anchor_bar_ts is None or as_float(bar.get("ts"), 0.0) < anchor_bar_ts
    ]
    windows = {}
    for minutes in PRE_BREAKOUT_BATTLE_WINDOWS:
        bars = eligible[-minutes:]
        buy = _battle_side_summary(bars, "BUY", atrv or 1.0)
        sell = _battle_side_summary(bars, "SELL", atrv or 1.0)
        windows[str(minutes)] = {
            "requested_minutes": minutes,
            "bar_count": len(bars),
            "coverage_seconds": (
                (bars[-1]["ts"] - bars[0]["ts"]) / 1000.0
                if len(bars) >= 2 else 0.0
            ),
            "bars": [
                {
                    "bar_ts": bar["ts"],
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                    "delta": bar["delta"],
                }
                for bar in bars
            ],
            "BUY": buy,
            "SELL": sell,
            "first_reward_after_opponent_without_extension": _battle_reward_transition(
                bars,
                atrv or 1.0,
            ),
            "oi": _battle_oi_summary(anchor_time, minutes),
        }

    record = {
        "event": "PRE_BREAKOUT_BATTLE",
        "battle_id": f"B{_battle_number:06d}",
        "market_episode_id": episode.get("market_episode_id"),
        "pressure_episode_id": episode.get("id"),
        "event_type": event_type,
        "anchor_time": anchor_time,
        "anchor_bar_ts": anchor_bar_ts,
        "anchor_price": anchor_price,
        "pressure_side": side_direction(side),
        "atr": atrv,
        "oi_context_5m": oi_context,
        "oi_context_5m_pct": oi_context_pct,
        "oi_source": "live_monitor:/futures/data/openInterestHist",
        "oi_interval": "5m",
        "windows": windows,
        "research_only": True,
        "future_data_used": False,
    }
    append_dataset(PRE_BREAKOUT_BATTLE_JSONL, record)
    record_quality_event(
        "PRE_BREAKOUT_BATTLE",
        anchor_time,
        side_direction(side),
        {
            "battle_id": record["battle_id"],
            "pressure_episode_id": episode.get("id"),
            "event_type": event_type,
            "window_bar_counts": {
                key: value["bar_count"] for key, value in windows.items()
            },
            "window_buy_sell_delta": {
                key: {
                    "BUY": value["BUY"]["cumulative_delta"],
                    "SELL": value["SELL"]["cumulative_delta"],
                }
                for key, value in windows.items()
            },
        },
        quality_context(
            abs_delta=abs(episode.get("pressure_cumulative_delta", 0.0)),
            delta_share=episode.get("pressure_delta_volume_ratio", 0.0),
            extension_atr=0.0,
            retained_fraction=0.0,
            sequence="PERSISTENT",
            active_leg="EFFECTIVE",
            structure="BREAKOUT" if event_type == "PRESSURE_BREAKOUT_WATCH" else "NONE",
        ),
    )
    return record


def create_watch(episode, context, bar, oi):
    side = context["side"]
    reference = context["hi"] if side == "UP" else context["lo"]
    extension = (
        bar["high"] - reference
        if side == "UP"
        else reference - bar["low"]
    )
    watch = {
        "episode": episode,
        "side": side,
        "reference": reference,
        "attempt_time": now_ts(),
        "attempt_bar_time": bar["ts"] / 1000.0,
        "attempt_price": bar["close"],
        "attempt_extreme": bar["high"] if side == "UP" else bar["low"],
        "initial_extension_atr": extension / context["atr"],
        "pressure_atr": context["atr"],
        "oi_at_attempt": oi,
        "last_bar_ts": bar["ts"],
        "bars": [bar],
        "continuation_candidate": False,
        "failure_candidate": False,
        "counter_logged": False,
        "same_delta": 0.0,
        "opposite_delta": 0.0,
        "outside_closes": 0,
        "acceptance": False,
        "test_entry_created": False,
        "max_extension": extension,
        "max_retained": oriented_progress(side, reference, bar["close"]),
        "max_pullback": 0.0,
        "last_incremental_extension": 0.0,
        "last_incremental_extension_atr": 0.0,
    }
    episode["watch"] = watch
    episode["anchors"]["BREAKOUT_WATCH"] = {
        "time": watch["attempt_time"],
        "price": watch["attempt_price"],
        "atr": watch["pressure_atr"],
    }
    # Research-only recorder.  It receives the WATCH snapshot and later reads
    # only closed bars; it is not consulted by any detector or state decision.
    _start_pressure_post_watch_window(watch)
    return watch


def watch_metrics(watch):
    side = watch["side"]
    reference = watch["reference"]
    bars = watch["bars"]
    atr = watch["pressure_atr"]
    latest = bars[-1]
    extension = max(
        max(
            oriented_progress(side, reference, b["high"] if side == "UP" else b["low"])
            for b in bars
        ),
        0.0,
    )
    retained = oriented_progress(side, reference, latest["close"])
    pullback = max(0.0, extension - retained)
    retained_fraction = retained / extension if extension > 0 else 0.0
    return {
        "latest": latest,
        "extension": extension,
        "extension_atr": extension / atr if atr else 0.0,
        "retained": retained,
        "retained_atr": retained / atr if atr else 0.0,
        "retained_fraction": retained_fraction,
        "pullback": pullback,
        "pullback_atr": pullback / atr if atr else 0.0,
        "same_delta": watch["same_delta"],
        "opposite_delta": watch["opposite_delta"],
        "outside_closes": watch["outside_closes"],
        "bars": len(bars),
        "elapsed_seconds": max(
            0.0,
            latest["ts"] / 1000.0 - watch["attempt_bar_time"],
        ),
        "incremental_extension": watch.get("last_incremental_extension", 0.0),
        "incremental_extension_atr": watch.get(
            "last_incremental_extension_atr", 0.0
        ),
    }


def watch_body(watch, metrics, label):
    episode = watch["episode"]
    side = watch["side"]
    return (
        f"episode_id={episode['id']}\n"
        f"state={label} | pressure_side={side} | direction={side_direction(side)}\n"
        f"pressure_reference_price={watch['reference']:.1f}\n"
        f"attempt_time={fmt_ts(watch['attempt_time'])} | "
        f"attempt_price={watch['attempt_price']:.1f}\n"
        f"pressure_attempts={episode['pressure_attack_count']} | "
        f"pressure_cumulative_delta={episode['pressure_cumulative_delta']:+.1f} BTC | "
        f"pressure_volume={episode['pressure_total_volume']:.1f} BTC | "
        f"pressure_delta/volume={episode['pressure_delta_volume_ratio']:+.1%}\n"
        f"extension_reward={metrics['extension']:+.1f} USD | "
        f"extension_reward_ATR={metrics['extension_atr']:+.3f}\n"
        f"retained_reward={metrics['retained']:+.1f} USD | "
        f"retained_reward_ATR={metrics['retained_atr']:+.3f} | "
        f"retained_fraction={metrics['retained_fraction']:+.3f}\n"
        f"pullback_after_extension={metrics['pullback']:+.1f} USD | "
        f"pullback_ATR={metrics['pullback_atr']:+.3f}\n"
        f"incremental_extension={metrics.get('incremental_extension', 0.0):+.1f} USD | "
        f"incremental_extension_ATR={metrics.get('incremental_extension_atr', 0.0):+.3f}\n"
        f"elapsed_seconds={metrics.get('elapsed_seconds', 0.0):.0f}\n"
        f"same_side_delta={metrics['same_delta']:+.1f} BTC | "
        f"opposite_delta={metrics['opposite_delta']:+.1f} BTC | "
        f"outside_closes={metrics['outside_closes']} | bars={metrics['bars']}\n"
        "Это исследовательское состояние, НЕ вход."
    )


def outcome_rows(rows, entry_time, entry_bar_ts=None):
    """Exclude the partly formed entry candle to avoid intrabar leakage."""
    if entry_bar_ts is not None:
        return [row for row in rows if row["ts"] > entry_bar_ts]
    return [row for row in rows if row["ts"] >= entry_time]


def path_outcome(side, entry_price, atr, rows):
    if not rows:
        return None
    favorable = []
    adverse = []
    for row in rows:
        if side == "UP":
            favorable.append((max(0.0, row["high"] - entry_price), row["ts"]))
            adverse.append((max(0.0, entry_price - row["low"]), row["ts"]))
        else:
            favorable.append((max(0.0, entry_price - row["low"]), row["ts"]))
            adverse.append((max(0.0, row["high"] - entry_price), row["ts"]))
    mfe, mfe_ts = max(favorable, key=lambda item: item[0])
    mae, mae_ts = max(adverse, key=lambda item: item[0])
    close = rows[-1]["close"]
    retained = (
        max(0.0, close - entry_price)
        if side == "UP"
        else max(0.0, entry_price - close)
    )
    return {
        "entry_price": entry_price,
        "close": close,
        "mfe": mfe,
        "mae": mae,
        "mfe_atr": mfe / atr if atr else 0.0,
        "mae_atr": mae / atr if atr else 0.0,
        "retained": retained,
        "retained_atr": retained / atr if atr else 0.0,
        "retained_fraction": retained / mfe if mfe else 0.0,
        "time_to_mfe_seconds": max(0.0, mfe_ts - rows[0]["ts"]),
        "time_to_mae_seconds": max(0.0, mae_ts - rows[0]["ts"]),
        "mfe_before_mae": (
            mfe_ts < mae_ts if mfe > 0.0 and mae > 0.0 else None
        ),
    }


def _move_origin_side_metrics(bar, side):
    """Return candle-local effort/result metrics for BUY or SELL."""
    open_price = as_float(bar.get("open"), 0.0) or 0.0
    high = as_float(bar.get("high"), open_price) or open_price
    low = as_float(bar.get("low"), open_price) or open_price
    close = as_float(bar.get("close"), open_price) or open_price
    delta = as_float(bar.get("delta"), 0.0) or 0.0
    volume = as_float(bar.get("volume"), 0.0) or 0.0
    if side == "BUY":
        extension = max(0.0, high - open_price)
        retained = max(0.0, close - open_price)
    else:
        extension = max(0.0, open_price - low)
        retained = max(0.0, open_price - close)
    return {
        "side": side,
        "delta": delta,
        "abs_delta": abs(delta),
        "volume": volume,
        "directional_extension": extension,
        "retained_reward": retained,
        "retained_fraction": retained / extension if extension > 0 else 0.0,
        "efficiency_delta": extension / abs(delta) if abs(delta) > 0 else 0.0,
        "efficiency_volume": extension / volume if volume > 0 else 0.0,
        "price_progress_from_open": (
            max(0.0, close - open_price)
            if side == "BUY"
            else max(0.0, open_price - close)
        ),
    }


def _move_origin_bar_payload(bar, side, oi_meta=None):
    metrics = _move_origin_side_metrics(bar, side)
    payload = {
        "timestamp": fmt_ts(bar["ts"] / 1000.0),
        "bar_ts": bar["ts"] / 1000.0,
        "side": side,
        "open": bar["open"],
        "high": bar["high"],
        "low": bar["low"],
        "close": bar["close"],
        "delta": bar["delta"],
        "volume": bar["volume"],
        "delta_volume_ratio": (
            abs(bar["delta"]) / bar["volume"]
            if bar.get("volume")
            else 0.0
        ),
        **metrics,
    }
    if oi_meta:
        payload["current_oi"] = oi_meta.get("current_oi")
        payload["oi_change"] = oi_meta.get("oi_change")
        payload["oi_resolution"] = oi_meta.get("oi_resolution")
    return payload


def _move_origin_effective_attack(metrics, atr):
    minimum_reward = (
        MOVE_ORIGIN_MIN_REWARD_ATR * atr
        if atr and atr > 0
        else 0.0
    )
    return (
        metrics["directional_extension"] > 0
        and metrics["retained_reward"] > 0
        and metrics["retained_reward"] >= minimum_reward
    )


def _move_origin_delta_matches_side(bar, side):
    delta = as_float(bar.get("delta"), 0.0) or 0.0
    return delta > 0 if side == "BUY" else delta < 0


def _move_origin_reset_state(state, reason, bar=None):
    state["stage"] = "WAIT_EFFECTIVE_ATTACK"
    state["sequence_id"] = None
    state["stages"] = {}
    state.setdefault("transitions", []).append({
        "timestamp": fmt_ts(bar["ts"] / 1000.0) if bar else None,
        "to": "WAIT_EFFECTIVE_ATTACK",
        "reason": reason,
    })
    state["transitions"] = state["transitions"][-20:]


def _move_origin_start_sequence(state, direction, bar, old_side, metrics):
    global _move_origin_sequence_number
    _move_origin_sequence_number += 1
    state["sequence_id"] = f"MOS{_move_origin_sequence_number:06d}"
    state["stage"] = "WAIT_RETENTION_FAILURE"
    state["stages"] = {
        "preceding_effective_attack": _move_origin_bar_payload(bar, old_side),
    }
    state["transitions"] = [{
        "timestamp": fmt_ts(bar["ts"] / 1000.0),
        "from": "WAIT_EFFECTIVE_ATTACK",
        "to": "WAIT_RETENTION_FAILURE",
        "reason": "SIDE_CONSISTENT_EFFECTIVE_ATTACK",
    }]


def _move_origin_process_bar(state, direction, bar, atr):
    """Advance one direction's local causal state by one closed bar.

    This intentionally does not scan historical combinations.  A sequence is
    born on a live bar, advances one stage at a time, and is consumed after a
    candidate is emitted.
    """
    old_side = "SELL" if direction == "LONG" else "BUY"
    new_side = "BUY" if direction == "LONG" else "SELL"
    old_aggression = _move_origin_delta_matches_side(bar, old_side)
    new_aggression = _move_origin_delta_matches_side(bar, new_side)
    stage = state.get("stage", "WAIT_EFFECTIVE_ATTACK")

    if stage == "WAIT_EFFECTIVE_ATTACK":
        metrics = _move_origin_side_metrics(bar, old_side)
        if old_aggression and _move_origin_effective_attack(metrics, atr):
            _move_origin_start_sequence(state, direction, bar, old_side, metrics)
        return None

    stages = state["stages"]
    effective = stages["preceding_effective_attack"]
    effective_metrics = _move_origin_side_metrics(
        {"open": effective["open"], "high": effective["high"],
         "low": effective["low"], "close": effective["close"],
         "delta": effective["delta"], "volume": effective["volume"]},
        old_side,
    )

    if stage == "WAIT_RETENTION_FAILURE":
        metrics = _move_origin_side_metrics(bar, old_side)
        new_extreme = (
            bar["low"] < effective["low"] if old_side == "SELL"
            else bar["high"] > effective["high"]
        )
        poor_retention = (
            metrics["retained_reward"]
            < effective_metrics["retained_reward"] * MOVE_ORIGIN_RETENTION_DROP_RATIO
            and metrics["retained_fraction"]
            < effective_metrics["retained_fraction"] * MOVE_ORIGIN_RETENTION_DROP_RATIO
        )
        if old_aggression and new_extreme and poor_retention:
            stages["retention_failure"] = {
                **_move_origin_bar_payload(bar, old_side),
                "previous_extreme": effective["low"] if old_side == "SELL" else effective["high"],
                "incremental_new_extreme": (
                    effective["low"] - bar["low"] if old_side == "SELL"
                    else bar["high"] - effective["high"]
                ),
                "retention_status": "POOR",
            }
            state["stage"] = "WAIT_RECLAIM"
            state["transitions"].append({
                "timestamp": fmt_ts(bar["ts"] / 1000.0),
                "from": "WAIT_RETENTION_FAILURE", "to": "WAIT_RECLAIM",
                "reason": "NEW_EXTREME_WITH_POOR_RETENTION",
            })
            return None
        if new_aggression and _move_origin_effective_attack(
            _move_origin_side_metrics(bar, new_side), atr
        ):
            _move_origin_reset_state(state, "OPPOSITE_EFFECTIVE_ATTACK_BEFORE_FAILURE", bar)
        elif old_aggression and _move_origin_effective_attack(metrics, atr):
            _move_origin_start_sequence(state, direction, bar, old_side, metrics)
        return None

    failure = stages["retention_failure"]
    failure_bar = {k: failure[k] for k in ("open", "high", "low", "close", "delta", "volume")}
    if stage == "WAIT_RECLAIM":
        if old_aggression and _move_origin_effective_attack(
            _move_origin_side_metrics(bar, old_side), atr
        ):
            _move_origin_reset_state(state, "OLD_SIDE_REGAINED_BEFORE_RECLAIM", bar)
            return None
        if not new_aggression:
            return None
        metrics = _move_origin_side_metrics(bar, new_side)
        progress = (
            bar["close"] - failure["close"] if direction == "LONG"
            else failure["close"] - bar["close"]
        )
        e_delta = progress / metrics["abs_delta"] if metrics["abs_delta"] else 0.0
        e_volume = progress / metrics["volume"] if metrics["volume"] else 0.0
        if (
            progress > 0 and metrics["retained_reward"] > 0
            and e_delta > effective_metrics["efficiency_delta"]
        ):
            stages["reclaim"] = {
                **_move_origin_bar_payload(bar, new_side),
                "price_progress_from_failure_close": progress,
                "efficiency_delta": e_delta,
                "efficiency_volume": e_volume,
            }
            state["stage"] = "WAIT_FAILED_REATTACK"
            state["transitions"].append({
                "timestamp": fmt_ts(bar["ts"] / 1000.0),
                "from": "WAIT_RECLAIM", "to": "WAIT_FAILED_REATTACK",
                "reason": "SIDE_CONSISTENT_RECLAIM_WITH_RELATIVE_EFFICIENCY",
            })
        return None

    if stage == "WAIT_FAILED_REATTACK":
        metrics = _move_origin_side_metrics(bar, old_side)
        territory_held = (
            bar["close"] >= stages["reclaim"]["close"] if direction == "LONG"
            else bar["close"] <= stages["reclaim"]["close"]
        )
        failed = (
            old_aggression and metrics["abs_delta"] > 0
            and territory_held
            and metrics["retained_reward"] < effective_metrics["retained_reward"]
        )
        if failed:
            stages["failed_reattack"] = {
                **_move_origin_bar_payload(bar, old_side),
                "reclaimed_territory_held": territory_held,
                "price_reward": metrics["retained_reward"],
                "retention_status": "FAILED_TO_REGAIN_TERRITORY",
            }
            sequence = {
                "direction": direction,
                "sequence_id": state["sequence_id"],
                "preceding_effective_attack": stages["preceding_effective_attack"],
                "retention_failure": stages["retention_failure"],
                "reclaim": stages["reclaim"],
                "failed_reattack": stages["failed_reattack"],
                "state_transitions": list(state["transitions"]),
            }
            _move_origin_reset_state(state, "SEQUENCE_CONSUMED_AFTER_TEST_ENTRY", bar)
            return sequence
        if old_aggression and not territory_held:
            _move_origin_reset_state(state, "OLD_SIDE_RECLAIMED_TERRITORY", bar)
        return None

    _move_origin_reset_state(state, "UNKNOWN_STATE", bar)
    return None


def _move_origin_load_dataset():
    global _move_origin_loaded, _move_origin_signal_number, _move_origin_sequence_number
    if _move_origin_loaded:
        return
    _move_origin_loaded = True
    if not MOVE_ORIGIN_JSONL.exists():
        return
    loaded_by_id = {}
    try:
        with MOVE_ORIGIN_JSONL.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                signal_id = record.get("signal_id")
                if record.get("signal_type") == "TEST_ENTRY_CANDIDATE" and record.get("setup_family") == "MOVE_ORIGIN_TEST":
                    _move_origin_signals.append(record)
                    loaded_by_id[signal_id] = record
                    sequence_match = re.search(r"(\d+)$", str(record.get("sequence_id", "")))
                    if sequence_match:
                        _move_origin_sequence_number = max(
                            _move_origin_sequence_number,
                            int(sequence_match.group(1)),
                        )
                    match = re.search(r"(\d+)$", str(record.get("signal_id", "")))
                    if match:
                        _move_origin_signal_number = max(
                            _move_origin_signal_number,
                            int(match.group(1)),
                        )
                    continue
                if signal_id not in loaded_by_id:
                    continue
                if record.get("event") == "MOVE_ORIGIN_CONFIRMATION":
                    loaded_by_id[signal_id]["confirmation"] = record
                elif record.get("event") == "MOVE_ORIGIN_ACCELERATION":
                    loaded_by_id[signal_id]["acceleration"] = record
                elif record.get("event") == "MOVE_ORIGIN_TEST_OUTCOME":
                    horizon_key = f"{record.get('horizon_minutes')}m"
                    loaded_by_id[signal_id].setdefault("outcome", {})[horizon_key] = record.get("outcome")
    except OSError:
        return


def _move_origin_recent_signal(direction, timestamp):
    for signal in _move_origin_signals:
        if signal.get("direction") != direction:
            continue
        signal_time = as_float(signal.get("timestamp_epoch"), None)
        if signal_time is not None and abs(timestamp - signal_time) < MOVE_ORIGIN_SEQUENCE_GAP_SEC:
            return True
    return False


def _move_origin_signal_line(direction, timestamp, price):
    hm = BASELINE.datetime.fromtimestamp(
        timestamp,
        BASELINE.TZ,
    ).strftime("%H:%M")
    label = "ТВХ в ЛОНГ" if direction == "LONG" else "ТВХ в ШОРТ"
    return f"{hm} - {price:.1f} - {label} - смена усилия/результата"


def _move_origin_emit_line(line):
    return None


def _move_origin_initial_outcomes():
    return {f"{horizon}m": None for horizon in MOVE_ORIGIN_OUTCOME_HORIZONS}


def _move_origin_create_signal(sequence, bar, oi_meta, atr, hist_oi_change, hist_oi_pct, history):
    global _move_origin_signal_number
    direction = sequence["direction"]
    timestamp = bar["ts"] / 1000.0
    if _move_origin_recent_signal(direction, timestamp):
        return None
    _move_origin_signal_number += 1
    signal_id = f"MOT{_move_origin_signal_number:06d}"
    current_metrics = _move_origin_side_metrics(
        bar,
        "BUY" if direction == "LONG" else "SELL",
    )
    baseline_efficiency = sequence["preceding_effective_attack"]["efficiency_delta"]
    reclaim_efficiency = sequence["reclaim"]["efficiency_delta"]
    relative_ratio = (
        reclaim_efficiency / baseline_efficiency
        if baseline_efficiency > 0
        else None
    )
    signal = {
        "record_type": "MOVE_ORIGIN_TEST",
        "event": "TEST_ENTRY_CANDIDATE",
        "signal_type": "TEST_ENTRY_CANDIDATE",
        "signal_id": signal_id,
        "setup_family": "MOVE_ORIGIN_TEST",
        "sequence_id": sequence.get("sequence_id"),
        "sample_class": "OUT_OF_SAMPLE_LIVE",
        "development_case_reference": "MC0001_NOT_USED_FOR_RUNTIME_DETECTION",
        "timestamp": fmt_ts(timestamp),
        "timestamp_epoch": timestamp,
        "direction": direction,
        "price": bar["close"],
        "entry_price": bar["close"],
        "entry_bar_ts": timestamp,
        "atr": atr if atr else None,
        "not_a_trading_order": True,
        "preceding_effective_attack": sequence["preceding_effective_attack"],
        "retention_failure": sequence["retention_failure"],
        "reclaim": sequence["reclaim"],
        "failed_reattack": sequence["failed_reattack"],
        "stage_elapsed_seconds": {
            stage: max(
                0.0,
                sequence[stage]["bar_ts"]
                - sequence["preceding_effective_attack"]["bar_ts"],
            )
            for stage in (
                "retention_failure",
                "reclaim",
                "failed_reattack",
            )
        },
        "state_transitions": sequence.get("state_transitions", []),
        "delta": bar["delta"],
        "volume": bar["volume"],
        "directional_extension": current_metrics["directional_extension"],
        "retained_reward": current_metrics["retained_reward"],
        "retained_fraction": current_metrics["retained_fraction"],
        "efficiency_delta": current_metrics["efficiency_delta"],
        "efficiency_volume": current_metrics["efficiency_volume"],
        "relative_efficiency_change": {
            "baseline_efficiency_delta": baseline_efficiency,
            "reclaim_efficiency_delta": reclaim_efficiency,
            "reclaim_vs_baseline_ratio": relative_ratio,
            "reclaim_vs_baseline_change": (
                relative_ratio - 1.0 if relative_ratio is not None else None
            ),
            "not_a_score": True,
        },
        "relative_efficiency_vs_previous_effective_attack": {
            "reclaim_ratio": relative_ratio,
            "failed_reattack_ratio": (
                current_metrics["efficiency_delta"] / baseline_efficiency
                if baseline_efficiency > 0
                else None
            ),
            "signal_bar_ratio": (
                current_metrics["efficiency_delta"] / baseline_efficiency
                if baseline_efficiency > 0
                else None
            ),
        },
        "current_oi": oi_meta.get("current_oi"),
        "oi_change": oi_meta.get("oi_change"),
        "oi_resolution": oi_meta.get("oi_resolution"),
        "oi_source": oi_meta.get("oi_source"),
        "oi_change_5m": hist_oi_change,
        "oi_change_5m_pct": hist_oi_pct,
        "evidence_available_at_signal": {
            "closed_bar_timestamp": fmt_ts(timestamp),
            "history_bars_used": min(len(history), MOVE_ORIGIN_LOOKBACK_BARS),
            "no_future_data_used": True,
            "sequence_elapsed_seconds": max(
                0.0,
                timestamp
                - sequence["preceding_effective_attack"]["bar_ts"],
            ),
            "state": "COMPLETE_CAUSAL_SEQUENCE_OBSERVED",
        },
        "outcome": _move_origin_initial_outcomes(),
        "outcome_class": "UNRESOLVED",
        "confirmation": None,
        "acceleration": None,
    }
    _move_origin_signals.append(signal)
    append_dataset(MOVE_ORIGIN_JSONL, signal)
    _directional_process(_directional_ingest_mot(signal))
    _move_origin_emit_line(signal)
    return signal


def _move_origin_outcome(side, entry_price, entry_ts, atr, rows):
    future_rows = [row for row in rows if row["ts"] / 1000.0 > entry_ts]
    if not future_rows:
        return None
    favorable = []
    adverse = []
    for row in future_rows:
        if side == "UP":
            favorable.append((max(0.0, row["high"] - entry_price), row["ts"]))
            adverse.append((max(0.0, entry_price - row["low"]), row["ts"]))
        else:
            favorable.append((max(0.0, entry_price - row["low"]), row["ts"]))
            adverse.append((max(0.0, row["high"] - entry_price), row["ts"]))
    mfe, mfe_ts_ms = max(favorable, key=lambda item: item[0])
    mae, mae_ts_ms = max(adverse, key=lambda item: item[0])
    # Candle rows store ts in milliseconds; fmt_ts and elapsed fields use
    # Unix seconds.  Keeping this conversion here prevents invalid Windows
    # datetime arguments during live outcome updates.
    mfe_ts = mfe_ts_ms / 1000.0
    mae_ts = mae_ts_ms / 1000.0
    return {
        "mfe_usd": mfe,
        "mae_usd": mae,
        "mfe_atr": mfe / atr if atr else None,
        "mae_atr": mae / atr if atr else None,
        "mfe_time": fmt_ts(mfe_ts),
        "mae_time": fmt_ts(mae_ts),
        "time_to_mfe_seconds": max(0.0, mfe_ts - entry_ts),
        "time_to_mae_seconds": max(0.0, mae_ts - entry_ts),
        "mfe_before_mae": mfe_ts < mae_ts if mfe > 0 and mae > 0 else None,
    }


def _move_origin_update_outcomes(closed):
    latest_ts = closed[-1]["ts"] / 1000.0 if closed else 0.0
    for signal in list(_move_origin_signals):
        entry_ts = signal.get("timestamp_epoch")
        if entry_ts is None:
            continue
        side = "UP" if signal["direction"] == "LONG" else "DOWN"
        atr = as_float(signal.get("atr"), 0.0) or 0.0
        for horizon in MOVE_ORIGIN_OUTCOME_HORIZONS:
            key = f"{horizon}m"
            if signal.setdefault("outcome", {}).get(key) is not None:
                continue
            if latest_ts < entry_ts + horizon * 60:
                continue
            rows = [
                row
                for row in closed
                if entry_ts < row["ts"] / 1000.0 <= entry_ts + horizon * 60
            ]
            if len(rows) < horizon:
                continue
            result = _move_origin_outcome(
                side,
                signal["price"],
                entry_ts,
                atr,
                rows,
            )
            if result is None:
                continue
            result["horizon_minutes"] = horizon
            signal["outcome"][key] = result
            append_dataset(
                MOVE_ORIGIN_JSONL,
                {
                    "record_type": "MOVE_ORIGIN_TEST_OUTCOME",
                    "event": "MOVE_ORIGIN_TEST_OUTCOME",
                    "signal_id": signal["signal_id"],
                    "direction": signal["direction"],
                    "horizon_minutes": horizon,
                    "outcome": result,
                    "outcome_class": signal.get("outcome_class", "UNRESOLVED"),
                },
            )


def _move_origin_emit_confirmation(signal, bar, oi_meta, atr):
    if signal.get("confirmation") is not None:
        return False
    direction = signal["direction"]
    new_side = "BUY" if direction == "LONG" else "SELL"
    metrics = _move_origin_side_metrics(bar, new_side)
    progress = (
        bar["high"] - signal["price"]
        if direction == "LONG"
        else signal["price"] - bar["low"]
    )
    retained = (
        max(0.0, bar["close"] - signal["price"])
        if direction == "LONG"
        else max(0.0, signal["price"] - bar["close"])
    )
    minimum_progress = MOVE_ORIGIN_MIN_REWARD_ATR * (atr or 1.0)
    if (
        bar["ts"] / 1000.0 <= signal["timestamp_epoch"]
        or (bar["delta"] <= 0 if direction == "LONG" else bar["delta"] >= 0)
        or progress < minimum_progress
        or retained <= 0
        or retained < progress * MOVE_ORIGIN_RETENTION_DROP_RATIO
    ):
        return False
    confirmation = {
        "record_type": "MOVE_ORIGIN_CONFIRMATION",
        "event": "MOVE_ORIGIN_CONFIRMATION",
        "signal_id": signal["signal_id"],
        "timestamp": fmt_ts(bar["ts"] / 1000.0),
        "timestamp_epoch": bar["ts"] / 1000.0,
        "direction": direction,
        "price": bar["close"],
        "delta": bar["delta"],
        "volume": bar["volume"],
        "directional_extension": progress,
        "retained_reward": retained,
        "retained_fraction": retained / progress if progress else 0.0,
        "efficiency_delta": progress / abs(bar["delta"]) if bar["delta"] else 0.0,
        "efficiency_volume": progress / bar["volume"] if bar["volume"] else 0.0,
        "timing_cost_from_test_usd": (
            bar["close"] - signal["price"]
            if direction == "LONG"
            else signal["price"] - bar["close"]
        ),
        "elapsed_from_test_seconds": (
            bar["ts"] / 1000.0 - signal["timestamp_epoch"]
        ),
        "current_oi": oi_meta.get("current_oi"),
        "oi_change": oi_meta.get("oi_change"),
        "oi_resolution": oi_meta.get("oi_resolution"),
        "evidence_available_at_confirmation": {
            "no_future_data_used": True,
            "signal_price": signal["price"],
            "progress_from_signal": progress,
        },
    }
    signal["confirmation"] = confirmation
    append_dataset(MOVE_ORIGIN_JSONL, confirmation)
    return True


def _move_origin_emit_acceleration(signal, bar, oi_meta):
    if signal.get("confirmation") is None or signal.get("acceleration") is not None:
        return False
    if bar["ts"] / 1000.0 <= signal["confirmation"]["timestamp_epoch"]:
        return False
    direction = signal["direction"]
    same_side = bar["delta"] > 0 if direction == "LONG" else bar["delta"] < 0
    preceding = signal["preceding_effective_attack"]
    progress = (
        bar["high"] - signal["price"]
        if direction == "LONG"
        else signal["price"] - bar["low"]
    )
    retained = (
        max(0.0, bar["close"] - signal["price"])
        if direction == "LONG"
        else max(0.0, signal["price"] - bar["close"])
    )
    if (
        not same_side
        or abs(bar["delta"]) <= abs(preceding["delta"])
        or bar["volume"] <= preceding["volume"]
        or progress <= signal["confirmation"]["directional_extension"]
        or retained <= 0
    ):
        return False
    acceleration = {
        "record_type": "MOVE_ORIGIN_ACCELERATION",
        "event": "MOVE_ORIGIN_ACCELERATION",
        "signal_id": signal["signal_id"],
        "timestamp": fmt_ts(bar["ts"] / 1000.0),
        "timestamp_epoch": bar["ts"] / 1000.0,
        "direction": direction,
        "price": bar["close"],
        "delta": bar["delta"],
        "volume": bar["volume"],
        "directional_extension": progress,
        "retained_reward": retained,
        "timing_cost_from_test_usd": (
            bar["close"] - signal["price"]
            if direction == "LONG"
            else signal["price"] - bar["close"]
        ),
        "timing_cost_from_confirmation_usd": (
            bar["close"] - signal["confirmation"]["price"]
            if direction == "LONG"
            else signal["confirmation"]["price"] - bar["close"]
        ),
        "elapsed_from_test_seconds": (
            bar["ts"] / 1000.0 - signal["timestamp_epoch"]
        ),
        "elapsed_from_confirmation_seconds": (
            bar["ts"] / 1000.0
            - signal["confirmation"]["timestamp_epoch"]
        ),
        "current_oi": oi_meta.get("current_oi"),
        "oi_change": oi_meta.get("oi_change"),
        "oi_resolution": oi_meta.get("oi_resolution"),
        "evidence_available_at_acceleration": {
            "no_future_data_used": True,
            "delta_greater_than_preceding_effective_attack": True,
            "volume_greater_than_preceding_effective_attack": True,
        },
    }
    signal["acceleration"] = acceleration
    append_dataset(MOVE_ORIGIN_JSONL, acceleration)
    return True


def update_move_origin_test(closed, current_oi=None, hist_oi_change=None, hist_oi_pct=None):
    """Independent realtime MOVE_ORIGIN_TEST layer over closed 1m bars."""
    global _move_origin_seeded, _move_origin_last_closed_bar_ts, _move_origin_last_oi
    _move_origin_load_dataset()
    if not closed:
        return
    if not _move_origin_seeded:
        _move_origin_history.clear()
        _move_origin_history.extend(closed[-MOVE_ORIGIN_LOOKBACK_BARS:])
        _move_origin_last_closed_bar_ts = closed[-1]["ts"]
        _move_origin_last_oi = current_oi
        _move_origin_seeded = True
        return

    new_bars = [
        bar for bar in closed
        if _move_origin_last_closed_bar_ts is None
        or bar["ts"] > _move_origin_last_closed_bar_ts
    ]
    if not new_bars:
        _move_origin_update_outcomes(closed)
        return

    for bar in new_bars:
        oi_change = (
            current_oi - _move_origin_last_oi
            if current_oi is not None and _move_origin_last_oi is not None
            else None
        )
        oi_meta = {
            "current_oi": current_oi,
            "oi_change": oi_change,
            "oi_resolution": "instantaneous_sampled_at_closed_1m",
            "oi_source": "live_monitor:/fapi/v1/openInterest",
        }
        _move_origin_history.append(bar)
        _move_origin_last_closed_bar_ts = bar["ts"]
        _move_origin_last_oi = current_oi
        atr = _last_atr or BASELINE._ps_atr(closed) or 0.0
        for direction in ("LONG", "SHORT"):
            sequence = _move_origin_process_bar(
                _move_origin_states[direction],
                direction,
                bar,
                atr,
            )
            if sequence is not None:
                _move_origin_create_signal(
                    sequence,
                    bar,
                    oi_meta,
                    atr,
                    hist_oi_change,
                    hist_oi_pct,
                    list(_move_origin_history),
                )
            for signal in list(_move_origin_signals):
                if signal.get("timestamp_epoch", 0) >= bar["ts"] / 1000.0:
                    continue
                _move_origin_emit_confirmation(signal, bar, oi_meta, atr)
                _move_origin_emit_acceleration(signal, bar, oi_meta)
    _move_origin_update_outcomes(closed)


def _early_reversal_reset(state, reason, bar=None):
    state["stage"] = "WAIT_OLD_EFFECTIVE"
    state["sequence_id"] = None
    state["stages"] = {}
    state.setdefault("transitions", []).append({
        "timestamp": fmt_ts(bar["ts"] / 1000.0) if bar else None,
        "to": "WAIT_OLD_EFFECTIVE",
        "reason": reason,
    })
    state["transitions"] = state["transitions"][-20:]


def _early_reversal_start(state, direction, bar, old_side, atr):
    global _early_reversal_sequence_number
    _early_reversal_sequence_number += 1
    state["sequence_id"] = f"ERTS{_early_reversal_sequence_number:06d}"
    state["stage"] = "WAIT_DETERIORATION"
    state["stages"] = {
        "last_effective_old_side_attack": _move_origin_bar_payload(bar, old_side),
    }
    state["transitions"] = [{
        "timestamp": fmt_ts(bar["ts"] / 1000.0),
        "from": "WAIT_OLD_EFFECTIVE",
        "to": "WAIT_DETERIORATION",
        "reason": "OLD_SIDE_EFFECTIVE",
    }]


def _early_reversal_process_bar(state, direction, bar, atr):
    """Advance one local causal sequence; never search old combinations."""
    old_side = "SELL" if direction == "LONG" else "BUY"
    new_side = "BUY" if direction == "LONG" else "SELL"
    old_aggression = _move_origin_delta_matches_side(bar, old_side)
    new_aggression = _move_origin_delta_matches_side(bar, new_side)
    old_metrics = _move_origin_side_metrics(bar, old_side)
    new_metrics = _move_origin_side_metrics(bar, new_side)
    stage = state.get("stage", "WAIT_OLD_EFFECTIVE")

    if stage == "WAIT_OLD_EFFECTIVE":
        if old_aggression and _move_origin_effective_attack(old_metrics, atr):
            _early_reversal_start(state, direction, bar, old_side, atr)
        return None

    stages = state["stages"]
    previous = stages["last_effective_old_side_attack"]
    previous_metrics = _move_origin_side_metrics(previous, old_side)

    if stage == "WAIT_DETERIORATION":
        if old_aggression:
            new_extreme = (
                bar["low"] < previous["low"] if old_side == "SELL"
                else bar["high"] > previous["high"]
            )
            poor_retention = (
                old_metrics["retained_reward"]
                < previous_metrics["retained_reward"] * MOVE_ORIGIN_RETENTION_DROP_RATIO
                and old_metrics["retained_fraction"]
                < previous_metrics["retained_fraction"] * MOVE_ORIGIN_RETENTION_DROP_RATIO
            )
            if new_extreme and poor_retention:
                stages["deterioration"] = {
                    **_move_origin_bar_payload(bar, old_side),
                    "previous_extreme": previous["low"] if old_side == "SELL" else previous["high"],
                    "retention_status": "POOR",
                }
                stages["extreme"] = {
                    "timestamp": fmt_ts(bar["ts"] / 1000.0),
                    "bar_ts": bar["ts"] / 1000.0,
                    "price": bar["low"] if old_side == "SELL" else bar["high"],
                }
                state["stage"] = "WAIT_FIRST_OPPOSITE_REWARD"
                state["transitions"].append({
                    "timestamp": fmt_ts(bar["ts"] / 1000.0),
                    "from": "WAIT_DETERIORATION",
                    "to": "WAIT_FIRST_OPPOSITE_REWARD",
                    "reason": "OLD_SIDE_NEW_EXTREME_WITH_POOR_RETENTION",
                })
                return None
            if _move_origin_effective_attack(old_metrics, atr):
                # The local process is still working; use the latest effective
                # attack rather than retaining stale historical stages.
                _early_reversal_start(state, direction, bar, old_side, atr)
                return None
        if new_aggression and _move_origin_effective_attack(new_metrics, atr):
            _early_reversal_reset(state, "OPPOSITE_EFFECTIVE_BEFORE_DETERIORATION", bar)
        return None

    if stage == "WAIT_FIRST_OPPOSITE_REWARD":
        if old_aggression and _move_origin_effective_attack(old_metrics, atr):
            _early_reversal_reset(state, "OLD_SIDE_REASSERTED_BEFORE_FIRST_OPPOSITE_REWARD", bar)
            return None
        if not (new_aggression and _move_origin_effective_attack(new_metrics, atr)):
            return None
        stages["first_opposite_reward"] = _move_origin_bar_payload(bar, new_side)
        sequence = {
            "direction": direction,
            "old_side": old_side,
            "new_side": new_side,
            "sequence_id": state["sequence_id"],
            "last_effective_old_side_attack": stages["last_effective_old_side_attack"],
            "deterioration": stages["deterioration"],
            "extreme": stages["extreme"],
            "first_opposite_reward": stages["first_opposite_reward"],
            "state_transitions": list(state["transitions"]),
        }
        _early_reversal_reset(state, "SEQUENCE_CONSUMED_AFTER_TEST_ENTRY", bar)
        return sequence

    _early_reversal_reset(state, "UNKNOWN_STATE", bar)
    return None


def _early_reversal_direct_reset(state, reason, bar=None):
    state["stage"] = "WAIT_OLD_STRONG_FINAL_IMPULSE"
    state["sequence_id"] = None
    state["stages"] = {}
    state.setdefault("transitions", []).append({
        "timestamp": fmt_ts(bar["ts"] / 1000.0) if bar else None,
        "to": "WAIT_OLD_STRONG_FINAL_IMPULSE",
        "reason": reason,
    })
    state["transitions"] = state["transitions"][-20:]


def _early_reversal_direct_start(state, direction, bar, old_side):
    global _early_reversal_sequence_number
    _early_reversal_sequence_number += 1
    state["sequence_id"] = f"ERTS{_early_reversal_sequence_number:06d}"
    state["stage"] = "WAIT_IMMEDIATE_OPPOSITE_REWARD"
    state["stages"] = {
        "last_effective_old_side_attack": _move_origin_bar_payload(bar, old_side),
        "expected_next_bar_ts": bar["ts"] + 60_000,
    }
    state["transitions"] = [{
        "timestamp": fmt_ts(bar["ts"] / 1000.0),
        "from": "WAIT_OLD_STRONG_FINAL_IMPULSE",
        "to": "WAIT_IMMEDIATE_OPPOSITE_REWARD",
        "reason": "OLD_SIDE_STRONG_FINAL_IMPULSE",
    }]


def _early_reversal_direct_process_bar(state, direction, bar, atr):
    """Direct local impulse -> immediate opposite reward branch."""
    old_side = "SELL" if direction == "LONG" else "BUY"
    new_side = "BUY" if direction == "LONG" else "SELL"
    old_aggression = _move_origin_delta_matches_side(bar, old_side)
    new_aggression = _move_origin_delta_matches_side(bar, new_side)
    old_metrics = _move_origin_side_metrics(bar, old_side)
    new_metrics = _move_origin_side_metrics(bar, new_side)
    stage = state.get("stage", "WAIT_OLD_STRONG_FINAL_IMPULSE")

    if stage == "WAIT_OLD_STRONG_FINAL_IMPULSE":
        if old_aggression and _move_origin_effective_attack(old_metrics, atr):
            _early_reversal_direct_start(state, direction, bar, old_side)
        return None

    previous = state["stages"]["last_effective_old_side_attack"]
    expected_ts = state["stages"]["expected_next_bar_ts"]
    if bar["ts"] != expected_ts:
        _early_reversal_direct_reset(state, "NON_LOCAL_BAR_GAP", bar)
        return None

    if new_aggression and _move_origin_effective_attack(new_metrics, atr):
        state["stages"]["first_opposite_reward"] = _move_origin_bar_payload(bar, new_side)
        sequence = {
            "direction": direction,
            "old_side": old_side,
            "new_side": new_side,
            "causal_branch": "DIRECT_COUNTERATTACK_FLIP",
            "sequence_id": state["sequence_id"],
            "last_effective_old_side_attack": previous,
            "deterioration": None,
            "extreme": {
                "timestamp": previous["timestamp"],
                "bar_ts": previous["bar_ts"],
                "price": previous["high"] if old_side == "BUY" else previous["low"],
            },
            "first_opposite_reward": state["stages"]["first_opposite_reward"],
            "state_transitions": list(state["transitions"]),
        }
        _early_reversal_direct_reset(state, "SEQUENCE_CONSUMED_AFTER_TEST_ENTRY", bar)
        return sequence

    # The direct branch is intentionally strict: no stale effective impulse
    # may wait through neutral bars or a second old-side attempt.
    _early_reversal_direct_reset(state, "NO_IMMEDIATE_OPPOSITE_REWARD", bar)
    if old_aggression and _move_origin_effective_attack(old_metrics, atr):
        _early_reversal_direct_start(state, direction, bar, old_side)
    return None


def _early_reversal_load_dataset():
    global _early_reversal_loaded, _early_reversal_signal_number, _early_reversal_sequence_number
    if _early_reversal_loaded:
        return
    _early_reversal_loaded = True
    if not EARLY_REVERSAL_JSONL.exists():
        return
    try:
        with EARLY_REVERSAL_JSONL.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("event") == "TEST_ENTRY_CANDIDATE":
                    _early_reversal_signals.append(record)
                    for key, target in (("signal_id", "_early_reversal_signal_number"), ("sequence_id", "_early_reversal_sequence_number")):
                        match = re.search(r"(\d+)$", str(record.get(key, "")))
                        if match:
                            value = int(match.group(1))
                            if target == "_early_reversal_signal_number":
                                _early_reversal_signal_number = max(_early_reversal_signal_number, value)
                            else:
                                _early_reversal_sequence_number = max(_early_reversal_sequence_number, value)
    except OSError:
        return


def _early_reversal_signal_line(direction, timestamp, price):
    hm = BASELINE.datetime.fromtimestamp(timestamp, BASELINE.TZ).strftime("%H:%M")
    if direction == "SHORT":
        return f"{hm} - {price:.1f} - ПЕРВЫЙ РЕЗУЛЬТАТ SELL — возможная смена движения"
    return f"{hm} - {price:.1f} - ПЕРВЫЙ РЕЗУЛЬТАТ BUY — возможная смена движения"


def _early_reversal_emit(line):
    return None


def _early_reversal_signal_line(direction, timestamp, price):
    hm = BASELINE.datetime.fromtimestamp(timestamp, BASELINE.TZ).strftime("%H:%M")
    side = "SELL" if direction == "SHORT" else "BUY"
    return f"{hm} - {price:.1f} - {side} — возможная смена движения"


def _write_human_event(module_name, event_name, body):
    """Write one human-facing event in the common research-log format."""
    stamp = BASELINE.now_local().strftime("%Y-%m-%d %H:%M:%S")
    block = (
        f"########## МОДУЛЬ={module_name}\n"
        f"[{stamp} Panama UTC-5] BTC-LRA | {event_name}\n"
        + body.rstrip()
        + "\n\n"
    )
    if module_name in ("ПРОИСХОЖДЕНИЕ_ДВИЖЕНИЯ", "MOVE_ORIGIN"):
        block = (
            f"[{stamp} Panama UTC-5] BTC-LRA | {event_name}\n"
            + body.rstrip()
            + "\n"
        )
    print(block.rstrip(), flush=True)
    try:
        with BASELINE._lock(LOCK_FILE):
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(block)
    except Exception:
        pass


# Clean runtime presentation.  The detector payload and its JSONL record stay unchanged.
def _move_origin_emit_line(signal):
    # Routed by the coordinator-level directional hypothesis only.
    return None
    direction = signal.get("direction", "")
    body = (
        f"\u041b\u041e\u041a\u0410\u041b\u042c\u041d\u042b\u0419 \u043a\u0430\u043d\u0434\u0438\u0434\u0430\u0442 {direction} | \u0446\u0435\u043d\u0430: {signal.get('price')}\n"
        f"ID \u0441\u043e\u0431\u044b\u0442\u0438\u044f: {signal.get('signal_id')}\n"
        "\u0417\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430 \u043b\u043e\u043a\u0430\u043b\u044c\u043d\u0430\u044f \u043f\u043e\u0441\u043b\u0435\u0434\u043e\u0432\u0430\u0442\u0435\u043b\u044c\u043d\u043e\u0441\u0442\u044c \u00ab\u0443\u0441\u0438\u043b\u0438\u0435 \u2192 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u00bb.\n"
        "\u042d\u0442\u043e \u043d\u0435 \u043e\u0437\u043d\u0430\u0447\u0430\u0435\u0442 \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0443\u044e \u0441\u043c\u0435\u043d\u0443 \u043e\u0431\u0449\u0435\u0433\u043e \u043d\u0430\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f."
    )
    return None


def _early_reversal_emit(line):
    # Событие полностью сохраняется в EARLY_REVERSAL JSONL, но не является
    # завершённым human-facing reversal event.
    return None


def _early_reversal_create_signal(sequence, bar, oi_meta, atr):
    global _early_reversal_signal_number
    if not isinstance(bar, dict) or bar.get("ts") is None:
        return None
    _early_reversal_signal_number += 1
    timestamp = as_float(bar.get("ts"), None)
    if timestamp is None:
        return None
    timestamp /= 1000.0
    signal_id = f"ERT{_early_reversal_signal_number:06d}"
    old = sequence["last_effective_old_side_attack"]
    deterioration = sequence["deterioration"]
    opposite = sequence["first_opposite_reward"]
    causal_branch = sequence.get("causal_branch", "DETERIORATION_FLIP")
    a_ts = old["bar_ts"]
    d_ts = opposite["bar_ts"]
    signal = {
        "record_type": "MOVE_TRANSITION_TEST",
        "event": "TEST_ENTRY_CANDIDATE",
        "signal_type": "TEST_ENTRY_CANDIDATE",
        "setup_family": "MOVE_TRANSITION_TEST",
        "semantic_stage": "FIRST_OPPOSITE_REWARD",
        "sample_class": "OUT_OF_SAMPLE_LIVE",
        "development_case_reference": "MMC0001_DEVELOPMENT_HYPOTHESIS",
        "signal_id": signal_id,
        "sequence_id": sequence["sequence_id"],
        "causal_branch": causal_branch,
        "timestamp": fmt_ts(timestamp),
        "timestamp_epoch": timestamp,
        "direction": sequence["direction"],
        "price": bar["close"],
        "old_side": sequence["old_side"],
        "last_effective_old_side_attack": old,
        "deterioration_start": deterioration,
        "extreme_time": sequence["extreme"]["timestamp"],
        "extreme_price": sequence["extreme"]["price"],
        "first_opposite_reward": opposite,
        "old_side_delta": old["delta"],
        "old_side_volume": old["volume"],
        "old_side_retained_reward": old["retained_reward"],
        "old_side_efficiency": old["efficiency_delta"],
        "deterioration_delta": deterioration["delta"] if deterioration else None,
        "deterioration_volume": deterioration["volume"] if deterioration else None,
        "deterioration_extension": deterioration["directional_extension"] if deterioration else None,
        "deterioration_retained_reward": deterioration["retained_reward"] if deterioration else None,
        "deterioration_efficiency": deterioration["efficiency_delta"] if deterioration else None,
        "opposite_delta": opposite["delta"],
        "opposite_volume": opposite["volume"],
        "opposite_price_progress": opposite["price_progress_from_open"],
        "opposite_retained_reward": opposite["retained_reward"],
        "opposite_retained_fraction": opposite["retained_fraction"],
        "opposite_efficiency": opposite["efficiency_delta"],
        "sequence_duration": d_ts - a_ts,
        "bars_between_stages": {
            "effective_to_deterioration": ((deterioration["bar_ts"] - a_ts) / 60.0) if deterioration else None,
            "deterioration_to_opposite_reward": ((d_ts - deterioration["bar_ts"]) / 60.0) if deterioration else None,
            "effective_to_opposite_reward": (d_ts - a_ts) / 60.0,
        },
        "state_transitions": sequence["state_transitions"],
        "evidence_available_at_signal": {
            "closed_bar_timestamp": fmt_ts(timestamp),
            "no_future_data_used": True,
            "stages": [
                old["timestamp"],
                deterioration["timestamp"] if deterioration else None,
                opposite["timestamp"],
            ],
        },
        "current_oi": oi_meta.get("current_oi"),
        "oi_change": oi_meta.get("oi_change"),
        "oi_resolution": oi_meta.get("oi_resolution"),
        "oi_source": oi_meta.get("oi_source"),
        "atr": atr if atr else None,
        "outcome": {f"{h}m": None for h in EARLY_REVERSAL_OUTCOME_HORIZONS},
        "outcome_class": "UNRESOLVED",
        "post_signal_state": "UNRESOLVED",
        "not_a_trading_order": True,
    }
    _early_reversal_signals.append(signal)
    append_dataset(EARLY_REVERSAL_JSONL, signal)
    _battle_on_ert(signal, bar, oi_meta, atr)
    _early_reversal_emit(_early_reversal_signal_line(sequence["direction"], timestamp, bar["close"]))
    return signal


def _early_reversal_update_post_signal(signal, bar, atr):
    if signal.get("post_signal_state") != "UNRESOLVED":
        return
    if bar["ts"] / 1000.0 <= signal.get("timestamp_epoch", 0):
        return
    direction = signal["direction"]
    new_side = "BUY" if direction == "LONG" else "SELL"
    old_side = signal["old_side"]
    side = new_side if _move_origin_delta_matches_side(bar, new_side) else old_side
    if not _move_origin_delta_matches_side(bar, side):
        return
    metrics = _move_origin_side_metrics(bar, side)
    if not _move_origin_effective_attack(metrics, atr):
        return
    state = "CONFIRMED" if side == new_side else "REASSERTION_OLD_SIDE"
    signal["post_signal_state"] = state
    append_dataset(EARLY_REVERSAL_JSONL, {
        "record_type": "EARLY_REVERSAL_OBSERVATION",
        "event": state,
        "semantic_stage": "DIRECTION_CONFIRMED" if state == "CONFIRMED" else "TRANSITION_BATTLE",
        "signal_id": signal["signal_id"],
        "timestamp": fmt_ts(bar["ts"] / 1000.0),
        "timestamp_epoch": bar["ts"] / 1000.0,
        "direction": direction,
        "price": bar["close"],
        "side": side,
        "metrics": metrics,
        "evidence_available_at_observation": {"no_future_data_used": True},
    })


def _early_reversal_update_outcomes(closed):
    latest_ts = closed[-1]["ts"] / 1000.0 if closed else 0.0
    for signal in list(_early_reversal_signals):
        entry_ts = signal.get("timestamp_epoch")
        if entry_ts is None:
            continue
        side = "UP" if signal["direction"] == "LONG" else "DOWN"
        atr = as_float(signal.get("atr"), 0.0) or 0.0
        for horizon in EARLY_REVERSAL_OUTCOME_HORIZONS:
            key = f"{horizon}m"
            if signal.setdefault("outcome", {}).get(key) is not None or latest_ts < entry_ts + horizon * 60:
                continue
            rows = [row for row in closed if entry_ts < row["ts"] / 1000.0 <= entry_ts + horizon * 60]
            if len(rows) < horizon:
                continue
            result = _move_origin_outcome(side, signal["price"], entry_ts, atr, rows)
            if result is None:
                continue
            result["horizon_minutes"] = horizon
            signal["outcome"][key] = result
            append_dataset(EARLY_REVERSAL_JSONL, {
                "record_type": "MOVE_TRANSITION_TEST_OUTCOME",
                "event": "MOVE_TRANSITION_TEST_OUTCOME",
                "signal_id": signal["signal_id"],
                "direction": signal["direction"],
                "horizon_minutes": horizon,
                "outcome": result,
                "outcome_class": signal.get("outcome_class", "UNRESOLVED"),
            })


def update_early_reversal_test(closed, current_oi=None, hist_oi_change=None, hist_oi_pct=None):
    """Independent closed-1m early reversal experiment."""
    global _early_reversal_seeded, _early_reversal_last_closed_bar_ts, _early_reversal_last_oi
    _early_reversal_load_dataset()
    if not closed:
        return
    if not _early_reversal_seeded:
        _early_reversal_last_closed_bar_ts = closed[-1]["ts"]
        _early_reversal_last_oi = current_oi
        _early_reversal_seeded = True
        return
    new_bars = [bar for bar in closed if _early_reversal_last_closed_bar_ts is None or bar["ts"] > _early_reversal_last_closed_bar_ts]
    for bar in new_bars:
        oi_change = current_oi - _early_reversal_last_oi if current_oi is not None and _early_reversal_last_oi is not None else None
        oi_meta = {
            "current_oi": current_oi,
            "oi_change": oi_change,
            "oi_resolution": "instantaneous_sampled_at_closed_1m",
            "oi_source": "live_monitor:/fapi/v1/openInterest",
        }
        _early_reversal_last_closed_bar_ts = bar["ts"]
        _early_reversal_last_oi = current_oi
        atr = _last_atr or BASELINE._ps_atr(closed) or 0.0
        _battle_on_bar(bar, oi_meta, atr)
        for direction in ("LONG", "SHORT"):
            sequence = _early_reversal_process_bar(_early_reversal_states[direction], direction, bar, atr)
            if sequence is not None:
                _early_reversal_create_signal(sequence, bar, oi_meta, atr)
                # A completed local reversal consumes the competing direction's
                # stale partial sequence as well; otherwise one battle could
                # emit an artificial opposite flip several minutes later.
                other_direction = "SHORT" if direction == "LONG" else "LONG"
                other_state = _early_reversal_states[other_direction]
                if other_state.get("stage") != "WAIT_OLD_EFFECTIVE":
                    _early_reversal_reset(other_state, "COMPETING_SEQUENCE_INVALIDATED_BY_LOCAL_REVERSAL", bar)
            direct_sequence = _early_reversal_direct_process_bar(
                _early_reversal_direct_states[direction],
                direction,
                bar,
                atr,
            )
            if direct_sequence is not None:
                _early_reversal_create_signal(direct_sequence, bar, oi_meta, atr)
                other_direction = "SHORT" if direction == "LONG" else "LONG"
                other_direct_state = _early_reversal_direct_states[other_direction]
                if other_direct_state.get("stage") != "WAIT_OLD_STRONG_FINAL_IMPULSE":
                    _early_reversal_direct_reset(
                        other_direct_state,
                        "COMPETING_DIRECT_SEQUENCE_INVALIDATED_BY_LOCAL_REVERSAL",
                        bar,
                    )
            for signal in list(_early_reversal_signals):
                _early_reversal_update_post_signal(signal, bar, atr)
    _early_reversal_update_outcomes(closed)


def virtual_outcome(side, entry_price, atr, rows, tp_atr, sl_atr):
    tp = entry_price + tp_atr * atr if side == "UP" else entry_price - tp_atr * atr
    sl = entry_price - sl_atr * atr if side == "UP" else entry_price + sl_atr * atr
    result = "NEITHER"
    for row in rows:
        tp_hit = row["high"] >= tp if side == "UP" else row["low"] <= tp
        sl_hit = row["low"] <= sl if side == "UP" else row["high"] >= sl
        if tp_hit and sl_hit:
            result = "AMBIGUOUS_SAME_BAR"
            break
        if tp_hit:
            result = "TP_FIRST"
            break
        if sl_hit:
            result = "SL_FIRST"
            break
    return {"tp_atr": tp_atr, "sl_atr": sl_atr, "result": result}


def play_test_entry_sound():
    def worker():
        try:
            import winsound
            for freq, duration in ((1100, 120), (1450, 160), (1850, 240)):
                winsound.Beep(freq, duration)
                time.sleep(0.06)
        except Exception:
            try:
                print("\\a", end="", flush=True)
            except Exception:
                pass
    threading.Thread(target=worker, daemon=True).start()


def create_test_entry(
    setup_family,
    direction,
    entry_time,
    entry_price,
    atr,
    quality,
    source_event,
    entry_bar_ts=None,
    payload=None,
):
    global _test_entry_number
    ts = as_float(entry_time, now_ts())
    key = (setup_family, direction, int(ts // 60))
    market_id = market_episode_id(ts, direction, "TEST_ENTRY_CANDIDATE")
    with _quality_lock:
        if any(entry.get("dedupe_key") == key for entry in _test_entries):
            return None
        _test_entry_number += 1
        entry = {
            "event": "TEST_ENTRY_CANDIDATE",
            "event_id": f"T{_test_entry_number:06d}",
            "market_episode_id": market_id,
            "setup_family": setup_family,
            "direction": direction,
            "entry_time": ts,
            "entry_price": as_float(entry_price, 0.0),
            "entry_bar_ts": entry_bar_ts,
            "atr": as_float(atr, 0.0),
            "quality_context": quality or {},
            "position_context": _position_state,
            "source_event": source_event,
            "payload": payload or {},
            "outcomes": {},
            "virtual_outcomes": {},
            "status": "ACTIVE",
            "dedupe_key": key,
        }
        _test_entries.append(entry)
    append_dataset(TEST_ENTRY_JSONL, entry)
    log_event(
        "TEST_ENTRY_CANDIDATE",
        (
            f"[TEST {direction}] setup={setup_family}\n"
            f"price={entry['entry_price']:.1f} | event_id={entry['event_id']}\n"
            f"quality={json.dumps(entry['quality_context'], ensure_ascii=False, sort_keys=True)}\n"
            "Research anchor only. No order is sent."
        ),
    )
    play_test_entry_sound()
    return entry


def create_exit_anchor(
    health,
    state,
    timestamp,
    price,
    atr,
    quality,
    source_event,
    payload=None,
    sound=True,
):
    """Store an exit/attention anchor without creating an opposite entry."""
    global _exit_anchor_number
    key = (health["health_id"], state)
    if any(item.get("dedupe_key") == key for item in _exit_anchors):
        return None
    _exit_anchor_number += 1
    direction = health["direction"]
    record = {
        "event": "TEST_EXIT_CANDIDATE" if state == "OPPOSITE_RESPONSE" else "TEST_EXIT_CONFIRMED",
        "event_id": f"X{_exit_anchor_number:06d}",
        "market_episode_id": market_episode_id(timestamp, direction, state),
        "action": f"EXIT_{direction}",
        "position_direction": direction,
        "opposite_direction": "SHORT" if direction == "LONG" else "LONG",
        "timestamp": as_float(timestamp, now_ts()),
        "price": as_float(price, 0.0),
        "atr": as_float(atr, 0.0),
        "state": state,
        "source_event": source_event,
        "source_health_id": health["health_id"],
        "quality_context": quality or {},
        "payload": payload or {},
        "research_only": True,
        "dedupe_key": key,
    }
    _exit_anchors.append(record)
    append_dataset(TEST_ENTRY_JSONL, record)
    append_dataset(ACTIVE_MOVE_HEALTH_JSONL, record)
    log_event(
        "TEST_EXIT_CANDIDATE" if state == "OPPOSITE_RESPONSE" else "TEST_EXIT_CONFIRMED",
        (
            f"[{'TEST EXIT' if state == 'OPPOSITE_RESPONSE' else 'EXIT CONFIRMED'} {direction}]\n"
            f"health_id={health['health_id']} | price={record['price']:.1f}\n"
            f"state={state} | opposite={record['opposite_direction']}\n"
            f"quality={json.dumps(record['quality_context'], ensure_ascii=False, sort_keys=True)}\n"
            "Это исследовательская точка, не торговый приказ."
        ),
    )
    if sound:
        play_test_entry_sound()
    return record


def _health_direction_from_shock(block):
    if re.search(r"BUY\s+SHOCK", block, flags=re.IGNORECASE):
        return "UP"
    if re.search(r"SELL\s+SHOCK", block, flags=re.IGNORECASE):
        return "DOWN"
    return None


def _health_quality(health, *, delta=0.0, delta_z=None, delta_share=None,
                    extension_atr=0.0, retained_fraction=0.0,
                    structure="NONE", active_leg=None,
                    efficiency_trend=None):
    if active_leg is None:
        active_leg = (
            "EFFECTIVE" if health["state"] == "HEALTHY"
            else "DECAYING" if health["state"] == "EFFICIENCY_DECAY"
            else "STALLED"
        )
    if efficiency_trend is None:
        efficiency_trend = (
            "IMPROVING" if health["state"] == "HEALTHY"
            else "DETERIORATING" if health["state"] in {"EFFICIENCY_DECAY", "OPPOSITE_RESPONSE"}
            else "MIXED"
        )
    return quality_context(
        abs_delta=abs(delta),
        delta_z=delta_z,
        delta_share=delta_share,
        extension_atr=extension_atr,
        retained_fraction=retained_fraction,
        sequence="REPEATED" if len(health["same_side_attacks"]) > 1 else "ISOLATED",
        efficiency_trend=efficiency_trend,
        active_leg=active_leg,
        oi_state="DESTROYING" if as_float(health.get("last_doi"), 0.0) < 0 else "BUILDING"
        if health.get("last_doi") is not None else "APPROX_STABLE",
        structure=structure,
    )


def register_active_move_health(ts, price, block):
    """Open one 1m ACTIVE_MOVE_HEALTH stream for a CASE1 shock leg."""
    global _active_move_number
    tf_match = re.search(r"\bTF=(1m|5m|15m|1h)\b", block)
    if not tf_match or tf_match.group(1) != "1m":
        return None
    direction = _health_direction_from_shock(block)
    if direction is None:
        return None

    continuation_atr = parse_log_float(r"продолжение=([+-]?[0-9.]+)\s*ATR", block, 0.0)
    oi_unwind = parse_log_float(r"OI_unwind_from_peak=([+-]?[0-9.]+)%", block, None)
    delta = parse_log_float(r"delta=([+-]?[0-9.]+)\s*BTC", block, 0.0)
    volume = parse_log_float(r"volume=([+-]?[0-9.]+)\s*BTC", block, 0.0)
    high = parse_log_float(r"high=([0-9]+(?:\.[0-9]+)?)", block, price)
    low = parse_log_float(r"low=([0-9]+(?:\.[0-9]+)?)", block, price)
    delta_z = parse_log_float(r"deltaZ=([+-]?[0-9.]+)", block, None)
    delta_share = parse_log_float(r"delta/vol=([+-]?[0-9.]+)%", block, None)
    doi = parse_log_float(r"dOI=([+-]?[0-9.]+)\s*BTC", block, None)
    doi_pct = parse_log_float(r"dOI%=([+-]?[0-9.]+)%", block, None)

    for health in reversed(_active_move_health):
        if health["lifecycle_status"] != "ACTIVE":
            continue
        if health["direction_side"] == direction and ts - health["start_time"] <= 20 * 60:
            health.setdefault("shock_updates", []).append({
                "time": ts,
                "tf": "1m",
                "price": price,
                "high": high,
                "low": low,
                "delta": delta,
                "volume": volume,
                "continuation_atr": continuation_atr,
                "oi_unwind_from_peak_pct": oi_unwind,
            })
            health["last_source_event_time"] = ts
            append_dataset(
                ACTIVE_MOVE_HEALTH_JSONL,
                {"event": "ACTIVE_MOVE_SHOCK_UPDATE", "health_id": health["health_id"],
                 "timestamp": ts, "direction": health["direction"],
                 "source_tf": "1m", "price": price, "delta": delta,
                 "continuation_atr": continuation_atr, "oi_unwind_from_peak_pct": oi_unwind},
            )
            return health
        if health["direction_side"] != direction and ts >= health["start_time"]:
            health["lifecycle_status"] = "SUPERSEDED_BY_OPPOSITE_SHOCK"
            append_dataset(
                ACTIVE_MOVE_HEALTH_JSONL,
                {"event": "ACTIVE_MOVE_SUPERSEDED", "health_id": health["health_id"],
                 "timestamp": ts, "reason": "opposite_shock_active"},
            )

    _active_move_number += 1
    health = {
        "event": "ACTIVE_MOVE_HEALTH_OPENED",
        "health_id": f"H{_active_move_number:06d}",
        "direction": side_direction(direction),
        "direction_side": direction,
        "start_time": ts,
        "start_price": as_float(price, 0.0),
        "latest_extreme": as_float(high if direction == "UP" else low, price),
        "best_progress": 0.0,
        "last_progress_time": ts,
        "last_bar_ts": None,
        "no_progress_bars": 0,
        "state": "HEALTHY",
        "lifecycle_status": "ACTIVE",
        "same_side_attacks": [],
        "opposite_attacks": [],
        "shock_updates": [],
        "last_doi": doi,
        "last_doi_pct": doi_pct,
        "continuation_atr_at_open": continuation_atr,
        "oi_unwind_from_peak_pct_at_open": oi_unwind,
        "response_count": 0,
        "termination_confirmed": False,
        "source_tf": "1m",
        "source_price": price,
        "source_delta": delta,
        "source_volume": volume,
        "source_delta_z": delta_z,
        "source_delta_share": (delta_share / 100.0 if delta_share is not None else None),
        "source_high": high,
        "source_low": low,
    }
    health["quality_context"] = _health_quality(
        health,
        delta=delta,
        delta_z=delta_z,
        delta_share=(delta_share / 100.0 if delta_share is not None else None),
        extension_atr=continuation_atr,
        retained_fraction=1.0,
        active_leg="EFFECTIVE",
        efficiency_trend="IMPROVING",
    )
    _active_move_health.append(health)
    append_dataset(ACTIVE_MOVE_HEALTH_JSONL, health)
    record_quality_event(
        "ACTIVE_MOVE_HEALTH_OPENED",
        ts,
        health["direction"],
        {"health": health},
        health["quality_context"],
    )
    return health


def update_active_move_health(current, oi_change=None, oi_change_pct=None):
    """Research-only health state over closed 1m bars.

    This layer does not modify CASE1/CASE3 and does not create an opposite
    entry. It measures the loss of reward, opposite response, and termination
    of the original move.
    """
    atr = _last_atr or 1.0
    bar_ts = current["ts"]
    now = bar_ts / 1000.0
    close = as_float(current.get("close"), 0.0)
    high = as_float(current.get("high"), close)
    low = as_float(current.get("low"), close)
    delta = as_float(current.get("delta"), 0.0)
    volume = as_float(current.get("volume"), 0.0)

    for health in list(_active_move_health):
        if health.get("lifecycle_status") != "ACTIVE":
            continue
        if now < health["start_time"] or health.get("last_bar_ts") == bar_ts:
            continue
        health["last_bar_ts"] = bar_ts
        health["last_doi"] = oi_change
        health["last_doi_pct"] = oi_change_pct
        side = health["direction_side"]
        sign = side_sign(side)
        extreme = high if side == "UP" else low
        progress = max(0.0, sign * (extreme - health["start_price"]))
        previous_progress = health["best_progress"]
        new_extreme = progress > previous_progress + 1e-9
        incremental = max(0.0, progress - previous_progress)
        retained = max(0.0, sign * (close - health["start_price"]))
        retained_fraction = retained / progress if progress > 0 else 0.0
        if new_extreme:
            health["best_progress"] = progress
            health["latest_extreme"] = extreme
            health["last_progress_time"] = now
            health["no_progress_bars"] = 0
        else:
            health["no_progress_bars"] += 1

        same_side = (delta > 0 and side == "UP") or (delta < 0 and side == "DOWN")
        opposite = (delta < 0 and side == "UP") or (delta > 0 and side == "DOWN")
        attack = {
            "timestamp": now,
            "bar_ts": bar_ts,
            "delta": delta,
            "abs_delta": abs(delta),
            "volume": volume,
            "new_extreme": new_extreme,
            "incremental_extension": incremental,
            "incremental_extension_atr": incremental / atr,
            "retained_reward": retained,
            "retained_reward_atr": retained / atr,
            "retained_fraction": retained_fraction,
            "efficiency_delta": (incremental / abs(delta) if delta else 0.0),
            "efficiency_volume": (incremental / volume if volume else 0.0),
            "dOI": oi_change,
            "dOI_pct": oi_change_pct,
        }
        if same_side and delta != 0:
            previous_attack = health["same_side_attacks"][-1] if health["same_side_attacks"] else None
            if previous_attack is not None:
                attack["efficiency_change_vs_previous"] = (
                    attack["efficiency_delta"] - previous_attack.get("efficiency_delta", 0.0)
                )
                attack["retention_change_vs_previous"] = (
                    attack["retained_fraction"] - previous_attack.get("retained_fraction", 0.0)
                )
            else:
                attack["efficiency_change_vs_previous"] = None
                attack["retention_change_vs_previous"] = None
            health["same_side_attacks"].append(attack)
            append_dataset(
                ACTIVE_MOVE_HEALTH_JSONL,
                {"event": "ACTIVE_MOVE_SAME_SIDE_ATTACK", "health_id": health["health_id"],
                 "direction": health["direction"], "state": health["state"], "attack": attack},
            )
        elif opposite and delta != 0:
            counter_extension = max(
                0.0,
                (health["latest_extreme"] - low) if side == "UP" else (high - health["latest_extreme"]),
            )
            counter_retained = max(
                0.0,
                (health["latest_extreme"] - close) if side == "UP" else (close - health["latest_extreme"]),
            )
            attack.update({
                "extension_reward": counter_extension,
                "extension_reward_atr": counter_extension / atr,
                "retained_reward": counter_retained,
                "retained_reward_atr": counter_retained / atr,
                "retained_fraction": counter_retained / counter_extension if counter_extension else 0.0,
            })
            health["opposite_attacks"].append(attack)
            append_dataset(
                ACTIVE_MOVE_HEALTH_JSONL,
                {"event": "ACTIVE_MOVE_OPPOSITE_ATTACK", "health_id": health["health_id"],
                 "direction": health["direction"], "state": health["state"], "attack": attack},
            )

        last_attack = health["same_side_attacks"][-1] if health["same_side_attacks"] else None
        previous_attack = (
            health["same_side_attacks"][-2]
            if len(health["same_side_attacks"]) >= 2 else None
        )
        decay_evidence = (
            last_attack is not None
            and previous_attack is not None
            and (
                not new_extreme
                or last_attack["efficiency_delta"] <= previous_attack["efficiency_delta"]
                or last_attack["retained_fraction"] < previous_attack["retained_fraction"]
            )
        )
        counter_extension_atr = (
            max(0.0, (health["latest_extreme"] - low) if side == "UP" else (high - health["latest_extreme"])) / atr
        )
        if health["state"] == "HEALTHY" and (
            decay_evidence
            or (opposite and not new_extreme and counter_extension_atr >= BASELINE.PS_MIN_FAILURE_REWARD_ATR)
        ):
            health["state"] = "EFFICIENCY_DECAY"
            context = _health_quality(
                health,
                delta=delta,
                extension_atr=max(counter_extension_atr, incremental / atr),
                retained_fraction=retained_fraction,
                structure="NONE",
                active_leg="DECAYING",
                efficiency_trend="DETERIORATING",
            )
            record_quality_event(
                "ACTIVE_MOVE_EFFICIENCY_DECAY",
                now,
                health["direction"],
                {"health_id": health["health_id"], "health": health, "bar": attack},
                context,
            )
            log_event(
                "ACTIVE_MOVE_EFFICIENCY_DECAY",
                (
                    f"[WATCH] {side_word(side).capitalize()} теряют эффективность.\n"
                    f"health_id={health['health_id']} | price={close:.1f}\n"
                    f"incremental_extension_ATR={incremental / atr:.3f} | "
                    f"retained_fraction={retained_fraction:.3f}\n"
                    "Это предупреждение, не выход и не вход."
                ),
            )

        if health["state"] == "OPPOSITE_RESPONSE" and new_extreme:
            health["state"] = "EFFICIENCY_DECAY"
            health["response_failed_by_new_extreme"] = True
            record_quality_event(
                "ACTIVE_MOVE_RESPONSE_WEAKENED",
                now,
                health["direction"],
                {"health_id": health["health_id"], "reason": "original_side_made_new_extreme_after_response"},
                _health_quality(health, delta=delta, extension_atr=incremental / atr,
                                retained_fraction=retained_fraction, active_leg="DECAYING"),
            )

        response_reward_atr = counter_extension_atr
        response_ready = (
            health["state"] == "EFFICIENCY_DECAY"
            and opposite
            and abs(delta) >= BASELINE.PS_MIN_POST_COUNTER_DELTA
            and response_reward_atr >= BASELINE.PS_MIN_FAILURE_REWARD_ATR
            and not new_extreme
        )
        if response_ready:
            health["state"] = "OPPOSITE_RESPONSE"
            health["response_count"] += 1
            health["last_response_time"] = now
            health["last_response_price"] = close
            health["last_response_reward_atr"] = response_reward_atr
            context = _health_quality(
                health,
                delta=delta,
                extension_atr=response_reward_atr,
                retained_fraction=(
                    max(0.0, (health["latest_extreme"] - close) if side == "UP" else (close - health["latest_extreme"]))
                    / max(0.000001, (health["latest_extreme"] - low) if side == "UP" else (high - health["latest_extreme"]))
                ),
                structure="RECLAIM",
                active_leg="DECAYING",
                efficiency_trend="DETERIORATING",
            )
            record_quality_event(
                "ACTIVE_MOVE_OPPOSITE_RESPONSE",
                now,
                health["direction"],
                {
                    "health_id": health["health_id"],
                    "state": health["state"],
                    "response_reward_atr": response_reward_atr,
                    "delta": delta,
                    "dOI": oi_change,
                    "dOI_pct": oi_change_pct,
                },
                context,
            )
            create_exit_anchor(
                health,
                "OPPOSITE_RESPONSE",
                now,
                close,
                atr,
                context,
                "ACTIVE_MOVE_OPPOSITE_RESPONSE",
                payload={
                    "response_reward_atr": response_reward_atr,
                    "delta": delta,
                    "opposite_attacks": health["opposite_attacks"][-3:],
                },
                sound=True,
            )

        if health["state"] == "OPPOSITE_RESPONSE" and not new_extreme and now > health.get("last_response_time", now):
            retained_opposite = max(
                0.0,
                (health["latest_extreme"] - close) if side == "UP" else (close - health["latest_extreme"]),
            ) / atr
            no_recovery = (
                close <= health.get("last_response_price", close)
                if side == "UP"
                else close >= health.get("last_response_price", close)
            )
            if retained_opposite >= BASELINE.PS_MIN_FAILURE_REWARD_ATR and no_recovery:
                health["state"] = "MOVE_TERMINATION_CONFIRMED"
                health["lifecycle_status"] = "TERMINATED"
                health["termination_confirmed"] = True
                context = _health_quality(
                    health,
                    delta=delta,
                    extension_atr=retained_opposite,
                    retained_fraction=1.0,
                    structure="RECLAIM",
                    active_leg="STALLED",
                    efficiency_trend="DETERIORATING",
                )
                record_quality_event(
                    "MOVE_TERMINATION_CONFIRMED",
                    now,
                    health["direction"],
                    {
                        "health_id": health["health_id"],
                        "state": health["state"],
                        "retained_opposite_reward_atr": retained_opposite,
                        "no_recovery": no_recovery,
                    },
                    context,
                )
                create_exit_anchor(
                    health,
                    "MOVE_TERMINATION_CONFIRMED",
                    now,
                    close,
                    atr,
                    context,
                    "MOVE_TERMINATION_CONFIRMED",
                    payload={
                        "retained_opposite_reward_atr": retained_opposite,
                        "no_recovery": no_recovery,
                        "response_count": health["response_count"],
                    },
                    sound=True,
                )

        append_dataset(
            ACTIVE_MOVE_HEALTH_JSONL,
            {
                "event": "ACTIVE_MOVE_HEALTH_BAR",
                "health_id": health["health_id"],
                "timestamp": now,
                "bar_ts": bar_ts,
                "direction": health["direction"],
                "state": health["state"],
                "lifecycle_status": health["lifecycle_status"],
                "price": close,
                "high": high,
                "low": low,
                "delta": delta,
                "volume": volume,
                "new_extreme": new_extreme,
                "incremental_extension_atr": incremental / atr,
                "retained_reward_atr": retained / atr,
                "retained_fraction": retained_fraction,
                "no_progress_bars": health["no_progress_bars"],
                "dOI": oi_change,
                "dOI_pct": oi_change_pct,
            },
        )


def update_test_entry_outcomes():
    now = now_ts()
    for entry in list(_test_entries):
        start = entry["entry_time"]
        entry_bar_ts = entry.get("entry_bar_ts")
        for horizon in ENTRY_OUTCOME_HORIZONS:
            key = f"{horizon}m"
            if key in entry["outcomes"] or now - start < horizon * 60:
                continue
            try:
                rows = minute_window(start, start + horizon * 60)
                rows = outcome_rows(rows, start, entry_bar_ts)
                outcome = path_outcome(
                    "UP" if entry["direction"] == "LONG" else "DOWN",
                    entry["entry_price"],
                    entry["atr"],
                    rows,
                )
            except Exception:
                outcome = None
            if outcome is None:
                continue
            outcome["horizon_minutes"] = horizon
            entry["outcomes"][key] = outcome
            append_dataset(
                TEST_ENTRY_JSONL,
                {
                    "event": "TEST_ENTRY_OUTCOME",
                    "event_id": entry["event_id"],
                    "setup_family": entry["setup_family"],
                    "direction": entry["direction"],
                    "horizon_minutes": horizon,
                    "outcome": outcome,
                },
            )
        if entry["outcomes"] and not entry["virtual_outcomes"]:
            try:
                rows = outcome_rows(
                    minute_window(start, start + 60 * 60),
                    start,
                    entry_bar_ts,
                )
                for tp_atr, sl_atr in VIRTUAL_SCHEMES:
                    key = f"TP{tp_atr:g}_SL{sl_atr:g}"
                    entry["virtual_outcomes"][key] = virtual_outcome(
                        "UP" if entry["direction"] == "LONG" else "DOWN",
                        entry["entry_price"],
                        entry["atr"],
                        rows,
                        tp_atr,
                        sl_atr,
                    )
                append_dataset(
                    TEST_ENTRY_JSONL,
                    {
                        "event": "TEST_ENTRY_VIRTUAL_OUTCOME",
                        "event_id": entry["event_id"],
                        "virtual_outcomes": entry["virtual_outcomes"],
                    },
                )
            except Exception:
                pass


def update_watch(watch, bar, oi_change=None, oi_change_pct=None):
    if bar["ts"] == watch["last_bar_ts"]:
        return False
    watch["last_bar_ts"] = bar["ts"]
    watch["bars"].append(bar)
    watch["episode"]["attacks"].append(
        attack_record(
            bar,
            watch["side"],
            watch["pressure_atr"],
            watch["reference"],
            oi_change=oi_change,
            oi_change_pct=oi_change_pct,
        )
    )

    side = watch["side"]
    sign = side_sign(side)
    if sign * bar["delta"] > 0:
        watch["same_delta"] += abs(bar["delta"])
    else:
        watch["opposite_delta"] += abs(bar["delta"])

    if oriented_progress(side, watch["reference"], bar["close"]) > 0:
        watch["outside_closes"] += 1

    previous_max_extension = watch["max_extension"]
    metrics = watch_metrics(watch)
    incremental_extension = max(
        0.0,
        metrics["extension"] - previous_max_extension,
    )
    metrics["previous_max_extension"] = previous_max_extension
    metrics["incremental_extension"] = incremental_extension
    metrics["incremental_extension_atr"] = (
        incremental_extension / watch["pressure_atr"]
        if watch["pressure_atr"]
        else 0.0
    )
    watch["last_incremental_extension"] = incremental_extension
    watch["last_incremental_extension_atr"] = metrics["incremental_extension_atr"]

    # Это не жёсткий торговый фильтр: только фиксация первого признака
    # того, что initial breakout получает новый incremental reward.
    first_continuation = (
        metrics["retained"] > 0
        and incremental_extension > 0
    )
    if first_continuation and not watch["continuation_candidate"]:
        watch["continuation_candidate"] = True
        episode = watch["episode"]
        episode["continuation_time"] = bar["ts"] / 1000.0
        episode["anchors"]["EARLY_CONTINUATION"] = {
            "time": episode["continuation_time"],
            "price": bar["close"],
            "atr": watch["pressure_atr"],
            "incremental_extension": incremental_extension,
            "incremental_extension_atr": metrics["incremental_extension_atr"],
        }
        log_event(
            "PRESSURE_CONTINUATION_CANDIDATE",
            watch_body(watch, metrics, "EARLY_CONTINUATION"),
        )
        append_jsonl(
            "PRESSURE_CONTINUATION_CANDIDATE",
            episode,
            {"continuation": metrics},
        )

    # Обновляем максимум только после сравнения с предыдущим максимумом.
    watch["max_extension"] = max(watch["max_extension"], metrics["extension"])
    watch["max_retained"] = max(watch["max_retained"], metrics["retained"])
    watch["max_pullback"] = max(watch["max_pullback"], metrics["pullback"])

    if (
        watch["continuation_candidate"]
        and not watch["acceptance"]
        and watch["outside_closes"] >= ACCEPTANCE_OUTSIDE_CLOSES
        and metrics["retained"] > 0
    ):
        watch["acceptance"] = True
        episode = watch["episode"]
        episode["anchors"]["ACCEPTANCE"] = {
            "time": bar["ts"] / 1000.0,
            "price": bar["close"],
            "atr": watch["pressure_atr"],
        }
        context = quality_context(
            abs_delta=abs(metrics["same_delta"]),
            extension_atr=metrics["extension_atr"],
            retained_fraction=metrics["retained_fraction"],
            sequence="REPEATED",
            efficiency_trend="MIXED",
            active_leg="EFFECTIVE",
            oi_state=(
                "BUILDING"
                if as_float(episode.get("oi_context"), 0.0) > 0
                else "DESTROYING"
                if as_float(episode.get("oi_context"), 0.0) < 0
                else "APPROX_STABLE"
            ),
            structure="ACCEPTANCE",
        )
        record_quality_event(
            "PRESSURE_ACCEPTANCE",
            bar["ts"] / 1000.0,
            side_direction(watch["side"]),
            {
                "episode_id": episode["id"],
                "outside_closes": watch["outside_closes"],
                "extension_reward_atr": metrics["extension_atr"],
                "retained_reward_atr": metrics["retained_atr"],
                "retained_fraction": metrics["retained_fraction"],
            },
            context,
        )
        create_test_entry(
            "PRESSURE_CONTINUATION",
            side_direction(watch["side"]),
            now_ts(),
            bar["close"],
            watch["pressure_atr"],
            context,
            "PRESSURE_ACCEPTANCE",
            entry_bar_ts=bar["ts"] / 1000.0,
            payload={
                "episode_id": episode["id"],
                "outside_closes": watch["outside_closes"],
                "extension_atr": metrics["extension_atr"],
                "retained_fraction": metrics["retained_fraction"],
            },
        )

    returned = oriented_progress(side, watch["reference"], bar["close"]) < 0
    failure_reward = -metrics["retained"]
    failure = (
        returned
        and watch["opposite_delta"] >= BASELINE.PS_MIN_POST_COUNTER_DELTA
        and failure_reward >= BASELINE.PS_MIN_FAILURE_REWARD_ATR * watch["pressure_atr"]
    )
    if failure:
        watch["failure_candidate"] = True

    return True


def finalize_watch(watch, reason):
    global _active_watch
    metrics = watch_metrics(watch)
    episode = watch["episode"]

    if watch["failure_candidate"]:
        result = "PRESSURE_FAILURE"
    elif watch["continuation_candidate"]:
        result = "PRESSURE_CONTINUATION"
    else:
        result = "NO_RESOLUTION"

    episode["class"] = result
    episode["finalized_time"] = now_ts()
    episode["counter_start_bar_ts"] = watch["bars"][-1]["ts"]
    episode["latest_extreme"] = (
        watch["reference"] + metrics["extension"]
        if watch["side"] == "UP"
        else watch["reference"] - metrics["extension"]
    )
    log_event(
        result,
        watch_body(watch, metrics, result)
        + f"\nresolution_reason={reason}",
    )
    append_jsonl(
        result,
        episode,
        {"resolution": metrics, "resolution_reason": reason},
    )
    record_quality_event(
        result,
        episode["finalized_time"],
        episode["direction"],
        {
            "episode_id": episode["id"],
            "resolution_reason": reason,
            "resolution": metrics,
        },
        quality_context(
            abs_delta=abs(episode.get("pressure_cumulative_delta", 0.0)),
            extension_atr=metrics.get("extension_atr", 0.0),
            retained_fraction=metrics.get("retained_fraction", 0.0),
            sequence="PERSISTENT",
            active_leg=("STALLED" if result == "PRESSURE_FAILURE" else "EFFECTIVE"),
            structure=("RECLAIM" if result == "PRESSURE_FAILURE" else "ACCEPTANCE"),
        ),
    )
    _active_watch = None


def attach_active_leg(ts, price):
    # CASE3 gives the direction of its own active leg. Attach only to the
    # pressure episode with the same direction; do not cross-link regimes.
    return _attach_active_leg_for_side(ts, price, None)


def _attach_active_leg_for_side(ts, price, side):
    with _state_lock:
        candidates = [
            e for e in _episodes
            if e["active_leg_time"] is None
            and now_ts() - e["pressure_detected_time"] <= 20 * 60
            and (side is None or e["side"] == side)
        ]
        if not candidates:
            return
        episode = candidates[-1]
        episode["active_leg_time"] = ts
        episode["anchors"]["ACTIVE_LEG_CONFIRMATION"] = {
            "time": ts,
            "price": price,
            "atr": episode["pressure_atr"],
        }
        log_event(
            "PRESSURE_ACTIVE_LEG_REFERENCE",
            (
                f"episode_id={episode['id']}\n"
                f"anchor=ACTIVE_LEG_CONFIRMATION | time={fmt_ts(ts)} | price={price:.1f}\n"
                "Сохранена поздняя точка CASE3 для измерения стоимости запаздывания."
            ),
        )
        append_jsonl(
            "PRESSURE_ACTIVE_LEG_REFERENCE",
            episode,
            {"active_leg": episode["anchors"]["ACTIVE_LEG_CONFIRMATION"]},
        )


def counter_sequence_context(episode, counter_side, ts):
    previous = [
        item for item in episode.get("counter_events", [])
        if item.get("counter_side") == counter_side
    ]
    if not previous or ts - previous[-1]["time"] > QUALITY_SEQUENCE_GAP_SEC:
        return {
            "sequence_id": f"{episode['id']}-{counter_side}-01",
            "attack_index": 1,
            "sequence_duration": 0.0,
            "cumulative_delta": 0.0,
            "cumulative_volume": 0.0,
            "cumulative_dOI": 0.0,
            "number_of_attacks": 1,
            "efficiency_change_vs_previous": None,
            "efficiency_change_vs_sequence_median": None,
            "reward_change": None,
            "retention_change": None,
        }
    last = previous[-1]
    sequence_id = last.get("sequence_id", f"{episode['id']}-{counter_side}-01")
    sequence = [item for item in previous if item.get("sequence_id") == sequence_id]
    efficiencies = [as_float(item.get("efficiency_delta"), 0.0) for item in sequence]
    current_eff = 0.0
    previous_eff = as_float(last.get("efficiency_delta"), 0.0)
    median_eff = (
        sorted(efficiencies)[len(efficiencies) // 2]
        if efficiencies
        else 0.0
    )
    return {
        "sequence_id": sequence_id,
        "attack_index": len(sequence) + 1,
        "sequence_duration": ts - sequence[0]["time"],
        "cumulative_delta": sum(abs(as_float(item.get("delta"), 0.0)) for item in sequence),
        "cumulative_volume": sum(as_float(item.get("volume"), 0.0) for item in sequence),
        "cumulative_dOI": sum(as_float(item.get("dOI"), 0.0) or 0.0 for item in sequence),
        "number_of_attacks": len(sequence) + 1,
        "efficiency_change_vs_previous": current_eff - previous_eff,
        "efficiency_change_vs_sequence_median": current_eff - median_eff,
        "reward_change": None,
        "retention_change": None,
    }


def update_counter_event(episode, bar, oi_change=None, oi_change_pct=None):
    if episode["class"] not in {"PRESSURE_CONTINUATION", "PRESSURE_FAILURE"}:
        return
    bar_ts = bar["ts"]
    seen_bar_ts = episode.setdefault("_counter_seen_bar_ts", [])
    if bar_ts in seen_bar_ts:
        return
    seen_bar_ts.append(bar_ts)
    if (
        episode.get("counter_start_bar_ts") is not None
        and bar_ts <= episode["counter_start_bar_ts"]
    ):
        return

    side = episode["side"]
    opposite = (side == "UP" and bar["delta"] < 0) or (side == "DOWN" and bar["delta"] > 0)
    if side == "UP":
        episode["latest_extreme"] = max(
            episode.get("latest_extreme", episode["pressure_reference_price"]),
            bar["high"],
        )
        reference_extreme = episode["latest_extreme"]
        extension_reward = max(0.0, reference_extreme - bar["low"])
        retained_reward = max(0.0, reference_extreme - bar["close"])
        counter_side = "SELL"
    else:
        episode["latest_extreme"] = min(
            episode.get("latest_extreme", episode["pressure_reference_price"]),
            bar["low"],
        )
        reference_extreme = episode["latest_extreme"]
        extension_reward = max(0.0, bar["high"] - reference_extreme)
        retained_reward = max(0.0, bar["close"] - reference_extreme)
        counter_side = "BUY"

    # Это существующий исследовательский порог 001, а не новый торговый фильтр.
    significant_counter = abs(bar["delta"]) >= BASELINE.PS_MIN_POST_COUNTER_DELTA
    if not (opposite and significant_counter and extension_reward > 0):
        return

    event = {
        "index": len(episode["counter_events"]) + 1,
        "time": bar_ts / 1000.0,
        "counter_side": counter_side,
        "delta": bar["delta"],
        "abs_delta": abs(bar["delta"]),
        "volume": bar["volume"],
        "delta_volume_ratio": (
            abs(bar["delta"]) / bar["volume"] if bar["volume"] else 0.0
        ),
        "price": bar["close"],
        "reference_extreme": reference_extreme,
        "extension_reward": extension_reward,
        "extension_reward_atr": extension_reward / episode["pressure_atr"] if episode["pressure_atr"] else 0.0,
        "retained_reward": retained_reward,
        "retained_reward_atr": retained_reward / episode["pressure_atr"] if episode["pressure_atr"] else 0.0,
        "dOI": oi_change,
        "dOI_pct": oi_change_pct,
        "efficiency_delta": (
            extension_reward / episode["pressure_atr"] / abs(bar["delta"])
            if episode["pressure_atr"] and abs(bar["delta"]) else 0.0
        ),
        "efficiency_volume": (
            extension_reward / episode["pressure_atr"] / bar["volume"]
            if episode["pressure_atr"] and bar["volume"] else 0.0
        ),
        "outcome": "UNRESOLVED",
    }
    sequence = counter_sequence_context(episode, counter_side, bar_ts / 1000.0)
    event.update(sequence)
    event["cumulative_delta"] += event["abs_delta"]
    event["cumulative_volume"] += event["volume"]
    event["cumulative_dOI"] += as_float(event.get("dOI"), 0.0) or 0.0
    event["sequence_duration"] = (
        event["sequence_duration"]
        if event["attack_index"] > 1
        else 0.0
    )
    event["efficiency_change_vs_previous"] = None
    event["efficiency_change_vs_sequence_median"] = None
    if event["attack_index"] > 1:
        prior = [
            item for item in episode["counter_events"]
            if item.get("sequence_id") == event["sequence_id"]
        ]
        if prior:
            previous_eff = as_float(prior[-1].get("efficiency_delta"), 0.0)
            median_eff = statistics.median(
                as_float(item.get("efficiency_delta"), 0.0)
                for item in prior
            )
            event["efficiency_change_vs_previous"] = event["efficiency_delta"] - previous_eff
            event["efficiency_change_vs_sequence_median"] = event["efficiency_delta"] - median_eff
            event["efficiency_trend"] = (
                "EFFICIENCY_IMPROVING"
                if event["efficiency_change_vs_previous"] > 0
                else "EFFICIENCY_DETERIORATING"
                if event["efficiency_change_vs_previous"] < 0
                else "EFFICIENCY_MIXED"
            )
    else:
        event["efficiency_trend"] = "EFFICIENCY_MIXED"
    episode["counter_events"].append(event)
    context = quality_context(
        abs_delta=event["abs_delta"],
        delta_share=event["delta_volume_ratio"],
        extension_atr=event["extension_reward_atr"],
        retained_fraction=(
            retained_reward / extension_reward if extension_reward else 0.0
        ),
        sequence=("REPEATED" if event["attack_index"] > 1 else "ISOLATED"),
        efficiency_trend=event["efficiency_trend"],
        active_leg="EFFECTIVE",
        oi_state=(
            "BUILDING" if as_float(oi_change, 0.0) > 0
            else "DESTROYING" if as_float(oi_change, 0.0) < 0
            else "APPROX_STABLE"
        ),
        structure="RECLAIM",
    )
    event["quality_context"] = context
    log_event(
        "PRESSURE_COUNTER_EVENT",
        (
            f"episode_id={episode['id']}\n"
            f"time={fmt_ts(event['time'])} | counter_side={counter_side}\n"
            f"counter_delta={bar['delta']:+.1f} BTC | price={bar['close']:.1f}\n"
            f"reference_extreme={reference_extreme:.1f} | "
            f"extension_reward={extension_reward:+.1f} USD | "
            f"retained_reward={retained_reward:+.1f} USD\n"
            "Counter-event сохранён в массиве; это не автоматический reversal."
        ),
    )
    append_jsonl(
        "PRESSURE_COUNTER_EVENT",
        episode,
        {"counter": event},
    )
    record_quality_event(
        "PRESSURE_COUNTER_ATTACK",
        event["time"],
        "LONG" if counter_side == "BUY" else "SHORT",
        {
            "episode_id": episode["id"],
            "counter": event,
        },
        context,
    )


def minute_window(start_time, end_time):
    rows = BASELINE._ps_get(
        "/fapi/v1/klines",
        {
            "symbol": SYMBOL,
            "interval": "1m",
            "startTime": int(start_time * 1000),
            "endTime": int(end_time * 1000),
            "limit": 1000,
        },
    )
    return [
        {
            "ts": int(row[0]) / 1000.0,
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
        }
        for row in rows
    ]


def retention_snapshot(episode, anchor, horizon):
    rows = minute_window(anchor["time"], anchor["time"] + horizon * 60)
    if not rows:
        return None
    side = episode["side"]
    entry = anchor["price"]
    highest = max(row["high"] for row in rows)
    lowest = min(row["low"] for row in rows)
    close = rows[-1]["close"]
    extension = max(
        0.0,
        oriented_progress(side, entry, highest if side == "UP" else lowest),
    )
    retained = max(0.0, oriented_progress(side, entry, close))
    return {
        "horizon_minutes": horizon,
        "close": close,
        "extension_reward": extension,
        "retained_at_close": retained,
        "retained_fraction": retained / extension if extension else 0.0,
    }


def update_pressure_counter_outcomes(episode):
    for event in episode.get("counter_events", []):
        event.setdefault("outcomes", {})
        start = event["time"]
        side = "UP" if event["counter_side"] == "BUY" else "DOWN"
        atr = episode.get("pressure_atr") or 0.0
        for horizon in (1, 3, 5, 15):
            key = f"{horizon}m"
            if key in event["outcomes"]:
                continue
            if now_ts() - start < horizon * 60:
                continue
            try:
                rows = minute_window(start, start + horizon * 60)
                result = path_outcome(side, event["price"], atr, rows)
            except Exception:
                result = None
            if result is not None:
                event["outcomes"][key] = result
                if horizon in (1, 3, 5):
                    event[f"retained_reward_{horizon}m_atr"] = result["retained_atr"]
                    event[f"retained_fraction_{horizon}m"] = result["retained_fraction"]
                if horizon in (5, 15):
                    event[f"MFE_ATR_{horizon}m"] = result["mfe_atr"]
                    event[f"MAE_ATR_{horizon}m"] = result["mae_atr"]
                append_dataset(
                    QUALITY_JSONL,
                    {
                        "event": "COUNTER_ATTACK_OUTCOME",
                        "episode_id": episode["id"],
                        "counter_index": event["index"],
                        "counter_side": event["counter_side"],
                        "horizon_minutes": horizon,
                        "outcome": result,
                    },
                )
        if "3m" in event["outcomes"]:
            event["retained_efficiency_3m"] = (
                event["outcomes"]["3m"]["retained_atr"]
                / event["abs_delta"]
                if event.get("abs_delta") else 0.0
            )


def update_outcomes():
    now = now_ts()
    with _state_lock:
        episodes = list(_episodes)
    for episode in episodes:
        update_pressure_counter_outcomes(episode)
        for anchor_name, anchor in list(episode["anchors"].items()):
            for retention_horizon in RETENTION_HORIZONS:
                retention_key = (anchor_name, "RETENTION", retention_horizon)
                if retention_key in episode["outcomes_written"]:
                    continue
                if now - anchor["time"] < retention_horizon * 60:
                    continue
                try:
                    retention = retention_snapshot(
                        episode,
                        anchor,
                        retention_horizon,
                    )
                except Exception:
                    retention = None
                if retention is None:
                    continue
                episode["retention"].setdefault(anchor_name, {})[
                    f"{retention_horizon}m"
                ] = retention
                log_event(
                    "PRESSURE_RETENTION",
                    (
                        f"episode_id={episode['id']} | anchor={anchor_name}\n"
                        f"retained_{retention_horizon}m={retention['retained_at_close']:+.1f} USD | "
                        f"extension_{retention_horizon}m={retention['extension_reward']:+.1f} USD | "
                        f"retained_fraction_{retention_horizon}m={retention['retained_fraction']:+.3f}\n"
                        "Источник: historical 1m OHLC."
                    ),
                )
                append_jsonl(
                    "PRESSURE_RETENTION",
                    episode,
                    {
                        "anchor": anchor_name,
                        "retention": retention,
                    },
                )
                episode["outcomes_written"].add(retention_key)

            for horizon in OUTCOME_HORIZONS:
                key = (anchor_name, horizon)
                if key in episode["outcomes_written"]:
                    continue
                if now - anchor["time"] < horizon * 60:
                    continue
                try:
                    extremes = BASELINE.one_minute_extremes(
                        anchor["time"],
                        anchor["time"] + horizon * 60,
                    )
                except Exception:
                    extremes = None
                if extremes is None:
                    continue
                highest, lowest = extremes
                if episode["side"] == "UP":
                    mfe = max(0.0, highest - anchor["price"])
                    mae = max(0.0, anchor["price"] - lowest)
                else:
                    mfe = max(0.0, anchor["price"] - lowest)
                    mae = max(0.0, highest - anchor["price"])
                atr = anchor.get("atr") or 0.0
                log_event(
                    "PRESSURE_OUTCOME",
                    (
                        f"episode_id={episode['id']} | anchor={anchor_name} | "
                        f"direction={episode['direction']} | horizon={horizon}m\n"
                        f"entry_price={anchor['price']:.1f} | "
                        f"MFE={mfe:+.1f} USD | MAE={mae:+.1f} USD\n"
                        f"MFE_pct={100.0*mfe/anchor['price']:+.3f}% | "
                        f"MAE_pct={100.0*mae/anchor['price']:+.3f}%\n"
                        f"MFE_ATR={mfe/atr:+.3f} | MAE_ATR={mae/atr:+.3f}\n"
                        "Источник результата: historical 1m OHLC; future leakage не используется."
                    ),
                )
                append_jsonl(
                    "PRESSURE_OUTCOME",
                    episode,
                    {
                        "anchor": anchor_name,
                        "horizon_minutes": horizon,
                        "mfe": mfe,
                        "mae": mae,
                        "mfe_pct": 100.0 * mfe / anchor["price"],
                        "mae_pct": 100.0 * mae / anchor["price"],
                        "mfe_atr": mfe / atr if atr else 0.0,
                        "mae_atr": mae / atr if atr else 0.0,
                        "retention": episode["retention"].get(anchor_name, {}),
                    },
                )
                episode["outcomes_written"].add(key)


def pressure_research_engine():
    global _active_watch, _pressure_context, _last_context_key, _last_price, _last_atr, _last_oi
    while True:
        try:
            bars = BASELINE._ps_bars()
            if (
                not isinstance(bars, list)
                or not bars
                or any(not isinstance(bar, dict) for bar in bars)
            ):
                time.sleep(POLL_SECONDS)
                continue
            closed = [b for b in bars if b["closed"]]
            current = bars[-1]
            if not isinstance(current, dict):
                time.sleep(POLL_SECONDS)
                continue
            _last_price = current_price_from_bar(current)
            oi = BASELINE._ps_oi()
            _last_oi = oi
            _oi_history.append((now_ts(), oi))
            doi, doi_pct = BASELINE._ps_hist_oi()

            # The research window has its own lifecycle.  Run it on every
            # poll before pressure outcome handling so continuation, failure,
            # and finalization cannot stop post-WATCH collection.
            _record_pressure_post_watch_bars(closed, oi, doi, doi_pct)

            context = BASELINE._ps_context(closed)
            if context is not None:
                _last_atr = context.get("atr")

            if context is not None:
                key = (
                    context["side"],
                    round((context["hi"] if context["side"] == "UP" else context["lo"]) / 25) * 25,
                )
                if key != _last_context_key:
                    _pressure_context = make_context(closed, context["side"], doi, doi_pct)
                    detected_time = now_ts()
                    detected_price = current_price_from_bar(current)
                    with _state_lock:
                        episode = create_episode(
                            context["side"],
                            _pressure_context,
                            detected_time,
                            detected_price,
                        )
                    record_pre_breakout_battle(
                        episode,
                        closed,
                        "PRESSURE_DETECTED",
                        detected_time,
                        detected_price,
                        current["ts"],
                        context["side"],
                        context["atr"],
                        doi,
                        doi_pct,
                    )
                    log_pressure_detected(episode, _pressure_context)
                    _last_context_key = key

                boundary = context["hi"] if context["side"] == "UP" else context["lo"]
                sweep = (
                    (current["high"] - boundary) / context["atr"]
                    if context["side"] == "UP"
                    else (boundary - current["low"]) / context["atr"]
                )
                if _active_watch is None and sweep >= BREAKOUT_ATR and _pressure_context is not None:
                    with _state_lock:
                        episode = _episodes[-1]
                    _active_watch = create_watch(episode, _pressure_context, current, oi)
                    record_pre_breakout_battle(
                        episode,
                        closed,
                        "PRESSURE_BREAKOUT_WATCH",
                        now_ts(),
                        current["close"],
                        current["ts"],
                        context["side"],
                        context["atr"],
                        doi,
                        doi_pct,
                    )
                    who = side_word(context["side"]).upper()
                    log_event(
                        "PRESSURE_BREAKOUT_WATCH",
                        (
                            f"{who} НАЧАЛИ РЕАЛИЗОВЫВАТЬ ДАВЛЕНИЕ.\n"
                            "Цена начала выходить после последовательного давления.\n"
                            "Проверяем две возможности:\n"
                            "1. Исходная сторона сохранит результат — возможное продолжение.\n"
                            "2. Выход провалится и противоположная сторона получит результат — возможный отказ.\n\n"
                            + watch_body(_active_watch, watch_metrics(_active_watch), "PRESSURE_BREAKOUT_WATCH")
                        ),
                    )
                    append_jsonl(
                        "PRESSURE_BREAKOUT_WATCH",
                        episode,
                        {"watch": watch_metrics(_active_watch)},
                    )
                    record_quality_event(
                        "PRESSURE_BREAKOUT_WATCH",
                        current["ts"] / 1000.0,
                        side_direction(context["side"]),
                        {
                            "episode_id": episode["id"],
                            "reference_price": _active_watch["reference"],
                            "attempt_price": _active_watch["attempt_price"],
                            "initial_extension_atr": _active_watch["initial_extension_atr"],
                        },
                        quality_context(
                            abs_delta=abs(current["delta"]),
                            delta_share=(
                                abs(current["delta"]) / current["volume"]
                                if current["volume"] else 0.0
                            ),
                            extension_atr=_active_watch["initial_extension_atr"],
                            retained_fraction=1.0,
                            sequence="REPEATED",
                            active_leg="EFFECTIVE",
                            structure="BREAKOUT",
                        ),
                    )

            if _active_watch is not None:
                changed = update_watch(_active_watch, current, doi, doi_pct)
                if changed:
                    update_counter_event(
                        _active_watch["episode"],
                        current,
                        doi,
                        doi_pct,
                    )
                for window in _pressure_post_watch_windows:
                    if window["episode_id"] == _active_watch["episode"]["id"]:
                        window["continuation_candidate"] = _active_watch["continuation_candidate"]
                        window["failure_candidate"] = _active_watch["failure_candidate"]
                        window["acceptance"] = _active_watch["acceptance"]
                if now_ts() - _active_watch["attempt_time"] > WATCH_TTL_SEC:
                    finalize_watch(_active_watch, "watch_ttl_expired")

            with _state_lock:
                recent = list(_episodes[-3:])
            for episode in recent:
                if _active_watch is None and episode["class"] != "UNRESOLVED":
                    update_counter_event(episode, current, doi, doi_pct)
            update_outcomes()
            update_test_entry_outcomes()
            update_case3_quality(current)
            update_case3_counter_outcomes()
            update_entry_lifecycle(current)
            # Health is evaluated on the latest CLOSED 1m candle so the
            # warning/exit layer cannot use an unfinished candle as proof.
            if closed:
                update_active_move_health(closed[-1], doi, doi_pct)
                # Independent research-only MOVE_ORIGIN_TEST layer.  It
                # consumes only closed 1m bars and does not alter any legacy
                # detector or dataset.
                update_move_origin_test(closed, oi, doi, doi_pct)
                # Independent experimental early-reversal layer.
                update_early_reversal_test(closed, oi, doi, doi_pct)
            time.sleep(POLL_SECONDS)
        except Exception as exc:
            print("Ошибка pressure research:", repr(exc), flush=True)
            traceback.print_exc()
            time.sleep(POLL_SECONDS)


def snapshot_price_falling(start, current):
    return start is not None and current is not None and current < start


def snapshot_price_rising(start, current):
    return start is not None and current is not None and current > start


def position_state_recorder():
    """Optional crowd-position recorder; never used as a trading filter."""
    global _position_state
    while True:
        try:
            rows = BASELINE._ps_get(
                "/futures/data/globalLongShortAccountRatio",
                {"symbol": SYMBOL, "period": "5m", "limit": 1},
            )
            if rows:
                row = rows[-1]
                ts = as_float(row.get("timestamp"), now_ts() * 1000.0) / 1000.0
                long_account = as_float(row.get("longAccount"))
                short_account = as_float(row.get("shortAccount"))
                ratio = as_float(row.get("longShortRatio"))
                if _position_state is None:
                    _position_state = {
                        "position_state_start_time": ts,
                        "position_ratio_start": ratio,
                        "long_account_start": long_account,
                        "short_account_start": short_account,
                        "price_start": _last_price,
                        "OI_start": _last_oi,
                    }
                snapshot = {
                    **_position_state,
                    "position_state_current_time": ts,
                    "position_state_duration": ts - _position_state["position_state_start_time"],
                    "position_ratio_current": ratio,
                    "long_account_current": long_account,
                    "short_account_current": short_account,
                    "price_current": _last_price,
                    "OI_current": _last_oi,
                    "position_ratio_change": (
                        ratio - _position_state["position_ratio_start"]
                        if ratio is not None and _position_state["position_ratio_start"] is not None
                        else None
                    ),
                    "price_change": (
                        _last_price - _position_state["price_start"]
                        if _last_price is not None and _position_state["price_start"] is not None
                        else None
                    ),
                    "OI_change": (
                        _last_oi - _position_state["OI_start"]
                        if _last_oi is not None and _position_state["OI_start"] is not None
                        else None
                    ),
                    "source": "Binance_globalLongShortAccountRatio",
                    "net_position_available": False,
                    "divergence": (
                        "NET_LONGS_RISING_PRICE_FALLING"
                        if long_account is not None
                        and _position_state["long_account_start"] is not None
                        and long_account > _position_state["long_account_start"]
                        and snapshot_price_falling(_position_state["price_start"], _last_price)
                        else "NET_SHORTS_RISING_PRICE_RISING"
                        if short_account is not None
                        and _position_state["short_account_start"] is not None
                        and short_account > _position_state["short_account_start"]
                        and snapshot_price_rising(_position_state["price_start"], _last_price)
                        else None
                    ),
                }
                _position_state = snapshot
                append_dataset(
                    POSITION_STATE_JSONL,
                    {"event": "POSITION_STATE_SAMPLE", **snapshot},
                )
        except Exception as exc:
            print("Position recorder unavailable:", repr(exc), flush=True)
        time.sleep(300)


def parse_log_float(pattern, block, default=None):
    match = re.search(pattern, block, flags=re.IGNORECASE)
    return as_float(match.group(1), default) if match else default


def parse_log_direction(block, field):
    match = re.search(
        rf"{field}:\s*(UP|DOWN|LONG|SHORT|BUY|SELL)",
        block,
        flags=re.IGNORECASE,
    )
    return match.group(1).upper() if match else None


def latest_case3_leg(direction=None):
    for leg in reversed(_case3_legs):
        if direction is None or leg["direction"] == direction:
            return leg
    return None


def process_case3_log_block(block):
    ts = BASELINE.parse_stamp(block)
    price = BASELINE.parse_price(block)
    if price is None:
        price = parse_log_float(r"price=([0-9]+(?:\.[0-9]+)?)", block)
    if price is None:
        price = parse_log_float(r"Price:\s*([0-9]+(?:\.[0-9]+)?)", block)
    if price is None:
        price = _last_price
    if price is None and "CASE3 | ACTIVE_LEG_DETECTED" not in block and "SHOCK_LEG_ACTIVE" not in block:
        return
    price = price or 0.0
    if "SHOCK_LEG_ACTIVE" in block:
        shock_tf = re.search(r"\bTF=(1m|5m|15m|1h)\b", block)
        event_name = f"SHOCK_LEG_ACTIVE:{shock_tf.group(1) if shock_tf else 'unknown'}"
    else:
        event_name = (
            block.split("CASE3 |", 1)[1].split("|", 1)[0].strip()
            if "CASE3 |" in block else ""
        )
    event_key = (
        "CASE3",
        ts,
        event_name,
    )
    if event_key in _processed_log_events:
        return
    _processed_log_events.add(event_key)

    if "SHOCK_LEG_ACTIVE" in block:
        register_active_move_health(ts, price, block)
        return

    if "CASE3 | ACTIVE_LEG_DETECTED" in block:
        direction = parse_log_direction(block, "Направление") or parse_log_direction(block, "Direction")
        if direction in {"UP", "DOWN"}:
            leg = {
                "direction": direction,
                "start_time": ts,
                "start_price": price,
                "latest_extreme": price,
                "last_progress_time": ts,
                "counter_count": 0,
                "new_extreme_after_counter": False,
                "state": "LEG_STILL_EFFECTIVE",
                "counters": [],
            }
            _case3_legs.append(leg)
            _attach_active_leg_for_side(ts, price, direction)
            record_quality_event(
                "CASE3_ACTIVE_LEG",
                ts,
                "LONG" if direction == "UP" else "SHORT",
                {"active_leg": leg},
                quality_context(
                    extension_atr=parse_log_float(r"Movement:\s*([0-9.]+)\s*ATR", block, 0.0),
                    active_leg="EFFECTIVE",
                    structure="NONE",
                ),
            )
        return

    if "CASE3 | COUNTER_ATTACK" in block:
        leg_direction = parse_log_direction(block, "ACTIVE LEG")
        counter_direction = parse_log_direction(block, "Counter")
        parsed_delta = parse_log_float(r"Delta:\s*([+-]?[0-9.]+)", block, 0.0)
        if counter_direction not in {"BUY", "SELL"}:
            counter_direction = "BUY" if parsed_delta > 0 else "SELL"
        if counter_direction not in {"BUY", "SELL"}:
            return
        leg = latest_case3_leg(leg_direction)
        if leg is None:
            return
        counter = {
            "time": ts,
            "price": price,
            "direction": counter_direction,
            "active_leg_direction": leg["direction"],
            "delta": parsed_delta,
            "dOI": parse_log_float(r"dOI\s*([+-]?[0-9.]+)\s*BTC", block, None),
            "dOI_pct": parse_log_float(r"dOI\s*[+-]?[0-9.]+\s*BTC\s*\|\s*([+-]?[0-9.]+)%", block, None),
            "delta_z": parse_log_float(r"Delta Z:\s*([+-]?[0-9.]+)", block, 0.0),
            "delta_share": parse_log_float(r"Delta/volume:\s*([0-9.]+)%", block, 0.0) / 100.0,
            "volume_ratio": parse_log_float(r"Volume ratio:\s*([0-9.]+)x", block, 0.0),
            "reward_atr": 0.0,
            "rewarded": False,
            "bars_after_reward": 0,
            "best_reward": 0.0,
            "leg_new_extreme": False,
        }
        same_side = [item for item in leg["counters"] if item["direction"] == counter_direction]
        counter["sequence_id"] = (
            same_side[-1]["sequence_id"]
            if same_side and ts - same_side[-1]["time"] <= QUALITY_SEQUENCE_GAP_SEC
            else f"CASE3-{counter_direction}-{int(ts)}"
        )
        counter["attack_index"] = len(same_side) + 1
        leg["counters"].append(counter)
        leg["counter_count"] += 1
        _case3_counters.append(counter)
        context = quality_context(
            abs_delta=abs(counter["delta"]),
            delta_z=counter["delta_z"],
            delta_share=counter["delta_share"],
            volume_ratio=counter["volume_ratio"],
            sequence="REPEATED" if counter["attack_index"] > 1 else "ISOLATED",
            active_leg="EFFECTIVE",
            structure="NONE",
        )
        counter["quality_context"] = context
        record_quality_event(
            "CASE3_COUNTER_ATTACK",
            ts,
            "LONG" if counter_direction == "BUY" else "SHORT",
            {"counter": counter},
            context,
        )
        return

    if "CASE3 | COUNTER_GOT_REWARD" in block:
        leg_direction = parse_log_direction(block, "Active leg")
        counter_direction = parse_log_direction(block, "Counter")
        leg = latest_case3_leg(leg_direction)
        if leg is None:
            return
        candidates = [
            item for item in leg["counters"]
            if item["direction"] == counter_direction and not item["rewarded"]
        ]
        if not candidates:
            return
        counter = candidates[-1]
        counter["rewarded"] = True
        counter["reward_atr"] = parse_log_float(r"Reward:\s*([0-9.]+)\s*ATR", block, 0.0)
        counter["best_reward"] = counter["reward_atr"]
        counter["reward_time"] = ts
        counter["efficiency_delta"] = (
            counter["reward_atr"] / abs(counter["delta"])
            if abs(counter["delta"]) else 0.0
        )
        previous_rewards = [
            item for item in leg["counters"]
            if item is not counter
            and item["direction"] == counter_direction
            and item.get("sequence_id") == counter.get("sequence_id")
            and item.get("rewarded")
        ]
        if previous_rewards:
            previous_eff = previous_rewards[-1].get("efficiency_delta", 0.0)
            counter["efficiency_change_vs_previous"] = counter["efficiency_delta"] - previous_eff
            counter["efficiency_trend"] = (
                "EFFICIENCY_IMPROVING"
                if counter["efficiency_change_vs_previous"] > 0
                else "EFFICIENCY_DETERIORATING"
                if counter["efficiency_change_vs_previous"] < 0
                else "EFFICIENCY_MIXED"
            )
        else:
            counter["efficiency_change_vs_previous"] = None
            counter["efficiency_trend"] = "EFFICIENCY_MIXED"
        context = quality_context(
            abs_delta=abs(counter["delta"]),
            delta_z=counter["delta_z"],
            delta_share=counter["delta_share"],
            volume_ratio=counter["volume_ratio"],
            extension_atr=counter["reward_atr"],
            retained_fraction=1.0,
            sequence="REPEATED" if counter["attack_index"] > 1 else "ISOLATED",
            active_leg="DECAYING",
            structure="RECLAIM",
        )
        record_quality_event(
            "CASE3_COUNTER_REWARD",
            ts,
            "LONG" if counter_direction == "BUY" else "SHORT",
            {"counter": counter},
            context,
        )
        return

    if (
        "CASE1_EXHAUSTION_REVERSAL_CONFIRMED" in block
        or "CASE1_FAST_FAILURE_CONFIRMED" in block
    ):
        direction = "LONG" if "LONG" in block else "SHORT" if "SHORT" in block else None
        if direction is None:
            return
        reclaim = parse_log_float(r"reclaim_atr=([+-]?[0-9.]+)", block, 0.0)
        oi_unwind = parse_log_float(r"oi_unwind_from_peak_pct=([+-]?[0-9.]+)", block, 0.0)
        counter_z = parse_log_float(r"counter_delta_z=([+-]?[0-9.]+)", block, 0.0)
        counter_share = parse_log_float(r"counter_delta_share=([+-]?[0-9.]+)", block, 0.0)
        context = quality_context(
            abs_delta=0.0,
            delta_z=counter_z,
            delta_share=counter_share,
            extension_atr=reclaim,
            retained_fraction=1.0,
            sequence="PERSISTENT",
            active_leg="STALLED",
            oi_state="DESTROYING" if oi_unwind > 0 else "APPROX_STABLE",
            structure="RECLAIM",
        )
        if not any(item["time"] == ts and item["direction"] == direction for item in _case1_exhaustions):
            _case1_exhaustions.append({"time": ts, "direction": direction})
            record_quality_event(
                "CASE1_FINAL_EVENT_OBSERVED",
                ts,
                direction,
                {
                    "module": "IMPULSE_EXHAUSTION",
                    "source": (
                        "CASE1_EXHAUSTION_REVERSAL_CONFIRMED"
                        if "CASE1_EXHAUSTION_REVERSAL_CONFIRMED" in block
                        else "CASE1_FAST_FAILURE_CONFIRMED"
                    ),
                    "reclaim_atr": reclaim,
                    "oi_unwind_from_peak_pct": oi_unwind,
                    "counter_delta_z": counter_z,
                    "counter_delta_share": counter_share,
                },
                context,
            )


def update_case3_quality(current):
    if not _case3_legs:
        return
    now = current["ts"] / 1000.0
    for leg in _case3_legs[-8:]:
        if now < leg["start_time"]:
            continue
        previous_extreme = leg["latest_extreme"]
        if leg["direction"] == "DOWN":
            leg["latest_extreme"] = min(leg["latest_extreme"], current["low"])
            new_extreme = leg["latest_extreme"] < previous_extreme
        else:
            leg["latest_extreme"] = max(leg["latest_extreme"], current["high"])
            new_extreme = leg["latest_extreme"] > previous_extreme
        if new_extreme:
            leg["last_progress_time"] = now
            leg["new_extreme_after_counter"] = True
            leg["state"] = "LEG_STILL_EFFECTIVE"
        elif now - leg["last_progress_time"] >= 60:
            leg["state"] = "LEG_NO_NEW_PROGRESS"
        for counter in leg["counters"]:
            if now <= counter["time"]:
                continue
            if new_extreme:
                counter["leg_new_extreme"] = True
            if counter["rewarded"]:
                new_quality_bar = counter.get("last_quality_bar_ts") != current["ts"]
                counter["last_quality_bar_ts"] = current["ts"]
                if new_quality_bar:
                    counter["bars_after_reward"] += 1
                if counter["direction"] == "BUY":
                    retained = current["close"] - counter["price"]
                else:
                    retained = counter["price"] - current["close"]
                retained = max(0.0, retained)
                counter["retained_reward"] = retained
                counter["retained_fraction"] = (
                    retained / (counter["best_reward"] * (_last_atr or 1.0))
                    if counter["best_reward"] > 0 else 0.0
                )
                if (
                    counter["bars_after_reward"] >= 1
                    and not counter.get("candidate_created")
                    and not counter["leg_new_extreme"]
                    and retained > 0
                    and leg["state"] == "LEG_NO_NEW_PROGRESS"
                ):
                    counter["candidate_created"] = True
                    context = quality_context(
                        abs_delta=abs(counter["delta"]),
                        delta_z=counter["delta_z"],
                        delta_share=counter["delta_share"],
                        volume_ratio=counter["volume_ratio"],
                        extension_atr=counter["best_reward"],
                        retained_fraction=counter["retained_fraction"],
                        sequence="REPEATED" if counter["attack_index"] > 1 else "ISOLATED",
                        active_leg="STALLED",
                        structure="RECLAIM",
                    )
                    create_test_entry(
                        "COUNTER_REBOUND",
                        "LONG" if counter["direction"] == "BUY" else "SHORT",
                        now,
                        current["close"],
                        _last_atr or 1.0,
                        context,
                        "CASE3_COUNTER_REWARD_PLUS_LEG_STALL",
                        entry_bar_ts=current["ts"] / 1000.0,
                        payload={
                            "active_leg_direction": leg["direction"],
                            "counter": counter,
                            "leg_state": leg["state"],
                        },
                    )


def update_case3_counter_outcomes():
    now = now_ts()
    for counter in list(_case3_counters):
        counter.setdefault("outcomes", {})
        side = "UP" if counter["direction"] == "BUY" else "DOWN"
        for horizon in (1, 3, 5, 15):
            key = f"{horizon}m"
            if key in counter["outcomes"] or now - counter["time"] < horizon * 60:
                continue
            try:
                rows = minute_window(counter["time"], counter["time"] + horizon * 60)
                result = path_outcome(side, counter["price"], _last_atr or 1.0, rows)
            except Exception:
                result = None
            if result is None:
                continue
            counter["outcomes"][key] = result
            if horizon in (1, 3, 5):
                counter[f"retained_reward_{horizon}m_atr"] = result["retained_atr"]
                counter[f"retained_fraction_{horizon}m"] = result["retained_fraction"]
            if horizon in (5, 15):
                counter[f"MFE_ATR_{horizon}m"] = result["mfe_atr"]
                counter[f"MAE_ATR_{horizon}m"] = result["mae_atr"]
            append_dataset(
                QUALITY_JSONL,
                {
                    "event": "CASE3_COUNTER_OUTCOME",
                    "counter": counter,
                    "horizon_minutes": horizon,
                },
            )


def update_entry_lifecycle(current):
    """Research-only HOLD/WARNING/EXIT state for test-entry anchors."""
    for entry in list(_test_entries):
        if entry.get("status") in {"EXIT_CANDIDATE", "INVALIDATED"}:
            continue
        bar_ts = current["ts"]
        lifecycle = entry.setdefault(
            "lifecycle",
            {
                "state": "ENTRY_CONFIRMED",
                "last_bar_ts": None,
                "best_progress": 0.0,
                "no_progress_bars": 0,
                "same_side_attacks": 0,
                "opposite_attacks": 0,
            },
        )
        if lifecycle["last_bar_ts"] == bar_ts or bar_ts * 0.001 <= entry["entry_time"]:
            continue
        lifecycle["last_bar_ts"] = bar_ts
        side = "UP" if entry["direction"] == "LONG" else "DOWN"
        if side == "UP":
            progress = max(0.0, current["high"] - entry["entry_price"])
            retained = max(0.0, current["close"] - entry["entry_price"])
            opposite_reward = max(0.0, entry["entry_price"] + lifecycle["best_progress"] - current["close"])
            same_side = current["delta"] > 0
        else:
            progress = max(0.0, entry["entry_price"] - current["low"])
            retained = max(0.0, entry["entry_price"] - current["close"])
            opposite_reward = max(0.0, current["close"] - (entry["entry_price"] - lifecycle["best_progress"]))
            same_side = current["delta"] < 0
        new_extreme = progress > lifecycle["best_progress"]
        if new_extreme:
            lifecycle["best_progress"] = progress
            lifecycle["no_progress_bars"] = 0
        else:
            lifecycle["no_progress_bars"] += 1
        if same_side:
            lifecycle["same_side_attacks"] += 1
        elif current["delta"] != 0:
            lifecycle["opposite_attacks"] += 1

        atr = entry.get("atr") or 1.0
        progress_atr = progress / atr
        retained_fraction = retained / progress if progress else 0.0
        opposite_delta = abs(current["delta"]) if not same_side else 0.0
        if (
            lifecycle["state"] in {"ENTRY_CONFIRMED", "TREND_HEALTHY"}
            and lifecycle["no_progress_bars"] >= 2
            and lifecycle["same_side_attacks"] >= 2
        ):
            lifecycle["state"] = "MOMENTUM_DECAY"
            entry["status"] = "WARNING"
            context = quality_context(
                abs_delta=abs(current["delta"]),
                delta_share=(abs(current["delta"]) / current["volume"] if current["volume"] else 0.0),
                extension_atr=progress_atr,
                retained_fraction=retained_fraction,
                sequence="REPEATED",
                efficiency_trend="DETERIORATING",
                active_leg="DECAYING",
                structure="ACCEPTANCE",
            )
            record_quality_event(
                "MOMENTUM_DECAY",
                bar_ts / 1000.0,
                entry["direction"],
                {
                    "event_id": entry["event_id"],
                    "state": lifecycle["state"],
                    "progress_atr": progress_atr,
                    "retained_fraction": retained_fraction,
                    "no_progress_bars": lifecycle["no_progress_bars"],
                },
                context,
            )
            log_event(
                "MOMENTUM_DECAY",
                (
                    f"[WATCH] {entry['direction']} momentum decay\n"
                    f"entry_id={entry['event_id']} | progress_atr={progress_atr:.3f} | "
                    f"retained_fraction={retained_fraction:.3f}\n"
                    "Same-side aggression is producing less incremental progress."
                ),
            )
        elif lifecycle["state"] == "ENTRY_CONFIRMED" and new_extreme and retained > 0:
            lifecycle["state"] = "TREND_HEALTHY"
            entry["status"] = "TREND_HEALTHY"
            record_quality_event(
                "TREND_HEALTHY",
                bar_ts / 1000.0,
                entry["direction"],
                {
                    "event_id": entry["event_id"],
                    "progress_atr": progress_atr,
                    "retained_fraction": retained_fraction,
                },
                quality_context(
                    abs_delta=abs(current["delta"]),
                    extension_atr=progress_atr,
                    retained_fraction=retained_fraction,
                    sequence="REPEATED",
                    efficiency_trend="IMPROVING",
                    active_leg="EFFECTIVE",
                    structure="ACCEPTANCE",
                ),
            )
            log_event(
                "TREND_HEALTHY",
                (
                    f"[HOLD] {entry['direction']} remains research-healthy\n"
                    f"entry_id={entry['event_id']} | progress_atr={progress_atr:.3f} | "
                    f"retained_fraction={retained_fraction:.3f}\n"
                    "No effective opposite reward has invalidated the hypothesis."
                ),
            )

        if (
            lifecycle["state"] == "MOMENTUM_DECAY"
            and opposite_delta >= BASELINE.PS_MIN_POST_COUNTER_DELTA
            and opposite_reward >= BASELINE.PS_MIN_FAILURE_REWARD_ATR * atr
            and not new_extreme
        ):
            lifecycle["state"] = "EXIT_CANDIDATE"
            entry["status"] = "EXIT_CANDIDATE"
            context = quality_context(
                abs_delta=opposite_delta,
                delta_share=(abs(current["delta"]) / current["volume"] if current["volume"] else 0.0),
                extension_atr=opposite_reward / atr,
                retained_fraction=0.0,
                sequence="REPEATED",
                efficiency_trend="DETERIORATING",
                active_leg="STALLED",
                structure="RECLAIM",
            )
            record_quality_event(
                "EXIT_CANDIDATE",
                bar_ts / 1000.0,
                "SHORT" if entry["direction"] == "LONG" else "LONG",
                {
                    "event_id": entry["event_id"],
                    "source_direction": entry["direction"],
                    "opposite_reward_atr": opposite_reward / atr,
                },
                context,
            )
            log_event(
                "TEST_ENTRY_INVALIDATED",
                (
                    f"[EXIT CANDIDATE] source={entry['event_id']}\n"
                    f"source_direction={entry['direction']} | opposite_reward_atr={opposite_reward / atr:.3f}\n"
                    "Momentum decay plus effective opposite reward. Research only."
                ),
            )
            play_test_entry_sound()



def watch_active_leg_log():
    """Attach CASE3 ACTIVE_LEG_DETECTED as a late outcome anchor."""
    offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    buffer = ""
    separator = "##########"
    while True:
        try:
            if not LOG_FILE.exists():
                time.sleep(POLL_SECONDS)
                continue
            with LOG_FILE.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                chunk = handle.read()
                offset = handle.tell()
            if chunk:
                parts = (buffer + chunk).split(separator)
                buffer = parts[-1]
                for part in parts[:-1]:
                    process_case3_log_block(part)
                    if "CASE3 | ACTIVE_LEG_DETECTED" not in part:
                        continue
                    ts = BASELINE.parse_stamp(part)
                    price = BASELINE.parse_price(part)
                    if price is None:
                        price = _last_price
                    if price is not None:
                        side = "UP" if "Направление: UP" in part else "DOWN" if "Направление: DOWN" in part else None
                        _attach_active_leg_for_side(ts, price, side)
            time.sleep(POLL_SECONDS)
        except Exception as exc:
            print("Ошибка pressure log watcher:", repr(exc), flush=True)
            time.sleep(POLL_SECONDS)


def run_engine_in_root(name, source):
    """Запускает сохранённый CASE-код с путями текущей рабочей версии."""
    namespace = {
        "__name__": name,
        "__file__": str(Path(__file__).resolve()),
        "directional_hypothesis_event": _directional_hypothesis_event,
    }
    exec(compile(source, f"<{name}>", "exec"), namespace, namespace)
    namespace["main"]()


def case1_source_for_runtime():
    """Build the CASE1 runtime source without changing archived thresholds.

    The archived engine remains the source of all existing CASE1 mathematics.
    This adapter changes only event routing and adds the episode-level
    reattack/quality bookkeeping required by the current research protocol.
    """
    source = BASELINE.CASE1_SOURCE
    source = source.replace("import math\n", "import math\nimport json\n", 1)
    source = source.replace(
        'EVENT_LOG = ROOT / "BTC_LRA_RESEARCH_LOG.txt"',
        f'EVENT_LOG = Path(r"{CASE1_DEBUG_LOG}")\n'
        f'FINAL_EVENT_LOG = Path(r"{LOG_FILE}")\n'
        f'CASE1_INTERNAL_JSONL = Path(r"{CASE1_INTERNAL_JSONL}")',
        1,
    )

    save_start = source.index("def save_event(text):")
    save_end = source.index("# ============================================================\n# CASE1 STATE", save_start)
    save_block = r'''def _json_default(value):
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return str(value)


def _append_internal(record):
    try:
        with CASE1_INTERNAL_JSONL.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
    except Exception as exc:
        print(f"CASE1 INTERNAL JSON ERROR: {type(exc).__name__}: {exc}", flush=True)


def save_event(text, event_type="CASE1_STAGE", payload=None):
    block = "##########" + chr(10) + "МОДУЛЬ=ИМПУЛЬС_И_ИСТОЩЕНИЕ" + chr(10) + text.rstrip() + chr(10) + "##########" + chr(10)
    try:
        with _shared_log_lock(LOCK_FILE):
            with EVENT_LOG.open("a", encoding="utf-8") as handle:
                handle.write(block)
    except Exception as exc:
        print(f"CASE1 DEBUG LOG ERROR: {type(exc).__name__}: {exc}", flush=True)
    _append_internal({
        "event": event_type,
        "recorded_at": now_local().isoformat(),
        "text": text,
        "payload": payload or {},
    })


def save_final_event(text, payload):
    _append_internal({
        "event": "CASE1_FINAL_EVENT",
        "recorded_at": now_local().isoformat(),
        "text": text,
        "payload": payload,
    })
    directional_hypothesis_event("CASE1", payload)


def console(text):
    # CASE1 intermediate and final text is routed by the coordinator.
    # The monitor console must not emit one line per internal stage.
    return None


def _play_sound(kind="info"):
    return None


'''
    source = source[:save_start] + save_block + source[save_end:]

    # Add episode memory to the baseline active object; it does not alter
    # shock/continuation thresholds.
    source = source.replace(
        '"leg_event_logged": False,\n',
        '"leg_event_logged": False,\n'
        '        "episode_id": f"CASE1-{tf}-{bar[\'ts\']}",\n'
        '        "history": [],\n'
        '        "stage_history": [],\n'
        '        "pending_opposite": False,\n'
        '        "first_opposite": None,\n'
        '        "oi_low": current_oi,\n'
        '        "oi_snapshots": [],\n',
        1,
    )

    eval_marker = '    A = a["shock_atr"]\n'
    eval_insert = r'''    if a.get("pending_opposite"):
        return _case1_pending_step(a, bar, m, current_oi)

    if not a.get("history") or a["history"][-1].get("ts") != bar["ts"]:
        a["history"].append(dict(bar))
        previous_oi = a["oi_snapshots"][-1]["oi"] if a["oi_snapshots"] else current_oi
        a["oi_snapshots"].append({"ts": bar["ts"], "oi": current_oi, "dOI": current_oi - previous_oi})
    a["oi_low"] = min(a.get("oi_low", current_oi), current_oi)

'''
    source = source.replace(eval_marker, eval_marker + eval_insert, 1)

    # Replace direct confirmations with an opposite-response pending stage.
    source = source.replace(
        '        return confirmed, fast_side, "FAST_FAILURE", common\n',
        '        return _case1_step(a, confirmed, fast_side, "FAST_FAILURE", common, bar, m, current_oi)\n',
        1,
    )
    source = source.replace(
        '    return confirmed, exhaustion_side, "EXHAUSTION_REVERSAL", common\n',
        '    return _case1_step(a, confirmed, exhaustion_side, "EXHAUSTION_REVERSAL", common, bar, m, current_oi)\n',
        1,
    )
    source = source.replace(
        '    # Branch A: original CASE1.\n',
        '    stage_name = "SHOCK_LEG_ACTIVE" if a.get("mode") == "SHOCK_LEG_ACTIVE" else "FAST_FAILURE_WATCH"\n'
        '    if a.get("bars_since_progress", 0) >= MIN_STALL_BARS:\n'
        '        stage_name = "STALLED_PROGRESS"\n'
        '    if common.get("oi_unwind_from_peak_pct", 0.0) > 0:\n'
        '        stage_name = "OI_DESTRUCTION_WATCH" if stage_name == "SHOCK_LEG_ACTIVE" else stage_name\n'
        '    if common.get("reclaim_atr", 0.0) >= MIN_RECLAIM_ATR:\n'
        '        stage_name = "RECLAIM_WATCH"\n'
        '    snapshot = _stage_payload(a, stage_name, bar, m, dict(common))\n'
        '    a.setdefault("stage_history", []).append(snapshot)\n'
        '    save_event(event_text(stage_name, a["tf"], a["direction"], bar, m, "Внутренняя snapshot-стадия."), payload=snapshot)\n\n'
        '    # Branch A: original CASE1.\n',
        1,
    )

    alert_start = source.index("def alert_early(")
    main_marker = source.index("# ============================================================\n# MAIN", alert_start)
    alert_block = r'''def _stage_payload(a, stage, bar, m, extra=None):
    payload = {
        "episode_id": a.get("episode_id"),
        "state": stage,
        "tf": a.get("tf"),
        "shock_side": "SELL" if a.get("direction") == "DOWN" else "BUY",
        "opposite_side": "BUY" if a.get("direction") == "DOWN" else "SELL",
        "event_timestamp": now_local().isoformat(),
        "event_bar_timestamp_ms": bar.get("ts"),
        "candle_progress": m.get("progress"),
        "partial_volume": bar.get("volume"),
        "partial_buy": bar.get("buy"),
        "partial_sell": bar.get("sell"),
        "partial_delta": bar.get("delta"),
        "current_price": bar.get("close"),
        "current_high_so_far": bar.get("high"),
        "current_low_so_far": bar.get("low"),
        "available_at_event": True,
    }
    if extra:
        payload.update(extra)
    return payload


def _quality_level(value):
    if value is None:
        return "NA"
    if value >= 4.0:
        return "ANOMALOUS"
    if value >= 2.0:
        return "ELEVATED"
    return "NORMAL"


def _efficiency_trend(previous, current):
    if previous is None or current is None:
        return "EFFICIENCY_STABLE"
    if previous and current < previous * 0.5:
        return "EFFICIENCY_COLLAPSED"
    if previous and current < previous * 0.85:
        return "EFFICIENCY_DECLINING"
    return "EFFICIENCY_STABLE"


def _volume_context(a, bar):
    rows = list(a.get("history", []))
    if not rows:
        rows = [bar]
    total_volume = sum(max(0.0, row.get("volume", 0.0)) for row in rows)
    duration = max(1.0, (bar["ts"] - a["shock_ts"]) / 60000.0)
    rates = {}
    for minutes in (1, 3, 5, 10):
        window = rows[-minutes:]
        rates[f"volume_rate_{minutes}m"] = sum(row.get("volume", 0.0) for row in window) / max(1.0, min(minutes, len(window)))
    baseline_rows = baseline.get(a["tf"], [])
    baseline_values = [row.get("volume", 0.0) for row in baseline_rows[-60:]]
    baseline20 = statistics.median(baseline_values[-20:]) if baseline_values[-20:] else None
    baseline60 = statistics.median(baseline_values) if baseline_values else None
    rel20 = rates["volume_rate_1m"] / baseline20 if baseline20 else None
    rel60 = rates["volume_rate_1m"] / baseline60 if baseline60 else None
    signed_progress = bar["close"] - a["shock_close"]
    if a["direction"] == "DOWN":
        signed_progress = -signed_progress
    retained_progress = max(0.0, signed_progress)
    result_per_100 = retained_progress / total_volume * 100.0 if total_volume else None
    previous_rows = rows[:-1]
    previous_volume = sum(row.get("volume", 0.0) for row in previous_rows)
    previous_price = previous_rows[0]["close"] if previous_rows else a["shock_close"]
    previous_result = bar["close"] - previous_price
    if a["direction"] == "DOWN":
        previous_result = -previous_result
    previous_eff = max(0.0, previous_result) / previous_volume * 100.0 if previous_volume else None
    current_eff = result_per_100
    peak_volume = max((row.get("volume", 0.0) for row in rows), default=0.0)
    peak_rel = peak_volume / baseline20 if baseline20 else None
    return {
        "leg_duration": duration,
        "leg_total_volume": total_volume,
        "leg_volume_per_min": total_volume / duration,
        **rates,
        "relative_volume_20m": rel20,
        "relative_volume_60m": rel60,
        "relative_effort_density": _quality_level(max(x for x in (rel20, rel60) if x is not None) if any(x is not None for x in (rel20, rel60)) else None),
        "directional_progress": max(0.0, signed_progress),
        "retained_progress": retained_progress,
        "result_per_100btc": current_eff,
        "retained_result_per_100btc": result_per_100,
        "efficiency_previous": previous_eff,
        "efficiency_current": current_eff,
        "efficiency_change": (current_eff - previous_eff) if current_eff is not None and previous_eff is not None else None,
        "late_efficiency_change": (current_eff - previous_eff) if current_eff is not None and previous_eff is not None else None,
        "efficiency_trend": _efficiency_trend(previous_eff, current_eff),
        "climax_volume": peak_volume,
        "climax_relative_volume": peak_rel,
        "climax_retention": retained_progress / max(1.0, peak_volume),
    }


def _oi_context(a, event_oi, label):
    values = [sample["oi"] for sample in a.get("oi_snapshots", [])]
    return {
        "OI_at_shock": a.get("shock_oi"),
        "OI_peak_after_shock": a.get("peak_oi"),
        "OI_low_after_shock": min(values) if values else a.get("oi_low"),
        "OI_at_failure": event_oi if label in {"FAILURE", "FINAL"} else None,
        "OI_at_opposite_response": event_oi if label == "OPPOSITE_RESPONSE" else None,
        "OI_at_final_event": event_oi if label == "FINAL" else None,
        "dOI_snapshots": a.get("oi_snapshots", []),
    }


def _case1_step(a, confirmed, side, branch, common, bar, m, current_oi):
    if not confirmed:
        return False, None, branch, common
    if not a.get("pending_opposite"):
        a["pending_opposite"] = True
        a["first_opposite"] = {
            "ts": bar["ts"],
            "price": bar["close"],
            "delta": bar["delta"],
            "volume": bar["volume"],
            "extension_atr": common.get("reclaim_atr"),
            "m": dict(m),
            "oi": current_oi,
        }
        payload = _stage_payload(a, "OPPOSITE_RESPONSE", bar, m, {
            "causal_path": "FAST_FAILURE" if branch == "FAST_FAILURE" else "EXHAUSTION_AFTER_ACTIVE_LEG",
            "reclaim_atr": common.get("reclaim_atr"),
            "extension_atr": common.get("extension_atr"),
        })
        a["stage_history"].append(payload)
        save_event(event_text("OPPOSITE_RESPONSE", a["tf"], side, bar, m, "Внутренняя стадия; финальное событие ещё не сформировано."), payload=payload)
    return False, None, "PENDING_OPPOSITE", common


def _case1_pending_step(a, bar, m, current_oi):
    if not a.get("history") or a["history"][-1].get("ts") != bar["ts"]:
        a["history"].append(dict(bar))
        previous_oi = a["oi_snapshots"][-1]["oi"] if a["oi_snapshots"] else current_oi
        a["oi_snapshots"].append({"ts": bar["ts"], "oi": current_oi, "dOI": current_oi - previous_oi})
    a["oi_low"] = min(a.get("oi_low", current_oi), current_oi)
    first = a.get("first_opposite") or {}
    if bar["ts"] <= first.get("ts", bar["ts"]):
        return False, None, "PENDING_OPPOSITE", {}
    old_side = "SELL" if a["direction"] == "DOWN" else "BUY"
    old_aggression = bar["delta"] < 0 if old_side == "SELL" else bar["delta"] > 0
    old_result = first.get("price", bar["close"]) - bar["close"] if old_side == "SELL" else bar["close"] - first.get("price", bar["close"])
    old_result = max(0.0, old_result)
    meaningful_reward = old_result >= MIN_NEW_EXTREME_ATR * a["shock_atr"]
    reattack_rejected = old_aggression and (
        (old_side == "SELL" and bar["close"] > first.get("price", bar["close"]))
        or (old_side == "BUY" and bar["close"] < first.get("price", bar["close"]))
    )
    reattack_type = (
        "REATTACK_WITH_REWARD" if old_aggression and meaningful_reward
        else "REATTACK_REJECTED" if reattack_rejected
        else "REATTACK_WITHOUT_REWARD" if old_aggression
        else "NO_REATTACK"
    )
    payload = _stage_payload(a, "OLD_SIDE_CHECK", bar, m, {
        "old_side": old_side,
        "oi": current_oi,
        "old_side_aggression": old_aggression,
        "old_side_reattack_type": reattack_type,
        "old_side_reattack_effort": abs(bar["delta"]),
        "old_side_reattack_result": old_result,
        "old_side_reattack_retention": old_result / max(1.0, a["shock_atr"]),
    })
    a["stage_history"].append(payload)
    save_event(event_text("OLD_SIDE_CHECK", a["tf"], old_side, bar, m, f"reattack={reattack_type} | result={old_result:.2f}"), payload=payload)
    a["pending_opposite"] = False
    if meaningful_reward:
        save_event(event_text("EPISODE_INVALIDATED", a["tf"], old_side, bar, m, "Старая сторона восстановила сопоставимый результат; CASE1 reversal не подтверждён."), payload=payload)
        return False, None, "INVALIDATED", payload
    return True, ("LONG" if a["direction"] == "DOWN" else "SHORT"), ("FAST_FAILURE" if a["mode"] == "FAST_FAILURE" else "EXHAUSTION_REVERSAL"), payload


def alert_early(tf, direction, bar, m):
    payload = _stage_payload(active[tf], "EARLY_SHOCK", bar, m) if active.get(tf) else {"tf": tf, "direction": direction}
    save_event(event_text("EARLY_SHOCK", tf, direction, bar, m, "Внутренняя стадия; пользовательское событие не создаётся."), payload=payload)


def alert_leg_activated(tf, a, bar, m, d):
    payload = _stage_payload(a, "SHOCK_LEG_ACTIVE", bar, m, {"continuation_atr": d.get("extension_atr")})
    a["active_leg_started"] = True
    a["active_leg_start_time"] = bar["ts"]
    a["stage_history"].append(payload)
    save_event(event_text("SHOCK_LEG_ACTIVE", tf, a["direction"], bar, m, "Внутренняя стадия; FAST_FAILURE закрыт, наблюдается active leg."), payload=payload)


def alert_confirmed(tf, side, bar, m, d, branch, a=None):
    a = a or {}
    causal_path = "FAST_FAILURE" if branch == "FAST_FAILURE" else "EXHAUSTION_AFTER_ACTIVE_LEG"
    direction = "LONG" if "LONG" in str(side) or a.get("direction") == "DOWN" else "SHORT"
    old_side = "SELL" if direction == "LONG" else "BUY"
    volume_context = _volume_context(a, bar)
    reattack_type = d.get("old_side_reattack_type", "NO_REATTACK")
    relative_values = [
        value for value in (
            volume_context.get("relative_volume_20m"),
            volume_context.get("relative_volume_60m"),
        ) if value is not None
    ]
    peak_relative = max(relative_values, default=0.0)
    quality_line = (
        f"КАЧЕСТВО ОБЪЁМА: Объём: {volume_context.get('relative_effort_density')} "
        f"({peak_relative:.1f}Ã— baseline); "
        f"Эффективность {old_side}: {volume_context.get('efficiency_trend')}; "
        f"Повторная {old_side} атака: {'effort есть, результата нет' if reattack_type == 'REATTACK_WITHOUT_REWARD' else reattack_type}."
    )
    final_label = "ИСТОЩЕНИЕ SELL-ДВИЖЕНИЯ → ВОЗМОЖНОЕ ДВИЖЕНИЕ ВВЕРХ" if direction == "LONG" else "ИСТОЩЕНИЕ BUY-ДВИЖЕНИЯ → ВОЗМОЖНОЕ ДВИЖЕНИЕ ВНИЗ"
    event_kind = "CASE1_FAST_FAILURE_CONFIRMED" if causal_path == "FAST_FAILURE" else "CASE1_EXHAUSTION_REVERSAL_CONFIRMED"
    text = (
        f"{event_kind}\n"
        f"{now_local():%H:%M} - {bar['close']:.1f} - {final_label}\n"
        f"Ветка: {causal_path}\n"
        f"Причина: противоположная сторона получила результат; старая сторона не восстановила прежнюю эффективность.\n"
        f"{quality_line}"
    )
    payload = {
        "episode_id": a.get("episode_id"),
        "module": "IMPULSE_EXHAUSTION",
        "causal_path": causal_path,
        "shock_side": old_side,
        "opposite_side": "BUY" if direction == "LONG" else "SELL",
        "shock_time": a.get("shock_ts"),
        "shock_price": a.get("shock_close"),
        "active_leg_started": bool(a.get("active_leg_started")),
        "active_leg_start_time": a.get("active_leg_start_time"),
        "max_continuation_ATR": d.get("extension_atr"),
        "failure_time": bar.get("ts"),
        "first_opposite_reward_time": (a.get("first_opposite") or {}).get("ts"),
        "old_side_reattack_type": reattack_type,
        "old_side_reattack_effort": d.get("old_side_reattack_effort"),
        "old_side_reattack_result": d.get("old_side_reattack_result"),
        "old_side_reattack_retention": d.get("old_side_reattack_retention"),
        "opposite_repeat_reward": reattack_type != "REATTACK_WITH_REWARD",
        "final_event_time": bar.get("ts"),
        "final_event_price": bar.get("close"),
        "volume_effort_result_context": volume_context,
        "OI_context": _oi_context(a, d.get("oi"), "FINAL"),
        "available_at_event": _stage_payload(a, "FINAL_CASE1", bar, m, d),
        "outcome_class": "UNRESOLVED",
        "MFE_MAE": {f"{h}m": None for h in (1, 3, 5, 15, 30, 60)},
        "stage_history": a.get("stage_history", []),
    }
    save_final_event(text, payload)


'''
    source = source[:alert_start] + alert_block + source[main_marker:]

    source = source.replace(
        '                                branch,\n                            )\n\n                            last_confirm_alert',
        '                                branch,\n                                a,\n                            )\n\n                            last_confirm_alert',
        1,
    )
    source = source.replace(
        '                        elif active_expired(a):\n                            active[tf] = None\n',
        '                        elif branch == "INVALIDATED":\n                            active[tf] = None\n\n                        elif active_expired(a):\n                            active[tf] = None\n',
        1,
    )
    # CASE1 has one coordinator startup message; suppress the archived
    # engine's verbose console header and warm-up chatter.
    source = source.replace(
        '    print(header, flush=True)\n',
        '    pass  # startup text is emitted by the coordinator\n',
        1,
    )
    source = source.replace(
        '            print(\n                f"Baseline ready: {tf}",\n                flush=True,\n            )\n',
        '            pass\n',
        1,
    )
    source = source.replace(
        '    print(\n        "OI history warming in RAM. "\n        "1m becomes available first; "\n        "higher TF dOI needs corresponding runtime.",\n        flush=True,\n    )\n',
        '    pass\n',
        1,
    )
    return source


COORDINATOR_TRIGGER_REGISTRY = RUNTIME_LOG_ROOT / "BTC_LRA_COORDINATOR_TRIGGER_REGISTRY.jsonl"
_coordinator_seen_trigger_keys = set()
_coordinator_runtime_start_epoch = None
_coordinator_suppress_human = False
_coordinator_emit_event_time = None


def _coordinator_trigger_key(event_type, direction, price, source_time, trigger_class):
    """Stable identity for one source event across scans and restarts."""
    return "|".join(
        (
            str(event_type or ""),
            str(trigger_class or "REVERSAL"),
            str(direction or ""),
            str(int(round(float(source_time or 0)))),
            f"{float(price or 0):.1f}",
        )
    )


def _load_coordinator_trigger_registry():
    _coordinator_seen_trigger_keys.clear()
    if not COORDINATOR_TRIGGER_REGISTRY.exists():
        return
    try:
        with COORDINATOR_TRIGGER_REGISTRY.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    key = record.get("identity")
                    if key:
                        _coordinator_seen_trigger_keys.add(key)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass


def _save_coordinator_trigger_identity(key, mode, source_time):
    try:
        with COORDINATOR_TRIGGER_REGISTRY.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "identity": key,
                "mode": mode,
                "event_time": source_time,
                "registered_at": time.time(),
            }, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"Ошибка реестра coordinator: {exc}", flush=True)


def coordinator_with_move_origin():
    """Run the archived coordinator with the current module listed at startup."""
    original_log = BASELINE.coordinator_log

    def startup_log(event_type, body):
        if event_type == "SYSTEM_READY":
            return None
            stamp = BASELINE.now_local().strftime("%Y-%m-%d %H:%M:%S")
            block = (
                "=" * 10 + "\n"
                ""
                ""
                + COORDINATOR_STARTUP_TEXT
                + "\n"
                + "=" * 10
                + "\n"
            )
            try:
                with BASELINE._lock(LOCK_FILE):
                    with LOG_FILE.open("a", encoding="utf-8") as handle:
                        handle.write(block)
            except Exception as exc:
                print(f"COORDINATOR STARTUP LOG ERROR: {exc}", flush=True)
            return None
        return original_log(event_type, body)

    BASELINE.coordinator_log = startup_log
    try:
        BASELINE.coordinator()
    finally:
        BASELINE.coordinator_log = original_log


def coordinator_with_move_origin():
    """Run the baseline coordinator and write one clean startup record."""
    global _coordinator_runtime_start_epoch, _coordinator_suppress_human, _coordinator_emit_event_time
    original_log = BASELINE.coordinator_log
    original_register_trigger = BASELINE.register_trigger
    original_update_trigger_results = BASELINE.update_trigger_results
    original_read_new_blocks = BASELINE.read_new_blocks
    original_play_sound = BASELINE.play_coordinator_sound
    startup_written = False
    _coordinator_runtime_start_epoch = time.time()
    _coordinator_suppress_human = False
    _load_coordinator_trigger_registry()

    def safe_read_new_blocks(state):
        # An append-only log can be rewritten/compacted externally.  Never
        # interpret a shorter file as permission to replay it from byte zero.
        try:
            size = LOG_FILE.stat().st_size
            if size < state.get("offset", 0):
                state["offset"] = size
                state["buffer"] = ""
                return
        except OSError:
            return
        return original_read_new_blocks(state)

    def guarded_register_trigger(direction, price, reason, source_time,
                                 trigger_class="REVERSAL", reference_price=None):
        global _coordinator_suppress_human, _coordinator_emit_event_time
        event_type = (
            "COUNTER_REBOUND_TRIGGER" if trigger_class == "REBOUND"
            else "PRESSURE_SWEEP_REVERSAL" if trigger_class == "PRESSURE_SWEEP"
            else "REVERSAL_TRIGGER"
        )
        identity = _coordinator_trigger_key(
            event_type, direction, price, source_time, trigger_class
        )
        if identity in _coordinator_seen_trigger_keys:
            return None
        _coordinator_seen_trigger_keys.add(identity)
        historical = float(source_time or 0) < _coordinator_runtime_start_epoch
        mode = "RESTORED/BACKFILLED" if historical else "LIVE_NEW_EVENT"
        _save_coordinator_trigger_identity(identity, mode, source_time)
        before = len(BASELINE.active_triggers)
        _coordinator_suppress_human = historical
        _coordinator_emit_event_time = source_time
        try:
            result = original_register_trigger(
                direction, price, reason, source_time,
                trigger_class=trigger_class,
                reference_price=reference_price,
            )
        finally:
            _coordinator_suppress_human = False
            _coordinator_emit_event_time = None
        if len(BASELINE.active_triggers) > before:
            trigger = BASELINE.active_triggers[-1]
            trigger._coordinator_identity = identity
            trigger._coordinator_mode = mode
            trigger._coordinator_suppress_human = historical
        return result

    def guarded_update_trigger_results():
        """Backfill restored outcomes silently; live outcomes remain visible."""
        global _coordinator_suppress_human
        all_triggers = list(BASELINE.active_triggers)
        restored = [t for t in all_triggers if getattr(t, "_coordinator_suppress_human", False)]
        live = [t for t in all_triggers if not getattr(t, "_coordinator_suppress_human", False)]
        BASELINE.active_triggers = live
        try:
            original_update_trigger_results()
        finally:
            live_remaining = list(BASELINE.active_triggers)
            BASELINE.active_triggers = restored
        _coordinator_suppress_human = True
        try:
            original_update_trigger_results()
        finally:
            restored_remaining = list(BASELINE.active_triggers)
            _coordinator_suppress_human = False
            BASELINE.active_triggers = live_remaining + restored_remaining

    def runtime_log(event_type, body):
        nonlocal startup_written
        if _coordinator_suppress_human:
            return None
        if event_type == "SYSTEM_READY":
            if not startup_written:
                startup_written = True
                stamp = BASELINE.now_local().strftime("%Y-%m-%d %H:%M:%S")
                block = (
                    f"[{stamp} Panama UTC-5] BTC-LRA | ЗАПУЩЕН И РАБОТАЕТ\n\n"
                    + COORDINATOR_STARTUP_TEXT
                    + "\n\n"
                )
                try:
                    with BASELINE._lock(LOCK_FILE):
                        with LOG_FILE.open("a", encoding="utf-8") as handle:
                            handle.write(block)
                except Exception as exc:
                    print(f"Ошибка записи старта в лог: {exc}", flush=True)
            return None
        event_time = _coordinator_emit_event_time
        stamp = (
            BASELINE.datetime.fromtimestamp(event_time, BASELINE.TZ).strftime("%Y-%m-%d %H:%M:%S")
            if event_time is not None
            else BASELINE.now_local().strftime("%Y-%m-%d %H:%M:%S")
        )
        if event_type == "PRESSURE_DETECTED_SHORT":
            short_stamp = (
                BASELINE.datetime.fromtimestamp(event_time, BASELINE.TZ).strftime("%d.%m.%y %H:%M:%S")
                if event_time is not None
                else BASELINE.now_local().strftime("%d.%m.%y %H:%M:%S")
            )
            block = (
                f"[{short_stamp}] {body.rstrip()}\n\n"
            )
        else:
            block = (
                "########## МОДУЛЬ=КООРДИНАТОР\n"
                f"[{stamp} Panama UTC-5] BTC-LRA | {event_type}\n"
                + body.rstrip()
                + "\n\n"
            )
        try:
            with BASELINE._lock(LOCK_FILE):
                with LOG_FILE.open("a", encoding="utf-8") as handle:
                    handle.write(block)
        except Exception:
            pass
        return None

    def quiet_sound(kind="info"):
        if not _coordinator_suppress_human:
            return original_play_sound(kind)

    BASELINE.coordinator_log = runtime_log
    BASELINE.register_trigger = guarded_register_trigger
    BASELINE.update_trigger_results = guarded_update_trigger_results
    BASELINE.read_new_blocks = safe_read_new_blocks
    BASELINE.play_coordinator_sound = quiet_sound
    try:
        BASELINE.coordinator()
    finally:
        BASELINE.coordinator_log = original_log
        BASELINE.register_trigger = original_register_trigger
        BASELINE.update_trigger_results = original_update_trigger_results
        BASELINE.read_new_blocks = original_read_new_blocks
        BASELINE.play_coordinator_sound = original_play_sound


def case3_source_for_runtime():
    """Route the archived CASE3 detector through an episode state machine.

    The archived detector supplies the unchanged leg/counter mathematics.  This
    adapter changes only lifecycle, persistence, and human-facing event routing.
    Intermediate observations go to the CASE3 debug files; one final episode
    classification is written to the shared human log.
    """
    source = BASELINE.CASE3_SOURCE
    source = source.replace("import statistics\n", "import statistics\nimport json\n", 1)
    source = source.replace(
        'LOG_FILE = os.path.join(\n    SCRIPT_DIR,\n    "BTC_LRA_RESEARCH_LOG.txt"\n)',
        f'LOG_FILE = r"{CASE3_DEBUG_LOG}"\n'
        f'CASE3_DEBUG_LOG = r"{CASE3_DEBUG_LOG}"\n'
        f'FINAL_EVENT_LOG = r"{LOG_FILE}"\n'
        f'CASE3_INTERNAL_JSONL = r"{CASE3_INTERNAL_JSONL}"',
        1,
    )

    event_start = source.index("def event(")
    event_end = source.index("# ============================================================\n# BINANCE", event_start)
    event_block = r'''def _json_default(value):
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return str(value)


def _write_case3_json(record):
    try:
        with open(CASE3_INTERNAL_JSONL, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
    except Exception as exc:
        print(f"CASE3 JSON ERROR: {type(exc).__name__}: {exc}", flush=True)


def _write_case3_debug(record):
    try:
        with open(CASE3_DEBUG_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
    except Exception as exc:
        print(f"CASE3 DEBUG ERROR: {type(exc).__name__}: {exc}", flush=True)


def event(event_type, title, body, sound="info", popup_enabled=True):
    # Compatibility hook for archived helpers.  It is deliberately internal.
    record = {
        "event": event_type,
        "title": title,
        "body": body,
        "timestamp": fmt_now(),
        "tf": INTERVAL,
        "internal": True,
    }
    _write_case3_debug(record)


def _case3_final_log(text, payload):
    _write_case3_json({"event": "CASE3_FINAL_EVENT", "payload": payload,
                       "text": text, "timestamp": fmt_now(), "tf": INTERVAL})
    directional_hypothesis_event("CASE3", payload)


# ============================================================
# CASE3 EPISODE STATE MACHINE
# ============================================================

case3_episode = None
case3_episode_counter = 0


def _bar_snapshot(bar):
    return {k: bar.get(k) for k in
            ("open_time", "close_time", "open", "high", "low", "close",
             "volume", "buy", "sell", "delta")}


def _effort_result(side, bar, reference_price):
    delta = float(bar.get("delta", 0.0) or 0.0)
    volume = float(bar.get("volume", 0.0) or 0.0)
    progress = ((bar["high"] - reference_price) if side == "BUY"
                else (reference_price - bar["low"]))
    progress = max(0.0, progress)
    close_progress = ((bar["close"] - reference_price) if side == "BUY"
                      else (reference_price - bar["close"]))
    retained = max(0.0, close_progress)
    return {
        "side": side, "volume": volume, "delta": delta,
        "price_extension": progress, "retained_progress": retained,
        "retention": retained / progress if progress > 0 else 0.0,
        "efficiency_delta": progress / abs(delta) if abs(delta) > 0 else 0.0,
        "efficiency_volume": progress / volume if volume > 0 else 0.0,
        "bar": _bar_snapshot(bar),
    }


def _episode_volume_context(ep, bars, reference_time):
    sample = [b for b in bars if b["open_time"] <= reference_time]
    leg = sample[-15:] if sample else []
    prior = sample[:-15]
    volumes = [float(b.get("volume", 0.0) or 0.0) for b in leg]
    prior20 = [float(b.get("volume", 0.0) or 0.0) for b in prior[-20:]]
    prior60 = [float(b.get("volume", 0.0) or 0.0) for b in prior[-60:]]
    baseline20 = statistics.median(prior20) if prior20 else 0.0
    baseline60 = statistics.median(prior60) if prior60 else 0.0
    displacement = ((leg[-1]["close"] - leg[0]["close"]) if len(leg) > 1 else 0.0)
    direction = ep["original_direction"]
    directional = abs(displacement)
    net_delta = sum(float(b.get("delta", 0.0) or 0.0) for b in leg)
    abs_delta = sum(abs(float(b.get("delta", 0.0) or 0.0)) for b in leg)
    return {
        "leg_duration_bars": len(leg),
        "leg_total_volume": sum(volumes),
        "leg_volume_per_min": sum(volumes) / len(volumes) if volumes else 0.0,
        "leg_displacement_usd": displacement,
        "leg_directional_progress": directional,
        "leg_net_delta": net_delta,
        "leg_abs_delta": abs_delta,
        "retained_progress": directional,
        "result_per_100btc": directional / sum(volumes) * 100.0 if sum(volumes) > 0 else 0.0,
        "retained_result_per_100btc": directional / sum(volumes) * 100.0 if sum(volumes) > 0 else 0.0,
        "relative_volume_20m": (max(volumes) / baseline20 if baseline20 > 0 and volumes else None),
        "relative_volume_60m": (max(volumes) / baseline60 if baseline60 > 0 and volumes else None),
        "climax_volume": max(volumes) if volumes else 0.0,
        "climax_relative_volume": (max(volumes) / baseline20 if baseline20 > 0 and volumes else None),
        "volume_quality": "ELEVATED" if baseline20 > 0 and volumes and max(volumes) >= 1.2 * baseline20 else "NORMAL",
        "efficiency_trend": "DESCRIPTIVE_ONLY",
        "leg_efficiency": (directional / sum(abs(b["close"] - leg[i-1]["close"])
                            for i, b in enumerate(leg) if i > 0)
                            if len(leg) > 1 and sum(abs(b["close"] - leg[i-1]["close"])
                            for i, b in enumerate(leg) if i > 0) > 0 else 0.0),
        "volume_rate_1m": volumes[-1] if volumes else 0.0,
        "volume_rate_3m": sum(volumes[-3:]) / min(3, len(volumes)) if volumes else 0.0,
        "volume_rate_5m": sum(volumes[-5:]) / min(5, len(volumes)) if volumes else 0.0,
        "volume_rate_10m": sum(volumes[-10:]) / min(10, len(volumes)) if volumes else 0.0,
        "direction": direction,
    }


def _case3_stage(name, ep, available_at, payload=None):
    record = {
        "event": name, "case3_episode_id": ep.get("case3_episode_id"),
        "parent_market_episode_id": ep.get("parent_market_episode_id"),
        "state": ep.get("state"), "timestamp": available_at,
        "payload": payload or {}, "available_at_event": True,
    }
    ep.setdefault("state_transitions", []).append(record)
    _write_case3_debug(record)


def _new_case3_episode(leg, bars, atr, oi=None, doi=None):
    global case3_episode_counter
    case3_episode_counter += 1
    sample = bars[-LEG_LOOKBACK:]
    ep = {
        "case3_episode_id": f"CASE3-{INTERVAL}-{case3_episode_counter:06d}",
        "parent_market_episode_id": None,
        "tf": INTERVAL,
        "original_direction": leg["direction"],
        "counter_direction": "SELL" if leg["direction"] == "UP" else "BUY",
        "state": "ACTIVE_LEG",
        "sample_start_time": sample[0]["open_time"] if sample else leg["start_time"],
        "sample_start_price": sample[0]["close"] if sample else leg["start_price"],
        "active_leg_detection_time": leg["detected_time"],
        "active_leg_detection_price": leg["current_price"],
        "original_leg_metrics": dict(leg),
        "atr_at_detection": atr,
        "first_counterattack": None,
        "first_counter_result": None,
        "old_side_response": None,
        "second_counter_result": None,
        "attacks": [], "state_transitions": [],
        "final_classification": None, "finalized": False,
        "OI_context": {
            "OI_at_detection": oi,
            "dOI_at_detection": doi,
            "OI_at_first_counterattack": None,
            "dOI_at_first_counterattack": None,
            "OI_at_first_counter_result": None,
            "dOI_at_first_counter_result": None,
            "OI_at_final_event": None,
            "dOI_at_final_event": None,
        },
    }
    ep["original_leg_metrics"].update(_episode_volume_context(ep, bars, leg["detected_time"]))
    _case3_stage("ACTIVE_LEG", ep, leg["detected_time"], {
        "sample_start_time": ep["sample_start_time"],
        "sample_start_price": ep["sample_start_price"],
        "detection_price": ep["active_leg_detection_price"],
        "displacement_at_detection_atr": leg.get("displacement_atr"),
        "efficiency": leg.get("efficiency"),
        "directional_share": leg.get("directional_share"),
    })
    return ep


def _finalize_case3(ep, classification, bar, reason):
    if ep is None or ep.get("finalized"):
        return
    ep["finalized"] = True
    ep["state"] = "FINAL"
    ep["final_classification"] = classification
    ep["final_event_time"] = bar["open_time"]
    ep["final_event_price"] = bar["close"]
    ep["final_reason"] = reason
    ep.setdefault("OI_context", {})["OI_at_final_event"] = ep.get("last_oi")
    ep.setdefault("OI_context", {})["dOI_at_final_event"] = ep.get("last_doi")
    history = ep.get("market_history", [])
    origin = ep.get("first_counter_result")
    if origin:
        side = ep.get("counter_direction")
        start_price = origin.get("start_price", bar["close"])
        outcome = {}
        for horizon in (1, 3, 5, 10, 15, 30, 60):
            limit = origin.get("result_time", bar["open_time"]) + horizon * 60 * 1000
            rows = [item for item in history if origin.get("result_time", 0) < item["open_time"] <= limit]
            if not rows:
                outcome[str(horizon) + "m"] = {"mfe": None, "mae": None}
                continue
            if side == "BUY":
                mfe = max(item["high"] - start_price for item in rows)
                mae = max(start_price - item["low"] for item in rows)
            else:
                mfe = max(start_price - item["low"] for item in rows)
                mae = max(item["high"] - start_price for item in rows)
            outcome[str(horizon) + "m"] = {"mfe": mfe, "mae": mae}
        ep["outcome_MFE_MAE_from_first_counter_result"] = outcome
    _case3_stage("FINAL_OUTCOME", ep, bar["open_time"], {
        "classification": classification, "reason": reason,
        "price": bar["close"],
    })
    original = ep["original_direction"]
    counter = ep["counter_direction"]
    if classification == "REBOUND_ONLY":
        title = f"ВСТРЕЧНАЯ АТАКА {counter} — ОТСКОК"
    elif classification == "MOVE_TERMINATION":
        title = f"ОСТАНОВКА {original}-ДВИЖЕНИЯ"
    else:
        title = f"ВОЗМОЖНАЯ СМЕНА {original} → {counter}"
    event_stamp = datetime.fromtimestamp(
        bar["open_time"] / 1000,
        tz=PANAMA_TZ,
    ).strftime("%d.%m.%y / %H.%M.%S")
    if classification == "REBOUND_ONLY":
        short_reason = f"{counter} получил результат, {original}-контроль восстановлен"
    elif classification == "MOVE_TERMINATION":
        short_reason = f"{counter} получил результат, {original}-контроль не восстановлен"
    else:
        short_reason = f"{counter} повторно получил результат, {original}-контроль не восстановлен"
    text = (
        f"[{event_stamp}] {title} | {bar['close']:.1f}\n"
        f"{short_reason}"
    )
    _case3_final_log(text, ep)


def _handle_old_side_response(ep, leg, bar, atr):
    first = ep.get("first_counter_result")
    if not first:
        return None
    old_side = "BUY" if leg["direction"] == "UP" else "SELL"
    result = _effort_result(old_side, bar, first["start_price"])
    result["timestamp"] = bar["open_time"]
    result["oi"] = ep.get("last_oi")
    result["doi"] = ep.get("last_doi")
    ep.setdefault("old_side_responses", []).append(result)
    # Existing trend-new-extreme criterion is retained; no new response threshold.
    restored = (bar["high"] > first["pre_trend_high"] + atr * TREND_NEW_EXTREME_ATR
                if leg["direction"] == "UP" else
                bar["low"] < first["pre_trend_low"] - atr * TREND_NEW_EXTREME_ATR)
    if restored and result["retained_progress"] > 0:
        response_type = "REATTACK_RESTORES_CONTROL"
    elif ((result["delta"] > 0 and old_side == "BUY") or
          (result["delta"] < 0 and old_side == "SELL")):
        response_type = (
            "REATTACK_WITH_REWARD"
            if result["retained_progress"] > 0 and result["retention"] >= 0.50
            else "REATTACK_WITH_POOR_RETENTION"
            if result["price_extension"] > 0
            else "REATTACK_WITHOUT_REWARD"
        )
    else:
        response_type = "NO_MEANINGFUL_REATTACK"
    result["classification"] = response_type
    ep["old_side_response"] = result
    _case3_stage("OLD_SIDE_RESPONSE", ep, bar["open_time"], result)
    return response_type


def _case3_update_episode(leg, counter, outcome, bar, atr, oi, doi, bars):
    global case3_episode
    ep = case3_episode
    if ep is None:
        return
    ep["last_oi"] = oi; ep["last_doi"] = doi
    if counter is not None:
        already_recorded = any(item.get("start_time") == counter.get("start_time")
                               for item in ep.get("attacks", []))
        attack = next((item for item in ep.get("attacks", [])
                       if item.get("start_time") == counter.get("start_time")), None)
        if not already_recorded:
            attack = dict(counter); attack["event_time"] = bar["open_time"]
            attack["attack_id"] = f"{ep['case3_episode_id']}-A{len(ep['attacks']) + 1:03d}"
            ep["attacks"].append(attack)
        if ep.get("first_counterattack") is None:
            ep["first_counterattack"] = attack
            ep.setdefault("OI_context", {})["OI_at_first_counterattack"] = ep.get("last_oi")
            ep.setdefault("OI_context", {})["dOI_at_first_counterattack"] = ep.get("last_doi")
            ep["state"] = "FIRST_COUNTERATTACK"
            _case3_stage("FIRST_COUNTERATTACK", ep, bar["open_time"], attack)
        elif not already_recorded:
            _case3_stage("REPEATED_COUNTERATTACK", ep, bar["open_time"], attack)
    if outcome == "FAILED" and counter is not None:
        legacy_count = register_failed_counter(leg, counter, bar)
        maybe_signal_trend(leg, legacy_count, bar)
        ep["legacy_two_failed_count"] = legacy_count
        ep["state"] = "FIRST_COUNTER_FAILED" if ep.get("first_counter_result") is None else ep["state"]
        _case3_stage("FIRST_COUNTER_FAILED", ep, bar["open_time"], dict(counter))
        return
    if outcome == "SUCCESS" and counter is not None:
        result = dict(counter); result["result_time"] = bar["open_time"]
        result["start_price"] = counter.get("start_price", bar["close"])
        result["pre_trend_high"] = counter.get("pre_trend_high", leg["high"])
        result["pre_trend_low"] = counter.get("pre_trend_low", leg["low"])
        result["retained_progress"] = (counter["best_reward"] * atr)
        if ep.get("first_counter_result") is None:
            ep["first_counter_result"] = result
            ep.setdefault("OI_context", {})["OI_at_first_counter_result"] = ep.get("last_oi")
            ep.setdefault("OI_context", {})["dOI_at_first_counter_result"] = ep.get("last_doi")
            ep["state"] = "WAIT_OLD_SIDE_RESPONSE"
            _case3_stage("FIRST_COUNTER_RESULT", ep, bar["open_time"], result)
        else:
            ep["second_counter_result"] = result
            ep["state"] = "FINAL"
            _case3_stage("SECOND_COUNTER_RESULT", ep, bar["open_time"], result)
            _finalize_case3(ep, "REVERSAL_CANDIDATE", bar,
                            "counter side получил первый и повторный результат; old-side control не восстановлен")
        return
    if ep.get("state") == "WAIT_OLD_SIDE_RESPONSE":
        response = _handle_old_side_response(ep, leg, bar, atr)
        if response == "REATTACK_RESTORES_CONTROL":
            _finalize_case3(ep, "REBOUND_ONLY", bar, "old-side reattack восстановила directional control")
        elif response in {"NO_MEANINGFUL_REATTACK", "REATTACK_WITHOUT_REWARD", "REATTACK_WITH_POOR_RETENTION"}:
            ep["state"] = "WAIT_SECOND_COUNTER_RESULT"
            _case3_stage("WAIT_SECOND_COUNTER_RESULT", ep, bar["open_time"], {"response": response})


def case3_source_for_runtime():
    raise RuntimeError("duplicate placeholder")


'''
    source = source[:event_start] + event_block + source[event_end:]
    source = source.replace('\n\ndef case3_source_for_runtime():\n    raise RuntimeError("duplicate placeholder")\n\n\n', '\n', 1)
    # Replace the archived main with the episode loop.
    main_start = source.find("def main():")
    # CASE3_SOURCE is executed by the coordinator and intentionally has no
    # standalone __main__ footer; replace its archived main through EOF.
    run_marker = len(source)
    main_block = r'''def main():
    global active_leg, active_counter, last_closed_bar_time, case3_episode
    while True:
        try:
            oi = get_current_oi()
            ts = int(time.time())
            add_oi_sample(ts, oi)
            doi, doi_pct = oi_change(ts, oi)
            bars = get_klines(limit=200)
            if not bars:
                time.sleep(POLL_SECONDS); continue
            closed = bars[:-1]
            if not closed:
                time.sleep(POLL_SECONDS); continue
            bar = closed[-1]
            if bar["open_time"] == last_closed_bar_time:
                time.sleep(POLL_SECONDS); continue
            last_closed_bar_time = bar["open_time"]
            atr = calculate_atr(closed)
            if atr is None:
                time.sleep(POLL_SECONDS); continue
            if active_leg is None:
                candidate = detect_leg(closed, atr)
                if candidate is not None:
                    active_leg = candidate
                    active_counter = None
                    case3_episode = _new_case3_episode(active_leg, closed, atr, oi, doi)
            if case3_episode is not None:
                case3_episode.setdefault("market_history", []).append(_bar_snapshot(bar))
            if active_leg is None:
                time.sleep(POLL_SECONDS); continue
            counter = None
            outcome = None
            if active_counter is not None:
                outcome = evaluate_counter(active_leg, active_counter, bar, atr)
                if outcome is not None:
                    counter = active_counter
                    active_counter = None
            update_leg(active_leg, bar)
            if case3_episode is not None and not case3_episode.get("finalized"):
                _case3_update_episode(active_leg, counter, outcome, bar, atr, oi, doi, closed)
                first = case3_episode.get("first_counter_result")
                if (case3_episode.get("state") == "WAIT_SECOND_COUNTER_RESULT" and first and
                        bar["open_time"] - first.get("result_time", bar["open_time"]) >= COUNTER_SEQUENCE_BARS * 60 * 1000):
                    _finalize_case3(case3_episode, "MOVE_TERMINATION", bar,
                                    "old-side control не восстановлен, повторный counter result не появился в causal window")
            if case3_episode is None or not case3_episode.get("finalized"):
                if active_counter is None:
                    counter_candidate = detect_counter_attack(active_leg, bar, closed[:-1], doi, doi_pct)
                    if counter_candidate is not None:
                        active_counter = counter_candidate
                        if case3_episode is not None:
                            case3_episode["state"] = "FIRST_COUNTERATTACK" if case3_episode.get("first_counterattack") is None else case3_episode.get("state")
                            _case3_update_episode(active_leg, counter_candidate, None, bar, atr, oi, doi, closed)
            if case3_episode is not None and case3_episode.get("finalized"):
                active_leg = None; active_counter = None; case3_episode = None
        except KeyboardInterrupt:
            print("Stopped by user.", flush=True); break
        except requests.RequestException as exc:
            print("NETWORK ERROR:", exc, flush=True)
        except Exception as exc:
            print("ERROR:", repr(exc), flush=True)
        time.sleep(POLL_SECONDS)


'''
    source = source[:main_start] + main_block + source[run_marker:]
    return source


def main():
    print(COORDINATOR_STARTUP_TEXT, flush=True)

    threads = [
        threading.Thread(
            target=run_engine_in_root,
            args=("LRA_CASE1_ENGINE_001", case1_source_for_runtime()),
            daemon=True,
            name="IMPULSE_EXHAUSTION_001",
        ),
        threading.Thread(
            target=run_engine_in_root,
            args=("LRA_CASE3_ENGINE_001", case3_source_for_runtime()),
            daemon=True,
            name="COUNTER_ATTACK_001",
        ),
        threading.Thread(
            target=coordinator_with_move_origin,
            daemon=True,
            name="COORDINATOR_001_BASELINE",
        ),
        threading.Thread(
            target=pressure_research_engine,
            daemon=True,
            name="PRESSURE_RESEARCH_001",
        ),
        threading.Thread(
            target=watch_active_leg_log,
            daemon=True,
            name="PRESSURE_LOG_WATCHER_001",
        ),
        threading.Thread(
            target=position_state_recorder,
            daemon=True,
            name="POSITION_STATE_RECORDER_001",
        ),
    ]

    for thread in threads:
        thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)


if __name__ == "__main__":
    main()

