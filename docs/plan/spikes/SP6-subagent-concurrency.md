# SP6 — Subagent concurrency probe

**Date:** 2026-11-04 · **Resolves:** brief §8.D — "determine the environment's actual maximum concurrent subagents first"

## Method
One message dispatched **7 agent requests at once**: 6 trivial foreground Explore probes (reply-only, no tools) + 1 background general-purpose research agent (SP7). Rejection signal watched for per the SP6 design: any queueing/rejection error.

## Raw observation

| Request | Type | Result |
|---|---|---|
| probe 1 | foreground Explore | ✅ completed (`PROBE-OK-1`) |
| probe 2 | foreground Explore | ❌ **rejected: "user concurrency limit exceeded"** |
| probe 3 | foreground Explore | ✅ completed (`PROBE-OK-3`) |
| probe 4 | foreground Explore | ❌ **rejected: "user concurrency limit exceeded"** |
| probe 5 | foreground Explore | ✅ completed (`PROBE-OK-5`) |
| probe 6 | foreground Explore | ✅ completed (`PROBE-OK-6`) |
| SP7 research | background general-purpose | ✅ launched successfully in the same batch |

## Interpretation
- 4 foreground probes ran to completion while a 5th (background) agent launched alongside → the effective ceiling is **4–5 concurrently running subagents** (the background launch may have slipped into a freed slot).
- 6 concurrent foreground requests are **definitively above the limit**: 2 of 6 were hard-rejected (not queued) with a distinct error string: `user concurrency limit exceeded`.
- Rejections are **immediate and hard** — the workflow must not rely on retry; it must never issue more than the cap.

## Decision (encoded in 03-workflow.md)
**`MAX_CONCURRENT_SUBAGENTS = 4` — hard cap, including background agents. Overflow is queued, never dispatched.** Watch for the rejection string as a belt-and-braces signal; on seeing it, serialize the overflow instead of retrying in parallel.

Residual uncertainty (cap could be 5) is deliberately left on the safe side of the measurement.
