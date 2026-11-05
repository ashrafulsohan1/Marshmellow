# Marshmellow — Build Pipeline Rules (user-mandated, 2026-11-05)

Three standing rules for the autonomous builder (`dwfrun-192a5372…`, amended in place) and for
any session continuing this work. Enforced via builder/reviewer/plan-author personas and the
script's own loop caps; every future milestone plan must respect them.

## Rule 1 — Task granularity
Divide a process into as many tasks as necessary, but never partition it so far that tasks stop
making sense by themselves.
**Enforcement:** plan-author persona ("each task makes sense standalone — a fresh engineer could
execute it with only the plan and the repo — but never fragment into incoherent units"); 5–8
tasks per milestone; every task has its own files/interfaces/test cycle/commit.

## Rule 2 — Loop budget
No more than 10 loops per task unless mandatory for completion. Validate every iteration:
re-read the failure and judge whether another round can plausibly succeed; escalate instead of
looping when it can't.
**Enforcement:** gate-fix loop hard-capped at 3 rounds, each round validated by a real
pytest/ruff/mypy re-run; reviewer-fix rounds capped at 1; milestone-fixer single round. All ≤ 10.
The persona adds the per-iteration validation duty.

## Rule 3 — Never re-send the whole session
After a first run, minimize token cost instead of resending full context.
**Enforcement / reasoning:**
- *Workflow:* task briefs pass **file paths, not contents**; workers read with their own tools.
  On resume, **settled steps replay from the journal at zero tokens** — a resume never re-sends
  anything. Ask texts are kept byte-identical across amendments precisely so that cache hits
  survive; only personas changed in this amendment (personas are not cache keys).
- *Main session:* context compaction is automatic; `docs/plan/BUILD-LOG.md` is the standing
  handoff (ledger, commit chain, resume mechanics) and the memory index carries project facts.
  A full handoff document would only be authored if work moved to a different agent/session.

## Token-consumption diagnosis (2026-11-05)

**Measured:** ~55M tokens over ~20.5 h run time (≈ 745 tok/s average; user-estimated 4000 tok/s
matches peak bursts inside heavy worker turns). Builders ≈ 47M of the 55M; reviewers 0.3–2.3M each (cheap).

**Outliers:** builder M2-T9 19.5M (2 steps — latency bench with verbose sweep output flooding
worker context), M2-T8 9.9M, M2-T7 6.2M — all *fix-round* sessions (2 asks) where accumulated
context + re-run heavy commands compound. Lean single-step tasks cost 0.6–5.9M (T6: 0.6M).

**Causes → fixes:**
1. Fresh builder per task re-reads repo+docs (isolation cost) → frugality rules in persona:
   targeted greps, no re-reads, task-scoped scope.
2. Verbose long-command output flooding worker context → persona: redirect benches/sweeps to
   files, read back summaries only. (Script-side gates were already tail-capped at 8 KiB.)
3. Fix-round context accumulation → already bounded (≤3 rounds, ≤10 per Rule 2) + per-iteration
   validation now mandatory.
4. Resume re-sends — none exist: journal replay is zero-token (Rule 3).
