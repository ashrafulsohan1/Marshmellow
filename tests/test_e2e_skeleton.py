"""M1 walking-skeleton E2E: scripted instruction → fakes → DE-pack seam (headless)."""

import pytest

from marshmellow.app import App
from marshmellow.core.config import Settings
from marshmellow.packs.base import ShellResult
from marshmellow.stt.fake import FakeSTT
from marshmellow.tts.fake import FakeTTS


class RecordingPack:
    """Stands in for WindowsPack: records open_app calls, opens nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def open_app(self, target: str) -> ShellResult:
        self.calls.append(target)
        return ShellResult(ok=True, detail=f"would open {target}")


@pytest.mark.scenario
async def test_open_notepad_through_full_loop(tmp_path) -> None:
    pack = RecordingPack()
    app = App(settings=Settings(), headless=True, log_dir=tmp_path, pack=pack)
    app.start()

    handle = await app.submit_text("open notepad")

    assert handle.result is not None and handle.result.ok
    assert pack.calls == ["notepad"]
    spoken = [s.text for s in app.tts.spoken]
    assert any(t.strip().lower() == "on it." for t in spoken), "ack must be spoken"
    assert any("notepad" in t.lower() for t in spoken), "result must be spoken"
    app.stop()


@pytest.mark.scenario
async def test_chat_instruction_never_touches_the_pack(tmp_path) -> None:
    pack = RecordingPack()
    app = App(settings=Settings(), headless=True, log_dir=tmp_path, pack=pack)
    app.start()

    handle = await app.submit_text("tell me a joke")

    assert handle.result is not None and handle.result.ok
    assert pack.calls == []
    assert app.tts.spoken  # still spoken back to the user
    app.stop()


async def test_headless_composition_stays_on_fakes_and_reports_quit(
    tmp_path,
) -> None:
    """The live entry (`python -m marshmellow`) is the only non-headless composition;
    headless runs must never load real models or open devices."""
    app = App(settings=Settings(), headless=True, log_dir=tmp_path)
    assert isinstance(app.stt, FakeSTT)
    assert isinstance(app.tts, FakeTTS)
    assert app.quit_requested is False
    app.stop()
