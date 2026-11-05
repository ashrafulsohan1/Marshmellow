"""AskUserChannel — spoken question → the next transcript on the bus (§3.13).

The voice half of the L3 confirmation: the question is spoken at ack priority,
the mascot drops to "listening", and the channel waits (a threading.Event set
by a bus subscription) for the NEXT SpeechSegment, then returns its transcript.
``""`` on timeout or kill — the ConfirmFlow then fails closed.

The wait runs in ~100 ms slices with the ``cancelled`` probe polled between
them, so a kill during a 20 s ask aborts within ~250 ms (§3.13). Composition
wires the probe to ``TaskManager.current_kill``; M2's pre-action kill check in
the executor seam remains the second net. Empty transcripts (mic noise,
undecodable audio) are ignored — only a real transcript counts as an answer.
Known limitation: our own spoken question can be captured by the mic (echo);
if it comes back as a transcript it will not match the yes whitelist, so the
loop re-asks and ultimately fails closed rather than confirming by accident.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Final, Protocol

from marshmellow.audio.loop import Transcriber
from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import SpeakRequest, SpeechSegment, Transcript

logger = logging.getLogger(__name__)

_POLL_SLICE_S: Final = 0.1  # kill-probe granularity: abort well within ~250 ms


class Speaker(Protocol):
    """The TTS seam (FakeTTS / PiperSpeaker satisfy it)."""

    def say(self, request: SpeakRequest) -> None: ...


class MascotSeam(Protocol):
    """The mascot state seam (Mascot.set_state is thread-safe via a queued signal)."""

    def set_state(self, state: str) -> None: ...


class AskUserChannel:
    """One spoken question, one transcript answer — or ``""``."""

    def __init__(
        self,
        tts: Speaker,
        stt: Transcriber,
        bus: EventBus,
        mascot: MascotSeam | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        self._tts = tts
        self._stt = stt
        self._bus = bus
        self._mascot = mascot
        self._timeout_s = timeout_s

    def ask(self, question: str, cancelled: Callable[[], bool] | None = None) -> str:
        """Speak `question`, wait for the next SpeechSegment, return its text.

        Returns the transcript of the first segment that transcribes to
        non-empty text, ``""`` on timeout or when `cancelled` turns True.
        """
        got: list[SpeechSegment] = []
        arrived = threading.Event()

        def on_segment(segment: SpeechSegment) -> None:
            got.append(segment)  # bus dispatch is synchronous: appends before set
            arrived.set()

        unsubscribe = self._bus.subscribe(SpeechSegment, on_segment)
        self._set_mascot("listening")
        try:
            # Ack priority: the question interrupts/leads the speaking queue.
            self._tts.say(SpeakRequest(text=question, priority="ack"))
            deadline = time.monotonic() + self._timeout_s
            while time.monotonic() < deadline:
                if cancelled is not None and cancelled():
                    logger.info("ask aborted: kill engaged during confirmation wait")
                    return ""
                if got:
                    text = self._transcribe(got.pop(0))
                    if text.strip():
                        return text
                    logger.info("ask: ignoring empty transcript segment")
                    continue
                arrived.wait(timeout=_POLL_SLICE_S)
                arrived.clear()  # `got` is the source of truth; a race re-fills it
            logger.info("ask timed out after %.1fs (no answer)", self._timeout_s)
            return ""
        finally:
            unsubscribe()
            self._set_mascot("idle")

    def _transcribe(self, segment: SpeechSegment) -> str:
        try:
            transcript: Transcript = self._stt.transcribe(segment)
        except Exception:
            logger.exception("ask: STT failed")
            return ""
        return transcript.text

    def _set_mascot(self, state: str) -> None:
        if self._mascot is not None:
            self._mascot.set_state(state)  # thread-safe: queued onto the GUI thread
