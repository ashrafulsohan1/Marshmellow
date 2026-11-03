"""Tray icon — the app's home when the main UI is dismissed (01-architecture §3.11).

Menu actions publish TrayAction on the bus; the engine (not the UI) decides
what mute/pause/quit mean.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from marshmellow.core.bus import EventBus

TrayActionKind = Literal["mute", "unmute", "open_ui", "pause", "quit"]

_MENU: tuple[tuple[str, TrayActionKind], ...] = (
    ("Mute", "mute"),
    ("Unmute", "unmute"),
    ("Open Marshmellow", "open_ui"),
    ("Pause tasks", "pause"),
    ("Quit", "quit"),
)


class TrayAction(BaseModel):
    kind: TrayActionKind


class Tray:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._icon = QSystemTrayIcon()
        self._icon.setToolTip("Marshmellow")
        # QSystemTrayIcon.setContextMenu does NOT take ownership — keep a
        # reference or Python GC deletes the menu (and its actions) mid-run.
        self._menu = QMenu()
        menu = self._menu
        self._actions: dict[TrayActionKind, QAction] = {}
        for label, kind in _MENU:
            act = menu.addAction(label)
            act.triggered.connect(
                lambda checked=False, k=kind: self._bus.emit_threadsafe(TrayAction(kind=k))
            )
            self._actions[kind] = act
        self._icon.setContextMenu(menu)

    def action(self, kind: TrayActionKind) -> QAction:
        return self._actions[kind]

    def show(self) -> None:
        self._icon.show()
