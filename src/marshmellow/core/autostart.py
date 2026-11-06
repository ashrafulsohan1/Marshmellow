"""Autostart via Windows Task Scheduler (D3) — closes OQ-6.

The scheduled task ``Marshmellow`` (ONLOGON, LIMITED) is the single source of
truth for the autostart setting: nothing here runs at app startup, so a task
deleted externally stays deleted (M5 plan, Task 3 pinned policy). Toggles are
applied only from the settings surfaces. Every function returns ``False`` with
a logged warning on any non-zero returncode, spawn failure, or non-win32
platform — it never raises into the caller.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]

AUTOSTART_TASK = "Marshmellow"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _platform_ok() -> bool:
    if sys.platform != "win32":
        logger.warning("autostart: not supported on %s — ignoring request", sys.platform)
        return False
    return True


def _apply(runner: Runner, argv: Sequence[str], action: str) -> bool:
    """Run one schtasks argv; False + warning on spawn failure or non-zero rc."""
    try:
        proc = runner(argv)
    except OSError as exc:
        logger.warning("autostart: %s failed: %s", action, exc)
        return False
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        logger.warning("autostart: %s failed (rc=%d): %s", action, proc.returncode, stderr)
        return False
    return True


def build_create_command(launch: str) -> list[str]:
    """schtasks /Create argv for the on-logon task running ``launch``.

    The /TR element wraps the whole launch in ONE outer pair of double quotes
    while the executable path stays quoted inside — schtasks' standard form.
    Both quote layers are needed because CreateProcess re-parses the command
    line: subprocess.list2cmdline escapes each embedded double quote (as a
    backslash-quote pair) when joining the argv, so the inner quotes reach the
    task's action string intact. The exact element is pinned verbatim in
    ``test_build_create_command_exact_argv``.
    """
    return [
        "schtasks",
        "/Create",
        "/F",
        "/TN",
        AUTOSTART_TASK,
        "/SC",
        "ONLOGON",
        "/RL",
        "LIMITED",
        "/TR",
        f'"{launch}"',
    ]


def autostart_launch_command() -> str:
    """The command the scheduled task runs, as one /TR-ready string.

    Frozen: the quoted ``sys.executable``. Dev checkout: the venv's
    ``pythonw.exe`` (no console window at logon) running ``-m marshmellow``,
    falling back to ``python.exe`` if ``pythonw.exe`` is missing.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    scripts = REPO_ROOT / ".venv" / "Scripts"
    exe = scripts / "pythonw.exe"
    if not exe.is_file():
        exe = scripts / "python.exe"
    return f'"{exe}" -m marshmellow'


def is_autostart_enabled(runner: Runner = _run) -> bool:
    """True iff the Marshmellow scheduled task exists (schtasks /Query rc 0)."""
    if not _platform_ok():
        return False
    return _apply(
        runner, ["schtasks", "/Query", "/TN", AUTOSTART_TASK], "query autostart task"
    )


def enable_autostart(launch: str | None = None, runner: Runner = _run) -> bool:
    """Create/overwrite the on-logon task. False (logged) on any failure."""
    if not _platform_ok():
        return False
    argv = build_create_command(launch if launch is not None else autostart_launch_command())
    return _apply(runner, argv, "enable autostart")


def disable_autostart(runner: Runner = _run) -> bool:
    """Delete the on-logon task. False (logged) on any failure."""
    if not _platform_ok():
        return False
    return _apply(
        runner, ["schtasks", "/Delete", "/TN", AUTOSTART_TASK, "/F"], "disable autostart"
    )
