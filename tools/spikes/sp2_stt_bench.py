"""SP2 - STT latency/accuracy bench for faster-whisper configs on this machine.

Fixtures: 12 fixed phrases synthesized with Windows SAPI (PowerShell) -> wav.
Note: WER here measures clean-TTS speech, so it is an optimistic, *comparative*
number across configs - real-mic WER is measured again in M2.

Writes docs/plan/spikes/data/sp2_stt.json.

Run: tools/spikes/.venv/Scripts/python.exe tools/spikes/sp2_stt_bench.py
"""
from __future__ import annotations

import json
import subprocess
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "docs" / "plan" / "spikes" / "data"
FIX = DATA / "sp2_fixtures"
FIX.mkdir(parents=True, exist_ok=True)

PHRASES = [
    "Hey Marshmellow, open the browser and check if the YouTube channel has new videos",
    "Open Notepad and type hello world",
    "What is the weather forecast for tomorrow",
    "Turn the volume down to twenty percent",
    "Open File Explorer and go to the Downloads folder",
    "Search for flights to Seattle next weekend",
    "Play my Spotify playlist called Focus",
    "Take a screenshot and send it to Anna on WhatsApp",
    "Set a reminder for nine AM on Friday",
    "Close all windows and put the computer to sleep",
    "What is the GitHub repository for this project",
    "Tell me a joke about marshmallows",
]

CONFIGS = [("base", "int8"), ("small", "int8"), ("distil-small.en", "int8")]


def synth(text: str, path: Path) -> None:
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{path}'); "
        f"$s.Speak('{text}'); "
        "$s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   check=True, timeout=120, capture_output=True)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def load_wav_16k(path: Path) -> np.ndarray:
    """16-bit wav -> mono float32 @16 kHz. Feeds faster-whisper as an ndarray,
    bypassing its PyAV file-decoding path (the deployment shape too: the
    product always hands the model live numpy PCM, never files)."""
    with wave.open(str(path), "rb") as w:
        sr, ch, raw = w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        idx = np.linspace(0.0, len(x) - 1.0, int(round(len(x) * 16000 / sr)))
        x = np.interp(idx, np.arange(len(x), dtype=np.float64), x)
    return x.astype(np.float32)


def wer(ref: str, hyp: str) -> float:
    r, h = ref.lower().split(), hyp.lower().split()
    d = np.zeros((len(r) + 1, len(h) + 1), dtype=int)
    d[:, 0] = np.arange(len(r) + 1)
    d[0, :] = np.arange(len(h) + 1)
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1,
                          d[i - 1, j - 1] + (r[i - 1] != h[j - 1]))
    return float(d[len(r), len(h)]) / max(1, len(r))


def make_fixtures() -> list[tuple[Path, str]]:
    clips = []
    for i, phrase in enumerate(PHRASES):
        wav = FIX / f"phrase_{i:02d}.wav"
        if not wav.exists():
            synth(phrase, wav)
        clips.append((wav, phrase))
    return clips


def bench(model_name: str, compute: str, clips) -> dict:
    from faster_whisper import WhisperModel
    t0 = time.perf_counter()
    model = WhisperModel(model_name, device="cpu", compute_type=compute)
    load_s = time.perf_counter() - t0
    rows = []
    for wav, ref in clips:
        t = time.perf_counter()
        segments, _ = model.transcribe(load_wav_16k(wav), language="en", beam_size=1)
        text = " ".join(s.text for s in segments).strip()
        infer = time.perf_counter() - t
        dur = wav_duration(wav)
        rows.append({"ref": ref, "hyp": text, "dur_s": round(dur, 2),
                     "infer_s": round(infer, 2), "rtf": round(infer / dur, 3),
                     "wer": round(wer(ref, text), 3)})
    rtf = float(np.mean([r["rtf"] for r in rows]))
    mean_wer = float(np.mean([r["wer"] for r in rows]))
    p95_infer = float(np.percentile([r["infer_s"] for r in rows], 95))
    print(f"{model_name}/{compute}: load={load_s:.1f}s mean_rtf={rtf:.3f} "
          f"wer={mean_wer:.3f} p95_infer={p95_infer:.2f}s")
    return {"model": model_name, "compute": compute, "load_s": round(load_s, 1),
            "mean_rtf": round(rtf, 3), "mean_wer": round(mean_wer, 3),
            "p95_infer_s": round(p95_infer, 2), "rows": rows}


def main() -> None:
    clips = make_fixtures()
    out = {"generated": datetime.now(timezone.utc).isoformat(),
           "fixtures": len(clips), "note": "SAPI-clean speech; comparative WER only",
           "configs": []}
    for name, compute in CONFIGS:
        try:
            out["configs"].append(bench(name, compute, clips))
        except Exception as exc:  # keep going; one bad model must not kill the bench
            out["configs"].append({"model": name, "compute": compute,
                                   "error": repr(exc)})
            print(f"{name}: ERROR {exc!r}")
    (DATA / "sp2_stt.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwritten: {DATA / 'sp2_stt.json'}")


if __name__ == "__main__":
    main()
