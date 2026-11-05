# Marshmellow — Product Architecture

**Status:** v1 draft for review · Last updated: 2026-11-05
Decisions referenced as D1–D6 (see [PLAN.md §1](../../PLAN.md#1-decisions-locked-with-user-2026-11-04)).
Changelog at bottom; every contract change must be recorded there.

## 0. Guiding constraints

1. **Foreground-visible is the default** for anything that manipulates visible UI (D6).
2. **Voice is bidirectional** — agent listens *and* speaks results (TTS is a first-class output, not an add-on).
3. **Generic protocol, not per-app integrations** — one perceive→identify→act→verify template for every app.
4. **Swappable model layer** — local (privacy/offline) and cloud API backends behind one interface.
5. **Windows first, pack-abstraction for the rest** — later GNOME/KDE/macOS = add a DE pack, not a rewrite.
6. **Ambient footprint** — tray + mascot + summoned main UI; nothing else visible while idle.
7. **Privacy posture v1** — all logs/transcripts stay local; no telemetry; mic indicator respected (muting the OS mic mutes us).

## 1. Runtime shape

One Python process (D1). `asyncio` for engine components; Qt main thread for UI; UIA calls marshalled off the Qt thread.
Structured logging (JSONL, local). Composition root wires real vs fake implementations from config — this is what makes
the voice loop testable without a human.

```
src/marshmellow/
  core/        contracts.py  bus.py  config.py  logging.py
  audio/       capture.py  vad.py  wakeword.py  hotkey.py
  stt/         base.py  fasterwhisper.py  fake.py
  tts/         base.py  piper.py  fake.py        # edge-tts = opt-in extra (OQ-7)
  llm/         base.py  openai_compat.py  ollama.py  fake.py
  perception/  uia_reader.py  vision.py  registry.py  snapshot.py
  actions/     executor.py  uia_actions.py  input_synthesis.py
  packs/       base.py  windows.py                  # future: gnome.py, kde.py, macos.py
  planner/     orchestrator.py  prompts/  tools.py
  tasks/       manager.py  foreground.py  background.py  monitors.py
  safety/      policy.py  gate.py
  ui/          mascot/  tray.py  main_window/
  app.py       composition root
```

## 2. Data flow — the voice loop

1. **Wake/trigger** — AudioEngine detects wake word or hotkey → mascot → *listening*.
2. **Capture** — VAD segments speech; end-of-speech (silence ~700 ms) or hotkey release closes the segment.
3. **Transcribe** — STT → `Transcript`.
4. **Normalize** — PromptBuilder matches control intents ("stop"…) or builds `Instruction` + context (active window, time, clipboard if permitted).
5. **Acknowledge** — brief TTS/mascot cue ("On it.") so the user knows the loop started.
6. **Plan & loop** — Planner classifies the task (foreground/background/simple-reply) and runs perceive→decide→act→verify until done or blocked.
7. **Speak result** — final answer via TTS; mascot → *idle*. Transcript + action trace logged.

Failure paths: STT empty → "Sorry, I didn't catch that."; LLM unreachable → local-adapter fallback if configured, else spoken error; step verification fails N times → ask user.

## 3. Component specs

### 3.1 AudioEngine & activation
**Responsibility:** mic capture (WASAPI via `sounddevice`), VAD (silero-vad), activation per D2 setting, self-muting while TTS plays.
**Interface:**
```python
class AudioEngine:
    def start(self) -> None                      # begins capture thread + event subscription
    def set_mode(self, mode: ActivationMode) -> None   # WAKE_WORD | HOTKEY | BOTH
    def set_muted(self, muted: bool) -> None
    # emits on EventBus: WakeDetected, SpeechSegment(pcm: np.ndarray), HotkeyPressed, MuteChanged
```
**Decisions:** wake-word engine chosen by M0-SP3 (openWakeWord default candidate — phrase constraints in OQ-1);
TTS playback suppresses wake detection (echo guard); OS-level mic mute honored by checking capture device state.
**Risks:** false wakes (M0 measures), CPU cost of always-on detection (target < 2% core).

### 3.2 STT
**Responsibility:** `SpeechSegment` → text. Wispr-Flow-grade accuracy is the bar.
**Interface:**
```python
class Transcriber(Protocol):
    def transcribe(self, segment: SpeechSegment) -> Transcript: ...
```
**Decisions:** default `faster-whisper` (model size from M0-SP2 bench: base vs small vs distil, int8); API STT adapter as opt-in.
**Risks:** latency on long utterances (streaming/partial decode evaluated in M2), proper nouns ("YouTube") handled via initial-prompt biasing.

### 3.3 PromptBuilder (intent & context)
**Responsibility:** transcript → either a **control intent** (handled locally, no LLM) or a normalized `Instruction` enriched with context.
**Interface:**
```python
def build(self, transcript: Transcript, ctx: ContextProvider) -> ControlIntent | Instruction
```
Context injected: foreground window title/app, time, screen count, clipboard text *only if* instruction references "clipboard/copied" (privacy gate).
**Decisions:** control intents are pattern-matched (regex/phrase list) — never spend an LLM call on "stop".
**Risks:** deixis ("open *that*") resolved via context snapshot, not guessed.

### 3.4 LLM abstraction
**Responsibility:** single interface over cloud (default) and local providers.
**Interface:**
```python
class LLMProvider(Protocol):
    async def complete(self, req: LLMRequest) -> LLMResponse   # messages, tools?, schema?, images?
    @property
    def capabilities(self) -> ProviderCaps                     # vision, tool_call, json_schema, max_tokens
```
**Adapters:** `openai_compat` (cloud default — provider-agnostic endpoint/key in config), `ollama` (local; vision-capable model required for perception fallback), `fake` (tests: scripted responses).
**Decisions:** all planner calls request JSON-schema-constrained output where supported; provider selection + fallback order in config; no provider-specific prompt logic leaks above this layer.
**Risks:** cloud outage → config fallback to local adapter if present, else spoken error + task parked.

### 3.5 Planner/Orchestrator
**Responsibility:** the brain. Classifies the instruction, then drives the generic interaction protocol.
**Behavior:**
- **Classify:** simple reply (no screen needed) · foreground task · background task (see §3.12 classes) · needs-clarification (asks user via voice).
- **Loop (foreground):** `snapshot perception → choose action → execute → verify effect → repeat`. Reactive single-next-action loop with a short step sketch in the prompt; sketch is advisory, reality wins.
- **Verify:** after each action, compare new snapshot against expectation (element state changed, expected text appeared). ≤3 retries → escalate to user.
- **Tools exposed to LLM:** `get_elements(query)`, `screenshot()`, `act(ActionRequest)`, `wait(ms)`, `ask_user(question)`, `finish(result)`.
**Interface:**
```python
class Planner:
    async def run(self, instruction: Instruction) -> TaskResult   # speaks result via TTS subscription
```
**Decisions:** prompt templates in `planner/prompts/` versioned; perception payloads are compact (element list w/ refs, not raw UIA dumps) to control tokens; vision fallback images sent as SoM.
**Risks:** token cost per step (mitigate: UIA-first text path is cheap; vision only on fallback); loops (bounded by max-steps + verify).

### 3.6 ScreenPerception
**Responsibility:** build `PerceptionSnapshot` = active window info + element list + screenshot refs. The D5 hybrid.
**Interface:**
```python
class ScreenPerception:
    def snapshot(self, hint: PerceptionHint | None = None) -> PerceptionSnapshot
```
**Composition policy:**
1. `UIAReader` walks the foreground window's UIA tree (`uiautomation` lib) → `Element`s with roles/names/values/bounds + available patterns (Invoke/Toggle/Value/Select/Expand).
2. If tree is empty/sparse (< N elements or requested by hint) → `VisionPerceiver`: screenshot (mss) → SoM annotation → VLM returns elements w/ ref → bbox.
3. Merge: UIA elements preferred; vision elements added for gaps; dedupe by bbox overlap.
**Element registry:** `ref` (e.g. `e17`) → live UIA node handle or screen point, valid for the task lifetime; refreshed each snapshot; refs the planner used may be re-validated before acting (element-gone → re-snapshot, not mis-click).
**Decisions:** UIA-first is the cheap deterministic path; vision is *fallback*, not parallel (latency/cost). Tree walk is cached per hwnd+tick.
**Risks:** UIA coverage on Electron/Chromium is good; on games/custom-drawn apps zero (that's the fallback's job); multi-monitor handled by targeting the foreground window's monitor (v1: primary; OQ-8).

### 3.7 ActionLayer
**Responsibility:** execute `ActionRequest`s safely and report `ActionResult` with post-state observation.
**Interface:**
```python
class ActionLayer:
    def execute(self, req: ActionRequest, snapshot: PerceptionSnapshot) -> ActionResult
```
**Action preference order:** UIA patterns (`Invoke`, `Toggle`, `Value.SetValue`, `SelectItem`, `ExpandCollapse`) → keyboard-equivalent (focus + type/hotkey) → synthesized mouse (SendInput, coordinate from element bbox center). Typing text: UIA ValuePattern first, SendInput Unicode fallback. Scroll: wheel events on target point.
**Decisions:** UIA-invoked actions are preferred over coordinate clicks (precise, no focus-stealing games); SendInput path runs at human-plausible speed with jitter (looks natural, avoids app rate-limits); every action is gated by Safety (§3.13) *before* execution.
**Risks:** focus changes between snapshot & action (re-validate ref); elevated (admin) windows are untouchable without elevation — surfaced as spoken limitation, never force-elevated.

### 3.8 DE packs
**Responsibility:** OS-level operations behind one interface; the Windows shell pack is v1.
**Interface:**
```python
class DEShell(Protocol):
    def open_app(self, target: str) -> Result        # name/alias/URI → resolution: aliases → Start search → shell: URIs
    def focus_window(self, query: WindowQuery) -> Result
    def window_mgmt(self, cmd: WindowCmd) -> Result   # minimize/maximize/close/move/snap
    def open_settings(self, page: str) -> Result      # ms-settings: deep links
def detect_de() -> str                                # "windows" | future: "gnome" | "kde" | "macos"
```
**Decisions:** `packs/windows.py` ships with a curated app-alias table (browser, file manager, terminal, …) + `ms-settings:` map; user-extendable via config. New DE = new pack module + registry entry (skill: `add-de-pack`, 04-tooling).

### 3.9 TTS & voice feedback
**Responsibility:** speak results, acknowledgments, and background-task reports; interruptible.
**Interface:**
```python
class Speaker:
    def say(self, req: SpeakRequest) -> None        # queued; interrupt=False won't cut ongoing speech
    def stop(self) -> None                          # barge-in kill
```
**Decisions:** default engine from M0-SP4 (piper candidate — offline, fast, small); text pre-processed for speakability (URLs/IDs summarized); queue with priorities (ack < result < background-report); speech playback triggers AudioEngine echo guard.
**Risks:** voice choice/quality subjectivity → voice selectable in settings; long answers summarized by planner prompt ("speak ≤ 2 sentences unless asked").

### 3.10 Mascot overlay
**Responsibility:** the always-on-top ambient presence + state signaling (the Wispr-Flow-bar role).
**States:** `idle · listening · thinking · acting · speaking · error · muted`.
**Interface (to EventBus):** subscribes to state events; publishes `MascotClicked` (toggle listening), `MascotMenuAction`.
**Decisions:** frameless transparent PySide6 widget, click-through when idle (so it never blocks the screen), draggable, right-click menu (mute, pause, open UI, quit); animation tech per OQ-3; performance budget: 0% CPU idle, < 2% while animating (render only on state change/frame tick).
**Risks:** occluding real UI when interactive → auto-dim after 10 s of unchanged state; click-through except when interacting.

### 3.11 Tray & Main UI
**Responsibility:** `QSystemTrayIcon` (state-colored; left-click toggles main window; menu: mute/pause/tasks/settings/quit). Main window (summoned, not resident):
- **Tasks dashboard:** live foreground task trace (steps + observations) and background task list (create/pause/resume/delete, last result, next check).
- **Transcript history:** past instructions, spoken replies, action traces (local only).
- **Settings:** activation mode (D2), autostart (D3), providers (cloud/local + keys), STT/TTS/wake settings, privacy toggles (clipboard access, L3 confirmations), mascot position/size, DE-pack aliases.
**Decisions:** Qt Widgets (not QML) for simplicity; UI is a thin EventBus subscriber — the engine never depends on UI running (headless mode = tests).

### 3.12 TaskManager
**Responsibility:** owns task lifecycle, foreground/background policy, monitoring, reporting.
**Task classes (D6 policy):**

| Class | Runs | Examples | Screen? |
|---|---|---|---|
| **Immediate-reply** | inline, no screen | "what's my battery level" (WMI) | never |
| **Foreground task** (default) | now, visible | "open browser, check channel" | owns screen |
| **Background monitor** | periodic/scheduled | "tell me when they upload" | **never** — uses URL/API/file/registry checks, or quiet UIA reads on its own (no input synthesis) |
| **Background job** | long-running | "compress that folder when I'm away" | only via **foreground lease** |
| **Scheduled action** | at time/event | "at 9, open my mail" | lease, announced |

**Foreground lease (mandatory surface for background→UI):** task requests lease → mascot shows *attention* state + TTS "I need the screen to do X — OK?" → consent (voice "yes" / tray button) → runs foreground with trace visible in dashboard → returns to background. **No silent UI takeover. Ever.**
**Interface:**
```python
class TaskManager:
    async def submit(self, spec: TaskSpec) -> TaskHandle
    def list_tasks(self, state: TaskState | None = None) -> list[TaskHandle]
    def control(self, task_id: str, cmd: Literal["pause","resume","cancel","run_now"]) -> Result
    # emits TaskEvent(*) on EventBus; background results → TTS ("Channel X uploaded: <title>")
```
Monitors (`tasks/monitors.py`): poll spec = interval + check (URL diff, API field, file hash, RSS) + trigger condition; jittered intervals; all monitor history in dashboard.

### 3.13 Safety/Consent layer
**Responsibility:** decide what the agent may do without asking; nothing here is optional plumbing.
**Action risk levels:**

| Level | Examples | Policy |
|---|---|---|
| **L0** read/observe | snapshot, read element, screenshot | autonomous |
| **L1** navigate/open | open app, click link, switch window | autonomous + announced |
| **L2** mutate content | type text, submit form, create/move files, copy | autonomous by default; undo where possible; traced |
| **L3** sensitive | credentials, payments/purchases, deletions/empty-trash, sending email/messages, account/security settings | **spoken confirmation required** ("Type the password and press Enter — confirm?") |

**Mechanisms:** `safety/gate.py` — every `ActionRequest` passes pre-execution; L3 confirmation = voice yes/no loop (with tray-dialog fallback if mic ambiguous); **kill switch** ("stop everything" control intent + tray pause) aborts actions and TTS instantly; secrets policy: agent never types passwords by default (fields detected via UIA `IsPassword` → L3 always); destructive batch ops (delete > 5 items) always confirm; every action + rationale logged locally.
**Config:** strictness preset (relaxed/standard/strict) + per-rule overrides in settings.

### 3.14 Config
**Responsibility:** single source of settings; TOML on disk, edited via settings UI.
`config.toml` sections: `[activation]` (mode, wake phrase, hotkey, autostart) · `[providers]` (llm cloud/local + keys via Windows Credential Manager, fallback order) · `[stt] [tts] [wake]` (engine choices from M0) · `[safety]` (preset, overrides) · `[ui]` (mascot, theme) · `[packs]` (aliases, settings map). Profiles: `cloud` (default) / `local` / `offline`.

### 3.15 EventBus & logging
**Responsibility:** async pub/sub (core bus, no external broker) + structured local logging.
All components communicate state via events (`WakeDetected`, `TaskEvent`, `SpeakRequest`, `MascotState`…); UI and TTS are *subscribers*, so engine runs headless in tests. Logs: JSONL per session in `%LOCALAPPDATA%/Marshmellow/logs/` — transcripts, snapshots summaries, action traces, errors; PII-conscious (no typed secrets ever logged; screenshots stored only for active task, then purged).

## 4. Core contracts (v1 sketch)

Authored in M1 as `src/marshmellow/core/contracts.py` (pydantic). Sketch here fixes names/types so parallel work can start; the file is the source of truth, and changes update §changelog.

```python
ActivationMode = Literal["wake_word", "hotkey", "both"]
class WindowInfo(BaseModel): hwnd: int; title: str; app: str; monitor: int
class SpeechSegment(BaseModel): pcm: np.ndarray; sample_rate: int; started_at: datetime; ended_at: datetime
class Transcript(BaseModel): text: str; language: str | None; confidence: float; source: Literal["mic","fixture"]

class ControlIntent(BaseModel): kind: Literal["stop","cancel","mute","unmute","sleep","wake","kill_all"]; matched_text: str
class ContextSnapshot(BaseModel): active_window: WindowInfo | None; time: datetime; clipboard: str | None = None
class Instruction(BaseModel): text: str; transcript: Transcript; context: ContextSnapshot

class Rect(BaseModel): x: int; y: int; w: int; h: int
ElementAction = Literal["invoke","toggle","set_value","select","expand","focus","read"]
class Element(BaseModel):
    ref: str; role: str; name: str; value: str | None; bbox: Rect
    enabled: bool; actions: set[ElementAction]; source: Literal["uia","vision"]; password: bool = False
class PerceptionSnapshot(BaseModel):
    active_window: WindowInfo | None; elements: list[Element]
    screenshot: Path | None; marked_image: Path | None; taken_at: datetime

RiskLevel = Literal["L0","L1","L2","L3"]
ActionKind = Literal["uia_invoke","uia_toggle","uia_set_value","uia_select","click","double_click",
                     "right_click","type_text","hotkey","scroll","focus_window","shell","wait",
                     "ask_user","done","fail"]
class ActionRequest(BaseModel):
    kind: ActionKind; target_ref: str | None; payload: dict = {}
    rationale: str; risk: RiskLevel
class ActionResult(BaseModel):
    ok: bool; observation: str; error: str | None; snapshot_after: PerceptionSnapshot | None

class TaskResult(BaseModel): ok: bool; summary_to_speak: str; details: str | None
TaskMode = Literal["immediate_reply","foreground","background_monitor","background_job","scheduled"]
class MonitorSpec(BaseModel): kind: Literal["url_diff","api_field","file_hash","rss"]; target: str; interval_s: int; condition: str
class TaskSpec(BaseModel):
    id: str; instruction: str; mode: TaskMode
    monitor: MonitorSpec | None = None; schedule: datetime | None = None; created_by: str = "user"
class TaskEvent(BaseModel): task_id: str; type: Literal["started","step","waiting","lease_request","result","failed","cancelled"]; payload: str; at: datetime

class SpeakRequest(BaseModel): text: str; priority: Literal["ack","result","background"]; interrupt: bool = False
```

## 5. Key technical risks → mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| UIA coverage gaps on some apps | planner blind | vision fallback (D5); M0-SP1 coverage survey up front |
| STT latency/accuracy below Wispr bar | UX dies at step 1 | M0-SP2 bench on this machine; model swappable; API STT opt-in |
| LLM mis-clicks / wrong element | user trust | UIA-first (semantic, not pixel) targeting; verify step; L-gates; max-retries → ask user |
| False wake / mic always-on concern | privacy trust | M0-SP3 false-wake measurement; strict mode (hotkey-only) is a setting (D2); visual mute state |
| TTS self-trigger (hears itself) | infinite loop | echo guard: wake detection suppressed during playback |
| Screen ownership conflicts (user working while agent acts) | unusable | foreground lease for background tasks; foreground tasks are user-requested by definition; "pause" tray control |
| Cloud outage | feature halt | provider fallback order in config; local adapter; spoken error, task parked not lost |
| Focus stealing / admin windows | flaky actions | re-validate refs before acting; elevated windows detected → spoken limitation |

## Changelog
- 2026-11-04: v1 draft created at planning stage (no product code yet).
- 2026-11-04: contracts authored in M1 (`core/contracts.py`) — one deviation from the §4 sketch: `SpeechSegment.pcm` is `bytes` (mono int16 LE) instead of `np.ndarray` (keeps numpy out of the contracts layer; numpy views are created at capture). Authored file is the source of truth.
- 2026-11-04 (M2): added `SpeakingStarted`/`SpeakingStopped` events — the echo-guard signal (wake detection suppressed while TTS plays).
- 2026-11-04 (M2): added the LLM layer models — `LLMMessage`, `LLMToolCall`, `LLMRequest`, `LLMResponse`, `ProviderCaps` (tool-calling loop between planner and provider).
- 2026-11-04 (M2): added activation events — `WakeDetected(score)` (wake phrase crossed the SP3 threshold), `HotkeyPressed`/`HotkeyReleased` (LL-keyboard-hook watcher, D2), and `ActivationDegraded(channel, message)` (a channel failed to start; `message` is spoken verbatim — e.g. hotkey hook registration failure, missing wake model).
- 2026-11-05 (M3 T2): added `PerceptionHint(query, prefer_vision)` — the optional `snapshot(hint)` argument from §3.6 (`prefer_vision` forces the SoM vision fallback per D5; `query` focuses perception). Added `LLMMessage.images: list[str]` — base64 PNG data-URLs on a message; `openai_compat` maps them to OpenAI `image_url` content parts (vision fallback path, D5). Both default-valued, so existing fakes needed no code change.
- 2026-11-05 (M3 T3): no contracts.py change. Perception internals extended for the ActionLayer (§3.7): `ElementRegistry.rematch(handle)` matches an already-resolved handle against the registry's CURRENT entries (runtime-id first, then role+name+IoU≥0.5) — the re-validation primitive for post-`snapshot()` walks; the real walker's node snapshot now carries the live UIA `control` so `UiaAutomationPatterns` invokes patterns on the real element and type_text can `SetFocus` it. Elevation refusal (§3.7 spoken limitation) uses the owning process's `TokenElevation` via the live control's `ProcessId` — there is no public `UIA_IsElevationRequired` UIA property (verified against the Windows SDK headers and learn.microsoft.com uiauto-automation-element-propids).
- 2026-11-05 (M3 T3, review fix): window-scoped re-validation. Registrations carry the hwnd they were walked from (`ElementHandle.hwnd`, `register(..., hwnd=)`), `ScreenPerception.snapshot(hint, hwnd=)` re-walks a SPECIFIC window (UIA-only, no artifacts) instead of only the foreground, and the ActionLayer revalidates + takes its post-state walk on the ELEMENT'S OWN window — so a focus change to another app or to another instance of the same app can neither refuse the act ("element gone") nor satisfy the ref with a wrong-window twin via the role+name+IoU fallback (rematch rejects cross-window candidates; proven on real two-Notepad windows with the real foreground seam). Added `packs.windows.window_title(hwnd)` so explicit-hwnd snapshots carry a meaningful title.
- 2026-11-06 (M4 T2): added `TaskState = Literal["pending","waiting","running","paused","completed","failed","cancelled"]` — the state machine behind the §3.12 task classes. `TaskManager` (tasks/manager.py) gains long-lived loops on a resident asyncio thread: `register` (background modes only, ValueError otherwise), `start`/`stop`, `list_tasks`/`control` per §3.12 with the M4-pinned `bool` return, plus `handle`/`task_mode`; `TaskHandle` grows `state/last_check/next_due/error`. Monitor loops run on injected `clock`/`rng`/`sleeper` seams: the first check captures the baseline and never triggers, then due-wait (jittered scheduler) → check via `to_thread` → trigger `ok and changed` → one-shot `result` + `completed`; check errors emit `waiting` events (never spoken) and fail the task after 5 consecutive; monitor trace is `["started","result"]` (no `step` — no executor actions). Scheduled tasks wait for `spec.schedule`, then run the planner turn exactly like `submit`. `register`/`control` marshal onto the resident loop (`run_coroutine_threadsafe` — called from planner worker threads and the GUI thread); `cancel_all` reaches long-lived loops so "stop everything" stops monitors. One-shot `submit` semantics unchanged.
- 2026-11-06 (M4 T3): the mascot gains the `"attention"` state (`#f6a5c0`) — the §3.12 D6 foreground-lease ask. `ForegroundLease` (tasks/foreground.py) is the single-holder consent token: `request(task_id, purpose)` emits TaskEvent `"lease_request"` (payload = purpose), shows mascot `"attention"`, confirms out loud ("I need the screen to {purpose} — OK?") through the same fail-closed ConfirmFlow as the L3 gate (tray fallback in GUI mode; headless ambiguity refuses), grants → mascot `"acting"`, refuses → `"idle"`; `release` is a no-op for non-holders. Enforcement lives in `TaskManager._executing` BEFORE the executor seam (the safety gate still sees every granted request): background mode + non-read action + no lease held → auto-request (one consent attempt per turn); refused → `ActionResult(ok=False, error="lease_denied")` and further attempts in the turn refuse without re-asking; `lease=None` fails closed with `"lease_required"`; the lease auto-releases in the turn's `finally`. `READ_KINDS = {wait, ask_user, done, fail, create_task}` run leaseless. Foreground/immediate-reply turns are unaffected; App builds the lease on the gate's shared ConfirmFlow construction (`_build_confirm`) and passes it to the TaskManager. No contracts.py change (`lease_request` already existed).
