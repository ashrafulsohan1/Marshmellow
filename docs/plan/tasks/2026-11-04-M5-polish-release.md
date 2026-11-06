# M5 Polish & Release — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline — every task here is
> cross-module integration over the existing composition; 03-workflow §1 keeps integration main-agent work).
> Steps use checkbox (`- [ ]`) syntax.

**Goal:** Marshmellow becomes daily-drivable and installable — the local LLM adapter exists and feeds the D5
vision fallback, provider hiccups recover instead of dying, autostart and onboarding make first run a wizard
not a config file, the wake word is finally self-trained (D8), the logs are provably clean, and a
pyinstaller bundle builds headlessly. The DoD's user-physical half (day-long hands-free dogfood, clean-account
install check) is one explicit checklist in Task 7 — everything before it is gated headlessly.

**Architecture:** no new engine components. Task 1 fills the one hole in the D4 adapter set (`ollama` is a
config kind since M1 with no implementation — `core/config.py:27`, the `App._build_planner` fallback warning at
`app.py:428-433`) and finally wires the D5 `VisionPerceiver` into the composition (`app.py:348` still passes
`vision=None`); Task 2 adds the §3.4 "provider selection + fallback order" as a wrapping `ProviderChain`;
Tasks 3–4 deliver the D2/D3/D7 user surfaces (autostart via Task Scheduler, first-run wizard + settings
dialog) over a packaged-asset resolver; Task 5 lands the D8 self-trained wake model and its missing half of
the audio wiring (`app.py:605-607` builds the wake watcher with `chunk_source=iter(())`); Task 6 hardens the
§3.15 scrubber and proves it over a whole session; Task 7 freezes it all (OQ-9 → pyinstaller).

