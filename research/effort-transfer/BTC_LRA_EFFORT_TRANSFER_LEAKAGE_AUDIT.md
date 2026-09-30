# BTC-LRA effort/result transfer leakage audit

## Scope

Audited commit `7dc60d8` and the corrected `btc-lra-zone-effort-transfer.js` against the existing MASTER 1m dataset and existing zone/event files. `btc-lra-001.py` was not modified.

## Leakage found in 7dc60d8

1. `departureEvent -> departureSide` was selected before iterating pre-departure bars. The pre-departure candidate therefore knew the future release direction.
2. `departure_ts` defined the analysis window end. This made the amount of future data available to the pre-departure pass depend on the future departure.
3. The future departure side was assigned to `oldSide/newSide`, so a pre-departure sequence could be labeled with a future winner.
4. The old confirmation test used `after.some(retained_reward > 0)`, which was too weak to describe an actual outcome.
5. The old record copied the final zone state into the research record without clearly separating it from causal event inputs.

## Corrections

- The causal pass now calculates BUY and SELL independently at every completed 1m bar.
- A candidate can only be created after the currently observed sequence: one side has an observed efficiency deterioration, the opposite side obtains reward, and the opposite side retains reward on a subsequent observed bar.
- The causal pass does not read `departure_ts`, departure direction, final zone state, future highs/lows, future return/retest data, or outcome labels.
- The causal observation window is fixed from the existing `BALANCE_ACTIVE` end (`end_ts`) for six hours, capped only by the available MASTER dataset end. It is not anchored to future departure.
- Departure direction is loaded only after candidate creation as an outcome label.
- Future MFE/MAE and return/maintenance/restoration fields are calculated only after the candidate timestamp and never feed back into candidate creation.
- After-fact labels are now `CANDIDATE_MATCHED_DEPARTURE`, `CANDIDATE_FAILED`, `CANDIDATE_LOCAL_ONLY`, or `UNRESOLVED`.

## Verification

- `node --check btc-lra-zone-effort-transfer.js` is required before replay.
- `btc-lra-001.py` remains untouched.
- No production human log or detector JSONL is written by this replay script.
