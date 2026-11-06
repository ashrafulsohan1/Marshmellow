"""First-run onboarding (M5 Task 4, D2/D3/D7) — one page, five sections.

A five-item checklist is not a wizard flow, so this is a single QDialog:
mic check (probe result text) · wake word (ready / train-later note) ·
activation radios (D2) · the API key (D7 — password echo, stored to Windows
Credential Manager, never echoed back, never written to the TOML) · the
autostart checkbox (D3). Every environment seam is constructor-injected (mic
probe, wake availability, key storage, autostart toggle) so offscreen tests
drive the real dialog on fakes.

``__main__.main()`` runs the wizard after the QApplication exists and before
``App(...)`` when ``should_onboard`` says no config was found; headless
entries never import it. Finish mutates the passed ``Settings`` in place and
persists them to ``product_config_path()``; Cancel (and any other close)
touches nothing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from marshmellow.core import autostart, secrets
from marshmellow.core.assets import default_wake_model_path
from marshmellow.core.config import Settings, product_config_path
from marshmellow.core.contracts import ActivationMode

logger = logging.getLogger(__name__)

#: Windows mic-permission-blocked streams enumerate but sample digital
#: silence — that is what this probe catches. Only effectively-zero RMS flags
#: it (a quiet room is not a failure); anything above counts as working.
_SILENCE_RMS: Final = 5.0
_PROBE_SECONDS: Final = 1

_WAKE_READY_TEXT = "Wake word: ready"
_WAKE_MISSING_TEXT = (
    "Wake word: not set up — the hotkey will be used; train later with "
    "tools/train_wake_model.py"
)


@dataclass(frozen=True)
class OnboardingResult:
    activation: ActivationMode
    autostart_enabled: bool
    key_stored: bool
    completed: bool  # False = closed/cancelled


def should_onboard(config_path: Path | None) -> bool:
    """Fresh-install check: no config was found AND the skip env var is unset."""
    if os.environ.get("MARSHMELLOW_SKIP_ONBOARDING"):
        return False
    return config_path is None


def probe_default_mic(sample_rate: int = 16000) -> tuple[bool, str]:
    """sounddevice default-input query + a 1 s capture RMS sanity check.

    Returns ``(ok, detail)`` — ``detail`` is rendered verbatim in the
    wizard's mic row. ``(False, detail)`` covers a missing audio backend, no
    default input device, a failing capture, and the Windows
    mic-permission-blocked case (the stream enumerates but samples silence).
    """
    try:
        import sounddevice as sd
    except Exception as exc:  # a broken PortAudio must not crash onboarding
        return False, f"audio backend unavailable ({exc})"
    try:
        info = sd.query_devices(kind="input")
        name = (
            str(info["name"])
            if isinstance(info, dict) and "name" in info
            else "default input"
        )
    except Exception as exc:
        return False, f"no default input device found ({exc})"
    try:
        recording = sd.rec(
            int(sample_rate * _PROBE_SECONDS),
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
        )
        sd.wait()
    except Exception as exc:
        return False, f"could not capture from {name} ({exc})"
    rms = float(np.sqrt(np.mean(np.asarray(recording, dtype=np.float64) ** 2)))
    if rms < _SILENCE_RMS:
        return False, f"{name} captured silence (rms {rms:.1f}) — check microphone permission"
    return True, f"{name} is capturing audio"


class OnboardingWizard(QDialog):
    """The one-page first-run dialog. See the module docstring for the contract."""

    def __init__(
        self,
        settings: Settings,
        *,
        mic_probe: Callable[[], tuple[bool, str]] = probe_default_mic,
        wake_available: Callable[[], bool] = lambda: default_wake_model_path() is not None,
        store_key: Callable[[str], None] = secrets.store_api_key,
        apply_autostart: Callable[[bool], bool] = autostart.set_autostart,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._store_key = store_key
        self._apply_autostart = apply_autostart
        self._key_stored = False

        self.setWindowTitle("Welcome to Marshmellow")
        layout = QVBoxLayout(self)

        # -- mic check row: the injected probe runs once, before the app starts --
        mic_ok, mic_detail = mic_probe()
        mic_text = f"Microphone: OK — {mic_detail}" if mic_ok else f"Microphone: {mic_detail}"
        self.mic_label = QLabel(mic_text, self)
        self.mic_label.setObjectName("onboarding-mic")
        layout.addWidget(self.mic_label)

        # -- wake row -------------------------------------------------------------
        wake_ok = wake_available()
        self.wake_label = QLabel(
            _WAKE_READY_TEXT if wake_ok else _WAKE_MISSING_TEXT, self
        )
        self.wake_label.setObjectName("onboarding-wake")
        layout.addWidget(self.wake_label)

        # -- activation radios (D2): wake options off while no wake model exists —
        #    the runtime ActivationDegraded spoken path covers a stale choice, but
        #    a fresh install gets the trusted hotkey channel preselected.
        self.wake_radio = QRadioButton("Wake word — say \u201chey marshmellow\u201d", self)
        self.hotkey_radio = QRadioButton(f"Hotkey — hold {settings.hotkey} and speak", self)
        self.both_radio = QRadioButton("Both — wake word or hotkey", self)
        radios: dict[ActivationMode, QRadioButton] = {
            "wake_word": self.wake_radio,
            "hotkey": self.hotkey_radio,
            "both": self.both_radio,
        }
        for mode, radio in radios.items():
            radio.setObjectName(f"activation-{mode}")
            layout.addWidget(radio)
        current = settings.activation
        if not wake_ok:
            self.wake_radio.setEnabled(False)
            self.both_radio.setEnabled(False)
            current = "hotkey"
        radios[current].setChecked(True)

        # -- API key (D7) ----------------------------------------------------------
        layout.addWidget(
            QLabel("API key (stored in Windows Credential Manager — never in the config):", self)
        )
        self.key_edit = QLineEdit(self)
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("paste your API key (optional)")
        layout.addWidget(self.key_edit)
        self.key_label = QLabel("", self)
        layout.addWidget(self.key_label)
        save_key_button = QPushButton("Save key", self)
        save_key_button.setObjectName("onboarding-save-key")
        save_key_button.clicked.connect(self._save_key)
        layout.addWidget(save_key_button)

        # -- autostart (D3): a fresh install starts unchecked ------------------------
        self.autostart_check = QCheckBox("Start Marshmellow when I log in", self)
        self.autostart_check.setObjectName("onboarding-autostart")
        layout.addWidget(self.autostart_check)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_button = QPushButton("Cancel", self)
        cancel_button.setObjectName("onboarding-cancel")
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)
        finish_button = QPushButton("Finish", self)
        finish_button.setObjectName("onboarding-finish")
        finish_button.setDefault(True)
        finish_button.clicked.connect(self._finish)
        buttons.addWidget(finish_button)
        layout.addLayout(buttons)

    def run(self) -> OnboardingResult:
        """Show the dialog modally; Finish mutates + persists the settings, any
        other close leaves them untouched."""
        if self.exec() != QDialog.DialogCode.Accepted:
            return OnboardingResult(
                activation=self._settings.activation,
                autostart_enabled=self._settings.autostart,
                key_stored=self._key_stored,
                completed=False,
            )
        activation = self._selected_activation()
        # A failure (logged by autostart) leaves the Run key absent — the
        # scheduler is the source of truth (M5 Task 3), so the result is False.
        autostart_enabled = (
            bool(self._apply_autostart(True)) if self.autostart_check.isChecked() else False
        )
        self._settings.activation = activation
        self._settings.autostart = autostart_enabled
        self._settings.save(product_config_path())
        return OnboardingResult(
            activation=activation,
            autostart_enabled=autostart_enabled,
            key_stored=self._key_stored,
            completed=True,
        )

    # -- sections ------------------------------------------------------------------

    def _save_key(self) -> None:
        """D7: the key goes to Windows Credential Manager, then the field is
        wiped — it is never echoed back and never written to the TOML."""
        key = self.key_edit.text().strip()
        if not key:
            return
        try:
            self._store_key(key)
        except Exception:
            logger.exception("storing the API key failed")
            self.key_label.setText("Could not store the API key — see the logs.")
            return
        self._key_stored = True
        self.key_edit.clear()
        self.key_label.setText("API key stored.")

    def _finish(self) -> None:
        self._save_key()  # a key typed but not yet saved is saved by Finish too
        self.accept()

    def _selected_activation(self) -> ActivationMode:
        if self.wake_radio.isChecked():
            return "wake_word"
        if self.hotkey_radio.isChecked():
            return "hotkey"
        return "both"
