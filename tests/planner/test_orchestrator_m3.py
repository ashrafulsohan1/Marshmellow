"""M3 planner tool loop (Task 5) — perceive/act/verify over a PlannerTools toolbox.

Offline + scripted (ScriptedLLM pattern per tests/planner/test_orchestrator.py):
the M3 tool set (get_elements/screenshot/act) flows through a toolbox; `act`
requests carry the planner's declared risk while the REAL SafetyGate
re-classifies authoritatively (d); the verify loop escalates after 3
consecutive failed/same-observation acts on one ref (e); the budget is cut at
25 steps (f); screenshot artifacts are purged at task end, success or timeout
(g); and App composition routes ask_user / shell / gated acts with injected
fakes end to end (h).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from marshmellow.app import App
from marshmellow.core.config import Settings
from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    ContextSnapshot,
    Element,
    Instruction,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    PerceptionHint,
    PerceptionSnapshot,
    ProviderCaps,
    Rect,
    Transcript,
    WindowInfo,
)
from marshmellow.packs.base import ShellResult
from marshmellow.perception.registry import ElementRegistry
from marshmellow.planner.orchestrator import (
    MAX_STEPS,
    PROMPT_VERSION,
    TIMEOUT_S,
    LLMPlanner,
)
from marshmellow.planner.tools import ScreenToolbox
from marshmellow.safety.gate import SafetyGate
from marshmellow.safety.policy import RiskPolicy

M3_TOOL_NAMES = {"open_app", "get_elements", "screenshot", "act", "wait", "ask_user", "finish"}
M2_TOOL_NAMES = {"open_app", "wait", "ask_user", "finish"}
ESCALATION = "verification failed 3× on this element — ask the user or finish(ok=false)"


# -- doubles -------------------------------------------------------------------


class ScriptedLLM:
    """Fake LLMProvider: replays scripted LLMResponses, records every request.

    Same replay contract as tests/planner/test_orchestrator.py (local copy —
    the tests directory is not a package and the M2 file must stay untouched).
    """

    def __init__(
        self,
        responses: list[LLMResponse],
        *,
        repeat_last: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.requests: list[LLMRequest] = []
        self._responses = list(responses)
        self._repeat_last = repeat_last
        self._delay = delay

    @property
    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(vision=False, tool_call=True, json_schema=False)

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        if self._delay:
            await asyncio.sleep(self._delay)
        if not self._responses:
            raise AssertionError("ScriptedLLM script exhausted")
        if self._repeat_last and len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)


class FakeToolbox:
    """PlannerTools double: scripted listing + act results, records requests.

    Deliberately does NOT implement purge(): without the §3.15 seam the
    planner must treat the purge as a no-op, not a crash.
    """

    def __init__(
        self,
        listing: str = "e1 Button 'Play' (920,540 60x24) [invoke]",
        results: list[ActionResult] | None = None,
    ) -> None:
        self.listing = listing
        self.results = list(results or [])
        self.requests: list[ActionRequest] = []
        self.get_calls: list[str | None] = []
        self.screenshot_calls = 0

    def get_elements(self, query: str | None) -> str:
        self.get_calls.append(query)
        return self.listing

    def screenshot(self) -> str:
        self.screenshot_calls += 1
        return ""

    def act(self, req: ActionRequest) -> ActionResult:
        self.requests.append(req)
        if not self.results:
            raise AssertionError("FakeToolbox act results exhausted")
        return self.results.pop(0)


class FakeScreenPerception:
    """ScreenPerception double: scripted snapshots, registered into the real
    registry exactly like ScreenPerception does (so gate/ActionLayer resolves)."""

    def __init__(
        self, registry: ElementRegistry, snapshots: list[PerceptionSnapshot]
    ) -> None:
        self._registry = registry
        self._queue = list(snapshots)
        self._last = snapshots[-1] if snapshots else PerceptionSnapshot()
        self.hints: list[PerceptionHint | None] = []

    def snapshot(
        self, hint: PerceptionHint | None = None, hwnd: int | None = None
    ) -> PerceptionSnapshot:
        self.hints.append(hint)
        snap = self._queue.pop(0) if self._queue else self._last
        self._last = snap
        window = snap.active_window
        self._registry.register(
            snap.elements,
            [e.ref for e in snap.elements],
            None,
            hwnd=window.hwnd if window is not None else None,
        )
        return snap


class RecordingActionLayer:
    """ActionLayer double: records execute() requests, always succeeds."""

    def __init__(self) -> None:
        self.requests: list[ActionRequest] = []

    def execute(
        self, req: ActionRequest, snapshot: PerceptionSnapshot | None = None
    ) -> ActionResult:
        self.requests.append(req)
        return ActionResult(ok=True, observation=f"did {req.kind}")


class ScriptedConfirm:
    """Confirmer double: scripted yes/no answers, records the questions."""

    def __init__(self, answers: list[bool]) -> None:
        self.answers = list(answers)
        self.questions: list[str] = []

    def confirm(self, question: str) -> bool:
        self.questions.append(question)
        return self.answers.pop(0) if self.answers else False


class RecordingPack:
    """WindowsPack double: records open_app calls, opens nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def open_app(self, target: str) -> ShellResult:
        self.calls.append(target)
        return ShellResult(ok=True, detail=f"would open {target}")


