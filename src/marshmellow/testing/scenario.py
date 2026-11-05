"""Scenario runner — YAML-driven end-to-end proof runs (M3 Tasks 6-7).

Two modes. **Scripted** (M3 Task 6) is fully headless: a scenario YAML pins the
instruction, the canned UIA states (a JSON list of ``PerceptionSnapshot`` dumps
mirroring a fixture page's states), the fake provider's ordered tool script, the
user's scripted confirm answers, and the assertions (the executed trace +
substrings of the final spoken summary). **Fixture-app** (M3 Task 7,
``run_fixture`` / ``mode: fixture:<alias>``) runs the same production loop
against a REAL app window: real perception walking the fixture's own window,
real ActionLayer, real SafetyGate — see the fixture section at the bottom.

``run_scripted`` builds ``App(headless=True)`` around the REAL M3 stack: the
real ``ScreenPerception`` with a real ``UIAReader`` over :class:`CannedUiaSource`
(the canned states walked as a UIA tree, refs re-derived exactly as in
production), and — when the scenario ships a vision reply — the REAL
``VisionPerceiver`` over the canned screenshot (D5: vision is wired only when a
vision-capable provider exists). Acts flow through the REAL ``SafetyGate`` +
``ConfirmFlow`` (the scripted ask channel answers; headless means no tray
fallback, so ambiguity refuses) in front of a recording action layer.

Zero real provider calls are possible: the only provider in the loop is
:class:`ScenarioProvider` (in-process, scripted). The vision fallback calls the
SAME fake — its vision requests are counted on the provider, so a scenario can
prove the SoM path ran without any network.

Executed-trace semantics (what the YAML ``trace`` asserts): a step is recorded
when it actually executed — ``get_elements``/``screenshot`` when the toolbox
read perception, ``act:<kind>`` ONLY when the request reached the action layer
(a gate refusal or an unknown ref is not an execution), and the terminal
``finish``. Two script entries configure the fakes instead of the tool loop:
``ask_user`` (``args.answer``) seeds the scripted confirm answers and ``vision``
(``args.reply``) is the fake provider's SoM answer; neither reaches the planner.
A ``finish`` call must be its own script entry (never bundled with actions) so
the orchestrator's verify-lite does not defer it.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml  # type: ignore[import-untyped]  # transitive dep (faster-whisper→ctranslate2)
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from marshmellow.actions.executor import ActionLayer
from marshmellow.actions.input_synthesis import InputSynthesizer
from marshmellow.actions.uia_actions import UiaAutomationPatterns
from marshmellow.app import App
from marshmellow.core.config import Settings
from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    Element,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    PerceptionSnapshot,
    ProviderCaps,
    Rect,
    SpeakRequest,
    WindowInfo,
)
from marshmellow.packs.base import DEShell
from marshmellow.packs.windows import WindowsPack, window_title
from marshmellow.perception import PerceptionError
from marshmellow.perception.registry import ElementRegistry
from marshmellow.perception.snapshot import ScreenPerception
from marshmellow.perception.uia_reader import UiaAutomationSource, UiaNode, UIAReader
from marshmellow.perception.vision import VisionPerceiver
from marshmellow.planner.orchestrator import LLMPlanner
from marshmellow.planner.tools import ScreenToolbox
from marshmellow.safety.confirm import ConfirmFlow
from marshmellow.safety.gate import SafetyGate
from marshmellow.safety.policy import RiskPolicy
from marshmellow.testing.fixtures import FixtureApp, FixtureError, focus_window
from marshmellow.tts.fake import FakeTTS

# Inverse of the reader's pattern map: canned Element actions back to the UIA
# pattern names the walk would have reported (uia_reader._PATTERN_MAP).
_ACTION_TO_PATTERN: Final[dict[str, str]] = {
    "invoke": "Invoke",
    "toggle": "Toggle",
    "set_value": "Value",
    "select": "SelectionItem",
    "expand": "ExpandCollapse",
}


class ScenarioError(RuntimeError):
    """A scenario YAML/fixture is malformed, or the runner was misconfigured."""


# -- YAML schema -----------------------------------------------------------------


class ScriptCall(BaseModel):
    """One ordered script entry: a tool call, or an ask_user/vision directive."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(pattern=r"^[a-z_]+$")
    args: dict[str, object] = Field(default_factory=dict)


