"""Fake microphone: replays WAV fixtures as int16 chunks (02-milestones testing strategy)."""

from __future__ import annotations

import wave
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from marshmellow.audio.capture import CHUNK_SAMPLES


class FixtureMic:
    def __init__(self, files: list[Path], chunk_samples: int = CHUNK_SAMPLES) -> None:
        self._files = files
        self._chunk_samples = chunk_samples

    def chunks(self) -> Iterator[np.ndarray]:
        for path in self._files:
            pcm = self._load_16k(path)
            for start in range(0, len(pcm), self._chunk_samples):
                yield pcm[start:start + self._chunk_samples]

    @staticmethod
    def _load_16k(path: Path) -> np.ndarray:
        with wave.open(str(path), "rb") as w:
            sr, channels, raw = (
                w.getframerate(), w.getnchannels(), w.readframes(w.getnframes()),
            )
        x = np.frombuffer(raw, dtype=np.int16)
        if channels > 1:
            x = x.reshape(-1, channels).mean(axis=1).astype(np.int16)
        if sr != 16000:
            idx = np.linspace(0.0, len(x) - 1.0, max(1, int(round(len(x) * 16000 / sr))))
            x = np.interp(idx, np.arange(len(x), dtype=np.float64), x)
        return x.astype(np.int16)
