# BTC-LRA project context

## Repository

- Local project: `C:\Users\miscp\OneDrive\Desktop\BTC-LRA-SCRIPTS`
- Public GitHub repository: `necr0x/BTC-LRA`
- Remote: `origin` → `https://github.com/necr0x/BTC-LRA.git`
- Primary branch: `main`

## Mandatory working rules

- Do not change detector logic without explicit user permission.
- Do not change thresholds or state machines without a separate explicit task.
- Preserve research JSONL and log files.
- Do not add old archives or backups to Git.
- Run a secret scan again immediately before any commit.
- Never display secret values.
- Before pushing, inspect `git diff` and the staged-file list.
- After an explicitly authorized change, commit and push to `origin/main` when GitHub authentication is valid.
- If GitHub CLI authentication is invalid, do not request a token; report the condition and do not push.

## Required initial inspection for a new session

Read, in order:

1. `BTC_LRA_LOGIC_MAP.md`
2. `.gitignore`
3. `btc-lra-001.py`
4. `git status`, current branch, and remotes

Verify GitHub CLI authentication with:

```powershell
& "C:\Program Files\GitHub CLI\gh.exe" auth status
```

## Semantic guardrails

- `BTC_LRA_LOGIC_MAP.md` is the reference map for detector and coordinator semantics.
- Detector modules produce research evidence; research events are not automatically trading orders.
- Preserve the distinction between detector evidence, coordinator routing, human presentation, and research telemetry.
- Do not reinterpret pressure, CASE1, CASE3, active-move, early-reversal, move-origin, battle, entry, exit, or directional-hypothesis events without explicit scope.

## Data handling

- JSONL files are research data sources and must be preserved.
- Human-readable logs are presentation copies and may not contain all internal telemetry.
- Existing archive/backup material remains local and must not be added to Git.
