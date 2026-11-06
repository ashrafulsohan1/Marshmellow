"""Provider resilience tests (M5 T2) — silent retries + primary→fallback chain (§3.4).

Retries are SILENT by design (a spoken word per hiccup is noise; the planner's
unavailable summary is the UX), so these tests pin the mechanics through the
injectable `sleep` seam: recorded sleeps, call counts per provider, fail-fast
on non-retryable errors, the caps vision-union, and the final
ProviderUnavailableError.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import openai
import pytest

from marshmellow.core.config import LLMProviderConfig, Settings, load_settings
from marshmellow.core.contracts import LLMMessage, LLMRequest, LLMResponse, ProviderCaps
from marshmellow.llm.base import ProviderUnavailableError
from marshmellow.llm.resilience import ProviderChain, RetryPolicy, default_retryable


class _ScriptedProvider:
    """Test double: replays outcomes (exceptions → responses) per complete() call."""

    def __init__(
        self,
        script: list[Exception | LLMResponse],
        caps: ProviderCaps | None = None,
    ) -> None:
        self.script = list(script)
        self.calls = 0
        self.caps = caps or ProviderCaps(vision=False, tool_call=True, json_schema=False)

    @property
    def capabilities(self) -> ProviderCaps:
        return self.caps

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _sleep_recorder() -> tuple[Callable[[float], Awaitable[None]], list[float]]:
    """A `sleep` seam that records instead of waits — pins retry cadence."""
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    return record, sleeps


def _request() -> LLMRequest:
    return LLMRequest(messages=[LLMMessage(role="user", content="hi")])


def _reply(text: str = "ok") -> LLMResponse:
    return LLMResponse(text=text, stop_reason="stop")


async def test_transient_retried_then_succeeds() -> None:
    """ConnectError once → silent retry (one recorded sleep) → success on try 2."""
    primary = _ScriptedProvider([httpx.ConnectError("boom"), _reply()])
    sleep, sleeps = _sleep_recorder()
    chain = ProviderChain(
        primary=primary, retry=RetryPolicy(max_attempts=2, backoff_s=1.0), sleep=sleep
    )

    response = await chain.complete(_request())

    assert response == _reply()
    assert primary.calls == 2
    assert sleeps == [1.0]


async def test_attempts_exhausted_falls_back() -> None:
    """Primary exhausted (2 tries) → the fallback answers on its first try."""
    primary = _ScriptedProvider(
        [httpx.ConnectError("boom"), httpx.ConnectError("boom")]
    )
    fallback = _ScriptedProvider([_reply("local")])
    sleep, sleeps = _sleep_recorder()
    chain = ProviderChain(primary=primary, fallback=fallback, sleep=sleep)

    response = await chain.complete(_request())

    assert response.text == "local"
    assert primary.calls == 2
    assert fallback.calls == 1
    assert sleeps == [1.0]  # the primary's single retry; the fallback answered first try


async def test_non_retryable_fails_fast() -> None:
    """ValueError is not transient → no sleep, straight to the fallback."""
    primary = _ScriptedProvider([ValueError("bad request shape")])
    fallback = _ScriptedProvider([_reply("local")])
    sleep, sleeps = _sleep_recorder()
    chain = ProviderChain(primary=primary, fallback=fallback, sleep=sleep)

    response = await chain.complete(_request())

    assert response.text == "local"
    assert primary.calls == 1
    assert fallback.calls == 1
    assert sleeps == []


async def test_both_fail_raises_unavailable() -> None:
    """Every provider exhausted (2 + 2 tries) → ProviderUnavailableError."""
    primary = _ScriptedProvider(
        [httpx.ConnectError("boom"), httpx.ConnectError("boom")]
    )
    fallback = _ScriptedProvider(
        [httpx.ConnectError("local down"), httpx.ConnectError("local down")]
    )
    sleep, _ = _sleep_recorder()
    chain = ProviderChain(primary=primary, fallback=fallback, sleep=sleep)

    with pytest.raises(ProviderUnavailableError):
        await chain.complete(_request())
    assert primary.calls == 2
    assert fallback.calls == 2


def test_caps_vision_union() -> None:
    """Chain caps = primary's, except `vision` ORs in the fallback's (§3.4)."""
    cloud = _ScriptedProvider(
        [], caps=ProviderCaps(vision=False, tool_call=True, json_schema=True)
    )
    local = _ScriptedProvider(
        [], caps=ProviderCaps(vision=True, tool_call=True, json_schema=False)
    )

    assert ProviderChain(primary=cloud, fallback=local).capabilities.vision is True
    # the rest of the caps stay the primary's
    assert ProviderChain(primary=cloud, fallback=local).capabilities.json_schema is True
    # and the fallback never lowers the primary's vision
    local_off = _ScriptedProvider(
        [], caps=ProviderCaps(vision=False, tool_call=True, json_schema=False)
    )
    assert ProviderChain(primary=local, fallback=local_off).capabilities.vision is True


def test_caps_no_fallback_passthrough() -> None:
    primary = _ScriptedProvider(
        [], caps=ProviderCaps(vision=False, tool_call=True, json_schema=False)
    )
    assert ProviderChain(primary=primary).capabilities == ProviderCaps(
        vision=False, tool_call=True, json_schema=False
    )


_HTTP_REQ = httpx.Request("POST", "http://provider.test/v1/chat")


def _status_error(status: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        f"HTTP {status}",
        request=_HTTP_REQ,
        response=httpx.Response(status, request=_HTTP_REQ),
    )


def _openai_status_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    return cls(
        f"HTTP {status}",
        response=httpx.Response(status, request=_HTTP_REQ),
        body=None,
    )


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        # httpx transport errors: transient
        (httpx.ConnectError("boom"), True),
        (httpx.ReadTimeout("slow"), True),
        (_status_error(429), True),
        (_status_error(500), True),
        (_status_error(503), True),
        # auth / request-shape 4xx: the key or request is wrong — retrying only delays the truth
        (_status_error(400), False),
        (_status_error(401), False),
        # openai SDK dialects of the same transient failures
        (openai.APIConnectionError(request=_HTTP_REQ), True),
        (openai.APITimeoutError(request=_HTTP_REQ), True),
        (_openai_status_error(openai.RateLimitError, 429), True),
        (_openai_status_error(openai.InternalServerError, 500), True),
        # anything else: fail fast
        (ValueError("nope"), False),
    ],
)
def test_default_retryable(exc: Exception, retryable: bool) -> None:
    assert default_retryable(exc) is retryable


# -- config: [providers] fallback ----------------------------------------------


def test_providers_fallback_parses(tmp_path: Path) -> None:
    """[providers] fallback parses into a second LLMProviderConfig (§3.4 order)."""
    p = tmp_path / "config.toml"
    p.write_text(
        '[providers.llm]\nkind = "openai_compat"\nmodel = "glm-4"\n'
        '[providers.fallback]\nkind = "ollama"\nmodel = "llava"\n',
        encoding="utf-8",
    )

    settings = load_settings(p)

    assert settings.providers.llm.kind == "openai_compat"
    assert settings.providers.fallback is not None
    assert settings.providers.fallback.kind == "ollama"
    assert settings.providers.fallback.model == "llava"


def test_fallback_defaults_to_none() -> None:
    assert Settings().providers.fallback is None


def test_fallback_roundtrips_through_save(tmp_path: Path) -> None:
    """save() omits a None fallback (tomli_w refuses null) and keeps a set one."""
    p = tmp_path / "config.toml"
    settings = Settings()
    settings.providers.fallback = LLMProviderConfig(kind="ollama", model="llava")
    settings.save(p)

    loaded = load_settings(p)

    assert loaded == settings
    assert loaded.providers.fallback is not None
    assert loaded.providers.fallback.model == "llava"

    Settings().save(p)  # default settings: fallback is None — save must not crash
    assert load_settings(p).providers.fallback is None
