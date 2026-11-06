"""TaskManager — one coroutine per task + long-lived background loops (01-architecture §3.12).

The one-shot ``submit`` (M1/M2) is unchanged: the planner runs on a worker
thread (``asyncio.to_thread`` — blocking work never touches a loop), every
action passes through the executor seam, the full TaskEvent trace lands on the
EventBus, and the kill switch cancels the running turn — with a per-task kill
flag the executor seam checks BEFORE every action (cancelling the coroutine
alone cannot stop the worker thread, so the flag is what makes "aborts actions
instantly" true).

M4 v2 adds the background task classes (§3.12): ``register`` accepts the
background modes and runs a long-lived loop per task on a *resident* asyncio
loop thread (lazy-started by ``start()``). Monitor loops capture a baseline on
the first check (never triggers — M4 pinned convention), then wait for
``next_due`` (jittered scheduler, ``tasks/scheduler.py``) through the injected
``clock``/``rng``/``sleeper`` seams and run the check itself via
``to_thread``. ``trigger = ok and changed`` completes the task one-shot with a
``result`` event; check errors emit ``waiting`` events (never spoken) and fail
the task after 5 consecutive errors. Scheduled tasks wait for
``spec.schedule`` and then run the planner turn exactly like ``submit``.

Monitors never emit ``step`` (no executor actions) — their trace is
``["started", "result"]``. ``register``/``control`` marshal onto the resident
loop via ``run_coroutine_threadsafe`` because they are called from planner
worker threads and the GUI thread; ``cancel_all`` reaches the long-lived loops
too, so "stop everything" stops monitors (§3.13).

M4 T3 adds the §3.12 D6 foreground lease: the per-turn executor seam
(``_executing``) requires a granted ``ForegroundLease`` before a background
mode's non-read action reaches the executor (and its safety gate); a refusal
is an ``ActionResult(ok=False, error="lease_denied"/"lease_required")`` and
the lease is released in the turn's ``finally``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import threading
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    ContextSnapshot,
    Instruction,
    MonitorSpec,
    TaskEvent,
    TaskMode,
    TaskResult,
    TaskSpec,
    TaskState,
    Transcript,
)
from marshmellow.tasks.foreground import READ_KINDS, ForegroundLease
from marshmellow.tasks.monitors import MonitorCheck, make_check
from marshmellow.tasks.scheduler import is_due, next_due

logger = logging.getLogger(__name__)

Executor = Callable[[ActionRequest], ActionResult]

TaskEventName = Literal["started", "step", "waiting", "lease_request", "result", "failed",
                        "cancelled"]

#: §3.12 background classes — the only modes ``register()`` accepts.
BACKGROUND_MODES: frozenset[TaskMode] = frozenset(
    {"background_monitor", "background_job", "scheduled"}
)

#: Consecutive failed checks before a monitor task fails (M4 pinned convention).
_MAX_CHECK_ERRORS = 5

#: Wait-phase sleep granularity: pause / run_now take effect within one chunk.
_WAIT_CHUNK_S = 0.1


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


@dataclass
class TaskHandle:
    spec: TaskSpec
    state: TaskState = "pending"
    result: TaskResult | None = None
    last_check: datetime | None = None
    next_due: datetime | None = None
    error: str | None = None


class Planner(Protocol):
    def run(self, instruction: Instruction, execute: Executor) -> TaskResult: ...


class TaskManager:
    def __init__(
        self,
        bus: EventBus,
        planner: Planner,
        execute: Executor,
        *,
        clock: Callable[[], datetime] = datetime.now,
        rng: Callable[[], float] = random.random,
        sleeper: Callable[[float], Awaitable[None]] | None = None,  # default asyncio.sleep
        check_factory: Callable[[MonitorSpec], MonitorCheck] = make_check,
        lease: ForegroundLease | None = None,  # D6: consented screen access (§3.12)
    ) -> None:
        self._bus = bus
        self._planner = planner
        self._execute = execute
        self._clock = clock
        self._rng = rng
        self._sleeper: Callable[[float], Awaitable[None]] = (
            sleeper if sleeper is not None else _default_sleep
        )
        self._check_factory = check_factory
        self._lease = lease
        self._running: dict[str, asyncio.Task[TaskHandle]] = {}
        self._kills: dict[str, threading.Event] = {}
        self._current_id: str | None = None  # the most recently started turn
        # -- v2: resident loop + long-lived loops (all state confined to that loop) --
        self._handles: dict[str, TaskHandle] = {}
        self._loops: dict[str, asyncio.Task[None]] = {}
        self._gates: dict[str, asyncio.Event] = {}  # wakes a loop parked while paused
        self._paused: set[str] = set()
        self._run_now: set[str] = set()
        self._resident_loop: asyncio.AbstractEventLoop | None = None
        self._resident_thread: threading.Thread | None = None
        self._resident_ready = threading.Event()

    # -- resident loop (v2) -------------------------------------------------------

    def start(self) -> None:
        """Start the resident loop thread (idempotent; also lazy-started by register())."""
        if self._resident_thread is not None and self._resident_thread.is_alive():
            return
        self._resident_ready = threading.Event()
        thread = threading.Thread(
            target=self._run_resident, name="marshmellow-tasks", daemon=True
        )
        self._resident_thread = thread
        thread.start()
        if not self._resident_ready.wait(timeout=5.0):
            raise RuntimeError("the task resident loop did not start")

    def stop(self) -> None:
        """Stop every long-lived loop, then the resident loop thread itself.

        App.stop() calls this BEFORE bus.stop(), so the ``cancelled`` events the
        loops emit on the way down still reach their subscribers. Safe to call
        without start() and more than once.
        """
        loop = self._resident_loop
        if loop is None:
            return
        if loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout=5.0)
            except Exception:
                logger.warning("task resident loop did not shut down cleanly", exc_info=True)
        thread = self._resident_thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=5.0)
        if not loop.is_running() and not loop.is_closed():
            loop.close()
        self._resident_loop = None
        self._resident_thread = None

    def _run_resident(self) -> None:
        loop = asyncio.new_event_loop()
        self._resident_loop = loop
        self._resident_ready.set()
        try:
            loop.run_forever()
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()

    async def _shutdown(self) -> None:
        """Cancel every loop and resident turn, then stop the resident loop."""
        resident = asyncio.get_running_loop()
        for loop_task in list(self._loops.values()):
            if not loop_task.done():
                loop_task.cancel()
        for task in list(self._running.values()):
            # Turns submitted on another loop (the voice loop's) belong to that
            # loop; cancelling them from here would not be thread-safe.
            if not task.done() and task.get_loop() is resident:
                task.cancel()
        pending = [t for t in self._loops.values() if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        resident.stop()

    # -- registration & control API (v2) -------------------------------------------

    def register(self, spec: TaskSpec) -> TaskHandle:
        """Register a BACKGROUND task and start its long-lived loop (ValueError otherwise).

        Thread-safe: the loop task is marshaled onto the resident loop, so this
        can be called from planner worker threads and the GUI thread. Returns
        immediately with the handle in state ``pending``; the loop then moves it
        through the TaskState machine. A monitor spec whose check cannot be
        built (``make_check`` ValueError on an unparseable target) raises HERE —
        synchronously, nothing registered — because a failure inside the
        marshaled spawn coroutine would leave the handle a silent "pending"
        zombie (the caller, e.g. the create_task path, can still react). A
        tz-aware scheduled spec is normalized to the naive-local wall clock the
        clock seam produces (the loops compare against ``self._clock()``).
        """
        if spec.mode not in BACKGROUND_MODES:
            raise ValueError(
                f"register() accepts background modes {sorted(BACKGROUND_MODES)}, "
                f"got {spec.mode!r}"
            )
        check: MonitorCheck | None = None
        if spec.mode == "background_monitor":
            if spec.monitor is None:
                raise ValueError("background_monitor requires spec.monitor")
            # Built HERE, on the caller thread, before anything is registered —
            # make_check validates the target format and must fail fast.
            check = self._check_factory(spec.monitor)
        if spec.mode == "scheduled":
            if spec.schedule is None:
                raise ValueError("scheduled requires spec.schedule")
            if spec.schedule.tzinfo is not None:
                # M4-T4 review fix: the loops compare spec.schedule against the
                # NAIVE-LOCAL clock seam (self._clock / datetime.now). A tz-aware
                # schedule ("…Z" / "+hh:mm" — valid ISO-8601 the create_task path
                # accepts) would TypeError _wait_for_runnable's comparison and
                # kill the loop task silently (handle stuck "waiting", no failed
                # event). Normalize to the local wall clock the seam produces —
                # the caller's spec object is not mutated (model_copy).
                spec = spec.model_copy(
                    update={"schedule": spec.schedule.astimezone().replace(tzinfo=None)}
                )
        self.start()
        handle = TaskHandle(spec=spec)
        self._handles[spec.id] = handle
        self._gates[spec.id] = asyncio.Event()
        loop = self._resident_loop
        if loop is None:  # pragma: no cover — start() just created it
            raise RuntimeError("resident loop missing after start()")
        future = asyncio.run_coroutine_threadsafe(self._spawn(handle, check), loop)
        # The future is never dropped: a spawn crash is logged and fails the
        # handle (a dropped concurrent future would swallow it silently).
        future.add_done_callback(lambda f: self._log_spawn_failure(spec.id, f))
        return handle

    async def _spawn(self, handle: TaskHandle, check: MonitorCheck | None) -> None:
        """Start the long-lived loop coroutine for a registered task (resident loop only)."""
        loop = asyncio.get_running_loop()
        spec = handle.spec
        if spec.mode == "background_monitor":
            if check is None:  # pragma: no cover — register() built it
                raise RuntimeError("monitor check missing at spawn")
            loop_task = loop.create_task(self._monitor_loop(handle, check))
        elif spec.mode == "scheduled":
            handle.next_due = spec.schedule
            loop_task = loop.create_task(self._scheduled_loop(handle))
        else:  # background_job: one long turn, started immediately
            loop_task = loop.create_task(self._job_loop(handle))
        self._loops[spec.id] = loop_task

    def _log_spawn_failure(self, task_id: str, future: Future[None]) -> None:
        """Defensive net: a spawn crash is logged and fails a still-pending handle."""
        if future.cancelled():
            return
        exc = future.exception()
        if exc is None:
            return
        logger.error("task %s loop crashed before it could start", task_id, exc_info=exc)
        handle = self._handles.get(task_id)
        if handle is not None and handle.state == "pending":
            handle.error = repr(exc)
            self._emit(task_id, "failed", f"task could not start: {exc}")
            handle.state = "failed"

    def list_tasks(self, state: TaskState | None = None) -> list[TaskHandle]:
        """All registered task handles (registration order); optionally one state only."""
        tasks = list(self._handles.values())
        if state is None:
            return tasks
        return [t for t in tasks if t.state == state]

    def handle(self, task_id: str) -> TaskHandle | None:
        return self._handles.get(task_id)

    def task_mode(self, task_id: str) -> TaskMode | None:
        """The mode of a registered task (None for unknown ids / one-shot submits)."""
        registered = self._handles.get(task_id)
        return registered.spec.mode if registered is not None else None

    def control(self, task_id: str, cmd: Literal["pause", "resume", "cancel", "run_now"]) -> bool:
        """§3.12 control API: False for unknown ids or commands that do not apply.

        Thread-safe: marshals onto the resident loop and blocks until applied —
        never call it from the resident loop thread itself.
        """
        loop = self._resident_loop
        if loop is None or not loop.is_running():
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(self._control(task_id, cmd), loop)
            return future.result(timeout=5.0)
        except Exception:
            logger.warning("control(%s, %s) failed", task_id, cmd, exc_info=True)
            return False

    async def _control(self, task_id: str, cmd: str) -> bool:
        handle = self._handles.get(task_id)
        if handle is None:
            return False
        if cmd == "pause":
            if handle.state in ("completed", "failed", "cancelled"):
                return False
            self._paused.add(task_id)
            handle.state = "paused"
            return True
        if cmd == "resume":
            if handle.state != "paused":
                return False
            self._paused.discard(task_id)
            handle.state = "waiting"
            monitor = handle.spec.monitor
            if monitor is not None:
                # A fresh interval from NOW: a pause is a deliberate freeze, not
                # an outage, so the next poll is one jittered interval out.
                handle.next_due = next_due(
                    monitor.interval_s, now=self._clock(), last=None, rng=self._rng
                )
            self._wake(task_id)
            return True
        if cmd == "run_now":
            if handle.state in ("completed", "failed", "cancelled"):
                return False
            self._run_now.add(task_id)
            self._wake(task_id)
            return True
        if cmd == "cancel":
            loop_task = self._loops.get(task_id)
            if loop_task is None or loop_task.done():
                return False
            kill = self._kills.get(task_id)
            if kill is not None:
                kill.set()  # instant abort for a planner turn running inside the loop
            loop_task.cancel()
            return True
        return False

    def _wake(self, task_id: str) -> None:
        gate = self._gates.get(task_id)
        if gate is not None:
            gate.set()

    # -- one-shot turns (M1/M2 semantics, unchanged) ---------------------------------

    def current_kill(self) -> threading.Event | None:
        """The ACTIVE turn's kill event (None when no turn is running).

        The §3.13 ask channel waits on this during an L3 confirmation: a kill
        during a 20 s spoken question must abort the ask within ~250 ms, and
        ``asyncio.to_thread`` is not interruptible — the event is the fast
        channel. M2's pre-action kill check in the executor seam remains the
        second net. With concurrent turns this is the most recent one (the
        voice loop runs a single turn).
        """
        if self._current_id is None:
            return None
        return self._kills.get(self._current_id)

    async def submit(self, spec: TaskSpec) -> TaskHandle:
        """One-shot foreground turn (M1 semantics)."""
        handle = TaskHandle(spec=spec)
        await self._run_planner_turn(handle)
        return handle

    async def _run_planner_turn(self, handle: TaskHandle) -> None:
        """One planner turn exactly like M1 ``submit``: started → steps → result|failed."""
        spec = handle.spec
        current = asyncio.current_task()
        kill = threading.Event()
        if current is not None:
            self._running[spec.id] = current
        self._kills[spec.id] = kill
        self._current_id = spec.id
        handle.state = "running"
        self._emit(spec.id, "started", spec.instruction)
        try:
            result = await asyncio.to_thread(
                self._planner.run, self._instruction(spec), self._executing(spec, kill)
            )
        except asyncio.CancelledError:
            # Kill switch / barge-in: the turn dies quietly — no result, no failure.
            # The flag makes the kill visible to the worker thread immediately, even
            # if this coroutine has not processed the cancellation yet.
            kill.set()
            logger.info("task %s cancelled", spec.id)
            self._emit(spec.id, "cancelled", spec.instruction)
            handle.state = "cancelled"
            raise
        except Exception as exc:
            logger.exception("task %s crashed", spec.id)
            result = TaskResult(ok=False, summary_to_speak="Something went wrong.",
                                details=repr(exc))
            handle.result = result
            handle.error = repr(exc)
            self._emit(spec.id, "failed", result.summary_to_speak)
            handle.state = "failed"
            return
        finally:
            if self._lease is not None:
                # D6: the turn is over — the screen goes back, granted or not.
                # release() never blocks (this is a LOOP thread; the lease may
                # have an in-flight consent on a worker thread).
                self._lease.release(spec.id)
            self._running.pop(spec.id, None)
            self._kills.pop(spec.id, None)
            if self._current_id == spec.id:
                self._current_id = None  # no active turn → current_kill() is None again
        handle.result = result
        self._emit(spec.id, "result" if result.ok else "failed", result.summary_to_speak)
        handle.state = "completed" if result.ok else "failed"

    def submit_text_background(self, instruction: str) -> asyncio.Task[TaskHandle]:
        """Fire-and-forget submission for the live loop (M2+)."""
        spec = TaskSpec(id=uuid.uuid4().hex[:8], instruction=instruction)
        return asyncio.get_running_loop().create_task(self.submit(spec))

    def cancel_all(self) -> None:
        """Kill switch (§3.13): cancel every running turn and stop its actions NOW.

        v2: long-lived loops are cancelled on the resident loop (thread-safe
        marshal), so "stop everything" stops monitors and scheduled tasks too.
        """
        for task_id, task in list(self._running.items()):
            kill = self._kills.get(task_id)
            if kill is not None:
                kill.set()  # before task.cancel(): visible to the worker thread instantly
            if not task.done():
                task.cancel()
        loop = self._resident_loop
        if loop is not None and loop.is_running() and self._loops:
            asyncio.run_coroutine_threadsafe(self._cancel_loops(), loop)

    async def _cancel_loops(self) -> None:
        for task_id, loop_task in list(self._loops.items()):
            if loop_task.done():
                continue
            kill = self._kills.get(task_id)
            if kill is not None:
                kill.set()
            loop_task.cancel()

    def cancel_task(self, task: asyncio.Task[TaskHandle]) -> None:
        """Cancel one running turn (barge-in / cancel intent) with an instant kill."""
        for task_id, running in list(self._running.items()):
            if running is task:
                kill = self._kills.get(task_id)
                if kill is not None:
                    kill.set()
                if not task.done():
                    task.cancel()
                return

    @staticmethod
    def _instruction(spec: TaskSpec) -> Instruction:
        return Instruction(
            text=spec.instruction,
            transcript=Transcript(text=spec.instruction, source="fixture"),
            context=ContextSnapshot(),
        )

    def _executing(self, spec: TaskSpec, kill: threading.Event) -> Executor:
        task_id = spec.id
        background = spec.mode in BACKGROUND_MODES
        lease_denied = False  # one consent attempt per turn: a refusal is final here

        def run(req: ActionRequest) -> ActionResult:
            nonlocal lease_denied
            if kill.is_set():
                # The turn was killed: refuse instead of executing (and never emit
                # a step event for an action that did not run).
                return ActionResult(
                    ok=False, observation="cancelled: kill switch engaged",
                    error="cancelled",
                )
            if background and req.kind not in READ_KINDS:
                # D6 (§3.12): "No silent UI takeover. Ever." — a background mode
                # touches the screen only through a granted foreground lease.
                # Raised HERE, before the executor seam: the safety gate still
                # sees every granted request; a refusal never runs an action.
                if self._lease is None:
                    return ActionResult(
                        ok=False,
                        observation=(
                            f"refused: {spec.mode} may not touch the screen "
                            "without a foreground lease"
                        ),
                        error="lease_required",
                    )
                if self._lease.held_by() != task_id:
                    if lease_denied:
                        return ActionResult(
                            ok=False,
                            observation="refused: the foreground lease was denied "
                            "for this turn",
                            error="lease_denied",
                        )
                    if not self._lease.request(task_id, req.rationale):
                        lease_denied = True
                        return ActionResult(
                            ok=False,
                            observation="refused: the user did not grant the "
                            "foreground lease",
                            error="lease_denied",
                        )
            self._emit(task_id, "step", f"{req.kind}: {req.rationale}")
            return self._execute(req)

        return run

    # -- long-lived loops (v2) --------------------------------------------------------

    async def _monitor_loop(self, handle: TaskHandle, check: MonitorCheck) -> None:
        """Baseline check → wait for next_due → check → one-shot trigger (§3.12).

        Never emits ``step`` (no executor actions). Check errors emit ``waiting``
        events and fail the task after 5 consecutive errors.
        """
        spec = handle.spec
        task_id = spec.id
        monitor = spec.monitor
        if monitor is None:  # pragma: no cover — register() validated
            return
        try:
            self._emit(task_id, "started", spec.instruction)
            handle.state = "running"
            baseline: str | None = None
            errors = 0
            while True:
                outcome = await asyncio.to_thread(check.check, baseline)
                handle.last_check = self._clock()
                if not outcome.ok:
                    errors += 1
                    handle.error = outcome.error or outcome.summary
                    self._emit(task_id, "waiting", outcome.summary)
                    if errors >= _MAX_CHECK_ERRORS:
                        self._emit(task_id, "failed", outcome.summary)
                        handle.state = "failed"
                        return
                else:
                    errors = 0
                    if baseline is not None and outcome.changed:
                        handle.result = TaskResult(ok=True, summary_to_speak=outcome.summary)
                        self._emit(task_id, "result", outcome.summary)
                        handle.state = "completed"
                        return
                    baseline = outcome.value  # first check captures the baseline
                handle.next_due = next_due(
                    monitor.interval_s, now=self._clock(), last=handle.last_check,
                    rng=self._rng,
                )
                if task_id not in self._paused:
                    handle.state = "waiting"
                await self._wait_for_runnable(handle)
                if task_id not in self._paused:  # a run_now during pause stays "paused"
                    handle.state = "running"
        except asyncio.CancelledError:
            self._mark_cancelled(handle)
            raise

    async def _scheduled_loop(self, handle: TaskHandle) -> None:
        """Wait for ``spec.schedule`` (or run_now), then run the planner turn like submit."""
        try:
            handle.state = "waiting"
            await self._wait_for_runnable(handle)
            await self._run_planner_turn(handle)
        except asyncio.CancelledError:
            self._mark_cancelled(handle)
            raise

    async def _job_loop(self, handle: TaskHandle) -> None:
        """A background job runs its one long turn immediately; its screen
        access is foreground-lease gated (§3.12 D6, enforced in _executing)."""
        try:
            await self._run_planner_turn(handle)
        except asyncio.CancelledError:
            self._mark_cancelled(handle)
            raise

    async def _wait_for_runnable(self, handle: TaskHandle) -> None:
        """Pause-aware wait until the next due instant (or a run_now request).

        Sleeping goes through the injected sleeper in small chunks so pause and
        run_now take effect within one chunk; while paused the loop parks on its
        wake event instead of spinning.
        """
        task_id = handle.spec.id
        gate = self._gates.get(task_id)
        while True:
            if task_id in self._run_now:
                self._run_now.discard(task_id)
                return
            if task_id in self._paused:
                if gate is not None:
                    gate.clear()  # consume stale wakes, then park until the next one
                    await gate.wait()
                else:  # pragma: no cover — gates are created at register()
                    await self._sleeper(_WAIT_CHUNK_S)
                continue
            due = handle.next_due
            now = self._clock()
            if due is not None and is_due(now, due):
                return
            remaining = (due - now).total_seconds() if due is not None else _WAIT_CHUNK_S
            await self._sleeper(min(remaining, _WAIT_CHUNK_S))
            # The injected sleeper may return inline (test fakes); yield anyway so
            # sibling loops and marshaled control() calls always get the loop.
            await asyncio.sleep(0)

    def _mark_cancelled(self, handle: TaskHandle) -> None:
        """Emit + record ``cancelled`` — once (a turn inside the loop may have already)."""
        if handle.state != "cancelled":
            self._emit(handle.spec.id, "cancelled", handle.spec.instruction)
            handle.state = "cancelled"

    def _emit(self, task_id: str, event: TaskEventName, payload: str) -> None:
        self._bus.emit(TaskEvent(task_id=task_id, type=event, payload=payload))
