# MBC0001 — 2026-09-27 balance, 00:35–02:30

Restored prior independent balance case. Raw window: 116 closed 1m bars; range 84424.9–84640.0; width 215.1 USD; total volume 3335.8 BTC; BUY 1709.4 BTC; SELL 1626.6 BTC; cumulative delta +82.3 BTC.

Snapshot at 02:30: **NO CLEAR ASYMMETRY**. The initial BUY burst at 00:35–00:37 used large effort and had poor retention at 00:37. SELL later obtained repeated lower-zone downside results. The strongest local flip candidate was 01:57 poor SELL retention followed by 01:58 low-effort BUY reclaim and additional BUY progress through 02:03. At 02:30 this was still not decisive because the price remained inside the frozen range and earlier SELL attacks had also produced real retained downside.

5m OI context was only partially available; no complete verified 5m series existed. Later outcome: first close above the frozen-window high at 02:48, followed by UP acceleration around 02:49–02:52. This outcome does not rewrite the 02:30 snapshot.

---

# MBC0002 — 2026-09-27 balance, 00:40–02:15

Timezone: Panama UTC-5. Raw analysis was frozen at 02:15 before opening later bars.

## Compact result

Raw source: `BTC_LRA_LIVE_2026-09-25_2038_DUMP.txt`, 96 closed 1m bars.

Window range: 84429.6–84593.6. Width: 164.0 USD. Midpoint: 84511.6.

Zones were fixed mechanically from the window range:

- LOWER: below 84484.2667;
- MIDDLE: 84484.2667–84538.9333;
- UPPER: above 84538.9333.

Residence was counted by close. Attack aggregates were separately computed for bars touching the lower or upper boundary, so wick extension and close retention are not mixed.

Window totals: volume 2142.0 BTC; taker BUY 1007.1 BTC; taker SELL 1135.1 BTC; cumulative delta -128.4 BTC.

As of 02:15: **NO CLEAR ASYMMETRY**, but with a weak local hypothesis that BUY efficiency improved after the 01:57 SELL retention failure.

## Raw causal timeline

### 00:50–00:59: lower SELL pressure initially works

At 00:50, SELL effort was 20.7 BTC and produced 24.3 USD retained downside. At 00:53 and 00:54, SELL effort increased to 26.7 and 28.2 BTC, but retained results were only 9.1 and 14.1 USD. This is a lower marginal result than the first attack, though not a complete failure.

BUY at 00:58 used 42.9 BTC and reclaimed 26.8 USD on a retained basis. SELL returned the close to 84437.1 at 00:59. This was a local transfer, not a completed control change.

### 01:15–01:25: both sides obtain real reward

SELL at 01:15–01:16 received 10.8 and 37.3 USD retained downside. The 01:17 bar had 57.5 BTC BUY effort but retained only 4.4 USD upside from its open, so the BUY effort did not immediately translate into comparable progress.

BUY at 01:20 then used 34.8 BTC and retained 33.7 USD upside. At 01:24, SELL effort of 37.7 BTC created a downside wick but the close finished 6.1 USD above its open: the directional SELL result was negative. The following 14.4 BTC SELL at 01:25 did obtain 19.1 USD downside. Therefore this sequence is not a clean repeated failure of SELL; it is a failed attempt followed by a successful reattack.

### 01:50–02:03: strongest effort/result deterioration

SELL at 01:50 used 63.2 BTC and retained 37.3 USD downside. At 01:55, 34.6 BTC retained 19.6 USD. At 01:57, another 35.8 BTC made a new low at 84437.5, but the close was 84456.2, leaving only 7.0 USD retained from the open despite a 25.7 USD downside extension.

This is the clearest `SELL effort → worsening retention` sequence in the window.

BUY at 01:58 then used only 24.6 BTC and reclaimed 23.7 USD, almost fully retained. At 02:00 the delta was slightly negative (-0.8 BTC), but the bar closed higher, so the small SELL imbalance did not produce downside close progress. BUY at 02:02 and 02:03 used 8.2 and 8.6 BTC and advanced to 84503.0.

This is the strongest local example of `old-side retention failure → relatively easy opposite-side progress`, but it was not sufficient by itself to declare a full directional departure as of 02:15.

### 02:05–02:15: progress without decisive acceptance

SELL at 02:05 used 13.6 BTC but produced only about 0.1 USD downside extension/retention. BUY at 02:10 gained 8.7 USD. At 02:15, BUY used 5.8 BTC and gained 12.7 USD from its open, but did not create a new high beyond the earlier 84503.0 local area. This supports improving local BUY ease, not yet broad acceptance.

## Quantitative upper/lower comparison

By close residence:

| Zone | Bars | Volume | BUY | SELL | Delta |
|---|---:|---:|---:|---:|---:|
| LOWER | 32 | 622.2 | 207.8 | 414.1 | -206.7 |
| MIDDLE | 47 | 1024.3 | 517.1 | 507.7 | +9.3 |
| UPPER | 17 | 495.5 | 282.2 | 213.3 | +69.0 |

For boundary-touch aggregates:

| Boundary | Relevant side | Effort | Extension | Retained result | Retained / BTC |
|---|---|---:|---:|---:|---:|
| LOWER | SELL | 497.0 BTC | 309.1 USD | 62.2 USD | 0.1251 |
| UPPER | BUY | 341.3 BTC | 241.5 USD | 15.5 USD | 0.0454 |

The upper BUY side generated substantial wick/price extension, but much less retained result per BTC than lower SELL. That is the main quantitative asymmetry in this window. It must not be treated as a synthetic score: the aggregates contain multiple overlapping local attacks.

## OI, 5-minute resolution

The requested 5m OI context is only partially available locally. Supplemental monitor fields show approximately -29.015 BTC around the 00:45/00:49 samples and -43.721 BTC around 01:03–01:05. There is no complete verified 5m OI level/change series for every bucket from 00:40 through 02:15.

These negative changes can be described as risk destruction/deleveraging context. They cannot identify whether longs or shorts were destroyed, and they are not used to assign directional control.

## Snapshot AS OF 02:15

1. BUY at the upper boundary looks less efficient in retained result: much extension, little retained close progress.
2. SELL in the lower zone used more effort and obtained more total retained downside, especially before 01:57.
3. BUY did not systematically fail everywhere; it showed a strong local response after 01:57.
4. SELL also did not systematically fail everywhere; 00:50, 01:15–01:16 and 02:05 behaved differently.
5. There is not enough evidence for a decisive LONG or SHORT hypothesis at 02:15.

Working hypothesis only: BUY ease was improving after the 01:57 SELL retention failure. Confirmation would require repeated BUY retained progress beyond the 84503 area and failed SELL reattack. A renewed effective SELL downside move would invalidate it.

## Post-snapshot outcome

Only after preserving the 02:15 snapshot, later bars were inspected. SELL first produced a meaningful counter-move at 02:17–02:19, moving from 84502.8 to 84445.5. BUY then reclaimed to 84537.2 by 02:34. The first close above the frozen-window high 84593.6 occurred at 02:48, closing 84598.6; 02:49–02:52 accelerated to 84699.9 and higher.

This later UP departure does not rewrite the original conclusion. At 02:15 there was still no decisive asymmetry: the market first delivered a SELL counter-move before the later BUY departure.
