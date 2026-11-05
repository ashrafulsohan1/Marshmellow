"""Mascot overlay — the always-on-top ambient presence (01-architecture §3.10).

Placeholder art: a rounded marshmallow whose fill color encodes agent state
(real art pipeline per OQ-3). Idle is click-through so it never blocks the
screen; any active state accepts clicks (which publish MascotClicked).

`set_state` is called from engine threads (bus drain, voice loop, task
workers) — it emits a Qt signal that lands on the GUI thread via a queued
connection, so no widget mutation ever happens off the Qt main thread.
"attention" is the §3.12 D6 foreground-lease ask: a background task wants the
screen and is waiting for the user's consent.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from marshmellow.core.bus import EventBus

MascotState = Literal[
    "idle", "listening", "thinking", "acting", "speaking", "error", "muted", "attention"
]
MASCOT_STATES: tuple[MascotState, ...] = (
    "idle", "listening", "thinking", "acting", "speaking", "error", "muted", "attention",
)

_STATE_COLORS: dict[MascotState, str] = {
    "idle": "#e8e8e8",
    "listening": "#7fd4ff",
    "thinking": "#c9a7ff",
    "acting": "#ffd47f",
    "speaking": "#9fe8b8",
    "error": "#ff8f8f",
    "muted": "#9a9a9a",
    "attention": "#f6a5c0",  # foreground-lease ask (§3.12 D6): consent wanted
}


class MascotClicked(BaseModel):
    pass


class Mascot(QWidget):
    _state_requested = Signal(str)

    def __init__(self, bus: EventBus | None = None) -> None:
        super().__init__()
        self._bus = bus
        self._state: MascotState = "idle"
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(64, 64)
        self._apply_click_through()
        self._state_requested.connect(self._apply_state)  # queued from other threads

    @property
    def state(self) -> MascotState:
        return self._state

    def set_state(self, state: MascotState) -> None:
        """Thread-safe: on the GUI thread this is direct, elsewhere queued."""
        self._state_requested.emit(state)

    def _apply_state(self, state: str) -> None:
        if state not in _STATE_COLORS:
            raise ValueError(f"unknown mascot state: {state!r}")
        self._state = state  # type: ignore[assignment]
        self._apply_click_through()
        self.update()

    def _apply_click_through(self) -> None:
        flag = Qt.WindowType.WindowTransparentForInput
        self.setWindowFlag(flag, self._state == "idle")

    def mousePressEvent(self, event: object) -> None:
        if self._bus is not None:
            self._bus.emit_threadsafe(MascotClicked())
        event.accept()  # type: ignore[attr-defined]

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_STATE_COLORS[self._state]))
        painter.drawRoundedRect(self.rect().adjusted(6, 6, -6, -6), 18, 18)
        painter.end()
