"""Tests for core.autostart — Task Scheduler on-logon toggle (D3, closes OQ-6)."""

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from marshmellow.core import autostart

LAUNCH_SPACES = '"C:\\Program Files\\Marshmellow\\Marshmellow.exe" --tray'


def fake_runner(
    returncode: int, calls: list[list[str]] | None = None
) -> autostart.Runner:
    """A Runner that records argv and returns a fixed returncode."""

    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode)

    return run


def test_build_create_command_exact_argv() -> None:
    argv = autostart.build_create_command(LAUNCH_SPACES)
    assert argv == [
        "schtasks",
        "/Create",
        "/F",
        "/TN",
        "Marshmellow",
        "/SC",
        "ONLOGON",
        "/RL",
        "LIMITED",
        "/TR",
        # /TR value pinned verbatim: the launch wrapped in ONE outer pair of
        # double quotes while the executable path stays quoted inside — schtasks'
        # standard form. Both layers are needed because CreateProcess re-parses
        # the command line; subprocess.list2cmdline escapes each embedded quote
        # so they reach the task action intact.
        '""C:\\Program Files\\Marshmellow\\Marshmellow.exe" --tray"',
    ]


def test_enable_uses_launch_command(monkeypatch: pytest.MonkeyPatch) -> None:
    default_launch = '"C:\\repo\\.venv\\Scripts\\pythonw.exe" -m marshmellow'
    monkeypatch.setattr(autostart, "autostart_launch_command", lambda: default_launch)

    calls: list[list[str]] = []
    assert autostart.enable_autostart(runner=fake_runner(0, calls)) is True
    assert calls == [autostart.build_create_command(default_launch)]

    calls.clear()
    assert autostart.enable_autostart(launch=LAUNCH_SPACES, runner=fake_runner(0, calls)) is True
    assert calls == [autostart.build_create_command(LAUNCH_SPACES)]

    assert autostart.enable_autostart(runner=fake_runner(1)) is False  # rc != 0 -> False


def test_is_enabled_true_false_and_error() -> None:
    calls: list[list[str]] = []
    assert autostart.is_autostart_enabled(runner=fake_runner(0, calls)) is True
    assert calls == [["schtasks", "/Query", "/TN", "Marshmellow"]]

    assert autostart.is_autostart_enabled(runner=fake_runner(1)) is False

    def boom(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise OSError("schtasks vanished")

    assert autostart.is_autostart_enabled(runner=boom) is False  # no raise


def test_disable() -> None:
    calls: list[list[str]] = []
    assert autostart.disable_autostart(runner=fake_runner(0, calls)) is True
    assert calls == [["schtasks", "/Delete", "/TN", "Marshmellow", "/F"]]

    assert autostart.disable_autostart(runner=fake_runner(1)) is False


def test_launch_command_frozen_vs_dev(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:\\dist\\Marshmellow.exe")
    assert autostart.autostart_launch_command() == '"C:\\dist\\Marshmellow.exe"'

    monkeypatch.delattr(sys, "frozen", raising=False)
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setattr(autostart, "REPO_ROOT", tmp_path)

    pythonw = scripts / "pythonw.exe"
    pythonw.write_bytes(b"")
    assert autostart.autostart_launch_command() == f'"{pythonw}" -m marshmellow'

    pythonw.unlink()
    python_exe = scripts / "python.exe"
    python_exe.write_bytes(b"")
    assert autostart.autostart_launch_command() == f'"{python_exe}" -m marshmellow'


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="exercises the non-win32 guard — cannot run on a real Windows host",
)
@pytest.mark.parametrize(
    "call",
    [
        autostart.is_autostart_enabled,
        autostart.enable_autostart,
        autostart.disable_autostart,
    ],
)
def test_non_win32_returns_false(call: Callable[[], bool]) -> None:
    assert call() is False
