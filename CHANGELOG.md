# Changelog

All notable changes to Marshmellow are documented here. This project adheres to
[Conventional Commits](https://www.conventionalcommits.org/) — see the per-commit
record with `git log --oneline`. The full build record lives in
[`docs/plan/`](docs/plan/) (architecture, milestones, task plans, evidence).

## Unreleased — M6 release-feedback round (2026-11-07 → 2026-11-08)

Post-v0.1.0 daily-use feedback: the four flaws from the feedback brief fixed on
`main` — no publish and no version bump (bundle rebuild is M6 Task 7; the
publish/version call stays with the user).

### M6 — Release feedback (2026-11-07 → 2026-11-08, commits `bb7f457…`)

- **Added:** parametric mascot art pipeline — `ui/mascot_art.paint(state, phase, size)`
  (deterministic, 8 states) + `tools/export_mascot_assets.py` → `assets/mascot/`
  stills; closes OQ-3 (`bb7f457`); mascot placement — bottom-right default,
  body-masked input (surroundings always pass through), drag with live clamp,
  persisted `[ui] mascot_x/mascot_y`, restore clamped into a visible screen
  (`ecd921d`); receiving-window turn-taking — collected speech becomes ONE
  instruction (3 s no-voice close), abort intents discard a pending window
  (`c1bcc18`), close tick re-arms from the window's remaining time (`1237620`);
  app identity — per-state tray/window/exe icons from the mascot stills + quit
  census (`f368dee`).
- **Changed:** the echo guard is a hard half-duplex ingest gate (`IngestMode`
  DROP/WAKE_ONLY/FULL) — while TTS plays nothing is heard (measured 0-ingestion
  fixture), media audio without a receiving window is structurally unable to
  become input, and voice barge-in is REMOVED (own-voice safety without AEC);
  the hotkey is the only barge-in (`563dfbf`).
- **Docs:** architecture reconciliation — §3.1/§3.9/§3.10/§3.11 re-read against
  the source and rewritten to the implemented model; OQ-3 closed; M6
  user-physical feel checklist (`docs(M6-T6)`).

## 0.1.0 — 2026-11-07

First release: a hands-free, voice-controlled Windows desktop agent — wake word /
hotkey → speech-to-text → LLM planner → UI-automated actions (vision fallback) →
spoken result, with a risk-gated action layer, background monitors, and a
tray+mascot UI. Private preview: all automated gates green; the user-physical
verification checklist remains open (`docs/plan/pending-user-verifications.md`).

### M0 — Research spikes (2026-11-04, no product code)

- **Spike:** UIA coverage survey (SP1: 5/5 corpus apps usable) + concurrency cap
  (SP6) (`cda10cb`); wake-word bench (SP3, openWakeWord ~4–6 ms/chunk) (`8df4939`);
  TTS resident bench (SP4, piper confirmed; SP2 root-caused to PyAV) (`28e25bd`);
  STT bench (SP2, base/int8 default, p95 1.21 s) (`f1a1e5e`); license verification
  folded into tooling — Porcupine AVOID, piper GPL subprocess-only (SP7) (`c409aef`).
- **Docs:** planning deliverables — architecture, milestones, workflow, tooling,
  open questions (`203e3a7`).

### M1 — Walking skeleton (2026-11-04, commits `2d00d93…ba5403e`)

- **Added:** repo scaffold (uv, pytest, ruff, package skeleton) (`2d00d93`);
  core contracts v1 (`0fd12fe`); EventBus with error isolation and threadsafe emit
  (`0304824`); typed config with TOML round-trip (`38db366`); JSONL structured
  logging with secret scrubbing (`4773d31`); deterministic fakes for
  stt/tts/llm/planner (`3e29c56`); Windows DE pack `open_app` with the
  new-windows-only rule (`db89ded`); mascot overlay states + tray, offscreen-tested
  (`e902e25`); composition root, simulator CLI, headless E2E — the walking
  skeleton (`ba5403e`).
- **Verified:** real E2E — `python -m marshmellow.tools.simulate "open notepad"`
  opened a real Notepad window through the full loop (`e8f2de9` evidence).

### M2 — Voice loop end-to-end (2026-11-05, commits `f112f50…a10db87`)

- **Added:** faster-whisper STT (numpy PCM path, SP2 config) (`903fcfc`); piper TTS
  as a resident streaming subprocess with SpeakingStarted/Stopped events
  (`50b55ec`); OpenAI-compatible provider with BYOK via Credential Manager and a
  secret-registered log scrubber (`fed8980`); audio capture with bundled silero VAD
  segmentation + FixtureMic (`f09d703`); activation — LL-hook hotkey and wake-word
  watcher (`5a194c7`); control-intent routing without the LLM (`44f7b43`); cloud
  LLM orchestrator v1 (shell/wait/ask/finish) (`ff42806`); voice loop with
  barge-in, kill switch, echo guard (`32c54db`); live composition — real STT/TTS
  wiring, config discovery, GUI entry (`8edfad9`).
- **Fixed:** planner driven on one resident loop (pooled LLM client is loop-bound)
  (`11ede6c`); review defects — loop teardown, kill-switch refusal, piper barge-in,
  mascot thread safety (`f112f50`); piper streamed over a framed raw-PCM worker
  protocol (piper1-gpl ≥ 1.8 cannot stream WAV on stdout) (`1c7fbab`); multi-line
  spoken text collapsed to one worker protocol line (`af67207`).
- **Measured:** latency budget harness + repeatable STT sweeps
  (`9cb16a5`, `399f19c`, `a10db87`) — wake→ack 0.3 ms, transcript median 1.80 s
  (near miss vs 1.5 s, close-dominated, levers recorded), TTS first audio 0.065 s
  warm; 12-utterance fixture suite WER ≤ 0.35 (`a68e92e` evidence).

### M3 — Generic UI interaction (2026-11-06, commits `0ea6b9e…9f19116`)

- **Added:** UIAReader with settle-poll cache + element registry (`8959ea9`); SoM
  vision fallback — VisionPerceiver, PerceptionHint, image parts (`f397fb6`);
  ActionLayer with ref re-validation + fixture-app harness (`db22a77`); safety gate
  L0–L3 with voice + tray confirmation, fail closed (`6bd6cc8`); planner tool loop
  (perceive/act/verify) + composition wiring (`9b3b7b0`); scripted scenario runner
  + reference/L3/vision scenarios (`4bcb287`); fixture-app scenario suite (8 real
  apps) + add-scenario skill (`9f19116`).
- **Fixed:** window-scoped ref re-validation (focus-change race) + unmasked
  real-foreground DoD test (`c70c06c`); review findings — blocking steps off the
  planner loop, GUI-thread confirm marshal, no password values in listings
  (`cd9f606`).
- **Verified:** L3 confirm-yes/no/ambiguous teeth (`tests/safety/`); window-move
  and focus-change races re-validated; per-step engine latency ≤ 1.2 ms; min-of-3
  RTF timing (`3a4f8fa`); 318 + 48 scenario tests green (`490102d` evidence).

### M4 — Background tasks (2026-11-07, commits `08da7d3…8d7be04`)

- **Added:** monitor checks (url_diff/api_field/file_hash/rss) + jittered
  scheduler (`08da7d3`); TaskManager task classes — long-lived loops, states,
  control API (`cfbeba8`); foreground lease — consented screen access for
  background tasks (`df9ef8f`); `create_task` path — LLM-created monitors with
  background voice reports (`48e2134`); task persistence across restart (baseline
  anti-re-report) (`087a601`); tasks dashboard (main window, tray open_ui)
  (`31ce198`); M4 soak harness (`8d7be04`).
- **Fixed:** D6 lease bypass on the LLM act path + loop freeze in lease release
  (`d263eeb`); `register()` surfaces monitor-check failures (`6d0485a`); tz-aware
  `create_task` schedules normalized — scheduled loops no longer die silently
  (`441622a`); review fixes — terminal scheduled rows dropped, persist race
  closed, graceful stop keeps rows, restored-paused parks first (`142e625`).
- **Docs:** pause-safety by design — ledger protocol, restart protocol, hardened
  watchdog (`fe8c273`); watchdog removed by user — resumption manual
  (`fd2ca78`).
- **Soak:** 24 h run terminated externally at 5.75 h — clean partial record
  (0 check errors, 0 false positives); verdict pending relaunch (`898247e`).

### M5 — Polish & harden (2026-11-07, commits `ee64c52…d718b30`)

- **Added:** Ollama local provider + D5 vision fallback wired in composition
  (`ee64c52`); provider retry + cloud-to-local fallback chain with a spoken
  unavailable error (`1d9ad4a`); autostart via Task Scheduler (D3, OQ-6) (`7b68a05`)
  → fixed to the HKCU Run key (schtasks ONLOGON needs admin) (`7025cc2`); first-run
  onboarding + settings dialog over the packaged-asset resolver (D2/D3/D7)
  (`f32809d`); self-trained hey-marshmellow wake model (D8) — chunk tap, trainer,
  ambient soak (`7e461b5`); privacy audit — URL-token log scrubbing +
  whole-session enforcement §3.15 (`7df88cb`); PyInstaller bundle — frozen piper
  worker exe, packaged models, smoke entry (OQ-9) (`31b2c8e`); startup/memory bench
  + day dogfood report + idle-RSS tuning levers (`bdb6fc7`).
- **Fixed:** dedicated D5 vision provider instance (HTTP pools are loop-bound)
  (`c0a4719`); settings dialog can escalate the §3.13 preset, never lower
  (`40922bc`); all shell launches pass the safety gate, tray mute/unmute/pause as
  recovery channel, atomic task-store writes, MIT LICENSE + README (audit S1+S2)
  (`bd418c9`); URL-credential scrub must stop before a JSON `\"` escape pair
  (`dbbe33b`); faster_whisper/openwakeword package data collected into the bundle
  (`a6519eb`); terminal task rows drop before the state flip (cross-thread
  observer race) (`d718b30`); wake feature extractors committed beside the wake
  model — a clean checkout could not load the wake scorer (openwakeword's wheel
  ships no resources/; the interim cleanroom FAIL is recorded in
  `docs/plan/CLEANROOM-VERIFY-final.md`) (`219d1c6`).
- **Verified:** bundle builds and `Marshmellow.exe --smoke` exits 0; cold start
  2.06 s (≤ 10 s ✅) and idle RSS 297.5 MB (≤ 400 MB ✅) after recorded tuning;
  cleanroom verification PASS — 453 tests from a fresh clone (`7b45aac`);
  release-readiness audit triaged (`7919329`); release-verification gate recorded
  (`c5a6d75`); M5 evidence + user guide, OQ-9 closed (`1f048da`); final cleanroom
  re-verification PASS after the extractor fix — all three gates exit 0 from a
  fresh clone (`219d1c6`).

### Security & privacy hardening (cross-milestone)

- Secret-registered log scrubber from the first cloud provider onward (`fed8980`);
  BYOK keys live only in Windows Credential Manager (`fed8980`, `f32809d`);
  whole-session §3.15 enforcement + URL-token scrubbing (`7df88cb`, `dbbe33b`);
  every shell launch through the safety gate (`bd418c9`); foreground lease
  fail-closed (`df9ef8f`); L3 confirmations fail closed on ambiguity
  (`6bd6cc8`).
