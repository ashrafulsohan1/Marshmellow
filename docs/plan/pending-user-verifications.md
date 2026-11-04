# Marshmellow — Pending User Verifications

Manual steps that need a human at the machine (speaking / listening / watching).
Each entry names the task that deferred it and what "done" looks like. Everything
automatable around these steps has already been implemented and gated.

## M2 Task 9 — assisted hands-free E2E: "hey marshmellow — open Notepad"

**Deferred from:** M2 plan Task 9 Step 2 (also discharges Task 2's "hear *On it.* from
the real speakers" and Task 4's real-device capture path, which this one session covers).

**Status:** not yet run — the wake word cannot lead this session yet: the self-trained
"hey marshmellow" model is still pending (D8/OQ-1), so the app degrades to
**hotkey activation** with a spoken hint at startup (expected: *"The wake word isn't
set up yet. Use the hotkey to talk to me."* — it may take a few seconds to start
speaking: the resident piper worker pays its model load on the first utterance).

**Run:**

1. `uv run python -m marshmellow` (tray + mascot appear; mic unmuted at the OS level).
2. Hold **ctrl+shift+m**, say **"open Notepad"**, release the keys.
3. Expected, hands-free end to end: mascot → listening → thinking → acting; *"On it."*
   spoken; a real Notepad window opens; result spoken; mascot → idle.
4. Barge-in: while the result is being spoken, just start talking — playback must stop
   immediately. Then say **"stop everything"** — any running task cancels, mascot → idle.
5. Repeat step 2 once more to confirm the loop still works after the barge-in.

**Done when:** steps 2–5 pass without touching mouse/keyboard (hotkey hold excepted
until the wake model ships). Record the date + any deviation in the M2 evidence block
in [02-milestones.md](02-milestones.md), then delete this entry.

**Optional (same session), cloud planner + the one open latency number:**

- Store the BYOK key: `uv run python -m marshmellow.tools.set_key`, then set
  `[providers.llm]` kind/base_url/model in `%LOCALAPPDATA%/Marshmellow/config.toml`.
  First cloud call doubles as the SP5 provider probe (OQ-2).
- With the key stored, re-run `uv run python tools/latency_m2.py` variant of the
  per-step measure (or just watch the log) and fill the pending "per agent step —
  cloud RTT" cell in the M2 budget table.

## Carry-over (lower priority, not blocking M2 DoD)

- **Live-mic wake false-accept soak** (SP3 "Limitations", planned for M2): ≥ 1 h of
  ambient audio through the real watcher once the self-trained "hey marshmellow"
  model exists — before the wake word is trusted hands-free.
- **Mascot on-screen placement/feel** (M1 deferred offscreen verification): tray icon
  currently renders with the system default ("No Icon set" warning) — art is OQ-3.
