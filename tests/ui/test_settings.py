"""Settings dialog + dashboard Settings… button tests — offscreen (M5 Task 4).

The dialog runs on injected fakes (autostart state/toggle, key storage) and
the save path is routed to tmp — never the real Credential Manager, Run key,
or %LOCALAPPDATA%.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QStandardItemModel  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from marshmellow.core.bus import EventBus  # noqa: E402
from marshmellow.core.config import SafetyConfig, Settings  # noqa: E402
from marshmellow.core.contracts import TaskState  # noqa: E402
from marshmellow.tasks.manager import TaskHandle  # noqa: E402
from marshmellow.ui import settings_dialog as settings_dialog_mod  # noqa: E402
from marshmellow.ui.main_window import TasksDashboard  # noqa: E402
from marshmellow.ui.settings_dialog import SettingsDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def product_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Route every dialog save to tmp — never the real %LOCALAPPDATA%."""
    path = tmp_path / "config.toml"
    monkeypatch.setattr(settings_dialog_mod, "product_config_path", lambda: path)
    return path


# -- SettingsDialog -------------------------------------------------------------------


def test_autostart_checkbox_initializes_from_autostart_state(
    qapp: QApplication,
) -> None:
    """The Run key (autostart_state) is the source of truth at open time."""
    checked = SettingsDialog(
        Settings(), apply_autostart=lambda enabled: enabled, autostart_state=lambda: True
    )
    assert checked.autostart_check.isChecked()
    unchecked = SettingsDialog(
        Settings(), apply_autostart=lambda enabled: enabled, autostart_state=lambda: False
    )
    assert not unchecked.autostart_check.isChecked()


def test_activation_and_preset_combo_reflect_settings(qapp: QApplication) -> None:
    dialog = SettingsDialog(
        Settings(activation="hotkey"),
        apply_autostart=lambda enabled: enabled,
        autostart_state=lambda: False,
    )
    assert dialog.activation_combo.currentText() == "hotkey"
    assert dialog.safety_combo.currentText() == "standard"
    assert dialog.hotkey_edit.text() == "ctrl+shift+m"


def test_invalid_hotkey_shows_error_and_does_not_save(
    qapp: QApplication, product_config: Path
) -> None:
    saved: list[Settings] = []
    settings = Settings()
    dialog = SettingsDialog(
        settings,
        apply_autostart=lambda enabled: enabled,
        autostart_state=lambda: False,
        on_saved=saved.append,
    )
    dialog.hotkey_edit.setText("notakey")  # parse_combo raises ValueError on this
    assert dialog.error_label.text() != ""  # the live validation error is visible
    _click(dialog, "settings-save")
    assert saved == []  # on_saved never fired
    assert not product_config.exists()  # nothing persisted
    assert settings.hotkey == "ctrl+shift+m"  # the setting is untouched


def test_valid_save_calls_on_saved(
    qapp: QApplication, product_config: Path
) -> None:
    stored: list[str] = []
    applied: list[bool] = []
    saved: list[Settings] = []
    settings = Settings(activation="both")

    def fake_apply(enabled: bool) -> bool:
        applied.append(enabled)
        return enabled

    dialog = SettingsDialog(
        settings,
        apply_autostart=fake_apply,
        autostart_state=lambda: False,
        store_key=stored.append,
        on_saved=saved.append,
    )
    dialog.activation_combo.setCurrentText("hotkey")
    dialog.hotkey_edit.setText("ctrl+alt+j")
    assert dialog.error_label.text() == ""  # valid combo clears the live error
    dialog.safety_combo.setCurrentText("strict")
    dialog.key_edit.setText("sk-replace-1")
    dialog.autostart_check.setChecked(True)
    _click(dialog, "settings-save")

    assert saved == [settings]  # on_saved fired with the mutated settings
    assert applied == [True]  # the checkbox change reached the toggle
    assert settings.activation == "hotkey"
    assert settings.hotkey == "ctrl+alt+j"
    assert settings.safety.preset == "strict"
    assert settings.autostart is True
    assert stored == ["sk-replace-1"]
    assert dialog.key_edit.text() == ""  # never echoed back after storing
    assert not dialog.isVisible()  # Save closes the dialog
    assert product_config.exists()
    assert "sk-replace-1" not in product_config.read_text(encoding="utf-8")  # D7


