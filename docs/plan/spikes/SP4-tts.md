# SP4 — TTS bench (piper, en_US-amy-medium)

**Date:** 2026-11-04 · **Machine:** user's Windows 11 desktop, CPU-only
**Resolves:** TTS engine default + voice-loop TTS latency budget (01-architecture §3.9, M2 latency targets)
**Data:** `data/sp4_tts.json` (CLI bench) + `data/sp4b_resident.json` (resident bench — authoritative)

## Method

Two passes, because the first was misleading: the CLI bench (`piper` subprocess per sentence) pays a **full model load on every invocation**, inflating RTF to 1.3–2.4. The resident bench (`sp4b`) loads the voice **once** and streams synthesis through piper's chunked API — that is the deployment shape Marshmellow will use (piper as an isolated subprocess per SP7's GPL ruling).

## Results (resident, load-once)

| Metric | Value |
|---|---|
| Model load | 7.69 s — **paid once at app startup**, not in the voice loop |
| First audio chunk | **0.21 s / 0.55 s / 0.34 s** (ack / long sentence / confirm prompt) |
| Total synth | 0.40–0.55 s per utterance |
| RTF | **0.10–0.17** (synthesis runs 6–10× faster than realtime on CPU) |

CLI-pass numbers (RTF 1.73 mean) are recorded in `sp4_tts.json` but discarded as load-dominated artifacts.

## Findings

1. **The ≤ 500 ms TTS-first-audio budget is met** by typical agent utterances (0.21–0.34 s); the longest test sentence hit 0.55 s — keep spoken acks short (already the planner prompt rule) and playback starts on the first chunk, so budget compliance is streaming-based, not file-based.
2. **CPU synthesis leaves the machine free** for STT + planner work — no GPU dependency for the voice loop.
3. Voice quality is subjective — **samples saved for the user to listen to**: `docs/plan/spikes/data/sp4_voice/sample_*.wav`.

## Decisions

- **Piper confirmed as TTS default** (`en_US-lessac-medium` per SP7 for the product; amy-medium used for this bench — both fine, user picks by ear).
- Deployment shape (SP7 license ruling): piper runs as an **isolated GPL subprocess**, loaded once, streaming audio chunks to the TTS queue over a pipe — the resident-bench numbers already reflect streaming; the subprocess boundary adds only small IPC cost (validated again in M2).
- Cold-start budget grows by ~8 s for the piper load (parallelized with STT/planner init; M5 measures total cold start ≤ 10 s — flag raised).
- edge-tts stays opt-in-only (≥7.3.1) per SP7.

## Limitations

- One voice benchmarked; alternative piper voices not benchmarked (all share the same engine, latency will be similar).
- First-audio latency was measured against pre-loaded text; the M2 E2E loop adds planner-response arrival time, which dominates.
