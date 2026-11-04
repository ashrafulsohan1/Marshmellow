"""Piper TTS as a resident streaming subprocess (SP7: GPL → subprocess isolation).

One worker process (``marshmellow.tts.piper_worker``, which imports piper only
inside its own process) lives for the app's lifetime: text lines go in on
stdin, length-prefixed raw PCM chunks come out on stdout — true streaming
first audio with exact utterance boundaries (the 0-length terminator), which
piper1-gpl's own stdout modes cannot give us (see piper_worker's docstring for
the measured CLI behavior). Playback honors the queue priority
(ack < result < background) and `interrupt=True` drops everything pending.
"""

from __future__ import annotations

import io
import logging
import os
import struct
import subprocess
import sys
import threading
import wave
from collections.abc import Callable, Iterator
from pathlib import Path

from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import (
    SpeakingStarted,
    SpeakingStopped,
    SpeakRequest,
)

logger = logging.getLogger(__name__)

_PRIORITY = {"ack": 0, "result": 1, "background": 2}

REPO_ROOT = Path(__file__).resolve().parents[3]


def default_voice_model(voice: str) -> Path | None:
    """Locate the piper ONNX model for `voice` (e.g. ``en_US-lessac-medium``).

    Search order: the ``MARSHMELLOW_VOICE_DIR`` env var, then ``voices/`` in the
    repo checkout (what ``python -m piper.download_voices <voice>`` fills).
    Model binaries stay out of git (re-downloadable), so None means "no voice
    on this machine" — the composition root degrades instead of guessing.
    """
    dirs = []
    env_dir = os.environ.get("MARSHMELLOW_VOICE_DIR")
    if env_dir:
        dirs.append(Path(env_dir))
    dirs.append(REPO_ROOT / "voices")
    for directory in dirs:
        candidate = directory / f"{voice}.onnx"
        if candidate.exists():
            return candidate
    return None


