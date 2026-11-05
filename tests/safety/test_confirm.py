"""ConfirmFlow tests — explicit yes whitelist, re-ask once, tray fallback, fail closed."""

from collections.abc import Callable

from marshmellow.safety.confirm import ConfirmFlow

QUESTION = "Delete 6 items — confirm?"


def scripted_ask(*answers: str) -> tuple[list[str], list[str], Callable[[str], str]]:
    """Returns (asked questions, answers handed out, the ask seam)."""
    questions: list[str] = []
    pending = list(answers)

    def ask(question: str) -> str:
        questions.append(question)
        return pending.pop(0) if pending else ""

    return questions, list(answers), ask


def test_explicit_yes_whitelist_confirms_immediately() -> None:
    for answer in ("yes", "YES", "  okay!  ", "go ahead", "sure", "yep", "do it", "confirm"):
        questions, _answers, ask = scripted_ask(answer)
        assert ConfirmFlow(ask=ask).confirm(QUESTION) is True, answer
        assert questions == [QUESTION], "an explicit yes must not re-ask"


def test_explicit_no_refuses_without_reasking() -> None:
    for answer in ("no", "nope", "cancel", "stop"):
        questions, _answers, ask = scripted_ask(answer)
        assert ConfirmFlow(ask=ask).confirm(QUESTION) is False, answer
        assert questions == [QUESTION], "an explicit no must not re-ask"


def test_ambiguous_answer_is_reasked_once_then_confirmed() -> None:
    questions, _answers, ask = scripted_ask("uh", "yes")
    assert ConfirmFlow(ask=ask).confirm(QUESTION) is True
    assert questions == [QUESTION, QUESTION], "one re-ask after an ambiguous answer"


def test_ambiguous_after_reask_consults_tray_fallback() -> None:
    questions, _answers, ask = scripted_ask("uh", "")
    fallback_questions: list[str] = []

    def fallback(question: str) -> bool:
        fallback_questions.append(question)
        return False

    flow = ConfirmFlow(ask=ask, fallback=fallback)
    assert flow.confirm(QUESTION) is False
    assert questions == [QUESTION, QUESTION], "re-asked exactly once"
    assert fallback_questions == [QUESTION], "the tray fallback gets the original question"


def test_no_fallback_fails_closed() -> None:
    # "yesterday" contains "yes" — substring-guessing must never confirm.
    questions, _answers, ask = scripted_ask("uh", "yesterday")
    assert ConfirmFlow(ask=ask).confirm(QUESTION) is False
    assert len(questions) == 2


def test_tray_yes_is_an_explicit_confirmation() -> None:
    questions, _answers, ask = scripted_ask("uh", "uh")
    flow = ConfirmFlow(ask=ask, fallback=lambda question: True)
    assert flow.confirm(QUESTION) is True
    assert len(questions) == 2


def test_silence_and_garbage_never_confirm() -> None:
    questions, _answers, ask = scripted_ask("", "mmm", "maybe not sure")
    flow = ConfirmFlow(ask=ask, fallback=lambda question: False)
    assert flow.confirm(QUESTION) is False
    assert len(questions) == 2, "max_reasks=1: exactly one re-ask, then the fallback"


def test_zero_reasks_goes_straight_to_fallback() -> None:
    questions, _answers, ask = scripted_ask("uh")
    flow = ConfirmFlow(ask=ask, fallback=lambda question: False, max_reasks=0)
    assert flow.confirm(QUESTION) is False
    assert questions == [QUESTION]
