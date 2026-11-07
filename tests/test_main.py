"""`--smoke`: the headless install-verification entry (M5 Task 7, OQ-9).

`Marshmellow.exe --smoke` must start and stop the app headlessly — no Qt
windows, no mic, no onboarding wizard — print "smoke ok" and exit 0. That is
the automated half of the installer's clean-account verification; the human
half is the onboarding checklist in pending-user-verifications.md.
"""

from __future__ import annotations

from typing import Any

from marshmellow.__main__ import main


class _SmokeAppDouble:
    """Records the start/stop lifecycle; never touches Qt or audio."""

    def __init__(self, calls: list[str], settings: Any, headless: bool = False) -> None:
        self.calls = calls
        self.settings = settings
        self.headless = headless
        calls.append(f"init headless={headless}")

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")


def test_smoke_flag(
    tmp_path: Any,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    monkeypatch.setenv("MARSHMELLOW_CONFIG", str(tmp_path / "missing.toml"))
    calls: list[str] = []

    def factory(settings: Any, headless: bool = False) -> _SmokeAppDouble:
        return _SmokeAppDouble(calls, settings, headless)

    rc = main(["--smoke"], app_factory=factory)
    assert rc == 0
    assert calls == ["init headless=True", "start", "stop"], (
        "smoke runs the app headless through one start/stop cycle"
    )
    assert capsys.readouterr().out.strip() == "smoke ok"


def test_smoke_defaults_to_the_real_app() -> None:
    """The script entry point (`marshmellow = marshmellow.__main__:main`) calls
    main() with no arguments: the factory must default to the real App."""
    import inspect

    from marshmellow.app import App

    sig = inspect.signature(main)
    factory = sig.parameters["app_factory"].default
    assert factory is App
