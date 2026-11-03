"""SP4 - TTS bench (piper, en_US voice).

Downloads one piper voice (en_US-amy-medium, ~63 MB) from HuggingFace and
synthesizes three Marshmellow-typical utterances. Measures synth time vs audio
duration (RTF) and saves the samples for the user to listen to.

Writes docs/plan/spikes/data/sp4_tts.json and wav samples next to it.

Run: tools/spikes/.venv/Scripts/python.exe tools/spikes/sp4_tts.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "docs" / "plan" / "spikes" / "data"
VOICE_DIR = DATA / "sp4_voice"
VOICE_DIR.mkdir(parents=True, exist_ok=True)

VOICE_BASE = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
              "en/en_US/amy/medium/")
MODEL = VOICE_DIR / "en_US-amy-medium.onnx"
CONF = VOICE_DIR / "en_US-amy-medium.onnx.json"

SENTENCES = [
    "On it. Opening the browser now.",
    "They posted a new video two days ago, titled Getting Started with Marshmellow.",
    "I need the screen to confirm this purchase. Is that okay?",
]


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def main() -> None:
    out = {"generated": datetime.now(timezone.utc).isoformat(),
           "voice": "en_US-amy-medium", "samples": []}
    if not MODEL.exists():
        print("downloading voice model (~63 MB)...")
        urllib.request.urlretrieve(VOICE_BASE + MODEL.name + "?download=true", MODEL)
    if not CONF.exists():
        urllib.request.urlretrieve(VOICE_BASE + CONF.name, CONF)

    import sys
    piper = shutil.which("piper") or str(Path(sys.executable).parent / "piper.exe")
    if not Path(piper).exists():
        # piper-tts >=1.3 exposes a python API instead of a console script
        try:
            from piper import PiperVoice  # noqa: F401
            piper = "python-api"
        except ImportError:
            out["error"] = "piper CLI and python API both unavailable"
            (DATA / "sp4_tts.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
            print(out["error"])
            return

    for i, text in enumerate(SENTENCES):
        wav_path = VOICE_DIR / f"sample_{i}.wav"
        t0 = time.perf_counter()
        if piper == "python-api":
            from piper import PiperVoice, SynthesisConfig
            voice = PiperVoice.load(str(MODEL), config_path=str(CONF))
            with wave.open(str(wav_path), "wb") as wf:
                voice.synthesize_wav(text, wf)
        else:
            subprocess.run([piper, "-m", str(MODEL), "-f", str(wav_path)],
                           input=text.encode(), check=True, timeout=180)
        synth = time.perf_counter() - t0
        dur = wav_duration(wav_path)
        out["samples"].append({"text": text, "synth_s": round(synth, 2),
                               "audio_s": round(dur, 2),
                               "rtf": round(synth / dur, 3),
                               "wav": str(wav_path.relative_to(ROOT))})
        print(f"sample {i}: synth={synth:.2f}s audio={dur:.2f}s rtf={synth / dur:.3f}")

    rtfs = [s["rtf"] for s in out["samples"]]
    out["mean_rtf"] = round(sum(rtfs) / len(rtfs), 3)
    (DATA / "sp4_tts.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nmean_rtf={out['mean_rtf']}  written: {DATA / 'sp4_tts.json'}")
    print("listen to the samples in docs/plan/spikes/data/sp4_voice/")


if __name__ == "__main__":
    main()
