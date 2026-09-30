# BTC-LRA Research Notebook

This is the canonical long-term research memory. It preserves the distinction between confirmed observations, working hypotheses, unproven interpretations, and rejected ideas. Runtime JSONL/CSV/state files are evidence sources, not substitutes for this notebook.

## Core principles

- `btc-lra-001.py` remains frozen production/reference; `btc-lra-002.py` is research-only.
- Closed-bar evidence must use only data available at the event timestamp. Future bars may be used only for explicitly labeled outcome analysis.
- OI, delta, taker flow, price response, effort/result, zone state, and time must be kept separate before interpretation.
- A candidate, warning, battle observation, or release observation is not automatically a trading signal.
- The project studies functional market state and risk transfer, not participant identity.
- Known benchmark timestamps are regression checks, never detector calibration or filtering rules.

## Confirmed findings

- The full MASTER contains 13,810 1m bars, 5,452,655 machine events, 43,252 battles, 39,552 releases, 216,164 human-eligible events, and zero future leakage.
- Finalization/memory hardening removed the prior multi-gigabyte finalization spike; the full replay completed with peak sampled memory around 0.93 GB.
- Long restart event-stream parity is validated after making causally relevant set traversal deterministic. TEST 1 split 7000 and TEST 2 split 11000 both have matching continuous/restarted event counts and no first divergence.
- TEST 1 A/B isolation proved unordered set iteration alone caused the parity failure; swing-candidate rehydration drift alone did not.
- OI alone does not identify position side or participant identity. Price up/down with OI up/down describes different observable regimes but is not a standalone directional rule.
- Aggression is not control; a counterattack is not automatically a reversal; exhaustion is not automatically reversal; a sweep is not required for every continuation.
- A locked range is a structural coordinate and historical risk footprint, not proof of an active cause. Old range evidence must be separated from current activation.
- `btc-lra-002.py` research events are causal observations; MFE/MAE and outcome fields are after-fact descriptors.

## Working hypotheses

### Risk build, absorption, result degradation, retry

The pre-result chain under investigation is: risk is built, opposing flow absorbs or accepts it, directional price result degrades relative to effort, the original side retries, and the episode either restores or fails. The useful object is functional state: who is spending effort, who receives price result, whether result is retained, and when risk becomes vulnerable.

### OI / risk model

OI build is not one mechanism. Fast/compressed and slow/distributed builds can produce different later responses. Price/OI combinations are descriptive evidence. OI falling during a move may be compatible with closure or compression, but does not identify who closed or opened. OI must be aligned to its actual sample timestamp and resolution.

### Absorption / effort-result model

Measure side-specific taker effort, delta, cumulative effort, directional extension, retained extension, pullback, and efficiency. A large effort with small or decaying result is compatible with absorption or adverse acceptance, but the model must not call this proof of passive liquidity or a market-maker action without order-book/depth evidence.

### Locked Range / risk migration model

Risk can be accumulated before a visible sideways range. A range can preserve a footprint while current activation migrates elsewhere. The same price level may be old footprint, current activation, or merely a coordinate. Structural path and nested ranges are context, not automatic resistance/support.

### Trend / pullback model

Trend continuation must be distinguished from terminal behavior. Ordinary pullbacks may restore the original side. A failed restoration requires a sequence of observations, not a single candle color or hardcoded efficiency cutoff. A new extreme without retention is an observation; it is not by itself proof of opposite control.

### Impulse collision model

A strong impulse and opposing response should be modeled as a collision/episode with effort, result, retention, and follow-through. The first counterattack can be absorbed without reversing control. The model should remain descriptive until prospective evidence validates a causal state.

### Multi-timeframe hypotheses

1m evidence requires 5m/15m/1h/4h structural context. Higher-timeframe zones are aggregates of the same chronological 1m source, not independent future knowledge. Parent/child range relationships help describe where risk is accepted or rejected.

## Benchmark episodes

