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

**Task 5 evidence (2026-11-07, dev desktop):** the D8 self-trained wake model exists and runs the REAL path —
`AudioEngine chunk_tap → ChunkQueue → WakeWordWatcher` — headless-gated so tests inject models explicitly.
`assets/wake/` ships `hey-marshmellow.onnx` (trained: 1000 piper-synthesized positives, 3 en_US voices,
lookalike+generic negatives, SP3 pink-noise/silence background, openwakeword `auto_train` 50000 steps; the
tool imports no piper — synthesis rides the existing `piper_worker` framing protocol) plus
`calibration.json` (threshold **0.99** — SP3's 0.5 has OBSERVED lookalike false accepts, so the histogram
said otherwise; positives 0.9885-0.9996, background max 0.0001, real-human-speech probe 0.0001; the
single-phoneme lookalike limitation is recorded, not hidden) and the license check for the two
feature-extractor ONNX deps (Apache-2.0 — SHIPPABLE per D8, in `assets/wake/README.md`). Gate: 473 passed,
3 skipped, 17 deselected · ruff clean · mypy strict clean (43 files). **Ambient soak**
(`tools/soak_wake_m5.py --hours 1` → [spikes/data/m5_wake_soak.json](spikes/data/m5_wake_soak.json)):
**7 false accepts/hour at threshold 0.99** — the wake word is NOT trusted hands-free yet; the hotkey
remains the trusted activation (D2) and the hands-free call goes to the Task 7 checklist (per
[pending-user-verifications.md](pending-user-verifications.md)). The full M5 DoD evidence block lands with
Task 7.

**Task 7 evidence (2026-11-07, agent-run on the dev desktop; OQ-9 closed):**

- **Installer (OQ-9 → pyinstaller one-dir, two exes — decision recorded in
  [05-open-questions.md](05-open-questions.md)):** `uv run python
  installer/fetch_bundle_inputs.py` (voice present; ct2 `Systran/faster-whisper-base`
  fetched into the untracked `models/` from the warm HF cache) → `uv run pyinstaller
  installer/marshmellow.spec --noconfirm` → `dist/Marshmellow/` (714 MB one-dir) with
  `Marshmellow.exe` + sibling `piper_worker.exe` (SP7 GPL boundary survives freezing —
  piper imports only in the worker process; the GUI Analysis even excludes the worker
  module). Packaged datas verified on disk: `_internal/wake/hey-marshmellow.onnx`
  (+ Apache-2.0 feature extractors — D8 ship call RESOLVED, wake path SHIPS),
  `_internal/voices/en_US-lessac-medium.onnx(.json)`,
  `_internal/models/faster-whisper-base/model.bin`. **Headless install verification:**
  `dist\Marshmellow\Marshmellow.exe --smoke` → `smoke ok`, exit 0.
- **Frozen-model resolution proven in the REAL GUI boot** (the bench's first run):
  session JSONL shows `loading STT model …\dist\Marshmellow\_internal\models\faster-whisper-base`
  and `TTS voice: en_US-lessac-medium (…\_internal\voices\…)` — no hub download, no
  `MARSHMELLOW_VOICE_DIR`. Two package-data gaps were caught exactly here and fixed in
  the spec (`collect_data_files`): faster_whisper's silero-VAD onnx (boot crashed without
  it) and openwakeword's `resources/models` (resolved relative to the package, unlike
  our copies in `_internal/wake`).
- **Bench (`tools/bench_startup_m5.py --runs 3`, real clock, 60 s settle, RSS = app +
  children sum → [spikes/data/m5_startup.json](spikes/data/m5_startup.json)):**

  | | pre-tuning | post-tuning |
  |---|---|---|
  | cold start (median) | 2.36 s | **2.06 s** (target ≤ 10 s → PASS) |
  | idle RSS sum (max of 3) | **536.8 MB — MISS** | **297.5 MB** (target ≤ 400 MB → PASS) |

  The MISS is recorded with the levers, not silently tuned: lazy STT load
  (`FasterWhisperSTT(lazy=True)` — ct2 session loads on first transcribe, measured
  ~0.6 s once) + deferred voice load (piper_worker loads the voice on the first stdin
  line) → −44 % idle RSS, both in [spikes/data/m5_startup.json](spikes/data/m5_startup.json)
  under `tuning`, pinned by `tests/stt/test_fasterwhisper.py::test_lazy_stt_defers_model_load_to_first_transcribe`
  and `tests/tts/test_piper.py::test_worker_defers_voice_load_to_first_utterance`.
