# BTC-LRA: фактическая карта логики и forensic P0002

Дата карты: 2026-09-28  
Источник: текущий `btc-lra-001.py` и доступные JSONL/log files.  
Изменения runtime-кода: нет.

## 1. Общий поток данных

```text
1m OHLCV / delta / volume / OI
        |
        +--> PRESSURE
        |      --> pressure episode / watch / continuation / failure
        |
        +--> CASE1: IMPULSE_EXHAUSTION
        |      --> CASE1 internal stages / final reversal evidence
        |
        +--> CASE3: COUNTERATTACK
        |      --> active leg / counter attack / counter reward /
        |          old-side response / final classification
        |
        +--> EARLY_REVERSAL_TEST
        |      --> local effort/result flips
        |
        +--> MOVE_ORIGIN_TEST
        |      --> local effort/result candidate / confirmation / outcomes
        |
        +--> ACTIVE_MOVE_HEALTH
        |      --> health, decay, opposite response, exit anchors
        |
        +--> EFFORT_RESULT_BATTLE
               --> ERT observations grouped into battle episodes

CASE1/CASE3/MOVE_ORIGIN source evidence
        --> DIRECTIONAL_HYPOTHESIS
        --> state persistence / history / optional short human event

PRESSURE, CASE1, CASE3, ACTIVE_MOVE and TEST_ENTRY
        --> coordinator / human log / research JSONL
```

Главное фактическое разделение: JSONL являются источниками research data; human log — presentation-копия и не содержит всех внутренних событий.

## 2. Файлы данных

| Файл | Фактическое назначение |
|---|---|
| `BTC_LRA_PRESSURE_EPISODES.jsonl` | Pressure episode records, attacks, retention, outcomes, continuation/failure |
| `BTC_LRA_EVENT_QUALITY.jsonl` | Нормализованные quality events от pressure, CASE1, CASE3, ACTIVE_MOVE и других слоёв |
| `BTC_LRA_ACTIVE_MOVE_HEALTH.jsonl` | Active-move health snapshots, attacks, bars, exit anchors |
| `BTC_LRA_TEST_ENTRIES.jsonl` | TEST_ENTRY candidates, exit anchors и forward outcomes |
| `BTC_LRA_POSITION_STATE.jsonl` | Samples внешнего position/account state |
| `BTC_LRA_IMPULSE_EXHAUSTION_CASE1.jsonl` | CASE1 internal/final data |
| `BTC_LRA_COUNTERATTACK_CASE3.jsonl` | CASE3 internal/final data |
| `BTC_LRA_EARLY_REVERSAL_TEST.jsonl` | ERT signals и post-signal outcomes |
| `BTC_LRA_MOVE_ORIGIN_TEST.jsonl` | MOVE_ORIGIN candidates, confirmations, accelerations и outcomes |
| `BTC_LRA_EFFORT_RESULT_BATTLE.jsonl` | Battle episode/state snapshots |
| `BTC_LRA_EFFORT_RESULT_BATTLE_DEBUG.log` | Подробные battle/debug records |
| `BTC_LRA_DIRECTIONAL_HYPOTHESIS.jsonl` | Coordinator-level hypothesis transitions |
| `BTC_LRA_DIRECTIONAL_HYPOTHESIS_STATE.json` | Persistent current directional state |
| `BTC_LRA_RESEARCH_LOG.txt` | Human-readable output; не полный telemetry source |

## 3. Модули

### 3.1 CASE1 / IMPULSE_EXHAUSTION

CASE1 запускается как runtime source из baseline `CASE1_SOURCE` (`case1_source_for_runtime`). Сохраняет промежуточные stages в `BTC_LRA_IMPULSE_EXHAUSTION_CASE1.jsonl` и debug log.

Фактически наблюдаемые CASE1 events включают:

