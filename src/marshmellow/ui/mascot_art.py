"""Parametric mascot art — the white marshmallow painter (M6-T1, closes OQ-3).

OQ-3 decision (c): live parametric vector rendering — one code-drawn design,
deterministic per-frame painting, no new dependencies. This module is the single
art source: the widget (M6-T2) paints it live while a state animates, and
`tools/export_mascot_assets.py` renders the same functions into the identity
stills under `assets/mascot/` (tray/window/exe icons follow via `--ico` in M6-T5).

Contract:
    paint(painter, state, phase, size)  — draw one full frame into an *active*
        QPainter whose logical origin (0, 0) is the top-left of a size × size
        cell; the painter state is saved/restored.
    render_image(state, phase, size)    — headless equivalent returning a
        Format_ARGB32_Premultiplied QImage (the export tool and the determinism
        tests drive this).

`phase` is the normalized animation clock in [0, 1) — every geometric quantity
is a pure function of (state, phase, size): no clock, no randomness, no text,
so identical inputs render bit-identical frames (tests/ui/test_mascot_art.py).
`PERIODS` maps each state to its wall-clock animation period in seconds; the
widget converts elapsed time → phase and only ticks a timer while a state with
a nonzero period is live (perf budget §3.10: 0% idle CPU). "muted" is the
flat-line state and never animates.

Design language: a white marshmallow cylinder (rounded rect body, soft ground
shadow, two eyes + a state mouth), wrapped in an aura ring in the state color
reused from `ui.mascot._STATE_COLORS`.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)

from marshmellow.ui.mascot import _STATE_COLORS, MASCOT_STATES, MascotState

# Wall-clock seconds per animation period; phase = (elapsed % period) / period.
PERIODS: dict[MascotState, float] = {
    "idle": 2.0,        # bob: sine, 2 s (M6 plan)
    "listening": 0.9,   # squash pulse
    "thinking": 2.4,    # orbit dots
    "acting": 1.6,      # shimmer sweep
    "speaking": 0.5,    # mouth flap (two flaps per period)
    "error": 0.7,       # shake (three excursions per period, decaying)
    "muted": 0.0,       # flat-line: static, never animates
    "attention": 1.2,   # aura pulse
}

_BODY_TOP_GRADIENT = "#ffffff"
_BODY_BOTTOM_GRADIENT = "#efeaef"  # subtle cylinder shading
_MUTED_BODY_BOTTOM = "#e7e5e9"  # desaturated body while muted
_FACE_INK = "#3a333d"  # warm dark gray for eyes/mouth
_THROB = math.tau  # one full sine cycle over phase ∈ [0, 1)


def paint(painter: QPainter, state: MascotState, phase: float, size: int) -> None:
    """Draw one frame of `state` at normalized animation clock `phase` ∈ [0, 1)."""
    if state not in MASCOT_STATES:
        raise ValueError(f"unknown mascot state: {state!r}")
    if not 0.0 <= phase < 1.0:
        raise ValueError(f"phase must be in [0, 1), got {phase!r}")
    if size <= 0:
        raise ValueError(f"size must be positive, got {size!r}")

    s = float(size)
    wob = _THROB * phase  # one sine cycle across the period

    # Per-state motion (dx, dy) and squash (sx, sy) about the body's bottom.
    dx, dy, sx, sy = 0.0, 0.0, 1.0, 1.0
    pulse = 0.0
    if state == "idle":
        dy = -0.018 * s * math.sin(wob)  # bob (sine, 2 s)
    elif state == "listening":
        sy = 1.0 + 0.05 * math.sin(wob)
        sx = 1.0 - 0.04 * math.sin(wob)
    elif state == "error":
        dx = 0.032 * s * math.sin(3.0 * wob) * (1.0 - 0.4 * phase)
    elif state == "attention":
        pulse = 0.5 + 0.5 * math.sin(wob)

    cx = s / 2.0
    body_w, body_h = 0.50 * s, 0.60 * s
    body = QRectF(cx - body_w / 2.0, 0.14 * s, body_w, body_h)
    bottom = body.bottom()
    radius = 0.22 * body_w

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)

    _ground_shadow(painter, body, cx, dy / (-0.018 * s), s)
    _aura_ring(painter, body, radius, state, pulse, s)
    _body_and_face(painter, body, radius, state, wob, dx, sx, sy, cx, bottom, s)
    if state == "thinking":
        _orbit_dots(painter, cx, body.top(), phase, s)

    painter.restore()


def render_image(state: MascotState, phase: float, size: int) -> QImage:
    """Headless single-frame render (export tool + determinism tests)."""
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    paint(painter, state, phase, size)
    painter.end()
    return img


def _ground_shadow(painter: QPainter, body: QRectF, cx: float, lift: float, s: float) -> None:
    """Soft elliptical ground shadow; lighter and narrower as the bob lifts."""
    lift = max(0.0, min(1.0, lift))
    alpha = int(70 - 35 * lift)
    rx = (0.30 - 0.02 * lift) * s
    cy = body.bottom() + 0.06 * s
    painter.save()
    painter.translate(cx, cy)
    painter.scale(1.0, 0.18)
    grad = QRadialGradient(QPointF(0.0, 0.0), rx)
    grad.setColorAt(0.0, QColor(30, 26, 34, alpha))
    grad.setColorAt(1.0, QColor(30, 26, 34, 0))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.fillRect(QRectF(-rx, -rx, 2.0 * rx, 2.0 * rx), grad)
    painter.restore()


def _aura_ring(
    painter: QPainter, body: QRectF, radius: float, state: MascotState, pulse: float, s: float
) -> None:
    """State-colored aura ring around the body (colors from ui.mascot._STATE_COLORS)."""
    if state == "attention":
        expand = (0.050 + 0.030 * pulse) * s
    else:
        expand = 0.050 * s
    aura = body.adjusted(-expand, -expand, expand, expand)
    color = QColor(_STATE_COLORS[state])
    color.setAlphaF(0.55 + 0.35 * pulse if state == "attention" else 0.80)
    pen = QPen(color, max(1.0, 0.022 * s))
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(aura, radius + expand, radius + expand)
    painter.setPen(Qt.PenStyle.NoPen)


def _body_and_face(
    painter: QPainter,
    body: QRectF,
    radius: float,
    state: MascotState,
    wob: float,
    dx: float,
    sx: float,
    sy: float,
    cx: float,
    bottom: float,
    s: float,
) -> None:
    """Marshmallow body (squash/shake transformed) + eyes and mouth per state."""
    painter.save()
    painter.translate(dx, 0.0)  # error shake
    painter.translate(cx, bottom)  # squash anchored at the body's base
    painter.scale(sx, sy)
    painter.translate(-cx, -bottom)

    grad = QLinearGradient(0.0, body.top(), 0.0, bottom)
    grad.setColorAt(0.0, QColor(_BODY_TOP_GRADIENT))
    grad.setColorAt(1.0, QColor(_MUTED_BODY_BOTTOM if state == "muted" else _BODY_BOTTOM_GRADIENT))
    painter.setBrush(grad)
    outline = QColor(0, 0, 0, 30)
    pen = QPen(outline, max(1.0, 0.010 * s))
    painter.setPen(pen)
    painter.drawRoundedRect(body, radius, radius)

    if state == "acting":
        _shimmer(painter, body, radius, wob, s)

    _eyes(painter, body, state, s)
    _mouth(painter, body, state, wob, s)
    painter.restore()


def _eyes(painter: QPainter, body: QRectF, state: MascotState, s: float) -> None:
    """Two eyes; look-up while thinking, half-lidded lines while muted."""
    ink = QColor(_FACE_INK)
    cx = body.center().x()
    ex = 0.150 * body.width()  # eye offset from center
    ey = body.top() + 0.36 * body.height() - (0.012 * s if state == "thinking" else 0.0)
    rx, ry = 0.025 * s, 0.032 * s
    if state == "listening":
        ry *= 1.15  # a touch wider — open to the user
    if state == "muted":
        pen = QPen(ink, max(1.0, 0.020 * s))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for side in (-1.0, 1.0):
            x = cx + side * ex
            painter.drawLine(QPointF(x - rx, ey), QPointF(x + rx, ey))
        return
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(ink)
    for side in (-1.0, 1.0):
        painter.drawEllipse(QPointF(cx + side * ex, ey), rx, ry)
    if state == "error":
        # angled brows — the frustrated look
        pen = QPen(ink, max(1.0, 0.018 * s))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        for side in (-1.0, 1.0):
            x = cx + side * ex
            painter.drawLine(
                QPointF(x - side * rx * 1.6, ey - ry * 2.1),
                QPointF(x + side * rx * 0.4, ey - ry * 1.2),
            )


def _mouth(painter: QPainter, body: QRectF, state: MascotState, wob: float, s: float) -> None:
    """State mouth: smile / o / line / talk flap / frown / flat-line."""
    ink = QColor(_FACE_INK)
    cx = body.center().x()
    my = body.top() + 0.58 * body.height()
    if state in ("idle", "acting"):
        pen = QPen(ink, max(1.0, 0.020 * s))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        mw = 0.060 * s if state == "acting" else 0.048 * s
        path = QPainterPath(QPointF(cx - mw, my))
        path.quadTo(QPointF(cx, my + 0.030 * s), QPointF(cx + mw, my))
        painter.drawPath(path)
    elif state in ("listening", "attention"):
        rx = 0.018 * s if state == "listening" else 0.014 * s
        ry = 0.026 * s if state == "listening" else 0.020 * s
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        painter.drawEllipse(QPointF(cx, my), rx, ry)
    elif state == "thinking":
        pen = QPen(ink, max(1.0, 0.018 * s))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(cx - 0.030 * s, my), QPointF(cx + 0.030 * s, my))
    elif state == "speaking":
        # talk flap: two full open/close cycles per animation period
        open_h = abs(math.sin(2.0 * wob))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        painter.drawEllipse(
            QPointF(cx, my), 0.030 * s, (0.012 + 0.036 * open_h) * s
        )
    elif state == "error":
        pen = QPen(ink, max(1.0, 0.020 * s))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        mw = 0.048 * s
        path = QPainterPath(QPointF(cx - mw, my + 0.012 * s))
        path.quadTo(QPointF(cx, my - 0.020 * s), QPointF(cx + mw, my + 0.012 * s))
        painter.drawPath(path)
    else:  # muted: the flat-line
        pen = QPen(ink, max(1.0, 0.022 * s))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(cx - 0.045 * s, my), QPointF(cx + 0.045 * s, my))


def _shimmer(painter: QPainter, body: QRectF, radius: float, wob: float, s: float) -> None:
    """Acting shimmer: a warm band sweeping across the body once per period."""
    path = QPainterPath()
    path.addRoundedRect(body, radius, radius)
    painter.save()
    painter.setClipPath(path)
    sweep = body.left() - 0.3 * s + (body.width() + 0.6 * s) * (wob / _THROB)
    painter.translate(sweep, body.center().y())
    painter.rotate(-20.0)
    grad = QLinearGradient(-0.09 * s, 0.0, 0.09 * s, 0.0)
    gold = QColor(_STATE_COLORS["acting"])
    grad.setColorAt(0.0, QColor(gold.red(), gold.green(), gold.blue(), 0))
    grad.setColorAt(0.5, QColor(gold.red(), gold.green(), gold.blue(), 110))
    grad.setColorAt(1.0, QColor(gold.red(), gold.green(), gold.blue(), 0))
    painter.setBrush(grad)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRect(QRectF(-0.09 * s, -1.2 * s, 0.18 * s, 2.4 * s))
    painter.restore()


def _orbit_dots(painter: QPainter, cx: float, body_top: float, phase: float, s: float) -> None:
    """Thinking orbit: three dots circling a halo above the head."""
    center = QPointF(cx, body_top - 0.02 * s)
    orbit_r = 0.13 * s
    for i in range(3):
        angle = _THROB * phase + i * _THROB / 3.0
        depth = 0.5 + 0.5 * math.sin(angle)  # front of the orbit: bigger + brighter
        pos = QPointF(
            center.x() + orbit_r * math.cos(angle),
            center.y() + 0.45 * orbit_r * math.sin(angle),
        )
        color = QColor(_STATE_COLORS["thinking"])
        color.setAlphaF(0.28 + 0.59 * depth)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        r = (0.022 + 0.014 * depth) * s
        painter.drawEllipse(pos, r, r)
