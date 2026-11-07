"""Echo/media guard (M6 T4) — the hard half-duplex ingest gate, measured.

While TTS plays (`SpeakingStarted…Stopped`) the ENTIRE ingest path is gated
off — no wake scoring, no VAD, no STT, no window opening (hard half-duplex,
plan option (a)). With no receiving window open, every chunk is dropped
BEFORE VAD — media audio without wake is structurally unable to become
input. Voice barge-in during playback is REMOVED (own-voice safety: without
AEC the app cannot distinguish its own speech from the user's); the hotkey
is the only barge-in — it kills TTS and opens a fresh window.

Three fixtures drive the REAL ingest path end-to-end (chunk source →
AudioEngine(ingest_gate) → VAD → bus → VoiceLoop → STT → TaskManager), with
every stage counted so the evidence table's numbers are measured, not
asserted. The negative proofs are structural: the scripted VAD flags every
chunk that REACHES it as speech, so any gate leak would surface as a
segment — zero feeds means zero ingestion, not content luck.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from marshmellow.app import App
from marshmellow.audio.activation import ChunkQueue, WakeWordWatcher
from marshmellow.audio.capture import (
    CHUNK_SAMPLES,
    SAMPLE_RATE,
    AudioEngine,
    IngestGate,
    IngestMode,
)
from marshmellow.audio.fixtures import FixtureMic
from marshmellow.audio.turn_taking import WindowState
from marshmellow.core.bus import EventBus
from marshmellow.core.config import Settings
from marshmellow.core.contracts import (
    HotkeyPressed,
    SpeakingStarted,
    SpeakingStopped,
    SpeechSegment,
    Transcript,
    WakeDetected,
)
from marshmellow.packs.base import ShellResult
from marshmellow.stt.fake import FakeSTT

REPO = Path(__file__).resolve().parents[2]
PIPER_VOICE_WAV = REPO / "assets" / "wake" / "fixture-hey-marshmellow.wav"

# -- measuring doubles -----------------------------------------------------------


class CountingSTT(FakeSTT):
    """FakeSTT + a transcription counter (the measured column: STT calls)."""

    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, segment: SpeechSegment) -> Transcript:
        self.calls += 1
        return super().transcribe(segment)


class ScriptedVAD:
    """VAD double: counts feeds; any chunk that REACHES the VAD is speech.

    `speech_chunks` scripts how many feeds score 1.0 before silence (0.0) —
    used by the positive control so a real segment flushes. The zero tests
    assert `feeds == 0`: structural proof the gate dropped every chunk.
    """

    def __init__(self, speech_chunks: int = 0) -> None:
        self.speech_chunks = speech_chunks
        self.feeds = 0

    def __call__(self, chunk: np.ndarray) -> float:
        self.feeds += 1
        return 1.0 if self.feeds <= self.speech_chunks else 0.0


class CountingSource:
    """Counts chunks yielded from a materialized list; the FIRST next() blocks
    until the test arms it, so the gate state is set before any audio flows."""

    def __init__(self, chunks: list[np.ndarray]) -> None:
        self.total = len(chunks)
        self._it = iter(chunks)
        self.yielded = 0
        self.armed = threading.Event()

    def __iter__(self) -> Iterator[np.ndarray]:
        return self

    def __next__(self) -> np.ndarray:
        if self.yielded == 0:
            self.armed.wait(timeout=10.0)
        chunk = next(self._it)  # StopIteration must not count as a yield
        self.yielded += 1
        return chunk


def _music_chunks() -> list[np.ndarray]:
    """Music-shaped audio: an A-major chord (220/277/330 Hz) with a 2 Hz
    tremolo — tonal, continuous, the shape a media player emits. 2.0 s @ 16 kHz."""
    duration_s = 2.0
    t = np.arange(int(duration_s * SAMPLE_RATE), dtype=np.float64) / SAMPLE_RATE
    chord = (
        np.sin(2 * np.pi * 220.0 * t)
        + np.sin(2 * np.pi * 277.18 * t)
        + np.sin(2 * np.pi * 329.63 * t)
    )
    tremolo = 0.5 * (1.0 + np.sin(2 * np.pi * 2.0 * t))
    pcm = (chord * tremolo / 3.0 * 26000).astype(np.int16)
    return [pcm[start : start + CHUNK_SAMPLES] for start in range(0, len(pcm), CHUNK_SAMPLES)]


class ScriptedWakeScorer:
    """WakeScorer double: counts scored frames; media music scores 0.0."""

    def __init__(self) -> None:
        self.frames = 0

    def predict(self, frame: np.ndarray) -> dict[str, float]:
        self.frames += 1
        return {"media_music": 0.0}


# -- the end-to-end capture harness ----------------------------------------------


class RecordingPack:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def open_app(self, target: str) -> ShellResult:
        self.calls.append(target)
        return ShellResult(ok=True, detail=f"would open {target}")


class FakeMascot:
    def __init__(self) -> None:
        self.state = "idle"

    def show(self) -> None:
        return None

    def set_state(self, state: str) -> None:
        self.state = state

    def close(self) -> None:
        return None


class _CaptureHarness:
    """The real ingest path on a headless App: a REAL AudioEngine wired to the
    app's ingest gate, a scripted VAD, the FakeSTT, the TaskManager — with
    every stage counted. `with_wake_queue` replicates the D8 mic sharing: the
    engine's chunk_tap feeds a ChunkQueue a wake watcher drains."""

    def __init__(
        self,
        tmp_path: Path,
        chunks: list[np.ndarray],
        *,
        vad: ScriptedVAD | None = None,
        with_wake_queue: bool = False,
    ) -> None:
        self.pack = RecordingPack()
        self.stt = CountingSTT()
        self.vad = vad if vad is not None else ScriptedVAD()
        self.app = App(
            settings=Settings(),
            headless=True,
            log_dir=tmp_path,
            pack=self.pack,
            stt=self.stt,
        )
        self.app.mascot = FakeMascot()  # type: ignore[assignment]
        self.source = CountingSource(chunks)
        self.tapped = 0
        self.segments: list[SpeechSegment] = []
        self.wake_events: list[WakeDetected] = []
        self.app.bus.subscribe(SpeechSegment, self.segments.append)
        self.app.bus.subscribe(WakeDetected, self.wake_events.append)
        self.tap_queue: ChunkQueue | None = None
        chunk_tap: Callable[[np.ndarray], None] | None = self._count_tap
        if with_wake_queue:
            self.tap_queue = ChunkQueue()
            chunk_tap = self._tap_into_queue
        self.engine = AudioEngine(
            bus=self.app.bus,
            chunk_source=self.source,
            vad=self.vad,
            ingest_gate=self.app.ingest_gate,  # the loop bound its probes at construction
            chunk_tap=chunk_tap,
        )
        self.app.audio_engine = self.engine

    def _count_tap(self, chunk: np.ndarray) -> None:
        self.tapped += 1

    def _tap_into_queue(self, chunk: np.ndarray) -> None:
        self.tapped += 1
        assert self.tap_queue is not None
        self.tap_queue(chunk)

    def stop(self) -> None:
        self.app.stop()
        if self.tap_queue is not None:
            self.tap_queue.close()


