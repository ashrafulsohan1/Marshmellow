"""Core contracts — single source of truth for cross-module data (01-architecture §4).

Only the main agent edits this file (03-workflow rule). Any change requires a
changelog entry in docs/plan/01-architecture.md.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ActivationMode = Literal["wake_word", "hotkey", "both"]


class WindowInfo(BaseModel):
    hwnd: int
    title: str
    app: str
    monitor: int = 0


class SpeechSegment(BaseModel):
    """Raw captured speech. `pcm` is mono 16-bit little-endian at `sample_rate`."""

    pcm: bytes
    sample_rate: int = 16000
    started_at: datetime
    ended_at: datetime


class Transcript(BaseModel):
    text: str
    language: str | None = None
    confidence: float = 1.0
    source: Literal["mic", "fixture"] = "mic"


class ControlIntent(BaseModel):
    kind: Literal["stop", "cancel", "mute", "unmute", "sleep", "wake", "kill_all"]
    matched_text: str


class ContextSnapshot(BaseModel):
    active_window: WindowInfo | None = None
    time: datetime = Field(default_factory=datetime.now)
    clipboard: str | None = None


class Instruction(BaseModel):
    text: str
    transcript: Transcript
    context: ContextSnapshot


class InstructionReady(BaseModel):
    """A receiving window closed: the joined collected speech is ONE instruction (M6 T3)."""

    text: str


class Rect(BaseModel):
    x: int
    y: int
    w: int
    h: int


ElementAction = Literal["invoke", "toggle", "set_value", "select", "expand", "focus", "read"]


class Element(BaseModel):
    ref: str
    role: str
    name: str
    value: str | None = None
    bbox: Rect
    enabled: bool = True
    actions: set[ElementAction] = Field(default_factory=set)
    source: Literal["uia", "vision"] = "uia"
    password: bool = False


class PerceptionSnapshot(BaseModel):
    active_window: WindowInfo | None = None
    elements: list[Element] = Field(default_factory=list)
    screenshot: Path | None = None
    marked_image: Path | None = None
    taken_at: datetime = Field(default_factory=datetime.now)


class PerceptionHint(BaseModel):
    """Optional focus for `ScreenPerception.snapshot(hint)` (01-architecture §3.6).

    `prefer_vision` forces the SoM vision fallback even on a rich UIA tree (D5);
    `query` narrows what perception should look for.
    """

    query: str | None = None
    prefer_vision: bool = False


RiskLevel = Literal["L0", "L1", "L2", "L3"]

ActionKind = Literal[
    "uia_invoke", "uia_toggle", "uia_set_value", "uia_select", "click", "double_click",
    "right_click", "type_text", "hotkey", "scroll", "focus_window", "shell", "wait",
    "ask_user", "done", "fail", "create_task",
]


class ActionRequest(BaseModel):
    kind: ActionKind
    target_ref: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    rationale: str
    risk: RiskLevel = "L1"


class ActionResult(BaseModel):
    ok: bool
    observation: str
    error: str | None = None
    snapshot_after: PerceptionSnapshot | None = None


class TaskResult(BaseModel):
    ok: bool
    summary_to_speak: str
    details: str | None = None


TaskMode = Literal[
    "immediate_reply", "foreground", "background_monitor", "background_job", "scheduled"
]

TaskState = Literal["pending", "waiting", "running", "paused", "completed", "failed",
                    "cancelled"]


class MonitorSpec(BaseModel):
    kind: Literal["url_diff", "api_field", "file_hash", "rss"]
    target: str
    interval_s: int = Field(gt=0)
    condition: str


class TaskSpec(BaseModel):
    id: str
    instruction: str
    mode: TaskMode = "foreground"
    monitor: MonitorSpec | None = None
    schedule: datetime | None = None
    created_by: str = "user"


class TaskEvent(BaseModel):
    task_id: str
    type: Literal["started", "step", "waiting", "lease_request", "result", "failed",
                  "cancelled"]
    payload: str = ""
    at: datetime = Field(default_factory=datetime.now)


class SpeakRequest(BaseModel):
    text: str
    priority: Literal["ack", "result", "background"] = "result"
    interrupt: bool = False


class SpeakingStarted(BaseModel):
    """TTS playback began (echo guard: suppress wake detection until Stopped)."""


class SpeakingStopped(BaseModel):
    """TTS playback ended (one event per utterance)."""


class WakeDetected(BaseModel):
    """Wake phrase scored >= threshold (M0-SP3: 0.5); carried for tuning and logs."""

    score: float


class HotkeyPressed(BaseModel):
    combo: str


class HotkeyReleased(BaseModel):
    combo: str


class ActivationDegraded(BaseModel):
    """An activation channel failed to start; `message` is spoken verbatim by the app."""

    channel: Literal["hotkey", "wake_word"]
    message: str


class LLMToolCall(BaseModel):
    name: str
    arguments: dict[str, object] = Field(default_factory=dict)


class LLMMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    # Base64 PNG data-URLs ("data:image/png;base64,...") — vision fallback path (D5).
    images: list[str] = Field(default_factory=list)
    # Assistant tool-call echo on the message stream (default empty; nothing
    # existing produces it): the Ollama native dialect round-trips it as
    # message.tool_calls with OBJECT arguments — openai_compat ignores it.
    tool_calls: list[LLMToolCall] = Field(default_factory=list)


class LLMRequest(BaseModel):
    messages: list[LLMMessage]
    tools: list[dict[str, object]] = Field(default_factory=list)
    max_tokens: int = 1024


class LLMResponse(BaseModel):
    text: str = ""
    tool_calls: list[LLMToolCall] = Field(default_factory=list)
    model: str = ""
    stop_reason: str | None = None


class ProviderCaps(BaseModel):
    vision: bool = False
    tool_call: bool = False
    json_schema: bool = False