def make_wav(pcm: bytes, sample_rate: int = 22050) -> bytes:
    """Canonical 44-byte-header mono 16-bit WAV (test helper / fixture format)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


class _ChunkReader:
    """Parses the worker's stdout protocol (see piper_worker) off one stream.

    Yields PCM chunks of the CURRENT utterance in ≤4096-byte pieces; sample
    rate arrives once per utterance before the chunks. ``current_rate`` holds
    the latest declared rate. On any protocol surprise, logs and yields
    nothing rather than playing garbage.
    """

    CHUNK_PIECE = 4096

    def __init__(self, stream: io.RawIOBase) -> None:
        self._stream = stream
        self.current_rate: int | None = None

    def _exact(self, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            part = self._stream.read(n - len(buf))
            if not part:
                return None
            buf += part
        return buf

    def utterances(self) -> Iterator[list[bytes]]:
        """Yield one complete utterance (list of PCM chunks) at a time."""
        while True:
            header = self._exact(4)
            if header is None:
                return
            (rate,) = struct.unpack("<I", header)
            if rate == 0:
                logger.warning("worker sent a zero sample rate; dropping utterance")
                return
            self.current_rate = rate
            chunks: list[bytes] = []
            while True:
                size_raw = self._exact(4)
                if size_raw is None:
                    return
                (size,) = struct.unpack("<I", size_raw)
                if size == 0:
                    break  # end of this utterance
                remaining = size
                while remaining > 0:
                    piece = self._exact(min(self.CHUNK_PIECE, remaining))
                    if piece is None:
                        logger.warning("worker stream ended mid-chunk")
                        return
                    remaining -= len(piece)
                    chunks.append(piece)
            yield chunks


class PiperSpeaker:
    def __init__(
        self,
        bus: EventBus,
        model: Path | None = None,
        sample_rate: int = 22050,
        spawn: Callable[[list[str]], subprocess.Popen] | None = None,
        player: Callable[[bytes], None] | None = None,
    ) -> None:
        self._bus = bus
        self._model = model
        self._sample_rate = sample_rate
        self._spawn = spawn or self._default_spawn
        self._player = player or self._default_player
        self._proc: subprocess.Popen | None = None
        self._stream: object | None = None  # sounddevice.OutputStream, lazy
        self._pending: list[tuple[int, int, SpeakRequest]] = []
        self._seq = 0
        # Monotonic "the user asked for silence" counter (§3.9): a barge-in bumps
        # it, and any utterance whose playback sees the counter move cuts itself
        # off. The speaker and its subprocess stay alive for the next utterance.
        self._silence_epoch = 0
        self._cv = threading.Condition()
        self._alive = False
        self._reader: _ChunkReader | None = None
        self._utterances: Iterator[list[bytes]] | None = None
        self._worker = threading.Thread(target=self._run, name="piper-worker", daemon=True)

    def start(self) -> None:
        self._alive = True
        self._worker.start()

    def say(self, request: SpeakRequest) -> None:
        with self._cv:
            if request.interrupt:
                self._pending.clear()
                self._silence_epoch += 1  # §3.9: interrupt=True cuts ongoing speech too
            self._seq += 1
            self._pending.append((_PRIORITY[request.priority], self._seq, request))
            self._cv.notify()

    def pending_texts(self) -> list[str]:
        with self._cv:
            return [req.text for _, _, req in sorted(self._pending, key=lambda t: (t[0], t[1]))]

    def stop(self) -> None:
        """Barge-in kill (§3.9): silence now, drop everything pending.

        The worker and the piper subprocess stay alive — the next say() speaks
        normally. Full teardown is `quit()`.
        """
        with self._cv:
            self._pending.clear()
            self._silence_epoch += 1

    def quit(self) -> None:
        """Full teardown: stop the worker and terminate the subprocess (no zombie)."""
        self._alive = False
        with self._cv:
            self._silence_epoch += 1
            self._cv.notify()
        if self._worker.is_alive():
            self._worker.join(timeout=3.0)
        if self._proc is not None:
            try:
                self._proc.stdin.close()  # type: ignore[union-attr]
            except Exception:
                pass
            self._proc.terminate()

    def _run(self) -> None:
        self._proc = self._spawn(self._argv())
        if self._proc.stdout is not None:
            self._reader = _ChunkReader(self._proc.stdout)  # type: ignore[arg-type]
            self._utterances = self._reader.utterances()
        while self._alive:
            with self._cv:
                if not self._pending:
                    self._cv.wait(timeout=0.2)
                    if not self._pending:
                        continue
                self._pending.sort(key=lambda t: (t[0], t[1]))
                _, _, request = self._pending.pop(0)
                epoch = self._silence_epoch  # read atomically with the pop
            if not self._alive:
                break
            self._speak(request, epoch)

    def _speak(self, request: SpeakRequest, epoch: int) -> None:
        assert self._proc is not None
        # The worker's protocol is one utterance per stdin line. Assistant text
        # reaches SpeakRequest verbatim (orchestrator → TaskEvent payload), and
        # an LLM reply can carry newlines: writing them verbatim makes the
        # worker synthesize one utterance PER LINE while this method consumes
        # exactly one — every later say() would play a leftover line's audio
        # and one utterance would never be spoken. Collapse to a single spoken
        # line. Whitespace-only text writes nothing: a blank line makes the
        # worker emit no utterance at all, which would block the stream read
        # forever.
        spoken = " ".join(request.text.split())
        if not spoken:
            logger.info("nothing speakable in %r; skipping utterance", request.text)
            return
        try:
            self._proc.stdin.write((spoken + "\n").encode("utf-8"))  # type: ignore[union-attr]
            self._proc.stdin.flush()  # type: ignore[union-attr]
        except (OSError, ValueError):
            logger.exception("worker stdin write failed; dropping utterance")
            return
        self._bus.emit(SpeakingStarted())
        cancelled = False
        try:
            # Pull EXACTLY ONE utterance: next() returns as soon as this
            # utterance's terminator is consumed — the resident stream never
            # EOFs, so iterating further would block on the next utterance's
            # header and never emit SpeakingStopped (the exact stdout hazard
            # Review Focus 3 warns about).
            chunks = next(self._utterances, None) if self._utterances else None
            if chunks is None:
                logger.error("piper worker stream ended; utterance dropped")
                return
            if self._reader is not None and self._reader.current_rate:
                self._sample_rate = self._reader.current_rate
            for chunk in chunks:
                with self._cv:
                    cancelled = epoch != self._silence_epoch or not self._alive
                if cancelled:
                    # The whole utterance is buffered before it yields, so the
                    # stream already sits at the next header — dropping the rest
                    # of the playback needs no drain pass.
                    break
                self._player(chunk)
        except Exception:
            logger.exception("piper playback failed")
        finally:
            self._bus.emit(SpeakingStopped())

    def _argv(self) -> list[str]:
        argv = [sys.executable, "-m", "marshmellow.tts.piper_worker"]
        if self._model is not None:
            argv.append(str(self._model))
        return argv

    def _default_spawn(self, argv: list[str]) -> subprocess.Popen:
        return subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _default_player(self, chunk: bytes) -> None:
        import numpy as np
        import sounddevice as sd

        if self._stream is None:
            self._stream = sd.OutputStream(
                samplerate=self._sample_rate, channels=1, dtype="int16"
            )
            self._stream.start()
        self._stream.write(np.frombuffer(chunk, dtype=np.int16))  # type: ignore[union-attr]
