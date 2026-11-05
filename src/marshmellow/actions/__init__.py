"""ActionLayer: execute ActionRequests — UIA patterns -> keyboard -> SendInput (§3.7)."""

from __future__ import annotations

from marshmellow.actions.executor import ActionLayer
from marshmellow.actions.input_synthesis import InputSynthesizer
from marshmellow.actions.uia_actions import UiaAutomationPatterns, UiaPatternInvoker

__all__ = ["ActionLayer", "InputSynthesizer", "UiaAutomationPatterns", "UiaPatternInvoker"]