- `CASE1_STAGE` — внутренние стадии episode;
- `EPISODE_INVALIDATED` — старое движение восстановило сопоставимый результат;
- `CASE1_FAST_FAILURE_CONFIRMED` — fast-failure ветка завершила causal sequence;
- `CASE1_EXHAUSTION_REVERSAL_CONFIRMED` — exhaustion/reversal ветка завершила causal sequence;
- `CASE1_FINAL_EVENT_OBSERVED` — адаптер quality/coordinator увидел финальное событие.

CASE1 использует impulse, exhaustion, delta, price progress, retention и OI context. CASE1 final event нормализуется адаптером `_directional_ingest_case1()` как `CASE1_FINAL_REVERSAL`. При `DIRECTIONAL_HYPOTHESIS=INACTIVE` CASE1 является разрешённым источником установки provisional control. CASE1 при challenged state может восстановить старый control, если evidence side совпадает.

Семантика: это evidence reversal/exhaustion, а не гарантированный entry или order.

### 3.2 CASE3 / COUNTERATTACK

CASE3 создаёт episode с:

```text
ACTIVE_LEG
→ FIRST_COUNTERATTACK
→ FIRST_COUNTER_FAILED или FIRST_COUNTER_RESULT
→ OLD_SIDE_RESPONSE
→ WAIT_SECOND_COUNTER_RESULT
→ SECOND_COUNTER_RESULT
→ FINAL
```

Фактические internal/stage records пишутся в `BTC_LRA_COUNTERATTACK_CASE3.jsonl` и debug log. Финальный event записывается как `CASE3_FINAL_EVENT` и направляется в directional adapter.

Финальные classifications:

| Classification | Семантика |
|---|---|
| `REBOUND_ONLY` | counter side получил результат, но old-side восстановил control |
| `MOVE_TERMINATION` | old-side движение перестало восстанавливаться после counter result |
| `REVERSAL_CANDIDATE` | counter side получил первый и повторный result, old-side control не восстановлен |

Нормализация использует `original_direction` и `counter_direction`, а не generic `direction`:

```text
original_side = BUY, counter_side = SELL  для original_direction=UP
original_side = SELL, counter_side = BUY  для original_direction=DOWN
```

`MOVE_TERMINATION` и `REVERSAL_CANDIDATE` не равны автоматически новому control. CASE3 semantics сначала интерпретируются adapter-ом, затем применяются directional rules.

### 3.3 PRESSURE

Основной engine: `pressure_research_engine()`. Он работает с закрытыми 1m bars, delta, volume, pressure window, reference price, ATR, OI/dOI и последовательностью attempts.

Фактический поток:

```text
PRESSURE_DETECTED
→ PRESSURE_RETENTION
→ PRESSURE_BREAKOUT_WATCH
→ PRESSURE_CONTINUATION_CANDIDATE
→ PRESSURE_CONTINUATION
```

или:

```text
PRESSURE_DETECTED
→ PRESSURE_BREAKOUT_WATCH
→ PRESSURE_FAILURE
```

`PRESSURE_FAILURE` означает failure pressure continuation/retention. Это не CASE1 reversal, не TEST_EXIT_CANDIDATE и не automatic directional control change.

Pressure records сохраняются в `BTC_LRA_PRESSURE_EPISODES.jsonl`; quality copies — в `BTC_LRA_EVENT_QUALITY.jsonl`; часть событий human-visible через coordinator/log routing. Retention/outcome records являются research telemetry и не равны live exit event.

### 3.4 ACTIVE_MOVE_HEALTH

Есть два связанных в коде слоя:

1. `register_active_move_health()` / `update_active_move_health()` — active move health stream;
2. `update_entry_lifecycle()` — lifecycle кандидата TEST_ENTRY.

Health stream пишет `ACTIVE_MOVE_HEALTH_OPENED`, `ACTIVE_MOVE_SHOCK_UPDATE`, `ACTIVE_MOVE_SUPERSEDED`, `ACTIVE_MOVE_SAME_SIDE_ATTACK`, `ACTIVE_MOVE_OPPOSITE_ATTACK`, `ACTIVE_MOVE_EFFICIENCY_DECAY`, `ACTIVE_MOVE_RESPONSE_WEAKENED`, `ACTIVE_MOVE_OPPOSITE_RESPONSE`, `MOVE_TERMINATION_CONFIRMED`, `ACTIVE_MOVE_HEALTH_BAR`.

