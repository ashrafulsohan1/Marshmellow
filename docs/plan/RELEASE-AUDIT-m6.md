# Marshmellow — M6 Pre-Publish Release Audit (rebuilt bundle candidate)

**Date:** 2026-11-08 · **Auditor:** fresh adversarial pass (read-only except the one fix below)
**Candidate:** the rebuilt one-dir bundle `dist/Marshmellow/` built from HEAD
`864e4629b483597ba67e672ef53f28b175401bb7` (M6 Tasks 1–6 + the post-T6 live-use safety fix)
**plus the Task 7 verification-pass fix** — the single working-tree delta
(`tests/ui/test_identity.py`, +14/−2, the clean-checkout census skip; no product code touched).
The Task 7 commit's code content is exactly what this round verified; the round-2 cleanroom
re-verified it independently (see [CLEANROOM-VERIFY-m6.md](CLEANROOM-VERIFY-m6.md)).
**Prior baseline:** [RELEASE-AUDIT-final.md](RELEASE-AUDIT-final.md) (v0.1.0 at `c6dc40d`) —
carry-over findings re-assessed against the M6 delta.
**Bounded fix rounds:** 2 allowed · **1 used** (F1 below). No product-code fix was required.

## Verdict

**PASS for the publish decision — 0 new high findings; 1 clean-checkout gap found and fixed
(fix round 1); all four M6 feedback flaws verified fixed and gated; the bundle builds, smokes
green, and carries every datas directory the spec names. The publish/version call remains the
user's (Task 7 Step 5 — no publish, no version bump).**

| Severity | Count | Findings |
|---|---|---|
| **High** | 0 open | F1 (clean-checkout census FAIL) found this round → **FIXED in round 1** |
| **Medium** | 5, all carry-over, unchanged | S3 spoken secrets in plaintext logs · S4 unconfirmed LLM-chosen monitor egress · S5 raw exception text as a secret channel · P1 automatic screenshot egress + crash-persistent temp artifacts · A1 model-authored rationale spoken/logged |
| **Low** | 6 open, 1 closed | L1 dead `api_key_ref` · L2 dev-mode STT hub fetch · L3 clipboard gate inert · L5 63 MB voice blob in history · L6 internal pipeline docs tracked · L7 dead `.gitignore:16` entry (`%LOCALAPPDATA%/`) · L8r pyproject lacks `license`/`readme` · **L9r CLOSED** (the frozen-bundle layout was spec-read-only in every prior round — this round built it and inspected it, §2) |

---

## 1. The candidate — build record

- **Inputs:** `installer/fetch_bundle_inputs.py` → all present before fetching
  (`voices/en_US-lessac-medium.onnx` + `.json`, `models/faster-whisper-base/model.bin` 145,217,532 B,
  `assets/mascot/marshmellow.ico` + `idle.png`); the script printed `[ok]`/no-op for existing inputs.
- **Build:** `uv run pyinstaller installer/marshmellow.spec --noconfirm` → **exit 0**
  (PYZ → PKG → EXE → COLLECT all "completed successfully"). 6 warnings, all standard
  PyInstaller noise: optional hidden imports not found (`pycparser.lextab/yacctab`, `tzdata`,
  `scipy.special._cdflib`) and the two ctypes-declared `UIAutomationClient_VC140_X64/X86.dll`
  COM type-library DLLs that load from the Windows system directory at runtime — not bundle data.
- **Layout (OQ-9 one-dir, two exes):** `dist/Marshmellow/Marshmellow.exe` (28,055,535 B, GUI +
  `--smoke` entry) and `piper_worker.exe` (5,347,608 B, the SP7 GPL-isolated subprocess) side by
  side in the dist root; `_internal/` (74 top-level entries) carries:
  `mascot/` (all 10 identity files — 8 state stills + `marshmellow.ico` + `preview.png`, 200 KB),
  `wake/` (the D8 self-trained model + both feature extractors + calibration, 3.4 MB),
  `voices/` (the lessac onnx pair), `models/faster-whisper-base/` (ct2 model + tokenizer),
  `faster_whisper/assets/silero_vad_v6.onnx` (the VAD the AudioEngine loads on boot — the M5
  frozen-crash fix), `piper/espeak-ng-data` (19 MB — the worker cannot phonemize without it),
  `openwakeword/resources` (belt-and-braces). **Bundle total 718 MB.**
