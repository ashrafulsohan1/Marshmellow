"""Piper worker subprocess — the GPL isolation boundary (SP7) with a streaming
protocol the resident speaker can parse.

Why not `python -m piper`: piper1-gpl ≥ 1.8 cannot stream a WAV to stdout —
the default output is ffplay or a file, and `-f -` synthesizes the whole
utterance into a temp file before copying (measured: stdout stays empty until
synthesis completes). `--output-raw` streams but has no utterance framing, so
SpeakingStarted/Stopped and barge-in drain lose their boundaries. This worker
keeps piper's streaming synthesis AND exact boundaries:

    stdin:  one utterance per line (UTF-8)
    stdout: per utterance — 4-byte LE sample_rate, then repeated
            (4-byte LE chunk_len + int16 LE PCM), terminated by chunk_len == 0

GPL compliance: piper is imported only inside this subprocess, never in the
 Marshmellow process (SP7 subprocess ruling; same boundary as the CLI, minus
the CLI's framing loss).
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m marshmellow.tts.piper_worker <voice.onnx>", file=sys.stderr)
        return 2
    model = Path(sys.argv[1])
    if not model.exists():
        print(f"voice model not found: {model}", file=sys.stderr)
        return 2

    from piper import PiperVoice

    voice = PiperVoice.load(model)  # config defaults to model path + ".json"
    out = sys.stdout.buffer
    header = struct.pack("<I", voice.config.sample_rate & 0xFFFFFFFF)
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        out.write(header)
        out.flush()
        for chunk in voice.synthesize(text):
            pcm = chunk.audio_int16_bytes
            if not pcm:
                continue
            out.write(struct.pack("<I", len(pcm)))
            out.write(pcm)
            out.flush()
        out.write(struct.pack("<I", 0))  # end-of-utterance terminator
        out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