Lifecycle TEST_ENTRY использует состояния:

```text
ENTRY_CONFIRMED
→ TREND_HEALTHY
→ MOMENTUM_DECAY
→ EXIT_CANDIDATE
```

А также может писать `TEST_EXIT_CANDIDATE`/`TEST_EXIT_CONFIRMED` через `create_exit_anchor()` для health state `OPPOSITE_RESPONSE`/`MOVE_TERMINATION_CONFIRMED`.

`MOMENTUM_DECAY` — warning об ухудшении incremental progress. Для lifecycle exit candidate требуется одновременно:

```text
state == MOMENTUM_DECAY
opposite_delta >= PS_MIN_POST_COUNTER_DELTA
opposite_reward >= PS_MIN_FAILURE_REWARD_ATR * ATR
not new_extreme
```

Это exit evidence, но не гарантированный position exit.

### 3.5 EARLY_REVERSAL_TEST

Функции `_early_reversal_*` отслеживают локальные effort/result flips и causal branches:

- `DETERIORATION_FLIP`;
- `DIRECT_COUNTERATTACK_FLIP`.

Records сохраняются в `BTC_LRA_EARLY_REVERSAL_TEST.jsonl`, включая source event data и outcomes. Detector не решает directional control. Human direct compact output был отделён от detector data; source event остаётся research observation.

Семантика: первая/локальная передача price reward между сторонами. Это не подтверждённый reversal, не exit и не entry само по себе.

### 3.6 MOVE_ORIGIN_TEST

`update_move_origin_test()` работает по закрытым 1m bars и отдельному history buffer. Использует effort/result, price progress, retained reward, efficiency, delta, volume, ATR и OI context.

Основные records:

- `TEST_ENTRY_CANDIDATE` с `setup_family=MOVE_ORIGIN_TEST`;
- `MOVE_ORIGIN_CONFIRMATION`;
- `MOVE_ORIGIN_ACCELERATION`;
- `MOVE_ORIGIN_TEST_OUTCOME`.

Кандидаты сохраняются в `BTC_LRA_MOVE_ORIGIN_TEST.jsonl`. `_move_origin_emit_line()` не является detector: его задача — presentation. Событие передаётся через directional coordinator adapter, если нужно coordinator-level human output.

Семантика: local effort→result candidate/reclaim. Это не automatic entry и не доказательство общего control.

### 3.7 EFFORT_RESULT_BATTLE

Battle получает существующие ERT signals через `_battle_on_ert()`, не пересчитывает ERT. Сначала создаётся `BATTLE_SEED`, после противоположной атаки — `BATTLE_ACTIVE`. Timeout технически равен `15*60` секунд.

Battle сохраняет attack-level effort/result/retention metrics по BUY и SELL, reattack sequence, cumulative snapshots и state transitions в `BTC_LRA_EFFORT_RESULT_BATTLE.jsonl`/debug.

Battle не подключён к `DIRECTIONAL_HYPOTHESIS` как SET source и не должен сам создавать human entry/control event. Его advantage labels — research classification, не trading signal.

### 3.8 TEST_ENTRY / POSITION_STATE

`create_test_entry()` сохраняет research candidate в `BTC_LRA_TEST_ENTRIES.jsonl`, quality context и virtual/forward outcomes. Orders не создаются.

`create_exit_anchor()` сохраняет `TEST_EXIT_CANDIDATE` или `TEST_EXIT_CONFIRMED` в `BTC_LRA_TEST_ENTRIES.jsonl` и `BTC_LRA_ACTIVE_MOVE_HEALTH.jsonl`; human output идёт через `log_event()` как research-only exit notification.

