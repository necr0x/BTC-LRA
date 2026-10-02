# BTC-LRA Neighbor Flow Analysis

Diagnostic-only output. The production STRONG/MEGA detector, AGGR, CONTROL, DOMINANCE, GUI, Pine and replay chronology were not changed.

All OI ratios use the existing `annotate_minutes()` causal baselines. 3m ratios use only completed rolling windows ending before the current minute. Per-minute CONTROL is the existing `Session.effort_result_control()` applied to that minute, in a fresh diagnostic session.

- CSV: `C:\Users\miscp\OneDrive\Desktop\BTC-LRA-SCRIPTS\data\research\BTC_LRA_NEIGHBOR_FLOW_ANALYSIS.csv`
- OI minutes analyzed: 527
- Current STRONG/MEGA minutes: 14

## Rule comparison

Extra minutes means selected minutes minus the current STRONG/MEGA set. Noise is a selected extra minute with no meaningful aggression and `CONTROL UNCLEAR`; this is a transparent diagnostic indicator, not a new detector rule.

| RULE | EXTRA MINUTES | EARLIEST USEFUL 22:xx | NOISE |
|---|---:|---|---|
| CURRENT ELEVATED | 160 | 22:11 | 15:33, 15:39, 16:04, 16:10, 16:49, 17:02, 17:30, 17:37, 17:43, 18:50, 19:18, 20:32, 21:20, 21:38, 21:47, 22:35, 22:49, 23:01 |
| 3M 2X | 53 | -- | 18:30, 19:18, 20:32, 21:28, 22:38 |
| 3M 3X | 24 | -- | 19:18, 20:32, 21:28, 22:38 |
| 3M 4X | 6 | -- | -- |
| 3M 5X | 2 | -- | -- |
| STRONG OR ELEVATED OR 3M | 179 | 22:11 | 15:33, 15:39, 16:04, 16:10, 16:49, 17:02, 17:30, 17:37, 17:43, 18:30, 18:50, 19:18, 20:32, 21:20, 21:28, 21:38, 21:47, 22:35, 22:38, 22:49, 23:01 |

## Requested minutes

| TIME | STRONG? | OI ACT X | 3M X | AGGR | EFF_RATIO | CONTROL |
|---|---|---:|---:|---|---:|---|
| 22:10 | NO | 0.83483309 | 0.72177437 | BUY 82.32758621% +15 BTC | 0.98581502 | CONTROL MARKET BUY |
| 22:11 | NO | 1.86322694 | 0.99204544 | SELL 85.18041237% +54.6 BTC | 0.28556738 | CONTROL LIMIT BUY |
| 22:12 | NO | 1.52888244 | 1.3173689 | SELL 67.55102041% +17.2 BTC | 0.06520236 | CONTROL LIMIT BUY |
| 22:13 | NO | 0.39549044 | 1.18184298 | SELL 74.62686567% +9.9 BTC | 1.90652658 | CONTROL MARKET SELL |
| 22:14 | NO | 0.35842961 | 0.69468669 | SELL 81.69642857% +14.2 BTC | 0.28424026 | CONTROL LIMIT BUY |
| 22:15 | NO | 1.02435312 | 0.52686069 | BUY 77.77777778% +4.5 BTC | 1.25658718 | CONTROL MARKET BUY |
| 22:16 | NO | 0.95811052 | 0.68915719 | BUY 54.47154472% +3.3 BTC | 3.57033811 | CONTROL UNCLEAR |
| 22:17 | NO | 1.98917552 | 1.15981117 | BUY 79.11931818% +41 BTC | -0.10161181 | CONTROL LIMIT SELL |
| 22:18 | NO | 0.86423875 | 1.11416344 | SELL 54.31472081% +1.7 BTC | 24.0502306 | CONTROL UNCLEAR |
| 22:19 | NO | 0.24694047 | 0.89963088 | BUY 71.92982456% +2.5 BTC | 0.62261703 | CONTROL MARKET BUY |
| 22:20 | NO | 0.42692974 | 0.46059079 | SELL 79.8816568% +10.1 BTC | 2.95155806 | CONTROL MARKET SELL |
| 22:21 | NO | 0.4579553 | 0.33465556 | SELL 69.375% +6.2 BTC | 4.9596291 | CONTROL MARKET SELL |
| 22:22 | NO | 0.37253817 | 0.36929262 | BUY 69.35483871% +4.8 BTC | 0.89363026 | CONTROL MARKET BUY |
| 22:23 | NO | 0.46868776 | 0.38043091 | SELL 55.93220339% +2.8 BTC | -7.17027562 | CONTROL LIMIT BUY |
| 22:24 | NO | 0.63688778 | 0.42928223 | SELL 66.66666667% +4.5 BTC | 4.45985778 | CONTROL MARKET SELL |
| 22:25 | NO | 0.67298395 | 0.5054785 | BUY 64.70588235% +5 BTC | 0.29343351 | CONTROL LIMIT SELL |
| 22:26 | NO | 2.01268869 | 0.90079088 | BUY 98.85222382% +68.1 BTC | 0.471689 | CONTROL LIMIT SELL |
| 22:27 | NO | 3.25749051 | 1.73393083 | BUY 73.41576507% +30.3 BTC | 0.02926572 | CONTROL LIMIT SELL |
| 22:28 | NO | 0.2131362 | 1.59215098 | BUY 54.08805031% +1.3 BTC | 0.48043635 | CONTROL UNCLEAR |
| 22:29 | NO | 1.67830592 | 1.48851074 | SELL 55.82329317% +5.8 BTC | -1.48889015 | CONTROL LIMIT BUY |
| 22:30 | NO | 0.9174408 | 0.78237524 | SELL 79.35483871% +9.1 BTC | 1.59427833 | CONTROL MARKET SELL |
| 22:31 | NO | 0.55790622 | 0.88962495 | SELL 63.06306306% +2.9 BTC | 7.5228592 | CONTROL MARKET SELL |
| 22:32 | NO | 0.62804339 | 0.61350739 | BUY 55.3030303% +2.8 BTC | 15.32255052 | CONTROL MARKET BUY |
| 22:33 | NO | 0.78621307 | 0.51587673 | SELL 66.85082873% +6.1 BTC | -1.80523747 | CONTROL LIMIT BUY |
| 22:34 | NO | 1.46984624 | 0.71635684 | SELL 66.07929515% +7.3 BTC | -0.91205223 | CONTROL LIMIT BUY |
| 22:35 | NO | 1.47448523 | 0.94070388 | BUY 50.82508251% +0.5 BTC | 0.17831232 | CONTROL UNCLEAR |
| 22:36 | NO | 1.75947774 | 1.25083307 | BUY 63.23024055% +7.7 BTC | 4.85148448 | CONTROL MARKET BUY |
| 22:37 | YES | 10.41324524 | 3.76711033 | BUY 69.39843069% +44.5 BTC | 0.06796188 | CONTROL LIMIT SELL |
| 22:38 | NO | 0.51393891 | 3.45313496 | SELL 54.41176471% +1.2 BTC | -1.53921984 | CONTROL UNCLEAR |
| 22:39 | YES | 8.73792669 | 5.33887354 | BUY 75.79872204% +64.6 BTC | 0.60102368 | CONTROL MARKET BUY |
| 22:40 | NO | 2.44150994 | 3.23046542 | SELL 76.64974619% +21 BTC | 0.3620763 | CONTROL LIMIT BUY |

