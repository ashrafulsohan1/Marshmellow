"""Receiving-window turn-taking (01-architecture §3.1, M6 Task 3).

The instruction-level boundary of the voice loop. The 0.7 s VAD end-silence
still closes individual SpeechSegments; this window decides when COLLECTED
speech becomes ONE instruction:

    DORMANT → OPEN → executing

- ``open()`` — wake / hotkey / spoken wake. From DORMANT or ``executing`` it
  starts a FRESH window (a wake during execution is never swallowed); during
  an already-OPEN window it just re-arms the close timer and keeps the
  already-collected speech (the user re-armed listening mid-utterance).
- ``feed_speech(text)`` — one STT segment's text. Accumulates and, as voice
  activity, resets the close timer. Feeds outside an OPEN window are ignored:
  the loop owns the policy of when speech implies a window (never while
  asleep).
- ``close()`` — joins the accumulated text with single spaces and emits ONE
  ``InstructionReady`` on the bus; with nothing accumulated it silently
  returns to DORMANT. After a dispatch the window stays ``executing`` until
  the next ``open()``. Returns whether an instruction was emitted.
- ``cancel()`` — the abort edge (OPEN → DORMANT, no emit). The loop calls it
  whenever an abort-family control intent (stop/cancel/kill_all/sleep) fires
  mid-window, so "stop everything" can never leave a collected-but-undispatched
  instruction behind to execute after the kill.

The close timer is clock-guarded, never a sleep: the owner compares
``clock()`` against the deadline through ``maybe_close()`` (the voice loop
arms an asyncio timer as the tick source; tests inject a fake clock and
advance it by hand). Every transition is lock-guarded — bus handlers run on
the drain thread while the close check runs on the voice loop thread, and the
``InstructionReady`` emit happens outside the lock so a subscriber may call
back into the window.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import Enum

from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import InstructionReady


class WindowState(Enum):
    DORMANT = "dormant"  # not collecting (initial / aborted / asleep)
    OPEN = "open"  # collecting speech toward one instruction
    EXECUTING = "executing"  # a dispatched instruction is in flight


class ReceivingWindow:
    """Accumulates speech segments into ONE instruction (M6 Task 3).

    ``open()`` / ``feed_speech()`` / ``close()`` are the interface pinned by
    the plan; ``maybe_close()`` is the clock-guarded tick the owner drives and
    ``cancel()`` is the abort edge. The window is the mechanism — when to open
    (wake/hotkey, or speech itself while awake) is the loop's policy.
    """

    def __init__(
        self,
        bus: EventBus,
        clock: Callable[[], datetime],
        close_after_s: float = 3.0,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self.close_after_s = close_after_s
        self._state = WindowState.DORMANT
        self._parts: list[str] = []
        self._deadline: datetime | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> WindowState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state is WindowState.OPEN

    def open(self) -> None:
        """Wake/hotkey: a fresh window — or, if already OPEN, a re-armed timer."""
        with self._lock:
            if self._state is not WindowState.OPEN:
                self._parts.clear()
            self._state = WindowState.OPEN
            self._deadline = self._clock() + timedelta(seconds=self.close_after_s)

    def feed_speech(self, text: str) -> None:
        """Accumulate one STT segment; voice activity resets the close timer."""
        stripped = text.strip()
        if not stripped:
            return  # empty text is not voice activity: no accumulation, no reset
        with self._lock:
            if self._state is not WindowState.OPEN:
                return  # DORMANT / executing: not collecting
            self._parts.append(stripped)
            self._deadline = self._clock() + timedelta(seconds=self.close_after_s)

    def maybe_close(self) -> bool:
        """Close if OPEN and the close timer has elapsed. Returns close()'s verdict."""
        with self._lock:
            if self._state is not WindowState.OPEN or self._deadline is None:
                return False
            if self._clock() < self._deadline:
                return False
        return self.close()

    def close(self) -> bool:
        """Join the accumulated text into ONE ``InstructionReady``; silent when empty."""
        joined = ""
        with self._lock:
            if self._state is not WindowState.OPEN:
                return False
            joined = " ".join(self._parts)
            self._parts.clear()
            self._deadline = None
            self._state = WindowState.EXECUTING if joined else WindowState.DORMANT
        if not joined:
            return False
        self._bus.emit(InstructionReady(text=joined))  # outside the lock: re-entry safe
        return True

    def cancel(self) -> None:
        """Abort edge (M6 Task 3): discard collected speech, back to DORMANT, no emit."""
        with self._lock:
            if self._state is not WindowState.OPEN:
                return
            self._parts.clear()
            self._deadline = None
            self._state = WindowState.DORMANT
