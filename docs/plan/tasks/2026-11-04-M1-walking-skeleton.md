# M1 Walking Skeleton — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline — M1 is integration-heavy and `contracts.py` is main-agent-only per 03-workflow). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** one command runs the app (tray + mascot), a scripted instruction flows through fake STT/LLM to the real Windows DE pack (Notepad opens), and the whole thing is proven by headless deterministic tests.

**Architecture:** composition root (`app.py`) wires real-vs-fake implementations from config; EventBus decouples the engine from UI; UI is a subscriber only. Contracts are fixed by `core/contracts.py` before any module work starts.

**Tech Stack:** Python 3.12 (uv-managed) · PySide6 · pydantic v2 · pytest · ruff · mypy (core only)

**Spec:** [PLAN.md](../../PLAN.md) · [01-architecture.md](../01-architecture.md) · [02-milestones.md](../02-milestones.md) (M1 section)

## Global Constraints

- Python 3.12 via uv; src layout `src/marshmellow/…`; tests in `tests/` mirroring packages.
- `ruff format && ruff check` clean; `mypy src/marshmellow/core` clean; `pytest -m "not manual"` green before every commit.
- Tests: no wall-clock sleeps; `QT_QPA_PLATFORM=offscreen` for Qt tests; markers `manual` and `scenario` registered in pyproject.
- `core/contracts.py` follows the v1 sketch in 01-architecture §4 exactly; only the main agent edits it.
- Conventional commits (`feat:`, `chore:`, `test:`).
- Nothing here talks to a network: no LLM/STT/TTS API calls in M1 (fakes only).

## Review Focus

1. **Pre-existing windows touched** (SP1 incident): DE-pack `open_app` must only ever address windows it launched — test: launch, capture new hwnd, assert a *second* identical instruction targets a *different* hwnd; never match by title against existing windows.
2. **EventBus handler exceptions swallowed:** a raising subscriber must be logged and must not prevent other subscribers — test both.
3. **Headless Qt flakiness:** mascot/tray tests run offscreen with `processEvents()` determinism, zero sleeps — CI-stable by construction.
4. **Config drift:** one pydantic `Settings` model is the only schema; TOML round-trip test pins it.
5. **Secrets in logs:** the JSONL logger must never emit typed text / transcript content at INFO — scrubber test with a fake secret.

---

### Task 1: Repo scaffold

**Files:** Create `pyproject.toml`, `src/marshmellow/__init__.py`, `src/marshmellow/py.typed`, `tests/test_smoke.py`, `tests/conftest.py`.

**Interfaces:** Produces importable package `marshmellow` (`__version__ = "0.1.0"`), pytest markers `manual`, `scenario`.

- [ ] Step 1: write failing test `tests/test_smoke.py` — `import marshmellow; assert marshmellow.__version__`
- [ ] Step 2: run `uv run pytest tests/test_smoke.py` → FAIL (no package)
- [ ] Step 3: write `pyproject.toml` (project, deps: `pydantic>=2`, `PySide6>=6.8`; dev deps: pytest, ruff, mypy; `[tool.ruff]` line-length 100; `[tool.pytest.ini_options]` markers) + package init + empty `conftest.py`
- [ ] Step 4: `uv run pytest tests/test_smoke.py` → PASS; `uv run ruff check .` → clean
- [ ] Step 5: commit `chore: M1 scaffold (uv, pytest, ruff, package skeleton)`

### Task 2: core contracts

**Files:** Create `src/marshmellow/core/contracts.py`, `tests/core/test_contracts.py`.

**Interfaces:** Produces every model from 01-architecture §4 verbatim: `WindowInfo, SpeechSegment, Transcript, ControlIntent, ContextSnapshot, Instruction, Rect, Element, PerceptionSnapshot, ActionRequest, ActionResult, TaskResult, MonitorSpec, TaskSpec, TaskEvent, SpeakRequest`, type aliases `ActivationMode, ElementAction, ActionKind, RiskLevel, TaskMode`.

- [ ] Step 1: failing tests — Element requires `ref/role/name`; ActionRequest rejects unknown `kind`; TaskSpec default mode is `foreground`; SpeakRequest priority default `result`
- [ ] Step 2: `uv run pytest tests/core/test_contracts.py` → FAIL
- [ ] Step 3: implement contracts.py (pydantic v2, from 01 §4 sketch)
- [ ] Step 4: run → PASS
- [ ] Step 5: commit `feat: core contracts v1 (01-architecture §4)`