`position_state_recorder()` отдельно пишет `POSITION_STATE_SAMPLE` в `BTC_LRA_POSITION_STATE.jsonl`. Это context, а не directional decision.

### 3.9 DIRECTIONAL_HYPOTHESIS

Persistent state:

```text
state: INACTIVE / ACTIVE / CHALLENGED
control: NONE / BUY / SELL
challenger
source/source_event_id/source_time/source_price
evidence
previous_broken_control
processed_source_ids
last_event_time
```

Источники и роли:

| Source | SET | SUPPORT | CHALLENGE | BREAK |
|---|---:|---:|---:|---:|
| CASE1 final | да | да | по стороне | restoration/break semantics |
| CASE3 `REVERSAL_CANDIDATE` | да при INACTIVE | при same candidate side | да | только из CHALLENGED causal sequence |
| CASE3 `REBOUND_ONLY` | нет | да | нет | нет |
| CASE3 `MOVE_TERMINATION` | нет | нет | matching `terminated_side` | только из CHALLENGED |
| MOVE_ORIGIN/MOT | нет | same-side support | opposite challenge | нет |
| BATTLE | нет | нет | observe-only | нет |

Stable identity использует source module/event fields; для CASE3 добавляются classification, event time, timeframe, sides и price. Duplicate source event не меняет state. Late event записывается как historical evidence и не переписывает realtime state.

История — `BTC_LRA_DIRECTIONAL_HYPOTHESIS.jsonl`; debug — `BTC_LRA_DIRECTIONAL_HYPOTHESIS_DEBUG.log`; state — `BTC_LRA_DIRECTIONAL_HYPOTHESIS_STATE.json`.

### 3.10 COORDINATOR

Coordinator управляет baseline log stream, trigger registry, historical/live routing и подключением module adapters. Registry: `BTC_LRA_COORDINATOR_TRIGGER_REGISTRY.jsonl`.

Coordinator не пересчитывает detector semantics. Он:

- принимает source events;
- применяет idempotency/historical routing;
- решает, какой event human-visible;
- пишет human block/short line через общий output path;
- передаёт CASE1/CASE3/MOT в directional hypothesis.

`SYSTEM_READY` — service message, не market event. Pressure и research modules могут также писать machine records независимо от human presentation.

## 4. Cross-module semantic map

