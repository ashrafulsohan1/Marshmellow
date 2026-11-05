"""SafetyGate — every ActionRequest passes here before execution (§3.13).

L0/L2 run autonomously, L1 is announced first (the injected ``announce`` lands
on the TTS ack-priority queue), L3 must pass :class:`~marshmellow.safety.confirm.ConfirmFlow`
(spoken yes/no with the tray fallback). A refusal is an ``ActionResult(ok=False)``
the planner can observe and react to — never an exception. Every decision and
its rationale are logged locally (§3.13).

The kill probe (wired to ``TaskManager.current_kill`` at composition) is
checked before classification AND again after a "yes" — a kill between the
confirmation and the execution still refuses. M2's pre-action kill check in
the executor seam remains the second net.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from marshmellow.core.contracts import ActionRequest, ActionResult, Element
from marshmellow.safety.policy import RiskPolicy

logger = logging.getLogger(__name__)

Executor = Callable[[ActionRequest], ActionResult]
Announcer = Callable[[str], None]
KillProbe = Callable[[], bool]
ElementResolver = Callable[[str | None], Element | None]


class Confirmer(Protocol):
    """The L3 confirmation seam (ConfirmFlow satisfies it)."""

    def confirm(self, question: str) -> bool: ...


@dataclass(frozen=True)
class SafetyDecision:
    """One gate decision: allowed to run at all, and whether it needs a
    spoken confirmation first. `reason` is the logged/spoken rationale."""

    allowed: bool
    needs_confirmation: bool
    reason: str


def _describe(req: ActionRequest, element: Element | None) -> str:
    if element is not None and element.name:
        return f"{element.name!r}"
    if req.target_ref:
        return req.target_ref
    return "no target"


def _question(req: ActionRequest, element: Element | None) -> str:
    kind_words = req.kind.replace("_", " ")
    return f"About to {kind_words} on {_describe(req, element)} — {req.rationale} — confirm?"


class SafetyGate:
    """Classifies one request (authoritatively, via RiskPolicy) and enforces
    the announce/confirm/refuse policy around an executor."""

    def __init__(
        self,
        policy: RiskPolicy,
        confirm: Confirmer,
        announce: Announcer,
        kill_probe: KillProbe | None = None,
        resolve: ElementResolver | None = None,
    ) -> None:
        self._policy = policy
        self._confirm = confirm
        self._announce = announce
        # `resolve` maps target_ref → the element the planner saw (the registry
        # seam): element-state escalations (password, sensitive name) work at
        # the guard seam, where only the request is in hand.
        self._resolve = resolve
        self._killed: KillProbe = kill_probe or (lambda: False)

    def check(self, req: ActionRequest, element: Element | None = None) -> SafetyDecision:
        decision, _level = self._decide(req, element)
        return decision

    def guard(self, execute: Executor) -> Executor:
        """Wrap an executor so every request is gated (§3.13)."""

        def gated(req: ActionRequest) -> ActionResult:
            element = self._resolve(req.target_ref) if self._resolve is not None else None
            decision, level = self._decide(req, element)
            if not decision.allowed:
                # The only not-allowed decision today is the kill probe — keep
                # the M2 executor-seam vocabulary ("cancelled") for it, so the
                # planner can distinguish a kill from a declined confirmation.
                killed = self._killed()
                if killed:
                    return ActionResult(
                        ok=False,
                        observation="cancelled: kill switch engaged",
                        error="cancelled",
                    )
                return ActionResult(
                    ok=False, observation=f"refused: {decision.reason}", error="refused"
                )
            if decision.needs_confirmation:
                if not self._confirm.confirm(_question(req, element)):
                    logger.info(
                        "safety: refused %s on %s — user did not confirm",
                        req.kind,
                        _describe(req, element),
                    )
                    return ActionResult(
                        ok=False,
                        observation=(
                            f"refused: user did not confirm {req.kind} "
                            f"on {_describe(req, element)}"
                        ),
                        error="refused",
                    )
                if self._killed():
                    # Second net: the kill landed between "yes" and execution.
                    return ActionResult(
                        ok=False,
                        observation="cancelled: kill switch engaged during confirmation",
                        error="cancelled",
                    )
            elif level == "L1":
                self._announce(req.rationale)
            return execute(req)

        return gated

    # -- decision ----------------------------------------------------------------

    def _decide(
        self, req: ActionRequest, element: Element | None
    ) -> tuple[SafetyDecision, str]:
        """Classify + decide; the returned level is reused by guard() for the
        L1 announce path. classify() is pure, so recomputation is safe.

        When the caller passes no element, the injected resolver fills it in
        from `target_ref` — element-state escalations (password, sensitive
        name) apply at every seam, guard and check alike."""
        if element is None and self._resolve is not None:
            element = self._resolve(req.target_ref)
        level = self._policy.classify(req, element)
        target = _describe(req, element)
        if self._killed():
            decision = SafetyDecision(False, False, "kill switch engaged")
        elif level == "L3":
            decision = SafetyDecision(
                True, True, f"{req.kind} on {target} is L3 — spoken confirmation required"
            )
        else:
            decision = SafetyDecision(
                True, False, f"{req.kind} on {target} is {level} — autonomous"
            )
        # §3.13: every decision + rationale logged (declared vs computed risk on record).
        logger.info(
            "safety decision: %s on %s declared=%s → allowed=%s confirm=%s (%s)",
            req.kind,
            target,
            req.risk,
            decision.allowed,
            decision.needs_confirmation,
            decision.reason,
        )
        return decision, level
