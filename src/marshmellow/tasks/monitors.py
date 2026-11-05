"""Monitor checks — deterministic polling for the four MonitorSpec kinds (M4 Task 1).

Checks are sync on purpose: the Task-2 monitor loop runs them via
``asyncio.to_thread``. A check is a pure function of (baseline, current world
state): ``baseline=None`` is the first run, which captures a baseline and never
triggers (M4 pinned convention), and ``changed`` compares the fresh value
against that baseline. HTTP kinds share an ``httpx.Client`` seam so tests use
``MockTransport`` and the app can inject its own client. Summary strings are
the M4-pinned spoken reports.

Target formats (M4 pinned conventions): ``api_field`` is ``<url>#<dotted.path>``
(the fragment is stripped before fetching — it is our field selector, not part
of the URL); ``url_diff``/``rss`` are plain URLs; ``file_hash`` is a local path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import feedparser
import httpx

from marshmellow.core.contracts import MonitorSpec

_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    value: str
    changed: bool
    summary: str
    error: str | None = None


class MonitorCheck(Protocol):
    def check(self, baseline: str | None) -> CheckResult: ...


def _fetch(url: str, client: httpx.Client | None) -> tuple[bytes | None, str | None]:
    """GET the url; returns (body, error) — body is None when the fetch failed."""
    try:
        response = (
            client.get(url)
            if client is not None
            else httpx.get(url, timeout=_TIMEOUT_S, follow_redirects=True)
        )
    except httpx.HTTPError as exc:
        return None, f"fetch failed: {exc}"
    if not response.is_success:
        return None, f"HTTP {response.status_code} from {url}"
    return response.content, None


def _failed(baseline: str | None, error: str) -> CheckResult:
    """Uniform error result: an error never counts as a change."""
    return CheckResult(ok=False, value=baseline or "", changed=False, summary=error, error=error)


def _result(
    value: str, baseline: str | None, changed_summary: str, same_summary: str
) -> CheckResult:
    changed = baseline is not None and value != baseline
    return CheckResult(
        ok=True, value=value, changed=changed, summary=changed_summary if changed else same_summary
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class UrlDiffCheck:
    """sha256 of the response body — "tell me when that page changes"."""

    def __init__(self, url: str, *, client: httpx.Client | None = None) -> None:
        self._url = url
        self._client = client

    def check(self, baseline: str | None) -> CheckResult:
        body, error = _fetch(self._url, self._client)
        if body is None:
            return _failed(baseline, f"page check failed: {error}")
        return _result(
            _sha256(body), baseline, "the watched page changed", "the watched page is unchanged"
        )


class ApiFieldCheck:
    """Stringified field at a dotted path over the JSON body."""

    def __init__(self, url: str, field_path: str, *, client: httpx.Client | None = None) -> None:
        self._url = url
        self._field_path = field_path
        self._client = client

    def check(self, baseline: str | None) -> CheckResult:
        body, error = _fetch(self._url, self._client)
        if body is None:
            return _failed(baseline, f"api check failed: {error}")
        try:
            field = _walk(json.loads(body), self._field_path)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            return _failed(baseline, f"field not found: {self._field_path} ({exc})")
        value = str(field)
        return _result(
            value, baseline, f"{self._field_path} is now {value}",
            f"{self._field_path} unchanged ({value})",
        )


def _walk(payload: object, path: str) -> object:
    """Walk a dotted path over a JSON payload; numeric segments index lists."""
    current: object = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.lstrip("-").isdigit():
            current = current[int(part)]  # IndexError -> caller reports "field not found"
        else:
            raise KeyError(part)
    return current


class RssCheck:
    """Newest entry id — "tell me when they upload"."""

    def __init__(self, url: str, *, client: httpx.Client | None = None) -> None:
        self._url = url
        self._client = client

    def check(self, baseline: str | None) -> CheckResult:
        body, error = _fetch(self._url, self._client)
        if body is None:
            return _failed(baseline, f"feed check failed: {error}")
        parsed = feedparser.parse(body)
        if not parsed.entries:
            return _failed(baseline, f"feed has no entries: {self._url}")
        newest = parsed.entries[0]
        value = str(newest.get("id", ""))
        title = str(newest.get("title", ""))
        return _result(value, baseline, f"new upload: {title}", "the feed is unchanged")


class FileHashCheck:
    """sha256 of a local file's bytes — local-only, no HTTP client."""

    def __init__(self, path: str) -> None:
        self._path = path

    def check(self, baseline: str | None) -> CheckResult:
        try:
            body = Path(self._path).read_bytes()
        except OSError as exc:
            return _failed(baseline, f"cannot read file: {self._path} ({exc})")
        return _result(
            _sha256(body), baseline, "the watched file changed", "the watched file is unchanged"
        )


def make_check(spec: MonitorSpec, *, client: httpx.Client | None = None) -> MonitorCheck:
    """Build the check for a MonitorSpec.

    Raises ValueError on an unparseable target (``api_field`` without its
    ``#dotted.path`` fragment) or an unknown kind (reachable only via
    ``model_construct``, which skips Literal validation).
    """
    match spec.kind:
        case "url_diff":
            return UrlDiffCheck(spec.target, client=client)
        case "api_field":
            url, sep, field_path = spec.target.partition("#")
            if not sep or not url or not field_path:
                raise ValueError(
                    f"api_field target must be '<url>#<dotted.path>', got {spec.target!r}"
                )
            return ApiFieldCheck(url, field_path, client=client)
        case "file_hash":
            return FileHashCheck(spec.target)
        case "rss":
            return RssCheck(spec.target, client=client)
        case _:
            raise ValueError(f"unknown monitor kind: {spec.kind!r}")