| Event | Source | Что означает фактически | Entry? | Exit? | Reversal/control? | Confirmation | Downstream | Human |
|---|---|---|---|---|---|---|---|---|
| `PRESSURE_DETECTED` | PRESSURE | обнаружено последовательное давление | нет | нет | нет | retention/outcome | pressure episode, quality | да, compact observation |
| `PRESSURE_BREAKOUT_WATCH` | PRESSURE | финальная попытка продолжения pressure | нет | косвенное warning | нет | continuation или failure | pressure engine | зависит от routing |
| `PRESSURE_FAILURE` | PRESSURE | pressure attempt не удержал result | нет | exit evidence candidate | нет | не является control break | pressure episode, quality | да/может быть compact |
| `PRESSURE_CONTINUATION` | PRESSURE | давление продолжилось | нет | нет | support current pressure | outcomes | pressure episode | presentation-dependent |
| `MOMENTUM_DECAY` | ACTIVE_MOVE | same-side effort даёт меньше incremental progress | нет | warning | нет | opposite response required | lifecycle | да |
| `ACTIVE_MOVE_OPPOSITE_RESPONSE` | ACTIVE_MOVE | opposite side получила response | нет | evidence | challenge evidence | termination/recovery | active health | обычно internal/quality |
| `TEST_EXIT_CANDIDATE` | ACTIVE_MOVE | decay + достаточный opposite reward + no new extreme | нет | да, исследовательский | нет | confirmed termination optional | TEST_ENTRY/active health | да |
| `TEST_EXIT_CONFIRMED` | ACTIVE_MOVE | opposite response не восстановлена, termination confirmed | нет | да, stronger research exit | нет | TEST_ENTRY/active health | да |
| `CASE1_FAST_FAILURE_CONFIRMED` | CASE1 | causal fast-failure sequence завершена | нет автоматически | possible exit evidence | possible reversal evidence | CASE1 final semantics | directional | source/final routing |
| `CASE1_EXHAUSTION_REVERSAL_CONFIRMED` | CASE1 | exhaustion/reversal sequence завершена | нет автоматически | possible exit | possible reversal | CASE1 final | directional | source/final routing |
| `CASE3 REBOUND_ONLY` | CASE3 | counter result был, old side восстановил control | нет | нет | support/restore old side | existing causal sequence | directional | final event |
| `CASE3 MOVE_TERMINATION` | CASE3 | old side control не восстановлен | нет | termination evidence | challenge/break only with state | challenger/follow-up | directional | final event |
| `CASE3 REVERSAL_CANDIDATE` | CASE3 | counter side получил первый и повторный result | not automatic | possible exit old side | candidate reversal evidence | coordinator state | directional | final event |
| `ERT DETERIORATION_FLIP` | ERT | local reward transfer after deterioration | no | no | local observation | none | battle/research | hidden or compact source |
| `ERT DIRECT_COUNTERATTACK_FLIP` | ERT | direct opposite local response | no | no | local observation | none | battle/research | hidden or compact source |
| `MOVE_ORIGIN TEST_ENTRY_CANDIDATE` | MOT | local effort→result sequence | candidate only | no | local evidence | confirmation/outcome | directional | coordinator-controlled |
| `MOVE_ORIGIN_CONFIRMATION` | MOT | later structural confirmation of MOT sequence | candidate evidence | no direct exit | support/reclaim | own sequence | MOT dataset | source presentation |
| `BATTLE_SEED/ACTIVE` | BATTLE | ERT events grouped into one struggle | no | no | comparative research | advantage evidence | battle dataset | no |
| `DIRECTIONAL CHALLENGED` | Coordinator | active control has opposite evidence | no | no direct exit | challenge only | restore/break evidence | hypothesis state | no |
| `CONTROL_BROKEN` | Coordinator | challenged control failed | no | possible exit context | control lost, not new control | later independent SET | hypothesis state | yes |
| `OLD_SIDE_RESTORED` | Coordinator | challenged old side produced explicit restoration evidence | no | no | control restored | source semantics | hypothesis state | normally no |

Эти события не эквивалентны. Например, `PRESSURE_FAILURE` не создаёт `TEST_EXIT_CANDIDATE`, `MOMENTUM_DECAY` не означает `CONTROL_BROKEN`, а `CASE3_REVERSAL_CANDIDATE` не означает автоматический новый control.

## 5. Уникальный PRESSURE_FAILURE P0002

В двух JSONL сохранены две копии одного episode event:

```text
27.09.26 20:21:28 | PRESSURE_FAILURE | SHORT | P0002
```

Цепочка:

```text
20:17:01 PRESSURE_DETECTED
20:18:23 PRESSURE_BREAKOUT_WATCH
20:21:28 PRESSURE_FAILURE
```

Ключевые поля:

```text
pressure_reference_price = 83850.0
attempt_price             = 83869.4
latest_extreme            = 83836.2
pressure_cumulative_delta = -552.7 BTC
pressure_total_volume     = 4178.0 BTC
retained_reward           = -126.0 USD
retained_fraction         = -9.130
pullback_after_extension  = 139.8 USD
incremental_extension     = 0.0 USD
```

В `PRESSURE_EPISODES` у P0002 сохранены minute attacks. Важный нюанс: pressure window содержит bars до момента `PRESSURE_DETECTED`; это не future leakage, а окно, которое было доступно engine в момент detection.

## 6. P0002: available-at-event timeline

Ниже используются только поля, записанные до или на соответствующий timestamp. Поля `PRESSURE_OUTCOME`, MFE/MAE и поздние retention/outcome records не используются для определения раннего события.

