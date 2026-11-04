# M3 Generic UI Interaction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline — safety design (Task 4), planner design (Task 5) and all integration are main-agent work per 03-workflow). Steps use checkbox (`- [ ]`) syntax.

**Goal:** the core promise — perceive, act, and verify on *any* app through the generic protocol: UIA-first element reading with re-validatable refs, a vision (SoM) fallback for UIA-blind surfaces, a safety gate L0–L3 with spoken confirmation, and a planner tool loop (get_elements → act → verify). Proven headlessly by a scripted + fixture-app scenario suite; closed by the assisted hands-free reference scenario.

**Architecture:** three new packages behind the unchanged executor seam (`tasks/manager.py:131`): `perception/` (UIAReader + element registry + vision fallback + ScreenPerception merge), `actions/` (ActionLayer: UIA patterns → keyboard → SendInput), `safety/` (RiskPolicy + SafetyGate + ConfirmFlow). The M2 orchestrator grows the M3 tool set and verify behavior. Thread discipline unchanged: UIA/SendInput run on the planner worker thread only, the planner keeps its one resident loop, the bus stays the only cross-component channel.

**Tech Stack:** adds `uiautomation>=2.0.29` (Apache-2.0, 04-tooling §1) for tree walking + patterns, `mss>=9` (MIT) + `Pillow>=10` (MIT-CMU) for screenshots/SoM annotation; input synthesis is raw ctypes `SendInput` (pyautogui deliberately avoided, 04-tooling). Everything else unchanged (Python 3.12/uv, pydantic v2, pytest, ruff, mypy strict — extended to the new packages).

**Spec:** [02-milestones.md M3](../02-milestones.md) · [01-architecture.md §3.5–3.7, §3.13](../01-architecture.md) · [SP1 UIA coverage survey](../spikes/SP1-uia-coverage.md) (settle-poll, <10 fallback threshold, new-windows-only rule) · decisions D1/D5/D6/D7.

## Global Constraints

