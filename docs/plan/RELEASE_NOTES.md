# Marshmellow v0.1.2 — Release Notes

**Date:** 2026-11-08 · **Status:** private preview · **Platform:** Windows 10/11
**Upgrade:** replaces the v0.1.0 bundle — unzip and run; `config.toml` and stored BYOK keys carry
over. (v0.1.0 notes: [RELEASE-NOTES-v0.1.0.md](RELEASE-NOTES-v0.1.0.md))

## What is Marshmellow

A hands-free, voice-controlled Windows desktop agent: say what you want, watch it happen on
screen, hear the result spoken back. An ambient assistant that lives in the tray, wears a
marshmallow mascot, and operates the computer the way a person would — one generic
perceive → act → verify protocol (Windows UI Automation first, screenshot+vision fallback), a
risk-gated action layer (L0–L3), and a BYOK cloud/local LLM planner.

## What changed since v0.1.0 (M6 release-feedback round)

### The mascot is designed (OQ-3 closed)
A code-drawn white-marshmallow identity — one parametric vector design animated across all 8
states (idle, listening, thinking, acting, speaking, error, muted, attention). Deterministic
frames, 30 fps only while animating, **0% CPU idle**. The same art source drives the tray icon
(state-colored), the window icon, and the exe icon — no more system-default tray warning.

### Mascot placement
Default position bottom-right (near the tray); drag it anywhere; the position persists across
restarts and is clamped back on screen if a saved monitor disappears. Input is masked to the
marshmallow body — the screen around it is never blocked.

### Turn-taking rework
"hey marshmellow" (or the hotkey) opens a **receiving window**: speak freely — multi-sentence
instructions with short pauses stay one instruction; **3 seconds of silence closes the window** and
execution begins. While Marshmellow speaks, the microphone ingest is hard-gated (half-duplex): the
app cannot hear its own voice, and media audio without a wake never becomes an instruction.
Fixture-measured: **zero own-voice ingestion** through the real capture path.

### Live-use safety fixes (from your feedback)
- Closing a browser **tab** can no longer close the **whole browser**: window-killer hotkeys
  (`alt+f4`, `ctrl+shift+w`, `ctrl+shift+f4`) now require spoken confirmation, and the planner
  carries an explicit tabs-are-not-windows rule.
- **User-first live-status check**: the planner checks whether an app is your foreground window
  before typing, clicking, or closing in it.
- A turn-window timeout no longer truncates the tail of your sentence.
- Tray: "Stop tasks" now also kills mid-speech TTS; Quit exits every helper process.

## Known limitations (honest)

- The self-trained wake model measured **7 false accepts/hour** in a 1-hour ambient soak at
  threshold 0.99 — **the hotkey remains the trusted activation channel**; treat hands-free wake as
  experimental until the soak numbers improve. The model ships in the bundle; the trade-off is
  recorded in `assets/wake/calibration.json`.
- Five medium security findings from the v0.1.0 audit (spoken-secret log scrubbing, monitor-URL
  confirmation, screenshot retention, spoken rationale, tray-stop semantics) are scheduled for
  v0.1.2 — see `docs/plan/RELEASE-AUDIT-m6.md`.
- Voice barge-in during TTS is replaced by hotkey barge-in (see turn-taking above).

## Verify & configure

```powershell
# after unzipping: Marshmellow.exe --smoke  → "smoke ok"
# BYOK key (stored in Windows Credential Manager, never on disk):
uv run python -m marshmellow.tools.set_key
```

Point `[providers.llm]` at your OpenAI-compatible endpoint in
`%LOCALAPPDATA%/Marshmellow/config.toml`. Full walkthrough: `docs/user-guide.md`; privacy:
`docs/privacy.md`; the complete build record: `docs/plan/`.
