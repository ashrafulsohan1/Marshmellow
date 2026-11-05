"""SafetyGate tests — check/guard flow, L1 announce, L3 confirmation, kill probe."""

import logging
import threading
from collections.abc import Callable

import pytest

from marshmellow.core.contracts import ActionRequest, ActionResult, Element, Rect
from marshmellow.safety.confirm import ConfirmFlow
from marshmellow.safety.gate import SafetyDecision, SafetyGate
from marshmellow.safety.policy import RiskPolicy


class ScriptedConfirm:
    """Confirmer seam fake: records the questions, replays scripted verdicts."""

    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.questions: list[str] = []

    def confirm(self, question: str) -> bool:
        self.questions.append(question)
        return self.answers.pop(0)


def scripted_ask(*answers: str) -> tuple[list[str], Callable[[str], str]]:
    questions: list[str] = []
    pending = list(answers)

    def ask(question: str) -> str:
        questions.append(question)
        return pending.pop(0) if pending else ""

    return questions, ask


def req(kind: str, *, ref: str | None = None, **payload: object) -> ActionRequest:
    """A request whose planner-declared risk is always "L0" — the gate must
    recompute from kind + element state regardless (Review Focus 2)."""
    return ActionRequest(
        kind=kind,  # type: ignore[arg-type]
        target_ref=ref,
        payload=dict(payload),
        rationale="open the file",
        risk="L0",
    )


def element(name: str = "Delete", *, password: bool = False) -> Element:
    return Element(
        ref="e1", role="button", name=name, bbox=Rect(x=0, y=0, w=10, h=10),
        password=password,
    )


@pytest.fixture()
def announced() -> list[str]:
    return []


def make_gate(
    confirm: object,
    announced: list[str],
    *,
    preset: str = "standard",
    kill: threading.Event | None = None,
    resolve: Callable[[str | None], Element | None] | None = None,
    execute: Callable[[ActionRequest], ActionResult] | None = None,
) -> tuple[SafetyGate, Callable[[ActionRequest], ActionResult], list[ActionRequest]]:
    """A gate whose guarded executor is under test; `calls` records every
    request that actually reached the executor. L3-only tests opt into the
    strict preset (kind-level L3 without an element)."""
    calls: list[ActionRequest] = []

    def default_execute(r: ActionRequest) -> ActionResult:
        calls.append(r)
        return ActionResult(ok=True, observation="ran")

    gate = SafetyGate(
        RiskPolicy(preset=preset),  # type: ignore[arg-type]
        confirm,  # type: ignore[arg-type]
        announced.append,
        kill_probe=(kill.is_set if kill is not None else None),
        resolve=resolve,
    )
    return gate, gate.guard(execute or default_execute), calls


def test_l0_l1_l2_flow_through_without_confirmation(announced: list[str]) -> None:
    confirm = ScriptedConfirm()
    _gate, execute, calls = make_gate(confirm, announced)
    for kind in ("wait", "uia_invoke", "type_text"):
        result = execute(req(kind))
        assert result.ok is True and result.observation == "ran"
    assert len(calls) == 3
    assert confirm.questions == [], "L0/L1/L2 never confirm"
    assert announced == ["open the file"], "exactly the L1 (uia_invoke) is announced"


def test_l1_is_announced_before_execution() -> None:
    """The announce lands on the shared log BEFORE the executor runs."""
    order: list[str] = []

    def execute(r: ActionRequest) -> ActionResult:
        order.append("executed")
        return ActionResult(ok=True, observation="ran")

    _gate, guarded, calls = make_gate(ScriptedConfirm(), order, execute=execute)
    result = guarded(req("uia_invoke", ref="e3"))
    assert result.ok is True
    assert order == ["open the file", "executed"], "L1 announce precedes execution"


def test_l3_confirm_yes_executes(announced: list[str]) -> None:
    confirm = ScriptedConfirm(True)
    _gate, execute, calls = make_gate(confirm, announced, preset="strict")
    result = execute(req("type_text", text="hello"))
    assert result.ok is True
    assert len(calls) == 1
    assert len(confirm.questions) == 1
    assert "type text" in confirm.questions[0] and "confirm" in confirm.questions[0].lower()
    assert not announced, "L3 goes through confirmation, not the L1 announce path"


def test_l3_confirm_no_refuses_as_action_result(announced: list[str]) -> None:
    _gate, execute, calls = make_gate(ScriptedConfirm(False), announced, preset="strict")
    result = execute(req("type_text", text="hello"))
    assert result.ok is False
    assert result.error == "refused"
    assert "refused" in result.observation.lower()
    assert not calls, "a refused action must never reach the executor"


def test_l3_ambiguous_voice_refuses_fail_closed(announced: list[str]) -> None:
    """Real ConfirmFlow: "uh" → re-ask → "uh" → no fallback → refuse."""
    questions, ask = scripted_ask("uh", "uh")
    _gate, execute, calls = make_gate(ConfirmFlow(ask=ask), announced, preset="strict")
    result = execute(req("type_text", text="hello"))
    assert result.ok is False and result.error == "refused"
    assert not calls
    assert questions == ["About to type text on no target — open the file — confirm?"] * 2


def test_kill_probe_set_refuses_without_executing(announced: list[str]) -> None:
    kill = threading.Event()
    gate, execute, calls = make_gate(ScriptedConfirm(True), announced, kill=kill)
    kill.set()
    result = execute(req("click"))
    assert result.ok is False
    assert result.error == "cancelled"
    assert not calls
    assert not announced
    decision = gate.check(req("click"))
    assert decision == SafetyDecision(
        allowed=False, needs_confirmation=False, reason="kill switch engaged"
    )


def test_kill_during_confirmation_refuses_even_after_yes(announced: list[str]) -> None:
    """The second net: a kill between "yes" and execution still refuses."""
    kill = threading.Event()

    class KillAfterYes:
        def confirm(self, question: str) -> bool:
            kill.set()  # the user says yes while the kill lands
            return True

    _gate, execute, calls = make_gate(KillAfterYes(), announced, preset="strict", kill=kill)
    result = execute(req("type_text", text="hello"))
    assert result.ok is False and result.error == "cancelled"
    assert not calls


def test_gate_resolves_element_and_overrides_declared_risk(announced: list[str]) -> None:
    """Task 5 DoD bypass attempt: declared risk "L0" on a password element must
    still be L3-confirmed — the gate re-classifies from the element state."""
    confirm = ScriptedConfirm(True)
    password_field = element("Password", password=True)
    gate, execute, calls = make_gate(
        confirm,
        announced,
        resolve=lambda ref: password_field if ref == "e1" else None,
    )
    decision = gate.check(req("type_text", ref="e1", text="secret"), None)
    assert decision.allowed and decision.needs_confirmation, decision.reason

    result = execute(req("type_text", ref="e1", text="secret"))
    assert result.ok is True and len(calls) == 1
    assert "Password" in confirm.questions[0]


def test_every_decision_is_logged_with_its_rationale(
    announced: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    gate, _execute, _calls = make_gate(ScriptedConfirm(False), announced)
    with caplog.at_level(logging.INFO, logger="marshmellow.safety.gate"):
        gate.check(req("uia_invoke", ref="e3"))
    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        "uia_invoke" in m and "allowed=True" in m and "L0" in m  # declared risk recorded
        for m in messages
    ), messages
