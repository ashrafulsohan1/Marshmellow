
from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    ContextSnapshot,
    Instruction,
    SpeakRequest,
    Transcript,
)
from marshmellow.llm.fake import FakeLLM
from marshmellow.planner.fake import FakePlanner
from marshmellow.stt.fake import FakeSTT
from marshmellow.tts.fake import FakeTTS


def _instruction(text: str) -> Instruction:
    return Instruction(
        text=text, transcript=Transcript(text=text, source="fixture"),
        context=ContextSnapshot(),
    )


def test_fake_stt_transcribe_text() -> None:
    t = FakeSTT().transcribe_text("open notepad")
    assert t.text == "open notepad"
    assert t.source == "fixture"
    assert t.confidence == 1.0


def test_fake_tts_records_speak_requests() -> None:
    tts = FakeTTS()
    tts.say(SpeakRequest(text="On it.", priority="ack"))
    tts.say(SpeakRequest(text="Done.", priority="result", interrupt=True))
    assert [s.text for s in tts.spoken] == ["On it.", "Done."]
    assert tts.spoken[1].interrupt is True
    tts.stop()  # must be a safe no-op


def test_fake_llm_replays_script_then_none() -> None:
    action = ActionRequest(
        kind="shell", payload={"open_app": "notepad"}, rationale="r", risk="L1"
    )
    llm = FakeLLM(script=[action])
    assert llm.next_action() == action
    assert llm.next_action() is None


def test_fake_planner_open_instruction() -> None:
    planner = FakePlanner()
    actions: list[ActionRequest] = []

    def execute(req: ActionRequest) -> ActionResult:
        actions.append(req)
        return ActionResult(ok=True, observation="window appeared")

    result = planner.run(_instruction("open notepad please"), execute)
    assert len(actions) == 1
    assert actions[0].kind == "shell"
    assert actions[0].payload["open_app"] == "notepad"
    assert actions[0].risk == "L1"
    assert result.ok
    assert "notepad" in result.summary_to_speak.lower()


def test_fake_planner_failure_speaks_failure() -> None:
    planner = FakePlanner()
    result = planner.run(
        _instruction("open zappt"), lambda req: ActionResult(
            ok=False, observation="no such app"
        )
    )
    assert not result.ok
    assert "zappt" in result.summary_to_speak.lower()


def test_fake_planner_chat_needs_no_screen() -> None:
    planner = FakePlanner()
    actions: list[ActionRequest] = []
    result = planner.run(
        _instruction("tell me a joke"),
        lambda req: actions.append(req) or ActionResult(ok=True, observation=""),  # type: ignore[func-returns-value]
    )
    assert actions == []
    assert result.ok
