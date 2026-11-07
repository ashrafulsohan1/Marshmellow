"""Release-audit fixes: S1 (gated shell path) + S2 (tray recovery channel)."""

from pathlib import Path

from marshmellow.app import App
from marshmellow.core.config import Settings
from marshmellow.core.contracts import ActionRequest
from marshmellow.packs.base import ShellResult
from marshmellow.safety.gate import SafetyDecision
from marshmellow.ui.tray import TrayAction


class RecordingPack:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def open_app(self, target: str) -> ShellResult:
        self.calls.append(target)
        return ShellResult(ok=True, detail=f"would open {target}")


class FakeEngine:
    def __init__(self) -> None:
        self.muted: list[bool] = []

    def set_muted(self, muted: bool) -> None:
        self.muted.append(muted)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _app(tmp_path: Path, pack: RecordingPack, engine: FakeEngine) -> App:
    return App(
        settings=Settings(), headless=True, log_dir=tmp_path,
        pack=pack, audio_engine=engine,
    )


def _allow(req: ActionRequest, element: object) -> tuple[SafetyDecision, str]:
    return (SafetyDecision(allowed=True, needs_confirmation=False, reason="test"), "L1")


def test_shell_action_passes_through_the_gate(tmp_path: Path) -> None:
    pack = RecordingPack()
    app = _app(tmp_path, pack, FakeEngine())
    seen: list[ActionRequest] = []

    def spy(req: ActionRequest, element: object) -> tuple[SafetyDecision, str]:
        seen.append(req)
        return _allow(req, element)

    app.gate._decide = spy  # type: ignore[method-assign]
    result = app._execute(
        ActionRequest(kind="shell", payload={"open_app": "notepad"},
                      rationale="r", risk="L1")
    )
    assert result.ok
    assert pack.calls == ["notepad"]
    assert len(seen) == 1 and seen[0].kind == "shell"


def test_shell_action_refused_when_gate_declines(tmp_path: Path) -> None:
    pack = RecordingPack()
    app = _app(tmp_path, pack, FakeEngine())
    app.gate._decide = lambda req, element=None: (
        SafetyDecision(allowed=False, needs_confirmation=False, reason="no"), "L1"
    )
    result = app._execute(
        ActionRequest(kind="shell", payload={"open_app": "notepad"},
                      rationale="r", risk="L1")
    )
    assert not result.ok
    assert result.error == "refused"
    assert pack.calls == []


def test_tray_mute_reaches_engine_and_unmute_recovers(tmp_path: Path) -> None:
    pack = RecordingPack()
    engine = FakeEngine()
    app = _app(tmp_path, pack, engine)
    app._on_tray_action(TrayAction(kind="mute"))
    assert engine.muted == [True]
    app._on_tray_action(TrayAction(kind="unmute"))
    assert engine.muted == [True, False]


def test_tray_pause_cancels_running_tasks(tmp_path: Path) -> None:
    pack = RecordingPack()
    app = _app(tmp_path, pack, FakeEngine())
    cancelled: list[bool] = []
    stopped: list[bool] = []
    app.manager.cancel_all = lambda: cancelled.append(True)  # type: ignore[method-assign]
    app.tts.stop = lambda: stopped.append(True)  # type: ignore[method-assign]
    app._on_tray_action(TrayAction(kind="pause"))
    assert cancelled == [True]
    assert stopped == [True]  # audit S6: pause kills mid-speech TTS too
