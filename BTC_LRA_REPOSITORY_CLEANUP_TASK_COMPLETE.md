REPOSITORY STRUCTURE CLEANUP + PATH-SAFE MIGRATION

The long restart-parity work is now complete:

TEST 1: PASS
TEST 2 split 11000: PASS
5,452,655 causal events
strict + semantic parity PASS
first divergent event: none

Do not start another full 13,810-bar parity run just for repository cleanup unless a shorter validation exposes a problem.

GOAL

Clean up the BTC-LRA repository structure so that source code, durable project memory, research artifacts, runtime logs, generated data, references, and archives are clearly separated.

This is NOT only a filesystem move.

You MUST audit and update every producer/consumer path so that after the migration all scripts continue to read/write the correct files in their new locations.

Do not change detector logic, thresholds, causal semantics, event classification, or btc-lra-001 behavior.

--------------------------------------------------
1. TARGET STRUCTURE
--------------------------------------------------

Repository root should remain small and understandable.

Target approximately:

BTC-LRA/
│
├─ btc-lra-001.py
├─ btc-lra-002.py
├─ btc-lra-002-semantic-audit.py
│
├─ PROJECT_STATE.md
├─ RESEARCH_NOTEBOOK.md
│
├─ data/
│  ├─ master/
│  └─ dumps/
│
├─ research/
│  ├─ restart-parity/
│  ├─ battle-resolution/
│  ├─ effort-transfer/
│  ├─ release-lifecycle/
│  ├─ terminal-release/
│  ├─ market-risk/
│  └─ experiments/
│
├─ runtime/
│  ├─ btc-lra-002/
│  └─ legacy/
│
├─ logs/
│  └─ ACTION_LOG.jsonl
│
├─ references/
│
└─ archive/

Adjust exact subfolder names only where current dependencies make another layout clearly better.

Do not create unnecessary folders.

--------------------------------------------------
2. CANONICAL PROJECT MEMORY
--------------------------------------------------

Reduce human-facing project memory to TWO canonical Markdown files.

A. PROJECT_STATE.md

Merge/replace the useful current-state content from:
BTC_LRA_WORKING_MEMORY.md

It should contain only:
- current project goal;
- current production/research status;
- what is running now;
- confirmed technical state;
- unresolved blockers;
- immediate next tasks;
- startup/resume instructions;
- important operational rules.

Do not turn this into a research-history archive.

B. RESEARCH_NOTEBOOK.md

Merge the durable useful content from:
BTC_LRA_RESEARCH_DECISION_LOG.md
BTC_LRA_PRE_RESULT_RISK_TRANSFER_HYPOTHESIS.md

Also incorporate only genuinely useful durable conceptual material from older research notes when appropriate.

RESEARCH_NOTEBOOK.md should contain:
- confirmed findings;
- hypotheses still unproven;
- rejected hypotheses/approaches when important;
- causal model development;
- benchmark episodes;
- research decisions and why;
- future experiments;
- relevant research commit SHAs.

Clearly distinguish:
CONFIRMED
WORKING HYPOTHESIS
OPEN QUESTION
BENCHMARK ONLY

Do not silently convert hypotheses into proven conclusions.

The operational JSONL log remains separate:

logs/ACTION_LOG.jsonl

It is machine/operational history, not mandatory reading in full at startup.

--------------------------------------------------
3. IMPORTANT: PATH DEPENDENCY AUDIT
--------------------------------------------------

BEFORE moving generated/runtime files, scan the entire tracked codebase for every path/file reference.

Search all .py, .js, .md config/helper code for references to filenames such as:

BTC_LRA_002_EVENTS.jsonl
BTC_LRA_002_STATE.json
BTC_LRA_002_ZONE_STATE.json
BTC_LRA_002_BATTLES.jsonl
BTC_LRA_002_RELEASES.jsonl
BTC_LRA_002_DEBUG.log
BTC_LRA_002_OI_SAMPLES.jsonl

and ALL other:
.json
.jsonl
.csv
.log
.md
.txt

files that scripts programmatically read or write.

Build a temporary path-dependency inventory:

FILE
PRODUCER
CONSUMER
CURRENT PATH
NEW PATH
PERSISTENT/RESTART REQUIRED?
GENERATED?
GIT TRACKED?

Do not commit this inventory unless it is useful as durable documentation.

--------------------------------------------------
4. UPDATE SCRIPT PATHS
--------------------------------------------------

After deciding the target locations, update every producer and consumer consistently.

Examples:

runtime state/output from btc-lra-002:
runtime/btc-lra-002/...

master datasets:
data/master/...

manual/imported dumps:
data/dumps/...

research experiment outputs:
research/<experiment>/...

references:
references/...

archives:
archive/...

IMPORTANT:

If a script writes a file to a new directory, it must create that directory safely when missing.

