"""RiskPolicy — the authoritative L0–L3 risk classification (01-architecture §3.13).

The planner's declared ``risk`` on an ActionRequest is advisory only:
:class:`RiskPolicy.classify` recomputes from the action kind plus the target
element's live state, so a mislabeled destructive act cannot slip through at
L0 (M3 Review Focus 2).

Escalations applied after the kind table:

- the target element is a UIA password field (``Element.password``) → **L3**,
  in every preset and override;
- the element name matches :data:`SENSITIVE_NAME_RE` → **L3**;
- a destructive batch larger than :data:`BATCH_DELETE_THRESHOLD` → **L3**
  (at or below the threshold a destructive batch is still at least L2);
- the ``strict`` preset escalates text entry (``type_text``/``uia_set_value``)
  to L3.

Config overrides (``[safety].overrides``, keyed by kind) may only ESCALATE:
they are applied as ``max(table_level, override)``, so the L3 floor is never
lowered (§3.13).
"""

from __future__ import annotations

import re
from typing import Final, Literal

from marshmellow.core.contracts import ActionRequest, Element, RiskLevel

Preset = Literal["relaxed", "standard", "strict"]

# §3.13 sensitive-name terms, case-insensitive substring match.
SENSITIVE_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"delete|trash|empty|send|pay|purchase|checkout|password|sign in|security|account",
    re.IGNORECASE,
)

# §3.13: destructive batch ops (delete > 5 items) always confirm.
BATCH_DELETE_THRESHOLD: Final = 5

_LEVEL_ORDER: Final[dict[str, int]] = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}

# The kind table (§3.13): L0 observe/no-op, L1 navigate/open, L2 mutate content.
# Kinds beyond the spec's examples: click variants follow `click` (L1), hotkey
# mutates like typed input (L2), and planner-internal no-ops are L0. Anything
# unknown defaults to L2 — an unmapped kind is treated as a mutation, never as safe.
_KIND_LEVELS: Final[dict[str, RiskLevel]] = {
    "wait": "L0",
    "ask_user": "L0",
    "done": "L0",
    "fail": "L0",
    "uia_invoke": "L1",
    "click": "L1",
    "double_click": "L1",
    "right_click": "L1",
    "focus_window": "L1",
    "shell": "L1",
    "type_text": "L2",
    "uia_set_value": "L2",
    "uia_select": "L2",
    "uia_toggle": "L2",
    "scroll": "L2",
    "hotkey": "L2",
}

# The strict preset escalates text entry to L3 (a "strict" user confirms typing).
_STRICT_ESCALATED: Final[frozenset[str]] = frozenset({"type_text", "uia_set_value"})

_BATCH_KEYS: Final = ("delete_items", "delete_count")


def _max_level(a: RiskLevel, b: RiskLevel) -> RiskLevel:
    return a if _LEVEL_ORDER[a] >= _LEVEL_ORDER[b] else b


def _delete_batch_size(req: ActionRequest) -> int | None:
    """The destructive batch size carried in the payload, if any.

    Two payload shapes are recognized: ``delete_items`` (a list/tuple of the
    items the planner intends to delete) and ``delete_count`` (a plain count).
    """
    items = req.payload.get("delete_items")
    if isinstance(items, (list, tuple)):
        return len(items)
    count = req.payload.get("delete_count")
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    return None


class RiskPolicy:
    """Pure, deterministic risk classification — no I/O, safe to re-run."""

    def __init__(
        self, preset: Preset = "standard", overrides: dict[str, RiskLevel] | None = None
    ) -> None:
        if preset not in ("relaxed", "standard", "strict"):
            raise ValueError(f"unknown safety preset {preset!r}")
        self.preset: Preset = preset
        self.overrides: dict[str, RiskLevel] = dict(overrides or {})

    def classify(self, req: ActionRequest, element: Element | None) -> RiskLevel:
        """The risk level for one request against its (optional) target element.

        The declared ``req.risk`` is deliberately ignored — the recomputed
        level is the authoritative one (§3.13).
        """
        level: RiskLevel = _KIND_LEVELS.get(req.kind, "L2")

        # Element-state escalations first: a password field or a sensitive
        # target is L3 regardless of kind, preset or override.
        if element is not None and (element.password or SENSITIVE_NAME_RE.search(element.name)):
            return "L3"

        batch = _delete_batch_size(req)
        if batch is not None:
            if batch > BATCH_DELETE_THRESHOLD:
                return "L3"
            level = _max_level(level, "L2")  # a small destructive batch still mutates

        if self.preset == "strict" and req.kind in _STRICT_ESCALATED:
            level = "L3"

        # Overrides only escalate (max), so the L3 floor is never lowered.
        override = self.overrides.get(req.kind)
        if override is not None:
            level = _max_level(level, override)
        return level
