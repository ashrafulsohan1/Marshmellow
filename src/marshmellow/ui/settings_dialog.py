"""Settings dialog (M5 Task 4, D2/D3/D7, §3.13 preset) — opened from the dashboard.

Modal on the GUI thread — and safe next to the engine: the spoken-confirm
fallback (``GuiMarshaledConfirm``'s BlockingQueuedConnection) is processed by
this dialog's nested event loop, so confirms stay live while settings are
open. Save validates (hotkey via ``parse_combo``), mutates the passed
``Settings``, persists them to ``product_config_path()``, fires ``on_saved``
and closes. PINNED: changes apply on the NEXT start — no live watcher rebuild.
The autostart checkbox is initialized from the Run key (the scheduler is the
source of truth, M5 Task 3); toggling it writes through to the Run key.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final, Literal, cast

from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from marshmellow.audio.activation import parse_combo
from marshmellow.core import autostart, secrets
from marshmellow.core.config import Settings, product_config_path
from marshmellow.core.contracts import ActivationMode

logger = logging.getLogger(__name__)

Preset = Literal["relaxed", "standard", "strict"]

#: §3.13 strictness ranking. The Global Constraint (M5 plan) allows this dialog
#: to escalate the preset, NEVER lower it — items below the configured preset
#: are disabled in the popup, and `_on_save` refuses them again (belt and
#: braces: `RiskPolicy` only max-escalates per-kind overrides, it never
#: constrains the preset itself, so this guard lives here).
_PRESET_RANK: Final[dict[str, int]] = {"relaxed": 0, "standard": 1, "strict": 2}


class SettingsDialog(QDialog):
    """The modal settings surface. See the module docstring for the contract."""

    def __init__(
        self,
        settings: Settings,
        *,
        apply_autostart: Callable[[bool], bool],
        autostart_state: Callable[[], bool] = autostart.is_autostart_enabled,
        store_key: Callable[[str], None] = secrets.store_api_key,
        on_saved: Callable[[Settings], None] | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._apply_autostart = apply_autostart
        self._autostart_state = autostart_state
        self._store_key = store_key
        self._on_saved = on_saved

        self.setWindowTitle("Marshmellow — Settings")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        # -- activation mode (D2) ----------------------------------------------------
        self.activation_combo = QComboBox(self)
        self.activation_combo.setObjectName("settings-activation")
        for mode in ("wake_word", "hotkey", "both"):
            self.activation_combo.addItem(mode)
        self.activation_combo.setCurrentText(settings.activation)
        form.addRow("Activation", self.activation_combo)

        # -- hotkey, validated live via parse_combo ------------------------------------
        self.hotkey_edit = QLineEdit(settings.hotkey, self)
        self.hotkey_edit.setObjectName("settings-hotkey")
        form.addRow("Hotkey", self.hotkey_edit)

        # -- autostart (D3): the Run key is the source of truth at open time -----------
        self.autostart_check = QCheckBox("Start Marshmellow when I log in", self)
        self.autostart_check.setObjectName("settings-autostart")
        self.autostart_check.setChecked(autostart_state())
        form.addRow("Autostart", self.autostart_check)

        # -- §3.13 preset: persist-only; the Global Constraint (M5 plan) lets the
        #    dialog ESCALATE it, never lower it — items below the persisted preset
        #    are disabled in the popup and Save refuses them again.
        self.safety_combo = QComboBox(self)
        self.safety_combo.setObjectName("settings-safety")
        for preset in ("relaxed", "standard", "strict"):
            self.safety_combo.addItem(preset)
        self.safety_combo.setCurrentText(settings.safety.preset)
        self._preset_floor = _PRESET_RANK[settings.safety.preset]
        self._preset_floor_name = settings.safety.preset
        safety_model = cast(QStandardItemModel, self.safety_combo.model())
        for preset, rank in _PRESET_RANK.items():
            item = safety_model.item(self.safety_combo.findText(preset))
            if item is not None:
                item.setEnabled(rank >= self._preset_floor)
        form.addRow("Safety preset", self.safety_combo)

        # -- API key replace (D7) --------------------------------------------------------
        self.key_edit = QLineEdit(self)
        self.key_edit.setObjectName("settings-key")
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("leave blank to keep the stored key")
        form.addRow("API key", self.key_edit)

        # live validation error for the hotkey (and any other save-time problem)
        self.error_label = QLabel("", self)
        self.error_label.setObjectName("settings-error")
        layout.addWidget(self.error_label)
        self.hotkey_edit.textChanged.connect(self._on_hotkey_changed)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_button = QPushButton("Cancel", self)
        cancel_button.setObjectName("settings-cancel")
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)
        save_button = QPushButton("Save", self)
        save_button.setObjectName("settings-save")
        save_button.setDefault(True)
        save_button.clicked.connect(self._on_save)
        buttons.addWidget(save_button)
        layout.addLayout(buttons)

    # -- save ----------------------------------------------------------------------

    def _on_save(self) -> None:
        if not self._validate_hotkey():
            return  # the live error label explains; nothing is mutated or saved
        self._settings.activation = cast(ActivationMode, self.activation_combo.currentText())
        self._settings.hotkey = self.hotkey_edit.text().strip()
        selected_preset = self.safety_combo.currentText()
        if _PRESET_RANK[selected_preset] < self._preset_floor:
            message = (
                f"the safety preset can escalate, never lower "
                f"(configured: {self._preset_floor_name})"
            )
            self.error_label.setText(message)
            return  # the floor holds: nothing is mutated, saved, or announced
        self._settings.safety.preset = cast(Preset, selected_preset)
        key = self.key_edit.text().strip()
        if key:
            try:
                self._store_key(key)
            except Exception:
                logger.exception("storing the API key failed")
                self.error_label.setText("Could not store the API key — see the logs.")
                return
            self.key_edit.clear()  # never echoed back after storing
        checked = self.autostart_check.isChecked()
        if checked != self._autostart_state():
            # A failure (logged by autostart) leaves the Run key absent — the
            # scheduler is the source of truth, so the setting records False.
            self._settings.autostart = bool(self._apply_autostart(checked))
        else:
            self._settings.autostart = checked  # already in the requested state
        self._settings.save(product_config_path())
        if self._on_saved is not None:
            self._on_saved(self._settings)
        self.accept()

    # -- validation ------------------------------------------------------------------

    def _on_hotkey_changed(self) -> None:
        self._validate_hotkey()

    def _validate_hotkey(self) -> bool:
        try:
            parse_combo(self.hotkey_edit.text().strip())
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return False
        self.error_label.clear()
        return True
