"""Export the mascot identity assets from the parametric painter (M6-T1, OQ-3).

    uv run python tools/export_mascot_assets.py [--out assets/mascot] [--size 256] [--ico]

Renders one still per mascot state (`{state}.png`) plus `preview.png`, a contact
sheet. Everything is generated from `marshmellow.ui.mascot_art` — no third-party
art (origin note lives in `assets/mascot/README.md`). Deterministic: each state
renders at a fixed canonical phase (the frame that best characterizes its
animation), so re-running reproduces byte-identical files.

The sheet carries no text: cells are in `MASCOT_STATES` order (idle, listening,
thinking, acting, speaking, error, muted, attention — also printed by the tool),
and the offscreen Qt platform has no font database anyway. Text-free rendering
keeps the sheet identical on every machine.

`--ico` (M6-T5) additionally writes `marshmellow.ico` — the exe/window/tray
identity icon, assembled by hand as a Vista+ multi-size ICO of PNG-encoded
entries (Qt's writer emits single-size files): ICONDIR + ICONDIRENTRYs over
PNG bytes of the idle still from 16 px (tray) up to 256 px (exe). The spec
stamps it onto the main exe (`installer/marshmellow.spec`) and `ui/tray.py::
app_icon` loads it for windows/taskbar.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

# QT_QPA_PLATFORM must be set before the first PySide6 import (headless export).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QRectF  # noqa: E402
from PySide6.QtGui import QColor, QImage, QImageWriter, QPainter  # noqa: E402

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

# M6-T5: the exe/tray icon ladder — 16 px (tray) up to 256 px (exe/installer).
ICO_SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)


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


# -- --ico (M6-T5): marshmellow.ico -------------------------------------------------


def _display(path: Path) -> str:
    """Repo-relative display when possible (tests export to tmp dirs)."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _png_bytes(img: QImage) -> bytes:
    """PNG-encode one frame into memory (QImage.save only writes files)."""
    buf = QBuffer()
    buf.open(QBuffer.OpenModeFlag.ReadWrite)
    writer = QImageWriter(buf, b"PNG")
    if not writer.write(img):
        raise RuntimeError(f"PNG encode failed: {writer.errorString()}")
    return bytes(buf.data())


def _ico_bytes(sizes: tuple[int, ...] = ICO_SIZES) -> bytes:
    """A multi-size ICO of the idle identity still: ICONDIR + ICONDIRENTRYs +
    PNG-encoded images (the Vista+ shell format — every entry a full PNG).
    Hand-assembled because Qt's ICO writer emits single-size files."""
    entries = [
        (size, _png_bytes(render_image("idle", CANONICAL_PHASES["idle"], size)))
        for size in sizes
    ]

    header = struct.pack("<HHH", 0, 1, len(entries))  # reserved=0, type=icon, count
    offset = 6 + 16 * len(entries)
    directory: list[bytes] = []
    for size, png in entries:
        directory.append(
            struct.pack(
                "<BBBBHHII",
                size % 256,  # 256 encodes as 0 (the 1-byte field's limit)
                size % 256,
                0,  # palette colors (PNG entries carry none)
                0,  # reserved
                1,  # planes
                32,  # bpp
                len(png),
                offset,
            )
        )
        offset += len(png)
    return header + b"".join(directory) + b"".join(png for _, png in entries)


def _write_ico(out: Path) -> Path:
    path = out / "marshmellow.ico"
    data = _ico_bytes()
    path.write_bytes(data)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Export mascot identity assets (M6-T1).")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "assets" / "mascot")
    parser.add_argument("--size", type=int, default=256, help="still size in px (square)")
    parser.add_argument(
        "--ico",
        action="store_true",
        help="also write marshmellow.ico (exe/window/tray identity icon, M6-T5)",
    )
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
        print(f"wrote {_display(path)} ({path.stat().st_size} bytes)")

    preview = out / "preview.png"
    if not _contact_sheet(args.size).save(str(preview), "PNG"):
        print(f"FAIL: could not write {preview}", file=sys.stderr)
        return 1
    print(f"wrote {_display(preview)} ({preview.stat().st_size} bytes)")

    if args.ico:
        try:
            ico_path = _write_ico(out)
        except RuntimeError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {_display(ico_path)} ({ico_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
