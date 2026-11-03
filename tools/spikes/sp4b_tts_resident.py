"""SP4b - resident-mode piper latency (the number that matters for Marshmellow).

The SP4 CLI bench pays a full model load on every invocation. This bench loads
the voice ONCE and then synthesizes the same three utterances, measuring
time-to-first-audio-chunk (streaming API when available) and per-utterance RTF.

Writes docs/plan/spikes/data/sp4b_resident.json.
Run: tools/spikes/.venv/Scripts/python.exe tools/spikes/sp4b_tts_resident.py
"""
from __future__ import annotations

import json
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "docs" / "plan" / "spikes" / "data"
VOICE_DIR = DATA / "sp4_voice"
MODEL = VOICE_DIR / "en_US-amy-medium.onnx"

SENTENCES = [
    "On it. Opening the browser now.",
    "They posted a new video two days ago, titled Getting Started with Marshmellow.",
    "I need the screen to confirm this purchase. Is that okay?",
]


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def main() -> None:
    from piper import PiperVoice

    out = {"generated": datetime.now(timezone.utc).isoformat(),
           "mode": "resident (load once)", "rows": []}
    t0 = time.perf_counter()
    voice = PiperVoice.load(str(MODEL), config_path=str(MODEL.with_suffix(".onnx.json")))
    out["load_s"] = round(time.perf_counter() - t0, 2)
    print(f"load: {out['load_s']}s (paid once at startup, not per utterance)")

    for i, text in enumerate(SENTENCES):
        try:
            t0 = time.perf_counter()
            first = None
            total_bytes = 0
            sr = None
            for ch in voice.synthesize(text):
                if first is None:
                    first = time.perf_counter() - t0
                total_bytes += len(ch.audio_int16_bytes)
                sr = getattr(ch, "sample_rate", sr)
            total = time.perf_counter() - t0
            dur = total_bytes / (2 * sr) if sr else 0.0
            row = {"text": text, "mode": "stream",
                   "first_audio_s": round(first, 2) if first else None,
                   "synth_s": round(total, 2), "audio_s": round(dur, 2),
                   "rtf": round(total / dur, 3) if dur else None}
        except Exception as exc:
            wav_path = VOICE_DIR / f"resident_{i}.wav"
            t0 = time.perf_counter()
            with wave.open(str(wav_path), "wb") as wf:
                voice.synthesize_wav(text, wf)
            total = time.perf_counter() - t0
            dur = wav_duration(wav_path)
            row = {"text": text, "mode": f"synthesize_wav (stream failed: {exc!r})",
                   "first_audio_s": None, "synth_s": round(total, 2),
                   "audio_s": round(dur, 2), "rtf": round(total / dur, 3)}
        out["rows"].append(row)
        print(row)
    (DATA / "sp4b_resident.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwritten: {DATA / 'sp4b_resident.json'}")


if __name__ == "__main__":
    main()
