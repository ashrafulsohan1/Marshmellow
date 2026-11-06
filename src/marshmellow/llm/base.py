"""LLM provider interface — cloud (BYOK) and local adapters behind one contract."""

from __future__ import annotations

from typing import Protocol

from marshmellow.core.contracts import LLMRequest, LLMResponse, ProviderCaps


class LLMProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCaps: ...

    async def complete(self, req: LLMRequest) -> LLMResponse: ...


class ProviderUnavailableError(RuntimeError):
    """Every configured provider failed after retries.

    The planner catches this and speaks the distinct unavailable summary,
    not the generic failure line (§3.4).
    """