| Time | Available evidence | Semantic stage | Could be used live? |
|---|---|---|---|
| 20:10 | Pressure window still shows DOWN pressure; current attack has delta около -0.8, directional progress около 37.1, retained close 0 | SELL pressure already present, but no explicit failure | Только как observation |
| 20:11 | delta около -8.7, progress около 29.3, retained 0 | weak continuation/low incremental result | Да, как deterioration observation, не как existing exit event |
| 20:12 | delta +23.8, progress около 27.3, retained 0 | opposite-flow evidence inside pressure window | Да, evidence only |
| 20:13 | delta +13.1, progress около 13.7, retained 0 | SELL result still not retained; BUY-flow response appears | Да, warning/evidence |
| 20:14 | delta +2.2, progress около 38.8, retained 0 | no retained close reward | Да, observation |
| 20:15 | delta -41.4, progress около 39.6, retained 0 | SELL aggression returns, but close retention remains 0 | Да, continuation quality concern |
| 20:16 | delta -65.5, progress около 64.0, retained 0 | strong SELL effort without retained result in stored attack snapshot | Да, earliest strong pre-failure deterioration evidence in this record |
| 20:17:01 | `PRESSURE_DETECTED`, cumulative delta -552.7, total volume 4178.0 | pressure episode recognized | Да, but not exit event |
| 20:18:06 | P0002 retention record, no retained reward | pressure has not retained the attempt | Да, warning evidence |
| 20:18:23 | `PRESSURE_BREAKOUT_WATCH`, attempt price 83869.4 | final continuation attempt is being watched | Да, potential exit-watch context; not yet failure |
| 20:19:04 | new P0003 `PRESSURE_DETECTED` also appears | overlapping/new pressure process | Да, but not proof of P0002 failure |
| 20:19:29 | P0002 retention remains 0 | continuation has not recovered retention | Да, strongest pre-failure retention evidence |
| 20:20:05 | P0002 retention remains 0; P0003 also has retention 0 | no retained pressure result | Да, exit-warning evidence, not final failure |
| 20:21:28 | `PRESSURE_FAILURE`; retained -126.0, fraction -9.130, pullback 139.8, incremental extension 0 | failure confirmed by existing pressure engine | Да, final existing failure event |

### P0002: earliest possible research interpretation

Самое раннее фактическое evidence deterioration — около **20:16**, когда сильный отрицательный delta продолжал прикладываться, но в сохранённом pressure attack snapshot `retained_reward=0`.

Самый ранний существующий event, который можно было использовать как `POTENTIAL EXIT SHORT` без нового detector и без будущих данных, — **20:18:23 `PRESSURE_BREAKOUT_WATCH`**, но только как watch/warning: он означает финальную попытку продолжения, а не failure.

Более строгая pre-failure точка — **20:19:29**, когда уже была сохранена P0002 retention без retained reward после breakout watch. Это всё ещё не существующий `PRESSURE_FAILURE` и не готовое trading rule.

До финального `PRESSURE_FAILURE`:

```text
20:18:23 → 20:21:28 = 185 секунд
20:19:29 → 20:21:28 = 119 секунд
```

Надёжную цену потенциального выхода нельзя определить точно из P0002 как отдельное поле: `PRESSURE_FAILURE` record не содержит dedicated event price. Доступны `attempt_price=83869.4`, `reference_price=83850.0` и `latest_extreme=83836.2`. Поэтому нельзя честно посчитать точную USD-разницу до failure без выбора surrogate price.

## 7. Cross-module P0002 audit

