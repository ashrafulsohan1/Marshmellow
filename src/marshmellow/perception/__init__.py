"""Screen perception: UIA tree reading, element registry, vision fallback (§3.6).

PerceptionError is the package-level typed failure (invalid hwnd, missing UIA
stack, capture failures) so callers catch one error class across submodules.
"""

from __future__ import annotations

__all__ = ["PerceptionError"]


class PerceptionError(RuntimeError):
    """A perception operation failed (e.g. invalid hwnd, UIA stack unavailable)."""
