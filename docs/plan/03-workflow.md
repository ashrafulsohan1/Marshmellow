# Marshmellow — Division of Workload & Subagent Discipline

**Status:** v1 draft for review · Last updated: 2026-11-04
Covers brief §8.C (module boundaries, ownership, dependency ordering, integration contracts) and §8.D
(subagent usage under a hard concurrency cap).

## 1. Build mode per module

| Work | Mode | Why |
|---|---|---|
| `core/contracts.py`, EventBus, config | **inline (main agent)** | every other module compiles against these; no drift allowed |
| Planner loop design, prompts | inline | architectural core; iterate with user feedback |
| Safety policy & gate | inline | too important to delegate blind |
| Integration of any two modules | inline | cross-module context required |
| M0 spikes SP1–SP7 | **subagents (parallel)** | independent research, report outputs |
| Module scaffolds vs contracts (audio, stt, tts, llm, perception, actions, packs) | subagents (parallel, after contracts) | isolated, contract-bound |
| Scenario YAML packs + test authoring | subagents (parallel) | well-specifiable outputs |
| Mascot assets & animation | subagent | creative artifact, clear acceptance |
| Docs, ADRs, spike reports | subagents (parallel) | low coupling |
| DE-pack content (aliases, ms-settings map) | subagent | table-authoring |

## 2. Dependency ordering

```
M0 spikes ──────────────────────────────────┐ (decisions feed choices below, not contracts)
contracts + EventBus + config + fakes (M1) ─┼─▶ audio/ (capture, vad, wake) ─┐
                                            ├─▶ stt/  ───────────────────────┤
                                            ├─▶ tts/  ───────────────────────┼─▶ M2 voice loop E2E
                                            ├─▶ llm/  (openai_compat, ollama)┘         │
                                            ├─▶ perception/ (uia_reader, registry) ─┐  │
                                            ├─▶ actions/ (executor, synthesis) ─────┼─▶ M3 generic UI
                                            ├─▶ packs/windows.py ───────────────────┘  │
                                            └─▶ ui/ (mascot, tray) ────────────────────┤
planner/ (needs llm + perception + actions fakes) ─────────────────────────────────────┤
tasks/ (needs planner + monitors) ─────────────────────────────────────────────────────┴─▶ M4 ▶ M5
```

**Rule:** a module may start the moment its dependencies' *contracts + fakes* exist — not their real implementations.
This is what makes the parallel lanes safe.

## 3. Integration contracts

1. `src/marshmellow/core/contracts.py` is the **single source of truth** for all cross-module data (v1 sketch: 01-architecture §4).
2. Only the **main agent** edits `contracts.py`. Subagents consume it; they never redefine shared types locally.
3. Any contract change = PR-level note in [01-architecture changelog](01-architecture.md#changelog) + bump fakes in the same change.
4. Modules communicate state via **EventBus events**, never by importing each other's internals. UI/TTS are subscribers only.
5. Parallel work merges cleanly *because* each lane codes against fakes; a lane is "done" when it passes its contract tests against the real neighbors at integration time.

## 4. Subagent usage & concurrency discipline (brief §8.D)

> **Cap rule: `MAX_CONCURRENT_SUBAGENTS = 4` (provisional).**
> SP6 in M0 empirically measures this environment's actual limit (batch-spawn increasing N in one message until
> queueing/rejection appears) and **this line gets corrected to the measured number**. Until measured, never exceed 4.

**Operating rules:**
1. Fan-out in a single message, count ≤ cap. Overflow work is **queued** and dispatched as slots free — never assumed-parallel.
2. Prefer **fewer, well-scoped briefs** over many loose ones. A brief with a fuzzy output is serialized, not parallelized.
3. Each subagent owns **isolated output paths** (its own files/directories) — no two concurrent subagents write the same file.
4. Subagents are **read-only on `contracts.py`** and on other lanes' outputs; integration is always main-agent work.
5. Research briefs return reports as files in `docs/plan/spikes/`, not chat dumps.

**Brief template** (every subagent prompt includes):
```
GOAL: one sentence.
INPUTS: files/specs to read (exact paths).
OUTPUT: exact artifact (path + format + acceptance check).
CONSTRAINTS: don't touch X; code against contracts.py; no new deps without naming them.
VERIFY: the command/check that proves the output is acceptable.
```

**What must stay sequential:** architecture decisions, contract changes, planner design, safety design, all integration,
plan amendments. Parallelism is for *leaf work* whose outputs can be reviewed in isolation.

## 5. Working agreement

- Small, frequently committed increments; conventional commits (`feat:`, `docs:`, `test:`, `spike:`).
- Every milestone starts with its task-level plan (writing-plans format) and ends with DoD evidence (02-milestones).
- Docs change with code: architecture changes update 01-architecture + changelog in the same change.
- Quality gates (hooks, 04-tooling §3) run automatically: format/lint on edit, scenario suite before "complete", commit-message check.