class PassGate:
    """Gate double: passes everything through, records the guarded requests."""

    def __init__(self) -> None:
        self.guarded: list[ActionRequest] = []

    def guard(self, execute: Callable[[ActionRequest], ActionResult]):
        def gated(req: ActionRequest) -> ActionResult:
            self.guarded.append(req)
            return execute(req)

        return gated


class FakeAskChannel:
    """AskUserChannel double: records questions, returns a scripted answer."""

    def __init__(self) -> None:
        self.questions: list[str] = []
        self.answer = "yes"

    def ask(self, question: str, cancelled: Callable[[], bool] | None = None) -> str:
        self.questions.append(question)
        return self.answer


# -- helpers --------------------------------------------------------------------


def _instruction(text: str, window: WindowInfo | None = None) -> Instruction:
    return Instruction(
        text=text,
        transcript=Transcript(text=text, source="fixture"),
        context=ContextSnapshot(active_window=window),
    )


def _call(name: str, **arguments: object) -> LLMToolCall:
    return LLMToolCall(name=name, arguments=dict(arguments))


def _executor() -> tuple[Callable[[ActionRequest], ActionResult], list[ActionRequest]]:
    executed: list[ActionRequest] = []

    def execute(req: ActionRequest) -> ActionResult:
        executed.append(req)
        if req.kind == "ask_user":
            return ActionResult(ok=True, observation="yes")
        return ActionResult(ok=True, observation="done")

    return execute, executed


def _element(
    ref: str,
    role: str = "Button",
    name: str = "Play",
    *,
    x: int = 920,
    y: int = 540,
    w: int = 60,
    h: int = 24,
    actions: tuple[str, ...] = ("invoke",),
    value: str | None = None,
    enabled: bool = True,
    password: bool = False,
) -> Element:
    return Element(
        ref=ref,
        role=role,
        name=name,
        value=value,
        bbox=Rect(x=x, y=y, w=w, h=h),
        actions=set(actions),  # type: ignore[arg-type]
        enabled=enabled,
        password=password,
    )


def _resolver(registry: ElementRegistry) -> Callable[[str | None], object]:
    def resolve(ref: str | None) -> object:
        if ref is None:
            return None
        handle = registry.resolve(ref)
        return handle.element if handle is not None else None

    return resolve


def _offered(llm: ScriptedLLM) -> set[str]:
    return {str(tool["function"]["name"]) for tool in llm.requests[0].tools}  # type: ignore[index]


# -- constants + tool set -------------------------------------------------------


def test_plan_constants_grow_for_m3() -> None:
    assert MAX_STEPS == 25
    assert TIMEOUT_S == 180.0
    assert PROMPT_VERSION == "v2"


def test_m3_tool_set_offered_only_with_a_toolbox() -> None:
    with_toolbox = ScriptedLLM([LLMResponse(text="Done.", stop_reason="stop")])
    LLMPlanner(with_toolbox, toolbox=FakeToolbox()).run(_instruction("hi"), _executor()[0])
    assert _offered(with_toolbox) == M3_TOOL_NAMES

    without = ScriptedLLM([LLMResponse(text="Done.", stop_reason="stop")])
    LLMPlanner(without).run(_instruction("hi"), _executor()[0])
    assert _offered(without) == M2_TOOL_NAMES  # no toolbox → the M2 set, unchanged


