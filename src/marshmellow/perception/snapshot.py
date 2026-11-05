"""ScreenPerception — the D5 hybrid snapshot (01-architecture §3.6).

UIA-first: the foreground window's UIA tree is walked, registered, and returned.
The SoM vision fallback runs ONLY when the tree is sparse (< FALLBACK_THRESHOLD
interactable elements, SP1) or the hint forces it; vision elements whose bbox
overlaps a UIA element (IoU > IOU_DEDUPE_THRESHOLD) are dropped — UIA always
wins. Vision failures degrade to a UIA-only snapshot, never an error.
Screenshot / marked-image artifact files are written ONLY on the vision path —
active-task-only, purged at task end by the planner (§3.15).
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Final

from marshmellow.core.contracts import (
    Element,
    PerceptionHint,
    PerceptionSnapshot,
    WindowInfo,
)
from marshmellow.packs.windows import window_title
from marshmellow.perception import PerceptionError
from marshmellow.perception.registry import ElementRegistry, bbox_iou
from marshmellow.perception.uia_reader import UIAReader
from marshmellow.perception.vision import VisionPerceiver

logger = logging.getLogger(__name__)

FALLBACK_THRESHOLD: Final = 10  # SP1: fewer interactable elements than this -> vision
IOU_DEDUPE_THRESHOLD: Final[float] = 0.5


class ScreenPerception:
    """Builds PerceptionSnapshots: UIA walk + registry, SoM vision merged in."""

    def __init__(
        self,
        reader: UIAReader,
        registry: ElementRegistry,
        foreground: Callable[[], WindowInfo | None],
        vision: VisionPerceiver | None = None,
    ) -> None:
        self._reader = reader
        self._registry = registry
        self._foreground = foreground
        self._vision = vision

    def snapshot(
        self, hint: PerceptionHint | None = None, hwnd: int | None = None
    ) -> PerceptionSnapshot:
        """UIA-first snapshot of the foreground window — or of `hwnd` when given.

        `hwnd` is the ActionLayer's revalidation seam (M3 focus-change race): the
        element's OWN window is re-walked, so a focus change cannot divert or
        mis-match the action. Explicit-hwnd walks are UIA-only — no vision
        fallback and no screenshot artifacts (they are identity checks, not
        planner-facing perception). Both paths register the walk's elements with
        the hwnd they came from (window-scoped rematch in the registry).
        """
        window = (
            WindowInfo(hwnd=hwnd, title=window_title(hwnd), app="")
            if hwnd is not None
            else self._foreground()
        )
        if window is None:
            return PerceptionSnapshot(active_window=None, elements=[])
        items = self._reader.read_with_nodes(window.hwnd)
        uia_elements = [item.element for item in items]

        vision_elements: list[Element] = []
        screenshot: Path | None = None
        marked_image: Path | None = None
        if (
            hwnd is None
            and self._vision is not None
            and (
                (hint is not None and hint.prefer_vision)
                or len(uia_elements) < FALLBACK_THRESHOLD
            )
        ):
            candidates, vision_ok = self._run_vision(self._vision, hint)
            if vision_ok:
                vision_elements = [
                    candidate
                    for candidate in candidates
                    if not self._overlaps_uia(candidate, uia_elements)
                ]
                screenshot = _write_png(self._vision.last_screenshot)
                marked_image = _write_png(self._vision.last_marked_image)

        merged = uia_elements + vision_elements
        # One register call per snapshot: UIA entries carry runtime id + live node,
        # vision entries carry runtime_id="" and node=None (bbox-only revalidation).
        self._registry.register(
            merged,
            [item.runtime_id for item in items] + [""] * len(vision_elements),
            [item.node for item in items] + [None] * len(vision_elements),
            hwnd=window.hwnd,
        )
        return PerceptionSnapshot(
            active_window=window,
            elements=merged,
            screenshot=screenshot,
            marked_image=marked_image,
        )

    def _run_vision(
        self,
        vision: VisionPerceiver,
        hint: PerceptionHint | None,
    ) -> tuple[list[Element], bool]:
        """One vision pass; `(elements, ok)` — ok=False on any PerceptionError."""
        try:
            return vision.perceive(hint), True
        except PerceptionError as exc:
            logger.warning("vision fallback failed; keeping UIA-only snapshot: %s", exc)
            return [], False

    @staticmethod
    def _overlaps_uia(candidate: Element, uia_elements: list[Element]) -> bool:
        return any(
            bbox_iou(candidate.bbox, existing.bbox) > IOU_DEDUPE_THRESHOLD
            for existing in uia_elements
        )


def _write_png(data: bytes | None) -> Path | None:
    """Persist one vision-path artifact to the temp dir (purged at task end, §3.15)."""
    if not data:
        return None
    fd, name = tempfile.mkstemp(prefix="marshmellow-shot-", suffix=".png")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return Path(name)
