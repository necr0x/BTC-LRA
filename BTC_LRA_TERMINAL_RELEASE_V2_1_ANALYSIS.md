# BTC-LRA Terminal Release V2.1

Research-only replay; existing V2 outputs are preserved. Production detector code is untouched. The impossible `(high - open) < 0` passive condition was removed; `close < open` is used only as a raw rejection observation where structurally armed, never as a standalone rule.

## Comparative sequence
| class | count | interpretation |
|---|---:|---|
| pullback later restored BUY | 5 | INTERNAL_PULLBACK / TEMPORARY_DECAY outcome |
| pullback before failed restoration | 1 | BUY_FAILED_TO_RESTORE outcome |
| passive-rejection candidates | 6 | hypothesis only; raw efficiency retained |
| LONG_EXIT_WARNING_CANDIDATE | 6 | passive BUY failure candidate, not a trading signal |
| OPPOSITE_SELL_CONTROL_CANDIDATE | 4 | separate SELL-control observation, not a trading signal |

## Benchmark 22.09

Episode: 2026-09-22 20:57:00 → 2026-09-23 00:30:00; outcome max high: 87247.3 at 2026-09-22 23:36:00.
Earlier restored pullback episodes: 5.
23:28–23:37 earliest passive/exit evidence: 2026-09-22 23:33:00; earliest SELL-control candidate: 2026-09-22 23:37:00.
First held SELL result after the actual outcome high: 2026-09-22 23:37:00.
Final pullback outcome: BUY_FAILED_TO_RESTORE at 2026-09-22 23:28:00.

### High attempts

- 2026-09-22 21:15:00 @ 86383.2; extension=36.69999999999709; effort=291.0530000000001; per100=12.609387293722133; close-vs-prev=-3.5; close-vs-new=-40.19999999999709; OI=106582.292; dOI=-27.012000000002445
- 2026-09-22 23:21:00 @ 86999; extension=35.60000000000582; effort=1911.822000000001; per100=1.8620980405082588; close-vs-prev=-2.6999999999970896; close-vs-new=-38.30000000000291; OI=none; dOI=none
- 2026-09-22 23:25:00 @ 87175.8; extension=19; effort=322.8700000000008; per100=5.884721404899791; close-vs-prev=-23; close-vs-new=-42; OI=106627.7; dOI=161.29899999999907
- 2026-09-22 23:33:00 @ 87224; extension=12; effort=919.1129999999994; per100=1.3056066011469762; close-vs-prev=-7.600000000005821; close-vs-new=-19.60000000000582; OI=none; dOI=none
- 2026-09-22 23:34:00 @ 87239.8; extension=15.80000000000291; effort=145.14000000000033; per100=10.886041063802448; close-vs-prev=-12.10000000000582; close-vs-new=-39.90000000000873; OI=none; dOI=none
- 2026-09-22 23:36:00 @ 87247.3; extension=7.5; effort=541.5339999999997; per100=1.384954591955446; close-vs-prev=-48.5; close-vs-new=-83.80000000000291; OI=none; dOI=none

## State semantics
- `PULLBACK_OBSERVATION` replaces ordinary terminal/decay labeling.
- `NEW_EXTREME_WITHOUT_RETENTION` is emitted at candle close using only data available through that close.
- `PASSIVE_REJECTION_CANDIDATE` requires the observed sequence, but does not quantify "little" with a hard threshold.
- `LONG_EXIT_WARNING_CANDIDATE` and `OPPOSITE_SELL_CONTROL_CANDIDATE` are separate research states.
- Previous-high references are candidate context, never automatic resistance.

Audit: PASS
