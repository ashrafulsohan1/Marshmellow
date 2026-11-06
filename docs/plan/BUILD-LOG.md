# Marshmellow — Autonomous Build Log

**Snapshot:** 2026-11-06 · **Builder run:** `dwfrun-71b6a13e-9fb6-48b7-8d7b-f67afa1961e7` (supersedes `dwfrun-da24d2ea`, cache-imported) · **Watchdog: REMOVED by user 2026-11-06 — resumption is manual**

## Milestone ledger

| Milestone | Status | Evidence |
|---|---|---|
| M0 research spikes | ✅ done | `docs/plan/spikes/` |
| M1 walking skeleton | ✅ done | DoD in `02-milestones.md`; real Notepad E2E |
| M2 voice loop | ✅ done | milestone gate passed; 142 tests; by-voice E2E deferred → `pending-user-verifications.md` |
| M3 generic UI interaction | ✅ done | DoD evidence `490102d`; 8-app scenario suite; safety gate L0–L3 fail-closed; planner tool loop |
| **M4 background tasks** | 🔨 **in progress — ~3 of 7 tasks done** | monitor checks + scheduler (`08da7d3`), task classes/control API (`cfbeba8`), foreground lease (`df9ef8f`, `d263eeb`); Task 4 re-dispatched with the ledger protocol |
| M5 polish + packaging | pending | plan authored by pipeline when reached |
| GitHub release v0.1.0 | pending | private `0xSpecters/Marshmellow` — pipeline publishes at the end |

## Pause-safety by design (2026-11-06)

Three layers, so a quota pause costs nothing and resumes itself:

1. **Journal replay (same-lineage resume).** On a quota stop, `ResumeWorkflowRun` replays every settled step at zero tokens and re-dispatches only the in-flight ask. Requirement: the script file stays byte-identical — **no edits of any kind during a pause** (personas are cache keys; the 2026-11-05 persona edit caused a ~3M-token re-walk).
2. **Repo-as-state ledger.** From M4-T4 on, every builder appends `{"milestone", "task", "commit"}` to `docs/plan/build-ledger.jsonl` inside its task commit. The script merges the ledger into its skip-lists at runtime, so even a **brand-new run after a dead session** starts live at the right task — no stale hardcoded `doneIds`, no `already_done` re-walks.
3. **Watchdog: removed by the user (2026-11-06).** Resumption is therefore **manual**: when quota returns, say "continue" in the session or run `ResumeWorkflowRun("dwfrun-71b6a13e-9fb6-48b7-8d7b-f67afa1961e7")`. The journal replay + ledger make that a one-step, zero-rework operation. If a future run errors on script bugs: refresh `doneIds`/`alreadyVerified` from the ledger + git log, then `AmendWorkflow` — never edit during a quota pause.

## Notable events

- 2026-11-05: persona-edit cache break → ~3M-token re-walk; fixed structurally (skip-lists + `alreadyVerified`); lessons in `WORKFLOW-RULES.md` + memory.
- 2026-11-06: first watchdog cron lost with workspace context → recreated with the pause-safe protocol; workflow made ledger-driven so watchdog loss is no longer fatal to correctness.
- Reviewer catches across M3: ref re-validation race, blocking steps off the planner loop, GUI-thread confirm marshal, password values in listings, lease bypass + loop freeze — all fixed pre-commit.

## What the user still owns

`pending-user-verifications.md`: assisted hands-free E2E (hotkey until wake model trains), BYOK key via `set_key` + config.toml, wake false-accept soak, mascot art (OQ-3).