async def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


# -- (i) TTS-on-mic fixture: own voice through the mic while speaking ------------


@pytest.mark.scenario
async def test_own_voice_playing_through_the_mic_ingests_zero_while_speaking(
    tmp_path: Path,
) -> None:
    """The piper output WAV (the assistant's own synthesized voice) plays
    through the mic path while SpeakingStarted is active → zero chunks reach
    the tap or the VAD, zero transcriptions, zero actions, zero windows."""
    chunks = list(FixtureMic([PIPER_VOICE_WAV]).chunks())
    assert len(chunks) > 30, "fixture must be a real phrase, not a blip"
    h = _CaptureHarness(tmp_path, chunks)
    h.app.start()
    try:
        h.app.bus.emit(SpeakingStarted())  # synchronous dispatch: the loop is speaking NOW
        h.source.armed.set()
        await _wait_for(lambda: h.source.yielded >= h.source.total)
        await asyncio.sleep(0.1)  # any leak would surface on the bus drain thread

        # measured: 0 at every stage past the gate (evidence table, row T1)
        assert h.source.yielded == h.source.total
        assert h.tapped == 0, "no wake scoring while speaking (hard half-duplex)"
        assert h.vad.feeds == 0, "every chunk dropped before VAD"
        assert h.segments == []
        assert h.stt.calls == 0, "zero transcriptions"
        assert h.pack.calls == [], "zero actions"
        assert h.wake_events == []
        assert h.app.voice._window.state is WindowState.DORMANT, "no window opening"
    finally:
        h.stop()


