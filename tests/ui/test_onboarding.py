"""First-run onboarding tests — offscreen (QT_QPA_PLATFORM per tests/conftest.py).

M5 Task 4 (D2/D3/D7): the wizard is driven on injected fakes (mic probe, wake
availability, key storage, autostart toggle) and the save path is routed to
tmp — never the real Credential Manager, Run key, or %LOCALAPPDATA%.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from marshmellow.core.config import Settings  # noqa: E402
from marshmellow.ui import onboarding as onboarding_mod  # noqa: E402
from marshmellow.ui.onboarding import (  # noqa: E402
    OnboardingResult,
    OnboardingWizard,
    should_onboard,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def product_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Route every wizard save to tmp — never the real %LOCALAPPDATA%."""
    path = tmp_path / "config.toml"
    monkeypatch.setattr(onboarding_mod, "product_config_path", lambda: path)
    return path


_OK_PROBE = lambda: (True, "fake mic is capturing audio")  # noqa: E731


# -- should_onboard -----------------------------------------------------------------


def test_should_onboard_true_when_no_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MARSHMELLOW_SKIP_ONBOARDING", raising=False)
    assert should_onboard(None) is True


def test_should_onboard_false_when_a_path_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MARSHMELLOW_SKIP_ONBOARDING", raising=False)
    assert should_onboard(Path("config.toml")) is False


def test_should_onboard_false_with_skip_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MARSHMELLOW_SKIP_ONBOARDING", "1")
    assert should_onboard(None) is False


# -- mic row (injected probe) --------------------------------------------------------


def test_probe_default_mic_injected(qapp: QApplication) -> None:
    """The injected probe's (ok, detail) tuple renders in the mic row."""
    wizard = OnboardingWizard(
        Settings(), mic_probe=lambda: (False, "no default input device")
    )
    assert "no default input device" in wizard.mic_label.text()
    wizard_ok = OnboardingWizard(
        Settings(), mic_probe=lambda: (True, "USB microphone is capturing audio")
    )
    assert "OK" in wizard_ok.mic_label.text()
    assert "USB microphone is capturing audio" in wizard_ok.mic_label.text()


# -- the wizard's five sections -------------------------------------------------------


def test_wake_options_disabled_when_wake_unavailable(qapp: QApplication) -> None:
    wizard = OnboardingWizard(
        Settings(), mic_probe=_OK_PROBE, wake_available=lambda: False
    )
    assert "not set up" in wizard.wake_label.text()
    assert not wizard.wake_radio.isEnabled()
    assert not wizard.both_radio.isEnabled()
    assert wizard.hotkey_radio.isEnabled()  # the trusted channel stays usable
    assert wizard.hotkey_radio.isChecked()  # and is the fallback selection


def test_wake_options_enabled_and_selected_when_available(
    qapp: QApplication,
) -> None:
    wizard = OnboardingWizard(
        Settings(activation="wake_word"),
        mic_probe=_OK_PROBE,
        wake_available=lambda: True,
    )
    assert "ready" in wizard.wake_label.text()
    assert wizard.wake_radio.isEnabled()
    assert wizard.both_radio.isEnabled()
    assert wizard.wake_radio.isChecked()  # the configured mode is preselected


def test_finish_stores_key_once_and_completes(
    qapp: QApplication, product_config: Path
) -> None:
    stored: list[str] = []
    settings = Settings()
    wizard = OnboardingWizard(settings, mic_probe=_OK_PROBE, store_key=stored.append)
    wizard.key_edit.setText("sk-test-123")
    result = _click_later(wizard, "onboarding-finish")
    assert stored == ["sk-test-123"]  # called once, with the entered text
    assert result == OnboardingResult(
        activation="hotkey",  # wake unavailable default → hotkey radio checked
        autostart_enabled=False,
        key_stored=True,
        completed=True,
    )
    assert settings.activation == "hotkey"  # mutated in place
    assert wizard.key_edit.text() == ""  # never echoed back after storing
    assert product_config.exists()
    assert "sk-test-123" not in product_config.read_text(encoding="utf-8")  # D7


def test_activation_radio_selection_lands_in_result(
    qapp: QApplication, product_config: Path
) -> None:
    settings = Settings(activation="both")
    wizard = OnboardingWizard(
        settings, mic_probe=_OK_PROBE, wake_available=lambda: True
    )
    wizard.hotkey_radio.setChecked(True)
    result = _click_later(wizard, "onboarding-finish")
    assert result.activation == "hotkey"
    assert settings.activation == "hotkey"


def test_autostart_checkbox_calls_apply_autostart(
    qapp: QApplication, product_config: Path
) -> None:
    applied: list[bool] = []
    settings = Settings()

    def fake_apply(enabled: bool) -> bool:
        applied.append(enabled)
        return enabled

    wizard = OnboardingWizard(
        settings, mic_probe=_OK_PROBE, apply_autostart=fake_apply
    )
    wizard.autostart_check.setChecked(True)
    result = _click_later(wizard, "onboarding-finish")
    assert applied == [True]
    assert result.autostart_enabled is True
    assert settings.autostart is True
    assert product_config.exists()


def test_cancel_returns_not_completed_and_touches_nothing(
    qapp: QApplication, product_config: Path
) -> None:
    stored: list[str] = []
    settings = Settings(activation="both")
    wizard = OnboardingWizard(settings, mic_probe=_OK_PROBE, store_key=stored.append)
    wizard.autostart_check.setChecked(True)
    result = _click_later(wizard, "onboarding-cancel")
    assert result.completed is False
    assert result.key_stored is False
    assert stored == []
    assert settings.activation == "both"  # untouched
    assert settings.autostart is False
    assert not product_config.exists()  # nothing persisted on cancel


# -- helpers --------------------------------------------------------------------------


def _click_later(wizard: OnboardingWizard, button_name: str) -> OnboardingResult:
    """Click `button_name` from inside the modal exec loop; return run()'s result."""
    button = wizard.findChild(QPushButton, button_name)
    assert button is not None, f"missing button {button_name}"
    QTimer.singleShot(
        0, lambda: QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    )
    return wizard.run()
