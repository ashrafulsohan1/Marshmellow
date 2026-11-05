"""RiskPolicy tests — the authoritative L0–L3 classification (§3.13, Review Focus 2)."""

import pytest

from marshmellow.core.contracts import ActionKind, ActionRequest, Element, Rect, RiskLevel
from marshmellow.safety.policy import BATCH_DELETE_THRESHOLD, SENSITIVE_NAME_RE, RiskPolicy


def req(kind: ActionKind, **payload: object) -> ActionRequest:
    """A request whose planner-declared risk is always the lowest — the gate
    must recompute from kind + element state regardless (Review Focus 2)."""
    return ActionRequest(kind=kind, payload=dict(payload), rationale="test", risk="L0")


def element(name: str = "OK button", *, password: bool = False) -> Element:
    return Element(
        ref="e1", role="button", name=name, bbox=Rect(x=0, y=0, w=10, h=10),
        password=password,
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        # L0 observe/no-op kinds.
        ("wait", "L0"),
        ("ask_user", "L0"),
        ("done", "L0"),
        ("fail", "L0"),
        # L1 navigate/open kinds.
        ("uia_invoke", "L1"),
        ("click", "L1"),
        ("double_click", "L1"),
        ("right_click", "L1"),
        ("focus_window", "L1"),
        ("shell", "L1"),
        # L2 mutate-content kinds.
        ("type_text", "L2"),
        ("uia_set_value", "L2"),
        ("uia_select", "L2"),
        ("uia_toggle", "L2"),
        ("scroll", "L2"),
        ("hotkey", "L2"),
    ],
)
def test_kind_table(kind: ActionKind, expected: RiskLevel) -> None:
    assert RiskPolicy().classify(req(kind), None) == expected


def test_sensitive_name_regex_covers_the_spec_terms() -> None:
    for term in (
        "delete", "trash", "empty", "send", "pay", "purchase", "checkout",
        "password", "sign in", "security", "account",
    ):
        assert SENSITIVE_NAME_RE.search(term), term
    assert SENSITIVE_NAME_RE.search("Delete All Items")
    assert SENSITIVE_NAME_RE.search("SIGN IN")
    assert not SENSITIVE_NAME_RE.search("OK button")
    assert not SENSITIVE_NAME_RE.search("Play")


@pytest.mark.parametrize(
    "name",
    ["Delete File", "Empty Trash", "Confirm payment", "Sign in", "Account settings",
     "Checkout"],
)
def test_sensitive_element_name_escalates_to_l3(name: str) -> None:
    assert RiskPolicy().classify(req("uia_invoke"), element(name)) == "L3"


def test_password_element_is_l3_even_with_declared_l0() -> None:
    """Review Focus 2: the planner's declared risk is advisory — a UIA IsPassword
    field is L3 always, in every preset and override (§3.13)."""
    policy = RiskPolicy()
    password = element("Password", password=True)
    assert policy.classify(req("type_text", text="hunter2"), password) == "L3"
    assert policy.classify(req("uia_set_value", text="x"), element("PIN", password=True)) == "L3"
    # Conservative: any kind whose target IS a password element classifies L3.
    assert policy.classify(req("wait"), element("Password", password=True)) == "L3"


def test_batch_delete_over_threshold_confirms() -> None:
    assert BATCH_DELETE_THRESHOLD == 5
    policy = RiskPolicy()
    six_items = req("uia_invoke", delete_items=list(range(6)))
    assert policy.classify(six_items, element("Items")) == "L3"
    assert policy.classify(req("uia_invoke", delete_count=6), None) == "L3"


def test_batch_delete_at_threshold_stays_mutate_level() -> None:
    """§3.13: destructive batch > 5 always confirms — at ≤ 5 it is still at
    least L2 (a destructive batch is never treated as read-only), but no L3.
    The element name here is deliberately non-sensitive so only the batch
    rule is under test ("Delete" as a NAME would escalate on its own)."""
    policy = RiskPolicy()
    five_items = req("uia_invoke", delete_items=list(range(5)))
    assert policy.classify(five_items, element("Items")) == "L2"
    assert policy.classify(req("uia_invoke", delete_count=5), None) == "L2"
    assert policy.classify(req("uia_invoke"), element("Items")) == "L1"  # no batch payload


def test_strict_preset_escalates_text_entry() -> None:
    strict = RiskPolicy(preset="strict")
    assert strict.classify(req("type_text", text="hello"), None) == "L3"
    assert strict.classify(req("uia_set_value", text="hello"), None) == "L3"
    assert strict.classify(req("click"), None) == "L1"  # navigation stays L1
    assert RiskPolicy().classify(req("type_text", text="hello"), None) == "L2"


def test_overrides_escalate_by_kind() -> None:
    policy = RiskPolicy(overrides={"uia_invoke": "L2"})
    assert policy.classify(req("uia_invoke"), None) == "L2"


def test_override_downward_is_rejected() -> None:
    policy = RiskPolicy(overrides={"click": "L0", "type_text": "L1"})
    assert policy.classify(req("click"), None) == "L1"
    assert policy.classify(req("type_text"), None) == "L2"


def test_l3_floor_holds_over_downward_override() -> None:
    """§3.13: the L3 floor is never lowered — overrides may only escalate."""
    policy = RiskPolicy(overrides={"type_text": "L1"})
    assert policy.classify(req("type_text", text="x"), element("Password", password=True)) == "L3"
    assert policy.classify(req("uia_invoke", delete_count=6), None) == "L3"


def test_unknown_preset_is_rejected() -> None:
    with pytest.raises(ValueError, match="preset"):
        RiskPolicy(preset="bogus")  # type: ignore[arg-type]