- **Dogfood tooling:** `uv run python tools/dogfood_report.py --date YYYY-MM-DD` turns
  the "zero crashes" DoD into a grep (exit 1 on any ERROR-with-traceback). Proven on
  the existing M2–M5-era session logs: 2026-11-04 → 0 crashes; 2026-11-06 → 0 crashes,
  5 instructions, 4 planner turns, 4 safety decisions. Spoken results and monitor
  triggers are greppable from M5 on (the new `spoken (prio, type):` session-log line).
- **Gates:** 493 passed, 3 skipped, 17 deselected · `ruff check .` clean · `mypy` clean
  (43 files). Commits `31b2c8e` (bundle), `bdb6fc7` (bench+levers+report), then the
  `docs:` evidence commit.
- **Soak verdicts carried:** monitors 5.75 h partial-clean (24 h verdict still pending
  a relaunch, M4 Task 7 evidence above); wake ambient soak 7 false accepts/hour at
  threshold 0.99 — **the hands-free wake call remains user-physical**, Step 6 of the
  Task 7 checklist (clean-account install + day dogfood + false-accept feel) is THE
  milestone's remaining work, recorded verbatim in
  [pending-user-verifications.md](pending-user-verifications.md) together with the M5
  Task 4 onboarding/settings checklist (one session discharges both).

### M6 — Release feedback (size: S)
**Goal:** fix the four post-v0.1.0 daily-use flaws — mascot art (OQ-3), placement/persistence, turn-taking (never hears itself or media), app identity + full tray control — then rebuild the verified bundle.
**Scope:** parametric mascot art + export pipeline; receiving-window turn-taking; hard half-duplex ingest gate; per-state identity icons; docs reconciliation.
**DoD:** the feedback brief's four flaws fixed and gated offscreen; bundle rebuilt + verified (Task 7); the user-physical feel checklist recorded.

**Task 6 evidence (2026-11-08, docs sweep):** §3.1/§3.9/§3.10/§3.11 of
[01-architecture.md](01-architecture.md) re-read against the source section by section
and rewritten to the implemented model — interface sketches now match the real
`AudioEngine` (no `set_mode`, no `MuteChanged`; `pcm: bytes`), `PiperSpeaker`
(`start/say/stop/quit`, priority queue; the "URLs/IDs summarized" speakability
rewriter never existed — brevity lives in the planner prompt), the tray
(`MascotIconProvider` per-state stills, Mute/Unmute/Open Marshmellow/Stop tasks/Quit,
engine-decided semantics, no left-click toggle) and the real TasksDashboard (no
transcript pane — transcripts live in the §3.15 JSONL logs); mascot §3.10 gained the
8th state `attention` and lost the nonexistent `MascotMenuAction`/right-click menu and
auto-dim claims. OQ-3 **closed** with the decision recorded; the M6 user-physical feel
checklist (art approval, drag/restart feel, 3 s close feel, media-guard feel, tray look
+ stop-from-tray) is recorded in
[pending-user-verifications.md](pending-user-verifications.md). Gates at commit:
`uv run pytest -q` (551 passed, 3 skipped, 17 deselected) / `ruff check .` / `mypy`
(43 files) green — docs-only change. `uv run pytest -m scenario`: 52 passed /
**1 environmental failure** — `test_fixture_scenario_browser` cannot currently pass
on this machine: Brave's renderer accessibility never exposes the page tree (probed
with the app's own walk: focused fresh walks over 12 s → 139 chrome-only elements,
the pin Edit never appears; "Energy Saver is on" per the chrome tree), while the
fixture file is unchanged since M3 (`4bcb287`), the failure sits in the fixture
launch/pin phase before any Marshmellow code runs, and the same suite was green at
M6-T4 (`563dfbf`). The clock fixture flaked in one run and passed in re-runs (real-app
launch timing). Both are `@pytest.mark.manual` real-desktop tests — the default suite
deselects them; re-run in Task 7's verification pass.
**Task 7 evidence (2026-11-08, bundle rebuilt + verified):** the four feedback flaws are fixed
and gated (T1–T6 above); the bundle was rebuilt from HEAD `864e462` and verified in-workflow
(0 product-code fixes needed; 1 clean-checkout test fix, round 1 of ≤ 2):

