# Marshmellow v0.1.0 — Release Notes

**Date:** 2026-11-07 · **Status:** private preview (v0.1.0) · **Platform:** Windows 10/11

## What is Marshmellow

Marshmellow is a hands-free, voice-controlled Windows desktop agent: say what you
want, watch it happen on screen, hear the result spoken back. It is not a chat
window — an ambient assistant that lives in the tray, wears a marshmallow mascot,
and operates the computer the way a person would, through one generic
perceive → act → verify protocol (Windows UI Automation first, screenshot+vision
fallback) with a risk-gated action layer (L0–L3) and a BYOK cloud/local LLM
planner. Architecture: [01-architecture.md](01-architecture.md); user walkthrough:
[docs/user-guide.md](../user-guide.md); privacy: [docs/privacy.md](../privacy.md).

## What works

Per-milestone evidence from [02-milestones.md](02-milestones.md) (each milestone's
DoD is demonstrated with test-run output on the record, not asserted):

### M0 — research spikes (2026-11-04, no product code)
Seven measured decisions on the dev machine: UIA coverage survey (5/5 corpus apps
usable), STT bench (faster-whisper **base/int8**, p95 1.21 s; small disqualified),
wake-word bench (openWakeWord, ~4–6 ms/chunk, 0 false accepts on ambient), TTS
bench (piper resident subprocess, first audio 0.21–0.55 s), concurrency cap,
license pass (piper GPL → subprocess-only boundary; Porcupine avoided).

### M1 — walking skeleton (2026-11-04; commits `2d00d93…ba5403e`)
The app exists and runs from one command: composition root, EventBus, typed TOML
config, JSONL logging with secret scrubbing, deterministic fakes for
STT/TTS/LLM/planner, tray + mascot states (offscreen-tested), Windows DE pack
`open_app` (new-windows-only rule). Real E2E: `python -m marshmellow.tools.simulate
"open notepad"` opened a real Notepad window through the full loop and closed only
the window it owned. Headless gates green at the time: 41 tests, ruff, mypy strict.

### M2 — voice loop end-to-end (2026-11-05; commits `f112f50…a10db87`)
Real audio in → cloud LLM in the middle → voice out: sounddevice mic capture with
bundled silero VAD segmentation, faster-whisper STT, OpenAI-compatible BYOK
provider (key in Windows Credential Manager, log-scrubbed), piper TTS as a resident
streaming subprocess (framed raw-PCM protocol), wake-word watcher + LL-hook hotkey,
control intents without the LLM (stop/mute), barge-in, kill switch, echo guard,
live composition. **Measured latency budget** (`tools/latency_m2.py` →
[spikes/data/m2_latency.json](spikes/data/m2_latency.json)): wake→ack 0.3 ms ✅ ·
end-speech→transcript median 1.80 s (❌ near miss vs 1.5 s, dominated by the
designed 0.7 s VAD close — recorded with levers, not silently tuned) · TTS first
audio 0.065 s warm ✅ · per-step engine overhead ~10 ms ✅ (cloud RTT unmeasured —
no BYOK key on the dev machine). 12-utterance fixture suite passes (WER ≤ 0.35,
RTF < 1.0); control intents covered headless.

### M3 — generic UI interaction (2026-11-06; commits `0ea6b9e…9f19116`)
The core promise — operate *any* app via the generic protocol, safely:
UIAReader with settle-poll cache + element registry, SoM vision fallback
(`VisionPerceiver`), ActionLayer (UIA-first + SendInput) with **ref re-validation**
proven against window-move and focus-change races (incl. a real-foreground DoD
test), safety gate L0–L3 with spoken confirmation (ambiguous answer = refused,
fail-closed), planner tool loop (perceive/act/verify), scripted scenario runner +
fixture-app suite over **8 real apps** (notepad, calculator, explorer, browser,
settings, terminal, clock, paint). Gates at evidence time: 318 passed + 48
scenario tests · ruff clean · mypy strict clean. Per-step engine latency ≤ 1.2 ms
(`tools/latency_m3.py`); a real `ms-settings:about` walk measured 1.04 s median —
in budget, with SP1's 7.5 s worst-case walk recorded as a burst risk.

### M4 — background tasks (2026-11-07; commits `08da7d3…8d7be04`)
Waiting-and-checking classes, managed from the dashboard: all four monitor kinds
(`url_diff`, `api_field`, `file_hash`, `rss`), jittered scheduler, TaskManager task
classes (scheduled actions + long-lived loops) with a control API, **foreground
lease** protocol (background tasks never touch the screen without spoken consent —
test-enforced fail-closed), `create_task` path (LLM-created monitors with
background voice reports), baseline anti-re-report + **persistence across
restart**, tasks dashboard (main window, tray `open_ui`). 61 tests in
`tests/tasks/`; full suite 393 passed. **24 h soak verdict PENDING** — first run
terminated externally at 5.75 h with a clean partial record: ~69 polls, exactly
one trigger for the one intended change, 0 check errors, 0 false positives
(`tools/soak_m4.py`).

