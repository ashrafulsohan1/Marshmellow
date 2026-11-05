"""ActionLayer — executes ActionRequests with ref re-validation (01 §3.7).

Flow per `execute()`: (1) resolve `target_ref` in the registry (unknown ->
planner-visible refusal); (2) revalidate the element against a fresh
perception walk — runtime-id match first, so a MOVED window still resolves at
its new bbox; a gone element refuses with a re-snapshot observation, NEVER a
coordinate fallback on a stale bbox (M3 Review Focus 1); (3) elevated
(IsElevationRequired) targets are refused — a spoken limitation, never
force-elevated; (4) dispatch: UIA patterns on the live node first, then
keyboard-equivalent (focus + type/hotkey), then synthesized mouse at the
REVALIDATED bbox center; hotkey/scroll re-focus the element's window first;
(5) post-state: a fresh snapshot WITHOUT screenshot artifacts plus an
observation naming the acted element and the new foreground.

`execute()` never raises — every failure is an ActionResult(ok=False) the
planner can observe and react to. All blocking work (UIA walks, pattern calls,
SendInput) happens in this synchronous call: it must run on the planner worker
thread only, never on the planner's asyncio loop or the Qt thread (Review
Focus 5).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Final, Protocol

from marshmellow.actions.input_synthesis import InputSynthesizer
from marshmellow.actions.uia_actions import (
    UiaPatternInvoker,
    live_control,
    uia_elevation_required,
)
from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    Element,
    PerceptionHint,
    PerceptionSnapshot,
    Rect,
)
from marshmellow.perception.registry import ElementRegistry
from marshmellow.perception.uia_reader import UiaNode

logger = logging.getLogger(__name__)

ElevationCheck = Callable[[UiaNode], bool]
FocusWindowFn = Callable[[int], bool]
SleepFn = Callable[[float], None]

# Kinds the action layer deliberately does not execute: shell/open_app and
# ask_user are routed at composition (app.py), done/fail are planner-internal,
# focus_window is a DEShell window operation (§3.8).
_NON_ELEMENT_KINDS: Final[frozenset[str]] = frozenset(
    {"shell", "focus_window", "ask_user", "done", "fail"}
)

_CLICK_VERBS: Final[dict[str, str]] = {
    "click": "clicked",
    "double_click": "double-clicked",
    "right_click": "right-clicked",
}


class PerceptionSource(Protocol):
    """The snapshot seam (ScreenPerception satisfies it)."""

    def snapshot(self, hint: PerceptionHint | None = None) -> PerceptionSnapshot: ...


def _center(bbox: Rect) -> tuple[int, int]:
    return (bbox.x + bbox.w // 2, bbox.y + bbox.h // 2)


def _payload_str(req: ActionRequest, key: str) -> str | None:
    value = req.payload.get(key)
    return value if isinstance(value, str) else None


class ActionLayer:
    """Executes one ActionRequest against the revalidated element (§3.7)."""

    def __init__(
        self,
        perception: PerceptionSource,
        registry: ElementRegistry,
        patterns: UiaPatternInvoker,
        inputs: InputSynthesizer,
        focus_window: FocusWindowFn | None = None,
        elevated: ElevationCheck | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        self._perception = perception
        self._registry = registry
        self._patterns = patterns
        self._inputs = inputs
        self._focus_window = focus_window
        self._elevated: ElevationCheck = (
            elevated if elevated is not None else uia_elevation_required
        )
        self._sleep: SleepFn = sleep if sleep is not None else time.sleep

    def execute(
        self, req: ActionRequest, snapshot: PerceptionSnapshot | None = None
    ) -> ActionResult:
        """Run one action; returns a result the planner can observe, never raises.

        `snapshot` pins the perception state used for revalidation — pass only a
        snapshot known to be current. The default (None) takes a brand-new
        `perception.snapshot()` walk, which is what makes the window-move and
        focus-change races safe (M3 DoD).
        """
        try:
            return self._execute(req, snapshot)
        except _ActionRefusal as refusal:
            return ActionResult(
                ok=False, observation=refusal.observation, error=refusal.error
            )
        except Exception as exc:
            logger.warning("action %s crashed: %s", req.kind, exc, exc_info=True)
            return ActionResult(
                ok=False,
                observation=f"action {req.kind} crashed: {exc}",
                error="crashed",
            )

    # -- flow -------------------------------------------------------------------

    def _execute(
        self, req: ActionRequest, snapshot: PerceptionSnapshot | None
    ) -> ActionResult:
        if req.kind == "wait":
            return self._wait(req)
        if req.kind in _NON_ELEMENT_KINDS:
            return ActionResult(
                ok=False,
                observation=f"kind {req.kind!r} is handled outside the action layer",
                error="unsupported",
            )

        ref = req.target_ref
        if not ref:
            return ActionResult(
                ok=False, observation="no target ref — re-snapshot", error="missing_ref"
            )
        handle = self._registry.resolve(ref)
        if handle is None:
            return ActionResult(
                ok=False,
                observation=f"unknown ref {ref} — re-snapshot",
                error="unknown_ref",
            )

        # (2) revalidate against a fresh walk. The walk re-registers the current
        # tree (with runtime ids + live nodes); rematch finds the element the
        # ref pointed at — by runtime id first (stable across window moves).
        fresh = snapshot if snapshot is not None else self._perception.snapshot()
        revalidated = self._registry.rematch(handle)
        if revalidated is None:
            return ActionResult(
                ok=False, observation="element gone — re-snapshot", error="stale_ref"
            )
        element = revalidated.element
        node = revalidated.node

        # (3) elevated windows are a spoken limitation, never force-elevated.
        if node is not None and self._elevated(node):
            logger.info("refusing %s on elevated element %r", req.kind, element.name)
            return ActionResult(
                ok=False,
                observation=(
                    f"{element.name!r} needs an elevated (administrator) window —"
                    " I can't act there"
                ),
                error="elevated",
            )

        verb = self._dispatch(req, element, node, fresh)
        return self._post_state(verb, element)

    def _dispatch(
        self,
        req: ActionRequest,
        element: Element,
        node: UiaNode | None,
        fresh: PerceptionSnapshot,
    ) -> str:
        """Steps (4): perform the action, return the observation verb. Raises on
        bugs only — every expected failure path returns via _fail."""
        if req.kind == "uia_invoke":
            self._pattern_step(element, node, self._patterns.invoke)
            return "invoked"
        if req.kind == "uia_toggle":
            self._pattern_step(element, node, self._patterns.toggle)
            return "toggled"
        if req.kind == "uia_select":
            self._pattern_step(element, node, self._patterns.select)
            return "selected"
        if req.kind == "uia_set_value":
            text = _payload_str(req, "text")
            if text is None:
                raise _ActionRefusal(
                    "uia_set_value needs a 'text' payload", "missing_text"
                )
            self._pattern_step(element, node, lambda n: self._patterns.set_value(n, text))
            return "set value of"
        if req.kind == "type_text":
            return self._type_text(req, element, node)
        if req.kind in _CLICK_VERBS:
            point = _center(element.bbox)
            self._inputs.click(
                point,
                right=req.kind == "right_click",
                double=req.kind == "double_click",
            )
            return _CLICK_VERBS[req.kind]
        if req.kind == "hotkey":
            combo = _payload_str(req, "combo")
            if combo is None:
                raise _ActionRefusal(
                    "hotkey needs a 'combo' payload like 'ctrl+s'", "missing_combo"
                )
            self._refocus_window(fresh)
            self._inputs.hotkey(combo)
            return f"sent hotkey '{combo}' to"
        if req.kind == "scroll":
            lines = req.payload.get("lines", 3)
            count = lines if isinstance(lines, int) and not isinstance(lines, bool) else 3
            self._refocus_window(fresh)
            self._inputs.scroll(_center(element.bbox), count)
            return f"scrolled {count} lines on"
        raise _ActionRefusal(f"kind {req.kind!r} is not an element action", "unsupported")

    def _type_text(
        self, req: ActionRequest, element: Element, node: UiaNode | None
    ) -> str:
        text = _payload_str(req, "text")
        if text is None:
            raise _ActionRefusal("type_text needs a 'text' payload", "missing_text")
        # UIA Value pattern first (§3.7); keyboard-equivalent focus + type second.
        if node is not None and "Value" in node.patterns:
            self._pattern_step(element, node, lambda n: self._patterns.set_value(n, text))
            return "set value of"
        if not self._focus_element(node, element):
            raise _ActionRefusal(
                f"could not focus {element.name!r} to type into it", "focus_failed"
            )
        self._inputs.type_text(text)
        return "typed into"

    def _pattern_step(
        self,
        element: Element,
        node: UiaNode | None,
        call: Callable[[UiaNode], bool],
    ) -> None:
        """One UIA pattern call on the live node; refusal when unavailable/failed."""
        if node is None:
            raise _ActionRefusal(
                f"{element.name!r} has no live UIA node (vision ref) — re-snapshot",
                "no_uia_node",
            )
        if not call(node):
            raise _ActionRefusal(
                f"UIA pattern on {element.name!r} failed — re-snapshot", "uia_failed"
            )

    def _focus_element(self, node: UiaNode | None, element: Element) -> bool:
        """Focus the element before keyboard input: UIA SetFocus on the live
        control, falling back to a synthesized click at the element center."""
        control = live_control(node) if node is not None else None
        if control is not None:
            try:
                if bool(control.SetFocus()):
                    return True
            except Exception:
                logger.warning(
                    "SetFocus failed on %s; falling back to click", element.ref,
                    exc_info=True,
                )
        self._inputs.click(_center(element.bbox))
        return True

    def _refocus_window(self, snapshot: PerceptionSnapshot) -> None:
        """hotkey/scroll re-focus the element's window first (§3.7). The fresh
        snapshot's active window is the window the element was walked from (D6)."""
        window = snapshot.active_window
        if self._focus_window is None or window is None:
            return
        if not self._focus_window(window.hwnd):
            logger.warning("focus_window(%#x) failed before action", window.hwnd)

    def _wait(self, req: ActionRequest) -> ActionResult:
        raw = req.payload.get("ms", 500)
        ms = raw if isinstance(raw, (int, float)) else 500
        self._sleep(ms / 1000.0)
        return ActionResult(ok=True, observation=f"waited {ms} ms")

    def _post_state(self, verb: str, element: Element) -> ActionResult:
        """Step (5): fresh snapshot WITHOUT screenshot artifacts + observation."""
        after = self._perception.snapshot().model_copy(
            update={"screenshot": None, "marked_image": None}
        )
        window = after.active_window
        title = window.title if window is not None else "unknown"
        return ActionResult(
            ok=True,
            observation=f"{verb} {element.role} {element.name!r} — foreground: {title}",
            snapshot_after=after,
        )


class _ActionRefusal(Exception):
    """Internal: an expected refusal, converted to ActionResult(ok=False)."""

    def __init__(self, observation: str, error: str) -> None:
        super().__init__(observation)
        self.observation = observation
        self.error = error
