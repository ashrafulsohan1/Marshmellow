# Marshmellow — M6 Release-Feedback Round: Mascot Identity, Turn-Taking, Tray Control

> **What this is:** post-v0.1.0 user feedback from running the real app. You built this project
> through M0–M5 and the release pipeline; this round fixes four flaws found in daily use, under
> the **same workflow settings and standing rules as every previous round** (§3). Author the M6
> task-level plan first (plan-author persona), then build. Repo state below is verified against
> git + docs as of 2026-11-07.

## 0. Current state snapshot

- v0.1.0 built and in-workflow-verified: bundle `Marshmellow-v0.1.0-win64.zip` (432 MB, root),
  `RELEASE-AUDIT-final.md` + `CLEANROOM-VERIFY-final.md` PASS, last commit `179bf3b`.
- All M0–M5 milestones gated complete; 494 tests; manual resume protocol (watchdog removed).
- Open items you already track: `docs/plan/pending-user-verifications.md` (M5 Task 7 checklist
  still open), and **OQ-3 mascot art pipeline** — which this round closes.
- Known placeholder state (from your own M1 deferral note): mascot is a color-coded rounded
  square painted in `src/marshmellow/ui/mascot.py`; the tray icon renders with the system
  default ("No Icon set" warning).

## 1. The four flaws (work items F1–F4)

### F1 — The mascot was never designed. Design it. (closes OQ-3)

- **Problem:** the mascot is a programmer placeholder. The product brief promises an animated
  white-marshmallow mascot as the ambient presence.
- **Required:** design the Marshmellow mascot — **white marshmallow theme** — animated across all
  8 states (`idle, listening, thinking, acting, speaking, error, muted, attention`, see
  `mascot.py` + `01-architecture §3.10`). Decide the art pipeline (PNG sprite sequence vs Lottie
  vs live vector) and record the OQ-3 decision. Assets bundled offline in `assets/mascot/`, with
  license/origin recorded in an `assets/mascot/README.md` (same pattern as `assets/wake/README.md`).
- **Constraints that still apply:** frameless translucent always-on-top widget; click-through
  when idle; performance budget 0% CPU idle, < 2% while animating (render only on state
  change/frame tick). Existing `set_state` thread-safety and `MascotClicked` behavior preserved.

### F2 — Mascot placement: start bottom-right, make it draggable

- **Problem:** the mascot appears mid-screen, which looks bad.
- **Required:** default position = **bottom-right corner** (near the tray); **draggable**;
  position **persists across restarts** (`mascot position/size` is already a planned setting in
  §3.11 — persist to config.toml). Clamp restored positions to a visible monitor area (position
  saved on a now-unplugged monitor must not strand the mascot off-screen).

### F3 — Turn-taking rework: stop the mic from hearing the app and the media (largest item)

- **Problem (user-observed):** the listener keeps listening while Marshmellow speaks its own TTS
  output, so the next input can ingest the assistant's own voice; a playing video/music risks
  being taken as instructions.
- **User-mandated turn-taking model (implement this):**
  1. Wake ("hey marshmellow") opens a **receiving window** for user input.
  2. **3 seconds of no voice input closes the window** — the assistant then treats the
     instruction as complete and starts executing. (This is also the instruction-end detector:
     multi-sentence instructions with short pauses stay one instruction.)
  3. **The device speaker's sound must never become input.** For *how*, suggest or implement your
     own solution — evaluate with measured numbers and pick one:
     (a) hard half-duplex gate: STT disabled while TTS plays (then re-specify what replaces
     barge-in — e.g., wake-word barge-in or hotkey only);
     (b) real acoustic echo cancellation (far-end suppression of our own TTS from the mic signal);
     (c) WASAPI loopback far-end subtraction/ducking.
  4. Media guard: audio from videos/music must never become instructions without a wake.
- **Where it lives now:** `src/marshmellow/audio/loop.py` (`_speaking` flag: wake suppressed
  during playback but speech-during-playback triggers barge-in and STT keeps running),
  `activation.py`, `capture.py`. Note the divergence to reconcile: `01-architecture §3.1` says
  "self-muting while TTS plays" while the loop implements live barge-in — pick a model, make the
  code and the spec agree, and update §3.1 + §3.9 + the doc changelog in the same change.
- **Acceptance:** fixture tests — (i) TTS-on-mic audio does not transcribe or act; (ii) wake →
  instruction with pauses < 3 s stays one instruction; (iii) > 3 s silence closes the window and
  execution starts; (iv) ambient media audio without wake never triggers; (v) updated
  `tests/test_voice_loop.py` + scenario suite green. Context you already have: the wake soak
  measured 7 false accepts/hour at threshold 0.99 (`spikes/data/m5_wake_soak.json`,
  `assets/wake/calibration.json`) — a false wake now opens a 3 s window, so keep the
  calibration data in view when wiring the window.

### F4 — Mascot becomes the app logo; tray gives full user control

