"""ForegroundLease — consented screen access for background tasks (§3.12, D6).

§3.12: "No silent UI takeover. Ever." A background mode (monitor / job /
scheduled) that needs to act on screen must first be granted the foreground
lease: the ask is announced (TaskEvent ``lease_request`` on the bus, mascot →
"attention"), then confirmed out loud — "I need the screen to {purpose} —
OK?" — through the same fail-closed :class:`~marshmellow.safety.confirm.ConfirmFlow`
as the L3 gate (spoken yes/no, tray fallback in GUI mode; headless has no
fallback, so ambiguity refuses). A grant holds until the turn's ``finally``
releases it. The TaskManager executor seam (tasks/manager.py) raises the ask
BEFORE the safety gate, so the gate still sees every granted request and a
refusal never reaches the executor.

``READ_KINDS`` are the executor actions a background mode may run leaseless:
none of them synthesize input.
"""

from __future__ import annotations

import logging
import threading

from marshmellow.audio.ask import MascotSeam
from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import TaskEvent
from marshmellow.safety.gate import Confirmer

logger = logging.getLogger(__name__)

#: §3.12 D6: the only executor actions a background mode may run leaseless.
READ_KINDS: frozenset[str] = frozenset({"wait", "ask_user", "done", "fail", "create_task"})


class ForegroundLease:
    """Single-holder consent token for background screen access (§3.12 D6).

    Thread-safe: planner turns run on worker threads and may ask concurrently,
    so consent is serialized — one spoken question at a time, holder
    bookkeeping atomic. A request while another task holds the lease refuses
    without asking the user again.
    """

    def __init__(
        self, bus: EventBus, confirm: Confirmer, mascot: MascotSeam | None = None
    ) -> None:
        self._bus = bus
        self._confirm = confirm
        self._mascot = mascot
        self._holder: str | None = None
        self._lock = threading.Lock()

    def request(self, task_id: str, purpose: str) -> bool:
        """Ask the user for the screen; True grants the lease to `task_id`.

        Emits ``lease_request`` (payload = purpose), shows the mascot
        "attention" state, then confirms out loud. Granted → mascot "acting";
        refused (or held by another task) → mascot "idle", False.
        """
        with self._lock:
            if self._holder == task_id:
                return True  # already granted this turn
            if self._holder is not None:
                logger.info(
                    "foreground lease held by %s — %s refused without asking",
                    self._holder,
                    task_id,
                )
                return False
            self._bus.emit(TaskEvent(task_id=task_id, type="lease_request", payload=purpose))
            if self._mascot is not None:
                self._mascot.set_state("attention")
            if not self._confirm.confirm(f"I need the screen to {purpose} — OK?"):
                logger.info("foreground lease refused for %s (%s)", task_id, purpose)
                if self._mascot is not None:
                    self._mascot.set_state("idle")
                return False
            self._holder = task_id
            if self._mascot is not None:
                self._mascot.set_state("acting")
            logger.info("foreground lease granted to %s (%s)", task_id, purpose)
            return True

    def release(self, task_id: str) -> None:
        """Give the screen back (no-op when `task_id` is not the holder)."""
        with self._lock:
            if self._holder != task_id:
                return
            self._holder = None
            if self._mascot is not None:
                self._mascot.set_state("idle")
            logger.info("foreground lease released by %s", task_id)

    def held_by(self) -> str | None:
        """The task currently holding the lease (None when free)."""
        with self._lock:
            return self._holder
