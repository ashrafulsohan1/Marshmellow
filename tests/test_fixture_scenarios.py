"""Fixture-app scenario suite (M3 Task 7) — 8 real apps through the generic protocol.

One test per YAML: the runner launches the fixture app via
``testing.fixtures.FixtureApp`` (its OWN window only, SP1 rule), runs the REAL
perception (UIAReader over the real tree) and the REAL ActionLayer behind the
real safety gate with the scripted fake provider, then asserts the YAML's
``assert_elements`` entries against a fresh UIA walk of the app's own window
(UIA tree state, never pixels — Review Focus 6) plus the executed trace and the
spoken summary. Each run closes its own window afterwards.

Marker convention (04-tooling): ``scenario`` = the M3 DoD suite (``pytest -m
scenario``), ``manual`` = real desktop needed — deselected by the default gate
and runnable by the agent without a user.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from marshmellow.packs.windows import WindowsPack
from marshmellow.testing.scenario import (
    ScenarioError,
    load_scenario,
    run_fixture,
    scripted_provider,
)
from marshmellow.tts.fake import FakeTTS

SCENARIOS = Path(__file__).parent / "scenarios"

FIXTURE_APPS = [
    "notepad",
    "calculator",
    "explorer",
    "browser",
    "settings",
    "terminal",
    "clock",
    "paint",
]


def _run_fixture(name: str, tmp_path: Path):
    spec = load_scenario(SCENARIOS / f"{name}.yaml")
    provider = scripted_provider(spec)
    report = run_fixture(
        spec,
        provider=provider,
        pack=WindowsPack(),
        tts=FakeTTS(),
        log_dir=tmp_path,
        # generous: cold app starts (Settings, a browser) can exceed 15 s under load
        fixture_timeout=30.0,
    )
    assert report.ok, f"trace={report.trace} spoken={report.spoken!r}"
    return spec, provider, report


# -- loader / schema --------------------------------------------------------------


@pytest.mark.scenario
@pytest.mark.parametrize("alias", FIXTURE_APPS)
def test_load_fixture_scenario_schema(alias: str) -> None:
    spec = load_scenario(SCENARIOS / f"fixture_{alias}.yaml")
    assert spec.mode == f"fixture:{alias}"
    assert spec.instruction.strip()
    assert spec.snapshots is None  # fixture mode: the app IS the state
    assert spec.trace, "every fixture scenario pins an executed trace"
    assert spec.spoken_contains, "every fixture scenario pins the spoken summary"
    assert spec.pin is not None, "app state must be pinned before acting"


@pytest.mark.scenario
def test_loader_rejects_launch_and_snapshots_on_wrong_mode(tmp_path: Path) -> None:
    scripted = (
        "name: x\ninstruction: y\nmode: scripted\nsnapshots: s.json\n"
        "launch: ms-settings:about\n"
    )
    path = tmp_path / "bad.yaml"
    path.write_text(scripted, encoding="utf-8")
    with pytest.raises(ScenarioError):
        load_scenario(path)
    with_snapshots = (
        "name: x\ninstruction: y\nmode: fixture:notepad\nsnapshots: s.json\n"
    )
    path.write_text(with_snapshots, encoding="utf-8")
    with pytest.raises(ScenarioError):
        load_scenario(path)


# -- the runner's failure paths ----------------------------------------------------


@pytest.mark.scenario
@pytest.mark.skipif(sys.platform != "win32", reason="ctypes window enumeration")
def test_run_fixture_fails_cleanly_on_missing_app(tmp_path: Path) -> None:
    yaml_text = (
        "name: missing-app\ninstruction: y\nmode: fixture:notepad\n"
        "pin:\n  role: Edit\n"
        "script:\n  - tool: finish\n    args: {ok: true, summary_to_speak: done}\n"
        "trace:\n  - finish\n"
    )
    path = tmp_path / "missing.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    spec = load_scenario(path)
    launched: list[list[str]] = []
    pack = WindowsPack(
        launcher=lambda argv: launched.append(argv),
        startfile=lambda target: None,
        which=lambda name: "C:/x",
    )
    provider = scripted_provider(spec)
    with pytest.raises(ScenarioError, match="fixture app failed"):
        run_fixture(
            spec,
            provider=provider,
            pack=pack,
            tts=FakeTTS(),
            log_dir=tmp_path,
            fixture_timeout=0.5,
        )
    assert launched, "the pack was asked to open the app before the failure"


# -- the 8-app fixture corpus (real desktop; pytest -m scenario) -------------------


@pytest.mark.scenario
@pytest.mark.manual
@pytest.mark.skipif(sys.platform != "win32", reason="real Windows apps")
def test_fixture_scenario_notepad(tmp_path: Path) -> None:
    _spec, _provider, _report = _run_fixture("fixture_notepad", tmp_path)


@pytest.mark.scenario
@pytest.mark.manual
@pytest.mark.skipif(sys.platform != "win32", reason="real Windows apps")
def test_fixture_scenario_calculator(tmp_path: Path) -> None:
    _spec, _provider, _report = _run_fixture("fixture_calculator", tmp_path)


@pytest.mark.scenario
@pytest.mark.manual
@pytest.mark.skipif(sys.platform != "win32", reason="real Windows apps")
def test_fixture_scenario_explorer(tmp_path: Path) -> None:
    _spec, _provider, _report = _run_fixture("fixture_explorer", tmp_path)


@pytest.mark.scenario
@pytest.mark.manual
@pytest.mark.skipif(sys.platform != "win32", reason="real Windows apps")
def test_fixture_scenario_browser(tmp_path: Path) -> None:
    _spec, _provider, _report = _run_fixture("fixture_browser", tmp_path)


@pytest.mark.scenario
@pytest.mark.manual
@pytest.mark.skipif(sys.platform != "win32", reason="real Windows apps")
def test_fixture_scenario_settings(tmp_path: Path) -> None:
    _spec, _provider, _report = _run_fixture("fixture_settings", tmp_path)


@pytest.mark.scenario
@pytest.mark.manual
@pytest.mark.skipif(sys.platform != "win32", reason="real Windows apps")
def test_fixture_scenario_terminal(tmp_path: Path) -> None:
    _spec, _provider, _report = _run_fixture("fixture_terminal", tmp_path)


@pytest.mark.scenario
@pytest.mark.manual
@pytest.mark.skipif(sys.platform != "win32", reason="real Windows apps")
def test_fixture_scenario_clock(tmp_path: Path) -> None:
    _spec, _provider, _report = _run_fixture("fixture_clock", tmp_path)


@pytest.mark.scenario
@pytest.mark.manual
@pytest.mark.skipif(sys.platform != "win32", reason="real Windows apps")
def test_fixture_scenario_paint(tmp_path: Path) -> None:
    spec, provider, report = _run_fixture("fixture_paint", tmp_path)
    # Open + read-only with ZERO VLM calls: no vision perceiver is wired, so the
    # sparse-tree threshold path degrades to a UIA-only snapshot (D5). SP1's
    # survey called Paint UIA-sparse; this build's Paint exposes a rich ribbon
    # (the count below records what the planner's lens actually saw).
    assert provider.vision_requests == []
    assert report.element_count is not None and report.element_count > 0
    assert spec.launch is None  # paint opens via the plain mspaint.exe alias
