r"""Autostart via the HKCU Run key (D3) — closes OQ-6.

Mechanism note (M5-T3 review fix): the plan's original choice — Task Scheduler
with an on-logon trigger — was rejected by measurement. On a non-elevated
shell ``schtasks /Create /SC ONLOGON`` fails with "ERROR: Access is denied."
(with and without ``/RL LIMITED``) while the same shell creates a DAILY task
successfully, so ONLOGON is admin-only and every standard user's toggle would
always fail — contradicting the OQ-6 decision basis "robust, toggleable, no
UAC". Implemented instead: an HKCU ``Software\Microsoft\Windows\CurrentVersion
\Run`` value (OQ-6 option c), which Explorer launches at user logon with no
elevation, managed through ``reg.exe``.

The stored Run value is the single source of truth for the autostart setting:
nothing here runs at app startup, so a value deleted externally stays deleted
(M5 plan, Task 3 pinned policy). Toggles are applied only from the settings
surfaces. Every function returns ``False`` with a logged warning on any
non-zero returncode, spawn failure, or non-win32 platform — it never raises
into the caller.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]

# Run-key value name (plan interface name kept; not a scheduled task anymore).
AUTOSTART_TASK = "Marshmellow"
RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _platform_ok() -> bool:
    if sys.platform != "win32":
        logger.warning("autostart: not supported on %s — ignoring request", sys.platform)
        return False
    return True


def _apply(runner: Runner, argv: Sequence[str], action: str) -> bool:
    """Run one reg.exe argv; False + warning on spawn failure or non-zero rc."""
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
    """reg add argv that stores ``launch`` as the autostart Run value.

    The data element is ``launch`` verbatim — executable path quoted inside,
    with NO extra outer quote pair. reg.exe stores the argv element as-is
    (measured: reg query echoes the inner-quoted spaced path back unchanged)
    and Explorer executes the value through CreateProcess at logon, so the
    inner quotes are what make a spaced path survive. Wrapping the data in an
    outer pair would store literal stray quotes into the value and break the
    launch — the same failure measured on the Task Scheduler /TR double-wrap.
    The exact element is pinned verbatim in
    ``test_build_create_command_exact_argv``.
    """
    return [
        "reg",
        "add",
        RUN_KEY,
        "/v",
        AUTOSTART_TASK,
        "/t",
        "REG_SZ",
        "/d",
        launch,
        "/f",
    ]


def autostart_launch_command() -> str:
    """The command stored as the Run value: one launch string.

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
    """True iff the Marshmellow Run value exists (reg query rc 0)."""
    if not _platform_ok():
        return False
    return _apply(
        runner,
        ["reg", "query", RUN_KEY, "/v", AUTOSTART_TASK],
        "query autostart value",
    )


def enable_autostart(launch: str | None = None, runner: Runner = _run) -> bool:
    """Create/overwrite the autostart Run value. False (logged) on failure."""
    if not _platform_ok():
        return False
    argv = build_create_command(launch if launch is not None else autostart_launch_command())
    return _apply(runner, argv, "enable autostart")


def disable_autostart(runner: Runner = _run) -> bool:
    """Delete the autostart Run value. False (logged) on any failure."""
    if not _platform_ok():
        return False
    return _apply(
        runner,
        ["reg", "delete", RUN_KEY, "/v", AUTOSTART_TASK, "/f"],
        "disable autostart",
    )