- **Artifacts stay untracked:** `git check-ignore -v` → `dist/`, `build/`, `voices/`, `models/`
  all ignored (`.gitignore:6,7,48,55`). Nothing from the build can leak into the commit.
- **Headless smoke:** `dist/Marshmellow/Marshmellow.exe --smoke` → prints **`smoke ok`**,
  **exit 0** — the frozen GUI graph imports and boots headless (FakeSTT/FakeTTS path).

## 2. M6 delta review (`c6dc40d..HEAD`) — the regression surface

43 files, +3420/−216; product code +1167/−70 across 12 files. Line-cited verdicts:

- **`core/contracts.py` — minimal, changelogged.** One addition: `InstructionReady(text: str)`
  (§4 sketch of the doc pins every contract change to the changelog; the 2026-11-07 M6-T3 entry
  records it as "One contracts addition: `InstructionReady(text)` (§3.1)").
- **`audio/capture.py` — the half-duplex + media-guard gate (`IngestGate`/`IngestMode`, M6-T4).**
  `mode` precedence: `DROP` (TTS playing) beats `WAKE_ONLY` (no window) beats `FULL`; without a
  binding both probes read False → fail-safe `WAKE_ONLY` (nothing reaches VAD). DROP skips the
  chunk before the wake tap AND the VAD (capture.py engine loop: `if mode is IngestMode.DROP:
  continue`); WAKE_ONLY taps only, drops before VAD (media audio without wake is structurally
  unable to become input). Leaving FULL resets the segmenter, so gate-dropped audio can never
  resurface as a segment stitched across the gap (`VADSegmenter.reset`).
- **`audio/loop.py` — wiring.** Hard half-duplex: a segment arriving while `_speaking` is dropped
  untranscribed (voice barge-in REMOVED — own-voice safety without AEC); the hotkey is the only
  barge-in and it kills TTS + opens a fresh window. The wake-suppression echo guard is retained
  as defense in depth. Abort-family intents (`stop/cancel/kill_all/sleep`) `cancel()` a pending
  window — collected-but-undispatched speech never executes after the kill. The close tick arms
  `max(remaining, 0.0) + 0.05 s` from `seconds_until_close()` (the M6-T3 review fix: the close
  lands one margin past the deadline however arms/feeds interleave — regression test
  `test_close_lands_one_margin_past_the_deadline_despite_late_feeds`).
- **`audio/turn_taking.py` (new, 147 lines) — read in full this round.** Every transition is
  `threading.Lock`-guarded; `InstructionReady` is emitted OUTSIDE the lock (re-entry safe);
  `feed_speech` outside an OPEN window is ignored (nothing collected while asleep); whitespace
  is not voice activity (no accumulation, no timer reset); close with nothing accumulated is
  silent. Matches §3.1 as rewritten at Task 6.
- **`ui/mascot.py` — placement contract (M6-T2).** Default = bottom-right of the primary
  screen's available geometry minus a 12 px edge margin; drag = press → track (live-clamped) →
  release reports once via `on_position_changed`; restore clamps via `screenAt` on the intended
  center with a nearest-screen fallback (`min` by Manhattan distance) — the unplugged-monitor
  case lands visible. Input = body-masked `QRegion` (body+aura ellipse): everything outside
  passes through in every state (the never-blocks promise), the body is draggable/clickable in
  every state. The 30 fps QTimer runs only while the current state animates; idle renders one
  static frame with zero timers.
- **`ui/tray.py` — identity + full control (M6-T5).** `MascotIconProvider` loads the committed
  still per state and falls back to re-rendering via the parametric painter — the icon is never
  null; state events arrive on the bus drain thread and hop to the GUI thread over a queued Qt
  signal. Menu = Mute/Unmute/Open Marshmellow/Stop tasks/Quit; the engine decides semantics.
- **`app.py`.** Mascot position persisted to `[ui] mascot_x/mascot_y` on drag end
  (`OSError`-guarded); `app_icon()` set app-wide and on the summoned dashboard; tray
  `pause` → `manager.cancel_all()` + `tts.stop()` (the S6 fix, `app.py:875`) + spoken ack.
