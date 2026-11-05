# Marshmellow — Tooling Inventory: Adopt / Evaluate / Avoid; Skills & Hooks

**Status:** v1 draft for review · Last updated: 2026-11-04 · Brief §8.E
Prior-art context: competitive scan (Nov 2026) — the full Marshmellow combination is unclaimed; every ingredient
exists open source. This doc decides **which ingredients we adopt vs build**.

## 1. Product libraries — ADOPT (licenses verified by [SP7](spikes/SP7-adopt-or-build.md), 2026-11-04)

| Component | Pick | License | Why | Fallback |
|---|---|---|---|---|
| STT | `faster-whisper` (1.2.1) | MIT | CTranslate2 Whisper, int8 CPU, streaming-capable — SP7 CONFIRM-ADOPT | API STT (opt-in) |
| Mic capture | `sounddevice` (≥0.5) | MIT | PortAudio WASAPI in/out for capture + piper PCM playback (M2 Task 4) | wave + winmm (no) |
| VAD | `silero-vad` | MIT | tiny, robust, battle-tested | WebRTC VAD |
| Wake word | `openWakeWord` (0.6.0) | **Apache-2.0 code; bundled pretrained models CC BY-NC-SA 4.0 → ship only self-trained models** (SP7) | offline; "hey marshmellow" trainable in <1 h via synthetic-data pipeline (SP7) | hotkey-only mode (D2 setting) — Porcupine has no free tier since 2026-06-30 (SP7) |
| TTS | `piper` (piper1-gpl ≥1.8.0) | **GPL-3.0-or-later** since 1.3/1.8 (last MIT: 1.2.0) → run as an isolated **subprocess**, never an in-process import (SP7) | offline, fast; voice `en_US-lessac-medium` from rhasspy/piper-voices | edge-tts, opt-in only, pin ≥7.3.1 (SP7) |
| UIA access | `uiautomation` (2.0.29) + `pywinauto` (pin 0.6.9 stable) | **Apache-2.0** / BSD-3-Clause (SP7) | tree walking + patterns — **ADOPTED in M3 Task 1** (`perception/uia_reader.py`; confirms the SP7 table); pywinauto for dialogs/depth (1.0.0b1 revived Oct 2026 — evaluate in M3 only if `uiautomation` hits a wall) | COM interop via ctypes |
| Input synthesis | **raw SendInput (ctypes)** | stdlib | primary path — `pyautogui` is stale (last release May 2023, SP7) | pyautogui only if a helper is needed |
| Screenshots | `mss` (≥9) | MIT | fast multi-monitor capture — **ADOPTED in M3 Task 2** (`perception/vision.py` foreground-window capture) | PIL.ImageGrab |
| Image annotation | Pillow (≥10) | MIT-CMU | SoM tile annotation (6×6 numbered grid) over captured PNGs — **ADOPTED in M3 Task 2** (Agent-S SoM pattern adopted as an idea, §2) | — |
| GUI/tray/mascot | `PySide6` | LGPL-3.0 (dynamic link OK) | tray, frameless overlay, widgets, animations | — |
| Async core | `asyncio` + `pydantic` | stdlib / MIT | EventBus, contracts | — |
| Local LLM | `ollama` | MIT | one-command local models, vision-capable | llama.cpp server |
| Config | `tomllib` + pydantic-settings | stdlib / MIT | typed config | — |
| RSS parsing (M4) | `feedparser` (≥6.0) | **BSD-2-Clause** (verified: 6.0.14 dist metadata, 2026-11-06) | tolerant RSS/Atom parsing for monitor checks — **ADOPTED in M4 Task 1** (`tasks/monitors.py`) | hand-rolled `xml.etree` (no) |
| Packaging (M5) | `uv`, `ruff`, `pytest`, `pyinstaller` | MIT/Apache | scaffold → installer | MSIX |

## 2. EVALUATE (patterns or opt-in, decide in SP7/M2)

- **Pipecat** (BSD-2, SP7-confirmed) — voice-pipeline patterns (transport/VAD/echo handling). Likely *pattern adoption only*; pulling the framework would fight our single-process asyncio design.
- **Agent S / Agent-S2** — GUI-agent loop + perception prompting patterns; **Apache-2.0** (SP7 — earlier "research license" assumption was wrong), so code adoption is legally fine; we still adopt *ideas* (SoM, verification loop) first.
- **OS-World** — scenario/benchmark shapes for our UI-test corpus; **Apache-2.0** (SP7).
- **edge-tts** — unofficial **Microsoft** endpoint; broke Oct 2026 (Sec-MS-GEC v2 token enforcement → 403s on ≤7.2.8), fixed in ≥7.3.1 but ~50% higher latency (SP7) → opt-in extra voice only, pin `edge-tts>=7.3.1`, never default.
- **Porcupine** — **AVOID** (SP7): no free/personal tier remains; free-tier AccessKeys stopped working 2026-06-30. openWakeWord with self-trained models is the wake path.

## 3. Dev-workflow tooling (this CLI), MCP, skills, hooks

**Already available in this coding CLI (dev-side only, not product deps):**
- `browser-use` plugin — black-box GUI testing of web frontends while developing M3 scenarios.
- `computer-use` plugin — comparative dogfooding of UI-automation behavior on Windows.
- `autoresearch` plugin — candidate for M5 latency/quality tuning loops (measured metric: per-step latency).

**MCP servers:** the *product* does not depend on MCP in v1. Extension point recorded (post-v1): Marshmellow's tool
registry may adopt MCP-shaped tool definitions so users can plug tools in. Revisit after M4.

**Project skills to author** (repo `.zcode/skills/`, wired as they become true):

| Skill | Purpose | Introduced |
|---|---|---|
| `simulate-spoken-instruction` | run an instruction through the loop via fixtures (no mic) — the daily dev entry | M1 |
| `run-voice-loop-e2e` | full deterministic E2E (scenario suite) + how to interpret failures | M1 |
| `add-de-pack` | author a new DE pack: interface, alias tables, settings URIs, test checklist | M1 (Windows), reused later |
| `add-scenario` | author a YAML scenario fixture for the runner (structure, app pinning, assertions) | M3 |
| `spike-report` | standard shape for M0 spike reports (question, method, numbers, decision) | M0 |
| `release-checklist` | M5 installer/autostart/privacy-audit gate | M5 |

**Hooks (quality gates) — spec now, wired in the M1 scaffold task:**

| Gate | Trigger | Action |
|---|---|---|
| format+lint | on edit / pre-commit | `ruff format --check`, `ruff check`, `mypy` (core only) |
| tests before completion | pre-"done" in session | `pytest -m "not manual"` must pass |
| scenario suite | pre-milestone-close | `pytest -m scenario` green + DoD evidence |
| commit hygiene | commit-msg | conventional prefixes (`feat/docs/test/spike/chore`) |
| contract-change guard | pre-commit on `contracts.py` | changelog entry in 01-architecture must be updated |

## 4. Explicit non-adoptions (v1)

- No per-app integrations (brief non-goal) — DE pack + generic protocol only.
- No cloud dependency in the core loop path — provider adapters are swappable, offline profile exists.
- No telemetry/SDKs of any kind (privacy posture, 01 §3.15).
- No Electron/webview UI (D1 decision: PySide6).
