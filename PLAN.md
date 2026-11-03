# Marshmellow — Project Plan (v1)

**Status:** Planning stage — **awaiting user review/approval. No product code until approved** (brief §9.3).
**Last updated:** 2026-11-04 · Owner: main agent + user

Marshmellow is an ambient, hands-free **voice-controlled desktop agent for Windows**: it listens for spoken
instructions, executes them end-to-end on the screen (like a person using the computer while the user watches),
and speaks results back. It is explicitly **not** a coding agent and **not** chat-window-first — the mascot
overlay + tray are the whole visible footprint. Full vision: original project brief (chat); terms: [Glossary](docs/plan/00-glossary.md).

---

## 1. Decisions locked (with user, 2026-11-04)

| # | Decision | Choice | Consequence |
|---|----------|--------|-------------|
| D1 | Tech stack | **Python 3.12 core**, PySide6 for tray/mascot/main UI | Best STT/TTS/ML + Windows UIA ecosystem; single runtime |
| D2 | Activation | **User-selectable in settings**: wake word (always-on local detection) · hotkey push-to-talk · both | Not a fixed design choice — a setting. Hotkey acts as instant override whenever wake word is active |
| D3 | Autostart | **User-selectable setting**: launch at login vs manual start | Implemented as OS autostart toggle (mechanism: see OQ-6) |
| D4 | LLM default | **Cloud API first** (OpenAI-compatible endpoint; provider-agnostic) | Local (Ollama/llama.cpp) is a first-class supported adapter, not the default |
| D5 | Perception | **Hybrid: UI Automation (UIA)-first + vision (screenshot + VLM) fallback** | Composed into one element list; vision required for UIA-blind surfaces |
| D6 | Mode policy | Foreground-visible is the default for anything touching UI; background only for waiting/checking task classes | Background tasks needing UI must escalate via a **foreground lease** (user-consented) |

Sub-decisions delegated to M0 spikes (with numbers, not opinions): STT model/size, wake-word engine, TTS engine default,
vision-LLM choice per provider. See [Milestones](docs/plan/02-milestones.md) and [Open Questions](docs/plan/05-open-questions.md).

## 2. Architecture at a glance

```
 mic ─▶ AudioEngine (VAD + wake-word) ─▶ STT ─▶ PromptBuilder ─▶ Planner/Orchestrator (LLM)
                                                                     │  perceive  ▲ verify
                                                                     ▼            │
                                                          ScreenPerception ◀── ActionLayer
                                                          (UIA tree + vision)  (UIA patterns, SendInput)
                                                                     │
        TaskManager (foreground / background, foreground-lease) ─────┤
                                                                     ▼
                    TTS (speaks results) ◀───────────────────────────┘
        EventBus ── connects everything ──▶ Mascot overlay · Tray · Main UI
```

