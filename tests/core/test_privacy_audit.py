"""§3.15 whole-session privacy audit (M5 Task 6).

One full headless scripted session — real ``App`` over the scripted provider —
that deliberately flows everything the plan says must never reach the session
log in the clear: a monitor-target URL carrying a query credential (the M4
handoff), a registered API key (a provider crash echoes it in a traceback),
and a ``password=`` shape, plus a screenshot-producing turn whose artifacts
must be purged at task end. The session JSONL is then read back row by row and
must be clean. ``docs/privacy.md`` is the audit's written output and must name
every storage location.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from marshmellow.app import App
from marshmellow.core import secrets
from marshmellow.core.config import Settings
from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    PerceptionHint,
    PerceptionSnapshot,
    WindowInfo,
)
from marshmellow.packs.base import ShellResult
from marshmellow.perception.registry import ElementRegistry
from marshmellow.planner.orchestrator import LLMPlanner
from marshmellow.planner.tools import ScreenToolbox
from marshmellow.tasks.manager import TaskManager
from marshmellow.tasks.monitors import CheckResult
from marshmellow.testing.scenario import ScenarioProvider

REPO_ROOT = Path(__file__).resolve().parents[2]

KEY = "sk-fake-marshmellow-audit-9931"
TOKEN_VALUE = "sekrit-query-value"
MONITOR_URL = f"https://api.example.com/feed?key={TOKEN_VALUE}&x=1"


@pytest.fixture(autouse=True)
def _isolated_logging():
    """The session log must be THIS test's file: reset the logging singleton
    around the run (which also drops any previously registered secrets)."""
    from marshmellow.core import logging as mlog

    mlog._reset_for_tests()
    yield
    mlog._reset_for_tests()


# -- doubles (the tests/ convention: local copies, tests are not a package) -----


class _FakeKeyring:
    """KeyringLike double: the fake backend for ``store_api_key``."""

    def __init__(self) -> None:
        self.stored: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, account: str, password: str) -> None:
        self.stored[(service, account)] = password

    def get_password(self, service: str, account: str) -> str | None:
        return self.stored.get((service, account))

    def delete_password(self, service: str, account: str) -> None:
        self.stored.pop((service, account), None)


class _RecordingPack:
    """WindowsPack double — the audit script must never open apps."""

    def open_app(self, target: str) -> ShellResult:
        raise AssertionError("the privacy-audit script must not open apps")


def _refuse_acts(req: ActionRequest) -> ActionResult:
    raise AssertionError("the privacy-audit script must not act on the screen")


class _ShotPerception:
    """ScreenPerception double: every snapshot is a vision-path one carrying
    REAL artifact files — the material the screenshot tool must purge (§3.15)."""

    def __init__(self, shot: Path, marked: Path) -> None:
        self.shot = shot
        self.marked = marked

    def snapshot(
        self, hint: PerceptionHint | None = None, hwnd: int | None = None
    ) -> PerceptionSnapshot:
        return PerceptionSnapshot(
            active_window=WindowInfo(hwnd=5, title="Feed page", app="Browser"),
            elements=[],
            screenshot=self.shot,
            marked_image=self.marked,
        )


class _StaticCheck:
    """Offline MonitorCheck double: captures a baseline, never reports a change —
    the url_diff monitor registers and parks without ever touching the network."""

    def check(self, baseline: str | None) -> CheckResult:
        return CheckResult(
            ok=True,
            value=baseline or "v1",
            changed=False,
            summary="the watched page is unchanged",
        )


class _FrozenClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 11, 7, 12, 0, 0)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class _IdleSleeper:
    """Never advances the frozen clock: a monitor's due-wait parks forever."""

    def __init__(self, clock: _FrozenClock) -> None:
        self.clock = clock
        self.advance_ratio = 0.0

    async def __call__(self, seconds: float) -> None:
        self.clock.advance(seconds * self.advance_ratio)


class _LeakingProvider(ScenarioProvider):
    """Scripted provider whose script exhaustion raises with the REGISTERED key
    in the message — the realistic way an API key reaches a traceback (a cloud
    provider echoing credentials in an auth error). The scrubber must catch it."""

    def __init__(self, calls: list[LLMToolCall], leak: Exception) -> None:
        super().__init__(calls)
        self._leak = leak

    async def complete(self, req: LLMRequest) -> LLMResponse:
        if not self._calls and not any(m.images for m in req.messages):
            raise self._leak
        return await super().complete(req)


