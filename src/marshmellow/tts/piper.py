"""Piper TTS as a resident streaming subprocess (SP7: GPL → subprocess isolation).

One piper process lives for the app's lifetime: text lines go in on stdin,
complete WAVs come out on stdout (piper's default output). The RIFF header's
data-size field makes utterance boundaries exact, so playback and the
SpeakingStarted/Stopped events are deterministic. Playback honors the queue
priority (ack < result < background) and `interrupt=True` drops everything
pending.
"""

from __future__ import annotations

import io
import logging
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


def make_wav(pcm: bytes, sample_rate: int = 22050) -> bytes:
    """Canonical 44-byte-header mono 16-bit WAV (test helper / fixture format)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _read_wav_stream(stream: io.RawIOBase, chunk_size: int = 4096) -> Iterator[bytes]:
    """Yield PCM chunks of ONE complete WAV from a stream of concatenated WAVs.

    Assumes the canonical 44-byte header piper writes (16-bit mono PCM). On any
    parse surprise, logs and yields nothing rather than playing garbage.
    """

    def exact(n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            part = stream.read(n - len(buf))
            if not part:
                return None
            buf += part
        return buf

    header = exact(44)
    if header is None:
        return
    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        logger.warning("piper stdout is not a WAV stream; dropping utterance")
        return
    data_size = int.from_bytes(header[40:44], "little")
    remaining = data_size
    while remaining > 0:
        part = exact(min(chunk_size, remaining))
        if not part:
            break
        remaining -= len(part)
        yield part


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
        try:
            self._proc.stdin.write((request.text + "\n").encode("utf-8"))  # type: ignore[union-attr]
            self._proc.stdin.flush()  # type: ignore[union-attr]
        except (OSError, ValueError):
            logger.exception("piper stdin write failed; dropping utterance")
            return
        self._bus.emit(SpeakingStarted())
        cancelled = False
        try:
            stream = _read_wav_stream(self._proc.stdout)  # type: ignore[arg-type]
            for chunk in stream:
                with self._cv:
                    cancelled = epoch != self._silence_epoch or not self._alive
                if cancelled:
                    break
                self._player(chunk)
            if cancelled and self._alive:
                # Drain the rest of this utterance unheard: piper keeps producing
                # the WAV it was asked for, and skipping to its end keeps the
                # stdout framing exact for the next utterance.
                for _ in stream:
                    pass
        except Exception:
            logger.exception("piper playback failed")
        finally:
            self._bus.emit(SpeakingStopped())

    def _argv(self) -> list[str]:
        argv = [sys.executable, "-m", "piper"]
        if self._model is not None:
            argv += ["--model", str(self._model)]
            config = Path(str(self._model) + ".json")
            if config.exists():
                argv += ["--config", str(config)]
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