# (a) golden trace: get_elements → act(uia_invoke) → finish speaks the summary
def test_golden_trace_get_elements_act_finish() -> None:
    toolbox = FakeToolbox(
        results=[ActionResult(ok=True, observation="invoked Button 'Play' — foreground: Player")]
    )
    llm = ScriptedLLM(
        [
            LLMResponse(tool_calls=[_call("get_elements")], stop_reason="tool_calls"),
            LLMResponse(
                tool_calls=[
                    _call(
                        "act",
                        kind="uia_invoke",
                        target_ref="e1",
                        rationale="press play",
                        risk="L1",
                    )
                ],
                stop_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[_call("finish", ok=True, summary_to_speak="Now playing.")],
                stop_reason="tool_calls",
            ),
        ]
    )
    execute, executed = _executor()

    result = LLMPlanner(llm, toolbox=toolbox).run(_instruction("play the song"), execute)

    assert result.ok and result.summary_to_speak == "Now playing."
    assert toolbox.get_calls == [None]  # no query
    assert len(toolbox.requests) == 1
    req = toolbox.requests[0]
    assert req.kind == "uia_invoke" and req.target_ref == "e1" and req.payload == {}
    assert req.rationale == "press play" and req.risk == "L1"
    assert executed == []  # get_elements/act ride the toolbox, NOT the M2 executor seam
    # the listing reached the model before it acted (refs come from get_elements)
    assert "e1 Button 'Play'" in llm.requests[1].messages[-1].content
    # the act result reached the model before finish (verify duty)
    assert "invoked Button 'Play'" in llm.requests[2].messages[-1].content


# (b) stale-ref act → error observation → model re-snapshots → retry succeeds
def test_stale_ref_error_then_resnapshot_then_retry() -> None:
    toolbox = FakeToolbox(
        results=[
            ActionResult(
                ok=False, observation="unknown ref e7 — re-snapshot", error="unknown_ref"
            ),
            ActionResult(ok=True, observation="invoked Button 'Play' — foreground: Player"),
        ]
    )
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[_call("act", kind="uia_invoke", target_ref="e7", rationale="press")],
                stop_reason="tool_calls",
            ),
            LLMResponse(tool_calls=[_call("get_elements")], stop_reason="tool_calls"),
            LLMResponse(
                tool_calls=[
                    _call("act", kind="uia_invoke", target_ref="e1", rationale="press play")
                ],
                stop_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[_call("finish", ok=True, summary_to_speak="Playing.")],
                stop_reason="tool_calls",
            ),
        ]
    )
    execute, _ = _executor()

    result = LLMPlanner(llm, toolbox=toolbox).run(_instruction("play"), execute)

    assert result.ok and result.summary_to_speak == "Playing."
    assert "ERROR" in llm.requests[1].messages[-1].content
    assert "re-snapshot" in llm.requests[1].messages[-1].content
    assert toolbox.get_calls == [None]  # the model re-snapshotted once after the error
    assert [r.target_ref for r in toolbox.requests] == ["e7", "e1"]
    # a single failed act must NOT trigger the escalation
    for req in llm.requests:
        assert all("verification failed" not in m.content for m in req.messages)


# (c) gate-refused L3 (confirm=no) → refusal observation → finish(ok=false)
def test_gate_refusal_feeds_back_and_finish_fails() -> None:
    toolbox = FakeToolbox(
        results=[
            ActionResult(
                ok=False,
                observation="refused: user did not confirm type_text on 'Password'",
                error="refused",
            )
        ]
    )
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    _call(
                        "act",
                        kind="type_text",
                        target_ref="e2",
                        payload={"text": "hi"},
                        rationale="fill the field",
                    )
                ],
                stop_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[
                    _call(
                        "finish",
                        ok=False,
                        summary_to_speak="You declined, so I stopped without typing.",
                    )
                ],
                stop_reason="tool_calls",
            ),
        ]
    )
    execute, _ = _executor()

    result = LLMPlanner(llm, toolbox=toolbox).run(_instruction("fill it"), execute)

    assert not result.ok
    assert "declined" in result.summary_to_speak
    assert "refused" in llm.requests[1].messages[-1].content  # refusal was observed


