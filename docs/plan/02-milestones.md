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
**DoD evidence (2026-11-04):** 41 tests passed + 1 manual deselected · ruff clean · mypy strict clean (core) ·
headless scenario E2E green (`open notepad` → `[started, step, result]` + ack + result spoken via FakeTTS) ·
**real E2E run**: `python -m marshmellow.tools.simulate "open notepad"` opened a real Notepad window through the
full loop and closed only the window it owned. Commits `2d00d93…ba5403e`. Tray+mascot launch verified offscreen;
`uv run marshmellow` GUI entry lands with the audio loop in M2 (composition root exists: `App(headless=False)`).

### M2 — Voice loop end-to-end (size: M)
**Goal:** real audio in, cloud LLM in the middle, voice out — hands-free for trivial tasks.
**Scope:** AudioEngine (VAD + activation per D2 setting incl. autostart toggle stub), faster-whisper STT, cloud planner,
piper TTS, mascot live states, barge-in + kill switch, echo guard.
**E2E (manual + assisted):** say "open Notepad and type hello world" hands-free.
**Latency budget (targets, adjusted by M0 numbers):** wake→listening-ack ≤ 500 ms · end-speech→transcript ≤ 1.5 s ·
per agent step ≤ 3 s · TTS first audio ≤ 500 ms · simple op total ≤ 15 s.
**DoD:** budget table measured and documented; control intents (stop/mute) work; a 10-utterance audio-fixture suite passes.

**DoD evidence (2026-11-04/05, machine: the dev desktop, CPU-only):** full gate green (`uv run pytest -q`:
142 passed + 1 manual deselected · ruff clean · mypy strict clean) · control intents covered headless
(`tests/test_voice_loop.py`: stop/mute/kill/barge-in/echo-guard) · 12-utterance SP2 fixture suite passes
(WER ≤ 0.35, RTF < 1.0) · live composition real (`uv run python -m marshmellow` boots: mic capture,
base/int8 STT, piper voice resolved, wake degrades to hotkey-only per D8 — session log `%LOCALAPPDATA%/Marshmellow/logs`).

**Latency budget measured** (`tools/latency_m2.py` → [spikes/data/m2_latency.json](spikes/data/m2_latency.json), at HEAD `a10db87`, 2026-11-04):

| Measure | Target | Measured | Verdict |
|---|---|---|---|
| wake→listening-ack | ≤ 0.5 s | **0.3 ms** crossing-frame → listening state (real watcher thread + real VoiceLoop; production adds one 80 ms frame + ~4–5 ms openWakeWord scoring per SP3) | ✅ huge margin |
| end-speech→transcript | ≤ 1.5 s | short command ("open notepad", 1.79 s clip): **1.60 s** = 0.7 s designed VAD close + 0.77 s STT · 12 SP2 fixtures (2.8–5.5 s utterances): median **1.80 s**, sweep medians 1.73 / 1.76 / 1.88 s | ❌ near miss, close-dominated |
| per agent step | ≤ 3 s | engine overhead **~10 ms** per scripted 2-call turn (TaskManager + prompt build + tool dispatch); **cloud RTT not yet measurable — no BYOK key stored** (D7/OQ-2 onboarding pending; first real call doubles as the SP5 probe) | ⏳ engine part trivially passes |
| TTS first audio | ≤ 0.5 s | warm (resident worker, hot model): **0.065 s** short ack (0.063–0.067 across 3) · 0.49 s long sentence · cold (first utterance incl. spawn + model load) 4.2–15.2 s across runs, paid once at startup | ✅ |
| simple op total | ≤ 15 s | component sum for "open notepad": ~0.1 (wake/ack) + ~1.6 (speech→transcript) + ~0.07 (ack audio) + planner (engine ~10 ms + **cloud RTT pending**) + shell action ≈ **2–4 s + one cloud RTT** | ✅ on track, cloud RTT open |

Findings: (1) the transcript budget is **dominated by the designed 0.7 s silence close + base/int8 decode of
silence-padded segments** (STT-only median 0.91 s — inside budget on its own); the levers, in impact order, are
trimming the segment's trailing silence before decode, a shorter close setting, and SP2's distil-with-VAD-trim
accuracy-upgrade path; left as a user decision, not silently tuned. (2) The piper integration was rewritten during
measurement — piper1-gpl ≥ 1.8 cannot stream WAV on stdout (default = ffplay-or-file; `-f -` buffers whole
utterances), so `tts/piper_worker.py` now speaks a framed raw-PCM protocol from its own subprocess (fix `1c7fbab`).
(3) This is an active desktop: STT reads ~2× worse when other work loads the CPU, hence medians over spaced-out
sweeps (`--stt-runs 3`) rather than a single lucky run; the JSON keeps per-sweep medians and the last sweep's rows.

