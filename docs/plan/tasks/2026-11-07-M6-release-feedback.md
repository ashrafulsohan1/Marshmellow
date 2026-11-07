# M6 Release-Feedback Round — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) or the M6
> dynamic workflow (same shape as `Marshmellow-build-to-release.dwf.ts`: plan-author → builders →
> reviewers, TaskOutcome contracts, ledger-merged skip-lists, manual resumption, cap 4). Steps use
> checkbox (`- [ ]`) syntax.

**Goal:** fix the four post-v0.1.0 daily-use flaws — design the mascot (closes OQ-3), place it
bottom-right with persisted drag, rework turn-taking so the app never hears itself or media, and
make the mascot the app identity with a fully-controlling tray — then rebuild the verified bundle.
**No publish and no re-version without an explicit user call** (§2 of the feedback brief).

**Architecture:** the turn-taking model is a **receiving window**: wake/hotkey opens it, voice
activity feeds it, 3 s of no voice closes it and dispatches the accumulated text as ONE
instruction. While TTS plays, the mic ingest path is hard-gated (half-duplex, measured) — barge-in
is hotkey-only in v1; media audio without a window never becomes input. The mascot becomes a
parametric vector rendering (one deterministic `paint(state, phase)`), whose export script produces
the identity assets (tray/window/exe icons).

**Tech Stack:** unchanged (PySide6, asyncio, faster-whisper, piper subprocess, openwakeword). New
deps: **none** (OQ-3 decision D-recorded: live parametric vector beats sprites/Lottie on
dependency weight, testability, perf budget, and identity reuse).

**Spec:** [M6-FEEDBACK-PROMPT.md](../../M6-FEEDBACK-PROMPT.md) ·
[01-architecture.md](../01-architecture.md) §3.1/§3.9/§3.10/§3.11 ·
[05-open-questions.md OQ-3](../05-open-questions.md) · [WORKFLOW-RULES.md](../WORKFLOW-RULES.md)

## Global Constraints

- Gates: `uv run pytest -q && uv run ruff check . && uv run mypy` — bare `&&` chain, never piped.
  Behavior changes also run `uv run pytest -m scenario`.
- Every task commit appends `{"milestone": "M6", "task": "Task N", "commit": "<short hash>"}` to
  `docs/plan/build-ledger.jsonl` (inside the task commit).
- No subagent edits `core/contracts.py`; shared files re-Read before editing while workers are live.
- No new runtime dependencies. Trainer/asset-tool extras stay in the `wake-training` uv group pattern.
- **No publish, no version bump** — bundle rebuild only (user owns the version call).
- Docs change with code: §3.1/§3.9/§3.10/§3.11 + changelog in the same commits as behavior.

## Review Focus

1. **Turn-taking regressions:** the window model must not reintroduce own-voice ingestion
   (fixture: TTS-on-mic audio → no transcription, no action), must keep multi-sentence
   instructions with <3 s pauses as ONE instruction, and must keep the hotkey escape working
   while TTS plays.
2. **Wake false-accept interaction:** a false wake now opens a 3 s window — the calibration data
   (7 false accepts/hour @ 0.99) must be referenced in the code path docs, and media speech inside
   a false window must still be bounded by the coherent-sentence requirement (documented residual
   risk, not silently accepted).
3. **Click-through vs drag conflict:** idle was `WindowTransparentForInput` — masking the input
   region to the marshmallow body must keep the "never blocks the screen" promise AND make drag
   possible; test both the mask geometry and the pass-through behavior.
4. **Position restore can strand the mascot:** restore must clamp to a currently-visible monitor
   (unplugged-monitor case) — test the clamp, not just the round-trip.
5. **Quit cleanliness:** tray Quit must exit every thread/worker (piper subprocess included) —
   process-census test, zero orphans.

---

### Task 1: Mascot art pipeline + assets (closes OQ-3)

**Files:** Create `src/marshmellow/ui/mascot_art.py` (parametric painter), `tools/export_mascot_assets.py`,
`assets/mascot/README.md` + exported stills. Modify `pyproject.toml` (nothing new — export tool
uses PySide6 already present).

**Interfaces:** `mascot_art.paint(painter: QPainter, state: MascotState, phase: float, size: int)`
— deterministic (fixed RNG seed / pure functions of `phase`); animation phases per state: bob
(sine, 2 s), listening squash pulse, thinking orbit dots, acting shimmer, speaking mouth scale,
error shake, muted flat-line, attention pulse. Design language: **white marshmallow cylinder**
(rounded rect body, soft shadow, two eyes + mouth per state, state-aura ring reusing the existing
`_STATE_COLORS`).

