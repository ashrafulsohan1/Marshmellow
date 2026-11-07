"""Mic capture + silero VAD segmentation (01-architecture §3.1).

The segmentation state machine (`VADSegmenter`) is pure and testable on
injected speech probabilities; `AudioEngine` wires it to a real mic (or any
chunk source — `FixtureMic` for tests). The event loop is never touched:
capture runs on its own thread, segments go out via `emit_threadsafe`.

M6 Task 4 adds the half-duplex echo/media gate to the ingest path: the
engine polls an `IngestGate` once per unmuted chunk and either drops it
outright (TTS playing — hard half-duplex), feeds only the wake tap (no
window — the media guard: dropped before VAD), or ingests it fully (a
window is open). Entering DROP resets the segmenter; leaving FULL into
WAKE_ONLY with an utterance in flight drains it to its flush instead
(review fix — the 3 s window timeout must not eat a mid-utterance sentence).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from enum import Enum

import numpy as np

from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import SpeechSegment

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 512  # silero's native 32 ms frame
END_SILENCE_S = 0.7
MIN_SPEECH_S = 0.25
THRESHOLD = 0.5  # M0-SP3


class IngestMode(Enum):
    """What the capture thread may do with one mic chunk (M6 T4, §3.1).

    DROP — TTS playback is active: the hard half-duplex wall. No wake scoring
        (the tap is off too) and no VAD — the agent cannot hear anything, so
        it can never hear itself or the media it is speaking over.
    WAKE_ONLY — no receiving window: the media guard. Chunks still feed the
        wake tap (voice activation must stay able to open a window) but are
        dropped BEFORE VAD — media audio without wake is structurally unable
        to become STT input. (One bounded exception, the drain-to-flush: a
        window that closed mid-utterance keeps the VAD fed until that one
        in-flight utterance resolves — a sentence spoken past the 3 s timeout
        arrives late-but-complete instead of being lost.)
    FULL — a window is open: wake scoring + VAD + STT ingest.
    """

    DROP = "drop"
    WAKE_ONLY = "wake_only"
    FULL = "full"


class IngestGate:
    """The half-duplex + media-guard policy, probed once per chunk (M6 T4).

    The VoiceLoop binds the two policy inputs at construction — its TTS
    playback flag and its receiving window — and the capture thread polls
    `mode` per chunk. Probes instead of pushed state: the window's own lock
    makes `is_open` authoritative, so the gate cannot drift from the loop.
    Without a binding both probes read False — the fail-safe WAKE_ONLY
    posture (wake scoring stays live, nothing reaches the VAD).
    """

    def __init__(
        self,
        speaking: Callable[[], bool] | None = None,
        window_open: Callable[[], bool] | None = None,
    ) -> None:
        self._speaking = speaking if speaking is not None else (lambda: False)
        self._window_open = window_open if window_open is not None else (lambda: False)

    def bind(
        self, speaking: Callable[[], bool], window_open: Callable[[], bool]
    ) -> None:
        """Wire the policy probes (the VoiceLoop does this at construction)."""
        self._speaking = speaking
        self._window_open = window_open

    @property
    def mode(self) -> IngestMode:
        if self._speaking():
            return IngestMode.DROP  # hard half-duplex beats every other state
        if self._window_open():
            return IngestMode.FULL
        return IngestMode.WAKE_ONLY


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

    def reset(self) -> None:
        """Discard any in-flight speech state (M6 T4): the ingest gate entered
        the playback wall, so a partial buffer must never resurface and close
        as a segment stitched across the gap."""
        self._buffer.clear()
        self._speech_chunks = 0
        self._silence_chunks = 0
        self._in_speech = False

    @property
    def in_flight(self) -> bool:
        """An utterance is open: speech has been flagged since the last
        flush/reset (review fix — the window-timeout drain reads this)."""
        return self._in_speech


class AudioEngine:
    """Chunk source → VAD → SpeechSegments on the bus. Own thread, never blocks.

    `chunk_tap` (M5 Task 5, D8) receives every UNMUTED chunk before VAD — the
    bridge to the wake word watcher. Tap exceptions are logged and never kill
    the capture thread; muting stops wake detection too (§3.1).

    `ingest_gate` (M6 Task 4) is the half-duplex + media-guard policy, polled
    once per unmuted chunk: DROP (TTS playing — no tap, no VAD), WAKE_ONLY
    (no window — tap only, the chunk is dropped before VAD) or FULL (window
    open — tap + VAD). Entering DROP resets the segmenter, so audio the
    playback wall dropped never resurfaces as half a segment. Leaving FULL
    into WAKE_ONLY with an utterance in flight starts a drain-to-flush: the
    VAD stays fed until that utterance resolves at its end-silence, so a
    sentence spoken past the ordinary 3 s window timeout arrives
    late-but-complete instead of being silently lost (review fix). Without a
    gate the engine ingests everything (pre-M6 behavior — tests, injected
    engines).
    """

    def __init__(
        self,
        bus: EventBus,
        chunk_source: Iterator[np.ndarray] | None = None,
        vad: Callable[[np.ndarray], float] | None = None,
        sample_rate: int = SAMPLE_RATE,
        chunk_tap: Callable[[np.ndarray], None] | None = None,
        ingest_gate: IngestGate | None = None,
    ) -> None:
        self._bus = bus
        self._source = chunk_source or _mic_chunks(sample_rate, CHUNK_SAMPLES)
        self._segmenter = VADSegmenter(vad or silero_vad(), sample_rate)
        self._chunk_tap = chunk_tap
        self._ingest_gate = ingest_gate
        self._gate_mode = IngestMode.FULL  # the mode the segmenter last fed under
        self._draining = False  # window closed mid-utterance: feed VAD until it flushes
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
            mode = (
                self._ingest_gate.mode
                if self._ingest_gate is not None
                else IngestMode.FULL
            )
            if mode is not self._gate_mode:
                if mode is IngestMode.DROP:
                    # Entering the playback wall discards the in-flight
                    # utterance: audio the wall dropped must never resurface
                    # as half a segment later (pre-gate parity — such a
                    # segment would have been emitted mid-playback and
                    # dropped untranscribed by the loop).
                    self._segmenter.reset()
                    self._draining = False
                elif self._gate_mode is IngestMode.FULL and self._segmenter.in_flight:
                    # The ordinary window timeout closed the gate mid-utterance
                    # (a continuous sentence never hits the 0.7 s end-silence,
                    # so nothing has been fed): drain-to-flush instead of
                    # discarding — the sentence must arrive late-but-complete.
                    self._draining = True
                self._gate_mode = mode
            if mode is IngestMode.WAKE_ONLY and self._draining:
                mode = IngestMode.FULL  # drain: keep the VAD fed until the flush
            if mode is IngestMode.DROP:
                continue  # hard half-duplex: no tap (no wake scoring), no VAD
            self._tap_chunk(chunk)
            if mode is IngestMode.WAKE_ONLY:
                continue  # media guard: dropped BEFORE VAD
            for segment in self._segmenter.feed(chunk):
                self._bus.emit_threadsafe(segment)
            if self._draining and not self._segmenter.in_flight:
                self._draining = False  # flushed (or below min_speech): guard resumes

    def _tap_chunk(self, chunk: np.ndarray) -> None:
        if self._chunk_tap is None:
            return
        try:
            self._chunk_tap(chunk)
        except Exception:
            logger.exception("chunk tap raised; capture continues")
