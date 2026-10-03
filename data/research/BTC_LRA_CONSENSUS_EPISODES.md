# BTC-LRA CONSENSUS EPISODES

Research-only collapse of BTC_LRA_RELEASE_STAGES. Production monitor, CONTROL, DOMINANCE, release-stage detector, thresholds and Pine are unchanged.

CSV: `C:\Users\miscp\OneDrive\Desktop\BTC-LRA-SCRIPTS\data\research\BTC_LRA_CONSENSUS_EPISODES.csv`

Grouping uses same SIDE and EARLY-time proximity <= 2 minutes. Consensus timestamps are causal first-vote timestamps.

## Collapsed benchmark episodes

SIDE | EARLIEST EARLY | VOTES | DEFENDED | RELEASE | PATH | PATHS SEEN
---|---|---:|---|---|---|---
SHORT | 2026-09-30 21:26:00 | 4/4 (10,15,20,30) | 2026-09-30 21:27:00 | NONE | EARLY_SHORT -> SHORT_DEFENDED -> INVALIDATED | EARLY_SHORT -> SHORT_DEFENDED -> INVALIDATED
LONG | 2026-09-30 22:01:00 | 1/4 (10) | NONE | NONE | EARLY_LONG -> INVALIDATED | EARLY_LONG -> INVALIDATED
LONG | 2026-09-30 22:37:00 | 4/4 (10,15,20,30) | NONE | 2026-09-30 22:39:00 | EARLY_LONG -> LONG_RELEASE | EARLY_LONG -> LONG_RELEASE
LONG | 2026-09-30 22:51:00 | 4/4 (10,15,20,30) | 2026-09-30 22:52:00 | 2026-09-30 22:56:00 | EARLY_LONG -> LONG_DEFENDED -> LONG_RELEASE | EARLY_LONG -> LONG_DEFENDED -> LONG_RELEASE

## Independent episode comparison

CLASS | COUNT | 10m MFE MEAN | 10m MFE MEDIAN | 10m MAE MEAN | 10m MAE MEDIAN
---|---:|---:|---:|---:|---:
| RELEASE | 8 | 69.84999999999673 | 60.999999999992724 | 13.212500000001455 | 1.0500000000029104 |
| DEFENDED_ONLY | 2 | 6.799999999995634 | 6.799999999995634 | 159.15000000000146 | 159.15000000000146 |
| NO_CONFIRM | 0 | None | None | None | None |
| INVALIDATED | 3 | 0.0 | 0.0 | 133.50000000000486 | 146.1999999999971 |

## Vote-level comparison

VOTES | COUNT | 10m MFE MEAN | 10m MFE MEDIAN | 10m MAE MEAN | 10m MAE MEDIAN
---|---:|---:|---:|---:|---:
| 1/4 | 2 | 0.0 | 0.0 | 135.10000000000582 | 135.10000000000582 |
| 2/4 | 2 | 50.75 | 50.75 | 83.30000000000291 | 83.30000000000291 |
| 3/4 | 4 | 32.14999999999418 | 30.14999999999418 | 62.45000000000073 | 49.0 |
| 4/4 | 5 | 68.45999999999768 | 53.69999999999709 | 27.580000000001746 | 0.0 |

## Questions

- Episodes after half-life collapse: **13**.
- EARLY to 4/4 consensus delays: `[2.0, 0.0, 0.0, 0.0, 0.0]` minutes where available.
- MARKET RELEASE and DEFENDED-only outcomes are reported separately; no threshold is selected.
- Good episodes without MARKET RELEASE remain visible in the DEFENDED_ONLY/NO_CONFIRM groups.
