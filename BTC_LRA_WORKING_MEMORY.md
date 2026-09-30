# BTC-LRA Working Memory

## Project rules
- `btc-lra-001.py` = frozen production/reference; do not change.
- `btc-lra-002.py` = active research observer.
- No future leakage.
- Known benchmark timestamps are regression checks only.
- Do not change market thresholds to reduce event population.
- Generated large logs/CSV files are not committed.
- Research changes require a decision-log entry.
- Verify the actual SHA after every commit.
- Report GitHub URLs as plain raw URLs.
- Canonical long-term research hypotheses and agenda: `BTC_LRA_PRE_RESULT_RISK_TRANSFER_HYPOTHESIS.md`.
- On every new session, read this file, the canonical hypothesis file, the tail of `BTC_LRA_RESEARCH_DECISION_LOG.md`, and recent `BTC_LRA_ACTION_LOG.jsonl` before continuing.
- Every intentional hypothesis-file update must be diffed, committed, pushed to `origin/main`, verified by actual SHA, and reported with the SHA and raw GitHub URL pinned to that SHA. Keep generated runtime outputs out of such commits; use a separate documentation commit when code changes are pending.

## Current research objective
Semantic parity and event-population audit for the existing BTC-LRA-002 full MASTER replay, without changing detector semantics, thresholds, or production output.

## Current active task
Preserve validated long-restart parity and prepare the production fix for a separate commit; population audit remains pending.

## Current execution state
- Status: `COMPLETED` (TEST 1 and TEST 2 restart parity passed; production fix remains uncommitted pending separate validation commit).
- PID: `123344` (`python.exe`), started `2026-09-30 02:43:37` local time; exited normally.
- Command: `btc-lra-002-semantic-audit.py --parity`.
- The process is no longer running; do not rerun until the failure is diagnosed.
- Current command: `btc-lra-002-semantic-audit.py --parity` completed normally.
- Expected output: `BTC_LRA_002_RESTART_PARITY.json`; TEST 1 and TEST 2 both report parity PASS.
- Per-case temporary outputs use `%TEMP%\btc002-parity-*` directories; the completed isolation run used separate `%TEMP%\btc002-isolation-*` directories.
- Result: `BTC_LRA_002_RESTART_PARITY.json` exists; TEST 1 and TEST 2 both have equal continuous/restarted event counts and matching strict digests, with no first divergent event.
- A/B result: `SET_ORDER_ONLY` restored strict and semantic digest parity; `SWING_REHYDRATION_ONLY` failed both; `BOTH` also restored strict and semantic digest parity. Event count was `2,061,933` in every branch. Battle/release object counts remain different in the audit harness because the restarted branch reports only post-split active objects (`7,211`/`6,590`) versus continuous full-run totals (`17,469`/`15,168`).

## Last completed milestone
Full MASTER technical replay after finalization compaction: 13,810 bars, 5,452,655 machine events, 43,252 battles, 39,552 releases, 216,164 human-eligible events, future leakage 0; peak RSS about 946 MB and peak private memory about 936 MB.

## Confirmed findings
- Memory/finalization hardening removed the prior giant finalization spike.
- 300-bar restart parity, telemetry, leakage, and human-suppression checks passed.
- Production/reference `btc-lra-001.py` remains unchanged.
- Full benchmark semantic parity and long restart parity are not yet proven.
- `0→9000` vs `0→7000→9000`: count `2,061,933` on both; digest continuous `fb607c6f78d504f08a6ef0582cdab9914583ef244c775f97d1d3a89f92a1ca0f`, restarted `57e923331ab0c531ab53d7f054b699b8d527865cfda4883dd7fe78d571210faa`; battles `17,469` vs `7,211`; releases `15,168` vs `6,590`.
- `0→13810` vs `0→11000→13810`: count `5,452,655` on both; digest continuous `ad2b2bf02e3438f62b6e20cd730cdc4ba049e86eea5715718f395e58c70ac6c1`, restarted `80140a8fa08d30b4e7b73e4d817f4f9a8db4f935f57c0905213ea6e738617c6f`; battles `43,252` vs `17,356`; releases `39,552` vs `16,544`.
- Correctly aligned TEST 1 event audit: first strict mismatch at sequence `938473`, `2026-09-25T01:53:00Z` (`PULLBACK_OBSERVATION`, BUY). Continuous zone/release: `5M-1790197500000` / `RELEASE-BATTLE-5M-1790197500000-1790299620000-1790301120000`; restart zone/release: `5M-1790269200000` / `RELEASE-BATTLE-5M-1790269200000-1790299620000-1790301120000`. Classification A: same event/time/direction/extreme fields, different IDs.
- First semantic mismatch at sequence `1052032`, `2026-09-25T06:14:00Z`: continuous `OPPOSITE_CONTROL_CANDIDATE` BUY versus restart `PULLBACK_OBSERVATION` SELL. Semantic digest remains equal through the strict identity drift, then diverges here.
- Split boundary evidence: membership and values for recent bars, TF rolling context, OI, cumulative volume, active/return zone IDs, active battle/release IDs, and processed-event tail match. Actual unordered iteration order of `active_zone_ids`, `return_zone_ids`, and their union differs between continuous and restarted engines. Production code directly iterates these sets in `update_zones()` and `active_zones_at()`.
- Split boundary evidence also shows `swing_candidates` differs: rehydration appends historical swing candidates after the persisted current list, leaving an old tail. This is a separate rehydration-state defect; it is not yet proven to be the first cause of the causal event-order drift.
- The completed findings and A/B isolation result are preserved in `BTC_LRA_RESEARCH_DECISION_LOG.md` and `BTC_LRA_ACTION_LOG.jsonl`.
- A/B isolation is now confirmed: unordered set iteration is sufficient to explain the strict and semantic event-stream divergence in TEST 1. Swing-candidate rehydration drift alone is not sufficient. Do not claim persistence parity validated yet; a minimal restart-only production fix still requires implementation review and a fresh `7000→9000` parity PASS.

