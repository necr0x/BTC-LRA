# BTC-LRA-001 — priority layer

Дата: 2026-09-26

## Добавлено

- `BTC_LRA_EVENT_QUALITY.jsonl` с объяснимыми quality-компонентами.
- `BTC_LRA_TEST_ENTRIES.jsonl` с исследовательскими `TEST_ENTRY_CANDIDATE`, MFE/MAE, path ordering и virtual TP/SL outcomes.
- `BTC_LRA_MARKET_EPISODES.jsonl` для группировки связанных raw events без удаления исходных событий.
- `BTC_LRA_POSITION_STATE.jsonl` для отдельной записи Binance global long/short account ratio.
- Pressure acceptance после breakout и test-entry family `PRESSURE_CONTINUATION`.
- Парсинг новых CASE1/CASE3 событий поверх общего raw log.
- Quality metrics для CASE3 counterattacks, same-side sequences и leg effectiveness.
- Исследовательский lifecycle `TREND_HEALTHY -> MOMENTUM_DECAY -> EXIT_CANDIDATE`.
- Outcomes с `time_to_MFE`, `time_to_MAE`, `MFE_before_MAE` и `AMBIGUOUS_SAME_BAR`.
- Отдельный `ACTIVE_MOVE_HEALTH` поверх CASE1: `HEALTHY -> EFFICIENCY_DECAY -> OPPOSITE_RESPONSE -> MOVE_TERMINATION_CONFIRMED`.
- `BTC_LRA_ACTIVE_MOVE_HEALTH.jsonl` с закрытыми 1m-метриками incremental reward, retention, efficiency и dOI.
- `BTC_LRA_PRE_BREAKOUT_BATTLE.jsonl` с сырыми закрытыми 1m-барами и окнами 10/20/30 минут до `PRESSURE_DETECTED` и `PRESSURE_BREAKOUT_WATCH`.
- В battle-слое отдельно считаются BUY/SELL effort, reward, retention, efficiency, последовательность атак, reward-transition и доступное OI-окно.
- Исследовательские `TEST_EXIT_CANDIDATE`/`TEST_EXIT_CONFIRMED` anchors; они не создают противоположную TEST_ENTRY и не отправляют ордер.

## Не изменено

- CASE1 и CASE3 detector source остаются baseline из архива.
- Исходные thresholds detector-ов не оптимизировались.
- Raw log и `BTC_LRA_PRESSURE_EPISODES.jsonl` не удаляются и не заменяются.
- Ни один test entry не отправляет ордер.
- OI и crowd-position данные не используются как торговое подтверждение.
- В ACTIVE_MOVE_HEALTH учитываются только CASE1 `SHOCK_LEG_ACTIVE` на TF=1m; 5m/15m сообщения не дублируют один и тот же leg.
- Warning/exit слой не меняет старые detector thresholds и работает по закрытым 1m-свечам.
- PRE_BREAKOUT_BATTLE ничего не сигнализирует и не включает звук; это только dataset для сравнения успешных, failed и unresolved breakout.

## Ограничения этой итерации

- Binance global long/short endpoint даёт account-ratio context, а не настоящий aggregate net position; это явно помечается как `net_position_available=false`.
- CASE3 historical replay через текстовый log получает только поля, которые реально присутствовали в raw event. Если detector не напечатал цену active leg, она не выдумывается.
- Thresholds quality-категорий являются описательными метками, а не торговыми фильтрами.

## Проверки

- `py_compile` прошёл.
- Import smoke прошёл без запуска сетевого цикла.
- Path-outcome smoke прошёл.
- Virtual TP/SL same-bar ambiguity smoke прошёл.
- CASE3 log-parser smoke прошёл.