# -- harness --------------------------------------------------------------------


async def _until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("condition was not reached in time")
        await asyncio.sleep(0.01)


def _session_text(log_dir: Path) -> str:
    files = sorted(log_dir.glob("session-*.jsonl"))
    assert files, "no session log written"
    return files[0].read_text(encoding="utf-8")


# -- the audit ------------------------------------------------------------------


async def test_session_log_is_clean(tmp_path: Path) -> None:
    shot = tmp_path / "marshmellow-shot-audit.png"
    marked = tmp_path / "marshmellow-marked-audit.png"
    shot.write_bytes(b"png")
    marked.write_bytes(b"png")
    log_dir = tmp_path / "logs"

    backend = _FakeKeyring()
    secrets.store_api_key(KEY, backend=backend)  # registers the key with the scrubber

    provider = _LeakingProvider(
        [
            LLMToolCall(name="screenshot", arguments={}),
            LLMToolCall(  # the M4 handoff: a monitor whose TARGET carries the credential
                name="create_task",
                arguments={
                    "instruction": f"watch {MONITOR_URL} and report changes",
                    "mode": "background_monitor",
                    "monitor": {
                        "kind": "url_diff",
                        "target": MONITOR_URL,
                        "interval_s": 300,
                        "condition": "the page changes",
                    },
                },
            ),
        ],
        leak=ValueError(f"provider crashed for api_key {KEY} mid-stream"),
    )
    toolbox = ScreenToolbox(_ShotPerception(shot, marked), ElementRegistry(), _refuse_acts)
    app = App(
        Settings(),
        headless=True,
        log_dir=log_dir,
        pack=_RecordingPack(),
        planner=LLMPlanner(provider, toolbox=toolbox),
        task_store_path=tmp_path / "tasks.json",
    )
    clock = _FrozenClock()
    app.manager = TaskManager(
        bus=app.bus,
        planner=app.planner,
        execute=app._execute,
        clock=clock,
        rng=lambda: 0.0,
        sleeper=_IdleSleeper(clock),
        lease=app.lease,
        store=app.task_store,
        check_factory=lambda spec: _StaticCheck(),
    )
    try:
        handle = await app.submit_text(
            f"watch {MONITOR_URL} and tell me when it changes (password=hunter9 there)"
        )
        await _until(lambda: handle.result is not None)
        await _until(lambda: bool(app.manager.list_tasks("waiting")))

        # the session really covered every audited path
        assert handle.result is not None and not handle.result.ok, str(handle.result)
        assert app.manager.list_tasks("waiting"), "the monitor was never registered"

        # §3.15: screenshots live only while the task is active — purged at task end
        assert not shot.exists() and not marked.exists(), (
            "screenshot artifacts survived the turn"
        )

        raw = _session_text(log_dir)
        entries = [json.loads(line) for line in raw.splitlines() if line.strip()]
        assert entries, "empty session log"  # every row must parse: no scrub corruption

        # the scrubbed URL did flow through the log (so the asserts below bite)
        assert "?key=[REDACTED]&x=1" in raw, "the monitor URL never reached the session log"
        assert KEY not in raw, "the registered API key leaked into the session log"
        assert TOKEN_VALUE not in raw, "the URL query credential leaked into the session log"
        assert re.search(r"password=(?!\[REDACTED\])", raw) is None, (
            "an unredacted password= shape persisted"
        )
        assert str(shot) not in raw and str(marked) not in raw, (
            "a screenshot artifact path persisted in the session log"
        )
        assert any(
            "exc" in entry and "api_key [REDACTED] mid-stream" in str(entry["exc"])
            for entry in entries
        ), "the key-bearing provider crash was not scrubbed in exc text"

        # the audit's written output exists and names every storage location
        privacy = REPO_ROOT / "docs" / "privacy.md"
        assert privacy.exists(), "docs/privacy.md is missing"
        privacy_text = privacy.read_text(encoding="utf-8")
        for marker in (
            "%LOCALAPPDATA%",  # the product data root
            "logs",  # the session JSONL directory
            "tasks.json",  # the background-task store
            "Credential Manager",  # where the API key lives
            "screenshot",  # active-task-only, then purged
            "transcript",  # transcripts stay local
            "telemetry",  # what never leaves the machine (01 §0.7)
        ):
            assert marker in privacy_text, f"privacy.md does not name {marker!r}"
    finally:
        app.stop()