Prefer centralized path definitions rather than scattering hardcoded strings throughout the code.

For Python, prefer pathlib.Path based on the repository/script directory, not the current shell working directory.

For example conceptually:

PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = PROJECT_ROOT / "runtime" / "btc-lra-002"

Do not make runtime correctness depend on launching the script from one specific working directory.

If JS research tools need equivalent path handling, update them too.

--------------------------------------------------
5. RESTART / PERSISTENCE FILES REQUIRE SPECIAL CARE
--------------------------------------------------

STATE, ZONE_STATE and any restart-critical persistent files must move consistently.

The same script that writes them must read the same new path after restart.

Do not accidentally start a fresh engine because the old state path changed.

Provide either:

A. a one-time safe migration of existing active state into the new runtime directory,

or

B. an explicit migration/fallback mechanism if needed.

Do not silently maintain two competing active state files.

After migration there must be exactly one canonical active location.

--------------------------------------------------
6. GENERATED FILE POLICY
--------------------------------------------------

Do not keep huge runtime/generated files in repository root.

Large/generated artifacts such as:
EVENTS
BATTLES
RELEASES
STATE
DEBUG
population outputs
temporary audit outputs
replay telemetry

should live under runtime/ or research/ as appropriate.

Do NOT add giant generated files to Git.

Update .gitignore for the new runtime/generated structure.

Tracked source/research documentation remains tracked.

--------------------------------------------------

--------------------------------------------------
7. LEGACY / OLD ARTIFACT CLEANUP
--------------------------------------------------

Do not blindly delete files.

Classify every root-level artifact.

A. ACTIVE SOURCE

Keep in root only if it is a current primary/general executable source.

Expected examples:

- btc-lra-001.py
- btc-lra-002.py
- btc-lra-002-semantic-audit.py

Other active general tools can remain only if that is clearly justified.

B. ACTIVE DURABLE MEMORY

Only these should remain canonical in root:

- PROJECT_STATE.md
- RESEARCH_NOTEBOOK.md

After their content has been safely merged, move superseded documents to:

archive/legacy-docs/

Likely examples:

- BTC_LRA_WORKING_MEMORY.md
- BTC_LRA_RESEARCH_DECISION_LOG.md
- BTC_LRA_PRE_RESULT_RISK_TRANSFER_HYPOTHESIS.md
- old PROJECT_CONTEXT / duplicate context documents if superseded

Do not archive them until useful content has been confirmed merged.

C. RESEARCH RESULTS

Move experiment-specific reports/outputs under logical research folders.

Examples:

- battle-resolution
- effort-transfer
- release-lifecycle
- terminal-release
- restart-parity
- market-risk
- pre-breakout
- pressure
- counterattack
- move-origin
- event-quality
- early-reversal

If an artifact is one-off/unfinished, use:

research/experiments/<short-topic>/

Avoid dozens of unnecessary top-level folders.

D. DATASETS

Move MASTER datasets to:

data/master/

Move manual/imported/window dumps to:

data/dumps/

Update every consumer.

E. RUNTIME OUTPUT

Move current btc-lra-002 live/replay files to:

runtime/btc-lra-002/

Examples:

- BTC_LRA_002_EVENTS.jsonl
- BTC_LRA_002_BATTLES.jsonl
- BTC_LRA_002_RELEASES.jsonl
- BTC_LRA_002_STATE.json
- BTC_LRA_002_ZONE_STATE.json
- BTC_LRA_002_DEBUG.log
- BTC_LRA_002_OI_SAMPLES.jsonl

Legacy runtime artifacts may go to:

runtime/legacy/

F. REFERENCES

Move:

- Tom Leksey PDF/text
- book/reference materials

to:

references/

G. ARCHIVED CODE

Old backup/archive source copies should not clutter root.

Examples:

- btc-lra-001.py.archive_*
- btc-lra-001.py.backup_*
- superseded experimental source copies

Move them under:

archive/code/

unless Git history already makes the copy unnecessary and it is clearly safe to delete.

Prefer archive over deletion when provenance is uncertain.

--------------------------------------------------
8. DO NOT BREAK RESEARCH REPRODUCIBILITY
--------------------------------------------------

When moving research artifacts, preserve the relationship between:

- producer script;
- analysis report;
- generated events/episodes;
- audit;
- benchmark notes.

Prefer preserving filenames and moving them as a coherent group.

Do not rename everything just for aesthetics.

If a research script assumes sibling files, update its path handling.

--------------------------------------------------
9. OLD ROOT PATHS MUST BE AUDITED AFTER MIGRATION
--------------------------------------------------

After moving and updating code, run a second global search for legacy root paths.

Any remaining reference to an old path must be one of:

- deliberate migration fallback;
- archival documentation;
- old immutable historical evidence.

There must be no accidental active producer/consumer still writing to root.

