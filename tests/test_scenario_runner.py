"""Scripted scenario runner (M3 Task 6) — YAML-driven headless end-to-end runs.

Each scenario drives the FULL production loop headlessly: App(headless=True)
over canned UIA states (the real ScreenPerception + real reader over a canned
tree source), the real safety gate in front of a recording action layer, and
the scripted fake provider built from the YAML's ``script`` — zero real
provider calls are possible (the fake is in-process; vision calls hit the same
fake and are counted on it).

The scenario pairs carry the DoD teeth:

- reference: the exact executed trace plus the channel answer travels
  canned page → planner → spoken summary;
- vision-fallback: the SoM reply's value exists NOWHERE except the fake
  provider's vision answer, so the spoken evidence proves vision refs were
  used, and the counted vision requests prove the provider is the fake;
- l3-confirm-yes / l3-confirm-no: same fixture, same ref, same kind — the only
  variable is the scripted confirm answer. The pair proves the gate asked (the
  "no" run is refused, so a gate that never asked would have executed there)
  and that "yes" executes the act afterwards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marshmellow.packs.base import ShellResult
from marshmellow.testing.scenario import (
    ScenarioError,
    load_scenario,
    load_snapshots,
    run_scripted,
    scripted_provider,
)
from marshmellow.tts.fake import FakeTTS

SCENARIOS = Path(__file__).parent / "scenarios"


class FakePack:
    """DEShell double: records open_app calls, opens nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def open_app(self, target: str) -> ShellResult:
        self.calls.append(target)
        return ShellResult(ok=True, detail=f"would open {target}")


def _run(name: str, tmp_path: Path):
    spec = load_scenario(SCENARIOS / f"{name}.yaml")
    provider = scripted_provider(spec)
    report = run_scripted(
        spec,
        snapshots=load_snapshots(spec.snapshots),
        provider=provider,
        pack=FakePack(),
        tts=FakeTTS(),
        log_dir=tmp_path,
    )
    return spec, provider, report


# -- loader ---------------------------------------------------------------------


@pytest.mark.scenario
def test_load_scenario_roundtrip() -> None:
    spec = load_scenario(SCENARIOS / "reference-check-channel.yaml")
    assert spec.name == "reference-check-channel"
    assert spec.mode == "scripted"
    assert spec.instruction.strip()
    assert spec.snapshots is not None and spec.snapshots.is_absolute()
    assert [call.tool for call in spec.script] == [
        "get_elements", "act", "get_elements", "finish",
    ]
    assert spec.trace == ["get_elements", "act:uia_invoke", "get_elements", "finish"]
    assert spec.spoken_contains == ["5 videos this week"]


@pytest.mark.scenario
@pytest.mark.parametrize(
    ("yaml_text", "why"),
    [
        ("- just a list\n", "non-mapping document"),
        ("name: x\ninstruction: y\nmode: nonsense\n", "unpinned mode value"),
        (
            "name: x\ninstruction: y\nmode: scripted\nbogus: 1\n",
            "unknown top-level key",
        ),
        ("instruction: y\nmode: scripted\n", "missing name"),
        ("name: x\nmode: scripted\n", "missing instruction"),
        ("name: x\ninstruction: y\nmode: scripted\n", "scripted without snapshots"),
        (
            "name: x\ninstruction: y\nmode: scripted\nsnapshots: s.json\n"
            "script:\n  - args: {}\n",
            "script entry without a tool",
        ),
        (
            "name: x\ninstruction: y\nmode: scripted\nsnapshots: s.json\n"
            "script:\n  - tool: ask_user\n    args: {}\n",
            "ask_user entry without a scripted answer",
        ),
        (
            "name: x\ninstruction: y\nmode: scripted\nsnapshots: s.json\n"
            "script:\n  - tool: vision\n    args: {}\n",
            "vision entry without a reply",
        ),
        (
            "name: x\ninstruction: y\nmode: scripted\nsnapshots: s.json\n"
            "trace:\n  - act:NOT A KIND\n",
            "malformed trace entry",
        ),
    ],
)
def test_loader_rejects_malformed_yaml(tmp_path: Path, yaml_text: str, why: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ScenarioError):
        load_scenario(path)
    with pytest.raises(ScenarioError):
        load_scenario(tmp_path / "does-not-exist.yaml")


# -- the four scripted scenarios -------------------------------------------------


@pytest.mark.scenario
def test_reference_scenario_runs_green(tmp_path: Path) -> None:
    spec, provider, report = _run("reference-check-channel", tmp_path)
    assert report.ok, f"trace={report.trace} spoken={report.spoken!r}"
    assert report.trace == ["get_elements", "act:uia_invoke", "get_elements", "finish"]
    # The channel answer exists only in canned state 2 (after the invoke):
    # speaking it proves the verify re-read saw the acted-on page.
    assert "5 videos this week" in report.spoken
    assert provider.vision_requests == []  # no vision provider configured → zero VLM calls


@pytest.mark.scenario
def test_vision_fallback_uses_fake_provider(tmp_path: Path) -> None:
    spec, provider, report = _run("vision-fallback", tmp_path)
    assert report.ok, f"trace={report.trace} spoken={report.spoken!r}"
    assert report.trace == ["get_elements", "act:click", "finish"]
    # Exactly one vision pass, served by the in-process fake (zero real calls
    # possible): the request carried the marked SoM image.
    assert len(provider.vision_requests) == 1
    image = provider.vision_requests[0].messages[-1].images[0]
    assert image.startswith("data:image/png;base64,")
    # "12,408 views" exists only in the SoM reply → the planner used vision refs.
    assert "12,408 views" in report.spoken


@pytest.mark.scenario
def test_l3_confirm_no_refuses_and_never_acts(tmp_path: Path) -> None:
    spec, provider, report = _run("l3-confirm-no", tmp_path)
    assert report.ok, f"trace={report.trace} spoken={report.spoken!r}"
    # The act tool call was made but REFUSED: the executed trace has no act.
    assert report.trace == ["get_elements", "finish"]
    assert "did not" in report.spoken.lower()
    assert "password" in report.spoken.lower()


@pytest.mark.scenario
def test_l3_confirm_yes_executes_act_after_confirmation(tmp_path: Path) -> None:
    spec, provider, report = _run("l3-confirm-yes", tmp_path)
    assert report.ok, f"trace={report.trace} spoken={report.spoken!r}"
    # Same fixture/ref/kind as the "no" twin — there the act never ran, so the
    # only variable is the scripted "yes": it executed the act.
    assert report.trace == ["get_elements", "act:uia_set_value", "finish"]
    assert "password field" in report.spoken.lower()
