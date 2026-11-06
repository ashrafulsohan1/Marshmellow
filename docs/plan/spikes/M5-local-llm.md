# M5 local LLM probe (SP5-style, LOCAL leg) — Ollama on this machine

Generated: 2026-11-06T19:57:17+00:00 · daemon `http://127.0.0.1:11434` · 5 runs per leg (after one untimed warmup) · git `c053b0f`

| model | planner median (ms) | tool-call rate | arg-valid rate | vision turn | vision median (ms) |
|---|---|---|---|---|---|
| `hf.co/mradermacher/Qwen2.5-Coder-1.5B-Instruct-heretic-GGUF:Q6_K` | 16658.3 | 0.0 | 0.0 | FAILED | None |

## Recommendation

**No local default.** No measured model produced reliably valid tool calls — do not wire a local planner default from this leg. For the D5 vision fallback a vision-capable model is required on top (e.g. `ollama pull qwen2.5vl:3b`), then re-run this bench.

## Raw data

`data/m5_ollama.json` (per-run latencies, tool-call arguments, errors).

Bench: `tools/bench_ollama_m5.py` — not part of the test gates; needs `ollama serve`.
