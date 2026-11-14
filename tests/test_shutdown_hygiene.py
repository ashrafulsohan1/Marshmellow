"""Regression guards for test-session shutdown hygiene.

The v0.1.2 cleanroom's run 1 of 3 hung >20 min at ``[100%]``: pytest's
``gc_collect_harder`` pass finalized a stopped-but-unclosed proactor event
loop whose ``close()`` parked forever in ``IocpProactor._poll`` on Windows.
tests/conftest.py now sweeps lingering loops at unconfigure, and tests must
not leak unclosed loops either (tests/llm/test_ollama.py's resident loops
were the known producer).
"""

import asyncio
import threading

from conftest import _close_lingering_event_loops


def test_sweep_closes_a_stopped_unclosed_loop() -> None:
    loop = asyncio.new_event_loop()
    assert not loop.is_closed()
    _close_lingering_event_loops()
    assert loop.is_closed()


def test_sweep_stops_and_closes_a_running_resident_loop() -> None:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(
        target=loop.run_forever, name="test-sweep-resident-loop", daemon=True
    )
    thread.start()
    try:
        assert loop.is_running()
        _close_lingering_event_loops()
        assert loop.is_closed()
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        if not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            if not loop.is_running():
                loop.close()
