"""Real STT tests: synthetic audio always runs; fixture-audio tier needs SP2 data."""

import json
import time
import wave
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from marshmellow.core.contracts import SpeechSegment
from marshmellow.stt.fasterwhisper import FasterWhisperSTT

SP2_DIR = Path(__file__).resolve().parents[2] / "docs" / "plan" / "spikes" / "data"


@pytest.fixture(scope="module")
def stt() -> FasterWhisperSTT:
    return FasterWhisperSTT()


def _segment(pcm_i16: np.ndarray, sr: int) -> SpeechSegment:
    now = datetime.now()
    return SpeechSegment(
        pcm=pcm_i16.tobytes(), sample_rate=sr,
        started_at=now, ended_at=now + timedelta(seconds=len(pcm_i16) / sr),
    )


def _tone(seconds: float, sr: int = 16000) -> np.ndarray:
    t = np.linspace(0.0, seconds, int(sr * seconds), endpoint=False)
    return (np.sin(2.0 * np.pi * 440.0 * t) * 8000.0).astype(np.int16)


def test_tone_transcribes_without_crash(stt: FasterWhisperSTT) -> None:
    transcript = stt.transcribe(_segment(_tone(0.5), 16000))
    assert transcript.text is not None
    assert transcript.source == "mic"


def test_resamples_non_16k_input(stt: FasterWhisperSTT) -> None:
    transcript = stt.transcribe(_segment(_tone(0.3, 22050), 22050))
    assert transcript.text is not None


def _read_wav_i16(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr, ch, raw = w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return x, sr


def _wer(ref: str, hyp: str) -> float:
    r, h = ref.lower().split(), hyp.lower().split()
    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1,
                          d[i - 1][j - 1] + (r[i - 1] != h[j - 1]))
    return d[len(r)][len(h)] / max(1, len(r))


@pytest.mark.fixture_audio
def test_fixture_audio_wer_and_rtf(stt: FasterWhisperSTT) -> None:
    data, fixdir = SP2_DIR / "sp2_stt.json", SP2_DIR / "sp2_fixtures"
    if not (data.exists() and fixdir.exists()):
        pytest.skip("SP2 fixtures not present on this machine")
    rows = json.loads(data.read_text(encoding="utf-8"))["configs"][0]["rows"]
    wers: list[float] = []
    rtfs: list[float] = []
    # Min-of-3 timing: a single-shot wall-clock sample measures ambient desktop
    # load (sync clients, other builds) as much as the model — identical code
    # measured median RTF 0.44 and 1.27 in two runs of this suite. The min over
    # repeats estimates the model's actual cost (timeit's convention), so the
    # SP2 gate `RTF < 1.0` keeps its threshold and stops flaking.
    rtf_repeats = 3
    for i, row in enumerate(rows):
        wav = fixdir / f"phrase_{i:02d}.wav"
        if not wav.exists():
            continue
        pcm, sr = _read_wav_i16(wav)
        dur = len(pcm) / sr
        segment = _segment(pcm, sr)
        elapsed = float("inf")
        for _ in range(rtf_repeats):
            t0 = time.perf_counter()
            transcript = stt.transcribe(segment)
            elapsed = min(elapsed, time.perf_counter() - t0)
        wers.append(_wer(row["ref"], transcript.text))
        rtfs.append(elapsed / dur)
    assert wers, "no fixture WAVs found"
    assert np.mean(wers) <= 0.35, f"WER regressed: {np.mean(wers):.3f}"
    assert np.median(rtfs) < 1.0, f"RTF regressed: {np.median(rtfs):.3f}"
