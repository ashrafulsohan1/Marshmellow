"""Planner tool seam — the perceive/act toolbox between the LLM and the screen (§3.5).

``get_elements``/``screenshot`` are PERCEPTION reads: they call ScreenPerception
directly and never produce an ``ActionRequest``. ``act`` is the gated executor
seam: every request it passes on is re-classified by the safety gate — the
planner's declared ``risk`` is advisory only (§3.13).

Screenshot / marked-image files exist only on the vision path while the task is
active (§3.15): :class:`ScreenToolbox` tracks every artifact a snapshot carried
so the planner can purge them at task end through :class:`ArtifactPurger`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    Element,
    PerceptionHint,
    PerceptionSnapshot,
)
from marshmellow.perception.registry import ElementRegistry

logger = logging.getLogger(__name__)

Executor = Callable[[ActionRequest], ActionResult]


@runtime_checkable
class ArtifactPurger(Protocol):
    """The §3.15 purge seam: delete the turn's screenshot artifacts.

    Optional on a ``PlannerTools`` toolbox — the planner purges via
    ``isinstance(toolbox, ArtifactPurger)`` and silently skips toolboxes
    without it (e.g. pure fakes that never create files).
    """

    def purge(self) -> None: ...


class PlannerTools(Protocol):
    """The M3 tool seam handed to ``LLMPlanner`` (``toolbox=``)."""

    def get_elements(self, query: str | None) -> str: ...
    def screenshot(self) -> str: ...
    def act(self, req: ActionRequest) -> ActionResult: ...


class SnapshotSource(Protocol):
    """The ScreenPerception seam (structurally: actions.executor.PerceptionSource)."""

    def snapshot(
        self, hint: PerceptionHint | None = None, hwnd: int | None = None
    ) -> PerceptionSnapshot: ...


def _line(element: Element) -> str:
    """One token-cheap element line, e.g. ``e12 Button 'Play' (920,540 60x24) [invoke]``."""
    actions = ",".join(sorted(element.actions))
    bbox = element.bbox
    text = (
        f"{element.ref} {element.role} '{element.name}' "
        f"({bbox.x},{bbox.y} {bbox.w}x{bbox.h}) [{actions}]"
    )
    if element.value is not None:
        text += f" value={element.value!r}"
    if not element.enabled:
        text += " (disabled)"
    if element.password:  # the value of a password field is never included
        text += " (password)"
    return text


class ScreenToolbox:
    """Real ``PlannerTools`` adapter over perception + registry + gated executor.

    ``execute`` is the composed executor seam (app.py: ask_user/shell routing in
    front of ``gate.guard(action_layer.execute)``), so every ``act`` is gated and
    ref-revalidated. ``registry`` is held for composition symmetry: perception
    registers each walk into it, and the gate/ActionLayer resolve refs through it.
    """

    def __init__(
        self,
        perception: SnapshotSource,
        registry: ElementRegistry,
        execute: Executor,
    ) -> None:
        self._perception = perception
        self._registry = registry
        self._execute = execute
        self._artifacts: list[Path] = []

    def get_elements(self, query: str | None = None) -> str:
        """Fresh snapshot → compact element lines (§3.5: compact payloads)."""
        snapshot = self._perception.snapshot(
            PerceptionHint(query=query) if query else None
        )
        self._remember(snapshot)
        window = snapshot.active_window
        if window is None:
            return "no active window"
        header = (
            f"window: {window.app or 'unknown'} — {window.title} "
            f"({len(snapshot.elements)} elements)"
        )
        return "\n".join([header, *(_line(e) for e in snapshot.elements)])

    def screenshot(self) -> str:
        """The current marked-image path; "" when none (the planner-hinted vision pass).

        ``prefer_vision`` is the D5 "planner hints it" path: a rich tree is not
        re-described, an image is annotated instead. Without a vision perceiver
        the snapshot degrades to UIA-only and "" comes back.
        """
        snapshot = self._perception.snapshot(PerceptionHint(prefer_vision=True))
        self._remember(snapshot)
        return str(snapshot.marked_image) if snapshot.marked_image else ""

    def act(self, req: ActionRequest) -> ActionResult:
        """The gated executor seam — never raises, failures are observations."""
        try:
            result = self._execute(req)
        except Exception as exc:
            logger.exception("executor crashed for act %s", req.kind)
            return ActionResult(
                ok=False, observation="the action executor crashed", error=repr(exc)
            )
        self._remember(result.snapshot_after)
        return result

    def purge(self) -> None:
        """Delete every screenshot/marked-image file this turn produced (§3.15)."""
        for path in self._artifacts:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("could not purge screenshot artifact %s", path)
        self._artifacts.clear()

    def _remember(self, snapshot: PerceptionSnapshot | None) -> None:
        """Track the turn's artifact files so `purge()` can delete them."""
        if snapshot is None:
            return
        for path in (snapshot.screenshot, snapshot.marked_image):
            if path is not None:
                self._artifacts.append(path)