Commits `f112f50…a10db87` (Tasks 1–8 landed in their own `feat:` commits — see git log; Task 9 added:
live composition `8edfad9`, piper streaming fix `1c7fbab`, bench harness `9cb16a5`+`399f19c`+`a10db87`,
evidence `docs:` commit).
**Assisted hands-free E2E is the one remaining manual step** → [pending-user-verifications.md](pending-user-verifications.md).

### M3 — Generic UI interaction (size: L)
**Goal:** the core promise — operate *any* app via the generic protocol, safely.
**Scope:** UIAReader + registry, vision fallback + SoM, ActionLayer (UIA-first + SendInput), planner tool loop with
verify, safety gate L0–L3, UI test harness (02-Testing below).
**E2E (assisted, then manual):** the reference scenario — "open the browser, check if channel X has new videos, tell me"
— completed hands-free with a spoken, *correct* answer.
**DoD:** scenario suite covers 8+ apps incl. one vision-fallback case; L3 confirmation demonstrable; element-ref
re-validation proven against window-move/focus-change races.

**DoD evidence (2026-11-06, machine: the dev desktop):** full gate green
(`uv run pytest -q`: **318 passed, 17 manual deselected** · `uv run pytest -m scenario`: **48 passed,
287 deselected** · `uv run ruff check .` clean · `uv run mypy` strict clean, 29 source files) ·
**scenario suite** = scripted runner (Task 6): `reference-check-channel` — the exact executed trace
`get_elements → act:uia_invoke → get_elements → finish` with the spoken "5 videos this week";
`vision-fallback` — the SoM path on the in-process fake provider, the "12,408 views" value existing
nowhere but the vision reply; `l3-confirm-yes` / `l3-confirm-no` — same fixture/ref/kind, only the
scripted answer differs: the gate asked, "no" refused and never acted — plus the fixture-app suite
(Task 7): **8 real apps** (notepad, calculator, explorer, browser, settings, terminal, clock, paint)
driven through real perception/ActionLayer/SafetyGate with UIA-tree assertions ·
**element-ref re-validation vs the races:** window-move (`test_click_uses_revalidated_bbox_center_after_a_move`,
`test_window_move_race_revalidates_to_new_bbox`) and focus-change
(`test_revalidation_walks_the_elements_own_window_after_focus_change`; real-foreground
`test_focus_change_race_types_into_original_notepad` under the `manual` marker) ·
**L3 gate unit teeth:** confirm-yes executes / confirm-no refuses as an action result /
silence-and-garbage never confirm, fail closed (`tests/safety/`).

**Per-step latency FYI** (`tools/latency_m3.py` → [spikes/data/m3_latency.json](spikes/data/m3_latency.json),
at HEAD `9f19116`, 2026-11-06): the scripted reference scenario through the real stack with a 0-RTT provider —
all four steps ≤ **1.2 ms** engine-side (walk/gate/act/dispatch over the canned page), so the ≤ 3 s per-step
budget is carried entirely by the cloud RTT, **unmeasured: no BYOK key stored on this machine** (verified via
`secrets.load_api_key()`; the first BYOK call doubles as the SP5 probe, OQ-2). The burst risk is slow-app walks:
a REAL `ms-settings:about` `get_elements` (settle-poll = two agreeing walks, 35 elements) measured **1.04 s
median / 1.06 s worst** — within budget with ~2 s headroom — and back-to-back re-reads (~0.9 s each) are
new-tick re-walks, so the `(hwnd, 0.1 s tick)` cache dedupes nothing across steps on slow apps. SP1's corpus
measured Settings walks up to **7.5 s** (a settle-poll would then be ~15 s ≈ 5× budget): if that appears here,
the tuning levers are the tick cache, `get_elements(query)` narrowing, `settle_deadline_s`, and SP1's
query-scoped/lazy walk — a measured decision, not silently tuned. Bench lesson: walking Settings *without* the
pin wait measures the pre-content empty frame agreeing with itself (0.04 s) — the fixture scenarios' `pin`
exists for exactly this (SP1 finding 2).

