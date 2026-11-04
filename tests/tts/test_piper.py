"""Piper TTS subprocess tests run on injected seams — no audio hardware, no GPL import."""

import io
import time
from pathlib import Path

import pytest

from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import (
    SpeakingStarted,
    SpeakingStopped,
    SpeakRequest,
)
from marshmellow.tts import piper as piper_mod
from marshmellow.tts.piper import PiperSpeaker, default_voice_model, make_wav


def test_default_voice_model_search_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MARSHMELLOW_VOICE_DIR", raising=False)
    monkeypatch.setattr(piper_mod, "REPO_ROOT", tmp_path)  # isolate from the real voices/
    voice = tmp_path / "voices" / "en_US-lessac-medium.onnx"
    assert default_voice_model("en_US-lessac-medium") is None  # nothing anywhere

    voice.parent.mkdir(parents=True)
    voice.write_bytes(b"onnx")
    found = default_voice_model("en_US-lessac-medium")
    assert found == voice  # repo voices/ dir

    env_dir = tmp_path / "envdir"
    env_dir.mkdir()
    (env_dir / "en_US-lessac-medium.onnx").write_bytes(b"onnx")
    monkeypatch.setenv("MARSHMELLOW_VOICE_DIR", str(env_dir))
    assert default_voice_model("en_US-lessac-medium") == env_dir / (
        "en_US-lessac-medium.onnx"
    )  # env override wins

    assert default_voice_model("en_US-missing-medium") is None


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


def test_quit_terminates_process_without_zombie() -> None:
    proc = FakeProc(b"")
    speaker = _speaker(EventBus(), proc, played=[])
    speaker.start()
    speaker.quit()
    assert proc.terminated
    assert proc.poll() == 0
    assert not speaker._worker.is_alive()


def test_stop_is_a_barge_in_kill_and_speaker_stays_usable() -> None:
    """§3.9: stop() silences playback now but must NOT shut the speaker down —
    the next say() after a barge-in still speaks (the old one-way shutdown made
    the first barge-in kill TTS for the whole session)."""
    bus = EventBus()
    stopped: list[bool] = []
    bus.subscribe(SpeakingStopped, lambda e: stopped.append(True))
    played: list[bytes] = []
    pcm = b"\x01\x00" * 500_000  # long utterance → plenty of chunks to barge into
    proc = FakeProc(make_wav(pcm, 22050))

    def slow_player(chunk: bytes) -> None:
        time.sleep(0.001)  # playback takes ~250ms, so stop() lands mid-utterance
        played.append(chunk)

    speaker = PiperSpeaker(
        bus=bus, spawn=lambda argv: proc, player=slow_player
    )
    speaker.start()
    speaker.say(SpeakRequest(text="a very long story"))
    assert _wait_for(lambda: len(played) > 5)

    speaker.stop()  # barge-in mid-utterance
    assert _wait_for(lambda: stopped), "the aborted utterance still emits SpeakingStopped"
    played_after_stop = len(played)
    time.sleep(0.05)
    assert len(played) == played_after_stop, "playback must halt after the barge-in"

    speaker.say(SpeakRequest(text="next"))  # the speaker survived the barge-in
    assert _wait_for(lambda: proc.stdin.getvalue().count(b"\n") == 2), (
        "the worker must pick up a new utterance after a barge-in"
    )
    speaker.quit()
    assert proc.terminated


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
