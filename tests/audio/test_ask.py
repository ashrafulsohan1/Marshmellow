"""AskUserChannel tests — spoken question → next bus SpeechSegment's transcript."""

import threading
import time
from collections.abc import Callable
from datetime import datetime

from marshmellow.audio.ask import AskUserChannel
from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import SpeechSegment
from marshmellow.stt.fake import FakeSTT
from marshmellow.tts.fake import FakeTTS


def _segment(text: str) -> SpeechSegment:
    now = datetime.now()
    return SpeechSegment(pcm=text.encode("utf-8"), started_at=now, ended_at=now)


def _raw_segment(pcm: bytes) -> SpeechSegment:
    now = datetime.now()
    return SpeechSegment(pcm=pcm, started_at=now, ended_at=now)


class FakeMascot:
    def __init__(self) -> None:
        self.states: list[str] = []

    def set_state(self, state: str) -> None:
        self.states.append(state)


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Condition-bounded wait for cross-thread handoffs (never a fixed sleep)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _channel(bus: EventBus, tts: FakeTTS, mascot: FakeMascot | None = None,
             timeout_s: float = 20.0) -> AskUserChannel:
    return AskUserChannel(tts=tts, stt=FakeSTT(), bus=bus, mascot=mascot, timeout_s=timeout_s)


def test_ask_speaks_the_question_then_returns_next_transcript() -> None:
    bus = EventBus()
    tts = FakeTTS()
    mascot = FakeMascot()
    channel = _channel(bus, tts, mascot)
    results: list[str] = []

    def run() -> None:
        results.append(channel.ask("Delete 6 items — confirm?"))

    thread = threading.Thread(target=run)
    thread.start()
    assert _wait_for(lambda: bool(tts.spoken)), "the question must be spoken (ack priority)"
    assert tts.spoken[0].text == "Delete 6 items — confirm?"
    assert tts.spoken[0].priority == "ack"
    assert "listening" in mascot.states, "the mascot must drop to listening"

    bus.emit(_segment("yes"))
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert results == ["yes"]
    bus.stop()


def test_ask_ignores_empty_transcripts_until_a_real_answer() -> None:
    bus = EventBus()
    tts = FakeTTS()
    channel = _channel(bus, tts)
    results: list[str] = []

    def run() -> None:
        results.append(channel.ask("confirm?"))

    thread = threading.Thread(target=run)
    thread.start()
    assert _wait_for(lambda: bool(tts.spoken))
    bus.emit(_raw_segment(b"\xff\xfe\xfa"))  # invalid UTF-8 → empty transcript: noise
    bus.emit(_segment("no"))  # deterministic in either interleaving: noise is skipped
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert results == ["no"]
    bus.stop()


def test_ask_times_out_with_empty_string() -> None:
    bus = EventBus()
    channel = _channel(bus, FakeTTS(), timeout_s=0.2)
    results: list[str] = []

    def run() -> None:
        results.append(channel.ask("confirm?"))

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert results == [""]  # timeout → "" → the ConfirmFlow fails closed
    bus.stop()


def test_ask_kill_during_wait_aborts_promptly() -> None:
    """A kill during the 20 s wait must abort the ask within ~250 ms (§3.13):
    the kill probe is polled between wait slices, TaskManager.current_kill is
    the composition-time wiring (M2's pre-action check stays the second net)."""
    bus = EventBus()
    tts = FakeTTS()
    channel = _channel(bus, tts)
    kill = threading.Event()
    results: list[str] = []

    def run() -> None:
        results.append(channel.ask("confirm?", cancelled=kill.is_set))

    thread = threading.Thread(target=run)
    thread.start()
    assert _wait_for(lambda: bool(tts.spoken)), "the ask must be waiting when the kill lands"
    kill.set()
    start = time.monotonic()
    thread.join(timeout=2.0)
    elapsed = time.monotonic() - start
    assert not thread.is_alive()
    assert results == [""]
    assert elapsed < 1.0, f"kill must abort the ask promptly, took {elapsed:.3f}s"
    bus.stop()


def test_ask_kill_before_wait_returns_empty_string_immediately() -> None:
    bus = EventBus()
    channel = _channel(bus, FakeTTS())
    start = time.monotonic()
    assert channel.ask("confirm?", cancelled=lambda: True) == ""
    assert time.monotonic() - start < 1.0
    bus.stop()


def test_ask_restores_the_mascot_after_the_answer() -> None:
    bus = EventBus()
    tts = FakeTTS()
    mascot = FakeMascot()
    channel = _channel(bus, tts, mascot)
    results: list[str] = []

    def run() -> None:
        results.append(channel.ask("confirm?"))

    thread = threading.Thread(target=run)
    thread.start()
    assert _wait_for(lambda: bool(tts.spoken))
    bus.emit(_segment("yes"))
    thread.join(timeout=5.0)
    assert results == ["yes"]
    assert mascot.states[-1] == "idle"
    bus.stop()
