# Marshmellow — Tooling Inventory: Adopt / Evaluate / Avoid; Skills & Hooks

**Status:** v1 draft for review · Last updated: 2026-11-04 · Brief §8.E
Prior-art context: competitive scan (Nov 2026) — the full Marshmellow combination is unclaimed; every ingredient
exists open source. This doc decides **which ingredients we adopt vs build**.

## 1. Product libraries — ADOPT (pending M0-SP7 license re-verification)

| Component | Pick | License | Why | Fallback |
|---|---|---|---|---|
| STT | `faster-whisper` | MIT | CTranslate2 Whisper, int8 CPU, streaming-capable | API STT (opt-in) |
| VAD | `silero-vad` | MIT | tiny, robust, battle-tested | WebRTC VAD |
| Wake word | `openWakeWord` | Apache-2.0 | offline, trainable phrase (OQ-1) | Porcupine (license ↓) |
| TTS | `piper` | MIT | offline, fast, small voices | edge-tts (opt-in, ↓) |
| UIA access | `uiautomation` + `pywinauto` | verify in SP7 (≈MIT/BSD-3) | tree walking + patterns; pywinauto for dialogs/depth | COM interop via ctypes |
| Input synthesis | `pyautogui` or raw SendInput (ctypes) | BSD-3 / stdlib | keyboard/mouse fallback layer | SendInput only |
| Screenshots | `mss` | MIT | fast multi-monitor capture | PIL.ImageGrab |
| GUI/tray/mascot | `PySide6` | LGPL-3.0 (dynamic link OK) | tray, frameless overlay, widgets, animations | — |
| Async core | `asyncio` + `pydantic` | stdlib / MIT | EventBus, contracts | — |
| Local LLM | `ollama` | MIT | one-command local models, vision-capable | llama.cpp server |
| Config | `tomllib` + pydantic-settings | stdlib / MIT | typed config | — |
| Packaging (M5) | `uv`, `ruff`, `pytest`, `pyinstaller` | MIT/Apache | scaffold → installer | MSIX |

## 2. EVALUATE (patterns or opt-in, decide in SP7/M2)

- **Pipecat** (BSD-2) — voice-pipeline patterns (transport/VAD/echo handling). Likely *pattern adoption only*; pulling the framework would fight our single-process asyncio design.
- **Agent S / Agent-S2** (research) — GUI-agent loop + perception prompting patterns; we adopt *ideas* (SoM, verification loop), not code (research license, AC by default).
- **OS-World** — scenario/benchmark shapes for our UI-test corpus.
- **edge-tts** — unofficial Google endpoint: better voices but ToS/reliability risk → opt-in extra voice only, never default.
- **Porcupine** — arbitrary custom wake phrases, but Picovoice commercial licensing → only if openWakeWord can't do "hey marshmellow".

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
