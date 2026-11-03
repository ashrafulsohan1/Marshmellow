"""DE-pack interface — OS-level operations keyed to the desktop environment.

A new desktop environment means a new pack module implementing DEShell
(01-architecture §3.8); nothing above this layer may call the OS directly.
"""

from __future__ import annotations

import sys
from typing import Protocol

from pydantic import BaseModel


class ShellResult(BaseModel):
    ok: bool
    detail: str = ""
    hwnd: int | None = None


class DEShell(Protocol):
    def open_app(self, target: str) -> ShellResult: ...


def detect_de() -> str:
    if sys.platform == "win32":
        return "windows"
    raise NotImplementedError(
        f"no DE pack for {sys.platform!r} yet — GNOME/KDE/macOS packs are future work"
    )
