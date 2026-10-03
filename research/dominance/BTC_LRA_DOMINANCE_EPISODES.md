# BTC-LRA F/E/R Dominance Benchmark Episodes

Primary source: `BTC_LRA_DOMINANCE_EPISODES.jsonl`. This report is generated from that JSONL schema.

| EPISODE | STATUS | START | END | F BUY/SELL | E BUY/SELL | R BUY/SELL | E-R DIVERGENCE | OI FLOW | PRICE RESULT DURING | OUTCOME 15M | OUTCOME 30M | OUTCOME 60M | NOTES |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| DOM-20261002-1600-1920 | BENCHMARK_ONLY | 2026-10-02 16:00:00 | 19:20 | 50.2 / 49.8 | 27.3 / 72.7 | 58.3 / 41.7 | +30.9 pp | -781.6 BTC | +80.2 USDT | 84590.0 | 84648.1 | 84589.1 | benchmark evidence; comparative validation required |
| DOM-20261003-0149-LIVE | LIVE_CANDIDATE | 2026-10-03 01:49:00 | OPEN | 51.7 / 48.3 | 44.9 / 55.1 | 54.0 / 46.0 | +9.0 pp | -968.4 BTC | OPEN | OPEN | OPEN | OPEN | unfinished; no result |

## Causal observation

The causal section records only information available inside the selected range. `F`, `E`, and `R` are full, event, and rest partitions respectively. Frozen-reference changes are relative percentage changes, not percentage-point changes.

## Outcome

Outcome fields are calculated after the selected range at 5m, 15m, 30m, and 60m. They are descriptive future measurements and are not used for causal-state classification.

## Interpretation guardrails

Benchmark evidence only; opposite-side and control cases are required. The episode does not establish whales, retail, confirmed absorption, short accumulation, market-maker activity, or a bullish signal.
