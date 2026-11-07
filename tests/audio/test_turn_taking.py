"""Receiving-window turn-taking (M6 Task 3) — the instruction-level boundary.

Two layers:

- `ReceivingWindow` unit tests on an injected fake clock: DORMANT → OPEN →
  executing, the 3 s voice-activity close, joining accumulated segments into
  ONE instruction. No sleeps anywhere — the clock is advanced by hand and
  `maybe_close()` is the clock-guarded tick.
- VoiceLoop wiring tests (headless, like tests/test_voice_loop.py): wake/hotkey
  → open, per-segment STT → feed, `InstructionReady` → PromptBuilder →
  TaskManager. The loop's real close timer does the dispatching (~3 s), and an
  abort intent must discard a pending window instead of executing it after the
  kill.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from marshmellow.app import App
from marshmellow.audio.loop import VoiceLoop
from marshmellow.audio.turn_taking import ReceivingWindow, WindowState
from marshmellow.core.bus import EventBus
from marshmellow.core.config import Settings
from marshmellow.core.contracts import (
    InstructionReady,
    SpeechSegment,
    Transcript,
    WakeDetected,
)
from marshmellow.packs.base import ShellResult

# -- window unit tests: fake clock, real bus ------------------------------------


class FakeClock:
    """Injected clock: tests advance it by hand; no wall-clock sleeps."""

    def __init__(self) -> None:
        self._now = datetime(2026, 11, 7, 12, 0, 0)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class _Harness:
    """A window on a real bus with the InstructionReady events it emits."""

    def __init__(self, close_after_s: float = 3.0) -> None:
        self.bus = EventBus()
        self.clock = FakeClock()
        self.ready: list[str] = []

        def _record(event: InstructionReady) -> None:
            self.ready.append(event.text)

        self.bus.subscribe(InstructionReady, _record)
        self.window = ReceivingWindow(self.bus, self.clock, close_after_s=close_after_s)

    def stop(self) -> None:
        self.bus.stop()


def test_wake_then_two_segments_dispatch_one_instruction_with_both_texts() -> None:
    h = _Harness()
    w = h.window
    w.open()  # wake
    w.feed_speech("open notepad")
    h.clock.advance(2.0)
    w.maybe_close()  # 2 s apart: inside the window, nothing dispatched yet
    assert h.ready == [] and w.state is WindowState.OPEN

    w.feed_speech("and type hello")
    h.clock.advance(3.0)
    assert w.maybe_close() is True

    assert h.ready == ["open notepad and type hello"], "both texts, ONE instruction"
    assert w.state is WindowState.EXECUTING
    h.stop()


def test_three_seconds_without_voice_closes_and_dispatches() -> None:
    h = _Harness()
    w = h.window
    w.open()
    w.feed_speech("open calc")

    h.clock.advance(2.5)
    assert w.maybe_close() is False, "2.5 s of silence must not close the window"
    assert h.ready == []

    h.clock.advance(0.5)  # exactly the 3 s close_after
    assert w.maybe_close() is True
    assert h.ready == ["open calc"]
    h.stop()


def test_close_with_nothing_accumulated_dispatches_nothing() -> None:
    h = _Harness()
    w = h.window
    w.open()  # wake that never got speech
    h.clock.advance(3.0)
    assert w.maybe_close() is False  # silent close, not an error, no dispatch
    assert h.ready == []
    assert w.state is WindowState.DORMANT, "nothing to execute: back to dormant"

    w.open()
    assert w.close() is False, "an explicit empty close stays silent too"
    assert h.ready == []
    h.stop()


def test_open_during_the_window_resets_the_close_timer() -> None:
    h = _Harness()
    w = h.window
    w.open()
    w.feed_speech("open notepad")
    h.clock.advance(2.0)
    w.open()  # hotkey again mid-collection: the close timer re-arms

    h.clock.advance(2.0)  # 4 s since the last feed — past the original deadline
    w.maybe_close()
    assert w.state is WindowState.OPEN, "the re-open reset the close timer"
    assert h.ready == []

    h.clock.advance(1.0)  # 3 s since the re-open
    assert w.maybe_close() is True
    assert h.ready == ["open notepad"], "already-collected speech is kept"
    h.stop()


def test_wake_while_executing_opens_a_fresh_window() -> None:
    h = _Harness()
    w = h.window
    w.open()
    w.feed_speech("open calc")
    h.clock.advance(3.0)
    w.maybe_close()
    assert w.state is WindowState.EXECUTING and h.ready == ["open calc"]

    w.open()  # wake during execution: never swallowed
    assert w.state is WindowState.OPEN
    w.feed_speech("open explorer")
    h.clock.advance(3.0)
    w.maybe_close()

    assert h.ready == ["open calc", "open explorer"], "the new window is unaffected"
    h.stop()


def test_speech_outside_an_open_window_is_ignored() -> None:
    h = _Harness()
    w = h.window
    w.feed_speech("hello")  # DORMANT: not collecting
    h.clock.advance(10.0)
    w.maybe_close()
    assert h.ready == [] and w.state is WindowState.DORMANT

    w.open()
    w.feed_speech("a")
    h.clock.advance(3.0)
    w.maybe_close()  # → EXECUTING
    w.feed_speech("b")  # no wake since: still not collecting
    h.clock.advance(10.0)
    w.maybe_close()
    assert h.ready == ["a"], "speech without an open window never joins a window"
    h.stop()


def test_empty_text_is_not_voice_activity() -> None:
    h = _Harness()
    w = h.window
    w.open()
    w.feed_speech("open calc")
    h.clock.advance(2.0)
    w.feed_speech("   ")  # empty/whitespace: no accumulation, no timer reset
    h.clock.advance(1.5)  # 3.5 s since the real feed
    assert w.maybe_close() is True
    assert h.ready == ["open calc"]
    h.stop()


def test_seconds_until_close_tracks_the_deadline() -> None:
    """The seam the loop's close tick arms from: remaining time, not a full window."""
    h = _Harness()
    w = h.window
    assert w.seconds_until_close() is None, "DORMANT: nothing to count down"

    w.open()
    assert w.seconds_until_close() == pytest.approx(3.0)
    h.clock.advance(1.0)
    assert w.seconds_until_close() == pytest.approx(2.0)
    w.feed_speech("open calc")  # voice activity resets the deadline
    assert w.seconds_until_close() == pytest.approx(3.0)
    h.clock.advance(3.0)  # exactly the deadline
    assert w.seconds_until_close() == pytest.approx(0.0)

    w.maybe_close()  # → EXECUTING
    assert w.seconds_until_close() is None
    h.stop()