- **`safety/policy.py` — window-killer escalation (post-T6 live-use fix).** `alt+f4`,
  `ctrl+shift+w`, `ctrl+shift+f4` (normalized) return `"L3"` UNCONDITIONALLY — placed before the
  strict-preset escalation, and overrides only escalate (`_max_level`), so preset/override cannot
  wave a window-killer through (policy.py `classify`: the `return "L3"` precedes both).
- **`planner/prompts/orchestrator_v2.md`.** Two prompt rules from the live-use incidents: tabs
  are not windows (`ctrl+w`/the tab's own close button, never `alt+f4`/`ctrl+shift+w`) and
  check-the-user's-live-app-before-acting (an app holding focus with recent user activity is
  theirs; defer and ask). Prompt-level mitigations — the gate (policy) is the enforcing layer;
  residual LLM-adherence risk folds into carry-over A1.

**The plan's five review-focus items, verified:**

1. **Turn-taking regressions:** two segments 2 s apart join into ONE instruction; 3 s of no
   voice closes and dispatches; empty close is silent; hotkey during OPEN re-arms and keeps
   text; wake while executing opens a fresh window — `tests/audio/test_turn_taking.py` (8
   fake-clock rows + 3 loop wiring rows, all green in both gate runs).
2. **Wake false-accept interaction:** the residual risk is DOCUMENTED, not silent — §3.1
   carries the 7 false-accepts/hour @ 0.99 calibration figure and bounds a false window's media
   speech to "whatever forms one coherent instruction inside that one window".
3. **Click-through vs drag:** mask geometry is asserted non-empty and smaller than the widget
   rect, and the pass-through/mask model replaced the old flag test
   (`tests/ui/test_mascot_position.py`).
4. **Position restore clamp:** restore at (50000, 50000) lands on the nearest visible screen —
   tested explicitly, not just the round-trip.
5. **Quit cleanliness:** the process-census test passed wherever a voice exists; the
   clean-checkout behavior is the round-1 fix (§3 below).

## 3. New findings