@pytest.mark.scenario
async def test_own_voice_through_the_mic_ingests_when_not_speaking(
    tmp_path: Path,
) -> None:
    """Paired control for the zero: the SAME WAV through the SAME path with
    playback off and a window open (gate FULL) ingests — the zeros above are
    the gate's doing, not a broken fixture path."""
    chunks = list(FixtureMic([PIPER_VOICE_WAV]).chunks())
    # 12 speech chunks (> the 0.25 s minimum) leaves enough of the ~1 s fixture
    # for the 0.7 s end-silence (22 chunks) that flushes the segment.
    vad = ScriptedVAD(speech_chunks=12)
    h = _CaptureHarness(tmp_path, chunks, vad=vad)
    h.app.start()
    try:
        h.app.bus.emit(WakeDetected(score=0.9))  # window opens → gate FULL
        h.source.armed.set()
        await _wait_for(lambda: h.source.yielded >= h.source.total)
        await _wait_for(lambda: h.stt.calls >= 1)

        # measured: the same audio, gate open (evidence table, row T2)
        assert h.vad.feeds == h.source.total, "every chunk reached the VAD"
        assert h.segments, "the phrase became a segment"
        assert h.stt.calls >= 1, "STT ran"
    finally:
        h.stop()


# -- (ii) media fixture: music-shaped audio, no window ---------------------------


@pytest.mark.scenario
async def test_media_audio_without_a_window_never_becomes_input(tmp_path: Path) -> None:
    """Music-shaped audio with no window: wake scoring stays LIVE (the tap
    sees every chunk and the real WakeWordWatcher scores frames) but every
    chunk is dropped BEFORE VAD — zero segments, zero transcriptions, zero
    actions, zero windows."""
    h = _CaptureHarness(tmp_path, _music_chunks(), with_wake_queue=True)
    assert h.tap_queue is not None
    scorer = ScriptedWakeScorer()
    watcher = WakeWordWatcher(
        h.app.bus,
        model_dir=REPO / "assets" / "wake",  # unused: the factory is scripted
        chunk_source=iter(h.tap_queue),
        scorer_factory=lambda _dir: scorer,
    )
    watcher.start()
    h.app.start()
    try:
        h.source.armed.set()
        await _wait_for(lambda: h.source.yielded >= h.source.total)
        await _wait_for(lambda: scorer.frames >= 1)  # the watcher scored the music
        await asyncio.sleep(0.1)  # any leak would surface on the bus drain thread

        # measured (evidence table, rows M1–M5)
        assert h.tapped == h.source.total, "wake scoring stayed live"
        assert scorer.frames >= 1, "frames really were scored"
        assert h.wake_events == [], "zero wake scorings ≥ threshold"
        assert h.vad.feeds == 0, "every chunk dropped BEFORE VAD — the media guard"
        assert h.segments == []
        assert h.stt.calls == 0, "media audio structurally unable to become input"
        assert h.pack.calls == []
        assert h.app.voice._window.state is WindowState.DORMANT, "zero windows"
    finally:
        h.tap_queue.close()
        watcher.stop()
        h.stop()


# -- (iii) hotkey during playback: the barge-in escape ---------------------------


def _loop_app(tmp_path: Path, pack: RecordingPack, stt: CountingSTT | None = None) -> App:
    app = App(
        settings=Settings(),
        headless=True,
        log_dir=tmp_path,
        pack=pack,
        stt=stt if stt is not None else CountingSTT(),
    )
    app.mascot = FakeMascot()  # type: ignore[assignment]
    return app


def _segment(text: str) -> SpeechSegment:
    """FakeSTT fixture encoding: the UTF-8 text rides in `pcm`."""
    now = datetime.now()
    return SpeechSegment(pcm=text.encode("utf-8"), started_at=now, ended_at=now)


@pytest.mark.scenario
async def test_voice_barge_in_is_removed_segment_during_playback_is_dropped(
    tmp_path: Path,
) -> None:
    """Own-voice safety (M6 T4): a SpeechSegment during playback is dropped
    untranscribed — TTS keeps playing, nothing is cancelled, no window opens.
    Without AEC the app cannot tell its own speech from the user's."""
    pack = RecordingPack()
    stt = CountingSTT()
    app = _loop_app(tmp_path, pack, stt)
    app.start()
    try:
        app.bus.emit(SpeakingStarted())
        app.bus.emit(_segment("open calc"))  # what voice barge-in used to kill TTS
        await asyncio.sleep(0.2)

        assert app.tts.stop_calls == 0, "voice barge-in is removed: TTS is not killed"
        assert stt.calls == 0, "the mid-playback segment is never transcribed"
        assert pack.calls == [], "and never dispatched"
        assert app.voice._window.state is WindowState.DORMANT, "no window opening"
    finally:
        app.stop()


