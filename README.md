# BTC-LRA

BTC-LRA is a research repository for chronological Bitcoin market-risk observations.

## Entry points

- `btc-lra-001.py` — frozen production/reference implementation.
- `btc-lra-002.py` — active research observer.
- `btc-lra-002-semantic-audit.py` — restart-parity and semantic audit tool.
- `btc-dump.py`, `btc-dump-window.py` — dump utilities.

## Repository navigation

- `PROJECT_STATE.md` — current operational state only.
- `RESEARCH_NOTEBOOK.md` — canonical long-term hypotheses, findings, benchmark episodes, decisions, and future experiments.
- `data/` — reusable master inputs and ad-hoc dumps.
- `research/` — persistent research outputs grouped by topic.
- `runtime/` — generated events, state, debug files, and operational logs.
- `logs/ACTION_LOG.jsonl` — append-only operational journal; consult its tail only when forensic detail is needed.
- `references/` — books, extracted source material, and reference pages.
- `archive/` — superseded code, documents, and generated snapshots.

## Permanent organization rules

1. No new research report files in the repository root.
2. New experiments go under `research/<topic>/`.
3. Generated runtime artifacts go under `runtime/`.
4. Canonical reusable input data go under `data/`.
5. References/books/source materials go under `references/`.
6. Obsolete snapshots go under `archive/`.
7. Current operational state goes only into `PROJECT_STATE.md`.
8. Long-term conclusions, hypotheses, benchmark cases, research decisions, and future experiments go only into `RESEARCH_NOTEBOOK.md`.
9. Do not create another memory, context, notes, research-log, hypothesis, or plan file without a strong structural reason.
10. Intentional updates to `PROJECT_STATE.md` or `RESEARCH_NOTEBOOK.md` must be committed and pushed to `origin/main`.
11. `RESEARCH_NOTEBOOK.md` must remain readable directly from GitHub.

## Starting a research session

1. Read `PROJECT_STATE.md`.
2. Read `RESEARCH_NOTEBOOK.md`.
3. Inspect the latest Git commits and active processes.
4. Use `logs/ACTION_LOG.jsonl` and `runtime/logs/` only when forensic detail is required.

Do not change detector logic, market semantics, thresholds, or `btc-lra-001.py` while organizing the repository. Generated runtime data is not automatically committed.