class ElementAssertion(BaseModel):
    """One UIA-tree assertion (fixture scenarios): a role plus substring matches.

    Matching is case-insensitive. ``value_contains`` reads the UIA Value pattern
    (edits/documents); ``text_contains`` reads the UIA TextPattern document text
    (conhost's 'Text Area', scrollbacks). Assertions run against a RAW walk of
    the window — ALL controls, not just the interactable subset the planner sees
    — so read-only surfaces (results, status text) are assertable too.
    """

    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1)
    name_contains: str = ""
    value_contains: str | None = None
    text_contains: str | None = None


class ScenarioSpec(BaseModel):
    """The pinned top-level YAML keys; unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    mode: str = Field(pattern=r"^scripted$|^fixture:[a-z0-9_-]+$")
    snapshots: Path | None = None
    script: list[ScriptCall] = Field(default_factory=list)
    trace: list[str] = Field(default_factory=list)
    spoken_contains: list[str] = Field(default_factory=list)
    # fixture mode only:
    launch: str | None = None  # opened via pack.open_app instead of the bare alias
    pin: ElementAssertion | None = None  # app state that must exist BEFORE acting
    assert_elements: list[ElementAssertion] = Field(default_factory=list)

    @field_validator("trace")
    @classmethod
    def _trace_entries(cls, value: list[str]) -> list[str]:
        for entry in value:
            if not re.fullmatch(r"[a-z_]+(:[a-z_]+)?", entry):
                raise ValueError(f"trace entry {entry!r} must be 'tool' or 'act:<kind>'")
        return value

    @model_validator(mode="after")
    def _pinned_combinations(self) -> ScenarioSpec:
        if self.mode == "scripted" and self.snapshots is None:
            raise ValueError("scripted mode requires a snapshots path")
        if self.mode != "scripted" and self.snapshots is not None:
            raise ValueError("canned snapshots belong to scripted mode only")
        if self.mode == "scripted" and (self.launch or self.pin or self.assert_elements):
            raise ValueError("launch/pin/assert_elements belong to fixture mode only")
        for entry in self.script:
            if entry.tool == "ask_user" and not isinstance(entry.args.get("answer"), str):
                raise ValueError("ask_user script entries need a string args.answer")
            if entry.tool == "vision" and not isinstance(entry.args.get("reply"), str):
                raise ValueError("vision script entries need a string args.reply")
        return self


@dataclass
class ScenarioReport:
    """The run's outcome: assertion verdict, executed trace, final spoken text.

    ``element_count`` (fixture runs) is the fresh interactable-element count of
    the fixture window after the run — the SP1 fallback-threshold measure.
    """

    ok: bool
    trace: list[str]
    spoken: str
    element_count: int | None = None


def load_scenario(path: Path) -> ScenarioSpec:
    """Load and pydantic-validate one scenario YAML.

    A relative ``snapshots`` path is resolved against the YAML's directory, so
    specs carry an absolute path from here on.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScenarioError(f"scenario file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ScenarioError(f"scenario {path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ScenarioError(f"scenario {path} must be a mapping of the pinned keys")
    try:
        spec = ScenarioSpec.model_validate(raw)
    except ValidationError as exc:
        raise ScenarioError(f"invalid scenario {path}: {exc}") from exc
    if spec.snapshots is not None and not spec.snapshots.is_absolute():
        spec.snapshots = path.parent / spec.snapshots
    if spec.launch is not None:
        candidate = path.parent / spec.launch
        if candidate.is_file():
            spec.launch = candidate.as_uri()  # a fixture file → file:/// URI
    return spec


def load_snapshots(path: Path) -> list[PerceptionSnapshot]:
    """Load the canned ``PerceptionSnapshot`` dumps for a scripted scenario.

    Fixture contract: elements are UIA-only (vision elements are produced live
    by the SoM fallback, D5) and each state's refs are exactly e1..eN in walk
    order — the real reader re-derives exactly those. Relative screenshot paths
    resolve against the JSON's directory.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScenarioError(f"snapshots file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"snapshots {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ScenarioError(
            f"snapshots {path} must be a non-empty JSON list of PerceptionSnapshot dumps"
        )
    try:
        snapshots = [PerceptionSnapshot.model_validate(item) for item in raw]
    except ValidationError as exc:
        raise ScenarioError(f"invalid PerceptionSnapshot dump in {path}: {exc}") from exc
    for snapshot in snapshots:
        for element in snapshot.elements:
            if element.source != "uia":
                raise ScenarioError(
                    f"{path}: canned elements must be UIA-only; got {element.ref} "
                    f"with source={element.source!r} (vision is produced live, D5)"
                )
        expected = [f"e{i}" for i in range(1, len(snapshot.elements) + 1)]
        if [element.ref for element in snapshot.elements] != expected:
            raise ScenarioError(f"{path}: element refs must be {expected} in walk order")
        for attr in ("screenshot", "marked_image"):
            value: Path | None = getattr(snapshot, attr)
            if value is not None and not value.is_absolute():
                setattr(snapshot, attr, path.parent / value)
    return snapshots


# -- fakes (the canned perception side and the scripted user side) -----------------


@dataclass
class CannedState:
    """One canned page state: the walked window, its UIA elements, its shot."""

    window: WindowInfo
    elements: list[Element]
    screenshot: Path | None


@dataclass
class CannedNode:
    """One canned UIA control — satisfies UiaNode, rebuilt from a fixture Element."""

    runtime_id: str
    control_type: str
    name: str
    value: str | None
    enabled: bool
    offscreen: bool
    password: bool
    rect: tuple[int, int, int, int]
    patterns: frozenset[str]

    @classmethod
    def from_element(cls, element: Element, index: int) -> CannedNode:
        return cls(
            runtime_id=f"canned-{index + 1}",
            control_type=element.role,
            name=element.name,
            value=element.value,
            enabled=element.enabled,
            offscreen=False,
            password=element.password,
            rect=(
                element.bbox.x,
                element.bbox.y,
                element.bbox.w,
                element.bbox.h,
            ),
            patterns=frozenset(
                _ACTION_TO_PATTERN[action]
                for action in element.actions
                if action in _ACTION_TO_PATTERN
            ),
        )


class CannedUiaSource:
    """UiaTreeSource over the canned states: ``walk`` returns the current state.

    ``advance()`` — wired to the recording action layer's executions — moves to
    the next canned state and drops the reader's walk cache (via ``on_advance``)
    so the next get_elements re-derives the new state instead of hitting the
    per-tick cache; the settle-poll then sees two agreeing fresh walks.
    """

    def __init__(self, states: list[CannedState]) -> None:
        if not states:
            raise ScenarioError("a scripted scenario needs at least one canned state")
        self.states = states
        self._index = 0
        self.on_advance: Callable[[], None] | None = None

    @property
    def index(self) -> int:
        return self._index

    @property
    def state(self) -> CannedState:
        return self.states[self._index]

    def walk(self, hwnd: int) -> list[UiaNode]:
        return [
            CannedNode.from_element(element, i)
            for i, element in enumerate(self.state.elements)
        ]

    def advance(self) -> None:
        if self._index < len(self.states) - 1:
            self._index += 1
            if self.on_advance is not None:
                self.on_advance()


class ScenarioActionLayer:
    """ActionLayer double: resolves refs through the shared registry, records
    every request that reached the layer (the executed-trace source for acts),
    mirrors the real layer's unknown-ref refusal, and advances the canned page
    state after each executed action."""

    def __init__(
        self, registry: ElementRegistry, on_execute: Callable[[], None] | None = None
    ) -> None:
        self.requests: list[ActionRequest] = []
        self._registry = registry
        self._on_execute = on_execute

    def execute(
        self, req: ActionRequest, snapshot: PerceptionSnapshot | None = None
    ) -> ActionResult:
        handle = self._registry.resolve(req.target_ref) if req.target_ref else None
        if req.target_ref and handle is None:
            return ActionResult(
                ok=False, observation="unknown ref — re-snapshot", error="unknown_ref"
            )
        self.requests.append(req)
        if self._on_execute is not None:
            self._on_execute()
        name = handle.element.name if handle is not None else (req.target_ref or req.kind)
        return ActionResult(ok=True, observation=f"did {req.kind} on {name!r}")


class ScriptedAskChannel:
    """AskUserChannel double: pops the scripted answers in order and records the
    questions. An exhausted queue answers "" — silence refuses, fail closed."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.questions: list[str] = []

    def ask(self, question: str, cancelled: Callable[[], bool] | None = None) -> str:
        self.questions.append(question)
        return self._answers.pop(0) if self._answers else ""


class ScenarioProvider:
    """The scripted fake LLM provider — zero real calls are possible.

    Planner traffic pops the YAML script's tool calls in order; a request
    carrying images is the SoM vision pass and gets the canned reply. Both
    kinds are recorded so scenarios can assert what actually ran.
    """

    def __init__(
        self, calls: list[LLMToolCall], vision_reply: str | None = None
    ) -> None:
        self._calls = list(calls)
        self._vision_reply = vision_reply
        self.emitted: list[LLMToolCall] = []
        self.planner_requests: list[LLMRequest] = []
        self.vision_requests: list[LLMRequest] = []

    @property
    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(
            vision=self._vision_reply is not None, tool_call=True, json_schema=False
        )

    @property
    def vision_reply(self) -> str | None:
        return self._vision_reply

    async def complete(self, req: LLMRequest) -> LLMResponse:
        if any(message.images for message in req.messages):
            self.vision_requests.append(req)
            return LLMResponse(text=self._vision_reply or "", model="scenario-fake")
        self.planner_requests.append(req)
        if not self._calls:
            raise AssertionError(
                "scenario script exhausted — the script must end with a finish entry"
            )
        call = self._calls.pop(0)
        self.emitted.append(call)
        return LLMResponse(tool_calls=[call], stop_reason="tool_calls", model="scenario-fake")


def scripted_provider(spec: ScenarioSpec) -> ScenarioProvider:
    """Build the fake provider from the spec's script: tool entries replay in
    order; the ask_user/vision directives configure the runner instead."""
    calls: list[LLMToolCall] = []
    vision_reply: str | None = None
    for entry in spec.script:
        if entry.tool == "ask_user":
            continue
        if entry.tool == "vision":
            reply = entry.args.get("reply")
            vision_reply = reply if isinstance(reply, str) else None
            continue
        calls.append(LLMToolCall(name=entry.tool, arguments=dict(entry.args)))
    return ScenarioProvider(calls, vision_reply)


# -- the runner -------------------------------------------------------------------


@dataclass
class _RunWiring:
    """Everything the run needs in one place (the App plus the recording seams)."""

    app: App
    layer: ScenarioActionLayer | _RecordingActionLayer
    provider: ScenarioProvider


def run_scripted(
    spec: ScenarioSpec,
    *,
    snapshots: list[PerceptionSnapshot],
    provider: ScenarioProvider,
    pack: DEShell,
    tts: FakeTTS,
    log_dir: Path | None = None,
) -> ScenarioReport:
    """Run one scripted scenario headlessly and assert trace + spoken result.

    Builds ``App(headless=True)`` over the canned perception and fakes, submits
    the instruction, and compares the executed trace and the final spoken
    summary against the spec. Returns the report; callers assert ``report.ok``
    (the report carries the evidence: trace + spoken).
    """
    if spec.mode != "scripted":
        raise ScenarioError(f"run_scripted runs mode 'scripted' only, got {spec.mode!r}")
    wiring = _build_run(spec, snapshots, provider, pack, tts, log_dir)
    wiring.app.start()
    try:
        handle = asyncio.run(wiring.app.submit_text(spec.instruction))
    finally:
        wiring.app.stop()
    result = handle.result
    trace = _executed_trace(wiring.provider.emitted, wiring.layer.requests)
    spoken = result.summary_to_speak if result is not None else ""
    ok = result is not None and _assertions_hold(spec, trace, spoken)
    return ScenarioReport(ok=ok, trace=trace, spoken=spoken)


def _build_run(
    spec: ScenarioSpec,
    snapshots: list[PerceptionSnapshot],
    provider: ScenarioProvider,
    pack: DEShell,
    tts: FakeTTS,
    log_dir: Path | None,
) -> _RunWiring:
    """Compose the headless App: real perception/gate/confirm over the fakes."""
    if not snapshots:
        raise ScenarioError("a scripted scenario needs at least one canned snapshot")
    states = _canned_states(snapshots)
    registry = ElementRegistry()
    source = CannedUiaSource(states)
    reader = UIAReader(source=source)
    source.on_advance = reader.invalidate  # state advanced → next walk must be fresh
    perception = ScreenPerception(
        reader=reader,
        registry=registry,
        foreground=lambda: states[source.index].window,
        vision=_build_vision(provider, source),
    )
    layer = ScenarioActionLayer(registry, on_execute=source.advance)
    ask = ScriptedAskChannel(_ask_answers(spec))
    gate = SafetyGate(
        policy=RiskPolicy(),
        confirm=ConfirmFlow(ask=ask.ask, fallback=None),  # headless: no tray → fail closed
        announce=lambda text: tts.say(SpeakRequest(text=text, priority="ack")),
        kill_probe=lambda: False,
        resolve=_resolver(registry),
    )
    # The toolbox's execute seam is only known after App construction (the
    # composed ask_user/shell/gate routing); the holder closes that gap.
    holder: dict[str, Callable[[ActionRequest], ActionResult]] = {}
    toolbox = ScreenToolbox(perception, registry, lambda req: holder["execute"](req))
    app = App(
        Settings(),
        headless=True,
        log_dir=log_dir,
        pack=pack,
        perception=perception,
        action_layer=layer,
        gate=gate,
        ask_channel=ask,
        planner=LLMPlanner(provider, toolbox=toolbox),
    )
    holder["execute"] = app._execute
    app.tts = tts  # ack announcements + the spoken result land on the caller's recorder
    return _RunWiring(app=app, layer=layer, provider=provider)


def _canned_states(snapshots: list[PerceptionSnapshot]) -> list[CannedState]:
    states: list[CannedState] = []
    for snapshot in snapshots:
        window = snapshot.active_window
        if window is None:
            raise ScenarioError("every canned snapshot needs an active_window")
        states.append(
            CannedState(
                window=window,
                elements=snapshot.elements,
                screenshot=snapshot.screenshot,
            )
        )
    return states


def _build_vision(
    provider: ScenarioProvider, source: CannedUiaSource
) -> VisionPerceiver | None:
    """The D5 wiring: vision only with a vision-capable (scripted) provider.

    The capture seam reads the CURRENT canned state's screenshot; the
    foreground rect is the screenshot's own pixel rect, so SoM tile numbers map
    1:1 onto image coordinates (deterministic canned geometry).
    """
    if provider.vision_reply is None:
        return None
    for state in source.states:
        if state.screenshot is None:
            raise ScenarioError(
                "vision scenarios need a screenshot on every canned state "
                f"(missing for window {state.window.title!r})"
            )
    first_shot = source.states[0].screenshot
    if first_shot is None:  # unreachable after the loop; narrows for the checker
        raise ScenarioError("vision scenarios need a screenshot on the first state")
    with Image.open(first_shot) as image:
        width, height = image.size
    rect = Rect(x=0, y=0, w=width, h=height)

    def capture(_rect: Rect) -> bytes:
        shot = source.state.screenshot
        if shot is None:  # validated above; keep the seam total
            raise ScenarioError("vision capture: canned state has no screenshot")
        return shot.read_bytes()

    return VisionPerceiver(provider, capture=capture, foreground_rect=lambda: rect)


def _resolver(registry: ElementRegistry) -> Callable[[str | None], Element | None]:
    """Gate seam: ref → the element the planner saw (element-state escalation)."""

    def resolve(ref: str | None) -> Element | None:
        if ref is None:
            return None
        handle = registry.resolve(ref)
        return handle.element if handle is not None else None

    return resolve


def _ask_answers(spec: ScenarioSpec) -> list[str]:
    """The scripted user replies, in script order (validated at load time)."""
    answers: list[str] = []
    for entry in spec.script:
        if entry.tool == "ask_user":
            answer = entry.args.get("answer")
            if isinstance(answer, str):
                answers.append(answer)
    return answers


def _executed_trace(
    emitted: list[LLMToolCall], executed_requests: list[ActionRequest]
) -> list[str]:
    """The executed steps in order (the YAML ``trace`` format).

    Non-act tool steps always execute (perception reads, the terminal finish),
    so they are recorded at emission. An ``act`` is recorded ONLY when its
    request actually reached the action layer — the orchestrator executes acts
    in emission order and each produces at most one layer request, so ordered
    matching by kind is exact. Refused/cancelled acts stay off the trace: they
    are not executed actions.
    """
    trace: list[str] = []
    next_request = 0
    for call in emitted:
        if call.name == "act":
            kind = call.arguments.get("kind")
            if (
                isinstance(kind, str)
                and next_request < len(executed_requests)
                and executed_requests[next_request].kind == kind
            ):
                trace.append(f"act:{kind}")
                next_request += 1
            continue
        trace.append(call.name)
    return trace


def _assertions_hold(spec: ScenarioSpec, trace: list[str], spoken: str) -> bool:
    if trace != spec.trace:
        return False
    lowered = spoken.lower()
    return all(needle.lower() in lowered for needle in spec.spoken_contains)


# -- fixture-app mode (M3 Task 7): the REAL stack against a REAL window -----------
#
# run_fixture launches the scenario's own fixture app (FixtureApp), pins its
# state, and runs the production loop — real ScreenPerception (UIA walk of the
# fixture window), real ActionLayer with UIA patterns + SendInput, real
# SafetyGate — driven by the same scripted fake provider as scripted mode. Zero
# real provider calls are possible. Assertions read UIA TREE STATE: the YAML's
# ``assert_elements`` entries are matched against a RAW UIA walk of the window
# (all controls — results/status text are assertable, not just interactables),
# while ``element_count`` reports the interactable count the planner's lens
# (the reader) sees — the SP1 fallback-threshold measure. The run closes its
# own window (the FixtureApp context) — new-windows-only, SP1.


class _RecordingActionLayer:
    """Wraps the REAL ActionLayer and records requests that EXECUTED (ok=True),
    so the executed-trace semantics match scripted mode: gate refusals, unknown
    refs and stale refs are attempts, not executions, and stay off the trace."""

    def __init__(self, real: ActionLayer) -> None:
        self._real = real
        self.requests: list[ActionRequest] = []

    def execute(
        self, req: ActionRequest, snapshot: PerceptionSnapshot | None = None
    ) -> ActionResult:
        result = self._real.execute(req, snapshot)
        if result.ok:
            self.requests.append(req)
        return result


def run_fixture(
    spec: ScenarioSpec,
    *,
    provider: ScenarioProvider,
    pack: WindowsPack,
    tts: FakeTTS,
    log_dir: Path | None = None,
    fixture_timeout: float = 15.0,
) -> ScenarioReport:
    """Run one fixture-app scenario against the real desktop stack.

    Launches ``fixture:<alias>`` via :class:`FixtureApp` (``launch`` opens a
    specific target instead of the bare alias), focuses it (D6: our own
    foreground window — also wakes lazy UIA trees such as Chromium's), waits
    for the ``pin`` element (app state pinned BEFORE acting), then submits the
    instruction. Fails cleanly (ScenarioError) when the fixture app cannot be
    launched or its window never appears.
    """
    if not spec.mode.startswith("fixture:"):
        raise ScenarioError(
            f"run_fixture runs mode 'fixture:<alias>' only, got {spec.mode!r}"
        )
    alias = spec.mode.split(":", 1)[1]
    try:
        with FixtureApp(
            alias, pack=pack, timeout=fixture_timeout, launch=spec.launch
        ) as fx:
            hwnd = fx.hwnd
            if hwnd is None:  # __enter__ guarantees it; narrows for the checker
                raise FixtureError("fixture window vanished right after launch")
            focus_window(hwnd)
            if spec.pin is not None:
                fx.wait_element(hwnd, spec.pin.role, spec.pin.name_contains, timeout=20.0)
            wiring = _build_fixture_run(spec, provider, pack, tts, log_dir, hwnd)
            wiring.app.start()
            try:
                handle = asyncio.run(wiring.app.submit_text(spec.instruction))
            finally:
                wiring.app.stop()
            result = handle.result
            trace = _executed_trace(provider.emitted, wiring.layer.requests)
            spoken = result.summary_to_speak if result is not None else ""
            element_count = len(_fresh_reader_walk(hwnd))
            ok = (
                result is not None
                and _assertions_hold(spec, trace, spoken)
                and _assert_elements_ok(hwnd, spec.assert_elements)
            )
            return ScenarioReport(
                ok=ok, trace=trace, spoken=spoken, element_count=element_count
            )
    except FixtureError as exc:
        raise ScenarioError(f"fixture app failed: {exc}") from exc


def _build_fixture_run(
    spec: ScenarioSpec,
    provider: ScenarioProvider,
    pack: WindowsPack,
    tts: FakeTTS,
    log_dir: Path | None,
    hwnd: int,
) -> _RunWiring:
    """Compose the headless App around the REAL perception/action/gate stack.

    Perception's foreground seam is pinned to the fixture window (the task owns
    it, D6 — scenario determinism); vision stays unwired (the fixture corpus is
    UIA-first; a sparse tree then simply returns fewer elements, never a VLM
    call — the paint scenario proves that on the provider).
    """
    registry = ElementRegistry()
    reader = UIAReader(source=UiaAutomationSource())
    perception = ScreenPerception(
        reader=reader,
        registry=registry,
        foreground=lambda: WindowInfo(hwnd=hwnd, title=window_title(hwnd), app=""),
        vision=None,
    )
    layer = _RecordingActionLayer(
        ActionLayer(
            perception=perception,
            registry=registry,
            patterns=UiaAutomationPatterns(),
            inputs=InputSynthesizer(),
            focus_window=focus_window,
        )
    )
    ask = ScriptedAskChannel(_ask_answers(spec))
    gate = SafetyGate(
        policy=RiskPolicy(),
        confirm=ConfirmFlow(ask=ask.ask, fallback=None),  # headless: ambiguity refuses
        announce=lambda text: tts.say(SpeakRequest(text=text, priority="ack")),
        kill_probe=lambda: False,
        resolve=_resolver(registry),
    )
    holder: dict[str, Callable[[ActionRequest], ActionResult]] = {}
    toolbox = ScreenToolbox(perception, registry, lambda req: holder["execute"](req))
    app = App(
        Settings(),
        headless=True,
        log_dir=log_dir,
        pack=pack,
        perception=perception,
        action_layer=layer,
        gate=gate,
        ask_channel=ask,
        planner=LLMPlanner(provider, toolbox=toolbox),
    )
    holder["execute"] = app._execute
    app.tts = tts  # ack announcements + the spoken result land on the caller's recorder
    return _RunWiring(app=app, layer=layer, provider=provider)


def _fresh_reader_walk(hwnd: int) -> list[Element]:
    """Fresh interactable walk of the fixture window (the planner's lens)."""
    reader = UIAReader(source=UiaAutomationSource())
    reader.invalidate(hwnd)
    try:
        return reader.read(hwnd)
    except PerceptionError:
        return []


@dataclass(frozen=True)
class _RawUiaNode:
    """One node of the raw UIA walk: role/name plus optional pattern reads."""

    role: str
    name: str
    value: str
    text: str


_RAW_WALK_DEPTH: Final = 30
_RAW_TEXT_LIMIT: Final = 8000


def _raw_uia_nodes(hwnd: int, *, with_values: bool, with_text: bool) -> list[_RawUiaNode]:
    """Walk EVERY control under the window (not just interactables).

    Roles are the control type names minus the ``Control`` suffix — the same
    convention the reader's roles use (Edit, Button, Document, Text, ...).
    Value/TextPattern reads are guarded: nodes without the pattern contribute
    empty strings. Only enabled when an assertion needs them (COM cost).
    """
    import uiautomation as uia  # Windows-only; untyped upstream

    root = uia.ControlFromHandle(hwnd)
    nodes: list[_RawUiaNode] = []
    stack: list[tuple[Any, int]] = [(root, 0)] if root is not None else []
    while stack:
        node, depth = stack.pop()
        try:
            name = str(node.Name or "")
        except Exception:
            name = ""
        value = ""
        if with_values:
            try:
                value = str(node.GetValuePattern().Value or "")
            except Exception:
                value = ""
        text = ""
        if with_text:
            try:
                text = str(node.GetTextPattern().DocumentRange.GetText(_RAW_TEXT_LIMIT) or "")
            except Exception:
                text = ""
        role = str(getattr(node, "ControlTypeName", "")).removesuffix("Control")
        nodes.append(_RawUiaNode(role=role, name=name, value=value, text=text))
        if depth < _RAW_WALK_DEPTH:
            try:
                stack.extend((child, depth + 1) for child in node.GetChildren())
            except Exception:
                pass
    return nodes


def _assert_elements_ok(hwnd: int, assertions: list[ElementAssertion]) -> bool:
    """Every assertion must match at least one raw node (case-insensitive)."""
    if not assertions:
        return True
    nodes = _raw_uia_nodes(
        hwnd,
        with_values=any(a.value_contains is not None for a in assertions),
        with_text=any(a.text_contains is not None for a in assertions),
    )
    lowered = [
        (node.role.lower(), node.name.lower(), node.value.lower(), node.text.lower())
        for node in nodes
    ]
    for assertion in assertions:
        role = assertion.role.lower()
        name_needle = assertion.name_contains.lower()
        matches = [
            node for node in lowered if node[0] == role and name_needle in node[1]
        ]
        if not matches:
            return False
        if assertion.value_contains is not None and not any(
            assertion.value_contains.lower() in node[2] for node in matches
        ):
            return False
        if assertion.text_contains is not None and not any(
            assertion.text_contains.lower() in node[3] for node in matches
        ):
            return False
    return True
