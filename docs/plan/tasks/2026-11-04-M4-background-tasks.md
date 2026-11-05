# M4 Background Tasks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline — the lease protocol and TaskManager state machine are safety-critical, cross-module integration work per 03-workflow §1). Steps use checkbox (`- [ ]`) syntax.

**Goal:** "watch this channel and tell me when they upload" works end to end — a monitor runs unattended, reports unprompted by voice, survives a restart without re-reporting, and is visible/pausable in the dashboard — while background tasks can **never** touch the screen without a user-consented foreground lease.

**Architecture:** deterministic monitors (url_diff/api_field/file_hash/rss — no LLM per tick, D4) + a jittered scheduler feed a TaskManager v2 that gains long-lived task loops on a resident asyncio thread, a `TaskState` machine, and the §3.12 control API (`list_tasks`/`control`). The LLM creates background tasks through a new `create_task` tool call routed at the executor seam; a `ForegroundLease` gates every screen-touching action from background modes behind spoken consent (D6, §3.12); a JSON `TaskStore` persists monitors + baselines across restart (§3.15 posture); the dashboard is a thin EventBus subscriber over Qt Widgets (§3.11).

**Tech Stack:** adds `feedparser` (BSD-2, RSS/Atom robustness — adopt over hand-parsing per PLAN §5); `httpx` (already a direct dep) for url_diff/api_field with `MockTransport` tests; stdlib `hashlib`, `asyncio`, `xml` untouched.

**Spec:** [02-milestones.md M4](../02-milestones.md) · [01-architecture.md §§3.5, 3.11, 3.12, 3.13, 3.15](../01-architecture.md) · decisions D1/D4/D5/D6 (+ D2/D3/D7/D8 untouched by M4).

## Global Constraints