@pytest.mark.scenario
async def test_hotkey_during_playback_kills_tts_and_opens_the_window(
    tmp_path: Path,
) -> None:
    """The barge-in escape is hotkey-only (M6 T4): the hotkey kills TTS and
    opens a fresh window mid-playback; ingest reopens and the next segment
    still processes."""
    pack = RecordingPack()
    app = _loop_app(tmp_path, pack)
    app.start()
    try:
        app.bus.emit(SpeakingStarted())
        app.bus.emit(HotkeyPressed(combo="ctrl+shift+m"))
        await _wait_for(lambda: app.tts.stop_calls >= 1)

        assert app.mascot is not None and app.mascot.state == "listening"
        assert app.voice._window.is_open, "the hotkey opened a fresh window"

        app.bus.emit(SpeakingStopped())  # what PiperSpeaker.stop() emits once it drains
        app.bus.emit(_segment("open calc"))
        await _wait_for(lambda: pack.calls == ["calc"], timeout=8.0)
    finally:
        app.stop()


# -- unit: the gate policy and the engine hook ------------------------------------


def test_gate_mode_precedence_speaking_beats_everything() -> None:
    speaking = [False]
    window_open = [False]
    gate = IngestGate(speaking=lambda: speaking[0], window_open=lambda: window_open[0])

    assert gate.mode is IngestMode.WAKE_ONLY, "no window: media guard (tap only)"
    window_open[0] = True
    assert gate.mode is IngestMode.FULL, "an open window ingests"
    speaking[0] = True
    assert gate.mode is IngestMode.DROP, "speaking wins over an open window"
    window_open[0] = False
    assert gate.mode is IngestMode.DROP, "hard half-duplex even without a window"


def test_engine_gate_modes_drop_tap_only_and_full() -> None:
    """The engine hook: DROP touches nothing; WAKE_ONLY feeds the tap but not
    the VAD; FULL feeds both — measured per mode on the same source shape."""
    for mode in IngestMode:
        bus = EventBus()
        vad = ScriptedVAD(speech_chunks=10**6)  # any chunk reaching VAD is speech
        tapped: list[np.ndarray] = []
        chunks = [np.zeros(CHUNK_SAMPLES, dtype=np.int16) for _ in range(6)]
        gate = IngestGate(
            speaking=lambda m=mode: m is IngestMode.DROP,
            window_open=lambda m=mode: m is IngestMode.FULL,
        )
        engine = AudioEngine(
            bus=bus,
            chunk_source=iter(chunks),
            vad=vad,
            ingest_gate=gate,
            chunk_tap=tapped.append,
        )
        engine.start()
        engine.stop()  # finite source: the thread drains and exits
        if mode is IngestMode.DROP:
            assert tapped == [] and vad.feeds == 0, f"{mode}: nothing at all"
        elif mode is IngestMode.WAKE_ONLY:
            assert len(tapped) == len(chunks) and vad.feeds == 0, (
                f"{mode}: tap live, VAD off"
            )
        else:
            assert len(tapped) == len(chunks) and vad.feeds == len(chunks), (
                f"{mode}: tap + VAD"
            )
        bus.stop()


def test_gate_transition_resets_in_flight_speech() -> None:
    """Speech buffered under FULL must never resurface after the gate drops
    the middle: partial audio from a closed window is discarded at the
    transition, so re-opening cannot emit a segment stitched across the gap.
    The window flips run INSIDE the source generator — on the capture thread,
    between chunk pulls — so the boundaries are deterministic, not timed."""
    bus = EventBus()
    got: list[SpeechSegment] = []
    bus.subscribe(SpeechSegment, got.append)

    speech = [np.full(CHUNK_SAMPLES, 1000, dtype=np.int16) for _ in range(10)]
    gap = [np.zeros(CHUNK_SAMPLES, dtype=np.int16) for _ in range(5)]
    tail = [np.zeros(CHUNK_SAMPLES, dtype=np.int16) for _ in range(30)]
    window_open = [True]

    def stream() -> Iterator[np.ndarray]:
        yield from speech  # FULL: buffered, unflushed (no end-silence yet)
        window_open[0] = False  # → WAKE_ONLY: the gap goes tap-only, VAD never sees it
        yield from gap
        window_open[0] = True  # → FULL again: the stale buffer must have been reset
        yield from tail  # 30 silences: with the reset, nothing can flush

    gate = IngestGate(speaking=lambda: False, window_open=lambda: window_open[0])
    vad = lambda c: 1.0 if int(np.abs(c).max()) > 0 else 0.0  # noqa: E731 — content-scripted
    engine = AudioEngine(bus=bus, chunk_source=stream(), vad=vad, ingest_gate=gate)
    engine.start()
    engine.stop()  # finite source: the thread drains and exits
    bus.stop()
    assert got == [], "no segment may be stitched across a gate transition"
