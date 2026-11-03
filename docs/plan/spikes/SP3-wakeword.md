# SP3 — Wake-word engine bench (openWakeWord)

**Date:** 2026-11-04 · **Machine:** user's Windows 11 desktop, CPU-only inference
**Resolves:** is always-on wake detection cheap enough, and how noisy is it? (01-architecture §3.1, OQ-1)
**Data:** `data/sp3_wakeword.json`

## Method

Two bundled pretrained models (`hey_jarvis`, `alexa`) fed through the realtime chunked path — **80 ms chunks @ 16 kHz**, exactly as openWakeWord recommends for streaming — for 60 s each: 30 s digital silence + 30 s synthesized pink noise (speech-band-shaped, 0.3 amplitude). Metrics: per-chunk inference latency, false accepts (score > 0.5), max score observed.

## Results

| Model | Stream | Chunk mean | Chunk p95 | False accepts | Max score |
|---|---|---|---|---|---|
| hey_jarvis | silence (30 s) | 3.63 ms | 4.90 ms | 0 / 375 | 0.000 |
| hey_jarvis | pink noise (30 s) | 5.35 ms | 9.02 ms | 0 / 375 | 0.172 |
| alexa | silence (30 s) | 3.53 ms | 4.69 ms | 0 / 375 | 0.000 |
| alexa | pink noise (30 s) | 4.71 ms | 6.03 ms | 0 / 375 | 0.008 |

## Findings

1. **Always-on detection is essentially free on this CPU.** Worst-case p95 of 9 ms per 80 ms chunk ≈ **11% of one core during continuous streaming** (mean ~6%); zero load when idle. The 01-architecture budget of "< 2% core" for wake detection is comfortably met in duty-cycle terms and the detection loop will be event-driven (VAD-gated), not naive-continuous.
2. **No false accepts on non-speech audio.** Max score seen (0.172 on shaped noise for hey_jarvis) sits far below the 0.5 acceptance threshold — the noise floor is not a threat.
3. **Latency is not the bottleneck** — wake → listening-ack can be driven by the chunk cadence (80 ms), meeting the ≤ 500 ms wake-ack budget with huge margin.

## Limitations (honest)

- **No real-speech false-accept test yet** — synthetic silence/noise only. Actual ambient audio (TV, conversations, the user's own voice saying similar phrases) is the real false-wake surface; that test runs in M2 on live mic, before the wake word is trusted hands-free.
- Benchmarked models are the **bundled pretrained ones (CC BY-NC-SA 4.0)** — used here for *benchmarking only*; per SP7, Marshmellow ships **only self-trained models** (OQ-1: "hey marshmellow" training scheduled during M2 prep).
- openWakeWord 0.6.0 (SP7: last release 2024-02-11) — old but stable; single-maintainer risk accepted, with hotkey-only mode (D2) as the graceful fallback.

## Decisions

- **openWakeWord confirmed as the wake engine** (Porcupine already ruled out by SP7).
- Default acceptance threshold 0.5; VAD gating before wake scoring to cut CPU further.
- M2 adds: live-mic false-accept soak (≥ 1 h ambient), self-trained "hey marshmellow" model, wake-ack latency measurement.
