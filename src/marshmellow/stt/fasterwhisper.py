"""Real STT — faster-whisper per M0-SP2 (base/int8, CPU).

Numpy float32 16 kHz PCM in, never file paths (SP2 rule: faster-whisper's
file-decoding path is incompatible with modern PyAV, and the product always
hands the model live capture anyway).
"""

from __future__ import annotations

import logging
import math

import numpy as np
from faster_whisper import WhisperModel

from marshmellow.core.contracts import SpeechSegment, Transcript

logger = logging.getLogger(__name__)


class FasterWhisperSTT:
    def __init__(
        self, model: str = "base", compute_type: str = "int8", language: str = "en"
    ) -> None:
        self._language = language
        logger.info("loading STT model %s (%s, cpu)...", model, compute_type)
        self._model = WhisperModel(model, device="cpu", compute_type=compute_type)
        logger.info("STT model ready")

    def transcribe(self, segment: SpeechSegment) -> Transcript:
        pcm = self._to_float32_16k(segment)
        segments, _info = self._model.transcribe(pcm, language=self._language, beam_size=1)
        texts: list[str] = []
        logprobs: list[float] = []
        for part in segments:
            texts.append(part.text)
            if part.avg_logprob is not None:
                logprobs.append(part.avg_logprob)
        text = " ".join(texts).strip()
        confidence = (
            round(math.exp(sum(logprobs) / len(logprobs)), 2) if logprobs else 1.0
        )
        return Transcript(text=text, language=self._language, confidence=confidence,
                          source="mic")

    @staticmethod
    def _to_float32_16k(segment: SpeechSegment) -> np.ndarray:
        x = np.frombuffer(segment.pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if segment.sample_rate != 16000:
            n_out = max(1, int(round(len(x) * 16000 / segment.sample_rate)))
            idx = np.linspace(0.0, len(x) - 1.0, n_out)
            x = np.interp(idx, np.arange(len(x), dtype=np.float64), x)
        return x.astype(np.float32)
