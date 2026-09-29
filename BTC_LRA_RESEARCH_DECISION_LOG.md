# BTC-LRA Research Decision Log

## 2026-09-29 17:05:10 -05:00 — Terminal Release V2.1

- **task / research question:** Исправить passive rejection analysis и определить самый ранний live-observable признак failed BUY retention в benchmark 2026-09-22 между 23:28 и 23:37.
- **working hypothesis:** После retained high рынок может продолжать BUY attempts, но новые extremes могут давать непропорционально малое extension и не удерживаться; это может быть exit-warning раньше SELL control.
- **what was inspected:** `btc-lra-terminal-release-v2.js`, 1-minute MASTER data, benchmark bars 23:27–23:37, parent zones, prior swing-high references и generated V2.1 replay outputs.
- **bugs / semantic problems found:** Условие `(high - open) < 0` невозможно. Обычный pullback ошибочно требовал отдельной terminal/decay семантики. SELL-control нельзя смешивать с passive BUY failure. Previous-high context не показывал отдельный audit доступности к началу approach.
- **changes made:** Добавлены `PULLBACK_OBSERVATION`, `NEW_EXTREME_WITHOUT_RETENTION`, raw attempt metrics, `PASSIVE_REJECTION_CANDIDATE`, `LONG_EXIT_WARNING_CANDIDATE`, `OPPOSITE_SELL_CONTROL_CANDIDATE`, causal OI paths, comparison restored-vs-final pullbacks и historical reference availability audit.
- **why those changes were chosen:** Они сохраняют causal evidence на candle close, отделяют outcome classification от live observation и не вводят новый hard threshold или утверждение о конкретном passive seller.
- **alternatives rejected and why:** `close < open` как standalone rule отклонён как недостаточный trading rule; V1 `REWARD_DECAY`/`EXTREME_NOT_RETAINED` отклонены как terminal proof; абсолютный historical maximum отклонён в пользу всех pre-existing swing candidates.
- **benchmark observations:** Пять предыдущих pullbacks восстановили BUY. В финальной последовательности 23:28 pullback, затем 23:33/23:34/23:36 new extremes without retention; earliest `LONG_EXIT_WARNING_CANDIDATE` — 23:33, earliest `OPPOSITE_SELL_CONTROL_CANDIDATE` — 23:37.
- **what remains unproven:** Passive opposing liquidity не идентифицирована; candidates не являются trading signals; proportional-efficiency interpretation не является универсальным detector threshold.
- **next research question:** Сравнить ту же attempt-sequence на других BUY releases и проверить, сохраняются ли различия между restored pullbacks и failed retention без hardcoded benchmark values.
- **commit SHA:** `7912907b1b906cbd9ef6b9509ac21ff60192bffd`

## 2026-09-29 17:31:40 -05:00 — BTC-LRA-002 first live market risk observer

- **task / research question:** Создать единый causal engine для replay/live и подготовить research-only realtime observer с persistent state, silent machine telemetry и ограниченным human output.
- **working hypothesis:** Одна последовательность закрытых 1m bars должна давать одинаковые causal states в replay и live; release failure следует наблюдать через cumulative effort/result, retention и restoration, а не через одиночную свечу.
- **what was inspected:** `btc-lra-001.py`, `btc-lra-market-risk-memory.js`, `btc-lra-battle-resolution.js`, logic map, existing benchmark event references и доступный MASTER 1m dataset.
- **bugs / semantic problems found:** Требовалось отделить causal evidence от outcome и не использовать будущие bars. Для retained pushes нужен cumulative effort since previous extreme. Resolution holding не должен появляться на той же свече, что candidate. Live startup должен seed context без human alerts.
- **changes made:** Добавлен независимый `btc-lra-002.py` с общим `CausalEngine`, replay/live adapters, 5m/15m/1h/4h aggregation, nested persistent zones, battle/release state, retained-push baselines, attempt metrics, passive-rejection/exit-warning и opposite-control machine events, persistent dedupe, OI metadata и live bootstrap. Добавлены требуемые persistent 002 files.
- **why those changes were chosen:** Это сохраняет один порядок обработки closed 1m bars, делает startup/restart recoverable и оставляет research observations отдельно от human alerts и торговых приказов.
- **alternatives rejected and why:** `btc-lra-001.py` не переиспользован и не изменён; live decision logic не подключает order book/liquidations/SPX/NQ; hard relative-impact threshold не добавлен; отдельные независимые TF engines отклонены в пользу общей 1m-derived hierarchy.
- **benchmark observations:** В коде подготовлены replay checks/reference hooks для 22.09, 28.09 и 29.09, но фактический replay/self-test не выполнен: в текущей Windows-среде доступен только нерабочий WindowsApps Python alias без установленного interpreter.
- **what remains unproven:** Benchmark parity, runtime syntax, actual 23:33/23:37 event timing, restart persistence и live Binance connectivity требуют запуска в окружении с Python и dataset replay. Это не утверждается как пройденное.
- **next research question:** Запустить replay/self-test, сравнить sequence parity и leakage audit на MASTER, затем объяснить semantic mismatches до live deployment.
- **commit SHA:** `db3b2adf0bb17734f43c327135fa842b5f8707c2`