**Tech Stack:** adds nothing to the app's runtime deps. Dev/bench only: `pyinstaller>=6.10` (installer),
`psutil` (startup/memory bench) — both dev-group, licenses recorded in 04-tooling. Wake-model *training*
deps (torch/tensorflow/speechbrain per openwakeword's upstream training flow) live in an optional uv group
(`wake-training`) imported by `tools/` only — never by `src/` or `tests/`.

**Spec:** [02-milestones.md M5](../02-milestones.md) (scope + DoD) · [01-architecture.md §§3.4, 3.11, 3.13,
3.14, 3.15](../01-architecture.md) · decisions D2/D3/D4/D5/D7/D8 (D1/D6 untouched by M5) · OQ-6 and OQ-9
close here.

## Global Constraints

- Baseline verified 2026-11-07 at HEAD `340c251` (all three run this session): `uv run pytest -q` →
  **393 passed, 17 deselected** (152.9 s) · `uv run ruff check .` → clean · `uv run mypy` → strict clean,
  **35 source files**. Every task re-runs the full gate; pytest stays un-piped.
- **§3.13 is non-negotiable:** no task touches `SafetyGate`, `ForegroundLease`, the kill probe, or the
  ConfirmFlow fail-closed semantics. The settings dialog may only *display and persist* the §3.13 strictness
  preset — it can escalate, never lower, and the L3 floor stays code-owned (§3.13).
- **D7:** the onboarding/settings key field goes through `secrets.store_api_key` (`core/secrets.py:23`)
  only — the key never lands in TOML, argv, or logs (Task 6's audit test enforces the log half).
- **D8:** bundled openWakeWord pretrained models never ship. The self-trained model ships *with* a recorded
  license check of its two feature-extractor ONNX deps (`melspectrogram.onnx`, `embedding_model.onnx`);
  if that check is unresolved, the bundle ships hotkey-only and the ship/no-ship call is an explicit Task 7
  checklist item — never a silent default.
- **No per-app integrations** — the Ollama adapter is generic (native `/api/chat` + OpenAI-dialect cloud stay
  the only two providers); no model-name or vendor branching above the `llm/` layer (§3.4).
- **No user interaction inside Tasks 1–6:** device probes, mic capture, model training, soaks, and installer
  runs are machine jobs (agent-runnable, unattended). Everything needing the user physically is Task 7's
  checklist step, recorded verbatim in [pending-user-verifications.md](../pending-user-verifications.md).
- **No wall-clock waits in tests** (02-milestones determinism rules): injected `sleep`/`runner`/probe seams;
  the real-clock jobs (training, soak, bench) live in `tools/` (ruff-excluded, own hygiene policy), not tests.
- Contract changes = 01-architecture changelog entry **+ fakes bumped in the same commit** (03-workflow
  rule 3). Planned: none in `core/contracts.py` — Tasks 1–2 extend `core/config.py` models instead.
- New deps land in pyproject with the task that needs them, license noted in 04-tooling (Task 7: pyinstaller
  + psutil, dev-group only; Task 5: `wake-training` optional group).

## Review Focus

1. **Frozen-executable subprocess hazard** — `PiperSpeaker._argv()` (`tts/piper.py:261-265`) spawns
   `sys.executable -m marshmellow.tts.piper_worker`; under pyinstaller `sys.executable` IS the GUI exe, so the
   speaker would relaunch the whole app per utterance. The frozen branch gets a dedicated unit test on a pure
   argv helper (Task 7), plus an installed-exe smoke that exercises TTS.
2. **Chunk-tap backpressure** — the wake watcher shares the capture thread's mic chunks; a slow openWakeWord
   score (~5–9 ms per 80 ms frame per SP3, but worst case unbounded on a loaded CPU) must never stall capture:
   drop-oldest queue with a drop counter, never a blocking put on the capture thread (Task 5).
3. **Retry storm / fail-fast** — only transport errors, timeouts, 429 and 5xx are retried; a 400 or a code
   bug fails fast without burning the fallback; both-providers-down surfaces as the spoken
   "can't reach the AI service" failure within seconds, never a silent hang (Task 2).
4. **Autostart toggle honesty** — Task Scheduler is the source of truth: the settings checkbox reads
   `is_autostart_enabled()`, toggling applies immediately, and `App.start()` never re-enables (an externally
   deleted task stays deleted). Exact `schtasks` argv incl. the `/TR` quoting is pinned in tests (Task 3).
5. **Scrub-after-format completeness** — the audit runs a whole scripted session (registered API key, a
   monitor URL with a query token, a screenshot turn) and asserts the session JSONL holds none of it — the
   scrub must run after formatting so message args and exception text are equally covered (Task 6).

---

### Task 1: Ollama local provider (D4) + D5 vision wiring in composition

**Files:**
- Create: `src/marshmellow/llm/ollama.py`, `tests/llm/test_ollama.py`, `tools/bench_ollama_m5.py` (ruff-excluded dir)
- Modify: `src/marshmellow/app.py` (new `_build_providers()`; `_build_perception` takes the vision provider), `src/marshmellow/llm/openai_compat.py` (`vision` ctor param), `src/marshmellow/core/config.py` (`LLMProviderConfig.vision`), `pyproject.toml` (`[tool.mypy] packages += "marshmellow.llm"`), `docs/plan/01-architecture.md` (§3.4 adapter note + changelog)
- Test: `tests/llm/test_ollama.py`, `tests/core/test_config.py` (vision field row)

**Interfaces:**
- Consumes: `LLMProvider` Protocol (`llm/base.py:10`: `capabilities` + `async complete(req: LLMRequest) -> LLMResponse`), `LLMMessage.images` base64 PNG data-URLs (`core/contracts.py:201`), `ProviderCaps` (`core/contracts.py:223`), httpx `MockTransport` test pattern (`tests/llm/test_openai_compat.py`).
- Produces:
```python
# llm/ollama.py
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
class OllamaProvider:
    def __init__(self, base_url: str = DEFAULT_OLLAMA_URL, model: str,
                 client: httpx.AsyncClient | None = None) -> None   # client = MockTransport seam
    @property
    def capabilities(self) -> ProviderCaps      # ProviderCaps(vision=True, tool_call=True, json_schema=False)
    async def complete(self, req: LLMRequest) -> LLMResponse
```
  One `POST {base_url}/api/chat`, `stream: false`. Mapping: `LLMMessage.images` data-URLs → Ollama's raw-base64 `images` list (strip the `data:image/png;base64,` prefix); assistant tool calls → Ollama `message.tool_calls` (`function.name`, `function.arguments` as an **object** — not OpenAI's JSON string); role `"tool"` messages → Ollama role `"tool"` with the content verbatim; `req.tools` passed through; non-2xx → `httpx.HTTPStatusError` (Task 2's retryable predicate consumes it).
```python
# llm/openai_compat.py — one new kwarg, default preserves today's behavior
def __init__(self, base_url: str, model: str, get_key: Callable[[], str | None],
             transport: httpx.AsyncBaseTransport | None = None, *, vision: bool = False) -> None
# core/config.py
class LLMProviderConfig(BaseModel):
    kind: Literal["fake", "openai_compat", "ollama"] = "fake"
    base_url: str = ""; model: str = ""; api_key_ref: str = ""
    vision: bool = False   # cloud model serves the D5 vision fallback
```
- Composition (`app.py`): new `_Providers(NamedTuple)` + `_build_providers() -> _Providers` (`planner: LLMProvider`, `vision: LLMProvider | None`) called in `__init__` BEFORE `_build_perception` (today `app.py:200`); `kind == "ollama"` → `OllamaProvider` (empty `model` → logged warning + FakePlanner, `vision=None` — replaces the "not implemented yet" branch `app.py:428-433`); `kind == "openai_compat"` → `OpenAICompatProvider(..., vision=cfg.vision)`; `vision` = the planner provider iff `capabilities.vision` (never the fake planner). `_build_perception` passes `vision=VisionPerceiver(provider=self._providers.vision)` or `None` — `VisionPerceiver(provider, capture=None, *, foreground_rect=None)` (`perception/vision.py:202`) unchanged. `_build_planner(settings)` returns `self._providers.planner`.

- [ ] Step 1: failing tests — `test_ollama_round_trip` (MockTransport: a scripted `/api/chat` JSON → `LLMResponse` with text + tool_calls; assert request body has `stream=false`, images stripped of the data-URL prefix, tool args object); `test_ollama_vision_message` (images mapped to the raw-base64 list); `test_ollama_http_error_raises` (500 → `httpx.HTTPStatusError`); `test_config_vision_default_false` + parse of a TOML with `vision = true`; composition test (headless `App(settings, headless=True, planner=...)` with `kind="ollama"` + injected provider? no — assert via `_build_providers` on a real App instance: ollama kind yields `OllamaProvider` and `vision is not None`; fake kind yields `vision is None`); `openai_compat` vision param test (caps reflect it)
- [ ] Step 2: run → FAIL (`ModuleNotFoundError: marshmellow.llm.ollama`)
- [ ] Step 3: implement (+ changelog; `_build_planner` keeps FakePlanner for `kind="fake"` byte-identical so M2 traces stay green)
- [ ] Step 4: run → PASS; full gate green **with `marshmellow.llm` now in the mypy strict list** (verified clean today: `uv run mypy --strict src/marshmellow/llm` → "no issues found in 3 source files")
- [ ] Step 5: bench (agent-run, needs the local Ollama daemon — not part of the gate): `uv run python tools/bench_ollama_m5.py --models <candidates> --runs 5` measures per planner-tool-turn and per vision-SoM-turn latency + tool-call JSON validity; writes `docs/plan/spikes/data/m5_ollama.json` + report `docs/plan/spikes/M5-local-llm.md` naming the recommended local default (the SP5-style probe, closed for the local leg)
- [ ] Step 6: commit `feat: Ollama local provider + D5 vision fallback wired in composition`

### Task 2: Provider resilience — retry, fallback chain, spoken unavailable-error (§3.4)

**Files:**
- Create: `src/marshmellow/llm/resilience.py`, `tests/llm/test_resilience.py`
- Modify: `src/marshmellow/core/config.py` (`ProvidersConfig.fallback`), `src/marshmellow/llm/base.py` (`ProviderUnavailableError`), `src/marshmellow/app.py` (`_build_providers` wraps the chain), `src/marshmellow/planner/orchestrator.py` (catch it → distinct spoken failure), `docs/plan/01-architecture.md` (§3.14 fallback order + changelog)
- Test: `tests/llm/test_resilience.py`, `tests/planner/test_orchestrator.py` (unavailable-summary row)

**Interfaces:**
- Consumes: `LLMProvider` (Task 1 providers + `llm/fake.py`), `LLMPlanner._run`'s exception handling (`planner/orchestrator.py:420-437`), `ProvidersConfig` (`core/config.py:33`).
- Produces:
```python
# llm/base.py
class ProviderUnavailableError(RuntimeError):
    """Every configured provider failed after retries — the planner speaks the unavailable summary."""
# llm/resilience.py
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2        # total tries per provider (1 retry)
    backoff_s: float = 1.0
def default_retryable(exc: Exception) -> bool   # httpx.TransportError · httpx.HTTPStatusError(429/5xx) ·
                                                # openai APIConnectionError/APITimeoutError/RateLimitError/InternalServerError
class ProviderChain:
    def __init__(self, primary: LLMProvider, fallback: LLMProvider | None = None,
                 retry: RetryPolicy = RetryPolicy(),
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,       # test seam
                 retryable: Callable[[Exception], bool] = default_retryable) -> None
    @property
    def capabilities(self) -> ProviderCaps   # primary's caps, except vision ORs in the fallback's
    async def complete(self, req: LLMRequest) -> LLMResponse
```
  Behavior: per provider, retry transient failures up to `max_attempts` sleeping `backoff_s` between tries; non-retryable → fail fast to the next provider (or raise); exhausted everywhere → `ProviderUnavailableError`. No retry-speak: retries are silent (a spoken word per hiccup is noise — the failure summary is the UX).
```python
# core/config.py
class ProvidersConfig(BaseModel):
    llm: LLMProviderConfig = Field(default_factory=LLMProviderConfig)
    fallback: LLMProviderConfig | None = None   # §3.4 fallback order — the local adapter behind the cloud
# planner/orchestrator.py
PROVIDER_UNAVAILABLE_SUMMARY = ("I can't reach the AI service right now. Check the internet "
                                "connection, or try again later.")
```
  `_run` gains `except ProviderUnavailableError` **before** the generic `except Exception` (`orchestrator.py:432`) → `TaskResult(ok=False, summary_to_speak=PROVIDER_UNAVAILABLE_SUMMARY, details=...)`. `App._build_providers` builds `fallback` when configured → `ProviderChain(primary, fallback)`; `vision` selection (Task 1) is unchanged (the chain reports vision if either side has it).

- [ ] Step 1: failing tests — `test_transient_retried_then_succeeds` (provider fails once with `httpx.ConnectError`, succeeds on try 2; `sleep` recorded once); `test_attempts_exhausted_falls_back` (primary always fails → fallback answers; call counts 2 + 1); `test_non_retryable_fails_fast` (primary raises `ValueError` → fallback called once, no sleeps); `test_both_fail_raises_unavailable` (call counts 2 + 2, `ProviderUnavailableError`); `test_caps_vision_union` (cloud `vision=False` + local `vision=True` → chain reports vision); orchestrator: scripted provider raising `ProviderUnavailableError` → `summary_to_speak == PROVIDER_UNAVAILABLE_SUMMARY`, task marked failed; config: TOML `[providers] fallback` parses into a second `LLMProviderConfig`
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (+ §3.14 doc line + changelog)
- [ ] Step 4: run → PASS; full gate green
- [ ] Step 5: commit `feat: provider retry + cloud-to-local fallback chain (spoken unavailable error)`

### Task 3: Autostart (D3) — Task Scheduler implementation (closes OQ-6)

**Files:**
- Create: `src/marshmellow/core/autostart.py`, `tests/core/test_autostart.py`
- Modify: `docs/plan/05-open-questions.md` (OQ-6 → closed: Task Scheduler on-logon, per its recommendation)
- Test: `tests/core/test_autostart.py`

**Interfaces:** consumes `Settings.autostart: bool` (`core/config.py:54` — the flag exists since M1, nothing implements it). Produces:
```python
# core/autostart.py
AUTOSTART_TASK = "Marshmellow"
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]   # subprocess.run(capture_output=True, text=True)
def build_create_command(launch: str) -> list[str]
#   ["schtasks", "/Create", "/F", "/TN", "Marshmellow", "/SC", "ONLOGON", "/RL", "LIMITED", "/TR", <quoted>]
#   where <quoted> is the launch wrapped in one outer pair of double quotes with the executable path
#   itself quoted inside (schtasks' standard form). The EXACT expected argv element — including the
#   escaping as it must reach CreateProcess — is pinned verbatim in test_build_create_command_exact_argv.
def autostart_launch_command() -> str
#   frozen (getattr(sys, "frozen", False)): quoted sys.executable;
#   dev: quoted <REPO_ROOT>\.venv\Scripts\pythonw.exe -m marshmellow  (no console window at logon;
#   python.exe fallback if pythonw.exe is missing)
def is_autostart_enabled(runner: Runner = _run) -> bool    # schtasks /Query /TN Marshmellow → returncode == 0
def enable_autostart(launch: str | None = None, runner: Runner = _run) -> bool
def disable_autostart(runner: Runner = _run) -> bool       # schtasks /Delete /TN Marshmellow /F
```
  All functions return `False` + a logged warning on any non-zero returncode or non-win32 platform (never raise into the caller). **Pinned policy:** the scheduled task is the source of truth — `App.start()` does NOT call any of these (an externally deleted task stays deleted); the toggle applies only from the Task 4 surfaces.

- [ ] Step 1: failing tests — `test_build_create_command_exact_argv` (launch with spaces → the `/TR` value is the launch wrapped in escaped inner quotes); `test_enable_uses_launch_command` (fake runner records argv, returns True on rc 0); `test_is_enabled_true_false_and_error` (rc 0 / rc 1 / OSError-injected runner → True / False / False, no raise); `test_disable`; `test_launch_command_frozen_vs_dev` (monkeypatched `sys.frozen` / repo layout — asserts the pythonw branch); non-win32 → all False (skipif guard per platform reality)
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (+ OQ-6 closure line)
- [ ] Step 4: run → PASS; full gate green
- [ ] Step 5: commit `feat: autostart via Task Scheduler (D3, OQ-6 closed)`

### Task 4: Packaged-asset resolver + first-run onboarding + settings dialog (D2/D3/D7)

**Files:**
- Create: `src/marshmellow/core/assets.py`, `src/marshmellow/ui/onboarding.py`, `src/marshmellow/ui/settings_dialog.py`
- Test: `tests/core/test_assets.py`, `tests/ui/test_onboarding.py`, `tests/ui/test_settings.py` (offscreen, `QT_QPA_PLATFORM` per `tests/conftest.py`)
- Modify: `src/marshmellow/core/config.py` (`product_config_path() -> Path` — the write-side twin of `default_config_path()`), `src/marshmellow/__main__.py` (first-run branch before `App(...)`), `src/marshmellow/ui/main_window.py` (dashboard "Settings…" button → injected `open_settings` callable), `src/marshmellow/app.py` (`open_settings` marshal passed to the dashboard), `docs/plan/01-architecture.md` (changelog)

**Interfaces:**
```python
# core/assets.py — frozen-aware packaged-resource resolver (used by Tasks 5 and 7)
def asset_dir() -> Path
#   frozen: Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)); dev checkout: repo root
def default_wake_model_dir() -> Path          # asset_dir()/"wake"
def default_wake_model_path() -> Path | None  # asset_dir()/"wake"/"hey-marshmellow.onnx" if it exists else None
def default_stt_model_dir() -> Path | None    # asset_dir()/"models"/"faster-whisper-base" if it exists else None
def default_voice_dir() -> Path | None        # asset_dir()/"voices" if it exists else None
# core/config.py
def product_config_path() -> Path             # %LOCALAPPDATA%/Marshmellow/config.toml (cwd fallback like store.py:64)
# ui/onboarding.py
@dataclass(frozen=True)
class OnboardingResult:
    activation: ActivationMode; autostart_enabled: bool; key_stored: bool; completed: bool  # False = closed/cancelled
def should_onboard(config_path: Path | None) -> bool
#   config_path is None (nothing found by default_config_path()) AND MARSHMELLOW_SKIP_ONBOARDING unset
def probe_default_mic(sample_rate: int = 16000) -> tuple[bool, str]
#   sounddevice default-input query + 1 s capture RMS sanity; (False, detail) on no device / silent capture
#   (Windows mic-permission-blocked streams enumerate but sample silence — that is what this catches)
class OnboardingWizard(QDialog):
    def __init__(self, settings: Settings, *,
                 mic_probe: Callable[[], tuple[bool, str]] = probe_default_mic,
                 wake_available: Callable[[], bool] = lambda: default_wake_model_path() is not None,
                 store_key: Callable[[str], None] = secrets.store_api_key,
                 apply_autostart: Callable[[bool], bool] = autostart.enable_autostart) -> None
    def run(self) -> OnboardingResult
```
  One page, five sections (a wizard flow is not warranted): mic check row (probe result text) · wake row ("wake word ready" / "not set up — the hotkey will be used; train later with `tools/train_wake_model.py`") · activation radios (D2; wake options disabled while `wake_available()` is False — the existing `ActivationDegraded` spoken path covers a stale choice) · API key field (password echo; Save → `store_key`, never echoed back, D7) · autostart checkbox (D3). Finish → `settings.activation`/`settings.autostart` mutated, `settings.save(product_config_path())`, returns the result. `__main__.main()` (`__main__.py:23-40`): after the QApplication exists, `if should_onboard(default_config_path()): result = OnboardingWizard(settings).run()` before `App(settings, headless=False)`; headless paths never see it.
```python
# ui/settings_dialog.py
class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, *,
                 apply_autostart: Callable[[bool], bool],
                 autostart_state: Callable[[], bool] = autostart.is_autostart_enabled,
                 store_key: Callable[[str], None] = secrets.store_api_key,
                 on_saved: Callable[[Settings], None] | None = None) -> None
```
  Fields: activation mode combo (D2) · hotkey edit validated live via `parse_combo` (`audio/activation.py:47`) · autostart checkbox initialized from `autostart_state()` (scheduler = source of truth) · safety preset combo (§3.13 config: relaxed/standard/strict — persist-only, may escalate, never lowers) · API key replace field. Save → validate → mutate → `settings.save(product_config_path())` → `on_saved`. **Pinned:** changes apply on next start (no live watcher rebuild); the modal dialog is fine because `GuiMarshaledConfirm`'s BlockingQueuedConnection is processed by the dialog's nested event loop, so the confirm fallback stays live. Dashboard: `TasksDashboard.__init__(bus, tasks, *, open_settings: Callable[[], None] | None = None)` + a "Settings…" button; `App._open_dashboard` passes a GUI-thread `self._open_settings` (lazy `SettingsDialog(settings, apply_autostart=...)` + `exec()`).

- [ ] Step 1: failing tests — assets: `test_asset_dir_dev_is_repo_root`, `test_default_wake_model_path_none_when_absent` (tmp `asset_dir` via monkeypatch); onboarding: `test_should_onboard_true_when_no_config` / false when a path exists / false with the env var; `test_probe_default_mic_injected` (fake probe tuple rendered); offscreen wizard with injected fakes: key field → `store_key` called once with the entered text, activation radio selection lands in the result, wake options disabled when `wake_available()` False, autostart checkbox → `apply_autostart(True)` called, cancel → `completed=False`; settings dialog: checkbox init from `autostart_state()`, invalid hotkey (`"notakey"` → `parse_combo` ValueError) shows an error and does NOT save, valid save calls `on_saved`; dashboard button calls the injected `open_settings`
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (+ changelog)
- [ ] Step 4: run → PASS; full gate green
- [ ] Step 5: commit `feat: first-run onboarding + settings dialog over packaged-asset resolver (D2/D3/D7)`

### Task 5: Self-trained wake model (D8) — mic chunk tap, trainer, ambient soak

**Files:**
- Create: `tools/train_wake_model.py`, `tools/soak_wake_m5.py`, `assets/wake/README.md` (what shipped, license-check result, calibration), `tests/audio/test_wake_training.py`
- Modify: `src/marshmellow/audio/capture.py` (`AudioEngine(..., chunk_tap=...)`), `src/marshmellow/audio/activation.py` (`ChunkQueue`), `src/marshmellow/app.py` (`_build_activation` resolves the model dir + wires the tap), `.gitignore` (negate `!assets/wake/hey-marshmellow.onnx`), `tests/audio/test_activation.py` (real-model rows, skipif absent), `docs/plan/01-architecture.md` (changelog), `docs/plan/pending-user-verifications.md` (carry-over entry: live-mic false-accept soak → discharged by Task 5's soak, checklist re-check in Task 7)
- Test: `tests/audio/test_wake_training.py`, `tests/audio/test_capture.py` (tap rows), `tests/audio/test_activation.py` (extend)

**Interfaces:**
```python
# audio/capture.py
class AudioEngine:
    def __init__(self, bus: EventBus, chunk_source: Iterator[np.ndarray] | None = None,
                 vad: Callable[[np.ndarray], float] | None = None, sample_rate: int = SAMPLE_RATE,
                 chunk_tap: Callable[[np.ndarray], None] | None = None) -> None
#   _loop calls the tap once per UNMUTED chunk, before VAD (mute stops wake too, §3.1) — tap exceptions
#   are logged and never kill the capture thread
# audio/activation.py
class ChunkQueue:
    """Capture thread → wake thread bridge. Drop-oldest: a stalled scorer never stalls capture."""
    def __init__(self, maxlen: int = 50) -> None          # 50 × 32 ms chunks ≈ 1.6 s of buffer
    def __call__(self, chunk: np.ndarray) -> None         # the tap: put_nowait; on full, drop-oldest
    def __iter__(self) -> Iterator[np.ndarray]            # the watcher's chunk_source; get(timeout=0.5) loop
    def close(self) -> None                               # sentinel → the iterator ends (clean watcher stop)
    @property
    def dropped(self) -> int
```
  Composition (`app.py`): `AudioEngine(..., chunk_tap=tap)` and `WakeWordWatcher(self.bus, model_dir=..., chunk_source=iter(tap))` share one `tap = ChunkQueue()`; `model_dir` = the injected `wake_model_dir` kwarg, else `default_wake_model_dir()` when it contains a model, else `None` (the existing spoken degrade, `audio/activation.py:370-377`, unchanged). Echo guard untouched (`VoiceLoop._on_wake_detected` suppression, `audio/loop.py:155-160`).
```python
# tools/train_wake_model.py
uv run python tools/train_wake_model.py --phrase "hey marshmellow" --utterances 1000 --seed 42 --out assets/wake [--plan]
#   --plan prints/saves the synthesis manifest (utterances per voice, negative list, seed) without training —
#   the headless-tested pure half. Full run: download 2–3 en_US piper voices → synthesize positives through
#   the EXISTING piper_worker subprocess protocol (stdin lines → framed PCM — no new license surface, the
#   tool imports no piper) → lookalike negatives ("hey marshmallow", "hey mellow", …) + SP3 pink-noise/silence
#   → openwakeword upstream custom-training flow (uv group `wake-training`, imported inside the training step
#   only) → exports assets/wake/hey-marshmellow.onnx + threshold calibration from the negative-score histogram
#   into assets/wake/calibration.json (SP3's 0.5 stands unless the histogram says otherwise — recorded, not
#   silently changed)
# tools/soak_wake_m5.py
uv run python tools/soak_wake_m5.py --hours 1 [--model-dir assets/wake]
#   real mic ambient through the REAL path (AudioEngine chunk_tap → ChunkQueue → WakeWordWatcher); counts
#   WakeDetected/hour → docs/plan/spikes/data/m5_wake_soak.json (discharges the SP3 carry-over)
```

- [ ] Step 1: failing tests — `test_chunk_queue_drop_oldest` (overflow → oldest gone, `dropped` counts, capture thread never blocks: tap latency bounded); `test_chunk_queue_close_ends_iteration`; `test_engine_tap_receives_unmuted_chunks_only` (fixture chunk_source through `AudioEngine`, muted → tap not called, tap exception swallowed + logged); `test_app_wires_tap_and_model_dir` (headless App with `audio_engine=None`: watcher chunk source is the shared queue when a fake model dir is injected via `wake_model_dir`); trainer pure half: manifest determinism (seed → identical manifest), calibration math (threshold = smallest score cutoff with false-accept rate ≤ target over a score list); real-model rows in `test_activation.py` (skipif `assets/wake` absent): a piper-synthesized phrase fixture fires `WakeDetected` within 1.5 s of onset, SP3 silence + pink-noise fixtures fire zero
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (+ `.gitignore` negation, README with the license-check result for `melspectrogram.onnx`/`embedding_model.onnx` — see Global Constraints D8)
- [ ] Step 4: run → PASS; full gate green
- [ ] Step 5: machine jobs (agent-run, no user): the full training run (validate via the Step 1 real-model rows + `assets/wake/README.md` record), then `uv run python tools/soak_wake_m5.py --hours 1` — verdict (false accepts/hour) into `docs/plan/spikes/data/m5_wake_soak.json` and the M5 evidence block
- [ ] Step 6: commit `feat: self-trained hey-marshmellow wake model (D8) — chunk tap, trainer, ambient soak`

### Task 6: Privacy audit — scrubber hardening + session-log enforcement (§3.15)

**Files:**
- Modify: `src/marshmellow/core/logging.py` (URL query-param scrub), `tests/core/test_logging.py` (extend)
- Create: `tests/core/test_privacy_audit.py`, `docs/privacy.md`
- Modify: `docs/plan/01-architecture.md` (changelog)

**Interfaces:** consumes `JsonFormatter.format`'s post-format scrub order (`core/logging.py:33-48`), `register_secret` (`core/logging.py:27`), `secrets.register_secret` wiring (`core/secrets.py:24,40`). Produces:
```python
# core/logging.py — second pattern, applied after _SECRET_RE inside JsonFormatter.format
_URL_SECRET_RE = re.compile(
    r"[?&#](?:token|key|sig|signature|access_token|api_key|apikey)=([^&\"\s]+)", re.IGNORECASE,
)
#   "?key=abc&x=1" → "?key=[REDACTED]&x=1" — monitor target URLs with query credentials (the M4 handoff)
```
  `docs/privacy.md` = the audit's written output: what is stored where (session JSONL in `%LOCALAPPDATA%/Marshmellow/logs`, transcripts local-only, `tasks.json`, Credential Manager for the key, screenshots active-task-only then purged), what never leaves the machine (no telemetry, 01 §0.7), the scrub rules, and how to purge everything.

- [ ] Step 1: failing tests — scrubber rows in `test_logging.py`: a URL with `?token=…`/`&api_key=…` redacted in msg AND in `exc` text (post-format order preserved); a token split across a formatted arg still caught; audit test `test_privacy_audit.py::test_session_log_is_clean`: a full headless scripted session (real `App(headless=True, log_dir=tmp, planner=scripted)`) whose instruction carries a monitor-target URL with a query token, a registered fake key (via `secrets.store_api_key(backend=fake)`), and a screenshot-producing turn → read the session JSONL and assert: no key substring, no URL token value, no unredacted `password=` shape, no screenshot path persisted after the turn's purge; plus `docs/privacy.md` exists and names each storage location
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (+ docs)
- [ ] Step 4: run → PASS; full gate green
- [ ] Step 5: commit `feat: privacy audit — URL-token log scrubbing + whole-session enforcement (§3.15)`

### Task 7: Installer (OQ-9) + startup/memory bench + docs + DoD + user-physical checklist

**Files:**
- Create: `installer/marshmellow.spec`, `installer/piper_worker_entry.py`, `installer/fetch_bundle_inputs.py`, `tools/bench_startup_m5.py`, `tools/dogfood_report.py`, `.zcode/skills/release-checklist/SKILL.md`, `README.md`, `docs/user-guide.md`
- Modify: `src/marshmellow/tts/piper.py` (frozen argv helper), `src/marshmellow/__main__.py` (`--smoke`), `src/marshmellow/app.py` (`_build_stt` uses `default_stt_model_dir()`), `pyproject.toml` (`dev += pyinstaller>=6.10, psutil`), `docs/plan/02-milestones.md` (M5 DoD evidence block), `docs/plan/pending-user-verifications.md` (M5 checklist entry), `docs/plan/05-open-questions.md` (OQ-9 → closed), `docs/plan/04-tooling.md` (packaging row + dev-dep licenses)
- Test: `tests/tts/test_piper.py` (frozen-argv rows)

**Interfaces:**
```python
# tts/piper.py — the frozen-executable fix (Review Focus 1)
def worker_argv(python_exe: Path | None, model: Path | None) -> list[str]
#   python_exe None + not frozen → [sys.executable, "-m", "marshmellow.tts.piper_worker", model?]
#   frozen → [Path(sys.executable).with_name("piper_worker.exe"), model?]   # the second spec Exe, dist root
# __main__.py — argparse: --smoke → App(load_settings(...), headless=True); start(); stop(); print("smoke ok");
#   return 0 (headless, no Qt windows — the headless install verification)
# tools/bench_startup_m5.py
uv run python tools/bench_startup_m5.py --exe dist/Marshmellow/Marshmellow.exe --runs 3
#   spawn → tail the newest %LOCALAPPDATA%/Marshmellow session JSONL for the "Marshmellow … started" line
#   (cold start), then after a 60 s settle: psutil RSS of the app process + its children (piper worker is a
#   separate process — record both, target checked against the sum) → docs/plan/spikes/data/m5_startup.json
# tools/dogfood_report.py
uv run python tools/dogfood_report.py --date YYYY-MM-DD
#   summarize a day's session logs: crash count (level=ERROR with an exc payload), instructions, spoken
#   results, monitor triggers — makes the "zero crashes" DoD a grep, not a memory
```
  Spec (`installer/marshmellow.spec`): TWO Exes — main `Marshmellow` (console=False) + `piper_worker` from `installer/piper_worker_entry.py` (console=False; calls `marshmellow.tts.piper_worker.main()`; piper is imported only inside it — the SP7 GPL boundary survives freezing). Datas: `assets/wake/*` (see D8 constraint — hotkey-only bundle if the feature-model license check is unresolved), the piper voice + its `.json` config → `_internal/voices` (fetched by `installer/fetch_bundle_inputs.py` at build time — never committed, `voices/` stays gitignored), the faster-whisper base ct2 model dir → `_internal/models/faster-whisper-base` (so a clean account installs offline-capable except the BYOK call). `App._build_stt` (`app.py:309-315`) passes `str(default_stt_model_dir())` when frozen and it exists, else the hub name `settings.stt_model` unchanged; `tts/piper.py:default_voice_model` (`:39-56`) gains the `default_voice_dir()` search entry.

- [ ] Step 1: failing tests — `test_worker_argv_dev_branch` (exact `[sys.executable, "-m", "marshmellow.tts.piper_worker", model]`), `test_worker_argv_frozen_branch` (monkeypatched frozen → sibling `piper_worker.exe` first element); `test_smoke_flag` (`main()` with `--smoke`, injected headless App double: start+stop called, exit 0)
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement (`fetch_bundle_inputs.py`, spec, `--smoke`, STT/voice frozen resolution)
- [ ] Step 4: build + headless verification (agent-run): `uv run pyinstaller installer/marshmellow.spec --noconfirm` → `dist/Marshmellow/Marshmellow.exe --smoke` exits 0; commit `feat: pyinstaller bundle — frozen piper worker exe, packaged models, smoke entry`
- [ ] Step 5: benches (agent-run, real clock): `tools/bench_startup_m5.py` → cold-start median and idle RSS into `docs/plan/spikes/data/m5_startup.json`; `tools/dogfood_report.py --help` proven on the M2–M4 session logs. Targets: cold-start ≤ 10 s, idle memory ≤ 400 MB (sum) — a miss is recorded with the tuning levers (lazy STT load, deferred piper spawn), a measured decision, never silently tuned
- [ ] Step 6 (**user-physical checklist** — recorded verbatim in pending-user-verifications.md, discharged in one session; this is the milestone's ONLY user-required work):
  1. **Clean-account install (DoD):** on a fresh Windows local account, run the built bundle → onboarding wizard appears → mic check passes → BYOK key stored → wake word works hands-free (**"hey marshmellow"** with no hotkey — first session led by the self-trained model) → enable autostart → sign out/in → the app launches at login. Outcome: ____________
  2. **Day-long hands-free dogfood (DoD):** use Marshmellow as the primary interface for a full workday, wake-word-led; afterwards `uv run python tools/dogfood_report.py --date <day>` → crash count **0**, friction list recorded. Outcome: ____________
  3. **Wake false-accept feel:** during the dogfood day, note any unprompted wake (the subjective complement to Task 5's 1 h soak). Outcome: ____________
  4. **D8 ship call (only if the feature-model license check is unresolved):** ship the wake path in the bundle, or release hotkey-only. Outcome: ____________
- [ ] Step 7: DoD evidence — write the M5 block in 02-milestones (gate outputs, bench table, soak verdicts, installer smoke) + `.zcode/skills/release-checklist/SKILL.md` (the repeatable gate: build → smoke → benches → privacy audit → dogfood) + `README.md`/`docs/user-guide.md` (install, onboarding, D2/D3 settings, L0–L3 safety, privacy pointer); commit `docs: M5 DoD evidence`

## Execution notes

- **Strictly sequential 1→7** (integration-heavy, inline per 03-workflow): Task 2 wraps Task 1's providers; Task 4 consumes Task 3's autostart functions and defines the asset resolver Tasks 5 and 7 need; Task 5 fills the onboarding's "wake available" row and the bundle's wake data; Task 6 must run after everything that logs (it audits the whole surface); Task 7 ships the lot. The only cleanly delegable leaves are the bench/report tool scripts and the user-guide prose (03-workflow brief template; cap 4 concurrent subagents).
- Task-completion rows land in `docs/plan/build-ledger.jsonl` via per-task `chore(M5-Tn)` commits — the M4 pattern (`chore(M4-T6): record Task 6 in build-ledger.jsonl`), driven by the build pipeline, not by this plan's steps.
- **Deliberately out of scope** (not silently dropped): a full Settings UI beyond the one dialog (providers, aliases, mascot size stay config-file — §3.14 covers them; expand on demand), OQ-8 multi-monitor (post-v1 per the register), OQ-3 mascot art (user-owned), recurring monitors and live-apply settings, MSIX (pyinstaller is the OQ-9 dogfood answer; revisit after the clean-account check), and mypy widening beyond `marshmellow.llm` (audio/stt/tts/ui lean on untyped third-party surfaces — a measured decision to leave, not an oversight).
- **Retry-speak rejected on purpose:** transient provider errors retry silently (Review Focus 3); the only spoken recovery UX is the final `PROVIDER_UNAVAILABLE_SUMMARY`. The §3.5 verify escalation (3-strike `ESCALATION_MESSAGE`, `orchestrator.py:471-472`) already exists from M3 — Task 2 does not touch it.
- Marker convention recap: `manual` = real-desktop-but-agent-runnable, deselected by the default gate. User-*physical* work exists only in Task 7 Step 6 / pending-user-verifications.md. Tasks 1–6 add no new pytest marker.
- Wake-model reality check: until Task 5's training run completes, every "wake" reference behaves as today — spoken degrade + hotkey (D8's trusted fallback). Tasks 1–4 and 6–7 are fully exercisable in that state; only Task 5's real-model test rows and Task 7's checklist item 1 depend on the model existing.
