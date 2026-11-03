import logging
import threading

import pytest
from pydantic import BaseModel

from marshmellow.core.bus import EventBus


class Ping(BaseModel):
    n: int


class Pong(BaseModel):
    n: int


def test_two_subscribers_both_receive() -> None:
    bus = EventBus()
    got1: list[Ping] = []
    got2: list[Ping] = []
    bus.subscribe(Ping, got1.append)
    bus.subscribe(Ping, got2.append)
    bus.emit(Ping(n=1))
    assert got1 == [Ping(n=1)]
    assert got2 == [Ping(n=1)]


def test_type_filtering() -> None:
    bus = EventBus()
    pings: list[Ping] = []
    bus.subscribe(Ping, pings.append)
    bus.emit(Pong(n=2))
    bus.emit(Ping(n=3))
    assert pings == [Ping(n=3)]


def test_raising_subscriber_is_isolated(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()
    ran: list[int] = []

    def boom(ev: Ping) -> None:
        raise RuntimeError("boom")

    bus.subscribe(Ping, boom)
    bus.subscribe(Ping, lambda ev: ran.append(ev.n))
    with caplog.at_level(logging.ERROR):
        bus.emit(Ping(n=7))
    assert ran == [7]
    assert any(
        r.exc_info is not None and isinstance(r.exc_info[1], RuntimeError)
        for r in caplog.records
    )


def test_emit_threadsafe_delivers_from_thread() -> None:
    bus = EventBus()
    got: list[Ping] = []
    done = threading.Event()

    def handler(ev: Ping) -> None:
        got.append(ev)
        done.set()

    bus.subscribe(Ping, handler)
    t = threading.Thread(target=lambda: bus.emit_threadsafe(Ping(n=3)))
    t.start()
    assert done.wait(timeout=5.0)  # condition-bounded wait, not a sleep
    t.join()
    assert got == [Ping(n=3)]
    bus.stop()


def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    got: list[Ping] = []
    unsub = bus.subscribe(Ping, got.append)
    bus.emit(Ping(n=1))
    unsub()
    bus.emit(Ping(n=2))
    assert got == [Ping(n=1)]
    bus.stop()