Pay special attention to:

- btc-lra-002.py
- btc-lra-002-semantic-audit.py
- active JS research scripts
- dump scripts
- MASTER-data readers
- restart/audit tools

--------------------------------------------------
10. PATH-SAFE VALIDATION
--------------------------------------------------

Do NOT rerun the entire 13,810-bar MASTER unless a shorter validation reveals a real failure.

A. STATIC VALIDATION

Require:

- Python compile PASS;
- JS syntax/basic invocation PASS where relevant;
- no unintended stale path references;
- required directories safely auto-created.

B. SHORT REPLAY VALIDATION

Run only enough bars to prove:

- MASTER data is read from the new location;
- runtime events are written to the new location;
- state is written to the new location;
- zone state is written to the new location;
- debug/OI/runtime outputs use new paths;
- engine logic still executes.

A few hundred bars should be enough initially.

C. SHORT RESTART PARITY VALIDATION

Use the existing deterministic harness on a short case:

0 → N continuous
vs
0 → split → restart → N

Require:

- strict parity PASS;
- semantic parity PASS.

Do not run the full 5.45M-event TEST 2 again merely for a path migration when the short deterministic test passes.

D. EXISTING-STATE MIGRATION VALIDATION

If a legacy current state is migrated:

- prove the new path is used;
- prove the legacy path is not rewritten;
- prove future state updates occur only in the new canonical location.

--------------------------------------------------
11. VERIFY THAT THE PATH MIGRATION DID NOT CHANGE RESULTS
--------------------------------------------------

This is a path/structure task, not market-research logic work.

For the short replay compare, at minimum:

- causal event count;
- strict digest where available;
- semantic digest;
- battle/release counts where relevant;
- restart result.

Path changes must not change event semantics/order.

If they do, stop and investigate instead of continuing cleanup.

--------------------------------------------------
12. DO NOT MIX CLEANUP WITH MARKET-LOGIC RESEARCH
--------------------------------------------------

During this task do NOT:

- modify detection thresholds;
- change zone logic;
- change battle logic;
- change release logic;
- change OI interpretation;
- add new research detectors;
- change human-event semantics;
- optimize event population;
- alter physical-episode clustering logic;
- implement FAST/SLOW OI research;
- implement absorption/collision research.

Repository cleanup only.

Any unrelated semantic diff is a blocker and must be removed from the cleanup commit.

--------------------------------------------------
13. PROTECT EXISTING UNCOMMITTED WORK
--------------------------------------------------

Before modifications:

1. git status
2. inspect all tracked/untracked changes;
3. identify unrelated current work;
4. determine which current changes belong to:
   - restart deterministic-order production fix;
   - audit harness;
   - research/action logs;
   - runtime/generated outputs.

There are known local changes that may intentionally be uncommitted.

Do not overwrite, discard, or accidentally bundle unrelated work into cleanup commits.

If necessary:

- first commit already completed validated production/audit work separately;
- or preserve it in a clearly isolated way before structural migration.

Do not use destructive reset/clean commands.

--------------------------------------------------
14. GIT STRATEGY
--------------------------------------------------

Keep Git history understandable.

A reasonable sequence is:

COMMIT 1 — VALIDATED PENDING TECHNICAL WORK

If there are already validated uncommitted restart/audit changes that belong before restructuring, inspect and commit them separately first.

Do not mix them with cleanup.

COMMIT 2 — CANONICAL MEMORY CONSOLIDATION

Create:

- PROJECT_STATE.md
- RESEARCH_NOTEBOOK.md

Move/archive old memory documents only after confirming successful merge.

COMMIT 3 — TRACKED DIRECTORY MOVES

Move tracked research/reference/archive/data files into the new structure.

COMMIT 4 — PATH-SAFE CODE MIGRATION

Update producers/consumers and .gitignore.

COMMIT 5 — FINAL STATE/RESUME DOCUMENTATION

Only if necessary.

The exact commit grouping may differ if another sequence is safer.

Do not produce one opaque mega-commit if clean separation is practical.

--------------------------------------------------
15. GITHUB MUST MATCH LOCAL TRACKED STRUCTURE
--------------------------------------------------

Final tracked structure must be pushed to:

origin/main

Local cleanup alone is not sufficient.

After every cleanup commit:

1. obtain actual commit SHA;
2. push;
3. verify remote main;
4. report the verified SHA.

GitHub tracked structure must match the final intended local tracked structure.

Ignored generated runtime files can remain local only.

--------------------------------------------------
16. PERMANENT MEMORY RULE AFTER MIGRATION
--------------------------------------------------

Codex startup/resume procedure becomes:

MANDATORY

1. read PROJECT_STATE.md
2. read RESEARCH_NOTEBOOK.md

CONDITIONAL