- **F1 — HIGH (found this round, FIXED in round 1): the quit census failed in a clean
  checkout.** Cleanroom round 0 (fresh clone at `864e462`, no `voices/` — model binaries stay
  out of git): `test_quit_via_tray_action_leaves_no_processes` FAILED — the composition root
  degrades to FakeTTS ("no piper voice … TTS stays silent"), no worker spawns, and the test's
  census assertion ("the app spawned no piper worker to census — TTS build changed?") hard-fails
  — contradicting its own docstring ("Voiceless machines spawn nothing — the census still guards
  the full quit path"). Fix (test-only, +14/−2): mirror the app's own voice decision
  (`default_voice_model(Settings(activation="hotkey").tts_voice)`) and skip with a documented
  reason when there is no voice — the same dev-gated tier as the wake-model/SP2 fixture skips
  (`tests/audio/test_activation.py:289`, `tests/stt/test_fasterwhisper.py:99`). The census keeps
  full strength wherever a voice exists (dev tree, CI with voices, and the frozen bundle ships
  one). Verified: dev-tree suite still 559 passed / 3 skipped (the census runs, voice present);
  cleanroom round 2 → the test skips cleanly and all three gates pass (§ CLEANROOM-VERIFY-m6).
  Dev-gated skip is honest here: a voiceless checkout has no worker to census, and the quit path
  itself stays covered by the headless quit tests.
- **No other new findings.** The egress census, secrets handling, and scrubber coverage are
  unchanged from the M5 audit (§5); the M6 delta introduced no network egress, no new
  subprocess, no new persistence beyond the two documented config ints.

## 4. Carry-over findings (re-assessed against the M6 delta)

Unchanged in class and severity; none is new to this round, none worsened by M6:

- **S3 (M):** spoken secrets / verbatim instructions in plaintext JSONL logs. The M6 delta added
  no verbatim-user-text log call site (the only `+logger` line carrying user text is the
  pre-existing "asleep; dropping instruction %r", moved, not new). Still open by design pending
  the structured-logging rework.
- **S4 (M):** LLM-created monitors fetch LLM-chosen URLs unconfirmed — untouched by M6.
- **S5 (M):** raw exception text into observations/logs — untouched by M6.
- **P1 (M):** automatic vision egress + crash-persistent temp screenshots — untouched by M6.
- **A1 (M):** model-authored rationale spoken/logged — M6 added prompt guidance (live-app rule)
  but the enforcement gap remains; the new window-killer L3 gate narrows one concrete incident
  class out of it.
- **S6 (was M) — FIXED:** `179bf3b` (tray Stop tasks kills mid-speech TTS) + regression in
  `tests/test_release_fixes.py:91`; re-verified `app.py:875` this round.
- **L9r (L) — CLOSED:** every prior round read the spec only; this round built the bundle and
  inspected the on-disk layout (§1) — both exes and all datas present as the spec promises.
- **L1/L2/L3/L5/L6/L7/L8r (L):** open, untouched by M6 (L5's 63 MB history blob and the
  `.gitignore:16` dead entry re-confirmed by read this round).

## 5. Privacy re-check (release-checklist §5, run this round)

- `tests/core/test_logging.py` + `tests/core/test_privacy_audit.py` green inside both full-suite
  runs (559 passed) — the machine-enforced `test_session_log_is_clean` included.
- New log call sites in the delta (grep over `git diff c6dc40d..HEAD -- src/`): mascot-persist
  OSError warning, chunk-tap exception, loop shutdown/barge-in/half-duplex notices — none
  carries secrets or raw user text.
- Egress census unchanged: `httpx`/`openai` only in `llm/{openai_compat,ollama,resilience}`,
  `httpx`/`feedparser` only in `tasks/monitors.py`; no telemetry/crash/update code; no
  `shell=True`/`os.system` anywhere in `src/`.
- Secret scans over the tree: only test fakes and a plan-doc quote — no real key material.

## 6. Checks run during THIS round (exact commands and outputs)

1. `uv run pytest -q && uv run ruff check . && uv run mypy` (tree, pre-build) → exit 0:
   **559 passed, 3 skipped, 17 deselected in 142.29s** · `All checks passed!` · `Success: no
   issues found in 43 source files`.
2. `uv run python installer/fetch_bundle_inputs.py && uv run pyinstaller installer/marshmellow.spec --noconfirm` → **exit 0** (§1).
3. `dist/Marshmellow/Marshmellow.exe --smoke` → **`smoke ok`, exit 0**.
4. `uv run pytest -m scenario` → exit 0: **45 passed, 8 skipped, 526 deselected in 28.68s**; the
   8 skips are the live-use guard itself (`-rs`: "user active (last input 0s ago) — real-app
   scenarios deferred") — deferred, not failed; the 10 scripted fixture scenarios passed.
5. Bundle inspection: `ls`/`du` over `dist/Marshmellow{,_internal}` (§1); `git check-ignore -v`
   (§1).
6. Delta review: `git diff c6dc40d..HEAD` per file (§2); `turn_taking.py` read in full.
7. Greps: new log sites, egress imports, `shell=True|os.system`, secret patterns, `git
   check-ignore` (§5).
8. Cleanroom rounds 0 + 2 (the F1 discovery and its fix verification) — full account in
   [CLEANROOM-VERIFY-m6.md](CLEANROOM-VERIFY-m6.md).
9. Post-fix gates: `uv run pytest -q` → **559 passed, 3 skipped, 17 deselected in 244.04s**
   (exit 0); `uv run ruff check .` → exit 0; `uv run mypy` → exit 0 (43 files).

## Verification notes

- **Not run — startup/memory bench (release-checklist §4):** the user was ACTIVE at run time
  (input idle 0 s; the same `GetLastInputInfo` guard the M6 live-use fix installed for real-app
  tests), and the bench boots the real GUI (mascot + tray on the user's desktop). Deferred with
  this reason; the M5 numbers remain the recorded baseline. **Not run — dogfood day (§6):**
  user-physical by design, recorded in [pending-user-verifications.md](pending-user-verifications.md).
- `--smoke` proves the frozen imports, not the packaged-model boot (release-checklist §3 note);
  the datas inspection in §1 is the structural check for the packaged-model paths. The visual
  identity (tray/exe icon look, mascot art approval) is user-physical (M6 Task 6 checklist).
- Read-only pass except the F1 test fix; this report and its sibling are the only files written
  to the tree besides that fix.
