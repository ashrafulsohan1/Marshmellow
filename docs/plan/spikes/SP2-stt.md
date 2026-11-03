# SP2 — STT bench (faster-whisper on this machine)

**Date:** 2026-11-04 · **Machine:** user's Windows 11 desktop, CPU-only, int8
**Resolves:** STT engine default (OQ-4) and the end-speech→transcript latency budget (M2)
**Data:** `data/sp2_stt.json`

## Method

12 fixed phrases (incl. proper nouns: Marshmellow, YouTube, GitHub, WhatsApp, Spotify) synthesized with Windows SAPI → 16-bit wav → **linear-resampled 22.05 kHz→16 kHz → fed to faster-whisper as float32 numpy PCM** (never a file path — see engineering note). `beam_size=1`, `language="en"`. WER on clean TTS speech is optimistic-biased → treat as **comparative only**; real-mic WER is re-measured in M2.

## Results

| Config | Load | Mean RTF | Mean WER | p95 infer |
|---|---|---|---|---|
| **base / int8** | 1.4 s | **0.310** | 15.6% | **1.21 s** |
| small / int8 | 2.1 s | 1.872 | 18.0% | 14.25 s |
| distil-small.en / int8 | 2.2 s | 0.963 | **14.8%** | 3.78 s |

## Findings

1. **`small` is disqualified on this CPU** — slower than realtime (RTF 1.87) with p95 of 14 s, ten times over the ≤ 1.5 s transcript budget.
2. **`base/int8` is the only config inside budget** (p95 1.21 s) with a 3.2× realtime margin; its WER handicap vs distil is small (15.6% vs 14.8% on this corpus).
3. `distil-small.en` has the best WER but p95 3.78 s busts the budget → keep as the **accuracy-upgrade candidate** for when we add VAD-trimmed audio, batched decoding, or GPU.
4. Absolute WER ~15% is inflated by the corpus (proper nouns, naive linear resampling, TTS voice); the ranking, not the number, drives the decision.

## Decisions

- **Default STT = faster-whisper `base`, int8, CPU, beam 1, `language="en"`** (OQ-4 closed).
- distil-small.en recorded as upgrade path; re-bench in M2 with VAD-trimmed real-mic audio.
- **Product rule (from the debugging trail):** always feed the transcriber **float32 16 kHz numpy PCM**, never file paths. faster-whisper 1.2.1's file-decoding path calls `av.open(..., metadata_errors=...)` which PyAV ≥15 removed (av 19.0.2 installed → hard TypeError); the numpy path never touches it. `av` remains a hard import dependency of faster-whisper — installed but unused for decode in our pipeline. PyAV versions 12–14 ship no Windows wheel for this setup (source build needs MSVC), so pinning old av is not an option.

## Limitations

- SAPI fixtures (clean, single voice) — real-mic accuracy/latency measured in M2's fixture-audio tier.
- No streaming/partial-hypothesis measurement yet (M2 scope: first-partial latency for long utterances).
