"""Tray icon — the app's home when the main UI is dismissed (01-architecture §3.11).

Menu actions publish TrayAction on the bus; the engine (not the UI) decides
what mute/pause/quit mean. The tray carries the app identity (M6-T5): the
icon is a per-state mascot still from `assets/mascot/` and follows the
mascot state via MascotStateEvent — idle, muted and the §3.12 attention ask
read differently at a glance, so the tray is a status surface too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from marshmellow.core.assets import asset_dir
from marshmellow.core.bus import EventBus
from marshmellow.ui.mascot import MASCOT_STATES, MascotState, MascotStateEvent

TrayActionKind = Literal["mute", "unmute", "open_ui", "pause", "quit"]

_MENU: tuple[tuple[str, TrayActionKind], ...] = (
    ("Mute", "mute"),
    ("Unmute", "unmute"),
    ("Open Marshmellow", "open_ui"),
    ("Stop tasks", "pause"),
    ("Quit", "quit"),
)

_ICON_PX = 64  # stills are 256 px; the tray scales down (Windows draws ~16-32 px)


class TrayAction(BaseModel):
    kind: TrayActionKind


def mascot_asset_root() -> Path:
    """Where the identity stills live: ``<asset root>/mascot`` — the repo
    checkout in dev, ``_internal/mascot`` in the frozen one-dir bundle (the
    spec datas mirror the directory one level below ``asset_dir()``)."""
    return asset_dir() / "mascot"


class MascotIconProvider:
    """Per-state identity icons from the `assets/mascot/` stills (M6-T5).

    Primary source is the committed still for the state — the exact art the
    exe icon and the preview sheet carry. If a still is missing from a
    checkout, the parametric painter (`ui.mascot_art`, the single art source
    the exporter renders from) re-renders the state, so the icon is never
    null: a broken asset directory degrades the tray, it never crashes it.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else mascot_asset_root()
        self._cache: dict[MascotState, QIcon] = {}

    def icon(self, state: MascotState) -> QIcon:
        cached = self._cache.get(state)
        if cached is not None:
            return cached
        icon = self._load(state)
        self._cache[state] = icon
        return icon

    def _load(self, state: MascotState) -> QIcon:
        still = self._root / f"{state}.png"
        if still.exists():
            return QIcon(str(still))
        from marshmellow.ui.mascot_art import render_image  # lazy: painter import

        return QIcon(QPixmap.fromImage(render_image(state, 0.0, _ICON_PX)))


def app_icon() -> QIcon:
    """The application identity icon for windows/taskbar: the generated
    `marshmellow.ico` when it ships (the same file the installer stamps into
    the exe), else the idle still."""
    root = mascot_asset_root()
    ico = root / "marshmellow.ico"
    if ico.exists():
        return QIcon(str(ico))
    return MascotIconProvider(root).icon("idle")


class Tray(QObject):
    """The system tray entry: menu actions + the state-mirroring identity icon.

    Icon updates arrive on the bus drain thread (MascotStateEvent); the
    QSystemTrayIcon lives on the GUI thread, so they hop over a queued Qt
    signal — the same never-touch-a-widget-off-thread pattern as Mascot.
    """

    _state_requested = Signal(str)

    def __init__(self, bus: EventBus, icon_provider: MascotIconProvider | None = None) -> None:
        super().__init__()
        self._bus = bus
        self._icon_provider = (
            icon_provider if icon_provider is not None else MascotIconProvider()
        )
        self._icon = QSystemTrayIcon()
        self._icon.setToolTip("Marshmellow")
        self._icon.setIcon(self._icon_provider.icon("idle"))
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
        # M6-T5: mirror the mascot state (subscribed here, applied on the GUI
        # thread via the queued signal above).
        self._state_requested.connect(self._apply_icon)
        self._bus.subscribe(MascotStateEvent, self._on_mascot_state)

    @property
    def icon(self) -> QIcon:
        """The QIcon currently set on the system tray entry."""
        return self._icon.icon()

    def icon_for(self, state: MascotState) -> QIcon:
        """The identity icon for one mascot state (the provider's mapping)."""
        return self._icon_provider.icon(state)

    def action(self, kind: TrayActionKind) -> QAction:
        return self._actions[kind]

    def show(self) -> None:
        self._icon.show()

    def _on_mascot_state(self, event: MascotStateEvent) -> None:
        self._state_requested.emit(event.state)  # queued onto the GUI thread

    def _apply_icon(self, state: str) -> None:
        if state not in MASCOT_STATES:  # belt and braces: never blank the tray
            return
        self._icon.setIcon(self._icon_provider.icon(cast(MascotState, state)))


def confirm_dialog(question: str) -> bool:
    """§3.13 tray fallback when voice confirmation is ambiguous: a blocking
    yes/no QMessageBox. Must run on the GUI thread (composition marshals it
    there). Defaults to No and Escape dismisses — fail closed either way.
    Offscreen-testable (QT_QPA_PLATFORM=offscreen, see tests/conftest.py)."""
    if QApplication.instance() is None:  # offscreen tests may not have one yet
        QApplication([])
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("Marshmellow")
    box.setText(question)
    box.addButton(QMessageBox.StandardButton.Yes)
    box.addButton(QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    # exec() returns the StandardButton value (Yes on accept; 0 on Escape/reject) —
    # compare the numeric value so offscreen tests that done() the dialog also work.
    return box.exec() == int(QMessageBox.StandardButton.Yes)


class GuiMarshaledConfirm(QObject):
    """Composition's GUI-thread marshal for the tray confirm fallback (§3.13).

    ``confirm_dialog`` must run on the GUI thread (QMessageBox creation and
    exec()); the safety gate calls its fallback from a planner worker thread.
    This marshal re-emits the request over a BlockingQueuedConnection: the
    calling worker blocks until the GUI thread has shown and answered the
    dialog. Called on the GUI thread itself it degrades to a direct call (a
    same-thread blocking-queued emit would deadlock).

    Known limit (documented M2 behavior): the calling thread waits for the
    user's answer, so a kill during the dialog is applied AFTER it returns —
    the gate re-checks the kill probe post-confirmation and refuses; the
    dialog itself stays fail-closed (No / Escape).
    """

    _requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._result = False
        self._requested.connect(self._show, Qt.ConnectionType.BlockingQueuedConnection)

    def __call__(self, question: str) -> bool:
        if QThread.currentThread() is self.thread():  # already the GUI thread
            return self._show(question)
        # Blocks the calling worker until _show() has stored the answer on the
        # GUI thread — the blocking emit is the ordering, no lock needed (a lock
        # held across emit would deadlock _show on the GUI thread).
        self._requested.emit(question)
        return self._result

    def _show(self, question: str) -> bool:
        self._result = confirm_dialog(question)
        return self._result
