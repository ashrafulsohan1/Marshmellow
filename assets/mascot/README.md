# assets/mascot — the generated mascot identity art (OQ-3)

M6 Task 1. **Origin: project-generated.** Every file here is rendered by the
parametric painter at [`src/marshmellow/ui/mascot_art.py`](../../src/marshmellow/ui/mascot_art.py)
via [`tools/export_mascot_assets.py`](../../tools/export_mascot_assets.py) — one
code-drawn white marshmallow animated across all 8 agent states (OQ-3 decision
(c): live parametric vector; no sprite sheets, no Lottie, no third-party or
AI-generated bitmaps anywhere in this directory).

## Regenerate

```sh
uv run python tools/export_mascot_assets.py
```

The render is deterministic: every geometric quantity is a pure function of
`(state, phase, size)` (no clock, no randomness, no text), and each state exports
at a fixed canonical phase — re-running the tool reproduces **byte-identical**
PNGs (verified 2026-11-07: two consecutive runs, identical SHA-256 for all nine
files). Edits to the art are made in `mascot_art.py`, never by touching these
PNGs by hand.

## Files

| File | What it is |
|---|---|
| `idle.png` … `attention.png` | one 256 × 256 still per `MascotState`, at the state's canonical phase (`CANONICAL_PHASES` in the export tool) |
| `preview.png` | 4 × 2 contact sheet, cells in `MASCOT_STATES` order: idle, listening, thinking, acting, speaking, error, muted, attention (text-free — the headless Qt platform has no font database, and text would make the sheet machine-dependent) |

Per-state animation (normalized clock `phase ∈ [0, 1)`, periods in
`mascot_art.PERIODS`): idle bob (sine, 2 s) · listening squash pulse · thinking
orbit dots · acting shimmer sweep · speaking mouth flap · error shake · muted
flat-line (static) · attention aura pulse.

## Consumers

- **M6-T2**: the mascot widget paints `mascot_art.paint` live (30 fps timer only
  while a nonzero-period state animates; idle renders one static frame).
- **M6-T5**: `tools/export_mascot_assets.py --ico` derives the tray/window/exe
  icons from the same painter (not yet implemented — this task ships stills only).

The PNGs are app assets, same license as the project; nothing here restricts
redistribution (no third-party inputs, unlike `assets/wake/` which carries the
extractor license notes).