- **Problem:** the app works as a background service that's hard to interact with for normal users.
- **Required:** the new mascot art becomes the **app identity**: tray icon (replacing the system
  default), main-window icon, and the exe icon in `installer/marshmellow.spec`. The tray must let
  a normal user fully control the app without Task Manager: "Stop tasks" and "Quit" already exist
  (`src/marshmellow/ui/tray.py`, audit S6 hardened mid-speech kill) — keep them working, verify
  Quit exits every thread/worker cleanly, and make the tray icon state-colored per §3.11 spec.
- **Acceptance:** tray icon renders with the mascot art (no "No Icon set" warning); menu actions
  publish `TrayAction`; Quit from tray = zero orphan processes; icon shows in the built bundle,
  not just in dev.

## 2. Milestone shape

This is **M6 — release-feedback polish**. Strawman split (plan-author persona finalizes; Rule 1:
5–8 tasks, each standalone-sensible, each with its own files/tests/commit):

- T1: mascot art assets + pipeline decision (closes OQ-3)
- T2: mascot widget — bottom-right default, drag, persistence, multi-monitor clamp
- T3: turn-taking state machine (receiving window + 3 s close)
- T4: echo/media guard mechanism (measured choice from F3's options)
- T5: app identity — tray/window/exe icons wired into the installer spec
- T6: docs + tests sweep (01-architecture §3.1/3.9/3.10/3.11 + changelog, 05-open-questions OQ-3
  closed, CHANGELOG entry)
- T7: rebuild the bundle through `.zcode/skills/release-checklist` + in-workflow verification pass

**Version call is mine, not yours:** rebuild the bundle, but do **not** publish or re-version
(0.1.1?) without asking me. No GitHub release action this round unless I say so.

## 3. Workflow settings — the same pipeline as every previous round (all mandatory)

1. **Standing rules** (`docs/plan/WORKFLOW-RULES.md`, all three): task granularity 5–8 per
   milestone, each standalone-sensible; loop budget ≤ 10 per task (gate-fix ≤ 3 rounds each
   validated by a **real** gate re-run, reviewer-fix ≤ 1, milestone-fixer 1); never re-send the
   whole session — briefs pass **file paths, not contents**, ask texts stay byte-identical across
   amendments, and **no script edits of any kind during a quota pause**.
2. **Dynamic-workflow run:** author the M6 run as a fresh dynamic workflow (same shape as
   `Marshmellow-build-to-release.dwf.ts`: plan-author → builders → reviewer personas, TaskOutcome
   contracts, bounded gates) with the ledger-merged skip-list pattern — merge
   `docs/plan/build-ledger.jsonl` at runtime so a dead session restarts live. Resumption is
   **manual** (watchdog removed): I say "continue", journal replay is zero-token.
3. **Repo-as-state ledger:** every task commit appends
   `{"milestone": "M6", "task": "...", "commit": "..."}` to `docs/plan/build-ledger.jsonl`.
4. **Subagent cap:** `MAX_CONCURRENT_SUBAGENTS = 4` **including background agents** (SP6).
   Overflow queues, never dispatches; on a rejection, serialize. Briefs use the standard template
   (GOAL / INPUTS / OUTPUT / CONSTRAINTS / VERIFY); each subagent owns isolated output paths; no
   subagent edits `core/contracts.py` or another lane's outputs. **Re-Read shared files before
   editing while workers are live.**
5. **Gates:** `uv run pytest -q && uv run ruff check . && uv run mypy` — bare `&&` chain,
   **never pipes** (pipes mask exit codes — this bit three times before). Behavior changes also
   run the scenario suite (`pytest -m scenario`). Conventional commits (`feat:`, `fix:`,
   `docs:`, `chore:`). Bench output goes to files; workers read back summaries only.
6. **Personas with frugality rules:** targeted greps, no repo re-reads, task-scoped scope.
   Reviewer verdicts separate **OPEN findings vs fixed findings** (the false-block lesson) —
   a finding is blocking only while open, with evidence (`file:line`, how verified).
7. **Docs change with code:** architecture/behavior changes update `01-architecture.md` + its
   changelog in the same commit; milestone evidence lands in `02-milestones.md`.
8. **Status reporting:** keep the visual pipeline-status dashboard refreshed — I follow progress
   through it, not through prose summaries.
9. **Verification gate:** the release-candidate pass runs inside the workflow (fresh auditor +
   clean-room verifier between "prepare" and any "publish"), bounded fix rounds (≤ 2), nothing
   publishes on a high-severity or cleanroom failure.

## 4. User-owned items (extend `pending-user-verifications.md`)

Add these M6 entries so I can discharge them in one sitting later, same format as the rest:
mascot art approval + on-screen feel; drag + restart-persistence feel; turn-taking feel (speak
an instruction with pauses, watch the 3 s close; play a video and confirm the assistant ignores
it); tray icon look + full stop-from-tray.

## 5. Start here

1. Re-read `PLAN.md`, `01-architecture.md` §3.1/3.9/3.10/3.11, `WORKFLOW-RULES.md`,
   `03-workflow.md`, and the current `mascot.py` / `tray.py` / `audio/loop.py`.
2. Close OQ-3 with a recommendation I can veto, author the M6 task plan (writing-plans format,
   `docs/plan/tasks/`), and show me the plan before builders dispatch.
3. Then run the M6 pipeline under the settings above.