# (d) bypass attempt: declared risk "L0" on a password element → still L3-confirmed
def test_declared_l0_on_password_element_still_requires_confirmation() -> None:
    registry = ElementRegistry()
    snapshot = PerceptionSnapshot(
        active_window=WindowInfo(hwnd=99, title="Sign in", app="TestApp"),
        elements=[_element("e5", role="Edit", name="", actions=("read", "set_value"),
                           x=10, y=10, w=200, h=24, password=True)],
    )
    perception = FakeScreenPerception(registry, [snapshot])
    layer = RecordingActionLayer()
    confirm = ScriptedConfirm([False])  # the user says no
    gate = SafetyGate(
        policy=RiskPolicy(),
        confirm=confirm,
        announce=lambda text: None,
        resolve=_resolver(registry),  # type: ignore[arg-type]
    )
    seen: list[ActionRequest] = []
    gated = gate.guard(layer.execute)

    def seam(req: ActionRequest) -> ActionResult:
        seen.append(req)
        return gated(req)

    toolbox = ScreenToolbox(perception, registry, seam)
    llm = ScriptedLLM(
        [
            LLMResponse(tool_calls=[_call("get_elements")], stop_reason="tool_calls"),
            LLMResponse(
                tool_calls=[
                    _call(
                        "act",
                        kind="type_text",
                        target_ref="e5",
                        payload={"text": "hunter2"},
                        rationale="fill the password field",
                        risk="L0",  # the bypass attempt
                    )
                ],
                stop_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[
                    _call(
                        "finish",
                        ok=False,
                        summary_to_speak="You said no, so I did not type the password.",
                    )
                ],
                stop_reason="tool_calls",
            ),
        ]
    )
    execute, _ = _executor()

    result = LLMPlanner(llm, toolbox=toolbox).run(_instruction("type my password"), execute)

    # the gate re-classified: declared L0 on a password field still demanded
    # spoken confirmation (§3.13: the declared risk is advisory only)
    assert confirm.questions and "confirm" in confirm.questions[0]
    assert layer.requests == []  # refused → nothing was executed
    assert not result.ok
    assert "refused" in llm.requests[2].messages[-1].content
    # the declared risk traveled on the request (carried, then overridden)
    assert len(seen) == 1
    assert seen[0].risk == "L0"
    assert seen[0].kind == "type_text" and seen[0].payload == {"text": "hunter2"}


# (e) 3× same failure on one ref → escalation message → ask_user round trip
def test_three_same_failures_inject_escalation_then_ask_user() -> None:
    failure = ActionResult(
        ok=False, observation="element gone — re-snapshot", error="stale_ref"
    )
    toolbox = FakeToolbox(results=[failure, failure, failure])
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[_call("act", kind="uia_invoke", target_ref="e3", rationale="press")],
                stop_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[_call("act", kind="uia_invoke", target_ref="e3", rationale="press")],
                stop_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[_call("act", kind="uia_invoke", target_ref="e3", rationale="press")],
                stop_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[
                    _call("ask_user", question="The button stopped responding — try elsewhere?")
                ],
                stop_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[
                    _call("finish", ok=False, summary_to_speak="The button would not respond.")
                ],
                stop_reason="tool_calls",
            ),
        ]
    )
    execute, executed = _executor()

    result = LLMPlanner(llm, toolbox=toolbox).run(_instruction("press it"), execute)

    assert not result.ok
    assert len(toolbox.requests) == 3  # exactly the three failing acts
    # the escalation user-message was injected after the third consecutive failure
    assert llm.requests[3].messages[-1].content == ESCALATION
    # the model then asked the user; the answer travelled back before finish
    assert any(r.kind == "ask_user" for r in executed)
    assert "yes" in llm.requests[4].messages[-1].content


# (f) step budget cut at 25
def test_step_budget_cut_at_twenty_five() -> None:
    llm = ScriptedLLM(
        [LLMResponse(tool_calls=[_call("open_app", target="notepad")], stop_reason="tool_calls")],
        repeat_last=True,
    )
    execute, executed = _executor()

    result = LLMPlanner(llm, toolbox=FakeToolbox()).run(_instruction("open notepad"), execute)

    assert not result.ok
    assert result.summary_to_speak  # speaks a failure, never hangs
    assert len(llm.requests) == 25
    assert len(executed) == 25  # open_app stays M2 sugar on the executor seam


# (g) screenshot purge: a vision-path file exists mid-task, gone after finish
def test_screenshot_artifacts_purged_at_task_end(tmp_path: Path) -> None:
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")
    marked = tmp_path / "marked.png"
    marked.write_bytes(b"png")
    registry = ElementRegistry()
    perception = FakeScreenPerception(
        registry,
        [
            PerceptionSnapshot(
                active_window=WindowInfo(hwnd=5, title="Canvas", app="Paint"),
                elements=[],
                screenshot=shot,
                marked_image=marked,
            )
        ],
    )
    toolbox = ScreenToolbox(perception, registry, _executor()[0])
    llm = ScriptedLLM(
        [
            LLMResponse(tool_calls=[_call("screenshot")], stop_reason="tool_calls"),
            LLMResponse(
                tool_calls=[_call("finish", ok=True, summary_to_speak="Had a look.")],
                stop_reason="tool_calls",
            ),
        ]
    )
    execute, _ = _executor()

    assert shot.exists() and marked.exists()  # the vision-path files exist mid-task
    result = LLMPlanner(llm, toolbox=toolbox).run(_instruction("what is on screen"), execute)

    assert result.ok
    assert not shot.exists() and not marked.exists()  # purged in the finally (§3.15)
    # the screenshot tool returned the marked-image path to the model
    assert str(marked) in llm.requests[1].messages[-1].content