### M5 — polish & harden (2026-11-07; commits `ee64c52…d718b30`)
- **Providers:** Ollama local provider + retry with cloud→local fallback chain
  (spoken unavailable error); dedicated vision-provider instance (fix for
  loop-bound HTTP pools).
- **Self-trained wake word (D8):** `hey-marshmellow.onnx` ships in
  `assets/wake/` (1000 piper-synthesized positives, calibrated **threshold 0.99**
  — positives 0.9885–0.9996, background max 0.0001; license check Apache-2.0
  recorded). Ambient soak measured **7 false accepts/hour at 0.99** — see
  limitations; the hotkey remains the trusted activation.
- **Autostart (D3):** HKCU Run key (Task Scheduler ONLOGON variant needed admin —
  fixed to the per-user Run key).
- **Onboarding + settings:** first-run wizard (mic probe, wake options, key stored
  once and never echoed into TOML, autostart) and a settings dialog over the
  packaged-asset resolver; the §3.13 safety preset can only escalate, never lower.
- **Privacy audit (§3.15):** URL-token log scrubbing with whole-session
  enforcement; every shell launch passes the safety gate; tray mute/unmute/pause
  as recovery channel; atomic task-store writes.
- **Installer (OQ-9):** PyInstaller **one-dir, two-exe** bundle
  (`installer/marshmellow.spec`) — see "Release artifact" below.
- **Bench** (`tools/bench_startup_m5.py` →
  [spikes/data/m5_startup.json](spikes/data/m5_startup.json)): cold start
  median **2.06 s** (target ≤ 10 s ✅); idle RSS **297.5 MB** (target ≤ 400 MB ✅,
  after lazy-STT + deferred-voice tuning from a recorded 536.8 MB miss).
- **Dogfood tooling:** `tools/dogfood_report.py --date YYYY-MM-DD` greps a day's
  session logs for ERROR-with-traceback (exit 1 on any crash); proven on
  2026-11-04 and 2026-11-06 logs → 0 crashes.
- Final gates at M5 evidence: **493 passed, 3 skipped, 17 manual deselected** ·
  `ruff check .` clean · `mypy` strict clean (43 files).

## Install & run (Windows)

### From source (uv, Python 3.12+)

```powershell
uv sync                                  # requires Python 3.12+ and uv
uv run python -m marshmellow             # tray + mascot; first run opens the onboarding wizard
```

### Frozen bundle (no Python needed on the target machine)

```powershell
uv run python installer/fetch_bundle_inputs.py   # voice + ct2 STT model (re-downloadable, untracked)
uv run pyinstaller installer/marshmellow.spec --noconfirm
dist\Marshmellow\Marshmellow.exe                 # tray + mascot GUI
dist\Marshmellow\Marshmellow.exe --smoke         # headless install check: prints "smoke ok", exit 0
```

