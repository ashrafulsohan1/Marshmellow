"""Piper TTS subprocess tests run on injected seams — no audio hardware, no GPL import."""

import io
import time

from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import (
    SpeakingStarted,
    SpeakingStopped,
    SpeakRequest,
)
from marshmellow.tts.piper import PiperSpeaker, make_wav


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeProc:
    """Subprocess stand-in: records stdin, replays canned WAV on stdout."""

    def __init__(self, wav: bytes) -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(wav)
        self.terminated = False
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = 0


def _speaker(bus: EventBus, proc: FakeProc, played: list[bytes]) -> PiperSpeaker:
    return PiperSpeaker(
        bus=bus,
        spawn=lambda argv: proc,
        player=lambda chunk: played.append(chunk),
    )


def test_say_streams_pcm_and_publishes_events() -> None:
    bus = EventBus()
    events: list[type] = []
    bus.subscribe(SpeakingStarted, lambda e: events.append(SpeakingStarted))
    bus.subscribe(SpeakingStopped, lambda e: events.append(SpeakingStopped))
    played: list[bytes] = []
    pcm = b"\x01\x00" * 100
    proc = FakeProc(make_wav(pcm, 22050))
    speaker = _speaker(bus, proc, played)

    speaker.start()
    speaker.say(SpeakRequest(text="On it."))
    assert _wait_for(lambda: SpeakingStopped in events)

    assert proc.stdin.getvalue() == b"On it.\n"
    assert played == [pcm]
    assert events == [SpeakingStarted, SpeakingStopped]
    speaker.stop()


def test_stop_terminates_process_without_zombie() -> None:
    proc = FakeProc(b"")
    speaker = _speaker(EventBus(), proc, played=[])
    speaker.start()
    speaker.stop()
    assert proc.terminated
    assert proc.poll() == 0
    assert not speaker._worker.is_alive()


def test_interrupt_clears_pending_before_start() -> None:
    speaker = _speaker(EventBus(), FakeProc(b""), played=[])
    speaker.say(SpeakRequest(text="first", priority="result"))
    speaker.say(SpeakRequest(text="urgent", priority="ack", interrupt=True))
    assert speaker.pending_texts() == ["urgent"]


def test_priority_ordering() -> None:
    speaker = _speaker(EventBus(), FakeProc(b""), played=[])
    speaker.say(SpeakRequest(text="low", priority="background"))
    speaker.say(SpeakRequest(text="mid", priority="result"))
    speaker.say(SpeakRequest(text="high", priority="ack"))
    assert speaker.pending_texts() == ["high", "mid", "low"]