- [ ] Step 1: failing test `tests/ui/test_mascot_art.py` — determinism (same state+phase → identical
      rendered QImage hash), all 8 states paint without error at sizes 16/64/256, phase ∈ [0,1) bounds
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement painter + `tools/export_mascot_assets.py` (renders `assets/mascot/{state}.png`
      at 256 px + `preview.png` contact sheet; `--ico` mode in T5)
- [ ] Step 4: run → PASS; run export tool; assets exist
- [ ] Step 5: `assets/mascot/README.md` (origin: project-generated by the painter at
      `tools/export_mascot_assets.py`, no third-party assets, regenerate command) + ledger line + commit
      `feat(M6-T1): parametric mascot art + export pipeline (closes OQ-3)`

### Task 2: Mascot widget — bottom-right default, drag, persistence, clamp

**Files:** Modify `src/marshmellow/ui/mascot.py`, `src/marshmellow/core/config.py` (UIConfig:
`mascot_x: int | None`, `mascot_y: int | None`), `src/marshmellow/app.py` (pass persisted position
on construction; save on drag end). Test: `tests/ui/test_mascot_position.py`.

**Interfaces:** `Mascot(bus, initial_pos: QPoint | None, on_position_changed: Callable[[int, int], None] | None)`;
painting delegates to `mascot_art.paint` with a 30 fps QTimer that runs **only while the current
state animates** (idle renders one static frame, zero timers). Input model: **body-masked input**
(`setMask` to the marshmallow ellipse) replaces `WindowTransparentForInput` — surroundings always
pass through (the "never blocks the screen" promise), the body is draggable and clickable in every
state; `MascotClicked` unchanged.

- [ ] Step 1: failing tests — default position is bottom-right of the primary screen's available
      geometry (with margin); drag emits `on_position_changed`; persisted position restores;
      out-of-restore clamps into `screenAt` visible geometry (unplugged-monitor case: restore at
      (50000, 50000) → lands visible); body mask is non-empty and smaller than the widget rect
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (drag: press → track → release saves via `on_position_changed`; clamp uses
      `QGuiApplication.screens()` available geometries)
- [ ] Step 4: run → PASS; offscreen suite green
- [ ] Step 5: ledger + commit `feat(M6-T2): mascot placement, drag, persistence, clamp`

### Task 3: Turn-taking state machine — receiving window + 3 s close

**Files:** Modify `src/marshmellow/audio/loop.py` (+ new `src/marshmellow/audio/turn_taking.py` if
size demands). Test: `tests/audio/test_turn_taking.py`.

