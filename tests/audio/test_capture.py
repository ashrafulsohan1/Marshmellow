"""Capture/VAD tests on injected probabilities and synthetic chunks — no mic, no model."""

import logging
import threading
import wave
from pathlib import Path

import numpy as np
import pytest

from marshmellow.audio.capture import AudioEngine, VADSegmenter
from marshmellow.audio.fixtures import FixtureMic
from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import SpeechSegment

CHUNK = 512
SR = 16000
# end-silence 0.7 s -> round(0.7 / (512/16000)) = 22 chunks


def _chunk() -> np.ndarray:
    return np.zeros(CHUNK, dtype=np.int16)


def test_bursts_segment_on_end_silence() -> None:
    probs = [0.9] * 16 + [0.0] * 30 + [0.9] * 16  # speech, >0.7 s gap, speech
    seg = VADSegmenter(vad=lambda c: probs.pop(0))
    segments: list[SpeechSegment] = []
    for _ in range(16 + 30 + 16):
        segments.extend(seg.feed(_chunk()))
    assert len(segments) == 1
    # first utterance = 16 speech chunks + 22 trailing-silence chunks at close
    assert len(segments[0].pcm) == (16 + 22) * CHUNK * 2
    assert segments[0].sample_rate == SR


def test_ignores_too_short_speech() -> None:
    probs = [0.9] * 5 + [0.0] * 30  # ~0.16 s < min speech 0.25 s
    seg = VADSegmenter(vad=lambda c: probs.pop(0))
    segments: list[SpeechSegment] = []
    for _ in range(35):
        segments.extend(seg.feed(_chunk()))
    assert segments == []


def test_speech_at_end_stays_open() -> None:
    probs = [0.9] * 16
    seg = VADSegmenter(vad=lambda c: probs.pop(0))
    segments: list[SpeechSegment] = []
    for _ in range(16):
        segments.extend(seg.feed(_chunk()))
    assert segments == []  # closes only after end-silence


def test_engine_emits_segments_on_bus() -> None:
    bus = EventBus()
    got: list[SpeechSegment] = []
    done = threading.Event()
    bus.subscribe(SpeechSegment, lambda s: (got.append(s), done.set()))
    probs = iter([0.9] * 16 + [0.0] * 30)
    engine = AudioEngine(
        bus=bus,
        chunk_source=iter([_chunk() for _ in range(16 + 30)]),
        vad=lambda c: next(probs),
    )
    engine.start()
    assert done.wait(timeout=5.0)
    engine.stop()
    assert len(got) == 1
    assert got[0].sample_rate == SR


def test_muted_engine_drops_everything() -> None:
    bus = EventBus()
    got: list[SpeechSegment] = []
    bus.subscribe(SpeechSegment, got.append)
    probs = iter([0.9] * 16 + [0.0] * 30)
    engine = AudioEngine(
        bus=bus,
        chunk_source=iter([_chunk() for _ in range(16 + 30)]),
        vad=lambda c: next(probs),
    )
    engine.set_muted(True)
    engine.start()
    engine.stop()  # finite source: thread drains and exits
    assert got == []


def test_engine_tap_receives_unmuted_chunks_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M5 Task 5 (D8): the chunk tap feeds the wake word bridge — once per
    UNMUTED chunk, before VAD (mute stops wake too, §3.1); a raising tap is
    logged and never kills the capture thread."""
    bus = EventBus()
    source = [_chunk() for _ in range(3)]
    tapped: list[np.ndarray] = []
    probs = iter([0.0] * 3)  # no speech → no segments; the tap still sees chunks
    engine = AudioEngine(
        bus=bus, chunk_source=iter(source), vad=lambda c: next(probs),
        chunk_tap=tapped.append,
    )
    engine.start()
    engine.stop()  # finite source: thread drains and exits
    assert len(tapped) == len(source)
    assert all(t is s for t, s in zip(tapped, source, strict=False))  # same chunks, in order

    tapped.clear()
    engine = AudioEngine(
        bus=bus, chunk_source=iter([_chunk() for _ in range(3)]),
        vad=lambda c: 0.0, chunk_tap=tapped.append,
    )
    engine.set_muted(True)  # mute stops wake too: the tap sees nothing
    engine.start()
    engine.stop()
    assert tapped == []

    def boom(_chunk: np.ndarray) -> None:
        raise RuntimeError("tap boom")

    got: list[SpeechSegment] = []
    done = threading.Event()
    bus.subscribe(SpeechSegment, lambda s: (got.append(s), done.set()))
    probs2 = iter([0.9] * 16 + [0.0] * 30)
    with caplog.at_level(logging.ERROR, logger="marshmellow.audio.capture"):
        engine = AudioEngine(
            bus=bus, chunk_source=iter([_chunk() for _ in range(16 + 30)]),
            vad=lambda c: next(probs2), chunk_tap=boom,
        )
        engine.start()
        assert done.wait(timeout=5.0), "capture thread died with the tap"
        engine.stop()
    assert len(got) == 1  # the segment still reached the bus
    tap_logs = [r for r in caplog.records if "tap" in r.message.lower()]
    assert tap_logs and tap_logs[0].exc_info is not None


def test_fixture_mic_replays_wav_chunks(tmp_path: Path) -> None:
    wav = tmp_path / "phrase.wav"
    pcm = np.zeros(32 * CHUNK, dtype=np.int16)  # 16384 samples: exact 512-chunks
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    chunks = list(FixtureMic([wav]).chunks())
    assert len(chunks) == 32
    assert all(len(c) == CHUNK for c in chunks)
