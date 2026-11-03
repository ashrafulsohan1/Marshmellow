"""EventBus — the only cross-module state channel (01-architecture §3.15).

Sync dispatch on the emitting thread; `emit_threadsafe` marshals events from
non-loop threads (UIA walkers, Qt slots) onto a daemon drain thread. One
raising subscriber is logged and never blocks the others.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

E = TypeVar("E", bound=BaseModel)


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[type[BaseModel], list[Callable[[BaseModel], None]]] = {}
        self._lock = threading.Lock()
        self._q: queue.Queue[BaseModel | None] = queue.Queue()
        self._worker = threading.Thread(
            target=self._drain, name="bus-worker", daemon=True
        )
        self._worker.start()

    def subscribe(
        self, etype: type[E], handler: Callable[[E], None]
    ) -> Callable[[], None]:
        """Register `handler` for events of exactly `etype`. Returns unsubscribe."""

        def on(ev: BaseModel) -> None:
            handler(ev)  # type: ignore[arg-type]

        with self._lock:
            self._subs.setdefault(etype, []).append(on)

        def unsubscribe() -> None:
            with self._lock:
                lst = self._subs.get(etype, [])
                if on in lst:
                    lst.remove(on)

        return unsubscribe

    def emit(self, event: BaseModel) -> None:
        """Dispatch synchronously on the caller's thread."""
        self._dispatch(event)

    def emit_threadsafe(self, event: BaseModel) -> None:
        """Dispatch on the bus drain thread (safe from any thread)."""
        self._q.put(event)

    def stop(self) -> None:
        self._q.put(None)
        self._worker.join(timeout=2.0)

    def _drain(self) -> None:
        while True:
            event = self._q.get()
            if event is None:
                return
            self._dispatch(event)

    def _dispatch(self, event: BaseModel) -> None:
        with self._lock:
            handlers = list(self._subs.get(type(event), []))
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception("EventBus subscriber raised for %s", type(event).__name__)