**Interfaces:** `ReceivingWindow(bus, clock, close_after_s: float = 3.0)` — states `DORMANT →
OPEN → executing`; `open()` (wake/hotkey), `feed_speech(text)` (accumulates; voice activity resets
the close timer), `close()` → emits `InstructionReady(text)` (joined accumulated text = ONE
instruction) unless nothing accumulated. The loop wires: wake/hotkey → `open()`; per-segment STT →
`feed_speech()`; `InstructionReady` → PromptBuilder → TaskManager. **The 0.7 s VAD end-silence
still closes individual SpeechSegments** — the window is the instruction-level boundary. Spec
reconciliation: §3.1 rewritten to the window model (echo guard supersedes "self-muting while TTS
plays"), §3.9 gains the barge-in rework (see Task 4), changelog entry in the same commit.

- [ ] Step 1: failing tests — (i) wake → two segments 2 s apart → one instruction with both texts;
      (ii) 3 s no-voice closes and dispatches; (iii) window closes with nothing accumulated → no
      dispatch, no error; (iv) hotkey during OPEN resets the close timer; (v) wake while executing
      → new window opens (never swallowed)
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (injected clock; no wall-clock sleeps)
- [ ] Step 4: run → PASS
- [ ] Step 5: docs reconciliation (§3.1 + changelog) in the same commit + ledger +
      `feat(M6-T3): receiving-window turn-taking (3 s close, one instruction)`

### Task 4: Echo/media guard — half-duplex gate, measured

**Files:** Modify `src/marshmellow/audio/loop.py`, `src/marshmellow/audio/capture.py` (ingest gate
hooks). Test: `tests/audio/test_echo_guard.py` + a measured-numbers table in the task's evidence.

**Interfaces:** while TTS playback is active (`SpeakingStarted…Stopped`), the **entire ingest path
is gated off** — no STT, no wake scoring, no window opening (hard half-duplex, option (a)).
**Barge-in rework:** hotkey opens a new window and kills TTS (existing control path); voice
barge-in during playback is REMOVED (own-voice safety — the app cannot distinguish its own speech
from yours without AEC). **Media guard:** no window → every audio chunk is dropped before VAD, so
media audio without wake is structurally unable to become input. Evaluation table (measured):
(a) fixture proves 0 own-voice ingestion at zero marginal cost — CHOSEN for v1; (b) real AEC
(`webrtc-audio-processing`) — probed: no maintained Windows wheel → deferred with reasoning;
(c) WASAPI loopback subtraction — DIY DSP, deferred. Both documented as post-v0.1.1 options with
the probe plan.

- [ ] Step 1: failing tests — (i) TTS-on-mic fixture: play the piper output WAV through the mic
      path while `SpeakingStarted` is active → zero transcriptions, zero actions; (ii) media fixture:
      music-shaped audio, no window → zero wake scorings, zero windows; (iii) hotkey during playback
      → window opens, TTS killed (barge-in escape preserved)
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement gate in the ingest path + loop wiring; (b)/(c) evaluation table recorded
- [ ] Step 4: run → PASS + `uv run pytest -m scenario`
- [ ] Step 5: ledger + commit `feat(M6-T4): half-duplex echo/media guard (measured, hotkey barge-in)`

### Task 5: App identity — tray/window/exe icons + full tray control

**Files:** Modify `src/marshmellow/ui/tray.py` (per-state icon from `assets/mascot/` stills,
state-colored via mascot-state subscription), `src/marshmellow/app.py` (window icon), `installer/marshmellow.spec`
(bundle `assets/mascot/` + exe icon). Test: `tests/ui/test_identity.py`.

**Interfaces:** `Tray(bus, icon_provider)` — icon swaps per mascot state (idle/muted/attention
visibly distinct); `App` sets `QIcon` on the main window; spec gains the icon + asset datas.
Quit-cleanliness: process-census test — start app (GUI mode offscreen), quit via
`TrayAction(kind="quit")`, assert zero `marshmellow`/`piper_worker` processes remain within the
injected shutdown budget.

- [ ] Step 1: failing tests — tray icon non-null after construction (no "No Icon set" warning in
      captured logs); icon changes on mascot state event; window icon set; spec file references
      the icon + mascot datas (static assert); quit-census passes
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (`tools/export_mascot_assets.py --ico` from Task 1 produces
      `assets/mascot/marshmellow.ico`)
- [ ] Step 4: run → PASS
- [ ] Step 5: ledger + commit `feat(M6-T5): app identity — mascot art in tray/window/exe; quit census`

### Task 6: Docs + tests sweep

**Files:** Modify `docs/plan/01-architecture.md` (§3.1/§3.9/§3.10/§3.11 rewritten to the implemented
model + changelog), `docs/plan/05-open-questions.md` (OQ-3 → closed, decision recorded),
`CHANGELOG.md` (M6 section), `docs/plan/pending-user-verifications.md` (M6 user-physical entries:
mascot art approval + on-screen feel; drag + restart-persistence feel; turn-taking feel — speak an
instruction with pauses, watch the 3 s close; play a video, confirm the assistant ignores it; tray
icon look + full stop-from-tray), `02-milestones.md` (M6 evidence placeholder).

- [ ] Step 1: verify every § reference matches the code (read each section against the source)
- [ ] Step 2: apply rewrites + entries; changelog entry per changed section
- [ ] Step 3: `uv run pytest -m scenario` green (docs-only, but run the gate anyway)
- [ ] Step 4: ledger + commit `docs(M6-T6): architecture reconciliation, OQ-3 closed, user checklist`

### Task 7: Rebuild bundle + in-workflow verification pass

**Files:** Build artifacts only (`dist/`, untracked) + `docs/plan/02-milestones.md` evidence.

- [ ] Step 1: rebuild via the release-checklist skill: `uv run pyinstaller installer/marshmellow.spec --noconfirm`
- [ ] Step 2: `dist/Marshmellow/Marshmellow.exe --smoke` → exit 0; confirm `piper_worker.exe` present
- [ ] Step 3: in-workflow verification pass (fresh auditor + clean-room verifier against the
      candidate, bounded fix rounds ≤ 2) — reports as `docs/plan/RELEASE-AUDIT-m6.md` /
      `CLEANROOM-VERIFY-m6.md`
- [ ] Step 4: M6 DoD evidence into `02-milestones.md`; ledger +
      `chore(M6-T7): bundle rebuilt and verified — publish/version decision reserved for the user`
- [ ] Step 5: **STOP — no publish, no version bump. The user decides.**

## Execution notes

- Strictly sequential T1→T7 (T2/T3 depend on T1's painter interface; T5 needs T1 exports; T7 needs all).
- M6 dynamic workflow: fresh script `Marshmellow-m6-release-feedback.dwf.ts` (plan-author skipped —
  this file IS the plan), TaskOutcome contracts + ledger-merged skip-lists + OPEN-vs-fixed reviewer
  verdicts (the false-block lesson) + verification gate from the v1 script. Resumption manual.
- Dashboard refreshed at dispatch and at each task completion (standing rule 8).