| Module | Evidence in 20:10–20:22 | Routing/result |
|---|---|---|
| PRESSURE | P0002 attacks, detection, retention, breakout watch, failure; также overlapping P0003 detection | P0002 failure written to pressure JSONL and quality JSONL; final human event at 20:21:28 |
| ACTIVE_MOVE_HEALTH | В доступном active-health JSONL нет полной telemetry для этого window; отдельные quality records P0002 не являются active-health records | Нельзя доказать active-health exit state для P0002 по сохранённому файлу |
| CASE1 | CASE1 runtime internal file содержит frequent `CASE1_STAGE` records, но timestamp field там представлен как `recorded_at` и требует отдельной stage-level semantic reconstruction; явного CASE1 final event в interval для P0002 не найдено | No proven CASE1 final control/exit event in this interval |
| CASE3 | CASE3 JSONL does not show a P0002 final event in 20:10–20:22 | No proven CASE3 counterattack/termination event for P0002 |
| EARLY_REVERSAL_TEST | ERT candidate `ERT000169` at 20:09, confirmed 20:10; `ERT000170` candidate at 20:22 is after P0002 failure | Before 20:21:28 no new ERT event in this window that proves exit |
| MOVE_ORIGIN_TEST | No P0002-specific MOT event before failure; next relevant historical MOT is later | No MOT-based early exit evidence |
| EFFORT_RESULT_BATTLE | Battle records are based on ERT input; no proven battle advantage transition tied to P0002 before failure | Observe-only for this episode |
| DIRECTIONAL_HYPOTHESIS | Existing coordinator state may receive CASE1/CASE3/MOT only; PRESSURE_FAILURE is not a SET source | No directional control transition caused by P0002 failure |
| TEST_ENTRY/POSITION_STATE | TEST_ENTRY/position records provide research/context, not an automatic exit for pressure failure | No order or direct position exit created by P0002 |
| COORDINATOR | Routes pressure human/quality events and separate detector events; does not turn P0002 failure into `TEST_EXIT_CANDIDATE` | Human visibility is presentation, not detector semantics |

## 8. Conclusions limited to this episode

1. `P0002` is one unique pressure episode duplicated in two JSONL files.
2. The first pressure-specific warning before failure is `PRESSURE_BREAKOUT_WATCH` at 20:18:23.
3. The first stronger pre-failure retention evidence is at 20:19:29, when P0002 still had no retained reward.
4. The final existing pressure failure event occurred at 20:21:28.
5. The evidence supports a research hypothesis that pressure failure was knowable before the final label, but the current system does not expose a separate `POTENTIAL_EXIT_SHORT` event from the combination `BREAKOUT_WATCH + failed retention`.
6. This is not a new rule and is not statistically validated: `PRESSURE_FAILURE` has `n=1` in the available JSONL set.
7. No detector, threshold, state machine, coordinator decision or human routing was changed while creating this map.

## 9. CASE2 LIVE CASE STUDIES

### CASE2-LIVE-001 — 28.09.2026

This is a research case study of the first available live CASE2 observation. It does not change CASE2 runtime logic, thresholds, state transitions, or human output.

#### Source data before the outcome

| Field | Value |
|---|---|
| LR detected | 2026-09-28 01:48:21 Panama UTC-5 |
| Timeframe | 5m |
| LR | 83002.9 — 83255.0 |
| Width | 252.1 USD / 0.303% |
| ATR | 119.9 |
| Inside ratio | 100% |
| Lower visits | 3 |
| Upper visits | 2 |
| Mid crosses | 3 |
| Net displacement / range | 0.18 |
| Directional efficiency | 0.08 |
| Average overlap | 0.37 |

After detection, CASE2 collected LIVE flow/OI only. Historical OI was not retroactively attached to the LR.

At `02:08:27`, after four new 5m candles, the saved live observation was:

| Zone | Delta | Volume |
|---|---:|---:|
| LOWER | 0.0 BTC | 0.0 BTC |
| MIDDLE | -27.2 BTC | 582.7 BTC |
| UPPER | +109.2 BTC | 381.3 BTC |

Additional saved fields:

- total OI change since LR detection: `-0.4 BTC`, approximately `0%`;
- BUY aggression bars: `0`;
- OI build during BUY aggression: `0 BTC`;
- CASE2 observed asymmetry: `BUY_SIDE`.

#### Observation / hypothesis / outcome separation

**OBSERVATION**

Upper-zone taker flow had a pronounced BUY asymmetry: `+109.2 BTC delta / 381.3 BTC volume`.

