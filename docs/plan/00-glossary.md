# Marshmellow Glossary

Terms used consistently across all planning docs and (later) code. Source: project brief; extended with planning decisions.

## Product & UX

- **Marshmellow (agent)** — the desktop assistant itself.
- **Marshmellow (mascot)** — the animated marshmallow always-on-top overlay signaling agent state; shares the agent's name deliberately.
- **Voice loop** — full round trip: user speaks → STT → intent/LLM → visible action → TTS result.
- **Ambient** — no window focus or hotkey required; agent runs in background, main UI summoned from tray.
- **Activation mode** — how listening starts (user-selectable, D2): **wake word** (always-on local detection, e.g. "hey marshmellow"), **hotkey push-to-talk**, or **both** (hotkey = instant override while wake word active).
- **Barge-in** — user speech or hotkey that interrupts the agent mid-action or mid-speech.
- **Control intent** — an utterance handled *without* the LLM planner: "stop", "cancel", "mute", "wake up", "go to sleep".
- **Kill switch** — "stop everything" / tray pause: halts all running tasks and TTS immediately.

## Execution model

- **Foreground task** — default mode; UI interaction happens live on screen, user watches.
- **Background task** — work that doesn't own the screen (monitoring, waiting, scheduled checks); reports back by voice.
- **Task class** — classification deciding foreground vs background (see 01-architecture §3.12).
- **Foreground lease** — consent protocol when a background task must touch visible UI: agent announces (mascot + voice), user consents, task temporarily runs foreground.
- **DE pack** — desktop-environment instruction pack: pre-built OS-level operations (open app, window management, settings deep-links) keyed to the GUI stack; Windows pack first.

## Perception & action

- **UIA / UI Automation** — Windows accessibility API exposing UI elements (roles, names, values, bounds, patterns); Marshmellow's primary perception channel.
- **Element** — one interactable unit perceived on screen (`ref`, role, name, value, bbox, available actions).
- **Element ref (`ref`)** — stable ID for an element within a task; planner addresses elements by ref, not by coordinates.
- **Element registry** — maps refs to live UIA nodes / screen points for the lifetime of a task.
- **Vision fallback** — screenshot + vision-language model (VLM) understanding for UI that exposes no accessibility tree (games, custom-drawn apps).
- **Set-of-Marks (SoM)** — screenshot annotated with numbered/labeled overlays so a VLM can reference screen regions reliably.
- **Perception snapshot** — composed view (active window + element list + screenshot refs) feeding one planner decision.
- **Perceive → decide → act → verify** — the generic app-interaction protocol; no per-app integrations.

## Safety

- **Risk level (L0–L3)** — action classification: L0 read/observe · L1 navigate/open · L2 mutate content · L3 sensitive (credentials, money, deletion, sending). L3 requires spoken confirmation.
- **Secrets policy** — credentials are never typed by the agent by default; per-secret opt-in arrives post-v1.

## Infra

- **Contracts** — `src/marshmellow/core/contracts.py`: typed dataclasses/pydantic models crossing module boundaries; single source of truth.
- **EventBus** — async pub/sub connecting engine components to UI (mascot, tray, main window) and logging.
- **Fake layer** — deterministic stand-ins (`FakeSTT`, `FakeTTS`, `FakeLLM`, scripted perception) enabling voice-loop tests without a human speaking.
- **Scenario** — YAML test fixture: simulated instruction + context + expected action trace, runnable headless.
- **Spike** — a bounded research task producing a report with numbers, not code.
