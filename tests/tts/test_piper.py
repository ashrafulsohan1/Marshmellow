"""Piper TTS subprocess tests run on injected seams — no audio hardware, no GPL import."""

import io
import struct
import sys
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
from marshmellow.tts.piper import PiperSpeaker, default_voice_model, worker_argv


def make_stream(sample_rate: int, *utterances: bytes) -> bytes:
    """Bytes a piper_worker subprocess would write for these utterances."""
    out = bytearray()
    for pcm in utterances:
        out += struct.pack("<I", sample_rate)
        out += struct.pack("<I", len(pcm)) + pcm
        out += struct.pack("<I", 0)
    return bytes(out)


def test_default_voice_model_search_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MARSHMELLOW_VOICE_DIR", raising=False)
    monkeypatch.setattr(piper_mod, "REPO_ROOT", tmp_path)  # isolate from the real voices/
    # The packaged-voice entry (M5 T7: _internal/voices in a frozen build) is
    # patched out here — the dev checkout's real voices/ dir must not leak in.
    monkeypatch.setattr(piper_mod, "default_voice_dir", lambda: None, raising=False)
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


def test_worker_defers_voice_load_to_first_utterance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5 T7 tuning lever (idle-RSS): the resident worker must NOT load the
    voice at spawn — PiperVoice.load happens once, on the first stdin line."""
    import io
    import types

    import marshmellow.tts.piper_worker as pw

    loads = 0

    class FakeVoice:
        def __init__(self) -> None:
            self.config = types.SimpleNamespace(sample_rate=22050)

        @classmethod
        def load(cls, model: Path) -> "FakeVoice":
            nonlocal loads
            loads += 1
            return cls()

        def synthesize(self, text: str):  # type: ignore[no-untyped-def]
            yield types.SimpleNamespace(audio_int16_bytes=b"\x01\x00" * 4)

    model_file = tmp_path / "voice.onnx"
    model_file.write_bytes(b"onnx")
    out_buf = io.BytesIO()
    monkeypatch.setattr(pw, "sys", types.SimpleNamespace(
        argv=["piper_worker", str(model_file)],
        stdin=io.StringIO("hello\n"),
        stdout=types.SimpleNamespace(buffer=out_buf),
        stderr=io.StringIO(),
    ))
    monkeypatch.setattr("piper.PiperVoice", FakeVoice)

    assert pw.main() == 0
    assert loads == 1, "the voice loads exactly once, on the first utterance"
    # framing intact: rate header + one chunk + terminator
    assert out_buf.getvalue() == struct.pack("<I", 22050) + struct.pack("<I", 8) + (
        b"\x01\x00" * 4
    ) + struct.pack("<I", 0)


def test_default_voice_model_bundled_dir_searched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5 T7: the packaged voice dir (asset_dir()/voices) is a search entry
    between the env override and the dev checkout's voices/."""
    monkeypatch.delenv("MARSHMELLOW_VOICE_DIR", raising=False)
    monkeypatch.setattr(piper_mod, "REPO_ROOT", tmp_path)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    voice = bundled / "en_US-lessac-medium.onnx"
    voice.write_bytes(b"onnx")
    monkeypatch.setattr(piper_mod, "default_voice_dir", lambda: bundled, raising=False)
    assert default_voice_model("en_US-lessac-medium") == voice

    env_dir = tmp_path / "envdir"
    env_dir.mkdir()
    (env_dir / "en_US-lessac-medium.onnx").write_bytes(b"onnx")
    monkeypatch.setenv("MARSHMELLOW_VOICE_DIR", str(env_dir))
    assert default_voice_model("en_US-lessac-medium") == env_dir / (
        "en_US-lessac-medium.onnx"
    )  # env override still wins over the bundle


def test_worker_argv_dev_branch() -> None:
    """M5 T7 (Review Focus 1): dev launches the worker as a module run of the
    current interpreter; no model → no trailing path (the worker prints usage)."""
    model = Path("voices/en_US-lessac-medium.onnx")
    assert worker_argv(None, model) == [
        sys.executable,
        "-m",
        "marshmellow.tts.piper_worker",
        str(model),
    ]
    assert worker_argv(None, None) == [sys.executable, "-m", "marshmellow.tts.piper_worker"]
    assert worker_argv(Path("py.exe"), None) == [
        "py.exe",
        "-m",
        "marshmellow.tts.piper_worker",
    ]