**NOT OBSERVED**

The data does not prove creation of new BUY positions. There were zero BUY aggression bars, zero OI build during BUY aggression, and total OI was approximately unchanged. Therefore the stronger wording about BUY aggression occurring while creating or maintaining OI is not supported by this case.

**PRE-OUTCOME HYPOTHESIS**

`BUY_SIDE` asymmetry followed by a DOWN departure is compatible with the research hypothesis that upper buyers may have become trapped. This is a hypothesis, not proof of locked-in causality.

**OUTCOME**

The saved CASE2 breakout record reports a DOWN departure from the LR. Its source departure time is `2026-09-28 02:20:00`, at `82929.9`. The human log block containing this result was written at `02:28:31`; that later write time is not the market event time.

**CLASSIFICATION**

`LRA_DIRECTIONAL_HYPOTHESIS_MATCH`, with `LOCKED_IN_CAUSALITY_NOT_PROVEN`.

#### Quantitative outcome available from existing records

The saved departure price was `82929.9`, which is `73.0 USD` below the LR lower boundary `83002.9` (approximately `0.088%` and `0.609 ATR` relative to the supplied ATR). This is the recorded departure displacement, not a maximum post-departure move.

The following fields were not separately saved in the available CASE2 research data and are therefore not reconstructed:

| Outcome field | Available value |
|---|---|
| Exact first trade/tick below 83002.9 | `NOT_AVAILABLE` |
| First close below LR | `NOT_SEPARATELY_RECORDED` |
| Maximum downside after departure | `NOT_AVAILABLE` |
| Return inside LR | `NOT_AVAILABLE` |
| Return time | `NOT_AVAILABLE` |
| 5m / 15m / 30m / 60m holding | `NOT_AVAILABLE` |
| MFE/MAE relative to LR lower boundary | `NOT_AVAILABLE` |
| OI/delta/volume at the exact departure bar | `NOT_SEPARATELY_RECORDED` |

The later `02:28:31` CASE2 block contains a seven-candle aggregate (`OI change -86.8 BTC`, lower-zone delta `-55.1 BTC`, lower-zone volume `222.6 BTC`), but it cannot be assigned back to the exact `02:20:00` departure without introducing unsupported reconstruction.

#### Research-only outcome schema for future CASE2 studies

This is a specification only. It is not implemented in runtime and does not create an automatic scorer.

```text
case_id
lr_detected_time
lr_low
lr_high
lr_width_pct

observed_asymmetry              # BUY_SIDE / SELL_SIDE / NONE
asymmetry_observation_time

upper_delta
upper_volume
lower_delta
lower_volume
oi_change_since_detection

departure_direction             # UP / DOWN / NONE
first_departure_time
first_close_outside_time

departure_mfe_5m
departure_mfe_15m
departure_mfe_30m
departure_mfe_60m
returned_inside_lr
return_time

hypothesis_relation              # LRA_MATCH / LRA_OPPOSITE / NO_DIRECTIONAL_TEST
locked_in_causality              # NOT_PROVEN
```

No CASE2 detector, threshold, state machine, human routing, or runtime file was changed for this case study.

### PRESSURE post-WATCH telemetry validation

- First live validation failed: baseline-only recorder; no post-WATCH snapshots were written.
- Root cause: `attempt_bar_timestamp` was stored in milliseconds, while `window_end_timestamp` was calculated in seconds. Every subsequent closed-bar timestamp was therefore outside the observation window.
- Fix implemented: observation-window boundary now uses milliseconds; recorder lifecycle has an independent cursor, completion flag, and sink-independent test path.
- Synthetic validation passed for two overlapping WATCH windows: `22` records total (baseline + 10 closed bars per window), no duplicate `(watch, bar_time)` keys, and both windows completed independently of `PRESSURE_CONTINUATION` / `NO_RESOLUTION`.
- Awaiting new live WATCH validation. Existing P0001/P0002 baseline-only records were not backfilled or reconstructed.