- **Gates** (un-piped `&&` chain, pre-build): `uv run pytest -q` → **559 passed, 3 skipped,
  17 deselected in 142.29s** · `ruff check .` → clean · `mypy` → clean (43 files) — exit 0.
  Post-fix re-run: pytest exit 0 again (559/3/17, 244.04s), ruff/mypy exit 0.
- **Build** (release-checklist skill): `uv run python installer/fetch_bundle_inputs.py` → all
  inputs present; `uv run pyinstaller installer/marshmellow.spec --noconfirm` → **exit 0**
  (6 benign warnings: optional hidden imports + the ctypes-declared UIA COM DLLs that load from
  the Windows system dir). One-dir two-exe layout: `dist/Marshmellow/Marshmellow.exe`
  (28,055,535 B) + `piper_worker.exe` (5,347,608 B) in the dist root; `_internal/` carries
  `mascot/` (all 10 identity files incl. the exe ico), `wake/` (D8 model + extractors),
  `voices/` (lessac pair), `models/faster-whisper-base/` (ct2), silero VAD onnx, and
  `piper/espeak-ng-data` (19 MB) — 718 MB total; `dist/`/`build/` gitignored.
- **Smoke:** `dist\Marshmellow\Marshmellow.exe --smoke` → **`smoke ok`, exit 0**.
- **Verification pass:** [RELEASE-AUDIT-m6.md](RELEASE-AUDIT-m6.md) — fresh adversarial audit of
  the M6 delta + bundle: **0 new high findings**; S6 closed (fix re-verified); L9r closed
  (bundle layout inspected — every prior round was spec-read-only); carry-over mediums
  (S3/S4/S5/P1/A1) unchanged; privacy re-check green (no new verbatim-log sites, egress census
  unchanged, secret scans clean). One HIGH found and fixed within the bounded budget: the quit
  census failed in a clean checkout (no `voices/` → no worker to census) → skip with a
  documented reason where no voice exists (test-only, same dev-gated tier as the wake-model/SP2
  skips; census still runs where a voice exists). [CLEANROOM-VERIFY-m6.md](CLEANROOM-VERIFY-m6.md)
  — round 0 FAIL (that census) → fix → **round 2 PASS: clone at `864e462` + the fix diff,
  `uv sync` 0, pytest 556 passed / 6 skipped / 17 deselected (exit 0), ruff 0, mypy 0**, clone
  removed after.
- **Round complete (2026-11-08):** the verification reports are committed; the final reviewer fix
  (`41a94ce` — window-timeout drains to flush instead of eating the sentence tail) is covered by
  the cleanroom's fix-diff scope; final gate at HEAD: **562 passed / 3 skipped / 17 deselected,
  ruff clean, mypy clean (43 files)**; `origin/main` synced through `41a94ce`. **Publish and
  version-bump deliberately reserved** — the rebuilt bundle sits in `dist/` (smoke ok) awaiting
  the user's v0.1.1 call; the M6 feel checklist is live in `pending-user-verifications.md`.
- **Scenario re-run** (Task 6's ask): `uv run pytest -m scenario` → exit 0: **45 passed,
  8 skipped** — the 8 are the live-use guard itself (`-rs`: "user active (last input 0s ago) —
  real-app scenarios deferred"); the 10 scripted fixture scenarios passed. The two
  `@pytest.mark.manual` real-desktop tests from the Task 6 note were NOT re-run — the user was
  active (input idle 0 s), the same guard defers them; the browser one remains environmentally
  broken on this machine (Task 6 probe).
- **Not run — startup/memory bench** (release-checklist §4): the user was active at run time
  and the bench boots the real GUI on their desktop (mascot + tray) — deferred with the
  live-use guard as the reason; M5's numbers stay the recorded baseline. **Not run — dogfood
  day** (§6): user-physical by design.
- **STOP — no publish, no version bump** (§2 of the feedback brief): the candidate sits in
  `dist/` (untracked); the publish/version call is the user's. The user-physical feel checklist
  (art approval, drag/restart feel, 3 s close feel, media-guard feel, tray look +
  stop-from-tray) remains open in [pending-user-verifications.md](pending-user-verifications.md).

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
