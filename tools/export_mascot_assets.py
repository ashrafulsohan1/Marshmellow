"""Export the mascot identity assets from the parametric painter (M6-T1, OQ-3).

    uv run python tools/export_mascot_assets.py [--out assets/mascot] [--size 256]

Renders one still per mascot state (`{state}.png`) plus `preview.png`, a contact
sheet. Everything is generated from `marshmellow.ui.mascot_art` — no third-party
art (origin note lives in `assets/mascot/README.md`). Deterministic: each state
renders at a fixed canonical phase (the frame that best characterizes its
animation), so re-running reproduces byte-identical files.

The sheet carries no text: cells are in `MASCOT_STATES` order (idle, listening,
thinking, acting, speaking, error, muted, attention — also printed by the tool),
and the offscreen Qt platform has no font database anyway. Text-free rendering
keeps the sheet identical on every machine.

The `--ico` mode (tray/window/exe icons, F4) lands in M6-T5 per the M6 plan.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# QT_QPA_PLATFORM must be set before the first PySide6 import (headless export).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402

from marshmellow.ui.mascot import MASCOT_STATES, MascotState  # noqa: E402
from marshmellow.ui.mascot_art import render_image  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# Fixed per-state phase — the frame that best characterizes the animation.
CANONICAL_PHASES: dict[MascotState, float] = {
    "idle": 0.0,        # rest pose
    "listening": 0.25,  # deepest squash
    "thinking": 0.20,   # orbit dots spread across the arc
    "acting": 0.50,     # shimmer mid-sweep
    "speaking": 0.125,  # mouth fully open (flap peak)
    "error": 0.125,     # shake at its first excursion
    "muted": 0.0,       # flat-line (static)
    "attention": 0.25,  # aura pulse peak
}

_SHEET_COLS, _SHEET_ROWS = 4, 2
_MARGIN, _GAP = 28.0, 18.0
_BG = "#f7f6f8"


def _contact_sheet(size: int) -> QImage:
    """Grid of all 8 states (canonical phase each) in MASCOT_STATES order."""
    cols, rows = _SHEET_COLS, _SHEET_ROWS
    w = int(_MARGIN * 2 + cols * size + (cols - 1) * _GAP)
    h = int(_MARGIN * 2 + rows * size + (rows - 1) * _GAP)
    sheet = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(QColor(_BG))
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    for i, state in enumerate(MASCOT_STATES):
        col, row = i % cols, i // cols
        x = _MARGIN + col * (size + _GAP)
        y = _MARGIN + row * (size + _GAP)
        img = render_image(state, CANONICAL_PHASES[state], size)
        painter.drawImage(QRectF(x, y, size, size).toRect(), img)
    painter.end()
    return sheet


def main() -> int:
    parser = argparse.ArgumentParser(description="Export mascot identity assets (M6-T1).")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "assets" / "mascot")
    parser.add_argument("--size", type=int, default=256, help="still size in px (square)")
    args = parser.parse_args()
    if args.size <= 0:
        parser.error("--size must be positive")

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    print(f"preview cell order: {', '.join(MASCOT_STATES)}")

    for state in MASCOT_STATES:
        img = render_image(state, CANONICAL_PHASES[state], args.size)
        path = out / f"{state}.png"
        if not img.save(str(path), "PNG"):
            print(f"FAIL: could not write {path}", file=sys.stderr)
            return 1
        print(f"wrote {path.relative_to(REPO_ROOT)} ({path.stat().st_size} bytes)")

    preview = out / "preview.png"
    if not _contact_sheet(args.size).save(str(preview), "PNG"):
        print(f"FAIL: could not write {preview}", file=sys.stderr)
        return 1
    print(f"wrote {preview.relative_to(REPO_ROOT)} ({preview.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