def test_cancel_discards_a_pending_instruction() -> None:
    h = _Harness()
    w = h.window
    w.open()
    w.feed_speech("open notepad")
    w.cancel()  # the loop's abort path (stop/cancel/kill_all/sleep mid-window)
    assert w.state is WindowState.DORMANT
    h.clock.advance(10.0)
    w.maybe_close()
    assert h.ready == [], "an aborted window must never dispatch"

    w.cancel()  # DORMANT: a no-op
    assert w.state is WindowState.DORMANT
    h.stop()


# -- voice loop wiring -----------------------------------------------------------


class RecordingPack:
    """Stands in for WindowsPack: records open_app calls, opens nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def open_app(self, target: str) -> ShellResult:
        self.calls.append(target)
        return ShellResult(ok=True, detail=f"would open {target}")


class FakeMascot:
    def __init__(self) -> None:
        self.state: str = "idle"

    def show(self) -> None:
        return None

    def set_state(self, state: str) -> None:
        self.state = state

    def close(self) -> None:
        return None


class FakeAudioEngine:
    def __init__(self) -> None:
        self.muted: bool | None = None
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def set_muted(self, muted: bool) -> None:
        self.muted = muted


def _segment(text: str) -> SpeechSegment:
    """FakeSTT fixture encoding: the UTF-8 text rides in `pcm`."""
    now = datetime.now()
    return SpeechSegment(pcm=text.encode("utf-8"), started_at=now, ended_at=now)


class SlowSTT:
    """Transcriber that sleeps on the worker thread — feeds land long after the wake arm."""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    def transcribe(self, segment: SpeechSegment) -> Transcript:
        time.sleep(self.delay)
        return Transcript(text=segment.pcm.decode("utf-8", errors="ignore"), source="fixture")


def _app(tmp_path: object, pack: RecordingPack) -> App:
    app = App(
        settings=Settings(),
        headless=True,
        log_dir=tmp_path,  # type: ignore[arg-type]
        pack=pack,
        audio_engine=FakeAudioEngine(),  # type: ignore[arg-type]
    )
    app.mascot = FakeMascot()  # type: ignore[assignment]
    return app


async def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


async def test_wake_then_two_segments_submit_one_joined_instruction(tmp_path) -> None:
    """Wake → two segments → the REAL close timer dispatches the joined text once."""
    pack = RecordingPack()
    app = _app(tmp_path, pack)
    assert isinstance(app.voice, VoiceLoop)
    app.start()
    try:
        app.bus.emit(WakeDetected(score=0.9))
        app.bus.emit(_segment("open notepad"))
        app.bus.emit(_segment("and open calc"))

        await _wait_for(lambda: pack.calls == ["notepad and open calc"], timeout=8.0)
        assert pack.calls == ["notepad and open calc"], "one dispatch, joined text"
    finally:
        app.stop()


async def test_wake_while_executing_starts_a_new_window(tmp_path) -> None:
    pack = RecordingPack()
    app = _app(tmp_path, pack)
    app.start()
    try:
        app.bus.emit(_segment("open notepad"))  # no wake: the loop opens the window itself
        await _wait_for(lambda: pack.calls == ["notepad"])  # window closed → executing

        app.bus.emit(WakeDetected(score=0.9))  # wake mid-execution: never swallowed
        app.bus.emit(_segment("open calc"))
        await _wait_for(lambda: pack.calls == ["notepad", "calc"], timeout=8.0)
        assert pack.calls == ["notepad", "calc"]
    finally:
        app.stop()


async def test_close_lands_one_margin_past_the_deadline_despite_late_feeds(tmp_path) -> None:
    """Regression (M6 T3 review): the tick re-arms from the REMAINING time, never a full window.

    Wake arms the tick at t0 (1.05 s out); the feed lands 0.5 s later (real-STT
    latency), moving the deadline to t0+1.5. A full-window re-arm dispatches at
    ~t0+2.1; remaining-based re-arming dispatches at ~t0+1.55 — one margin past
    the deadline however the arm/feed timing falls out.
    """
    pack = RecordingPack()
    app = App(
        settings=Settings(),
        headless=True,
        log_dir=tmp_path,  # type: ignore[arg-type]
        pack=pack,
        audio_engine=FakeAudioEngine(),  # type: ignore[arg-type]
        stt=SlowSTT(delay=0.5),
    )
    app.mascot = FakeMascot()  # type: ignore[assignment]
    app.voice._window = ReceivingWindow(app.bus, datetime.now, close_after_s=1.0)
    app.start()
    try:
        began = time.monotonic()
        app.bus.emit(WakeDetected(score=0.9))
        app.bus.emit(_segment("open notepad"))
        await _wait_for(lambda: pack.calls == ["notepad"], timeout=8.0)
        dispatch_at = time.monotonic() - began
        assert dispatch_at < 1.85, (
            f"dispatch took {dispatch_at:.2f}s — the close tick must re-arm from the "
            "window's remaining time (a full-window re-arm lands ~2.1s)"
        )
    finally:
        app.stop()


async def test_abort_intent_discards_a_pending_window(tmp_path) -> None:
    """stop-everything mid-window must not leave the pending instruction to execute."""
    pack = RecordingPack()
    app = _app(tmp_path, pack)
    # Short window so the negative (nothing dispatched) is provable in real time;
    # the real clock still drives the loop's close timer.
    app.voice._window = ReceivingWindow(app.bus, datetime.now, close_after_s=0.3)
    app.start()
    try:
        app.bus.emit(_segment("open notepad"))  # collected, not dispatched
        app.bus.emit(_segment("stop everything"))  # abort family: discard the window
        await _wait_for(lambda: app.tts.stop_calls >= 1)
        await asyncio.sleep(0.7)  # well past the 0.3 s close deadline
        assert pack.calls == [], "an abort must discard the pending instruction"
    finally:
        app.stop()