**Task 8 Step 2 — the assisted/manual E2E checklist — is the remaining user-physical step**
→ [pending-user-verifications.md](pending-user-verifications.md) (BYOK live-cloud run; reference scenario
assisted then hands-free; live L3 confirm no/yes/ambiguous; real vision fallback on a UIA-sparse surface;
live kill switch).

Commits `0ea6b9e…9f19116` (Tasks 1–7 in their own `feat:`/`fix:`/`test:` commits — see git log; Task 8 adds
the bench + this evidence `docs:` commit).

### M4 — Background tasks (size: M)
**Goal:** waiting-and-checking classes work and are managed from the dashboard.
**Scope:** TaskManager classes, monitor framework (url_diff/api_field/file_hash/rss), scheduler, voice reporting,
tasks dashboard UI, **foreground lease** protocol, task persistence across restart.
**E2E:** "watch this channel and tell me when they upload" → later, unprompted spoken report; task visible/pausable in UI.
**DoD:** background tasks never synthesize input without a lease (test-enforced); monitor survives app restart; false-positive report rate acceptable on a 24 h soak.

**M4 evidence:** the monitor framework (all four kinds), jittered scheduler,
TaskManager task classes, foreground lease, `create_task` path, persistence and
dashboard are test-enforced headless — `tests/tasks/` (61 tests: monitors per kind
over httpx `MockTransport`, jitter bounds, one-shot trigger semantics, baseline
anti-re-report across restart, lease denial fail-closed, persist/restore paths,
dashboard view) — full suite **393 passed, 17 deselected** · `ruff check .` clean ·
`mypy` strict clean at `8d7be04` (2026-11-07).

**24 h soak** (`tools/soak_m4.py`, commit `8d7be04`): runs the REAL composition —
`App(headless=True)` → TaskManager → `make_check` → jittered scheduler, real
clock — against a file-backed fixture feed with scheduled intended changes (≥ 3
poll intervals apart, ≥ 10 % unchanged tail); monitors are one-shot, so the
harness re-arms per trigger and seals each re-arm baseline in the ledger (a
change consumed that way is accounted, not missing). Every TaskEvent lands as a
JSONL row `{at, task, changed, ok, summary, error}`; the summary counts triggers,
check errors and **false-positive candidates = fixture triggers whose source hash
was already credited (triggers on unchanged content)**. Smoke proof at soak scale
(`--hours 0.01`, three variants: fixture-only, `--feed <file>`, `--feed
http://127.0.0.1` rss): intended changes == triggers credited, 0 false positives,
verdict PASS.

**Soak verdict: PENDING — first run terminated externally at 5.75 h, partial
record clean.** The 24 h run (`--hours 24 --interval-s 300`, fixture feed,
launched 2026-11-07T00:45:58) was killed by the host machine at ~06:31 (no
harness fault: no traceback, no summary — the process was removed externally
while other sessions actively use the machine; no reboot). Over its 5.75 h it
behaved exactly per the verdict criteria: ~69 polls at 300 s ± 10 %, the single
intended change in that window (v1, 04:11:41) produced **exactly one trigger**
(04:16:12, credited), the re-arm sealed, **0 check errors, 0 false-positive
candidates** (`soak_report.jsonl` + `mutations.jsonl` in the run dir; report
schema `{at, task, changed, ok, summary, error}`). The full 24 h verdict (6
intended changes, false-positive report rate) still requires a complete run:
relaunch `uv run python tools/soak_m4.py --hours 24 --interval-s 300` from a
session that owns the machine window, on a tree at rest — the harness imports
the live tree and isolates all state in its own run dir.

**Task 7 Step 4 — the assisted/manual M4 checklist — is the remaining user-physical
step** → [pending-user-verifications.md](pending-user-verifications.md) (BYOK live
+ "watch this channel" → unprompted spoken report; dashboard pause/resume/delete on
the real screen; live lease consent no→refused / yes→opens; kill switch with a
monitor running; restart persistence with no duplicate report).

Commits `08da7d3…8d7be04` (Tasks 1–7 in their own `feat:`/`fix:`/`test:` commits —
see git log; Task 7 adds the soak harness + this evidence `docs:` commit).

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
