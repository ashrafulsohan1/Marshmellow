import pytest
from pydantic import ValidationError

from marshmellow.core.contracts import (
    ActionRequest,
    Element,
    SpeakRequest,
    TaskSpec,
)


def test_element_requires_core_fields() -> None:
    with pytest.raises(ValidationError):
        Element(ref="e1")  # role/name/bbox missing
    el = Element(
        ref="e1", role="ButtonControl", name="Copy",
        bbox={"x": 0, "y": 0, "w": 10, "h": 10},
        enabled=True, actions={"invoke"}, source="uia",
    )
    assert el.ref == "e1"
    assert el.password is False
    assert el.actions == {"invoke"}


def test_action_request_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        ActionRequest(kind="teleport", target_ref=None, rationale="x", risk="L0")
    req = ActionRequest(
        kind="click", target_ref="e3", payload={"button": "left"},
        rationale="open link", risk="L1",
    )
    assert req.kind == "click"


def test_task_spec_default_mode_is_foreground() -> None:
    spec = TaskSpec(id="t1", instruction="open notepad")
    assert spec.mode == "foreground"


def test_speak_request_defaults() -> None:
    s = SpeakRequest(text="hi")
    assert s.priority == "result"
    assert s.interrupt is False
