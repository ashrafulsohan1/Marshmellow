"""Provider resilience — silent retries + the primary→fallback chain (§3.4).

Per provider: a transient failure (`default_retryable`) is retried up to
`max_attempts` total tries, sleeping `backoff_s` between tries; anything
non-retryable fails fast to the next provider. Exhausted everywhere →
`ProviderUnavailableError`. Retries are SILENT by design — a spoken word per
hiccup is noise; the planner's unavailable summary is the UX.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

import httpx
import openai

from marshmellow.core.contracts import LLMRequest, LLMResponse, ProviderCaps
from marshmellow.llm.base import LLMProvider, ProviderUnavailableError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    """Retry cadence per provider: `max_attempts` total tries (1 retry by default)."""

    max_attempts: int = 2
    backoff_s: float = 1.0


# Frozen-dataclass singleton: the `retry` default (a shared immutable instance,
# not a per-call construction — ruff B008).
DEFAULT_RETRY: Final = RetryPolicy()


def default_retryable(exc: Exception) -> bool:
    """Transient failures worth a silent retry.

    httpx transport errors and 429/5xx, plus the openai SDK's dialects of the
    same (APIConnectionError — APITimeoutError subclasses it — RateLimitError,
    InternalServerError). Auth- and request-shape 4xx are NOT retryable: the
    key or request is wrong, and retrying only delays the (better) fallback or
    the spoken error.
    """
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    if isinstance(exc, openai.APIConnectionError):
        return True
    return isinstance(exc, (openai.RateLimitError, openai.InternalServerError))


class ProviderChain:
    """An `LLMProvider` over a primary and an optional fallback (§3.4).

    `complete` tries each provider with silent retries (`RetryPolicy`), then
    the next; exhaustion everywhere raises `ProviderUnavailableError`.
    `capabilities` reports the primary's caps, except `vision` ORs in the
    fallback's — a cloud planner without vision behind a vision-capable local
    model still reports vision. The `sleep` and `retryable` parameters are
    test seams (record instead of wait; pin the predicate).
    """

    def __init__(
        self,
        primary: LLMProvider,
        fallback: LLMProvider | None = None,
        retry: RetryPolicy = DEFAULT_RETRY,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,  # test seam
        retryable: Callable[[Exception], bool] = default_retryable,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._retry = retry
        self._sleep = sleep
        self._retryable = retryable

    @property
    def capabilities(self) -> ProviderCaps:
        caps = self._primary.capabilities
        if self._fallback is not None and self._fallback.capabilities.vision:
            caps = caps.model_copy(update={"vision": True})
        return caps

    async def complete(self, req: LLMRequest) -> LLMResponse:
        providers: tuple[LLMProvider, ...] = (
            (self._primary, self._fallback) if self._fallback is not None else (self._primary,)
        )
        last: Exception | None = None
        for provider in providers:
            try:
                return await self._complete_one(provider, req)
            except Exception as exc:
                last = exc
                more = provider is not providers[-1]
                logger.warning(
                    "LLM provider %s failed: %r — %s",
                    type(provider).__name__,
                    exc,
                    "trying the fallback" if more else "providers exhausted",
                )
        raise ProviderUnavailableError(
            f"all {len(providers)} configured provider(s) failed; last: {last!r}"
        ) from last

    async def _complete_one(self, provider: LLMProvider, req: LLMRequest) -> LLMResponse:
        """One provider: up to `max_attempts` tries, silent sleeps between."""
        attempts = max(1, self._retry.max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                return await provider.complete(req)
            except Exception as exc:
                if attempt == attempts or not self._retryable(exc):
                    raise
                logger.info(
                    "transient provider failure (%s), retrying in %.1fs",
                    type(exc).__name__,
                    self._retry.backoff_s,
                )
                await self._sleep(self._retry.backoff_s)
        raise AssertionError("unreachable: attempts >= 1")  # pragma: no cover
