# BTC-LRA effort/result transfer research

Scope: existing zones only; master 1m dataset 2026-09-20 00:00:00 -> 2026-09-29 14:09:00 Panama UTC-5.
No zones were created or modified. No thresholds, detectors, state machines, live output or trading decisions were changed.
This layer records BUY/SELL effort, directional reward, retained reward, result per 100 BTC, OI change and efficiency trajectory from each zone's BALANCE_ACTIVE evidence through departure plus 60m.
TRANSFER_CANDIDATE is fixed only from data available at its candidate timestamp. TRANSFER_CONFIRMED_AFTER_FACT uses later bars only as an outcome label.

## Benchmark cases

- 15M-1790574300000 | 2026-09-28 06:32:00 | BUY->SELL | price=82957.4 | TRANSFER_CONFIRMED_AFTER_FACT | outcome={"5m":{"mfe_usd":4.2999999999883585,"mae_usd":87},"15m":{"mfe_usd":4.2999999999883585,"mae_usd":124.20000000001164},"30m":{"mfe_usd":4.399999999994179,"mae_usd":124.20000000001164},"60m":{"mfe_usd":4.399999999994179,"mae_usd":520.3000000000029}}
- 15M-1790639100000 | 2026-09-29 00:36:00 | BUY->SELL | price=82963.4 | TRANSFER_CONFIRMED_AFTER_FACT | outcome={"5m":{"mfe_usd":37.19999999999709,"mae_usd":176.60000000000582},"15m":{"mfe_usd":37.19999999999709,"mae_usd":368.6000000000058},"30m":{"mfe_usd":37.19999999999709,"mae_usd":569.7000000000116},"60m":{"mfe_usd":37.19999999999709,"mae_usd":965.2000000000116}}

## Control cases (departure without an in-window transfer candidate)

- 5M-1789945500000 | 5M | departure=2026-09-21 03:20:00 | state=RETESTED | bounds=80819.4..82099.9
- 5M-1789968900000 | 5M | departure=2026-09-21 03:35:00 | state=RETESTED | bounds=81356..82700
- 5M-1789980000000 | 5M | departure=2026-09-21 08:05:00 | state=RETESTED | bounds=83273.6..85285
- 5M-1790145000000 | 5M | departure=2026-09-23 05:20:00 | state=RETESTED | bounds=85638.8..86534.9
- 5M-1790166600000 | 5M | departure=2026-09-23 12:10:00 | state=RETESTED | bounds=83813.8..85908.5

## Measured fields
- `effort`: taker buy/sell volume from 1m source.
- `price reward`: positive close-to-close movement for the side on each closed bar.
- `retained reward`: current progress from zone baseline in the side direction.
- `efficiency trajectory`: cumulative directional reward / cumulative side effort * 100.
- OI is used only where the source has an actual 5m sample; it is not interpolated.
- Future MFE/MAE are stored only after candidate timestamp and are never used to create the candidate.

## Interpretation boundary
- `NO_TRANSFER` means no causal transfer candidate was observed in the stored walk-forward window.
- `TRANSFER_CANDIDATE` means the causal sequence was observable at that time.
- `TRANSFER_CONFIRMED_AFTER_FACT` is an outcome label, not a live decision.
- These labels do not mean reversal, trapped inventory, market-maker activity, or an entry signal.
