"""Unit tests use injected seams (no real windows); the manual test opens Notepad."""

import sys
from pathlib import Path

import pytest

from marshmellow.packs.base import ShellResult
from marshmellow.packs.windows import (
    WindowsPack,
    close_window,
    top_level_windows,
    wait_new_window,
)


def _pack(launched: list[list[str]] | None = None, files: list[str] | None = None,
          which: dict[str, str] | None = None) -> WindowsPack:
    # NB: `x or []` would swap an EMPTY recording list for a throwaway one
    # (empty containers are falsy) — bind explicitly instead.
    launched = launched if launched is not None else []
    files = files if files is not None else []
    which = which if which is not None else {}
    return WindowsPack(
        launcher=lambda argv: launched.append(argv),
        startfile=lambda target: files.append(target),
        which=lambda name: which.get(name),
    )


def test_detect_de() -> None:
    from marshmellow.packs.base import detect_de
    assert detect_de() == "windows"


def test_resolve_notepad_alias() -> None:
    pack = _pack(which={"notepad.exe": "C:/Windows/System32/notepad.exe"})
    assert pack.resolve("notepad").argv == ["notepad.exe"]


def test_resolve_case_insensitive_and_strips() -> None:
    pack = _pack(which={"notepad.exe": "x"})
    assert pack.resolve("  NOTEPAD ").argv == ["notepad.exe"]


def test_terminal_picks_first_available() -> None:
    pack = _pack(which={"cmd.exe": "x"})  # wt.exe absent
    assert pack.resolve("terminal").argv == ["cmd.exe"]
    pack2 = _pack(which={"wt.exe": "x", "cmd.exe": "x"})
    assert pack2.resolve("terminal").argv == ["wt.exe"]


def test_settings_is_a_startfile_uri() -> None:
    assert _pack().resolve("settings").uri == "ms-settings:"


def test_unknown_target_resolves_to_none() -> None:
    assert _pack().resolve("zappt") is None


def test_open_app_unknown_fails_cleanly() -> None:
    result = _pack().open_app("zappt")
    assert isinstance(result, ShellResult)
    assert not result.ok
    assert "zappt" in result.detail


def test_open_app_launches_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[list[str]] = []
    pack = _pack(launched=launched, which={"notepad.exe": "x"})
    result = pack.open_app("notepad")
    assert result.ok
    assert launched == [["notepad.exe"]]


def test_open_app_uri_uses_startfile(monkeypatch: pytest.MonkeyPatch) -> None:
    files: list[str] = []
    pack = _pack(files=files)
    result = pack.open_app("settings")
    assert result.ok
    assert files == ["ms-settings:"]


@pytest.mark.manual
@pytest.mark.skipif(sys.platform != "win32", reason="real Windows apps")
def test_open_and_close_notepad_real() -> None:
    pack = WindowsPack()
    before = set(top_level_windows())
    result = pack.open_app("notepad")
    assert result.ok
    hwnd = wait_new_window(before, "notepad", timeout=15.0)
    assert hwnd, "no new Notepad window appeared"
    assert close_window(hwnd)


def test_ctypes_helpers_smoke_offscreen(tmp_path: Path) -> None:
    """Enumeration must work and not raise even with zero matches."""
    windows_now = top_level_windows()
    assert isinstance(windows_now, dict)
    assert wait_new_window(set(windows_now), "__definitely_not_a_window__",
                           timeout=0.5) is None
