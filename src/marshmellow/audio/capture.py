"""Mic capture + silero VAD segmentation (01-architecture §3.1).

The segmentation state machine (`VADSegmenter`) is pure and testable on
injected speech probabilities; `AudioEngine` wires it to a real mic (or any
chunk source — `FixtureMic` for tests). The event loop is never touched:
capture runs on its own thread, segments go out via `emit_threadsafe`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta

import numpy as np

from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import SpeechSegment

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 512  # silero's native 32 ms frame
END_SILENCE_S = 0.7
MIN_SPEECH_S = 0.25
THRESHOLD = 0.5  # M0-SP3


def silero_vad() -> Callable[[np.ndarray], float]:
    """Real VAD adapter over faster-whisper's bundled onnx silero (no torch)."""
    from faster_whisper.vad import get_vad_model

    model = get_vad_model()

    def prob(chunk: np.ndarray) -> float:
        audio = chunk.astype(np.float32) / 32768.0
        return float(model(audio)[0])

    return prob


def _mic_chunks(sample_rate: int, chunk_samples: int) -> Iterator[np.ndarray]:
    import sounddevice as sd

    with sd.InputStream(
        samplerate=sample_rate, channels=1, dtype="int16", blocksize=chunk_samples
    ) as stream:
        while True:
            data, overflowed = stream.read(chunk_samples)
            if overflowed:
                logger.warning("mic buffer overflowed; dropping audio")
            yield data.reshape(-1)


class VADSegmenter:
    def __init__(
        self,
        vad: Callable[[np.ndarray], float],
        sample_rate: int = SAMPLE_RATE,
        chunk_samples: int = CHUNK_SAMPLES,
        end_silence_s: float = END_SILENCE_S,
        min_speech_s: float = MIN_SPEECH_S,
        threshold: float = THRESHOLD,
    ) -> None:
        self._vad = vad
        self._chunk_s = chunk_samples / sample_rate
        self._end_silence_chunks = max(1, round(end_silence_s / self._chunk_s))
        self._min_speech_chunks = max(1, round(min_speech_s / self._chunk_s))
        self._threshold = threshold
        self._buffer: list[np.ndarray] = []
        self._speech_chunks = 0
        self._silence_chunks = 0
        self._in_speech = False
        self._total = 0

    def feed(self, chunk: np.ndarray) -> list[SpeechSegment]:
        self._total += 1
        probability = self._vad(chunk)
        if probability >= self._threshold:
            self._in_speech = True
            self._speech_chunks += 1
            self._silence_chunks = 0
            self._buffer.append(chunk)
            return []
        if not self._in_speech:
            return []
        self._silence_chunks += 1
        self._buffer.append(chunk)
        if self._silence_chunks >= self._end_silence_chunks:
            closed = self._flush()
            self._in_speech = False
            return closed
        return []

    def _flush(self) -> list[SpeechSegment]:
        buffer, self._buffer = self._buffer, []
        speech_chunks, self._speech_chunks = self._speech_chunks, 0
        self._silence_chunks = 0
        if speech_chunks < self._min_speech_chunks:
            return []
        pcm = np.concatenate(buffer).astype(np.int16).tobytes()
        ended = datetime.now()
        started = ended - timedelta(seconds=len(buffer) * self._chunk_s)
        return [SpeechSegment(pcm=pcm, sample_rate=SAMPLE_RATE,
                              started_at=started, ended_at=ended)]


class AudioEngine:
    """Chunk source → VAD → SpeechSegments on the bus. Own thread, never blocks."""

    def __init__(
        self,
        bus: EventBus,
        chunk_source: Iterator[np.ndarray] | None = None,
        vad: Callable[[np.ndarray], float] | None = None,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        self._bus = bus
        self._source = chunk_source or _mic_chunks(sample_rate, CHUNK_SAMPLES)
        self._segmenter = VADSegmenter(vad or silero_vad(), sample_rate)
        self._muted = False
        self._alive = False
        self._thread = threading.Thread(target=self._loop, name="audio", daemon=True)

    def start(self) -> None:
        self._alive = True
        self._thread.start()

    def stop(self) -> None:
        self._alive = False
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def set_muted(self, muted: bool) -> None:
        self._muted = muted  # echo guard / tray mute drop everything

    def _loop(self) -> None:
        for chunk in self._source:
            if not self._alive or self._muted:
                if not self._alive:
                    return
                continue
            for segment in self._segmenter.feed(chunk):
                self._bus.emit_threadsafe(segment)
