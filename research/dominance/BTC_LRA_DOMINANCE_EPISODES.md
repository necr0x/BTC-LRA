# BTC-LRA F/E/R Dominance Benchmark Episodes

Primary source: `BTC_LRA_DOMINANCE_EPISODES.jsonl`. This report is generated from that JSONL schema.

| Episode | Status | Range (Panama) | F BUY/SELL | E BUY/SELL | R BUY/SELL | OI FLOW BTC | Events | Price Δ USDT | E−R SELL pp | 5m / 15m / 30m / 60m close |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| DOM-2026-10-02-1600-1920 | BENCHMARK_ONLY | 2026-10-02 16:00:00 → 19:20 | 50.2 / 49.8 | 27.3 / 72.7 | 58.3 / 41.7 | -781.6 | 14 | +80.2 | +30.9 | 84493.6 / 84590.0 / 84648.1 / 84589.1 |

## Causal observation

The causal section records only information available inside the selected range. `F`, `E`, and `R` are full, event, and rest partitions respectively. Frozen-reference changes are relative percentage changes, not percentage-point changes.

## Outcome

Outcome fields are calculated after the selected range at 5m, 15m, 30m, and 60m. They are descriptive future measurements and are not used for causal-state classification.

## Interpretation guardrails

Benchmark evidence only; opposite-side and control cases are required. The episode does not establish whales, retail, confirmed absorption, short accumulation, market-maker activity, or a bullish signal.