## Unproven / open questions
- Whether the production fix should also correct `swing_candidates` rehydration as a separate restart-state defect, after the set-order cause is fixed.
- If parity fails, the first divergent event and causal field.
- Why 5.45M raw machine events reduce to 43k battles, 39k releases, and 216k human-eligible events.
- Exact full-MASTER representation, battle, release, physical-episode, and unique-human-event population mapping.

## Pending tasks
- [x] Wait for PID 123344 and inspect result.
- [x] Report parity PASS/FAIL summary.
- [x] Produce field-level first strict and semantic divergence diagnostics from aligned event streams.
- [x] Compare state immediately before the first strict divergence and at the split boundary.
- [x] Prove isolation of set-order drift versus swing-candidate drift with audit-only probes.
- [x] Complete `SET_ORDER_ONLY`, `SWING_REHYDRATION_ONLY`, and `BOTH` TEST 1 variants.
- [x] Propose and implement the smallest restart-only deterministic-order fix; do not change detector conditions.
- [x] After the fix, rerun TEST 1 `7000→9000`; strict and semantic parity PASS.
- [x] After TEST 1 PASS, run TEST 2 `11000→13810`; strict parity PASS with no first divergent event.
- [ ] Run blind event-population audit on the existing full MASTER; no filtering or threshold changes.
- [ ] Use Sep 22/23/28/29 timestamps only as post-analysis regression checks.
- [ ] Update this memory and action log after each milestone.

## Completed tasks
- [x] Finalization/memory hardening and full MASTER technical replay.
- [x] Compact expected release context in hot state.
- [x] Establish semantic parity + population-audit workstream.
- [x] Complete long restart parity run; both cases failed digest/population parity.
- [x] Complete aligned TEST 1 causal signature audit and identify first strict/semantic divergences.
- [x] Complete TEST 1 A/B isolation: deterministic set ordering is sufficient for strict and semantic parity; swing rehydration alone is insufficient.
- [x] Implement deterministic ordering for causally relevant zone/release set iteration in `btc-lra-002.py`.
- [x] Validate TEST 1 after the fix: `2,061,933` events, strict digest `c031b208f621db5e7d2d6c93bab9011cd20fb259bf5659b14506ea1a4896a9d5`, semantic digest `365824597dacc345e5fce1bed4e1e3409d47895c356a253c5df58adedeadfc24`, and no first divergence.
- [x] Validate TEST 2 after the fix: `5,452,655` events, strict digest `c52cf931bccca84bbd6939e657ec0e0355214c214851f84186704da8dd46043b` on both branches, and no first divergent event.

## Latest relevant commits
- `17df982e40060861e49b656f112007b22bdbc353` — compact expected release context in hot state.
- `d21ef076bee054d0bb242814fea0b89446a61889` — decision log update.
- `0d137f7e43a9eac89674b3901b48005ac9f07863` — harden 002 replay memory finalization.

## Resume instructions
On a new session, first read this file, the tail of `BTC_LRA_RESEARCH_DECISION_LOG.md`, and the last 20–50 records of `BTC_LRA_ACTION_LOG.jsonl`. Then run `git status`, inspect recorded processes and expected outputs, and inspect `BTC_LRA_002_RESTART_DIVERGENCE.json`, `BTC_LRA_002_SPLIT_STATE.json`, and their temp signature/state paths. Do not start TEST 2 or event-population research. First isolate unordered set iteration from `swing_candidates` rehydration drift, then propose the smallest restart-only fix.
