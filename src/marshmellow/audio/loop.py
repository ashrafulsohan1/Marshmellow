"""Voice loop — collected speech in, spoken results out (01-architecture §2/§3.1).

The integration piece between the audio side of the house and the task
pipeline (M6 Task 3 turn-taking):

    SpeechSegment → STT → ReceivingWindow.feed_speech
        → (3 s without voice) InstructionReady(joined text = ONE instruction)
        → PromptBuilder → control intent (handled locally, never an LLM)
          | TaskManager.submit_text_background → spoken result

Safety behaviors that must always work:

- **Barge-in** (Review Focus 5): a SpeechSegment while TTS is playing stops
  playback and cancels the in-flight turn — and the new segment is still
  processed.
- **Kill switch** (§3.13): "stop everything" cancels every running task,
  stops TTS, drops the mascot to idle — and DISCARDS a pending receiving
  window, so a collected-but-undispatched instruction never executes after
  the kill.
- **Echo guard** (Review Focus 1): ``WakeDetected`` while TTS plays is
  suppressed, so the agent never hears itself wake. Speech capture stays
  live during playback — that is what makes barge-in possible.

Turn-taking: wake/hotkey/spoken wake ``open()`` the ReceivingWindow; the
first instruction speech while awake opens it implicitly (the M2
always-listening behavior, now windowed); each fed segment resets the 3 s
close timer; the closing window emits ONE ``InstructionReady`` with the
joined text. The 0.7 s VAD end-silence still closes individual
SpeechSegments — the window is the instruction-level boundary (§3.1).

Blocking seams (STT) run through ``asyncio.to_thread`` on this loop's own
resident loop thread; neither the bus worker nor the Qt thread ever blocks
(Review Focus 2).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Final, Protocol

from marshmellow.audio.turn_taking import ReceivingWindow
from marshmellow.core.contracts import (
    ContextSnapshot,
    ControlIntent,
    HotkeyPressed,
    InstructionReady,
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

# Abort-family intents discard a pending receiving window: an explicit stop/
# cancel/kill/sleep must never leave collected-but-undispatched speech to
# execute after the kill. mute/unmute/wake don't touch the instruction stream.
_ABORT_INTENTS: Final[frozenset[str]] = frozenset({"stop", "cancel", "kill_all", "sleep"})

# The loop's close-check tick fires this far past the window's deadline; the
# window's injected clock, not the timer, decides whether closing is due.
_CLOSE_MARGIN_S: Final[float] = 0.05


class Transcriber(Protocol):
    """STT seam: segment in, text out (FasterWhisperSTT for real, FakeSTT in tests)."""

    def transcribe(self, segment: SpeechSegment) -> Transcript: ...


class VoiceLoop:
    """Subscribes the audio events and drives the M2 voice loop (§2 steps 3–7).

    Bus handlers are synchronous and cheap; anything that must wait (STT, a
    task turn) is marshalled onto this loop's resident asyncio loop thread,
    where ``TaskManager.submit_text_background`` needs a running loop anyway.
    """

    def __init__(self, app: App, window: ReceivingWindow | None = None) -> None:
        self._app = app
        self._builder = PromptBuilder()
        self._window = window if window is not None else ReceivingWindow(
            bus=app.bus, clock=datetime.now
        )
        self._speaking = False  # TTS playback window (echo guard + barge-in)
        self._sleeping = False  # "go to sleep": instructions dropped until wake
        self._inflight: asyncio.Task[TaskHandle] | None = None
        self._seg_lock = asyncio.Lock()  # segments are processed in arrival order
        self._seg_tasks: set[asyncio.Task[None]] = set()
        self._close_handle: asyncio.TimerHandle | None = None
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
            bus.subscribe(InstructionReady, self._on_instruction_ready),
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
            async def _finish() -> None:
                # The loop's last coroutine, run ON the loop thread: kill the
                # inflight turn, cancel leftover segment tasks, let them settle,
                # then stop — never a cross-loop gather from the caller's thread.
                self._stopping = True
                if self._close_handle is not None:
                    self._close_handle.cancel()
                    self._close_handle = None
                self._cancel_inflight()
                leftover = [t for t in self._seg_tasks if not t.done()]
                for task in leftover:
                    task.cancel()
                if leftover:
                    await asyncio.gather(*leftover, return_exceptions=True)
                self._seg_tasks.clear()
                loop.stop()

            asyncio.run_coroutine_threadsafe(_finish(), loop)
            thread.join(timeout=2.0)
            if thread.is_alive():
                logger.error("voice loop thread survived stop(); loop leaked")
            else:
                loop.close()
        logger.info("voice loop stopped")

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

    def _on_instruction_ready(self, event: InstructionReady) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():  # not started, or already stopped
            return
        try:
            loop.call_soon_threadsafe(self._spawn_dispatch, event.text)
        except RuntimeError:  # the loop stopped under us: drop, never leak a coroutine
            logger.info("instruction dropped: voice loop is shutting down")

    def _spawn_dispatch(self, text: str) -> None:
        if self._stopping or self._loop is None:
            return
        task = self._loop.create_task(self._dispatch_ready(text))
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
        self._window.open()  # fresh window — or a re-armed timer if already OPEN
        self._set_mascot("listening")
        self._arm_close_timer()

    # -- segment pipeline (runs on the loop thread) -------------------------------

    async def _handle_segment(self, segment: SpeechSegment) -> None:
        if self._speaking:
            logger.info("barge-in: speech during TTS playback")
            self._app.tts.stop()
            self._speaking = False  # keep responding even if Stopped is never emitted
            self._cancel_inflight()
        async with self._seg_lock:  # order: one segment fully handled at a time
            transcript = await asyncio.to_thread(self._transcribe, segment)
            text = transcript.text.strip()
            if not text:
                if self._window.is_open:
                    logger.info("empty transcript inside the receiving window; ignored")
                else:
                    logger.info("empty transcript; asking the user to repeat")
                    self._app.tts.say(SpeakRequest(text=UNRECOGNIZED_ACK, priority="ack"))
                return
            parsed = self._builder.build(transcript, ContextSnapshot())
            if isinstance(parsed, ControlIntent):
                # Control intents act immediately — never held for the window to
                # close (the kill switch cannot wait 3 s), and never accumulated.
                self._handle_intent(parsed)
                return
            if self._sleeping:
                # Asleep: instructions wait; control intents (wake, kill_all, …)
                # always pass — the kill switch never sleeps.
                logger.info("asleep; dropping instruction %r", parsed.text)
                return
            if not self._window.is_open:
                # No explicit activation: the first instruction speech while awake
                # opens the window itself (the M2 always-listening behavior —
                # wake/hotkey instead give an explicit fresh window, §3.1).
                self._window.open()
            self._window.feed_speech(text)
            self._arm_close_timer()

    async def _dispatch_ready(self, text: str) -> None:
        """One closed window = ONE instruction (M6 T3): joined text → TaskManager."""
        parsed = self._builder.build(Transcript(text=text), ContextSnapshot())
        if isinstance(parsed, ControlIntent):
            self._handle_intent(parsed)
            return
        if self._sleeping:
            logger.info("asleep; dropping instruction %r", parsed.text)
            return
        self._set_mascot("thinking")
        self._submit(parsed.text)

    # -- the window's close tick (clock decides, the timer only pokes) -------------

    def _arm_close_timer(self) -> None:
        """Schedule the next clock-guarded close check (callable from any thread)."""
        loop = self._loop
        if loop is None or loop.is_closed() or self._stopping:
            return

        def _arm() -> None:
            if self._close_handle is not None or not self._window.is_open:
                return
            self._close_handle = loop.call_later(
                self._window.close_after_s + _CLOSE_MARGIN_S, self._on_close_check
            )

        try:
            loop.call_soon_threadsafe(_arm)
        except RuntimeError:  # the loop stopped under us: nothing to arm
            pass

    def _on_close_check(self) -> None:
        self._close_handle = None
        self._window.maybe_close()  # emits InstructionReady iff the deadline passed
        self._arm_close_timer()  # still open (speech moved the deadline): check again

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
        if intent.kind in _ABORT_INTENTS:
            # stop/cancel/kill_all/sleep also discard a pending receiving window:
            # collected-but-undispatched speech must never execute after the kill.
            self._window.cancel()
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
            # Through the manager so the executor kill flag engages synchronously —
            # task.cancel() alone cannot stop the planner's worker thread.
            self._app.manager.cancel_task(task)

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
