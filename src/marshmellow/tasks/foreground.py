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

    Thread discipline: consent is modal — one spoken question at a time, so
    ``request`` serializes on the lock and HOLDS it across the ask (up to the
    ConfirmFlow's re-ask + fallback, tens of seconds). That is safe only
    because request() runs on planner WORKER threads; release() runs on
    event-loop threads (the turn's finally), so it NEVER takes the lock
    blocking (see release). A request while another task holds the lease (or
    while its consent is in flight) refuses without asking the user again.
    """

    def __init__(
        self, bus: EventBus, confirm: Confirmer, mascot: MascotSeam | None = None
    ) -> None:
        self._bus = bus
        self._confirm = confirm
        self._mascot = mascot
        self._holder: str | None = None
        self._lock = threading.Lock()
        # Releases that lost the race with an in-flight consent (kill during
        # the ask): the pending request() honors them instead of granting a
        # lease to a turn that is already gone.
        self._deferred: set[str] = set()

    def request(self, task_id: str, purpose: str) -> bool:
        """Ask the user for the screen; True grants the lease to `task_id`.

        Emits ``lease_request`` (payload = purpose), shows the mascot
        "attention" state, then confirms out loud. Granted → mascot "acting";
        refused (or held by another task, or the turn ended mid-ask) → mascot
        "idle", False. BLOCKING — worker threads only, never a loop thread.
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
            self._deferred.discard(task_id)  # a fresh ask supersedes stale tombstones
            self._bus.emit(TaskEvent(task_id=task_id, type="lease_request", payload=purpose))
            if self._mascot is not None:
                self._mascot.set_state("attention")
            granted = self._confirm.confirm(f"I need the screen to {purpose} — OK?")
            if task_id in self._deferred:
                # The turn's finally ran while we were asking (kill / barge-in
                # cancelled it): the grant must not outlive the turn.
                self._deferred.discard(task_id)
                granted = False
            if granted:
                self._holder = task_id
                if self._mascot is not None:
                    self._mascot.set_state("acting")
                logger.info("foreground lease granted to %s (%s)", task_id, purpose)
            else:
                logger.info("foreground lease refused for %s (%s)", task_id, purpose)
                if self._mascot is not None:
                    self._mascot.set_state("idle")
            return granted

    def release(self, task_id: str) -> None:
        """Give the screen back (no-op when `task_id` is not the holder).

        NEVER BLOCKS — the caller is an event-loop thread (the turn's
        finally), and the lock is held across in-flight consents. A busy lock
        means a consent is mid-ask; it only started when nobody held, so this
        release is either a no-op (another task wins the ask) or races with
        OUR OWN ask — tombstoned here and honored by that ask before it could
        grant a lease to a dead turn.
        """
        if self._lock.acquire(blocking=False):
            try:
                if self._holder == task_id:
                    self._holder = None
                    if self._mascot is not None:
                        self._mascot.set_state("idle")
                    logger.info("foreground lease released by %s", task_id)
            finally:
                self._lock.release()
        else:
            self._deferred.add(task_id)

    def held_by(self) -> str | None:
        """The task currently holding the lease (None when free).

        Lock-free on purpose: a GIL-atomic attribute read. Taking the lock
        could park a planner worker behind an in-flight consent for the length
        of a spoken ask; a stale reading is safe because request() re-checks
        under the lock before granting.
        """
        return self._holder
