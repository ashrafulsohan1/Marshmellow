
from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    TaskEvent,
    TaskSpec,
)
from marshmellow.planner.fake import FakePlanner
from marshmellow.tasks.manager import TaskManager


def _manager(bus: EventBus, executed: list[ActionRequest], ok: bool = True) -> TaskManager:
    def execute(req: ActionRequest) -> ActionResult:
        executed.append(req)
        return ActionResult(ok=ok, observation="window appeared" if ok else "no such app")

    return TaskManager(bus=bus, planner=FakePlanner(), execute=execute)


async def test_submit_runs_planner_and_emits_event_trace() -> None:
    bus = EventBus()
    events: list[TaskEvent] = []
    bus.subscribe(TaskEvent, events.append)
    executed: list[ActionRequest] = []
    manager = _manager(bus, executed)

    handle = await manager.submit(TaskSpec(id="t1", instruction="open notepad"))

    assert handle.result is not None and handle.result.ok
    assert executed and executed[0].payload["open_app"] == "notepad"
    assert [e.type for e in events if e.task_id == "t1"] == ["started", "step", "result"]
    bus.stop()


async def test_failed_task_emits_failed_event_and_speaks_failure() -> None:
    bus = EventBus()
    events: list[TaskEvent] = []
    bus.subscribe(TaskEvent, events.append)
    manager = _manager(bus, executed=[], ok=False)

    handle = await manager.submit(TaskSpec(id="t2", instruction="open zappt"))

    assert handle.result is not None and not handle.result.ok
    assert [e.type for e in events if e.task_id == "t2"] == ["started", "step", "failed"]
    bus.stop()


async def test_chat_instruction_has_no_step_event() -> None:
    bus = EventBus()
    events: list[TaskEvent] = []
    bus.subscribe(TaskEvent, events.append)
    manager = _manager(bus, executed=[])

    handle = await manager.submit(TaskSpec(id="t3", instruction="tell me a joke"))

    assert handle.result is not None and handle.result.ok
    assert [e.type for e in events if e.task_id == "t3"] == ["started", "result"]
    bus.stop()
