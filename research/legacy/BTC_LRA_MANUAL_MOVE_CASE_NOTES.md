# MMC0001 — 2026-09-26 03:35–03:40 SHORT

## Compact result

- Requested direction: SHORT
- Raw source: `BTC_LRA_LIVE_2026-09-25_2038_DUMP.txt`
- Analysis window: 03:05–04:40 Panama time
- Earliest justified entry candidate: **03:39 @ 84248.4**
- Confirmed entry: **03:40 @ 84230.0**
- Confirmation cost: approximately **18.4 USD** in directional movement
- Termination warning: **03:37**
- Termination confirmed: **03:41**
- Sustained downside acceleration: approximately **03:45–03:49**

## Raw development

Before 03:30 the market was not yet in the relevant acceleration. From 03:30:

```text
03:30  O 84063.7 H 84082.8 L 84063.6 C 84082.8 | V 17.5  | delta +13.5
03:31  O 84082.8 H 84126.1 L 84082.7 C 84126.1 | V 63.3  | delta +48.8
03:32  O 84126.1 H 84223.3 L 84126.0 C 84192.3 | V 424.3 | delta +240.6
03:33  O 84192.3 H 84227.6 L 84187.0 C 84218.9 | V 135.3 | delta +36.3
03:34  O 84219.0 H 84235.4 L 84200.2 C 84235.4 | V 90.6  | delta +23.3
```

BUY aggression was still producing upside progress. There was no defensible SHORT origin yet.

## Transition

```text
03:35  O 84235.3 H 84235.4 L 84205.9 C 84223.4 | V 73.3  | delta +21.0
03:36  O 84223.4 H 84260.0 L 84223.3 C 84257.6 | V 108.6 | delta +65.1
03:37  O 84257.7 H 84296.6 L 84252.0 C 84253.1 | V 238.9 | delta +19.2
03:38  O 84253.1 H 84283.4 L 84253.1 C 84283.4 | V 61.7  | delta +22.7
03:39  O 84283.3 H 84283.4 L 84241.4 C 84248.4 | V 89.5  | delta -37.5
03:40  O 84248.5 H 84287.6 L 84217.0 C 84230.0 | V 122.6 | delta -27.0
03:41  O 84230.0 H 84230.0 L 84194.3 C 84213.5 | V 84.3  | delta +5.6
```

### 03:37 — termination watch

BUY still had positive delta, but the new high 84296.6 was not held: close was only 84253.1. The candle had a large upper wick and poor conversion of effort into retained price.

### 03:38 — efficiency deterioration becomes clearer

BUY delta remained positive at +22.7 BTC, but the high 84283.4 was below the previous high. BUY effort no longer produced marginal upside expansion.

This is a warning, not yet a SHORT entry.

### 03:39 — earliest justified SHORT candidate

SELL delta turned to -37.5 BTC. The bar moved from 84283.3 to 84248.4 and printed a low of 84241.4.

The important fact is not merely the negative delta. SELL immediately obtained downside price reward after BUY had just failed to extend the high. This is the first point where the data available at that close supports a SHORT hypothesis.

### 03:40 — confirmation

SELL delta remained negative at -27.0 BTC. The low extended to 84217.0 and the close moved to 84230.0. The bar also wicked back to 84287.6, so this is not a perfect one-sided candle, but the close still extended the downside response.

### 03:41 — termination confirmation

Delta turned slightly positive at +5.6 BTC, yet price continued down to 84213.5. BUY aggression did not restore the prior upside process. This is stronger evidence of control transfer than the first red response alone.

## Effort/result interpretation

The observable sequence is:

```text
BUY acceleration
→ new high with poor retention at 03:37
→ positive BUY effort without a new high at 03:38
→ SELL obtains immediate downside reward at 03:39
→ SELL extends downside at 03:40
→ price continues lower despite positive delta at 03:41
```

No actor identity is inferred. The data only show changing price response to aggression.

## OI

Minute-level OI for 03:05–04:40 is not present in the available raw dump or position-state dataset. The relevant PRESSURE record later reports `oi_context = -56.289 BTC` around 04:29, but that is an aggregate later observation and is not used as evidence for the 03:39 entry.

## System events

The available research datasets contain a later SHORT pressure record `P0004` around 04:29 at reference price 83920.0. Its role in this manual case is late continuation, not origin evidence.

No reliably time-aligned CASE1/CASE3/ACTIVE_MOVE_HEALTH event was found for 03:35–03:41 in the current datasets. The raw dump is therefore the primary evidence for the origin analysis.

## Outcome

Outcome is measured only after fixing the entries:

| Entry | Horizon | MFE | MAE |
|---|---:|---:|---:|
| 03:39 @ 84248.4 | 1m | 31.4 | 39.2 |
| 03:39 @ 84248.4 | 3m | 54.1 | 39.2 |
| 03:39 @ 84248.4 | 5m | 54.1 | 39.2 |
| 03:39 @ 84248.4 | 15m | 71.7 | 39.2 |
| 03:39 @ 84248.4 | 30m | 118.8 | 39.2 |
| 03:39 @ 84248.4 | 60m | 328.4 | 39.2 |
| 03:40 @ 84230.0 | 1m | 35.7 | 0.0 |
| 03:40 @ 84230.0 | 3m | 35.7 | 18.4 |
| 03:40 @ 84230.0 | 5m | 35.7 | 18.5 |
| 03:40 @ 84230.0 | 15m | 53.3 | 18.5 |
| 03:40 @ 84230.0 | 30m | 175.6 | 18.5 |
| 03:40 @ 84230.0 | 60m | 310.0 | 18.5 |

These outcomes do not justify moving the entry backward; they only measure the cost of waiting for confirmation.

## Classification

`EARLY_ORIGIN_CANDIDATE_WITH_CONFIRMATION`

This case is not reduced to the existence of a large later red candle. The earliest defensible point is the first SELL reward after the BUY-side marginal progress had already deteriorated.