def test_autostart_not_toggled_when_already_in_requested_state(
    qapp: QApplication, product_config: Path
) -> None:
    applied: list[bool] = []

    def fake_apply(enabled: bool) -> bool:
        applied.append(enabled)
        return enabled

    dialog = SettingsDialog(
        Settings(),
        apply_autostart=fake_apply,
        autostart_state=lambda: True,  # Run key already on, checkbox shows it
    )
    _click(dialog, "settings-save")
    assert applied == []  # no redundant registry write
    assert dialog._settings.autostart is True


# -- §3.13 preset: escalate, never lower (Global Constraint, M5 plan:36-38) ------------


def test_preset_cannot_be_lowered(qapp: QApplication, product_config: Path) -> None:
    """A lower preset is refused on Save: nothing persisted, on_saved unfired."""
    saved: list[Settings] = []
    settings = Settings()  # standard — the floor this dialog may not go below
    dialog = SettingsDialog(
        settings,
        apply_autostart=lambda enabled: enabled,
        autostart_state=lambda: False,
        on_saved=saved.append,
    )
    assert _preset_item_enabled(dialog, "relaxed") is False  # disabled in the popup
    assert _preset_item_enabled(dialog, "strict") is True  # escalation stays open
    dialog.safety_combo.setCurrentText("relaxed")  # setCurrentIndex ignores the flag
    _click(dialog, "settings-save")
    assert dialog.error_label.text() != ""  # the refusal is explained
    assert saved == []  # on_saved never fired
    assert not product_config.exists()  # nothing persisted
    assert settings.safety.preset == "standard"  # the floor holds


def test_strict_floor_disables_both_lower_preset_items(qapp: QApplication) -> None:
    """A strict-configured dialog can only stay strict — lower items are locked."""
    dialog = SettingsDialog(
        Settings(safety=SafetyConfig(preset="strict")),
        apply_autostart=lambda enabled: enabled,
        autostart_state=lambda: False,
    )
    assert _preset_item_enabled(dialog, "relaxed") is False
    assert _preset_item_enabled(dialog, "standard") is False
    assert _preset_item_enabled(dialog, "strict") is True


def test_relaxed_floor_leaves_everything_selectable(qapp: QApplication) -> None:
    """The lowest preset is no floor at all: standard and strict stay open."""
    dialog = SettingsDialog(
        Settings(safety=SafetyConfig(preset="relaxed")),
        apply_autostart=lambda enabled: enabled,
        autostart_state=lambda: False,
    )
    assert _preset_item_enabled(dialog, "relaxed") is True
    assert _preset_item_enabled(dialog, "standard") is True
    assert _preset_item_enabled(dialog, "strict") is True


# -- dashboard "Settings…" button -------------------------------------------------------


class _EmptyDirectory:
    """Minimal TaskDirectory double: an empty table, no control ops."""

    def list_tasks(self, state: TaskState | None = None) -> list[TaskHandle]:
        return []

    def control(self, task_id: str, cmd: Literal["pause", "resume", "cancel", "run_now"]) -> bool:
        return False


def test_dashboard_settings_button_calls_open_settings(qapp: QApplication) -> None:
    opened: list[bool] = []
    dash = TasksDashboard(
        bus=EventBus(),
        tasks=_EmptyDirectory(),  # type: ignore[arg-type]
        open_settings=lambda: opened.append(True),
    )
    button = dash.findChild(QPushButton, "settings-button")
    assert button is not None
    assert button.text().startswith("Settings")
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert opened == [True]


def test_dashboard_without_open_settings_has_no_button(qapp: QApplication) -> None:
    dash = TasksDashboard(bus=EventBus(), tasks=_EmptyDirectory())  # type: ignore[arg-type]
    assert dash.findChild(QPushButton, "settings-button") is None


# -- helpers --------------------------------------------------------------------------


def _click(dialog: SettingsDialog, button_name: str) -> None:
    button = dialog.findChild(QPushButton, button_name)
    assert button is not None, f"missing button {button_name}"
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)


def _preset_item_enabled(dialog: SettingsDialog, preset: str) -> bool:
    """Whether the preset is selectable in the combo popup (item flag, M5 fix)."""
    combo = dialog.safety_combo
    idx = combo.findText(preset)
    assert idx >= 0, f"preset {preset!r} not in the combo"
    item = cast(QStandardItemModel, combo.model()).item(idx)
    assert item is not None
    return item.isEnabled()
