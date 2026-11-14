"""Shared test configuration: Qt must run headless in CI and in dev test runs."""

import asyncio
import gc
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _close_lingering_event_loops() -> None:
    """Close every event loop the test session created and abandoned.

    ``BaseEventLoop.__del__`` closes a stopped-but-unclosed loop at GC time,
    and on Windows ``BaseProactorEventLoop.close`` (``IocpProactor.close`` →
    ``_poll``) waits for cancelled overlapped operations to report back — if
    one never does, that wait never ends. The v0.1.2 cleanroom hit this: run
    1 of 3 parked >20 min at ``[100%]`` while ``pytest_unconfigure``'s
    ``gc_collect_harder`` finalized an unclosed proactor loop
    (docs/plan/CLEANROOM-VERIFY-v012.md §2). Closing loops here, before that
    GC pass, means it has nothing left to close.
    """
    loops = {
        obj
        for obj in gc.get_objects()
        if isinstance(obj, asyncio.AbstractEventLoop) and not obj.is_closed()
    }
    for loop in loops:
        if loop.is_running():
            try:
                # Resident loops on daemon threads (LLMPlanner / Perceiver style)
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass  # already shut down under us
    for _ in range(100):  # ≤5 s: let stopped loops actually stop before close
        if not any(loop.is_running() for loop in loops):
            break
        time.sleep(0.05)
    for loop in loops:
        try:
            loop.close()
        except RuntimeError:
            pass  # still running (its daemon thread outlived the 5 s budget)


@pytest.hookimpl(tryfirst=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    # tryfirst: must run before _pytest.unraisableexception's own
    # pytest_unconfigure, whose gc_collect_harder pass is what finalized the
    # hung loop (see _close_lingering_event_loops).
    _close_lingering_event_loops()