def test_worker_argv_frozen_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """M5 T7 (Review Focus 1): a frozen build cannot run `python -m` — the
    worker is the spec's second exe sitting next to Marshmellow.exe (dist root),
    and the model path (when known) is the only argument."""
    exe = Path(r"C:\apps\Marshmellow\Marshmellow.exe")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    model = exe.parent / "_internal" / "voices" / "en_US-lessac-medium.onnx"
    argv = worker_argv(None, model)
    assert argv[0] == str(exe.with_name("piper_worker.exe"))
    assert argv[1:] == [str(model)]

    assert worker_argv(None, None) == [str(exe.with_name("piper_worker.exe"))]


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
    proc = FakeProc(make_stream(22050, pcm))
    speaker = _speaker(bus, proc, played)

    speaker.start()
    speaker.say(SpeakRequest(text="On it."))
    assert _wait_for(lambda: SpeakingStopped in events)

    assert proc.stdin.getvalue() == b"On it.\n"
    assert played == [pcm]
    assert events == [SpeakingStarted, SpeakingStopped]
    speaker.stop()


def test_worker_stream_advertises_sample_rate() -> None:
    """The protocol header must reach the speaker: a 16 kHz voice plays at 16 kHz."""
    bus = EventBus()
    played: list[bytes] = []
    pcm = b"\x02\x00" * 50
    speaker = PiperSpeaker(
        bus=bus, spawn=lambda _argv: FakeProc(make_stream(16000, pcm)),
        player=played.append,
    )
    speaker.start()
    speaker.say(SpeakRequest(text="test"))
    assert _wait_for(lambda: bool(played))
    assert speaker._sample_rate == 16000
    speaker.quit()


def test_resident_stream_speaks_one_utterance_per_say() -> None:
    """Regression (real stdout never EOFs): one say() must consume exactly one
    utterance and emit SpeakingStopped — never block pulling the next one."""
    bus = EventBus()
    stopped: list[bool] = []
    bus.subscribe(SpeakingStopped, lambda e: stopped.append(True))
    played: list[bytes] = []
    pcm1, pcm2 = b"\x01\x00" * 100, b"\x02\x00" * 100
    speaker = PiperSpeaker(
        bus=bus, spawn=lambda _argv: FakeProc(make_stream(22050, pcm1, pcm2)),
        player=played.append,
    )
    speaker.start()
    speaker.say(SpeakRequest(text="first"))
    assert _wait_for(lambda: len(stopped) == 1), "first utterance must complete alone"
    assert played == [pcm1]
    speaker.say(SpeakRequest(text="second"))
    assert _wait_for(lambda: len(stopped) == 2), "second utterance must complete"
    assert played == [pcm1, pcm2]
    speaker.quit()


def test_multiline_text_becomes_one_protocol_line() -> None:
    """Regression (review defect): an LLM reply with embedded newlines used to be
    written verbatim, so the worker synthesized one utterance PER LINE while the
    speaker consumed exactly one — the next say() played the leftover line's
    audio and one utterance was never spoken."""
    bus = EventBus()
    played: list[bytes] = []
    pcm1, pcm2 = b"\x01\x00" * 100, b"\x02\x00" * 100
    proc = FakeProc(make_stream(22050, pcm1, pcm2))
    speaker = PiperSpeaker(bus=bus, spawn=lambda _argv: proc, player=played.append)
    speaker.start()

    speaker.say(SpeakRequest(text="Result line one.\nResult line two."))
    assert _wait_for(lambda: len(played) == 1)
    assert proc.stdin.getvalue() == b"Result line one. Result line two.\n", (
        "multi-line text must collapse to a single protocol line"
    )

    speaker.say(SpeakRequest(text="On it."))
    assert _wait_for(lambda: len(played) == 2)
    assert played == [pcm1, pcm2], "no leftover utterance may desync the next say()"
    speaker.quit()


def test_blank_text_writes_nothing_and_stays_framed() -> None:
    """Whitespace-only text must not reach the worker: a blank stdin line makes
    the worker emit no utterance at all, which would block the stream read
    forever and deafen every later say()."""
    bus = EventBus()
    started: list[bool] = []
    bus.subscribe(SpeakingStarted, lambda e: started.append(True))
    played: list[bytes] = []
    pcm = b"\x01\x00" * 50
    proc = FakeProc(make_stream(22050, pcm))
    speaker = PiperSpeaker(bus=bus, spawn=lambda _argv: proc, player=played.append)
    speaker.start()

    speaker.say(SpeakRequest(text="  \n\t "))
    time.sleep(0.05)  # give the worker a beat to (not) act
    assert proc.stdin.getvalue() == b""
    assert not started and not played, "nothing speakable → nothing spoken"

    speaker.say(SpeakRequest(text="On it."))
    assert _wait_for(lambda: len(played) == 1), "stream stays framed after a blank"
    speaker.quit()


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
    proc = FakeProc(make_stream(22050, pcm))

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
