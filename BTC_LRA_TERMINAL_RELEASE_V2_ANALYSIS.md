# BTC-LRA Terminal Release V2

Research-only replay; existing V1 outputs are preserved. Production detector code is untouched.
V1 events at 21:01–21:04 are not used as proof of terminal release. The benchmark BUY release begins 20:57 at 86255; 23:36 at 87247.3 is used only as after-fact navigation in the outcome.

## Comparative sequence
| class | count | interpretation |
|---|---:|---|
| temporary decay later restored BUY | 5 | INTERNAL_PULLBACK / TEMPORARY_DECAY |
| decay before failed restoration | 1 | BUY_FAILED_TO_RESTORE; outcome-only |
| passive-rejection candidates | 0 | behavioral hypothesis only; no seller identity inferred |

## Benchmark 22.09

Episode: 2026-09-22 20:57:00 → 2026-09-23 00:30:00; max high observed in outcome window: 87247.3 at 2026-09-22 23:36:00.
Earliest challenged: 2026-09-22 21:01:00; earliest SELL held result after the actual outcome high: 2026-09-22 23:37:00.
Final decay outcome: BUY_FAILED_TO_RESTORE at 2026-09-22 23:28:00; earlier BUY restorations remain separate outcomes.

### High sequence

- HIGH_1 2026-09-22 20:57:00 @ 86257.8; extension=2.8000000000029104; effort_since_previous=23.207; incremental_extension_per_100_BTC=12.065325117434009; retained_at_close=true
- HIGH_2 2026-09-22 20:58:00 @ 86280; extension=22.19999999999709; effort_since_previous=63.05899999999999; incremental_extension_per_100_BTC=35.20512535878637; retained_at_close=true
- HIGH_3 2026-09-22 20:59:00 @ 86333.1; extension=53.10000000000582; effort_since_previous=110.83500000000001; incremental_extension_per_100_BTC=47.90905399919323; retained_at_close=true
- HIGH_4 2026-09-22 21:00:00 @ 86346.5; extension=13.39999999999418; effort_since_previous=71.06299999999999; incremental_extension_per_100_BTC=18.85650760591895; retained_at_close=true
- HIGH_5 2026-09-22 21:15:00 @ 86383.2; extension=36.69999999999709; effort_since_previous=291.0530000000001; incremental_extension_per_100_BTC=12.609387293722133; retained_at_close=false
- HIGH_6 2026-09-22 21:17:00 @ 86387.5; extension=4.30000000000291; effort_since_previous=37.24599999999998; incremental_extension_per_100_BTC=11.544863878008142; retained_at_close=true
- HIGH_7 2026-09-22 21:18:00 @ 86406.9; extension=19.39999999999418; effort_since_previous=36.38199999999995; incremental_extension_per_100_BTC=53.32307184870048; retained_at_close=true
- HIGH_8 2026-09-22 21:19:00 @ 86443.9; extension=37; effort_since_previous=79.548; incremental_extension_per_100_BTC=46.51279730477196; retained_at_close=true
- HIGH_9 2026-09-22 21:20:00 @ 86528; extension=84.10000000000582; effort_since_previous=121.13200000000006; incremental_extension_per_100_BTC=69.4283921672273; retained_at_close=true
- HIGH_10 2026-09-22 21:21:00 @ 86593.3; extension=65.30000000000291; effort_since_previous=168.06799999999998; incremental_extension_per_100_BTC=38.853321274723875; retained_at_close=true
- HIGH_11 2026-09-22 22:20:00 @ 86594.5; extension=1.1999999999970896; effort_since_previous=920.2000000000005; incremental_extension_per_100_BTC=0.13040643338373062; retained_at_close=true
- HIGH_12 2026-09-22 22:21:00 @ 86636; extension=41.5; effort_since_previous=83.79700000000003; incremental_extension_per_100_BTC=49.52444598255306; retained_at_close=true
- HIGH_13 2026-09-22 22:22:00 @ 86643.5; extension=7.5; effort_since_previous=30.989000000000033; incremental_extension_per_100_BTC=24.20213624189226; retained_at_close=true
- HIGH_14 2026-09-22 22:23:00 @ 86726.7; extension=83.19999999999709; effort_since_previous=274.92599999999993; incremental_extension_per_100_BTC=30.2626888690037; retained_at_close=true
- HIGH_15 2026-09-22 22:24:00 @ 86781; extension=54.30000000000291; effort_since_previous=168.63200000000006; incremental_extension_per_100_BTC=32.200294131601886; retained_at_close=false
- HIGH_16 2026-09-22 22:39:00 @ 86798.1; extension=17.10000000000582; effort_since_previous=563.3400000000001; incremental_extension_per_100_BTC=3.035467035894099; retained_at_close=true
- HIGH_17 2026-09-22 22:40:00 @ 86963.4; extension=165.29999999998836; effort_since_previous=488.8069999999998; incremental_extension_per_100_BTC=33.817027988549356; retained_at_close=true
- HIGH_18 2026-09-22 23:21:00 @ 86999; extension=35.60000000000582; effort_since_previous=1911.822000000001; incremental_extension_per_100_BTC=1.8620980405082588; retained_at_close=false
- HIGH_19 2026-09-22 23:23:00 @ 87156.8; extension=157.8000000000029; effort_since_previous=864.6569999999992; incremental_extension_per_100_BTC=18.250011276148005; retained_at_close=true
- HIGH_20 2026-09-22 23:25:00 @ 87175.8; extension=19; effort_since_previous=322.8700000000008; incremental_extension_per_100_BTC=5.884721404899791; retained_at_close=false
- HIGH_21 2026-09-22 23:27:00 @ 87212; extension=36.19999999999709; effort_since_previous=352.7359999999999; incremental_extension_per_100_BTC=10.262632677128817; retained_at_close=true
- HIGH_22 2026-09-22 23:33:00 @ 87224; extension=12; effort_since_previous=919.1129999999994; incremental_extension_per_100_BTC=1.3056066011469762; retained_at_close=false
- HIGH_23 2026-09-22 23:34:00 @ 87239.8; extension=15.80000000000291; effort_since_previous=145.14000000000033; incremental_extension_per_100_BTC=10.886041063802448; retained_at_close=false
- HIGH_24 2026-09-22 23:36:00 @ 87247.3; extension=7.5; effort_since_previous=541.5339999999997; incremental_extension_per_100_BTC=1.384954591955446; retained_at_close=false

## State semantics
- `DECAY_OBSERVATION` is live and causal; it is not terminal.
- `PASSIVE_REJECTION_CANDIDATE` records behavior compatible with passive opposing liquidity without asserting a limit seller.
- Restoration and terminal/failed classifications are outcomes, never live evidence.
- `EXTREME_NOT_RETAINED` is not emitted for each local high; active release extremes are represented by the HIGH_1 → pullback → HIGH_2 sequence.

Audit: PASS
