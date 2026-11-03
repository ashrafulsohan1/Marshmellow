# M2 Voice Loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline — integration-heavy; contracts and safety-critical wiring are main-agent work per 03-workflow). Steps use checkbox (`- [ ]`) syntax.

**Goal:** say "hey marshmellow" (or hold the hotkey), speak "open Notepad and type hello", watch it happen, hear the result spoken back — with barge-in and a kill switch that always work.

**Architecture:** real implementations replace the M1 fakes behind unchanged contracts: AudioEngine (capture+VAD+activation) → STT → PromptBuilder (control intents | instruction) → TaskManager/LLMPlanner → PiperSpeaker. Every blocking boundary crosses a thread or subprocess; the event loop never blocks.

**Tech Stack:** adds `sounddevice`, `silero-vad`, `openwakeword` (code Apache-2.0 — **self-trained model only**, D8), `openai` SDK (MIT, OpenAI-compatible endpoint), `keyring` (MIT → Windows Credential Manager, D7).

**Spec:** [02-milestones.md M2](../02-milestones.md) · [01-architecture.md](../01-architecture.md) · decisions D2/D7/D8.

## Global Constraints

- All M1 gates stay green: `uv run pytest -q && uv run ruff check . && uv run mypy` (pytest un-piped — see gate discipline).
- **No API key ever reaches disk logs or TOML** — Credential Manager only; scrubber test must prove it.
- Audio unit tests run on numpy arrays / fixture WAVs (no mic in CI); anything needing real devices is `manual`.
- The `av`/faster-whisper rule: **numpy float32 16 kHz PCM in, never file paths** (SP2).
- Bundled openWakeWord models are dev/test-only, env-gated (`MARSHMELLOW_DEV_MODELS=1`); the product ships only the self-trained "hey marshmellow" model.
- New deps land in pyproject with the task that needs them, license noted in 04-tooling.

## Review Focus

1. **Echo loop** — TTS audio re-triggers the wake word → infinite self-talk. Echo guard (wake suppressed while `SpeakingStarted…SpeakingStopped`) gets a dedicated test.
2. **Blocked event loop** — audio callbacks / STT / LLM must never run inline on the Qt or asyncio loop; every blocking seam is `to_thread`/subprocess, asserted by design review + a watchdog test (loop stays responsive during a fake 2 s transcribe).
3. **Piper subprocess lifecycle** — stop()/quit must not leak zombies (test asserts process exit); stdout buffer must not deadlock on long text (streamed, never read-all).
4. **Hotkey already registered** (another app owns ctrl+shift+m) → degrade with a spoken warning and tray flag, never crash — test with an injected failure.
5. **Barge-in correctness** — speech during TTS stops playback AND cancels the in-flight planner turn without corrupting TaskManager state (test with interleaved fixture events).

---

### Task 1: Real STT

**Files:** Create `src/marshmellow/stt/fasterwhisper.py`, `tests/stt/test_fasterwhisper.py`. Modify `pyproject.toml` (av already a dep of faster-whisper).

**Interfaces:** `FasterWhisperSTT(model: str = "base", compute_type: str = "int8", language: str = "en")` with `.transcribe(segment: SpeechSegment) -> Transcript` — int16 LE bytes → float32 16 kHz numpy (SP2 rule), `beam_size=1`.

- [ ] Step 1: failing tests — synthetic tone segment returns a Transcript without crashing; SP2 fixture dir (if present, `fixture_audio` marker, skipif missing) transcribes with WER ≤ 0.35 and median RTF < 1.0
- [ ] Step 2: run → FAIL (module missing)
- [ ] Step 3: implement
- [ ] Step 4: run → PASS
- [ ] Step 5: commit `feat: faster-whisper STT (numpy PCM path, SP2 config)`

### Task 2: Real TTS (piper resident subprocess)

**Files:** Create `src/marshmellow/tts/piper.py`, `tests/tts/test_piper.py`.

**Interfaces:** `PiperSpeaker(model_dir: Path, sample_rate=22050)` with `.say(req: SpeakRequest)`, `.stop()`, queue honoring priority/interrupt; emits `SpeakingStarted`/`SpeakingStopped` on the bus; raw-PCM streaming playback via `sounddevice.OutputStream`.

- [ ] Step 1: failing tests — (a) with a fake subprocess seam, `say()` writes the text line to stdin and consumes canned PCM chunks in order; (b) `stop()` terminates the process (no zombie: `proc.poll() is not None` after join timeout); (c) interrupt=True clears the queue
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (verify the piper1-gpl CLI streaming invocation against `piper --help` first; subprocess + reader thread)
- [ ] Step 4: run → PASS; `manual` test: hear "On it." from the real speakers
- [ ] Step 5: commit `feat: piper TTS as resident streaming subprocess`

### Task 3: LLM provider + BYOK

**Files:** Create `src/marshmellow/llm/base.py`, `src/marshmellow/llm/openai_compat.py`, `src/marshmellow/core/secrets.py`, `tests/llm/test_openai_compat.py`. Modify `pyproject.toml` (openai, keyring).

**Interfaces:** `LLMProvider(Protocol): async complete(req: LLMRequest) -> LLMResponse` (LLMRequest/Response added to contracts — changelog entry); `OpenAICompatProvider(base_url, model, get_key)` via the `openai` SDK with injectable httpx transport; `secrets.store_api_key(key) / load_api_key() -> str | None` (keyring service `"Marshmellow"`); `tools/set_key.py` CLI for onboarding.

- [ ] Step 1: failing tests — (a) golden offline tool-call round trip via httpx `MockTransport` (streamed chat.completions with tool_calls); (b) `store/load` round-trip with a fake keyring backend; (c) scrubber integration: a logged error containing the key string never appears in the JSONL
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (contracts changelog entry in the same commit)
- [ ] Step 4: run → PASS
- [ ] Step 5: commit `feat: OpenAI-compatible provider, BYOK via Credential Manager`

