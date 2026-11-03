# Marshmellow — Development Workflow: Milestones & Testing

**Status:** v1 draft for review · Last updated: 2026-11-04
Durations are *effort sizes* (S ≈ 1 session, M ≈ 2–4, L ≈ 1–2 weeks of sessions), not dates — detailed product
requirements arrive in later messages and may reshape M2+. Each milestone gets its own **task-level implementation
plan** (writing-plans format) authored just-in-time when the milestone starts.

## Milestone ladder

### M0 — Research spikes (size: M) · *no product code*
**Goal:** replace every delegated sub-decision with a measured number on *this machine*.
All outputs are reports in `docs/plan/spikes/`; each names a decision it resolves.

| Spike | Question | Method | Resolves |
|---|---|---|---|
| SP1 | What % of real apps expose usable UIA trees? | Script walks UIA trees over a corpus: Chrome, Edge, Explorer, Notepad, Calculator, Settings, Spotify, Teams, a game | perception fallback thresholds (01 §3.6) |
| SP2 | Which STT config meets latency + accuracy bar? | faster-whisper base/small/distil × int8/float16: RTF + WER on 20 fixed phrases incl. proper nouns | STT default (01 §3.2) |
| SP3 | Wake-word engine + false-wake rate? | openWakeWord vs Porcupine over 1 h ambient audio; phrase feasibility ("hey marshmellow" custom training) | wake engine (01 §3.1, OQ-1) |
| SP4 | TTS default: quality/latency/barge-in? | piper voices vs alternatives; first-audio latency; interruption feel | TTS default (01 §3.9) |
| SP5 | Cloud provider round-trip: vision + JSON-schema + latency? | One OpenAI-compat call each: GLM/Z.AI + OpenAI + one local Ollama vision model | provider defaults (01 §3.4, OQ-2) |
| SP6 | What is this environment's actual max concurrent subagents? | Batch-spawn N trivial agents in one message, increase N until queueing/rejection observed | **subagent cap recorded in 03-workflow.md** |
| SP7 | Adopt-or-build final pass | Deep-dive licenses/quality of pipecat, agent-s patterns, OS-World scenarios, uiautomation/pywinauto licenses | 04-tooling.md final table |

**DoD:** all 7 reports exist with numbers; every OQ marked "M0" in [05-open-questions.md](05-open-questions.md) is closed;
03-workflow cap corrected; no `src/` code written.

### M1 — Walking skeleton (size: M)
**Goal:** the app exists, runs from one command, and moves one instruction through fake implementations.
**Scope:** repo scaffold (uv, ruff, pytest, CI-able), `core/contracts.py` + EventBus + config, fake STT/TTS/LLM/planner,
tray icon + static mascot states, `packs/windows.py` open-app path, console fixture runner (`simulate-spoken-instruction`).
**E2E (deterministic):** instruction "open notepad" fed via console fixture → DE pack opens Notepad → `SpeakRequest` captured by FakeTTS.
**DoD:** `uv run marshmellow` starts tray+mascot; E2E test green headless; contracts + fakes merged; all quality hooks active.

### M2 — Voice loop end-to-end (size: M)
**Goal:** real audio in, cloud LLM in the middle, voice out — hands-free for trivial tasks.
**Scope:** AudioEngine (VAD + activation per D2 setting incl. autostart toggle stub), faster-whisper STT, cloud planner,
piper TTS, mascot live states, barge-in + kill switch, echo guard.
**E2E (manual + assisted):** say "open Notepad and type hello world" hands-free.
**Latency budget (targets, adjusted by M0 numbers):** wake→listening-ack ≤ 500 ms · end-speech→transcript ≤ 1.5 s ·
per agent step ≤ 3 s · TTS first audio ≤ 500 ms · simple op total ≤ 15 s.
**DoD:** budget table measured and documented; control intents (stop/mute) work; a 10-utterance audio-fixture suite passes.

### M3 — Generic UI interaction (size: L)
**Goal:** the core promise — operate *any* app via the generic protocol, safely.
**Scope:** UIAReader + registry, vision fallback + SoM, ActionLayer (UIA-first + SendInput), planner tool loop with
verify, safety gate L0–L3, UI test harness (02-Testing below).
**E2E (assisted, then manual):** the reference scenario — "open the browser, check if channel X has new videos, tell me"
— completed hands-free with a spoken, *correct* answer.
**DoD:** scenario suite covers 8+ apps incl. one vision-fallback case; L3 confirmation demonstrable; element-ref
re-validation proven against window-move/focus-change races.

### M4 — Background tasks (size: M)
**Goal:** waiting-and-checking classes work and are managed from the dashboard.
**Scope:** TaskManager classes, monitor framework (url_diff/api_field/file_hash/rss), scheduler, voice reporting,
tasks dashboard UI, **foreground lease** protocol, task persistence across restart.
**E2E:** "watch this channel and tell me when they upload" → later, unprompted spoken report; task visible/pausable in UI.
**DoD:** background tasks never synthesize input without a lease (test-enforced); monitor survives app restart; false-positive report rate acceptable on a 24 h soak.

### M5 — Polish & harden (size: M)
**Goal:** daily-drivable, resilient, installable.
**Scope:** local LLM adapter tuned (Ollama vision model benchmark), error recovery & retry UX, installer
(pyinstaller/MSIX — OQ-9), autostart (D3) real implementation, onboarding (mic permission, wake-word setup),
privacy audit (log scrubbing), docs.
**DoD:** user dogfoods a full day hands-free with zero crashes; cold-start ≤ 10 s; memory ≤ 400 MB idle; installer verified on a clean Windows account.

## Testing strategy (applies from M1 onward)

**Principle: no human speech required to run the suite.** Every layer is fake-able at its contract; real audio is exercised by a small fixture suite, and by the user.

| Harness piece | Replaces | Mechanism |
|---|---|---|
| `FakeSTT` | microphone | text fixture → `Transcript` |
| `AudioFixturePlayer` | microphone (real pipeline) | WAV files → `SpeechSegment`s through real VAD/STT path |
| `FakeTTS` | speakers | captures `SpeakRequest`s for assertions |
| `FakeLLM` | cloud/local model | scripted `ActionRequest` traces + golden JSON responses; provider adapters get recorded-response (VCR-style) contract tests |
| Scripted perception | live screen | canned `PerceptionSnapshot`s per fixture app state |
| UI harness | manual clicking | drives **fixture apps** (Notepad, Calculator, a local test page in Chrome) via the real ActionLayer; assertions read **UIA tree state**, not screenshots |
| Scenario runner | human E2E | YAML: instruction + mock context + expected action trace + expected spoken result; `pytest -m scenario` headless |

**Test tiers:** unit (per module vs fakes) → scenario (voice loop + UI protocol headless) → fixture-audio (real STT/VAD) →
manual dogfood checklist (weekly, from M2). CI: tiers 1–2 on every push; 3 nightly.
**Determinism rules:** no wall-clock waits in tests (injected clock); no live LLM calls in CI; screenshots only for active task & purged (01 §3.15); each scenario pins app state before acting.

## Verification culture
- Every milestone ends with its DoD *demonstrated* (test run output or recorded manual session), not asserted.
- The verification-before-completion discipline applies: evidence before "done" claims.
