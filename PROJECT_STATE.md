# BTC-LRA Project State

## Current objective

Complete repository reorganization without changing detector logic, thresholds, market semantics, causal behavior, or research results.

## Operational status

- Long restart parity is validated after deterministic set-ordering fix.
- TEST 1 (`0→9000`, split `7000`): PASS; `2,061,933` events; strict and semantic parity PASS; no first divergence.
- TEST 2 (`0→13810`, split `11000`): PASS; `5,452,655` events; strict parity PASS; no first divergent event.
- No replay/live writer processes are currently running; stability check passed before migration.
- Production fix in `btc-lra-002.py` and audit tooling changes remain separate from this layout work until validation/commit review.

## Frozen rules

- `btc-lra-001.py` is the frozen production/reference implementation.
- `btc-lra-002.py` is the active research observer.
- Do not change detector conditions, thresholds, battle/release semantics, future-leakage rules, or production market output during organization work.
- Known benchmark timestamps are regression checks only.
- Generated runtime artifacts are not project memory and must not be added to Git; the append-only operational journal is `logs/ACTION_LOG.jsonl`.
- Current state belongs only in this file; long-term hypotheses and research conclusions belong only in `RESEARCH_NOTEBOOK.md`.
- Intentional updates to this file or `RESEARCH_NOTEBOOK.md` must be committed and pushed to `origin/main`.

## Repository layout

- Source entry points remain at root: `btc-lra-001.py`, `btc-lra-002.py`, `btc-lra-002-semantic-audit.py`, `btc-dump.py`, `btc-dump-window.py`.
- Reusable inputs: `data/master/`; ad-hoc dumps: `data/dumps/`.
- Persistent research outputs: `research/<topic>/`.
- Generated runtime: `runtime/`.
- Books and source material: `references/`.
- Historical snapshots and superseded context: `archive/`.

## Current blockers / next tasks

1. Complete the focused cleanup commits and push `origin/main`.
2. Preserve local generated runtime/dump/replay evidence while keeping those artifacts out of Git.
3. Continue the blind event-population audit only after the repository layout is stable.

## Migration audit result

- Root-level source, documentation, reference, data, research-output, runtime, archive, and temporary categories were classified and moved into the target layout.
- Active path references were updated only where required by the moves. The two old path strings embedded in `btc-lra-001.py` are replacement anchors for archived CASE source templates, not active output paths.
- Python compilation passed with the project Python 3.14 interpreter; all five moved research JavaScript files passed `node --check`; the semantic-audit `--help` smoke test passed.
- The largest local generated artifacts remain preserved under `runtime/`; they are excluded from Git. No full replay was launched for this cleanup.

## Resume protocol

On a new session, read `PROJECT_STATE.md`, then `RESEARCH_NOTEBOOK.md`, inspect latest commits, and check for active processes before touching code or moving runtime files. Read only the tail of `logs/ACTION_LOG.jsonl` when operational chronology is needed; use `runtime/logs/` for runtime forensics. Never start a duplicate long replay when a recorded process or output exists.

## Latest relevant commits

- `0e4312578af1773b80d2a731752e2417258a57ce` — persisted canonical hypothesis document and prior working memory.
- `17df982e40060861e49b656f112007b22bdbc353` — compact expected release context.
- `0d137f7e43a9eac89674b3901b48005ac9f07863` — replay memory/finalization hardening.