- All M3 gates stay green: `uv run pytest -q` (baseline verified 2026-11-06: **318 passed, 17 deselected**) · `uv run ruff check .` · `uv run mypy` (29 files strict-clean) — pytest un-piped.
- **§3.13 is non-negotiable:** every `ActionRequest` still passes `SafetyGate`; the lease guard is *additive* (task-layer policy) and never lowers a risk level; L3 always confirms; the kill probe and pre-action kill check are untouched.
- **D6 DoD, test-enforced:** background tasks never synthesize input without a granted foreground lease (`lease_required`/`lease_denied` refusals are `ActionResult(ok=False)`).
- **No per-app integrations** — monitors are exactly the four generic `MonitorSpec.kind` values; the watch-channel E2E goes through them, never bespoke code.
- **No wall-clock waits in tests** (02-milestones determinism rules): injected `clock`/`rng`/`sleeper` seams; condition-bounded waits only (existing `tests/` convention).
- Contract changes = 01-architecture changelog entry **+ fakes bumped in the same commit** (03-workflow rule 3). Planned: `TaskState`, `ActionKind += "create_task"` (Tasks 2, 4).
- `marshmellow.tasks` joins the `[tool.mypy] packages` list in Task 2 (verified strict-clean today: `uv run mypy --strict src/marshmellow/tasks` → 1 file, no issues).
- New deps land in pyproject with the task that needs them, license noted in 04-tooling (feedparser BSD-2 in Task 1; zero others planned).
- **Pinned conventions (spec-silent choices, binding for all tasks):**
  - First check at registration captures the **baseline** and never triggers (`baseline=None` → `changed` ignored).
  - A monitor task **completes after its first trigger** (one-shot; recurring watching is out of scope).
  - Check errors emit `TaskEvent("waiting")`, are **never spoken**; 5 consecutive → task `failed` (spoken once, `background` priority).
  - `cancel_all` cancels long-lived loops too; `cancelled`/`completed`/`failed` tasks are **not persisted**; only `background_monitor` + `scheduled` survive restart (jobs die with the process, logged).
  - `control(...) -> bool` (§3.12 sketch's `Result` has no contracts type; bool is the pinned shape).
  - `api_field` target format: `<url>#<dotted.path>`; `url_diff`/`rss`: plain URL; `file_hash`: local path.
  - Monitor summary formats (the spoken report): rss `new upload: {title}` · url_diff `the watched page changed` · api_field `{path} is now {value}` · file_hash `the watched file changed`.
  - Scheduler jitter: `jitter_s = (rng() * 2 - 1) * interval_s * 0.1` (uniform ±10 %).
  - LLM-created monitors need `interval_s >= 30` (no hot loops from the model; `MonitorSpec` itself stays `gt=0`).
  - Lease `READ_KINDS = frozenset({"wait", "ask_user", "done", "fail", "create_task"})` — the only executor actions a background mode may run leaseless.

## Review Focus

1. **Silent UI takeover by a background task** (§3.12: "No silent UI takeover. Ever.") — a background job acting on screen must first ask, out loud, and get a yes. → Task 3: `test_background_action_without_lease_refused`, `test_denial_remembered_for_turn` (and the lease-None fail-closed case).
2. **False re-report after restart** — a lost baseline would re-announce the same upload on every app start (spam = trust death). → Task 5: `test_restart_does_not_re_report`.
3. **Check storm** — a permanently failing check (bad URL) must not spin the loop or spam the user; waits, logs, then one spoken failure. → Task 2: `test_check_errors_wait_then_fail`.
4. **Kill switch not reaching background loops** — "stop everything" must stop monitors, not just foreground turns. → Task 2: `test_cancel_all_stops_long_lived_loops`.
5. **GUI-thread violations from the dashboard** — pause/resume clicked in Qt must never call into asyncio from the GUI thread (freeze/crash). → Task 6: offscreen `test_dashboard_pause_button_calls_control` (control marshals to the task loop).

---

### Task 1: Monitor checks + jittered scheduler

**Files:**
- Create: `src/marshmellow/tasks/monitors.py`, `src/marshmellow/tasks/scheduler.py`
- Test: `tests/tasks/test_monitors.py`, `tests/tasks/test_scheduler.py`
- Modify: `pyproject.toml` (add `feedparser>=6.0`), `docs/plan/04-tooling.md` (license row: BSD-2)

**Interfaces:**
- Consumes: `MonitorSpec` (`core/contracts.py:134`) unchanged.
- Produces:
```python
# monitors.py
@dataclass(frozen=True)
class CheckResult:
    ok: bool; value: str; changed: bool; summary: str; error: str | None = None
class MonitorCheck(Protocol):
    def check(self, baseline: str | None) -> CheckResult: ...
def make_check(spec: MonitorSpec, *, client: httpx.Client | None = None) -> MonitorCheck  # ValueError on unparseable target
# scheduler.py
def next_due(interval_s: int, *, now: datetime, last: datetime | None,
             rng: Callable[[], float] = random.random, jitter_fraction: float = 0.1) -> datetime
def is_due(now: datetime, due: datetime) -> bool
```
  `UrlDiffCheck`/`ApiFieldCheck`/`RssCheck` take `client: httpx.Client | None` (MockTransport seam); `FileHashCheck(target)` is local-only. `value` = sha256 hex (url_diff, file_hash) · stringified field (api_field) · newest entry id (rss). Checks are sync (the Task-2 loop runs them via `to_thread`).

- [ ] Step 1: failing tests — file_hash on a tmp file (baseline None → `changed=False`, modify → `changed=True`, missing → `ok=False`); url_diff + api_field via `httpx.MockTransport` (pinned `#dotted.path` parse, non-2xx → `ok=False`); rss from two fixture-feed byte strings differing by one entry (`changed=True`, summary `new upload: <title>`); `make_check` unknown kind → ValueError; scheduler: `last=None` → `now + 0.9·interval` (rng=0.0) and `now + 1.1·interval` (rng=1.0), `is_due` boundary
- [ ] Step 2: run → FAIL (`ModuleNotFoundError: marshmellow.tasks.monitors`)
- [ ] Step 3: implement — feedparser for rss (`entries[0].id` as baseline value; ok=False when no entries); sha256 of response/file bytes; api_field walks the dotted path over `response.json()`
- [ ] Step 4: run → PASS (`uv run pytest tests/tasks -q`); full gate green
- [ ] Step 5: commit `feat: monitor checks (url_diff/api_field/file_hash/rss) + jittered scheduler`

### Task 2: TaskManager v2 — task classes, states, control API

**Files:**
- Modify: `src/marshmellow/core/contracts.py` (add `TaskState = Literal["pending","waiting","running","paused","completed","failed","cancelled"]`), `src/marshmellow/tasks/manager.py`, `src/marshmellow/app.py` (`manager.start()` in `App.start()`; `manager.stop()` **before** `bus.stop()` in `App.stop()`), `pyproject.toml` (`[tool.mypy] packages += "marshmellow.tasks"`), `docs/plan/01-architecture.md` (changelog)
- Test: `tests/tasks/test_manager.py` (extend), `tests/core/test_contracts.py` (TaskState row)

**Interfaces:**
- Consumes: `MonitorCheck`/`make_check`/`next_due`/`is_due` (Task 1), existing `submit`/`cancel_all`/`current_kill` semantics (`tasks/manager.py:79-137`).
- Produces (all new `__init__` params keyword-only with defaults — existing tests stay green):
```python
BACKGROUND_MODES: frozenset[TaskMode] = frozenset({"background_monitor", "background_job", "scheduled"})
@dataclass
class TaskHandle:
    spec: TaskSpec; state: TaskState = "pending"; result: TaskResult | None = None
    last_check: datetime | None = None; next_due: datetime | None = None; error: str | None = None
class TaskManager:
    def __init__(self, bus, planner, execute, *, clock: Callable[[], datetime] = datetime.now,
                 rng: Callable[[], float] = random.random,
                 sleeper: Callable[[float], Awaitable[None]] | None = None,   # default asyncio.sleep
                 check_factory: Callable[[MonitorSpec], MonitorCheck] = make_check) -> None
    def register(self, spec: TaskSpec) -> TaskHandle     # background modes only; ValueError otherwise
    def start(self) -> None / def stop(self) -> None      # resident loop thread (lazy-start acceptable)
    def list_tasks(self, state: TaskState | None = None) -> list[TaskHandle]
    def control(self, task_id: str, cmd: Literal["pause","resume","cancel","run_now"]) -> bool
    def handle(self, task_id: str) -> TaskHandle | None
    def task_mode(self, task_id: str) -> TaskMode | None
```
  Monitor loop per task on the resident loop: baseline check → await until `next_due` (sleeper seam; recomputed from `clock`) → check via `to_thread` → trigger = `ok and changed` → emit `TaskEvent("result", payload=summary)`, set result, state `completed`. Scheduled mode: wait until `spec.schedule`, then run the planner turn exactly like `submit` (`asyncio.to_thread(self._planner.run, …)`), with `run_now` forcing it early. `register`/`control` marshal onto the resident loop via `run_coroutine_threadsafe` (they are called from planner worker threads and the GUI thread). Monitor trace is `["started", "result"]` — monitors never emit `step` (no executor actions).

- [ ] Step 1: failing tests — `test_monitor_fires_once_then_completes` (scripted check changes on 2nd tick; FakeClock + recording instant sleeper; event trace `["started","result"]`, state `completed`, payload = summary); `test_first_check_captures_baseline_without_triggering` (changed=True on the very first check → no result event); `test_control_pause_resume_run_now` (pause → due check skipped; resume recomputes `next_due`; run_now checks immediately, returns True); `test_check_errors_wait_then_fail` (5 consecutive `ok=False` → `waiting` events each, then `failed`, exactly one failed event); `test_cancel_all_stops_long_lived_loops` (monitor → `cancelled` event, state `cancelled`, no further checks); `test_list_tasks_filters_by_state`; `test_scheduled_task_runs_at_due_time` (clock past `schedule` → FakePlanner turn ran, result spoken-ready in `handle.result`); `test_register_rejects_foreground_modes` (ValueError)
- [ ] Step 2: run → FAIL (`register`/`control`/`list_tasks` missing)
- [ ] Step 3: implement (+ changelog entry; keep one-shot `submit` behavior byte-identical so M2/M3 tests stay green)
- [ ] Step 4: run → PASS; full gate green **including `uv run mypy` with tasks/ now in the strict list**
- [ ] Step 5: commit `feat: TaskManager task classes — long-lived loops, states, control API`

### Task 3: Foreground lease — consented screen access (D6)

**Files:**
- Create: `src/marshmellow/tasks/foreground.py`
- Modify: `src/marshmellow/tasks/manager.py` (`lease: ForegroundLease | None = None` kwarg + enforcement in `_executing`), `src/marshmellow/ui/mascot.py` (`MascotState` += `"attention"`, color `#f6a5c0` in `_STATE_COLORS`), `src/marshmellow/app.py` (build + wire the lease), `docs/plan/01-architecture.md` (changelog: mascot attention state)
- Test: `tests/tasks/test_foreground_lease.py`, `tests/ui/test_ui.py` (attention-state row)

**Interfaces:**
- Consumes: `Confirmer` protocol (`safety/gate.py:33`, `confirm(question) -> bool`), `ConfirmFlow` (fail-closed spoken yes/no, `safety/confirm.py`), `TaskManager._executing` per-task executor wrapper (`tasks/manager.py:149`), `Mascot.set_state` (thread-safe queued signal, `ui/mascot.py:66`), `TaskEvent.type="lease_request"` (contracts, already exists).
- Produces:
```python
READ_KINDS: frozenset[str] = frozenset({"wait", "ask_user", "done", "fail", "create_task"})
class ForegroundLease:
    def __init__(self, bus: EventBus, confirm: Confirmer, mascot: MascotSeam | None = None) -> None
    def request(self, task_id: str, purpose: str) -> bool   # emits lease_request, mascot "attention", confirm("I need the screen to {purpose} — OK?"), then "acting"/"idle"
    def release(self, task_id: str) -> None                  # no-op when not the holder
    def held_by(self) -> str | None
```
  Enforcement (in `_executing`, BEFORE the executor seam → the gate still sees every granted request): background mode + `kind not in READ_KINDS` + no lease held → auto-`request(task_id, req.rationale)` (one attempt per turn); refused → `ActionResult(ok=False, error="lease_denied")`, further attempts in the same turn refuse without re-asking; granted → action executes through the unchanged gate; lease auto-`release`d in the turn's `finally`. `lease=None` + background non-read → refuse `lease_required` (fail closed). Foreground/immediate-reply modes are unaffected.

- [ ] Step 1: failing tests — `test_background_action_without_lease_refused` (background_job turn, scripted `Confirmer`→False: click refused `lease_denied`, executor never called, fake mascot saw `attention` then `idle`); `test_lease_grant_lets_action_run` (True → executor called, `held_by()` is None after the turn); `test_denial_remembered_for_turn` (two non-read actions → exactly one `confirm` call); `test_read_actions_need_no_lease` (monitor-mode `wait` executes leaseless); `test_lease_none_fails_closed` (background click, no lease → `lease_required`); `test_foreground_unaffected` (foreground click with lease=None executes); mascot test: `MASCOT_STATES` includes `"attention"`, `set_state("attention")` accepted
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (App wiring: `ConfirmFlow(ask=self.ask_channel.ask, fallback=self._confirm_fallback())` — same fail-closed semantics as the L3 gate; headless: no fallback, ambiguity refuses)
- [ ] Step 4: run → PASS; full gate green
- [ ] Step 5: commit `feat: foreground lease — consented screen access for background tasks`

### Task 4: `create_task` — LLM-created monitors + background voice reporting

**Files:**
- Modify: `src/marshmellow/core/contracts.py` (`ActionKind` += `"create_task"`), `src/marshmellow/planner/orchestrator.py` (tool spec + mapping + `M3_TOOLS`), `src/marshmellow/planner/prompts/orchestrator_v2.md` (when to create background tasks), `src/marshmellow/app.py` (`_execute` routing + background-priority reporting), `docs/plan/01-architecture.md` (changelog)
- Test: `tests/planner/test_orchestrator.py` (extend), `tests/tasks/test_create_task.py` (new)

**Interfaces:**
- Consumes: `TaskManager.register`/`task_mode`/`list_tasks` (Task 2), lease enforcement (Task 3), scripted-provider test pattern (`testing/scenario.py:378` ScenarioProvider; FakeTTS).
- Produces: OpenAI function spec `create_task` — args `{instruction: string (required), mode: enum(background_monitor, background_job, scheduled) (required), monitor: {kind: enum(url_diff,api_field,file_hash,rss), target: string, interval_s: integer ≥ 30, condition: string} (required for background_monitor), schedule: ISO-8601 string (required for scheduled)}` → `ActionRequest(kind="create_task", payload={...}, risk="L1")`. `App._create_task(req)` validates the payload (pydantic + interval floor + monitor/schedule presence) and calls `manager.register`; errors are `ActionResult(ok=False, observation="invalid create_task payload: …")`. `App._wire_voice_feedback` speaks `result`/`failed` events for background modes at `SpeakRequest(priority="background")` (mode via `manager.task_mode`); foreground unchanged.

- [ ] Step 1: failing tests — orchestrator: `create_task` call maps to the request with payload intact, bad args → invalid-observation ActionResult, tool spec listed in `M3_TOOLS`; integration (headless App, scripted provider): instruction → `create_task` → monitor registered (`list_tasks()` shows it, `waiting`) → scripted check changes on tick 2 → FakeTTS received `SpeakRequest(priority="background")` whose text is the monitor summary → task `completed`; invalid payload (`interval_s: 5`, monitor missing) → refusal observation, nothing registered
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (+ changelog; FakePlanner untouched — no scripted M2 trace changes)
- [ ] Step 4: run → PASS; full gate green
- [ ] Step 5: commit `feat: create_task path — LLM-created monitors with background voice reports`

### Task 5: Persistence across restart (baselines travel with tasks)

**Files:**
- Create: `src/marshmellow/tasks/store.py`
- Modify: `src/marshmellow/tasks/manager.py` (`store: TaskStore | None = None` kwarg, persist-on-change, `restore()`), `src/marshmellow/app.py` (`task_store_path: Path | None = None` kwarg, default `%LOCALAPPDATA%/Marshmellow/tasks.json`, `manager.restore()` in `App.start()`), `docs/plan/01-architecture.md` (changelog)
- Test: `tests/tasks/test_store.py`, `tests/tasks/test_manager.py` (restore rows)

**Interfaces:**
- Consumes: `TaskSpec`/`TaskState` (contracts), manager registration path (Task 2).
- Produces:
```python
class StoredTask(BaseModel):
    spec: TaskSpec
    baseline: str | None = None          # last seen check value — the anti-re-report record
    state: TaskState = "waiting"          # live states only; "paused" restores paused
class TaskStore:
    def __init__(self, path: Path) -> None
    def load(self) -> list[StoredTask]    # missing/malformed → [] + logged warning (config.py convention)
    def save(self, tasks: list[StoredTask]) -> None
def default_task_store_path() -> Path
```
  Manager: persists after register, after every check, and on every control op (only `background_monitor`/`scheduled`, only live states); `restore() -> int` re-registers stored tasks passing `baseline` into the first check (so a restart never re-reports).

- [ ] Step 1: failing tests — `test_store_round_trip` (tmp path); `test_malformed_store_yields_empty_with_warning`; `test_restart_does_not_re_report` (trigger once → new TaskManager + same store + `restore()` → next unchanged check emits nothing; the following changed check emits exactly one result); `test_completed_and_cancelled_not_persisted`; `test_restore_registers_scheduled_task`; `test_restore_preserves_paused`
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (+ changelog)
- [ ] Step 4: run → PASS; full gate green
- [ ] Step 5: commit `feat: task persistence across restart (baseline anti-re-report)`

### Task 6: Tasks dashboard (main window, tray `open_ui`)

**Files:**
- Create: `src/marshmellow/ui/main_window.py`
- Modify: `src/marshmellow/app.py` (`_on_tray_action` "open_ui" → GUI-thread marshal → lazily-built `TasksDashboard`; App satisfies `TaskDirectory`)
- Test: `tests/ui/test_dashboard.py` (offscreen, `QT_QPA_PLATFORM` per `tests/conftest.py`)

**Interfaces:**
- Consumes: `TrayAction(kind="open_ui")` (already emitted by `ui/tray.py:20-26`), `TaskHandle`/`TaskState`/`TaskEvent` on the bus, the `GuiMarshaledConfirm` marshal pattern (`ui/tray.py:77`) for engine→GUI-thread hops.
- Produces:
```python
class TaskDirectory(Protocol):
    def list_tasks(self, state: TaskState | None = None) -> list[TaskHandle]: ...
    def control(self, task_id: str, cmd: Literal["pause","resume","cancel","run_now"]) -> bool: ...
class TasksDashboard(QMainWindow):   # Qt Widgets per §3.11; engine never depends on it
    def __init__(self, bus: EventBus, tasks: TaskDirectory) -> None
    def show_dashboard(self) -> None
```
  Background task table (instruction, state, last result/error, next check; per-row Pause/Resume/Run now/Delete → `control("pause"/"resume"/"run_now"/"cancel")`); live trace pane receiving **all** `TaskEvent`s (foreground steps + monitor `waiting`/`result` history — §3.12 "all monitor history in dashboard") via a queued Qt Signal — engine threads never touch widgets.

- [ ] Step 1: failing tests — `FakeDirectory` double: table row count mirrors `list_tasks()`; `test_dashboard_pause_button_calls_control` (QTest click → `control(task_id, "pause")`, GUI thread never blocks); TaskEvent emission updates the trace pane (`qapp.processEvents()` pump); `run_now`/`cancel` wired; `show_dashboard` raises + shows
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement
- [ ] Step 4: run → PASS (`uv run pytest tests/ui -q`); full gate green
- [ ] Step 5: commit `feat: tasks dashboard (main window, tray open_ui)`

### Task 7: Soak harness + DoD evidence + user-physical checklist

**Files:**
- Create: `tools/soak_m4.py` (ruff-excluded dir, own hygiene policy)
- Modify: `docs/plan/02-milestones.md` (M4 DoD evidence block), `docs/plan/pending-user-verifications.md` (M4 checklist entry)

**Interfaces:**
- Consumes: everything Tasks 1–6 wired (`App(headless=True)`, `TaskStore`, monitor framework).
- Produces: `uv run python tools/soak_m4.py --hours 24 --interval-s 300 [--feed URL_OR_FILE] [--report-jsonl PATH]` — runs the real monitor framework + scheduler with the real clock against a fixture feed (file-backed) and optionally one real feed; JSONL rows `{at, task, changed, ok, summary, error}`; final stdout summary (trigger count, error count, false-positive candidates = triggers with unchanged source). Verdict recorded in 02-milestones.

- [ ] Step 1: soak harness + smoke run (`--hours 0.01`) proving JSONL + summary output; commit `test: M4 soak harness`
- [ ] Step 2: launch the 24 h soak in the background (fixture feed; verdict criteria: trigger count = intended changes only, zero triggers on unchanged content)
- [ ] Step 3: evidence run: `uv run pytest -q` · `uv run ruff check .` · `uv run mypy`
- [ ] Step 4 (**user-physical checklist** — recorded verbatim in pending-user-verifications.md, discharged one session):
  1. BYOK stored (D7) so the cloud planner is live, then assisted: "hey marshmellow — watch this channel and tell me when they upload" → monitor appears in the dashboard; later, **unprompted spoken report** "new upload: …" (the M4 E2E).
  2. Dashboard on the real screen: tray → Open Marshmellow → task visible; Pause → no reports; Resume; Delete.
  3. Live lease consent: scheduled task ("in one minute, open Calculator") → spoken "I need the screen to … — OK?" → "no" → refused, nothing opens; repeat with "yes" → opens with a visible trace.
  4. Kill switch live with a monitor running: "stop everything" → monitor cancelled, no further reports.
  5. Restart persistence live: create a monitor → quit → relaunch → still listed, **no duplicate report**.
- [ ] Step 5: read the soak verdict into 02-milestones M4 (false-positive report rate), write the DoD evidence block; commit `docs: M4 DoD evidence`

## Execution notes

- Strictly sequential 1→7: monitors feed the manager, the manager carries the lease enforcement point, `create_task` needs both, persistence and the dashboard hang off the manager, DoD closes. Only Task 6's widget authoring is cleanly delegable leaf work (03-workflow brief template; cap 4 concurrent subagents).
- Deliberately out of scope (YAGNI, not silently dropped): recurring monitors (a trigger completes the task), LLM adjudication of free-text `condition` strings (the deterministic trigger is "changed"; `condition` rides along in dashboard/report details), a dashboard **create-task form** (creation is voice-only in M4 — the DoD's UI requirement is visible/pausable), monitor concurrency caps, and mascot behavior for background reports beyond the existing result wiring.
- No new deps beyond feedparser; httpx/`MockTransport` covers network kinds headlessly — zero real network in CI.
- `SpeakRequest(priority="background")` already exists in contracts — no TTS-layer change needed.
- Marker convention recap: `manual` = real desktop needed, deselected by the default gate, runnable by the agent without a user; the M4 suite adds no new marker. User-*physical* items live only in Task 7 Step 4 / pending-user-verifications.md.
- M5 handoffs noticed along the way (do not fix here): autostart (D3) matters once monitors are useful after reboot; log-scrubbing audit (M5) should cover monitor target URLs.