## Focused interpretation

- **22:17**: OI ACT X `1.98917552`, AGGR MAG X `4.63276836`, CONTROL `CONTROL LIMIT SELL`, EFF_RATIO `-0.10161181`. It is absent from the current event table because `STRONG/MEGA=NO/NO` is false; the table is driven only by individual STRONG minutes.
- **22:26**: OI ACT X `2.01268869`, AGGR MAG X `7.96491228`, CONTROL `CONTROL LIMIT SELL`, EFF_RATIO `0.471689`. It is absent from the current event table because `STRONG/MEGA=NO/NO` is false; the table is driven only by individual STRONG minutes.
- **22:27**: OI ACT X `3.25749051`, AGGR MAG X `3.42372881`, CONTROL `CONTROL LIMIT SELL`, EFF_RATIO `0.02926572`. It is absent from the current event table because `STRONG/MEGA=NO/NO` is false; the table is driven only by individual STRONG minutes.
- **22:37**: OI ACT X `10.41324524`, AGGR MAG X `6.59259259`, CONTROL `CONTROL LIMIT SELL`, EFF_RATIO `0.06796188`. It is absent from the current event table because `STRONG/MEGA=YES/NO` is false; the table is driven only by individual STRONG minutes.
- **22:39**: OI ACT X `8.73792669`, AGGR MAG X `10.50406504`, CONTROL `CONTROL MARKET BUY`, EFF_RATIO `0.60102368`. It is absent from the current event table because `STRONG/MEGA=YES/NO` is false; the table is driven only by individual STRONG minutes.

**Earliest 22:10–22:37 meaningful aggression with opposite LIMIT control:** 22:11.

## Counts by comparison window

| WINDOW | RULE | SELECTED | EXTRA |
|---|---|---:|---:|
| 14:45–15:00 | CURRENT ELEVATED | 10 | 4 |
| 14:45–15:00 | 3M 2X | 10 | 4 |
| 14:45–15:00 | 3M 3X | 10 | 4 |
| 14:45–15:00 | 3M 4X | 10 | 4 |
| 14:45–15:00 | 3M 5X | 8 | 2 |
| 14:45–15:00 | STRONG OR ELEVATED OR 3M | 12 | 6 |
| 20:25–20:45 | CURRENT ELEVATED | 4 | 3 |
| 20:25–20:45 | 3M 2X | 3 | 2 |
| 20:25–20:45 | 3M 3X | 2 | 1 |
| 20:25–20:45 | 3M 4X | 0 | -1 |
| 20:25–20:45 | 3M 5X | 0 | -1 |
| 20:25–20:45 | STRONG OR ELEVATED OR 3M | 5 | 4 |
| 22:10–22:40 | CURRENT ELEVATED | 11 | 9 |
| 22:10–22:40 | 3M 2X | 4 | 2 |
| 22:10–22:40 | 3M 3X | 4 | 2 |
| 22:10–22:40 | 3M 4X | 1 | -1 |
| 22:10–22:40 | 3M 5X | 1 | -1 |
| 22:10–22:40 | STRONG OR ELEVATED OR 3M | 12 | 10 |

No production threshold was selected by this diagnostic pass.
