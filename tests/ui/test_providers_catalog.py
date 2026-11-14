"""Provider catalog + settings provider section (D7 — users never edit files).

Catalog tests are pure; dialog tests run offscreen with injected fakes (key
store/load, probe) — never the real Credential Manager or network.
"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("PySide6")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from marshmellow.core.config import Settings  # noqa: E402
from marshmellow.ui.providers_catalog import (  # noqa: E402
    NOT_CONFIGURED_LABEL,
    PROVIDERS,
    ProbeResult,
    probe_provider,
    provider_for,
)
from marshmellow.ui.settings_dialog import SettingsDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


# -- catalog (pure) -------------------------------------------------------------------


def test_catalog_ids_unique_and_kinds_valid() -> None:
    ids = [p.id for p in PROVIDERS]
    assert len(ids) == len(set(ids))
    for p in PROVIDERS:
        assert p.kind in ("openai_compat", "ollama")
        # custom is type-your-own-endpoint by definition — empty lists are its shape
        assert p.base_urls or p.id == "custom", f"{p.id} lists no base URLs"


def test_catalog_covers_the_named_providers() -> None:
    """The user named ZAI, Codex, and Claude — all must be selectable."""
    ids = {p.id for p in PROVIDERS}
    assert {"zai", "codex", "anthropic", "openai", "ollama", "custom"} <= ids


def test_provider_for_matches_stored_config() -> None:
    assert provider_for("fake", "").result is None if False else provider_for("fake", "") is None
    zai = provider_for("openai_compat", "https://api.z.ai/api/paas/v4")
    assert zai is not None and zai.id == "zai"
    # kind alone is ambiguous for openai_compat → None
    assert provider_for("openai_compat", "") is None
    # unique-kind providers match without a base URL
    assert provider_for("ollama", "").id == "ollama"


def test_probe_provider_reports_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key", request=request)

    result = probe_provider(
        "https://api.example.com/v1", "m", "k",
        transport=httpx.MockTransport(handler),
    )
    assert not result.ok
    assert "401" in result.detail


def test_probe_provider_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer k1"
        return httpx.Response(200, json={"choices": []}, request=request)

    result = probe_provider(
        "https://api.example.com/v1/", "m", "k1",
        transport=httpx.MockTransport(handler),
    )
    assert result.ok
    assert result.detail.startswith("connection ok")


# -- dialog cascade + save ------------------------------------------------------------


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, account: str, password: str) -> None:
        self.store[(service, account)] = password

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))


def _make_dialog(qapp: QApplication, settings: Settings | None = None):
    keyring = FakeKeyring()
    dialog = SettingsDialog(
        settings or Settings(),
        apply_autostart=lambda checked: True,
        autostart_state=lambda: False,
        # the dialog's seams are single-arg; the fake keyring speaks the
        # 3-arg keyring API — adapt here, once
        store_key=lambda key: keyring.set_password("Marshmellow", "llm_api_key", key),
        load_key=lambda: keyring.get_password("Marshmellow", "llm_api_key"),
        probe_fn=lambda base_url, model, key: ProbeResult(ok=True, detail=f"probed {base_url}"),
    )
    return dialog, keyring


def test_provider_combo_starts_not_configured(qapp: QApplication) -> None:
    dialog, _ = _make_dialog(qapp)
    assert dialog.provider_combo.currentData() == ""
    assert dialog.provider_combo.currentText() == NOT_CONFIGURED_LABEL


def test_cascading_base_urls_follow_provider(qapp: QApplication) -> None:
    dialog, _ = _make_dialog(qapp)
    dialog.provider_combo.setCurrentText("Z.AI / GLM")
    urls = [dialog.base_url_combo.itemText(i) for i in range(dialog.base_url_combo.count())]
    assert "https://api.z.ai/api/paas/v4" in urls
    assert dialog.base_url_combo.currentText() == "https://api.z.ai/api/paas/v4"
    assert dialog.model_combo.currentText() == "glm-4.6"
    assert "vision-capable" in dialog.provider_note.text().lower()


def test_ollama_provider_does_not_require_key(qapp: QApplication) -> None:
    dialog, _ = _make_dialog(qapp)
    dialog.provider_combo.setCurrentText("Ollama (local, no key)")
    assert "no api key needed" in dialog.key_edit.placeholderText().lower()


def test_save_writes_provider_config(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(
        "marshmellow.ui.settings_dialog.product_config_path", lambda: tmp_path / "config.toml"
    )
    saved: list[Settings] = []
    dialog, keyring = _make_dialog(qapp)
    dialog._on_saved = lambda s: saved.append(s)
    dialog.provider_combo.setCurrentText("Z.AI / GLM")
    dialog.key_edit.setText("sk-test-123")
    dialog._on_save()
    assert dialog._settings.providers.llm.kind == "openai_compat"
    assert dialog._settings.providers.llm.base_url == "https://api.z.ai/api/paas/v4"
    assert dialog._settings.providers.llm.model == "glm-4.6"
    assert keyring.get_password("Marshmellow", "llm_api_key") == "sk-test-123"
    assert saved


def test_save_requires_key_for_key_providers(qapp: QApplication) -> None:
    dialog, _ = _make_dialog(qapp)
    dialog.provider_combo.setCurrentText("OpenAI")
    dialog.key_edit.setText("")  # nothing stored either
    dialog._on_save()
    assert "api key" in dialog.error_label.text().lower()
    assert dialog._settings.providers.llm.kind == "fake"  # untouched


def test_stored_config_preselects_dropdowns(qapp: QApplication) -> None:
    settings = Settings()
    settings.providers.llm.kind = "openai_compat"
    settings.providers.llm.base_url = "https://api.z.ai/api/paas/v4"
    settings.providers.llm.model = "glm-4.6"
    dialog, _ = _make_dialog(qapp, settings)
    assert dialog.provider_combo.currentData() == "zai"
    assert dialog.model_combo.currentText() == "glm-4.6"


def test_test_connection_reports_probe_result(qapp: QApplication) -> None:
    dialog, _ = _make_dialog(qapp)
    dialog.provider_combo.setCurrentText("Z.AI / GLM")
    dialog._on_test_connection()  # injected probe_fn returns instantly
    # the probe thread emits a queued signal — pump the event loop until it lands
    for _ in range(50):
        QApplication.processEvents()
        if "probed" in dialog.probe_label.text():
            break
        QTest.qWait(20)
    assert "probed" in dialog.probe_label.text()
    assert dialog.test_button.isEnabled()