## 2026-09-29 18:26:55 -05:00 — BTC-LRA-002 hardening: causal transfer and restart safety

- **task / research question:** Привести текущий 002 engine к исследованной transfer/battle/release semantics без добавления новой стратегии и сделать restart context causal-safe.
- **working hypothesis:** Transfer должен зависеть от cumulative side effort/result, efficiency deterioration, opposite reward и retention; candidate обязан быть invalidated при restoration старой стороны. Первый pullback не является opposite control.
- **what was inspected:** `btc-lra-zone-effort-transfer.js`, `btc-lra-battle-resolution.js`, `BTC_LRA_TRANSFER_EVENTS.jsonl`, `BTC_LRA_BATTLE_RESOLUTION_EVENTS.jsonl`, existing 002 implementation и benchmark reference events.
- **bugs / semantic problems found:** Winner был основан главным образом на close относительно base. Candidate мог пережить противоположные flips. Первый pullback мог породить opposite control. Baseline имел только `OK/INSUFFICIENT`. Human text зеркально ошибался для SELL. State не rehydrate-ил TF buffers/swing references. OI выравнивался по bar open, а event не различал candle label и observable close time. Overlapping zones могли спамить BATTLE_STARTED.
- **changes made:** Перенесена cumulative effort/result efficiency-track логика с first opposite reward и retained evidence. Добавлены candidate invalidation, `TRANSFER_CHALLENGED`, `OLD_SIDE_RESTORED`, `ORIGINAL_SIDE_RESTORED`, cumulative failed-high attempt episodes и single passive warning. Baseline status стал `NO_BASELINE`/`N=n`; human warning показывает impact, baseline и relative impact. Добавлены human geometric zone grouping, `bar_time`/`observable_at`, nearest-prior OI sample metadata, 4h bootstrap depth (`3120` minutes), technical rehydration и restart parity harness.
- **why those changes were chosen:** Эти изменения следуют уже существующим research semantics, не используют future outcome для live state и не добавляют thresholds, trend logic или order decisions.
- **alternatives rejected and why:** Close/base crossing отклонён как transfer detector; same-candle opposite control отклонён; hard relative-impact cutoff не добавлен; overlapping machine zones не удаляются; historical bars при restart не переэмитят events.
- **benchmark observations:** Python 3.14.6 найден в `C:\Users\miscp\AppData\Local\Python\pythoncore-3.14-64\python.exe`. `py_compile` PASS. Self-test на 100, 300 и 400 bars: replay/restart parity PASS, future-leakage PASS, human suppression PASS; 400 bars дали 5m/15m context. Полный MASTER replay и benchmarks 22.09/28.09/29.09 не завершены из-за резкого роста overlapping battle telemetry/runtime на длинной trajectory; PASS не заявляется.
- **what remains unproven:** Full 4h startup parity, exact historical benchmark event timestamps, full persistent dedupe over complete MASTER, Binance live connectivity и performance на полном dataset. Human clustering требует проверки на полном overlapping-zone sample.
- **next research question:** Оптимизировать/профилировать полный replay без изменения semantics, затем выполнить 2880+ bar multi-TF, benchmark timeline и live adapter smoke test.
- **commit SHA:** `3ad49bc9ca22177fd8db0e224d4af4e25dadded0`