| Component | One-line responsibility | Spec |
|---|---|---|
| AudioEngine | mic capture, VAD, wake-word/hotkey activation | [01 §3.1](docs/plan/01-architecture.md#31-audioengine--activation) |
| STT | speech → text (faster-whisper default) | [01 §3.2](docs/plan/01-architecture.md#32-stt) |
| PromptBuilder | transcript + context → normalized `Instruction`; control intents | [01 §3.3](docs/plan/01-architecture.md#33-promptbuilder-intent--context) |
| LLM layer | pluggable providers (cloud OpenAI-compat default; Ollama local) | [01 §3.4](docs/plan/01-architecture.md#34-llm-abstraction) |
| Planner/Orchestrator | instruction → perceive→decide→act→verify loop | [01 §3.5](docs/plan/01-architecture.md#35-plannerorchestrator) |
| ScreenPerception | UIA element tree + vision fallback → `PerceptionSnapshot` | [01 §3.6](docs/plan/01-architecture.md#36-screenperception) |
| ActionLayer | execute UIA-pattern actions; SendInput fallback | [01 §3.7](docs/plan/01-architecture.md#37-actionlayer) |
| DE packs | OS-level ops per desktop environment (Windows first) | [01 §3.8](docs/plan/01-architecture.md#38-de-packs) |
| TTS | speaks results; queue + barge-in | [01 §3.9](docs/plan/01-architecture.md#39-tts--voice-feedback) |
| Mascot overlay | always-on-top state indicator (idle/listening/thinking/acting/speaking) | [01 §3.10](docs/plan/01-architecture.md#310-mascot-overlay) |
| Tray + Main UI | settings, task dashboard, transcript history | [01 §3.11](docs/plan/01-architecture.md#311-tray--main-ui) |
| TaskManager | foreground vs background task classes; monitoring; voice reporting | [01 §3.12](docs/plan/01-architecture.md#312-taskmanager) |
| Safety/Consent | action risk levels, confirmation gates, kill switch, secrets policy | [01 §3.13](docs/plan/01-architecture.md#313-safetyconsent-layer) |
| Config / EventBus / Logging | settings, pub/sub, structured local logs | [01 §3.14–3.15](docs/plan/01-architecture.md#314-config) |

Core typed contracts (`Transcript`, `PerceptionSnapshot`, `Element`, `ActionRequest`, `TaskSpec`, …):
[01 §4](docs/plan/01-architecture.md#4-core-contracts-v1-sketch) — authored in M1, single source of truth for all parallel work.

## 3. Milestones overview

| Milestone | Goal | Done when (DoD) |
|---|---|---|
| **M0** Research spikes | Replace guesses with measured numbers | Spike reports in `docs/plan/spikes/`; every delegated sub-decision resolved |
| **M1** Walking skeleton | Repo scaffold + contracts + fake everything | One command runs the app; scripted instruction opens Notepad; deterministic tests green |
| **M2** Voice loop E2E | Real audio → cloud LLM → spoken result, hands-free | "Open Notepad and type hello" by voice; latency budget measured |
| **M3** Generic UI interaction | Perceive/act/verify on *any* app + safety gates | YouTube-check scenario completed hands-free with spoken answer |
| **M4** Background tasks | Monitors, scheduling, voice reporting, dashboard | "Watch this channel and tell me when they upload" works end-to-end |
| **M5** Polish/harden | Local-model tuning, installer, autostart, dogfood | Day-long hands-free dogfood session; installer builds |

Details, per-milestone definitions of done and the **deterministic testing strategy** (fake STT/TTS/LLM,
audio fixtures, UIA-assertion test harness): [02-milestones.md](docs/plan/02-milestones.md).

## 4. How we build it (workflow + subagent discipline)

- **Contract-first, then parallel:** `core/contracts.py` + fakes land first; independent modules build against fakes; integration is sequential and owned by the main agent.
- **Subagent cap:** **4 concurrent** subagents — measured 2026-11-04 (SP6: 7 concurrent requests → 4 ran, 2 rejected *user concurrency limit exceeded*; background agents count toward the cap). Fan-out never exceeds the cap; overflow queues.
- Inline (sequential, main agent): contracts, planner design, safety design, all cross-module integration.
- Delegated (parallelizable): spike research, module scaffolds against contracts, scenario/test authoring, docs, mascot assets.

## 5. Adopt vs build (tooling inventory)

Pre-build inventory of libraries, MCP servers, skills, and hooks — adopt-where-possible list with licenses
(faster-whisper, silero-vad, openWakeWord, piper, uiautomation/pywinauto, PySide6, Ollama; what we deliberately
avoid and why): [04-tooling.md](docs/plan/04-tooling.md). Project skills to author (`add-de-pack`,
`run-voice-loop-e2e`, `simulate-spoken-instruction`, …) and quality-gate hooks are specified there.

## 6. Open questions

Full register with options + recommendations: [05-open-questions.md](docs/plan/05-open-questions.md).
Highest-impact, needing user input before/during M0:

1. **Wake-word phrase** — "hey marshmellow" requires custom training on openWakeWord; Porcupine does arbitrary phrases but is commercial-licensed. (OQ-1)
2. **Cloud provider & key for v1 default** — OpenAI-compatible default endpoint; which provider/key goes in first-party config. (OQ-2)
3. **Mascot art pipeline** — PNG sprite sequence vs Lottie vs live vector; who authors the art. (OQ-3)
4. **Language** — English-only v1 (recommend) or bilingual from M2. (OQ-5)

## 7. What happens next

1. **You review this plan** (start at `PLAN.md`, then 01–05). Approve, or mark up changes.
2. Broader product requirements arrive (as you said they would) → docs get extended, open questions closed.
3. On approval: run **M0 spikes** (research/reports only — still no product code), then author the detailed
   task-level M1 plan in writing-plans format and begin the walking skeleton.
