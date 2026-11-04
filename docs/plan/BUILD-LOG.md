# Marshmellow — Autonomous Build Log

**Snapshot:** 2026-11-05 (ahead of daily quota pause) · **Builder run:** `dwfrun-192a5372-d1d5-42da-8762-3f442be7fdce` · **Watchdog:** cron every 20 min (`automation-2a5dfc7e`)

## Milestone ledger

| Milestone | Status | Evidence |
|---|---|---|
| M0 research spikes | ✅ done | SP1–SP7 reports in `docs/plan/spikes/` |
| M1 walking skeleton | ✅ done | DoD evidence in `02-milestones.md`; 41 tests; real Notepad E2E |
| **M2 voice loop E2E** | ✅ **done + milestone gate passed** | Tasks 5–9 built by the pipeline (activation, control intents, cloud orchestrator, voice loop w/ barge-in + kill switch + echo guard, piper framed protocol, latency bench harness); DoD evidence committed (`a68e92e`); assisted by-voice E2E deferred → `pending-user-verifications.md` |
| **M3 generic UI interaction** | 🔨 **in progress — 1 of 8 tasks done** | Task 1 (UIAReader settle-poll cache + element registry, `8959ea9`) committed; **Task 2 (vision fallback SoM + perception merge) mid-execution** — its uncommitted files are in the tree and owned by the running worker, do not touch them |
| M4 background tasks | pending | plan not yet authored |
| M5 polish + packaging | pending | plan not yet authored |
| GitHub release v0.1.0 | pending | private repo `0xSpecters/Marshmellow`; pipeline creates + pushes + releases |

## Resume mechanics (if/when the run pauses on daily quota)

- The run **journal replays**: every settled step (currently 54/55) resumes from cache at zero token cost — nothing is rebuilt.
- The **watchdog cron fires every 20 min**; after the daily quota reset it will resume the run automatically (stop reason `provider` → resume when the fix — quota availability — is in place).
- Manual resume, if ever needed: `ResumeWorkflowRun` with run id `dwfrun-192a5372-d1d5-42da-8762-3f442be7fdce`. Script: `.zcode/workflow-drafts/Marshmellow-build-to-release.dwf.ts`.
- Run health at snapshot: `stalled=no`, `consecutive_failures=0`, ~55M tokens spent, M3 Task 2 executing.

## Notable events so far

- Provider quota stop (2026-11-04, code 1005) → user top-up → resumed cleanly; Task 5 work replayed from journal.
- Reviewer caught real defects pre-release: Task 7 (multi-line spoken text vs worker protocol, `af67207`), Task 8 ×4 (loop teardown, kill-switch refusal, piper barge-in, mascot thread safety — `f112f50`), Task 9 ×1 (bench methodology — `a10db87`, `399f19c`).
- LLM planner had to be driven on one resident loop (`11ede6c`) — pooled async client was loop-bound.

## Commit chain (38 total; feat/fix highlights)

`8959ea9` UIAReader+registry → `0ea6b9e` M3 plan → `af67207` protocol fix → `a68e92e` M2 DoD → `1c7fbab` piper framing → `8edfad9` live composition + GUI entry → `f112f50` review fixes → `32c54db` voice loop → `11ede6c` resident-loop planner → `ff42806` orchestrator → `44f7b43` control intents → `5a194c7` activation → `f09d703` capture/VAD → `fed8980` BYOK provider → `50b55ec` piper subprocess → `903fcfc` real STT → (M1 skeleton: `2d00d93…ba5403e`) → (M0 spikes: `cda10cb…28e25bd`)

## What the user still owns

See `pending-user-verifications.md`: the assisted hands-free E2E (hotkey until the wake model trains), BYOK key entry (`set_key` CLI + config.toml), the wake false-accept soak, mascot art (OQ-3).