### Task 4: Audio capture + VAD

**Files:** Create `src/marshmellow/audio/capture.py`, `src/marshmellow/audio/fixtures.py`, `tests/audio/test_capture.py`. Modify `pyproject.toml` (sounddevice, silero-vad).

**Interfaces:** `AudioEngine(device_factory=None, sample_rate=16000)` — capture thread → silero VAD → 700 ms silence closes a segment → `emit_threadsafe(SpeechSegment)`; `FixtureMic(files: list[Path])` replays WAVs as PCM chunks (the fake mic seam); `SpeakingStarted/Stopped` suppression hook for the echo guard.

- [ ] Step 1: failing tests — synthetic burst+silence arrays segment into the expected number of SpeechSegments; FixtureMic replays a WAV through the same pipeline; no real device touched
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement
- [ ] Step 4: run → PASS (device path covered by Task 9 manual test)
- [ ] Step 5: commit `feat: audio capture with silero VAD segmentation`

### Task 5: Activation — hotkey + wake word

**Files:** Create `src/marshmellow/audio/activation.py`, `tests/audio/test_activation.py`.

**Interfaces:** `HotkeyWatcher(combo="ctrl+shift+m", mode: toggle|hold)` — ctypes `WH_KEYBOARD_LL` hook thread (stdlib, no new deps), emits `HotkeyPressed/Released`; clean unregister on stop; registration failure → spoken degradation event; `WakeWordWatcher(model_dir | None, chunk_source)` — openWakeWord 80 ms chunk loop → `WakeDetected` (threshold 0.5, SP3).

- [ ] Step 1: failing tests — hotkey handler reacts to injected key events; stop() unregisters (hook count via injection seam); registration-failure path emits the degradation event; wake watcher emits WakeDetected on a fixture chunk sequence containing the phrase (dev-models env-gated, skipif unavailable)
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement
- [ ] Step 4: run → PASS
- [ ] Step 5: commit `feat: activation — LL-hook hotkey and wake-word watcher`

### Task 6: PromptBuilder (control intents)

**Files:** Create `src/marshmellow/core/prompt_builder.py`, `tests/core/test_prompt_builder.py`.

**Interfaces:** `PromptBuilder.build(transcript, context) -> ControlIntent | Instruction` — table of control intents (stop, cancel, mute, unmute, wake up, go to sleep, stop everything→kill_all); everything else becomes an Instruction with context. No LLM call, ever.

- [ ] Step 1: failing table-driven tests (each intent, case-insensitivity, "please stop the music" is NOT a control intent)
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement
- [ ] Step 4: run → PASS
- [ ] Step 5: commit `feat: control-intent routing without LLM`

### Task 7: LLM orchestrator v1

**Files:** Create `src/marshmellow/planner/orchestrator.py`, `tests/planner/test_orchestrator.py`.

**Interfaces:** `LLMPlanner(provider: LLMProvider, tools)` implementing the M1 `Planner` protocol — M2 tool set: `open_app`, `wait`, `ask_user`, `finish`; JSON-schema-constrained actions; max-steps (12) + wall-clock timeout (60 s); verify-lite (observation fed back before finish); classifies immediate_reply vs foreground; no UI tools yet (M3).

- [ ] Step 1: failing tests — FakeLLM-driven golden traces: (a) one-shot shell action → finish; (b) ask_user round trip; (c) max-steps safety cut; (d) provider exception → spoken failure, task marked failed
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (prompts in `planner/prompts/`, versioned)
- [ ] Step 4: run → PASS
- [ ] Step 5: commit `feat: cloud LLM orchestrator v1 (shell/wait/ask/finish)`

### Task 8: Voice-loop integration (barge-in, kill switch, echo guard)

**Files:** Modify `src/marshmellow/app.py` (+ new `src/marshmellow/audio/loop.py` if size demands), `tests/test_voice_loop.py`.

**Interfaces:** `VoiceLoop(app)` — subscribes SpeechSegment → PromptBuilder → control-intent handling (mute/stop/kill_all local) or `manager.submit_text_background`; barge-in: SpeechSegment during speaking → `tts.stop()` + cancel in-flight turn; kill_all: cancel tasks + stop TTS + mascot idle; echo guard per Review Focus 1. Settings drive real-vs-fake wiring (D2 activation mode, D7 provider).

- [ ] Step 1: failing headless tests — (a) fixture SpeechSegment "open notepad" ends with spoken result; (b) "stop everything" cancels a running fake task; (c) speech during speaking stops TTS and the next segment still processes; (d) wake during TTS is suppressed
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement
- [ ] Step 4: run → PASS
- [ ] Step 5: commit `feat: voice loop with barge-in, kill switch, echo guard`

### Task 9: Latency budget + assisted E2E + DoD

- [ ] Step 1: measure on this machine: wake→ack, end-speech→transcript, per-step, TTS first audio; write the table into 02-milestones M2 section vs targets (≤0.5/≤1.5/≤3/≤0.5 s)
- [ ] Step 2: assisted manual E2E (user speaks): "hey marshmellow — open Notepad" hands-free
- [ ] Step 3: full gate + DoD evidence into 02-milestones; commit `docs: M2 DoD evidence`

## Execution notes

- Tasks 1–5 are independent once contracts land; 6–8 are sequential. Inline (no subagents) — heavy local-hardware coupling.
- The self-trained wake model training (synthetic-TTS pipeline, SP7/OQ-1) runs alongside Tasks 4–5; until it exists, wake-mode tests use dev-gated bundled models and the hotkey path is the trusted one.