### Task 3: EventBus

**Files:** Create `src/marshmellow/core/bus.py`, `tests/core/test_bus.py`.

**Interfaces:** Produces `class EventBus: def subscribe(self, etype, handler) -> Unsubscribe; def emit(self, event) -> None; def emit_threadsafe(self, event) -> None` — sync dispatch, handler exceptions isolated + logged, `emit_threadsafe` for marshalling from non-loop threads (UIA, Qt).

- [ ] Step 1: failing tests — (a) two subscribers both receive; (b) raising subscriber logged, second subscriber still runs; (c) `emit_threadsafe` from a thread delivers (poll with injected-condition, no sleep)
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (plain sync dispatch + queue drain via `emit`; `logging` module logger)
- [ ] Step 4: run → PASS
- [ ] Step 5: commit `feat: EventBus with error isolation and threadsafe emit`

### Task 4: config

**Files:** Create `src/marshmellow/core/config.py`, `tests/core/test_config.py`.

**Interfaces:** Produces `Settings` (pydantic-settings TOML): `activation: ActivationMode = "both"`, `autostart: bool = False`, `providers.llm.kind = "fake"`, `safety.preset = "standard"`, `ui.mascot…`; `load_settings(path|None) -> Settings`, `settings.save(path)`.

- [ ] Step 1: failing tests — defaults when file missing; TOML round-trip (save→load equal); malformed TOML → defaults + logged warning
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement
- [ ] Step 4: run → PASS
- [ ] Step 5: commit `feat: typed config with TOML round-trip`

### Task 5: structured logging

**Files:** Create `src/marshmellow/core/logging.py`, `tests/core/test_logging.py`.

**Interfaces:** Produces `setup_logging(log_dir: Path) -> logging.Logger` — JSONL per session, fields `{ts, level, name, msg, extra}`; scrubbing filter redacts configured secret patterns.

- [ ] Step 1: failing tests — JSONL line parses; scrubber removes `password=…` style matches; `setup_logging` is idempotent
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (stdlib logging + JSON formatter + filter)
- [ ] Step 4: run → PASS
- [ ] Step 5: commit `feat: JSONL structured logging with scrubbing`

### Task 6: fakes (STT/TTS/LLM/planner-script)

**Files:** Create `src/marshmellow/stt/fake.py`, `src/marshmellow/tts/fake.py`, `src/marshmellow/llm/fake.py`, `src/marshmellow/planner/fake.py`, `tests/test_fakes.py`.

**Interfaces:** `FakeSTT.transcribe_text(text) -> Transcript` · `FakeTTS` records `SpeakRequest`s (`self.spoken: list[SpeakRequest]`) · `FakeLLM` replays scripted `ActionRequest` sequences · `FakePlanner.run(instruction, actions: ActionLayer-like) -> TaskResult` — keyword-driven M1 stub: instruction containing "open X" → `ActionRequest(kind="shell", payload={"open_app": "X"})` then `done`.

- [ ] Step 1: failing tests — FakeSTT/Transcript round-trip; FakeTTS records interrupt+priority; FakePlanner emits shell action for "open notepad" and done for "hello"
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement
- [ ] Step 4: run → PASS
- [ ] Step 5: commit `feat: deterministic fakes for stt/tts/llm/planner`

### Task 7: DE pack (Windows, open-app only)

**Files:** Create `src/marshmellow/packs/base.py`, `src/marshmellow/packs/windows.py`, `tests/packs/test_windows_pack.py`.

**Interfaces:** `class DEShell(Protocol): open_app(target) -> Result; detect_de() -> str` · `WindowsPack.open_app`: alias table (`browser, notepad, calculator, explorer, terminal, settings`) → `os.startfile`/`subprocess.Popen`; returns `Result(ok, hwnd|None, detail)`; **only records/uses windows created after the call** (SP1 rule).