The bundle ships the faster-whisper STT model, the piper voice, and the
self-trained wake model inside `_internal\` — a clean account installs
offline-capable; the only thing you supply is your own LLM API key.

## Configuration

- **BYOK API key (D7):** `uv run python -m marshmellow.tools.set_key` stores it in
  **Windows Credential Manager** (keyring) — never on disk, and a secret-scrubber
  keeps it out of every log line. The key is read from the credential store; the
  config file never contains it.
- **Config file:** `%LOCALAPPDATA%/Marshmellow/config.toml` (created by the
  onboarding wizard on first run). Main keys (from `core/config.py`):

  | Key | Values (default) | Purpose |
  |---|---|---|
  | `activation` | `wake_word` / `hotkey` / `both` (default `both`) | D2: how you start talking |
  | `autostart` | bool (default `false`) | D3: launch at login (HKCU Run key) |
  | `stt_model` | e.g. `base` (default `base`) | faster-whisper model (M0-SP2) |
  | `tts_voice` | `en_US-lessac-medium` | piper voice |
  | `[providers.llm]` | `kind` = `openai_compat` / `ollama` (+ `base_url`, `model`) | the planner's LLM (BYOK cloud or local) |
  | `[safety]` | `preset` = `relaxed` / `standard` / `strict`; `overrides` keyed by action kind | §3.13: presets may only escalate — the L3 confirmation floor is never lowered |

- Hotkey default **ctrl+shift+m** (hold to talk, release to send); **"stop
  everything"** is the always-available kill switch.

## Release artifact

The M5 packaging task (OQ-9, closed in
[05-open-questions.md](05-open-questions.md)) deliberately ships a **one-dir,
two-exe PyInstaller bundle**, not a single onefile executable:

- `dist/Marshmellow/Marshmellow.exe` — the GUI entry (`--smoke` verifies headless)
- `dist/Marshmellow/piper_worker.exe` — the isolated subprocess that keeps piper's
  GPL code out of the GUI process (SP7 license boundary survives freezing)
- `dist/Marshmellow/_internal/` — the shared model tree (wake, STT, voice)

Onefile was considered and rejected in OQ-9: per-run extraction cost works against
the ≤ 10 s cold-start DoD, and the two exes share one `_internal` tree so a clean
account installs offline-capable. **Consequence: there is no single
`dist/Marshmellow.exe` onefile artifact — the release artifact is the
`dist/Marshmellow/` directory.** MSIX remains the post-v1 packaging upgrade path.

## Known limitations

### Unverified user-physical steps — everything in [pending-user-verifications.md](pending-user-verifications.md)

The release is gated on automated evidence only; the following manual sessions are
**still pending** and are recorded verbatim in
[pending-user-verifications.md](pending-user-verifications.md):

1. **M2 Task 9 — assisted hands-free E2E** ("open Notepad" via hotkey-held session,
   barge-in and repeat after it) — not yet run by a human at the machine.
2. **M3 Task 8 Step 2 — manual E2E checklist** — BYOK live-cloud planner, the
   reference scenario hands-free with a *correct* spoken answer, live L3
   confirm (no/yes/ambiguous), real vision fallback on a UIA-sparse surface,
   live kill switch.
3. **M4 Task 7 Step 4 — manual M4 checklist** — "watch this channel" → unprompted
   spoken report, dashboard pause/resume/delete on the real screen, live lease
   consent (no → refused / yes → opens), kill switch with a monitor running,
   restart persistence with no duplicate report.
4. **M5 Task 7 — installer + milestone DoD** — clean-account install with the
   onboarding wizard, **a full hands-free dogfood day** (`tools/dogfood_report.py`
   → crash count 0), wake false-accept feel, and the D8 ship call (already
   resolved to *ship the wake path* unless re-opened).
5. **M5 Task 4 — onboarding wizard + settings dialog live** (real mic probe,
   Credential Manager, HKCU Run key seams behind the gated offscreen tests).

Until these run, treat v0.1.0 as a **private preview validated headlessly**; the
DoD claims marked "user-physical" above are unverified, not disproven.

### Measured/recorded limitations (evidence on file)

- **Wake word not trusted hands-free yet:** the self-trained model measured
  **7 false accepts/hour** at the calibrated threshold 0.99 over 1 h of ambient
  audio ([spikes/data/m5_wake_soak.json](spikes/data/m5_wake_soak.json)); a spoken
  lookalike ("hey marshmallow") is recorded as a known single-phoneme lookalike
  limitation (`assets/wake/README.md`). Use `activation = "hotkey"` (or the
  default `both` with the hotkey as the trusted path) until the day-dogfood
  observation (M5 Task 7 step 3).
- **Transcript latency near miss:** end-speech→transcript median **1.80 s** vs the
  1.5 s target — dominated by the designed 0.7 s silence close; levers recorded
  in the M2 evidence block, deliberately left as a user decision rather than
  silently tuned.
- **Cloud per-step RTT unmeasured:** no BYOK key was stored on the dev machine, so
  the per-agent-step budget is carried by an unmeasured cloud RTT; the first BYOK
  call doubles as the SP5 provider probe.
- **Slow-app walks:** SP1 measured Settings walks up to 7.5 s on some surfaces —
  if hit, the recorded tuning levers are the tick cache, `get_elements(query)`
  narrowing, `settle_deadline_s`, and query-scoped walks.
- **M4 soak verdict pending:** the 24 h false-positive soak was terminated
  externally at 5.75 h with a clean partial record (0 false positives, 0 check
  errors); the full verdict needs a complete relaunch (`uv run python
  tools/soak_m4.py --hours 24 --interval-s 300`).
- **Mascot art is placeholder** (system-default tray icon render) — OQ-3 is
  user-owned and open.
- **Mypy strictness is scoped** to the core packages (`core`, `perception`,
  `actions`, `testing`, `safety`, `planner`, `tasks`, `llm`); audio/stt/tts/ui
  lean on untyped third-party surfaces — a recorded decision, not an oversight
  (M5 plan "deliberately out of scope").

## Build provenance

Built from git `main` at the M5 Task 7 head (`d718b30`); the full automated gate
re-run for this release is recorded in the release-preparation commit history
(`uv run pytest -q`, `uv run ruff check .`, `uv run mypy`).