def test_screenshot_artifacts_purged_on_timeout(tmp_path: Path) -> None:
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")
    registry = ElementRegistry()
    perception = FakeScreenPerception(
        registry,
        [
            PerceptionSnapshot(
                active_window=WindowInfo(hwnd=5, title="T", app="A"), screenshot=shot
            )
        ],
    )
    toolbox = ScreenToolbox(perception, registry, _executor()[0])
    llm = ScriptedLLM(
        [LLMResponse(tool_calls=[_call("get_elements")], stop_reason="tool_calls")],
        repeat_last=True,
        delay=0.02,
    )

    result = LLMPlanner(llm, toolbox=toolbox, timeout_s=0.05).run(
        _instruction("look"), _executor()[0]
    )

    assert not result.ok  # wall-clock cut
    assert not shot.exists()  # the finally purge ran anyway


# -- ScreenToolbox (real adapter) ------------------------------------------------


def test_screen_toolbox_element_lines_are_token_cheap() -> None:
    registry = ElementRegistry()
    perception = FakeScreenPerception(
        registry,
        [
            PerceptionSnapshot(
                active_window=WindowInfo(hwnd=5, title="Player", app="Spotify"),
                elements=[
                    _element("e12", name="Play"),
                    _element("e2", role="Edit", name="", x=10, y=10, w=200, h=24,
                             actions=("read", "set_value"), password=True),
                    _element("e3", role="Edit", name="Search", value="hi", x=30, y=30,
                             w=120, h=24, actions=("read", "set_value"), enabled=False),
                ],
            )
        ],
    )
    toolbox = ScreenToolbox(perception, registry, _executor()[0])

    listing = toolbox.get_elements(None)
    lines = listing.splitlines()

    assert lines[0] == "window: Spotify — Player (3 elements)"
    assert "e12 Button 'Play' (920,540 60x24) [invoke]" in lines
    assert "e2 Edit '' (10,10 200x24) [read,set_value] (password)" in lines
    assert "e3 Edit 'Search' (30,30 120x24) [read,set_value] value='hi' (disabled)" in lines
    assert registry.resolve("e12") is not None  # get_elements registered the walk

    toolbox.get_elements("the search box")
    assert perception.hints[-1] == PerceptionHint(query="the search box")
    toolbox.get_elements("")  # an empty query is no hint at all
    assert perception.hints[-1] is None


# (h) App(headless=True) with injected fakes, end to end
@pytest.mark.scenario
async def test_app_headless_composition_with_injected_fakes(tmp_path: Path) -> None:
    pack = RecordingPack()
    layer = RecordingActionLayer()
    gate = PassGate()
    ask = FakeAskChannel()
    registry = ElementRegistry()
    perception = FakeScreenPerception(registry, [])
    holder: dict[str, Callable[[ActionRequest], ActionResult]] = {}
    toolbox = ScreenToolbox(perception, registry, lambda req: holder["execute"](req))
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[_call("open_app", target="notepad")], stop_reason="tool_calls"
            ),
            LLMResponse(
                tool_calls=[
                    _call(
                        "act",
                        kind="type_text",
                        payload={"text": "hello"},
                        rationale="type the greeting",
                    )
                ],
                stop_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[
                    _call(
                        "finish", ok=True, summary_to_speak="Opened Notepad and typed hello."
                    )
                ],
                stop_reason="tool_calls",
            ),
        ]
    )
    planner = LLMPlanner(llm, toolbox=toolbox)

    app = App(
        Settings(),
        headless=True,
        log_dir=tmp_path,
        pack=pack,
        perception=perception,
        action_layer=layer,
        gate=gate,
        ask_channel=ask,
        planner=planner,
    )
    holder["execute"] = app._execute
    app.start()
    try:
        handle = await app.submit_text("open notepad and type hello")

        assert handle.result is not None and handle.result.ok
        assert handle.result.summary_to_speak == "Opened Notepad and typed hello."
        assert pack.calls == ["notepad"]  # shell → the DE pack, not the gate
        assert [(r.kind, r.payload.get("text")) for r in layer.requests] == [
            ("type_text", "hello")
        ]
        assert gate.guarded and gate.guarded[0].kind == "type_text"  # acts pass the gate
        spoken = [s.text for s in app.tts.spoken]
        assert any("typed hello" in t.lower() for t in spoken)  # FakeTTS spoke the result
    finally:
        app.stop()
