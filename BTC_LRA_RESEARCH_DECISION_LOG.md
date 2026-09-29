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