- 2026-09-22/23 terminal-release sequence: earlier pullbacks restored BUY; the later sequence produced new extremes without retention, passive-rejection/exit-warning candidates, and later opposite-control observations. These are regression examples, not hardcoded rules.
- 2026-09-25: pressure/sweep SHORT, SELL shock continuation, BUY counterattack, and late-dump cases test the difference between aggression, control, restoration, and stale active legs.
- 2026-09-26→27: slow rising segment, impulse/collision examples, and old-range aging cases test risk migration and multi-timeframe context.
- 2026-09-27: points 3/4 and return-to-area examples test whether a prior range remains active or is only historical footprint.
- 2026-09-28/29: live/replay observations and release lifecycle episodes remain descriptive benchmark material.

Benchmark dates and named episodes are used after blind analysis as regression checks only.

## Rejected / falsified ideas

- Participant identity cannot be inferred from candles/OI alone.
- A single `close < open`, `close > open`, or impossible `(high - open) < 0` condition is not a terminal or trading rule.
- Close/base crossing alone is not a transfer detector.
- A same-candle opposite observation is not sufficient proof of opposite control.
- A hard relative-impact threshold must not be introduced merely to reduce event population.
- Absolute historical maximums are not valid substitutes for pre-existing swing candidates.
- Future outcome labels must not be used as live detector inputs.

## OI / risk model

Retain source resolution, sample timestamp, OI value, dOI, age, and alignment metadata. Treat OI as context for risk build/decay and inventory replacement hypotheses. Do not fabricate liquidations or infer long/short identity from OI direction.

## Absorption / effort-result model

The minimum auditable record includes side effort, cumulative effort, price result, extension, retained result, pullback, and efficiency trajectory. `PASSIVE_REJECTION_CANDIDATE`, `LONG_EXIT_WARNING_CANDIDATE`, and opposite-control candidates are research observations and require sequence context.

## Locked Range / risk migration model

Separate old footprint from current activation, local boundaries from parent boundaries, and structural coordinates from causal proof. Range departure, return, retest, turnover, and OI path are measurable events; “trapped inventory” and “market maker” are hypotheses requiring additional data.

## Trend / pullback model

Track restored pullbacks, failed restoration attempts, new extremes, retention, and the first held opposite result. Do not collapse temporary decay into terminal failure. Keep outcome analysis separate from live-observable event evidence.

## Impulse collision model

Use battle areas and market episodes to group related raw events without deleting raw evidence. Compare effort/result on both sides and require retention/follow-through before stronger interpretation.

## Multi-timeframe hypotheses

The 1m engine derives 5m, 15m, 1h, and 4h context. Higher-timeframe context is available only after the relevant closed bars. Parent nesting, range aging, and interaction density are candidate explanatory variables, not thresholds.

## Open research questions

- Why 5.45M machine events reduce to 43k battles, 39k releases, and 216k human-eligible events.
- How raw events map to zone representations, battle episodes, release episodes, physical market episodes, and unique human events.
- Which OI/flow/depth observations can distinguish absorption, risk migration, and inventory replacement prospectively.
- Whether swing-candidate rehydration should receive a separate restart-state repair even though it was not the parity root cause.
- How stale ranges and nested parent paths affect later release quality without introducing hard thresholds.
- Whether the descriptive candidate states remain stable in live long-duration operation.

## Pending experiments

1. Blind event-population audit over the existing full MASTER; no filtering or threshold changes.
2. Physical episode/zone-representation grouping and unique-human-event audit.
3. Order-book/depth validation of absorption and passive-risk hypotheses.
4. Prospective validation of effort/result degradation and restoration sequences.
5. Separate audit of swing-candidate rehydration semantics.

## Decision history

Historical decisions are preserved in `archive/docs/BTC_LRA_RESEARCH_DECISION_LOG.md`. The current parity decision is: deterministic ordering is the minimal restart parity fix; TEST 1 and TEST 2 passed; no detector or threshold changes were made. The pre-result risk-transfer theory and benchmark agenda are preserved in this notebook from the former canonical hypothesis document.
