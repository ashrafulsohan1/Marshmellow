"""ActionLayer tests — fake seams only, no real windows (M3 Task 3 Step 1).

Covers the §3.7 flow: ref resolution → revalidation against a fresh walk →
elevation refusal → UIA-first dispatch → SendInput fallback → post-state
observation — plus Review Focus 5 (blocking walk never stalls the planner's
resident asyncio loop).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from marshmellow.actions.executor import ActionLayer
from marshmellow.actions.input_synthesis import InputSynthesizer, KeyboardEvent, MouseEvent
from marshmellow.core.contracts import (
    ActionRequest,
    Element,
    PerceptionSnapshot,
    Rect,
    WindowInfo,
)
from marshmellow.perception.registry import ElementRegistry
from marshmellow.perception.uia_reader import UiaNode

WINDOW: Final = WindowInfo(hwnd=4321, title="Fake App", app="fake")


@dataclass
class FakeControl:
    """Live-control stand-in: records SetFocus (the real one is uiautomation.Control)."""

    focused: bool = False

    def SetFocus(self) -> bool:
        self.focused = True
        return True


@dataclass
class FakeNode:
    """UiaNode stand-in carrying a live control for patterns/focus."""

    patterns: frozenset[str] = frozenset()
    control: FakeControl | None = None


def _el(
    ref: str,
    role: str = "Button",
    name: str = "OK",
    bbox: tuple[int, int, int, int] = (0, 0, 100, 50),
    actions: frozenset[str] = frozenset({"invoke", "read"}),
) -> Element:
    x, y, w, h = bbox
    return Element(ref=ref, role=role, name=name, bbox=Rect(x=x, y=y, w=w, h=h),
                   actions=set(actions))


State = tuple[list[Element], list[str], list[UiaNode | None]]


@dataclass
class FakePatterns:
    """UiaPatternInvoker stand-in: records calls, returns the scripted verdict."""

    ok: bool = True
    calls: list[tuple[str, UiaNode | None, str]] = field(default_factory=list)

    def invoke(self, node: UiaNode | None) -> bool:
        self.calls.append(("invoke", node, ""))
        return self.ok

    def toggle(self, node: UiaNode | None) -> bool:
        self.calls.append(("toggle", node, ""))
        return self.ok

    def set_value(self, node: UiaNode | None, text: str) -> bool:
        self.calls.append(("set_value", node, text))
        return self.ok

    def select(self, node: UiaNode | None) -> bool:
        self.calls.append(("select", node, ""))
        return self.ok

    def expand(self, node: UiaNode | None, expand: bool) -> bool:
        self.calls.append(("expand", node, str(expand)))
        return self.ok


@dataclass
class RecorderSendInput:
    events: list[KeyboardEvent | MouseEvent] = field(default_factory=list)
    order: list[str] | None = None  # optional shared focus-vs-input ordering log

    def __call__(self, batch: list[KeyboardEvent | MouseEvent]) -> int:
        self.events.extend(batch)
        if self.order is not None:
            self.order.append(f"input:{type(batch[0]).__name__}")
        return len(batch)


@dataclass
class RecordingSleep:
    durations: list[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)


@dataclass
class FocusRecorder:
    """focus_window seam: records hwnds, marks a shared order log."""

    hwnds: list[int] = field(default_factory=list)
    order: list[str] = field(default_factory=list)

    def __call__(self, hwnd: int) -> bool:
        self.hwnds.append(hwnd)
        self.order.append("focus")
        return True


class FakePerception:
    """Mimics ScreenPerception: each snapshot() serves the next scripted state
    and registers it (elements + runtime ids + nodes) into the shared registry."""

    def __init__(
        self,
        registry: ElementRegistry,
        states: Sequence[State],
        window: WindowInfo | None = WINDOW,
    ) -> None:
        self._registry = registry
        self._states = list(states)
        self._window = window
        self.calls = 0

    def snapshot(self, hint: object = None) -> PerceptionSnapshot:
        index = min(self.calls, len(self._states) - 1)
        self.calls += 1
        elements, runtime_ids, nodes = self._states[index]
        self._registry.register(elements, runtime_ids, nodes)
        return PerceptionSnapshot(active_window=self._window, elements=list(elements))


def _seed(
    registry: ElementRegistry,
    elements: Sequence[Element],
    runtime_ids: Sequence[str],
    nodes: Sequence[UiaNode | None] | None = None,
) -> None:
    """Pre-register the planner-visible state (what get_elements would have done)."""
    registry.register(list(elements), list(runtime_ids), list(nodes) if nodes else None)


def _layer(
    perception: FakePerception,
    registry: ElementRegistry,
    patterns: FakePatterns,
    recorder: RecorderSendInput,
    *,
    focus: FocusRecorder | None = None,
    elevated: object = None,
    sleep: RecordingSleep | None = None,
) -> ActionLayer:
    kwargs: dict[str, object] = {}
    if focus is not None:
        kwargs["focus_window"] = focus
    if elevated is not None:
        kwargs["elevated"] = elevated
    if sleep is not None:
        kwargs["sleep"] = sleep
    return ActionLayer(
        perception=perception,
        registry=registry,
        patterns=patterns,
        inputs=InputSynthesizer(
            send_input=recorder, delay_ms=(20, 60), jitter_px=0,
            sleep=sleep if sleep is not None else (lambda _s: None),
        ),
        **kwargs,  # type: ignore[arg-type]
    )


def _request(kind: str, ref: str | None = "e1", **payload: object) -> ActionRequest:
    return ActionRequest(
        kind=kind,  # type: ignore[arg-type]
        target_ref=ref,
        payload=dict(payload),
        rationale="test",
    )


# -- (a) uia_invoke goes through the pattern invoker, never SendInput ----------


def test_uia_invoke_uses_pattern_invoker_and_no_send_input() -> None:
    registry = ElementRegistry()
    node = FakeNode(patterns=frozenset({"Invoke"}), control=FakeControl())
    _seed(registry, [_el("e1")], ["rid-1"], [node])
    perception = FakePerception(registry, [([_el("e1")], ["rid-1"], [node])])
    patterns, recorder = FakePatterns(), RecorderSendInput()
    layer = _layer(perception, registry, patterns, recorder)

    result = layer.execute(_request("uia_invoke"))

    assert result.ok is True
    assert [call[0] for call in patterns.calls] == ["invoke"]
    assert patterns.calls[0][1] is node
    assert recorder.events == []  # never touched SendInput
    assert result.snapshot_after is not None
    assert result.snapshot_after.screenshot is None  # post-state carries no screenshot
    assert "OK" in result.observation and "Button" in result.observation
    assert "Fake App" in result.observation  # names the new foreground


def test_uia_invoke_failure_is_a_result_not_an_exception() -> None:
    registry = ElementRegistry()
    _seed(registry, [_el("e1")], ["rid-1"], [FakeNode(patterns=frozenset({"Invoke"}))])
    perception = FakePerception(registry, [([_el("e1")], ["rid-1"], [FakeNode()])])
    layer = _layer(perception, registry, FakePatterns(ok=False), RecorderSendInput())

    result = layer.execute(_request("uia_invoke"))

    assert result.ok is False
    assert result.error is not None


# -- (b) type_text: Value pattern first, else focus + SendInput with delays ----


def test_type_text_on_value_target_uses_set_value_pattern() -> None:
    registry = ElementRegistry()
    node = FakeNode(patterns=frozenset({"Value"}), control=FakeControl())
    edit = _el("e1", role="Edit", name="doc", actions=frozenset({"set_value", "read"}))
    _seed(registry, [edit], ["rid-1"], [node])
    perception = FakePerception(registry, [([edit], ["rid-1"], [node])])
    patterns, recorder = FakePatterns(), RecorderSendInput()
    layer = _layer(perception, registry, patterns, recorder)

    result = layer.execute(_request("type_text", text="hello"))

    assert result.ok is True
    assert patterns.calls == [("set_value", node, "hello")]
    assert recorder.events == []  # UIA Value path: no key events at all


def test_type_text_on_non_value_target_focuses_then_types() -> None:
    registry = ElementRegistry()
    control = FakeControl()
    node = FakeNode(patterns=frozenset({"Invoke"}), control=control)
    _seed(registry, [_el("e1")], ["rid-1"], [node])
    perception = FakePerception(registry, [([_el("e1")], ["rid-1"], [node])])
    sleep = RecordingSleep()
    recorder = RecorderSendInput()
    layer = _layer(perception, registry, FakePatterns(), recorder, sleep=sleep)

    result = layer.execute(_request("type_text", text="hi"))

    assert result.ok is True
    assert control.focused is True  # focused the element before typing
    assert [(e.scan, e.unicode, e.up) for e in recorder.events] == [
        (ord("h"), True, False),
        (ord("h"), True, True),
        (ord("i"), True, False),
        (ord("i"), True, True),
    ]
    assert sleep.durations
    assert all(0.020 <= d <= 0.060 for d in sleep.durations)  # inter-key delays


# -- (c) unknown ref / gone element: re-snapshot observation, NO input ----------


def test_unknown_ref_refuses_with_resnapshot_observation_and_no_input() -> None:
    registry = ElementRegistry()
    perception = FakePerception(registry, [([], [], [])])
    patterns, recorder = FakePatterns(), RecorderSendInput()
    layer = _layer(perception, registry, patterns, recorder)

    result = layer.execute(_request("click", ref="e9"))

    assert result.ok is False
    assert "unknown ref" in result.observation
    assert "re-snapshot" in result.observation
    assert patterns.calls == []
    assert recorder.events == []


def test_gone_element_refuses_with_resnapshot_observation_and_no_input() -> None:
    registry = ElementRegistry()
    _seed(registry, [_el("e1")], ["rid-1"], [FakeNode()])
    # the fresh walk no longer contains rid-1 anywhere
    perception = FakePerception(
        registry, [([_el("e1", name="Other")], ["rid-2"], [FakeNode()])]
    )
    patterns, recorder = FakePatterns(), RecorderSendInput()
    layer = _layer(perception, registry, patterns, recorder)

    result = layer.execute(_request("click"))

    assert result.ok is False
    assert "element gone" in result.observation
    assert "re-snapshot" in result.observation
    assert patterns.calls == []
    assert recorder.events == []


# -- (d) elevated target refuses ------------------------------------------------


def test_elevated_target_refuses_without_any_input() -> None:
    registry = ElementRegistry()
    node = FakeNode(patterns=frozenset({"Invoke"}), control=FakeControl())
    _seed(registry, [_el("e1")], ["rid-1"], [node])
    perception = FakePerception(registry, [([_el("e1")], ["rid-1"], [node])])
    patterns, recorder = FakePatterns(), RecorderSendInput()
    layer = _layer(perception, registry, patterns, recorder, elevated=lambda _node: True)

    result = layer.execute(_request("uia_invoke"))

    assert result.ok is False
    assert "elevat" in result.observation.lower()
    assert patterns.calls == []
    assert recorder.events == []


# -- (e) click targets the REVALIDATED bbox center ------------------------------


def test_click_uses_revalidated_bbox_center_after_a_move() -> None:
    registry = ElementRegistry()
    _seed(registry, [_el("e1", bbox=(0, 0, 100, 50))], ["rid-1"], [FakeNode()])
    # the window moved: same runtime id, new bbox — the old bbox is stale
    moved = _el("e1", name="OK", bbox=(500, 300, 100, 50))
    perception = FakePerception(registry, [([moved], ["rid-1"], [FakeNode()])])
    recorder = RecorderSendInput()
    layer = _layer(perception, registry, FakePatterns(), recorder)

    result = layer.execute(_request("click"))

    assert result.ok is True
    assert recorder.events == [
        MouseEvent(kind="move", x=550, y=325),  # center of the NEW bbox
        MouseEvent(kind="down", button="left"),
        MouseEvent(kind="up", button="left"),
    ]


# -- (f) hotkey/scroll re-focus the element's window, then emit sequences -------


def test_hotkey_refocuses_window_and_emits_down_then_reverse_up() -> None:
    registry = ElementRegistry()
    _seed(registry, [_el("e1")], ["rid-1"], [FakeNode()])
    perception = FakePerception(registry, [([_el("e1")], ["rid-1"], [FakeNode()])])
    focus = FocusRecorder()
    recorder = RecorderSendInput(order=focus.order)  # shared ordering log
    layer = _layer(perception, registry, FakePatterns(), recorder, focus=focus)

    result = layer.execute(_request("hotkey", combo="ctrl+shift+s"))

    assert result.ok is True
    assert focus.hwnds == [4321]  # the element's (scripted) window
    assert [(e.vk, e.up) for e in recorder.events] == [
        (0x11, False), (0x10, False), (0x53, False),
        (0x53, True), (0x10, True), (0x11, True),
    ]
    assert focus.order[0] == "focus"  # re-focus strictly precedes any input
    assert focus.order[1].startswith("input")


def test_scroll_refocuses_window_and_wheels_at_element_center() -> None:
    registry = ElementRegistry()
    _seed(registry, [_el("e1", bbox=(200, 100, 60, 40))], ["rid-1"], [FakeNode()])
    at_point = _el("e1", bbox=(200, 100, 60, 40))
    perception = FakePerception(registry, [([at_point], ["rid-1"], [FakeNode()])])
    focus, recorder = FocusRecorder(), RecorderSendInput()
    layer = _layer(perception, registry, FakePatterns(), recorder, focus=focus)

    result = layer.execute(_request("scroll", lines=2))

    assert result.ok is True
    assert focus.hwnds == [4321]
    assert recorder.events == [
        MouseEvent(kind="move", x=230, y=120),
        MouseEvent(kind="wheel", delta=120),
        MouseEvent(kind="wheel", delta=120),
    ]


# -- (g) wait uses the injected sleep -------------------------------------------


def test_wait_uses_injected_sleep_and_touches_nothing_else() -> None:
    registry = ElementRegistry()
    perception = FakePerception(registry, [([], [], [])])
    sleep = RecordingSleep()
    recorder = RecorderSendInput()
    layer = _layer(perception, registry, FakePatterns(), recorder, sleep=sleep)

    result = layer.execute(_request("wait", ref=None, ms=250))

    assert result.ok is True
    assert sleep.durations == [0.25]
    assert recorder.events == []
    assert perception.calls == 0  # no walk needed for a wait


# -- (h) blocking walk does not stall the planner's resident asyncio loop -------


async def test_blocking_walk_does_not_stall_the_resident_loop() -> None:
    """Review Focus 5: UIA walks/pattern calls/SendInput are blocking and run on
    the planner worker thread only. A slow fake walk behind asyncio.to_thread
    must leave the loop free to service its timers throughout."""
    registry = ElementRegistry()
    node = FakeNode(patterns=frozenset({"Invoke"}), control=FakeControl())
    _seed(registry, [_el("e1")], ["rid-1"], [node])

    class SlowPerception:
        def __init__(self) -> None:
            self.calls = 0

        def snapshot(self, hint: object = None) -> PerceptionSnapshot:
            self.calls += 1
            time.sleep(0.25)  # the blocking "UIA walk"
            registry.register([_el("e1")], ["rid-1"], [node])
            return PerceptionSnapshot(active_window=WINDOW, elements=[_el("e1")])

    layer = ActionLayer(
        perception=SlowPerception(),  # type: ignore[arg-type]
        registry=registry,
        patterns=FakePatterns(),
        inputs=InputSynthesizer(send_input=RecorderSendInput()),
    )

    loop = asyncio.get_running_loop()
    beats: list[float] = []

    async def heartbeat() -> None:
        while True:
            beats.append(loop.time())
            await asyncio.sleep(0.02)

    watcher = asyncio.ensure_future(heartbeat())
    try:
        result = await asyncio.to_thread(layer.execute, _request("uia_invoke"))
    finally:
        watcher.cancel()

    assert result.ok is True
    assert len(beats) > 5
    gaps = [b - a for a, b in zip(beats, beats[1:], strict=False)]
    assert max(gaps) < 0.15  # a 0.25s on-loop walk would gap by ~0.25s
