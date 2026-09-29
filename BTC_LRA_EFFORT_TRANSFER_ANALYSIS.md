# BTC-LRA effort/result transfer research — leakage-fixed

Scope: existing zones only; master 1m dataset 2026-09-20 00:00:00 -> 2026-09-29 14:09:00 Panama UTC-5.
No zones were created or modified. No thresholds, detectors, state machines, live output or trading decisions were changed.
Pre-event candidate logic uses only completed bars through candidate time. The future departure direction, final zone state and future outcomes are never inputs.
Future MFE/MAE and after-fact labels are stored only after candidate creation.

## Benchmark 2026-09-28 06:00–07:15 Panama
- 2026-09-28 06:17:00 | SELL->BUY | price=83049.9 | CANDIDATE_LOCAL_ONLY | departure=NONE
- 2026-09-28 06:23:00 | SELL->BUY | price=83027.9 | CANDIDATE_LOCAL_ONLY | departure=NONE
- 2026-09-28 06:50:00 | BUY->SELL | price=82955.2 | CANDIDATE_FAILED | departure=BUY
- 2026-09-28 06:56:00 | SELL->BUY | price=83018.9 | CANDIDATE_MATCHED_DEPARTURE | departure=BUY

## Benchmark 2026-09-29 00:20–02:00 Panama
- 2026-09-29 00:32:00 | BUY->SELL | price=83137.1 | CANDIDATE_LOCAL_ONLY | departure=NONE
- 2026-09-29 00:43:00 | SELL->BUY | price=83256 | CANDIDATE_LOCAL_ONLY | departure=NONE
- 2026-09-29 00:43:00 | SELL->BUY | price=83256 | CANDIDATE_MATCHED_DEPARTURE | departure=BUY
- 2026-09-29 00:46:00 | BUY->SELL | price=83200.2 | CANDIDATE_FAILED | departure=BUY
- 2026-09-29 00:48:00 | SELL->BUY | price=83231.3 | CANDIDATE_MATCHED_DEPARTURE | departure=BUY

## Control case
- 2026-09-20 03:38:00 | 5M-1789880400000 | BUY->SELL | CANDIDATE_FAILED | departure=BUY

## Outcome fields
- `future_outcome`: MFE/MAE at 5/15/30/60m, calculated only after the candidate timestamp.
- `actual_departure`, `time_to_departure_minutes`, `candidate_side_maintained_advantage`, `original_side_restored_result`, and `returned_through_candidate_origin` are after-fact descriptors.
- `CANDIDATE_MATCHED_DEPARTURE`, `CANDIDATE_FAILED`, `CANDIDATE_LOCAL_ONLY`, and `UNRESOLVED` are outcome labels, never candidate inputs.

Counts: zones=128, causal candidates=994, benchmark candidates=10.
