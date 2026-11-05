"""Safety/consent layer (01-architecture §3.13).

Every ActionRequest passes the SafetyGate before execution; L3 needs an
explicit spoken (or tray) confirmation; ambiguity fails closed.
"""

from marshmellow.safety.confirm import ConfirmFlow
from marshmellow.safety.gate import SafetyDecision, SafetyGate
from marshmellow.safety.policy import (
    BATCH_DELETE_THRESHOLD,
    SENSITIVE_NAME_RE,
    RiskPolicy,
)

__all__ = [
    "BATCH_DELETE_THRESHOLD",
    "SENSITIVE_NAME_RE",
    "ConfirmFlow",
    "RiskPolicy",
    "SafetyDecision",
    "SafetyGate",
]
