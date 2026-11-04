"""Voice loop — SpeechSegment in, spoken results out (01-architecture §2, M2 Task 8).

The integration piece between the audio side of the house and the task
pipeline:

    SpeechSegment → STT → PromptBuilder → control intent (handled locally,
    never an LLM) | TaskManager.submit_text_background → spoken result

Safety behaviors that must always work:

- **Barge-in** (Review Focus 5): a SpeechSegment while TTS is playing stops
  playback and cancels the in-flight turn — and the new segment is still
  processed.
- **Kill switch** (§3.13): "stop everything" cancels every running task,
  stops TTS and drops the mascot to idle.
- **Echo guard** (Review Focus 1): ``WakeDetected`` while TTS plays is
  suppressed, so the agent never hears itself wake. Speech capture stays
  live during playback — that is what makes barge-in possible.

Blocking seams (STT) run through ``asyncio.to_thread`` on this loop's own
resident loop thread; neither the bus worker nor the Qt thread ever blocks
(Review Focus 2).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Protocol

from marshmellow.core.contracts import (
    ContextSnapshot,
    ControlIntent,
    HotkeyPressed,
    Instruction,
    SpeakingStarted,
    SpeakingStopped,
    SpeakRequest,
    SpeechSegment,
    Transcript,
    WakeDetected,
)
from marshmellow.core.prompt_builder import PromptBuilder

if TYPE_CHECKING:
    from collections.abc import Callable

    from marshmellow.app import App
    from marshmellow.tasks.manager import TaskHandle

logger = logging.getLogger(__name__)

UNRECOGNIZED_ACK = "Sorry, I didn't catch that."


class Transcriber(Protocol):
    """STT seam: segment in, text out (FasterWhisperSTT for real, FakeSTT in tests)."""

    def transcribe(self, segment: SpeechSegment) -> Transcript: ...


class VoiceLoop:
    """Subscribes the audio events and drives the M2 voice loop (§2 steps 3–7).

    Bus handlers are synchronous and cheap; anything that must wait (STT, a
    task turn) is marshalled onto this loop's resident asyncio loop thread,
    where ``TaskManager.submit_text_background`` needs a running loop anyway.
    """

    def __init__(self, app: App) -> None:
        self._app = app
        self._builder = PromptBuilder()
        self._speaking = False  # TTS playback window (echo guard + barge-in)
        self._sleeping = False  # "go to sleep": instructions dropped until wake
        self._inflight: asyncio.Task[TaskHandle] | None = None
        self._seg_lock = asyncio.Lock()  # segments are processed in arrival order
        self._seg_tasks: set[asyncio.Task[None]] = set()
        self._stopping = False
        self._unsubs: list[Callable[[], None]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        bus = self._app.bus
        self._unsubs = [
            bus.subscribe(SpeechSegment, self._on_speech_segment),
            bus.subscribe(SpeakingStarted, self._on_speaking_started),
            bus.subscribe(SpeakingStopped, self._on_speaking_stopped),
            bus.subscribe(WakeDetected, self._on_wake_detected),
            bus.subscribe(HotkeyPressed, self._on_hotkey_pressed),
        ]
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="voice-loop", daemon=True
        )
        self._thread.start()
        logger.info("voice loop started")

    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        loop, thread = self._loop, self._thread
        self._loop = self._thread = None
        if loop is not None and thread is not None:
            loop.call_soon_threadsafe(self._shutdown)
            thread.join(timeout=2.0)
            if not loop.is_running():
                self._drain(loop)  # clean cancel of any leftover segment tasks
                loop.close()
        logger.info("voice loop stopped")

    def _shutdown(self) -> None:
        self._stopping = True
        self._cancel_inflight()
        assert self._loop is not None
        self._loop.stop()

    def _drain(self, loop: asyncio.AbstractEventLoop) -> None:
        """Last turn of the loop (caller's thread; it has stopped): cancel leftovers."""
        leftover = [t for t in self._seg_tasks if not t.done()]
        for task in leftover:
            task.cancel()
        if leftover:
            loop.run_until_complete(asyncio.gather(*leftover, return_exceptions=True))
        self._seg_tasks.clear()

    # -- bus handlers (synchronous; run on the emitting thread) -------------------

    def _on_speech_segment(self, segment: SpeechSegment) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():  # not started, or already stopped
            return
        try:
            loop.call_soon_threadsafe(self._spawn_segment, segment)
        except RuntimeError:  # the loop stopped under us: drop, never leak a coroutine
            logger.info("segment dropped: voice loop is shutting down")

    def _spawn_segment(self, segment: SpeechSegment) -> None:
        if self._stopping or self._loop is None:
            return
        task = self._loop.create_task(self._handle_segment(segment))
        self._seg_tasks.add(task)
        task.add_done_callback(self._seg_tasks.discard)

    def _on_speaking_started(self, event: SpeakingStarted) -> None:
        self._speaking = True

    def _on_speaking_stopped(self, event: SpeakingStopped) -> None:
        self._speaking = False

    def _on_wake_detected(self, event: WakeDetected) -> None:
        if self._speaking:
            # Echo guard (Review Focus 1): TTS audio must not re-trigger the wake
            # word — that is the infinite self-talk loop.
            logger.info("wake suppressed while speaking (score=%.2f)", event.score)
            return
        self._activate()

    def _on_hotkey_pressed(self, event: HotkeyPressed) -> None:
        self._activate()

    def _activate(self) -> None:
        self._sleeping = False
        self._set_mascot("listening")

    # -- segment pipeline (runs on the loop thread) -------------------------------

    async def _handle_segment(self, segment: SpeechSegment) -> None:
        if self._speaking:
            logger.info("barge-in: speech during TTS playback")
            self._app.tts.stop()
            self._speaking = False  # keep responding even if Stopped is never emitted
            self._cancel_inflight()
        async with self._seg_lock:  # order: one segment fully handled at a time
            transcript = await asyncio.to_thread(self._transcribe, segment)
            if not transcript.text.strip():
                logger.info("empty transcript; asking the user to repeat")
                self._app.tts.say(SpeakRequest(text=UNRECOGNIZED_ACK, priority="ack"))
                return
            parsed = self._builder.build(transcript, ContextSnapshot())
            if self._sleeping and isinstance(parsed, Instruction):
                # Asleep: instructions wait; control intents (wake, kill_all, …)
                # always pass — the kill switch never sleeps.
                logger.info("asleep; dropping instruction %r", parsed.text)
                return
            if isinstance(parsed, ControlIntent):
                self._handle_intent(parsed)
                return
            self._set_mascot("thinking")
            self._submit(parsed.text)

    def _transcribe(self, segment: SpeechSegment) -> Transcript:
        try:
            return self._app.stt.transcribe(segment)
        except Exception:
            logger.exception("STT failed")
            return Transcript(text="")

    def _submit(self, text: str) -> None:
        task = self._app.manager.submit_text_background(text)
        self._inflight = task
        task.add_done_callback(self._on_turn_done)
        logger.info("instruction submitted: %r", text)

    def _on_turn_done(self, task: asyncio.Task[TaskHandle]) -> None:
        if self._inflight is task:
            self._inflight = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("voice turn crashed: %r", exc)

    # -- control intents (local handling, never an LLM call) -----------------------

    def _handle_intent(self, intent: ControlIntent) -> None:
        logger.info("control intent: %s", intent.kind)
        if intent.kind == "stop":
            self._app.tts.stop()
            self._speaking = False
        elif intent.kind == "cancel":
            self._cancel_inflight()
        elif intent.kind == "mute":
            self._set_muted(True)
        elif intent.kind == "unmute":
            self._set_muted(False)
        elif intent.kind == "sleep":
            self._sleeping = True
            self._set_mascot("idle")
        elif intent.kind == "wake":
            self._activate()
        elif intent.kind == "kill_all":
            self._kill_all()

    def _kill_all(self) -> None:
        """Kill switch: cancel every task, stop TTS, mascot idle."""
        self._app.tts.stop()
        self._speaking = False
        self._cancel_inflight()
        self._app.manager.cancel_all()
        self._set_mascot("idle")

    def _cancel_inflight(self) -> None:
        task = self._inflight
        if task is not None and not task.done():
            task.cancel()

    # -- small side-effect helpers -------------------------------------------------

    def _set_muted(self, muted: bool) -> None:
        engine = self._app.audio_engine
        if engine is not None:
            engine.set_muted(muted)
        self._set_mascot("muted" if muted else "listening")

    def _set_mascot(self, state: str) -> None:
        mascot = self._app.mascot
        if mascot is not None:
            mascot.set_state(state)  # type: ignore[attr-defined]
