"""SP3 - wake-word engine bench (openWakeWord, pretrained models).

Feeds 30s of silence and 30s of pink noise through the realtime chunked path
(80 ms chunks @16 kHz) and measures per-chunk latency + false accepts.
Custom-phrase training feasibility for "hey marshmellow" is covered by SP7 web
research; this bench answers: is always-on detection cheap enough here?

Writes docs/plan/spikes/data/sp3_wakeword.json.

Run: tools/spikes/.venv/Scripts/python.exe tools/spikes/sp3_wakeword.py
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "docs" / "plan" / "spikes" / "data"
DATA.mkdir(parents=True, exist_ok=True)

SR = 16000
CHUNK = 1280  # 80 ms, as recommended by openwakeword
MODELS = ["hey_jarvis", "alexa"]


def pink_noise(n: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(n)
    f = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n, d=1.0 / SR)
    freqs[0] = freqs[1]
    x = np.fft.irfft(f / np.sqrt(freqs), n)
    x = x / (np.abs(x).max() + 1e-9) * 0.3
    return (x * 32767).astype(np.int16)


def run_model(wname: str, silence: np.ndarray, noise: np.ndarray) -> dict:
    from openwakeword.model import Model
    m = Model(wakeword_models=[wname], inference_framework="onnx")
    stats = {}
    for label, arr in (("silence", silence), ("pink_noise", noise)):
        lat: list[float] = []
        accepts = 0
        scores = []
        for i in range(0, len(arr), CHUNK):
            t = time.perf_counter()
            pred = m.predict(arr[i:i + CHUNK])
            lat.append(time.perf_counter() - t)
            score = max(pred.values()) if isinstance(pred, dict) else float(pred)
            scores.append(float(score))
            if score > 0.5:
                accepts += 1
        stats[label] = {
            "chunk_ms_mean": round(float(np.mean(lat)) * 1000, 2),
            "chunk_ms_p95": round(float(np.percentile(lat, 95)) * 1000, 2),
            "false_accept_chunks": accepts,
            "max_score": round(float(np.max(scores)), 3),
        }
    return stats


def main() -> None:
    import openwakeword
    print("downloading pretrained models (first run only)...")
    try:
        openwakeword.utils.download_models()
    except Exception as exc:
        print(f"download_models warning: {exc!r}")
    silence = np.zeros(SR * 30, dtype=np.int16)
    noise = pink_noise(SR * 30)
    out = {"generated": datetime.now(timezone.utc).isoformat(),
           "sr": SR, "chunk_ms": 80, "seconds_per_stream": 60, "models": {}}
    for wname in MODELS:
        try:
            out["models"][wname] = run_model(wname, silence, noise)
            print(wname, json.dumps(out["models"][wname]))
        except Exception as exc:
            out["models"][wname] = {"error": repr(exc)}
            print(f"{wname}: ERROR {exc!r}")
    (DATA / "sp3_wakeword.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwritten: {DATA / 'sp3_wakeword.json'}")


if __name__ == "__main__":
    main()