- All M2 gates stay green: `uv run pytest -q && uv run ruff check . && uv run mypy` (pytest un-piped — gate discipline). Note: `tests/stt/test_fasterwhisper.py::test_fixture_audio_wer_and_rtf` is load-sensitive on this desktop (M2 DoD finding: STT ~2× worse under CPU load) — judge that tier in isolation, never mid-suite.
- Each new package (`marshmellow.perception`, `.actions`, `.safety`, `.testing`, `.planner`) is added to `[tool.mypy] packages` in the task that creates it — the strict gate grows with M3.
- **Safety §3.13 is non-negotiable:** every `ActionRequest` passes `SafetyGate` before execution; the gate re-classifies risk authoritatively from kind + target-element state (the planner's declared `risk` is advisory only); UIA `IsPassword` fields are L3 **always**, in every preset and override; the L3 floor is never lowered; ambiguous confirmation fails closed; every decision + rationale logged locally.
- **D5 discipline:** UIA-first is the only default path; vision runs only when the tree is sparse (< 10 interactable elements, SP1 threshold) or the planner hints it; vision results never overwrite UIA elements; screenshots exist only while the task is active and are purged at task end (§3.15).
- **D6:** M3 acts only on foreground-visible windows the task owns; no background input synthesis anywhere (that is M4's foreground lease).
- **No per-app integrations:** no app-name conditionals in `src/marshmellow/{perception,actions,safety,planner,tasks}`; fixture apps appear only in the generic DE-pack alias table (`packs/windows.py`) and in `tests/scenarios/`.
- **SP1 rules:** `snapshot()` = settle-poll + per-`(hwnd, tick)` tree cache; the test harness only ever addresses windows it launched itself — never "any window whose title matches" (SP1 incident).
- Contract changes are main-agent-only (`core/contracts.py`), with a 01-architecture changelog entry + fakes bumped in the same commit (03-workflow rule 3).
- Tests: no wall-clock sleeps (injected clocks / real events with generous timeouts); real-app tests carry `manual` (+ `scenario` where scenario-driven), are `skipif` non-Windows, and are run explicitly in-task; the default gate stays free of real-app launches.
- New deps land in pyproject with the task that needs them, license noted in 04-tooling.

## Review Focus

1. **Stale-ref mis-click (DoD-critical).** Snapshot → window moves / focus changes → action at the old bbox hits the wrong target. Every action re-validates its ref (UIA runtime-id, bbox-IoU ≥ 0.5 fallback) against a fresh walk; element-gone → an error observation telling the planner to re-snapshot — never a coordinate fallback on a stale bbox. Unit + real window-move/focus-change tests.
2. **Planner-declared risk is not trusted.** An LLM can label a destructive `act` with `risk: "L0"`. `RiskPolicy.classify` recomputes from kind + element state; `type_text` on an `IsPassword` field with declared L0 still requires L3 confirmation (dedicated test).
3. **Fail-closed confirmations.** Ambiguous voice ("uh", silence, timeout) must refuse the L3 action — re-ask once, tray-dialog fallback, then refuse. "Yes" is a whitelist regex, never substring-guessing.
4. **Vision fallback runaway.** Vision is fallback-only: a rich tree must produce zero VLM calls (fake-provider zero-call assert); dedupe keeps UIA over vision on bbox overlap; screenshot files purged at task end.
5. **Blocking seams.** UIA walks, pattern calls and SendInput are blocking: planner worker thread only; the planner's resident asyncio loop and the Qt thread never touch them (same discipline as M2 Review Focus 2 — a slow-fake-walk test asserts the loop stays responsive).
6. **Scenario determinism.** Fixture-app scenarios pin app state before acting and assert UIA tree state, never screenshots or pixels; each run launches and closes its own fixture app (new-windows-only).

---

### Task 1: UIAReader + element registry

**Files:** Create `src/marshmellow/perception/__init__.py`, `src/marshmellow/perception/uia_reader.py`, `src/marshmellow/perception/registry.py`, `tests/perception/test_uia_reader.py`, `tests/perception/test_registry.py`. Modify `pyproject.toml` (add `uiautomation>=2.0.29`; `[tool.mypy] packages += "marshmellow.perception"`), `docs/plan/04-tooling.md` (adopted, Apache-2.0 — confirms SP7 table).

**Interfaces:**
- `UiaNode` (Protocol: `runtime_id: str`, `control_type`, `name`, `value`, `enabled`, `offscreen`, `password`, `rect: tuple[int, int, int, int]`, `patterns: frozenset[str]`) and `UiaTreeSource(Protocol): def walk(self, hwnd: int) -> list[UiaNode]` — the seam for all unit tests. Real `UiaAutomationSource()` over `uiautomation.ControlFromHandle(hwnd)` (Name/Value/IsEnabled/IsOffscreen/IsPassword/BoundingRectangle/pattern availability/RuntimeId).
- `UIAReader(source: UiaTreeSource | None = None, settle_deadline_s: float = 2.0)` with `.read(hwnd: int) -> list[Element]` — settle-poll until two consecutive walks agree (or deadline, SP1 finding 2); per-`(hwnd, tick)` cache + `.invalidate(hwnd: int | None = None)` (SP1 finding 3); refs `e1…eN` assigned per walk; SP1 interactable filter (not offscreen; has an action pattern or carries a value); pattern map `Invoke→invoke, Toggle→toggle, Value→set_value, SelectionItem→select, ExpandCollapse→expand` (every element gets `read`); `IsPassword → Element.password`.
- `ElementHandle` (dataclass: `ref`, `element`, `runtime_id`, `node`) and `ElementRegistry` with `.register(elements, runtime_ids, nodes=None)`, `.resolve(ref) -> ElementHandle | None`, `.revalidate(ref) -> ElementHandle | None` — matches the previous walk's element in the current walk by `runtime_id` (fallback: role + name + bbox IoU ≥ 0.5), returns the refreshed handle or `None` (element gone). `ElementHandle` is imported by `actions/` (Task 3), never redefined there.

- [ ] Step 1: failing tests (fake `UiaNode` lists, no real windows) — (a) walk maps role/name/value/bbox/enabled into `Element`s with refs e1…eN; (b) pattern set → `actions`; `password=True` propagates; (c) settle-poll: a source whose 2nd walk differs converges and both walks are consumed; (d) cache: second `read(hwnd)` within one tick walks once; `invalidate()` forces a re-walk; (e) registry round-trip register→resolve; (f) revalidate: same runtime-id with moved bboxes → refreshed handle carrying the NEW bbox; runtime-id absent but role+name+bbox-IoU ≥ 0.5 → matched; element absent → None; (g) empty tree → `[]`, revalidate → None
- [ ] Step 2: run `uv run pytest tests/perception` → FAIL (module missing)
- [ ] Step 3: implement (`UiaAutomationSource` behind a guarded import so the module imports cleanly off-Windows; invalid hwnd → typed `PerceptionError`)
- [ ] Step 4: run → PASS; `uv run pytest -m manual tests/perception` (real Notepad via `WindowsPack.open_app` + `wait_new_window`: `read(hwnd)` ≥ 10 interactable Elements incl. an Edit + buttons; `close_window` its own hwnd) → PASS
- [ ] Step 5: commit `feat: UIAReader with settle-poll cache + element registry`

### Task 2: Vision fallback (SoM) + ScreenPerception merge

**Files:** Create `src/marshmellow/perception/vision.py`, `src/marshmellow/perception/snapshot.py`, `src/marshmellow/perception/prompts/vision_v1.md`, `tests/perception/test_vision.py`, `tests/perception/test_snapshot.py`. Modify `src/marshmellow/core/contracts.py`, `src/marshmellow/llm/openai_compat.py`, `pyproject.toml` (add `mss>=9`, `Pillow>=10`), `docs/plan/04-tooling.md` (mss MIT, Pillow MIT-CMU).

**Contracts (main-agent change — 01-architecture changelog entries in the same commit):**
- `PerceptionHint(BaseModel): query: str | None = None; prefer_vision: bool = False` — the `snapshot(hint)` parameter from 01 §3.6.
- `LLMMessage.images: list[str] = []` (base64 PNG data-URLs) — `openai_compat` maps them to `image_url` content parts; `ProviderCaps.vision` already exists.

**Interfaces:**
- `GRID: Final = 6` — SoM = deterministic 6×6 numbered tiles over the window rect (agent-s SoM pattern adopted as an idea, 04-tooling §2).
- `VisionPerceiver(provider: LLMProvider, capture: Callable[[Rect], bytes] | None = None)` with `.annotate(png: bytes) -> tuple[bytes, list[Rect]]` (Pillow overlay, pure) and `.perceive(hint: PerceptionHint | None) -> list[Element]` — capture the foreground window rect (mss), annotate, `LLMRequest` with the marked image + versioned JSON prompt, parse defensively; results are `Element(source="vision", ref="v1…", bbox=tile rect)`. Prompt in `perception/prompts/vision_v1.md` via `load_vision_prompt()` (mirrors `planner.orchestrator.load_system_prompt`).
- `FALLBACK_THRESHOLD: Final = 10` (SP1) and `ScreenPerception(reader, registry, foreground: Callable[[], WindowInfo | None], vision: VisionPerceiver | None = None)` with `.snapshot(hint: PerceptionHint | None = None) -> PerceptionSnapshot` — UIA walk + `registry.register`; vision only when `hint.prefer_vision` or interactable count < FALLBACK_THRESHOLD; dedupe by bbox IoU > 0.5 with UIA preferred; `screenshot`/`marked_image` set only on the vision path (active-task-only, §3.15).

- [ ] Step 1: failing tests — (a) `annotate` on a generated PNG returns same-size image bytes + 36 tile rects and visibly different bytes; (b) scripted fake provider (canned JSON) → `perceive` returns vision Elements with tile bboxes, `source="vision"`; malformed model JSON → empty list, no crash; (c) rich fake tree (50 elements) → `snapshot()` calls the provider zero times; sparse tree (3 elements) → vision elements merged; (d) `hint.prefer_vision` forces vision on a rich tree; (e) overlapping uia/vision element → UIA kept; (f) `openai_compat` image mapping via httpx `MockTransport` (one request, `image_url` part present)
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (contracts changelog + fake bumps in the same commit)
- [ ] Step 4: run → PASS; `uv run pytest -m manual tests/perception` (real Notepad snapshot: ≥ threshold elements, provider zero-called, window-rect capture works) → PASS
- [ ] Step 5: commit `feat: vision fallback with SoM tiles + ScreenPerception merge (D5)`

### Task 3: ActionLayer (UIA-first → keyboard → SendInput) + re-validation races

**Files:** Create `src/marshmellow/actions/__init__.py`, `src/marshmellow/actions/uia_actions.py`, `src/marshmellow/actions/input_synthesis.py`, `src/marshmellow/actions/executor.py`, `src/marshmellow/testing/__init__.py`, `src/marshmellow/testing/fixtures.py`, `tests/actions/test_input_synthesis.py`, `tests/actions/test_executor.py`, `tests/actions/test_real_apps.py`. Modify `pyproject.toml` (`[tool.mypy] packages += "marshmellow.actions", "marshmellow.testing"`).

**Interfaces:**
- `UiaPatternInvoker(Protocol)`: `invoke(node) -> bool`, `toggle(node) -> bool`, `set_value(node, text) -> bool`, `select(node) -> bool`, `expand(node, expand) -> bool`; real `UiaAutomationPatterns()` wraps the registry's live node handles (§3.7 preference order step 1).
- `InputSynthesizer(send_input=None, delay_ms=(20, 60))`: `.click(point, *, right=False, double=False)`, `.type_text(text)` (Unicode SendInput, surrogate-pair safe), `.hotkey(combo)` ("ctrl+shift+s" → down-sequence then reverse up), `.scroll(point, lines)` — jittered, human-plausible event streams (§3.7); injectable `send_input` records events in tests.
- `ActionLayer(perception, registry, patterns, inputs, focus_window=None, elevated=None, sleep=None)` with `.execute(req: ActionRequest, snapshot: PerceptionSnapshot | None = None) -> ActionResult` (01 §3.7 signature; snapshot=None takes a fresh one). Flow: (1) `target_ref` → `registry.resolve` (None → ok=False "unknown ref — re-snapshot"); (2) revalidate against a fresh `perception.snapshot()` (None → ok=False "element gone — re-snapshot", **no coordinate fallback**); (3) elevated target (`IsElevationRequired` seam) → ok=False spoken limitation, never force-elevated; (4) dispatch: `uia_*` via patterns on the live node; `type_text` = Value pattern first else focus + InputSynthesizer; `click`/`double_click`/`right_click` at the revalidated bbox center; `hotkey`/`scroll` re-focus the element's window first; `wait` via injected sleep; (5) post-state: fresh snapshot WITHOUT screenshot into `ActionResult.snapshot_after`, observation naming the acted element + new foreground.
- `testing/fixtures.py`: `FixtureApp.open(alias, pack=None)` context manager — snapshots the pre-launch window set, launches via `WindowsPack`, `wait_new_window`, exposes `.hwnd`; `.close()` WM_CLOSEs only its own hwnd (SP1 rule); `wait_element(hwnd, role, name_contains, timeout) -> Element`, `element_value(hwnd, ref) -> str` (UIA-tree assertion helpers), `move_window(hwnd, x, y)` (ctypes SetWindowPos — for the race tests).

- [ ] Step 1: failing tests (fake seams, no real windows) — (a) `uia_invoke` uses the pattern invoker, never SendInput; (b) `type_text` on a value-bearing target uses `set_value`; on a non-value target focuses + recorded events with inter-key delays; (c) unknown ref and gone-element both return the re-snapshot observation and perform NO input; (d) elevated target refuses; (e) `click` targets the REVALIDATED bbox center (old bbox ignored after a move); (f) `hotkey`/`scroll` emit the expected recorded sequences; (g) `wait` uses the injected sleep; (h) slow fake walk (planner-thread) does not stall the planner's asyncio loop (watchdog, Review Focus 5)
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement
- [ ] Step 4: run → PASS; `uv run pytest -m manual tests/actions` — the DoD race proof (02-milestones M3): real Notepad via `FixtureApp`: type_text lands (UIA value == text); real Calculator: click digit/`= ` refs, assert display "42"; **window-move race**: snapshot → `move_window` → act on the old ref → revalidation matches by runtime-id at the new bbox → action succeeds; **focus-change race**: snapshot a Notepad ref → focus another window → `type_text` → re-focused, text lands in Notepad; every FixtureApp closed in teardown
- [ ] Step 5: commit `feat: ActionLayer with ref re-validation + fixture-app harness`

### Task 4: Safety gate L0–L3 + confirmation flow

**Files:** Create `src/marshmellow/safety/__init__.py`, `src/marshmellow/safety/policy.py`, `src/marshmellow/safety/gate.py`, `src/marshmellow/safety/confirm.py`, `src/marshmellow/audio/ask.py`, `tests/safety/test_policy.py`, `tests/safety/test_gate.py`, `tests/safety/test_confirm.py`, `tests/audio/test_ask.py`. Modify `src/marshmellow/core/config.py` (`SafetyConfig.overrides: dict[str, RiskLevel] = {}`), `src/marshmellow/ui/tray.py`, `src/marshmellow/tasks/manager.py`, `pyproject.toml` (`[tool.mypy] packages += "marshmellow.safety"`).

**Interfaces:**
- `SENSITIVE_NAME_RE` (delete|trash|empty|send|pay|purchase|checkout|password|sign in|security|account — case-insensitive) and `BATCH_DELETE_THRESHOLD: Final = 5` (§3.13: destructive batch > 5 always confirms).
- `RiskPolicy(preset="standard", overrides=None)` with `.classify(req: ActionRequest, element: Element | None) -> RiskLevel` — kind table (L0: wait; L1: uia_invoke/click/focus_window/shell; L2: type_text/uia_set_value/uia_select/uia_toggle/scroll) then escalation: `element.password` → L3; sensitive element name → L3; delete batch > 5 → L3; `strict` preset additionally escalates `type_text`/`uia_set_value` to L3; overrides keyed by kind; L3 entries rejected when overridden downward.
- `SafetyDecision(allowed: bool, needs_confirmation: bool, reason: str)` and `SafetyGate(policy, confirm, announce)` with `.check(req, element) -> SafetyDecision` and `.guard(execute: Executor) -> Executor` — L1 announced before execution (injected `announce` → TTS ack-priority), L3 via ConfirmFlow, refusals are `ActionResult(ok=False)` (planner-visible, never exceptions); every decision + rationale logged (§3.13).
- `ConfirmFlow(ask, fallback=None, max_reasks=1)` with `.confirm(question: str) -> bool` — explicit yes-whitelist; ambiguous → re-ask once → tray fallback → **False** (fail closed).
- `AskUserChannel(tts, stt, bus, mascot=None, timeout_s=20.0)` with `.ask(question: str, cancelled: Callable[[], bool] | None = None) -> str` — TTS ack-priority question, mascot listening, waits the next `SpeechSegment` on the bus (threading.Event, kill-probe polled between slices), returns the transcript text, `""` on timeout/kill. `TaskManager` exposes the active turn's kill event (`current_kill() -> threading.Event | None`) so a kill during a 20 s wait aborts the ask within ~250 ms (M2's pre-action kill check remains the second net).
- `confirm_dialog(question: str) -> bool` (module-level in `ui/tray.py`, offscreen-testable QMessageBox) — the §3.13 tray fallback when voice is ambiguous.

- [ ] Step 1: failing tests — (a) classify table: every kind → expected level; password element + declared `risk:"L0"` → still L3 (Review Focus 2); sensitive name → L3; batch delete 6 → L3, 5 → L2; strict preset escalates; overrides apply; L3 override downward rejected; (b) gate: L0/L1/L2 flow through, L1 announced, L3 confirm=yes executes, confirm=no/ambiguous → refusal ActionResult, kill-probe set → refusal; (c) confirm: yes set → True; "uh"/"" → re-ask → fallback consulted → False; (d) AskUserChannel: scripted bus SpeechSegment after the question → returns its text; timeout → ""; kill set → prompt ""; (e) tray dialog offscreen: accept → True, dismiss → False
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement
- [ ] Step 4: run → PASS
- [ ] Step 5: commit `feat: safety gate L0-L3 with voice + tray confirmation (fail closed)`

### Task 5: Planner tool loop v2 (perceive/act/verify) + composition wiring

**Files:** Modify `src/marshmellow/planner/orchestrator.py` (`MAX_STEPS: Final = 25`, `TIMEOUT_S: Final = 180.0`, `PROMPT_VERSION = "v2"`), `src/marshmellow/app.py`, `pyproject.toml` (`[tool.mypy] packages += "marshmellow.planner"`). Create `src/marshmellow/planner/tools.py`, `src/marshmellow/planner/prompts/orchestrator_v2.md`, `tests/planner/test_orchestrator_m3.py`.

**Interfaces:**
- `PlannerTools(Protocol)`: `get_elements(query: str | None) -> str` (token-cheap lines `e12 Button 'Play' (920,540 60x24) [invoke]`, §3.5 compact payloads), `screenshot() -> str` (current marked-image path, `""` when none), `act(req: ActionRequest) -> ActionResult` (the gated executor seam). Real adapter `ScreenToolbox(perception, registry, execute)` in `planner/tools.py`.
- `LLMPlanner(provider, tools=None, toolbox: PlannerTools | None = None, max_steps=25, timeout_s=180.0)` — `M3_TOOLS = open_app · get_elements · screenshot · act · wait · ask_user · finish` (`open_app` kept as M2 sugar so M2 traces stay green; `get_elements`/`screenshot` call the toolbox directly — they are NOT executor actions; `act(kind, target_ref, payload, rationale)` maps to `ActionRequest` with the planner's `risk` carried but overridden by the gate); `act`/`wait`/`ask_user` results travel back as the existing `ACTION → RESULT` user messages.
- Verify loop (§3.5): after each act the observation is fed back (M2 verify-lite already defers `finish`); the orchestrator additionally tracks failed/same-observation acts per target ref — 3 consecutive → inject the escalation user-message ("verification failed 3× on this element — ask the user or finish(ok=false)").
- Screenshot purge: at task end (finally) the planner deletes `screenshot`/`marked_image` files created during the turn (§3.15).
- `app.py`: `_build_perception()` / `_build_action_layer()` / `_build_gate()` (real UIA stack + gate + `AskUserChannel`; injectable `App.__init__` params `perception=`, `action_layer=`, `gate=`, `ask_channel=`, `planner=` for headless tests); `_execute` routes: `ask_user` → AskUserChannel, `shell`+open_app → pack, everything else → `gate.guard(action_layer.execute)`; `_build_planner` passes the toolbox into `LLMPlanner` (fake-provider kind still gets the keyword `FakePlanner` unless `planner=` is injected).

- [ ] Step 1: failing tests (scripted `ScriptedLLM` provider per `tests/planner/test_orchestrator.py`, fake perception/toolbox) — (a) golden trace get_elements → act(uia_invoke e3) → finish speaks the summary; (b) stale-ref act → error observation → model re-snapshots → retry succeeds; (c) gate-refused L3 (confirm=no) → refusal observation → finish(ok=false) mentions the refusal; (d) bypass attempt: act with declared `risk:"L0"` on a password element → still L3-confirmed (gate overrode); (e) 3× same failure on one ref → escalation message injected → ask_user round trip; (f) step budget cut at 25; (g) screenshot purge: a vision-path file exists mid-task, gone after finish; (h) `App(headless=True)` with injected fakes: submit_text("open notepad and type hello") → FakeTTS spoken result and the fake layer received the type_text request
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (prompt v2: refs come from the latest get_elements; on any ERROR observation re-snapshot before retrying; verification duty after every act; speak ≤ 2 sentences)
- [ ] Step 4: run → PASS; regression `uv run pytest tests/planner tests/test_voice_loop.py` (M2 tools/traces unchanged); `uv run python -m marshmellow.tools.simulate "open notepad"` still opens + closes real Notepad
- [ ] Step 5: commit `feat: M3 planner tool loop (perceive/act/verify) + composition wiring`

### Task 6: Scenario runner (YAML, scripted mode)

**Files:** Create `src/marshmellow/testing/scenario.py`, `tests/test_scenario_runner.py`, `tests/scenarios/fixtures/` (canned snapshot JSON + `local_page.html`), `tests/scenarios/reference-check-channel.yaml`, `tests/scenarios/vision-fallback.yaml`, `tests/scenarios/l3-confirm-yes.yaml`, `tests/scenarios/l3-confirm-no.yaml`. No pyproject change (`scenario` marker exists).

**Interfaces:**
- YAML schema (top-level keys pinned): `name`, `instruction`, `mode` (`scripted` | `fixture:<alias>`), `snapshots` (path to a canned JSON list of `PerceptionSnapshot` dumps; scripted mode), `script` (ordered fake-provider tool-call list), `trace` (asserted executed actions, in order), `spoken_contains` (substrings the final spoken summary must include).
- `load_scenario(path: Path) -> ScenarioSpec` (pydantic-validated) and `run_scripted(spec, *, snapshots, provider, pack, tts) -> ScenarioReport(ok, trace, spoken)` — builds `App(headless=True)` with the canned perception + fakes, submits the instruction, asserts trace + spoken result.
- Canned snapshot fixtures: a browser-like local page state (reference scenario), a UIA-sparse canvas state (vision fallback: 3 UIA elements + screenshot whose SoM tiles the fake provider reads), a settings form with a password field (L3 cases).

- [ ] Step 1: failing tests — loader validates and rejects malformed YAML; the four scripted scenarios run green: reference trace `[get_elements, act:uia_invoke, get_elements, finish]` + spoken contains the channel answer; vision-fallback asserts vision (`source="vision"`) refs were used and the provider is the fake (zero real calls possible); l3-confirm-no ends with the refusal spoken + no act executed; l3-confirm-yes executes the act after confirmation
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (runner + fixtures; `local_page.html` = static channel-like page with an input + button and a JS-updated result element — a fixture, not an integration)
- [ ] Step 4: run `uv run pytest -m scenario tests/test_scenario_runner.py` → PASS; default gate still green
- [ ] Step 5: commit `test: scripted scenario runner + reference/L3/vision scenarios`

### Task 7: Fixture-app scenario suite (8+ apps) + add-scenario skill

**Files:** Create `tests/scenarios/fixture_notepad.yaml`, `fixture_calculator.yaml`, `fixture_explorer.yaml`, `fixture_browser.yaml`, `fixture_settings.yaml`, `fixture_terminal.yaml`, `fixture_clock.yaml`, `fixture_paint.yaml`; `.zcode/skills/add-scenario/SKILL.md` (04-tooling §3 row: authored in M3). Modify `src/marshmellow/packs/windows.py` (generic alias-table rows only: `clock → ms-clock:`, `paint → mspaint.exe`), `tests/packs/test_windows_pack.py` (resolve rows).

**Interfaces:** `run_fixture(spec)` in `testing/scenario.py` for `mode: fixture:<alias>` — launches the fixture app via `testing/fixtures.FixtureApp`, runs the REAL perception + real ActionLayer with a scripted fake provider, asserts `assert_elements` entries from the YAML (UIA tree state) + `spoken_contains`; each scenario pins app state before acting and closes its own window after (Review Focus 6). Corpus (8): notepad (type + read back), calculator (compute, assert display), explorer (read item names), browser (open `local_page.html` via its `file:///` URI: set_value input, click button, assert result), settings (ms-settings:about, read-only), terminal (type an `echo` line, read it back), clock (read-only), paint (open + read; UIA-sparse → exercises the threshold path without any VLM call).

- [ ] Step 1: failing tests — one test per YAML marked `@pytest.mark.scenario @pytest.mark.manual` (+ `skipif` non-Windows); `run_fixture` fails cleanly on a missing fixture app; `clock`/`paint` alias rows resolve
- [ ] Step 2: run → FAIL (YAMLs/aliases missing)
- [ ] Step 3: author the 8 YAMLs + the skill page (scenario structure, app pinning, UIA assertion rules)
- [ ] Step 4: run `uv run pytest -m scenario` → PASS on this desktop (evidence captured); default gate `uv run pytest -q` still green without launching apps; `uv run ruff check . && uv run mypy` clean
- [ ] Step 5: commit `test: fixture-app scenario suite (8 apps) + add-scenario skill`

### Task 8: DoD evidence + assisted/manual E2E checklist

- [ ] Step 1: evidence run: `uv run pytest -q` · `uv run pytest -m scenario` · `uv run ruff check .` · `uv run mypy` (fixture-audio tier judged in isolation per Global Constraints)
- [ ] Step 2 (**user-physical checklist** — assisted first, then hands-free; outcomes recorded verbatim):
  1. BYOK key stored via `tools/set_key.py` (D7) so the cloud planner is live.
  2. **Reference scenario, assisted:** "hey marshmellow — open the browser and check if <user's channel> has new videos, tell me" → spoken, *correct* answer; then the **hands-free** repeat (no keyboard).
  3. **Live L3 confirmation:** trigger an L3 action (password field on `local_page.html`, or a delete in a scratch folder) → hear the spoken confirmation; answer "no" → refused; "yes" → executed; ambiguous "uh" → refused (fail closed, live).
  4. **Real vision fallback:** a UIA-sparse surface (e.g. Paint canvas) with a vision-capable provider → planner still perceives and reports.
  5. **Kill switch live:** "stop everything" mid-UI-task → actions stop instantly, mascot idles.
- [ ] Step 3: per-step latency FYI table for the reference scenario vs the M2 per-step ≤ 3 s budget (SP1: Settings walks up to 7.5 s — note cache/timeout tuning if exceeded)
- [ ] Step 4: write DoD evidence into 02-milestones M3 section (gate output, scenario list, checklist results, latency note); commit `docs: M3 DoD evidence`

## Execution notes

- Strictly sequential 1→8: perception feeds actions feeds safety feeds the planner; scenario tiers (6→7) and DoD (8) close the milestone. Task 7's YAML authoring is the only cleanly delegable leaf work (03-workflow brief template; cap 4 concurrent subagents).
- **pywinauto 1.0.0b1 evaluation** (04-tooling: "evaluate in M3"): optional side-quest after Task 1, only if `uiautomation` hits a wall (dialogs/depth); record the verdict in 04-tooling either way.
- The reference E2E channel is chosen by the user at run time — no per-app integration is authored; the planner discovers the page generically, which is the point of M3.
- Real vision needs a vision-capable BYOK provider (D4/D7): the scripted vision path is the headless proof; the real-VLM attempt is Task 8's checklist item, not a gate.
- `PerceptionHint` and `LLMMessage.images` are the only planned contract changes; anything else discovered mid-milestone gets the changelog + fakes-bumped treatment in the same commit (03-workflow rule 3).
- Marker convention recap: `manual` = real desktop needed, deselected by the default gate, runnable by the agent without a user; `scenario` = the M3 DoD suite (`pytest -m scenario`); fixture-target scenarios carry both.