3. inspect recent tail of logs/ACTION_LOG.jsonl only when operational chronology is useful.

Do not recreate multiple overlapping memory documents later.

When new durable market/research knowledge is discovered:

update RESEARCH_NOTEBOOK.md

When current execution state / blockers / next steps change:

update PROJECT_STATE.md

When recording chronological operations:

append logs/ACTION_LOG.jsonl

--------------------------------------------------
17. RESEARCH NOTEBOOK GIT-PERSISTENCE RULE
--------------------------------------------------

RESEARCH_NOTEBOOK.md is the canonical durable shared research memory.

Whenever it is intentionally updated:

1. inspect the diff;
2. commit the update;
3. push to origin/main;
4. verify actual remote SHA;
5. report exact SHA;
6. report raw plain GitHub URL pinned to that SHA.

Do not leave important durable research conclusions only in chat, terminal output, or an unpushed local file.

Major intentional updates to PROJECT_STATE.md needed for reliable resume should also be pushed.

--------------------------------------------------
18. ROOT CLEANLINESS TARGET
--------------------------------------------------

After migration, repository root should not contain dozens of files such as:

BTC_LRA_*_DEBUG.log
BTC_LRA_*_EVENTS.jsonl
BTC_LRA_*_STATE.json
BTC_LRA_*_ANALYSIS.md
BTC_LRA_*_AUDIT.md
btc-lra-001.py.archive_*
btc-lra-001.py.backup_*

Root should visually communicate:

- current main source code;
- canonical current state;
- canonical research notebook;
- organized project directories.

Correctness first, cleanliness second.

--------------------------------------------------
19. DO NOT DELETE EVIDENCE CASUALLY
--------------------------------------------------

Before deleting anything:

- determine whether Git tracks it;
- determine whether it is reproducible;
- determine whether it is referenced by a script/report;
- determine whether it contains unique historical evidence.

Prefer moving uncertain/legacy evidence to archive over deletion.

Large reproducible generated outputs do not need to be committed merely to preserve them.

--------------------------------------------------
20. FINAL VALIDATION REPORT
--------------------------------------------------

Before calling cleanup complete, report all of the following.

STRUCTURE

- final repository root listing;
- important subdirectories;
- canonical memory files.

PATH AUDIT

- major programmatic path references changed;
- remaining intentional legacy fallback paths;
- confirmation that active scripts no longer write migrated outputs to root.

RUNTIME

Report canonical paths for at least:

- btc-lra-002 runtime directory;
- STATE;
- ZONE_STATE;
- EVENTS;
- BATTLES;
- RELEASES;
- DEBUG;
- OI samples.

DATA

Report canonical paths for:

- MASTER datasets;
- dumps/manual windows.

VALIDATION

Report:

- Python compile result;
- relevant JS validation result;
- short replay result;
- short restart parity result;
- strict/semantic parity;
- whether a full replay was necessary;
- confirmation that detector semantics/thresholds were unchanged.

GIT

For every cleanup-related commit report:

- exact verified SHA;
- commit purpose;
- files moved/changed;
- push/remote verification.

Finally provide raw plain GitHub URLs pinned to the final cleanup SHA for:

- PROJECT_STATE.md
- RESEARCH_NOTEBOOK.md
- btc-lra-002.py
- btc-lra-002-semantic-audit.py

Do not format these as Markdown links. Print the raw URLs.

--------------------------------------------------
21. HARD CONSTRAINTS
--------------------------------------------------

- Do not modify btc-lra-001.py market semantics.
- Do not modify btc-lra-002.py detector/market semantics.
- Do not change thresholds.
- Do not reduce event population by filtering.
- Do not delete research evidence without classification.
- Do not add giant runtime outputs to Git.
- Do not rerun the full MASTER merely for confidence if short deterministic path/restart validation is sufficient.
- Do not leave duplicate active state paths.
- Do not leave canonical project memory split across many competing documents.
- Do not break execution from non-root working directories.
- Do not move files while an active process is writing them.
- Do not claim completion until local paths, restart persistence, validation, Git structure, and remote push are verified.

--------------------------------------------------
DEFINITION OF DONE
--------------------------------------------------

This task is complete only when:

1. repository root is clean and understandable;
2. tracked files are organized into the new directory structure;
3. runtime/generated artifacts no longer clutter the root;
4. every active producer/consumer path is updated;
5. restart-critical files have one canonical active location;
6. scripts create required target directories safely;
7. short replay works;
8. short restart parity passes;
9. project memory is reduced to:
   - PROJECT_STATE.md
   - RESEARCH_NOTEBOOK.md
   - logs/ACTION_LOG.jsonl
10. .gitignore matches the new generated/runtime structure;
11. GitHub reflects the new tracked structure;
12. cleanup commits are pushed and exact SHAs verified;
13. no detector logic or threshold semantics changed.