- [ ] Step 1: failing tests — alias resolution maps `notepad→notepad.exe`, `browser→default via startfile` (unit, monkeypatched launcher); unknown target → `Result(ok=False)`; integration `tests/packs/test_windows_pack.py::test_open_and_close_notepad` marked `manual` opens real Notepad, asserts new hwnd, closes it
- [ ] Step 2: run unit → FAIL
- [ ] Step 3: implement
- [ ] Step 4: run unit → PASS; run `pytest -m manual tests/packs` locally → PASS (real window opened+closed)
- [ ] Step 5: commit `feat: Windows DE pack open_app (new-windows-only rule)`

### Task 8: TaskManager (foreground path only)

**Files:** Create `src/marshmellow/tasks/manager.py`, `tests/tasks/test_manager.py`.

**Interfaces:** Produces `class TaskManager: def __init__(self, bus, planner, actions); def submit(self, spec: TaskSpec) -> TaskHandle` — runs the planner, emits `TaskEvent(started/step/result/failed)`, returns handle with `result: TaskResult | None`.

- [ ] Step 1: failing test — scripted planner + stub actions: submit → events sequence `[started, step, result]`, handle.result.ok
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (asyncio task per submission; M1 = foreground only)
- [ ] Step 4: run → PASS
- [ ] Step 5: commit `feat: TaskManager foreground path with event trace`

### Task 9: mascot + tray (offscreen-testable)

**Files:** Create `src/marshmellow/ui/mascot.py`, `src/marshmellow/ui/tray.py`, `tests/ui/test_ui.py`.

**Interfaces:** `Mascot(QWidget)`: frameless/always-on-top/transparent, `set_state(state: Literal["idle","listening","thinking","acting","speaking","error","muted"])`, click-through when idle, `MascotClicked` on bus when clicked; `Tray(QObject)`: `QSystemTrayIcon` + menu (mute, open UI stub, quit) publishing to bus.

- [ ] Step 1: failing tests (offscreen) — widget constructs with all 7 states; `set_state` updates accessible property; tray constructs with menu actions wired to bus events
- [ ] Step 2: run with `QT_QPA_PLATFORM=offscreen` → FAIL
- [ ] Step 3: implement (placeholder art: rounded marshmallow shape drawn in `paintEvent`; real assets per OQ-3 later)
- [ ] Step 4: run → PASS
- [ ] Step 5: commit `feat: mascot overlay states + tray (placeholder art)`

### Task 10: composition root + simulator CLI + E2E

**Files:** Create `src/marshmellow/app.py`, `src/marshmellow/tools/simulate.py`, `tests/test_e2e_skeleton.py`.

**Interfaces:** `class App: def __init__(self, settings, headless: bool = False); def start(); def stop()` — wires bus, logging, fakes, WindowsPack, TaskManager, and (if not headless) mascot+tray. `python -m marshmellow.tools.simulate "open notepad"` builds `Instruction` via FakeSTT → TaskManager → prints event trace + spoken lines.

- [ ] Step 1: failing E2E (`scenario` marker, headless) — `App(settings, headless=True)`.start → submit `TaskSpec(instruction="open notepad", mode="foreground")` → assert TaskEvents `[started, step, result]` + FakeTTS captured a `SpeakRequest` whose text mentions success + WindowsPack was invoked with target notepad (fake launcher in this test — the *real* window is Task 7's manual test)
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement app.py + simulate.py (thin: parse arg → FakeSTT → submit; print trace)
- [ ] Step 4: `uv run pytest -m scenario` → PASS; `uv run ruff check .` + `uv run mypy src/marshmellow/core` → clean
- [ ] Step 5: run `uv run python -m marshmellow.tools.simulate "open notepad"` (real) → Notepad opens, console shows event trace, then close it
- [ ] Step 6: commit `feat: M1 walking skeleton — E2E scripted instruction opens Notepad`

### Task 11: M1 DoD evidence + docs touch

- [ ] Step 1: full gate: `uv run pytest -m "not manual" && uv run ruff check . && uv run mypy src/marshmellow/core` — paste output as DoD evidence in 02-milestones.md M1 section
- [ ] Step 2: commit `docs: M1 DoD evidence`

## Execution notes

- Tasks 1→2→3→4→5 are strictly sequential (later tasks import earlier ones).
- Tasks 6/7 can parallelize *after* Task 2 (both consume contracts only) — but with the measured cap of 4 subagents and M1 being small, inline sequential is cheaper than brief-writing; deviate only if a task stalls.
- Tasks 8–10 depend on 3+6+7 (manager needs planner+actions+bus).
