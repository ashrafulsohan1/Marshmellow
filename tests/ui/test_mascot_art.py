"""Parametric mascot art (M6-T1, closes OQ-3): determinism, coverage, phase bounds.

The painter is the single art source for the mascot (widget in M6-T2, exported
stills via tools/export_mascot_assets.py), so its contract is pinned here:
identical (state, phase, size) → bit-identical frame; every state paints at
widget + asset sizes; the normalized animation clock is strictly [0, 1).
"""

import hashlib

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from marshmellow.ui.mascot import MASCOT_STATES  # noqa: E402
from marshmellow.ui.mascot_art import PERIODS, paint, render_image  # noqa: E402

SIZES = (16, 64, 256)  # widget floor, widget default, exported-asset size


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _hash(img: QImage) -> str:
    return hashlib.sha256(bytes(img.constBits())).hexdigest()


def test_paint_is_deterministic(qapp: QApplication) -> None:
    for state in MASCOT_STATES:
        for phase in (0.0, 0.3, 0.617):
            a = _hash(render_image(state, phase, 64))
            b = _hash(render_image(state, phase, 64))
            assert a == b, (state, phase)


def test_all_states_paint_without_error_at_all_sizes(qapp: QApplication) -> None:
    for state in MASCOT_STATES:
        for size in SIZES:
            img = render_image(state, 0.4, size)
            assert not img.isNull()
            assert img.width() == size and img.height() == size
            # not blank: marshmallow body + aura must actually produce pixels
            assert any(bytes(img.constBits())[3::4]), (state, size)


def test_paint_draws_into_a_caller_supplied_painter(qapp: QApplication) -> None:
    img = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    paint(painter, "idle", 0.5, 64)
    painter.end()
    assert any(bytes(img.constBits())[3::4])


@pytest.mark.parametrize("phase", [0.0, 0.125, 0.5, 0.999])
def test_phase_in_bounds_paints_every_state(qapp: QApplication, phase: float) -> None:
    for state in MASCOT_STATES:
        assert not render_image(state, phase, 64).isNull()


@pytest.mark.parametrize("phase", [-0.001, -1.0, 1.0, 1.5, float("nan")])
def test_phase_out_of_bounds_raises(qapp: QApplication, phase: float) -> None:
    with pytest.raises(ValueError):
        render_image("idle", phase, 64)


def test_unknown_state_raises(qapp: QApplication) -> None:
    with pytest.raises(ValueError):
        render_image("vibrating", 0.0, 64)  # type: ignore[arg-type]


def test_phase_drives_the_frame(qapp: QApplication) -> None:
    # the bob is a sine over one period — two phases must not render alike
    assert _hash(render_image("idle", 0.0, 64)) != _hash(render_image("idle", 0.25, 64))


def test_states_render_distinctly(qapp: QApplication) -> None:
    hashes = {_hash(render_image(s, 0.5, 64)) for s in MASCOT_STATES}
    assert len(hashes) == len(MASCOT_STATES)


def test_animation_periods_declared(qapp: QApplication) -> None:
    # widget-side wall-clock → phase mapping ships with the art (M6-T2 consumes it)
    assert set(PERIODS) == set(MASCOT_STATES)
    assert PERIODS["idle"] == 2.0  # bob: sine, 2 s (M6 plan)
    assert PERIODS["muted"] == 0.0  # flat-line: static, never animates
    assert all(PERIODS[s] > 0.0 for s in MASCOT_STATES if s != "muted")
