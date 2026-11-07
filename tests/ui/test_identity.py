"""App identity tests — tray/window/exe icons + quit cleanliness (M6-T5, F4).

Offscreen (QT_QPA_PLATFORM set in tests/conftest.py). Covers:

- the tray icon is non-null the moment the tray exists and shows (no Qt
  "No Icon set" warning), sourced from the `assets/mascot/` stills;
- the icon follows the mascot state: a MascotStateEvent on the bus swaps the
  tray icon, and idle/muted/attention render visibly distinct pixels;
- the App sets the identity QIcon app-wide AND on the lazily built main
  window (the tasks dashboard, §3.11);
- a static assert that `installer/marshmellow.spec` bundles the identity
  assets (`assets/mascot` datas) and stamps the exe with `marshmellow.ico`;
- `tools/export_mascot_assets.py --ico` produces a well-formed multi-size
  `marshmellow.ico`;
- the quit census: a GUI-mode (offscreen) app started and quit via
  TrayAction(kind="quit") leaves zero `marshmellow`/`piper_worker` processes
  within the injected shutdown budget — the piper subprocess (real on any
  machine with a voice installed, absent otherwise) must never outlive quit.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from marshmellow.app import App  # noqa: E402
from marshmellow.core.bus import EventBus  # noqa: E402
from marshmellow.core.config import Settings  # noqa: E402
from marshmellow.core.contracts import ActionResult, PerceptionSnapshot  # noqa: E402
from marshmellow.planner.fake import FakePlanner  # noqa: E402
from marshmellow.stt.fake import FakeSTT  # noqa: E402
from marshmellow.ui.mascot import MascotStateEvent  # noqa: E402
from marshmellow.ui.tray import MascotIconProvider, Tray, TrayAction  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPORT_TOOL = REPO_ROOT / "tools" / "export_mascot_assets.py"

#: The injected shutdown budget (M6-T5): quit must fully unwind — engine stop,
#: TTS subprocess termination, process exit — within this many seconds.
SHUTDOWN_BUDGET_S = 30.0


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _pump_until(qapp: QApplication, predicate: Callable[[], bool], timeout_s: float) -> bool:
    """Process Qt events until `predicate` holds or the timeout lapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _wait_until(predicate: Callable[[], bool], timeout_s: float, interval_s: float = 0.25) -> bool:
    """Plain wall-clock wait for non-Qt conditions (bus-thread flags, process
    census) — never a fixed sleep, and never a Qt pump where none is needed."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


# -- doubles (same seams as tests/ui/test_dashboard.py; GUI mode, all silent) ----


class _HeadlessHook:
    def install(self, on_key: object) -> int:
        return 0

    def run(self) -> None:
        return None

    def request_stop(self) -> None:
        return None

    def uninstall(self, handle: int) -> None:
        return None


class _SilentAudio:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _IdlePerception:
    def snapshot(self, hint: object = None, hwnd: int | None = None) -> PerceptionSnapshot:
        return PerceptionSnapshot()


class _RefusingActionLayer:
    def execute(
        self, req: object, snapshot: PerceptionSnapshot | None = None
    ) -> ActionResult:
        raise AssertionError("the identity tests must not act on the screen")


class _PassGate:
    def guard(self, execute: Callable[[ActionResult], ActionResult]) -> Callable[
        [ActionResult], ActionResult
    ]:
        return execute


class _SilentAsk:
    def ask(self, question: str, cancelled: Callable[[], bool] | None = None) -> str:
        return ""


def _gui_app(tmp_path: Path) -> App:
    """A GUI-mode App on injected fakes — TTS stays the real-or-fake build so
    the quit census observes whatever process ownership the machine produces."""
    return App(
        Settings(activation="hotkey"),
        headless=False,
        log_dir=tmp_path,
        stt=FakeSTT(),
        audio_engine=_SilentAudio(),  # type: ignore[arg-type]
        hotkey_backend=_HeadlessHook(),  # type: ignore[arg-type]
        perception=_IdlePerception(),  # type: ignore[arg-type]
        action_layer=_RefusingActionLayer(),  # type: ignore[arg-type]
        gate=_PassGate(),  # type: ignore[arg-type]
        ask_channel=_SilentAsk(),  # type: ignore[arg-type]
        planner=FakePlanner(),
        task_store_path=tmp_path / "tasks.json",
    )


# -- tray icon presence -----------------------------------------------------------


def test_tray_icon_non_null_and_no_warning(qapp: QApplication) -> None:
    """The tray carries the idle identity icon from construction; showing it
    must not log Qt's "No Icon set" warning."""
    from PySide6.QtCore import qInstallMessageHandler

    messages: list[str] = []

    def _collect(mode: object, context: object, message: object) -> None:
        text = message.decode("utf-8", "replace") if isinstance(message, bytes) else str(message)
        messages.append(text)

    bus = EventBus()
    try:
        previous = qInstallMessageHandler(_collect)
        tray = Tray(bus=bus)
        try:
            tray.show()
            for _ in range(3):
                qapp.processEvents()
            assert not tray.icon.isNull()
        finally:
            qInstallMessageHandler(previous)
        assert not any("No Icon set" in m for m in messages), messages
    finally:
        bus.stop()


def test_tray_icon_follows_mascot_state_event(qapp: QApplication) -> None:
    """A MascotStateEvent on the bus swaps the tray icon (queued onto the GUI
    thread — the bus drain thread never touches the QSystemTrayIcon)."""
    bus = EventBus()
    try:
        tray = Tray(bus=bus, icon_provider=MascotIconProvider())
        before = tray.icon.pixmap(64, 64).toImage()
        bus.emit_threadsafe(MascotStateEvent(state="muted"))
        assert _pump_until(
            qapp, lambda: tray.icon.pixmap(64, 64).toImage() != before, timeout_s=5.0
        )
    finally:
        bus.stop()


def test_idle_muted_attention_icons_visibly_distinct(qapp: QApplication) -> None:
    """The three identity-critical states render different pixels — a user can
    tell muted from idle and the lease-ask "attention" at a glance."""
    provider = MascotIconProvider()
    images = [
        provider.icon(state).pixmap(64, 64).toImage()  # type: ignore[arg-type]
        for state in ("idle", "muted", "attention")
    ]
    for img in images:
        assert not img.isNull()
    assert images[0] != images[1]
    assert images[0] != images[2]
    assert images[1] != images[2]


# -- window icon ------------------------------------------------------------------


def test_app_sets_identity_icon_on_app_and_main_window(
    qapp: QApplication, tmp_path: Path
) -> None:
    """App stamps the identity QIcon app-wide AND on the lazily built main
    window (the dashboard, §3.11) when open_ui summons it."""
    app = _gui_app(tmp_path)
    try:
        assert QApplication.instance() is not None
        assert not QApplication.instance().windowIcon().isNull()
        assert app.dashboard is None  # lazy until the tray's open_ui arrives
        app.bus.emit_threadsafe(TrayAction(kind="open_ui"))
        assert _pump_until(qapp, lambda: app.dashboard is not None, timeout_s=5.0)
        assert app.dashboard is not None and not app.dashboard.windowIcon().isNull()
    finally:
        app.bus.stop()


# -- installer spec (static assert) ------------------------------------------------


def test_spec_bundles_mascot_assets_and_exe_icon() -> None:
    """The bundle spec ships the identity stills (datas) and stamps the main
    exe with the generated .ico — a broken checkout fails at build time."""
    spec = (REPO_ROOT / "installer" / "marshmellow.spec").read_text(encoding="utf-8")
    assert "marshmellow.ico" in spec, "spec must reference the generated exe icon"
    assert "icon=" in spec, "the main EXE must carry the icon"
    assert '"mascot"' in spec, "spec datas must bundle assets/mascot into _internal"
    assert '"assets"' in spec, "spec datas must point at the repo assets dir"


# -- export tool --ico -------------------------------------------------------------


def test_export_tool_ico_flag_writes_valid_multi_size_ico(tmp_path: Path) -> None:
    """`--ico` produces marshmellow.ico: an ICONDIR of PNG entries spanning
    small (16 px, tray) to large (256 px, exe) sizes."""
    result = subprocess.run(
        [sys.executable, str(EXPORT_TOOL), "--ico", "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    ico = tmp_path / "marshmellow.ico"
    assert ico.exists(), "the tool must write marshmellow.ico next to the stills"
    data = ico.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    assert count >= 2, f"expected a multi-size icon, got {count} entries"

    sizes: set[int] = set()
    heights: set[int] = set()
    offset = 6
    for _ in range(count):
        w, h, _colors, _res, planes, bpp, nbytes, img_off = struct.unpack(
            "<BBBBHHII", data[offset : offset + 16]
        )
        assert planes == 1 and bpp == 32
        entry = data[img_off : img_off + nbytes]
        assert entry[:8] == b"\x89PNG\r\n\x1a\n", "entries must be PNG-encoded"
        sizes.add(256 if w == 0 else w)
        heights.add(256 if h == 0 else h)
        offset += 16
    assert sizes == heights, "icon entries must be square"
    assert 16 in sizes and 256 in sizes, f"tray+exe sizes missing: {sorted(sizes)}"


# -- quit census -------------------------------------------------------------------


def _marshmellow_processes(since_epoch: float) -> list[int]:
    """PIDs of marshmellow/piper_worker processes created after `since_epoch`.

    Name matches cover the frozen layout (Marshmellow.exe / piper_worker.exe);
    the cmdline match covers the dev layout (python -m
    marshmellow.tts.piper_worker). Creation-time scoping keeps unrelated
    long-running installs out of the census.
    """
    try:
        import psutil

        me = os.getpid()
        found: list[int] = []
        for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            try:
                info = proc.info
            except Exception:  # noqa: BLE001 — a vanished process is not a match
                continue
            if info["pid"] == me or info["create_time"] is None:
                continue
            if info["create_time"] < since_epoch:
                continue
            name = (info["name"] or "").lower()
            cmdline = " ".join(info["cmdline"] or []).lower()
            if (
                name in ("marshmellow.exe", "piper_worker.exe")
                or "marshmellow.tts.piper_worker" in cmdline
            ):
                found.append(info["pid"])
        return found
    except ImportError:  # pragma: no cover — psutil is a dev dependency
        return []


def test_quit_via_tray_action_leaves_no_processes(qapp: QApplication, tmp_path: Path) -> None:
    """Quit census (M6-T5): GUI-mode (offscreen) app → TrayAction(kind="quit")
    → within the injected shutdown budget zero marshmellow/piper_worker
    processes remain. On a machine with a piper voice the app really owns the
    GPL-isolated worker subprocess — quit must terminate it (no zombie)."""
    started = time.time()
    app = _gui_app(tmp_path)
    app.start()
    stopped = False
    try:
        # The TTS subprocess (dev: python -m marshmellow.tts.piper_worker) is
        # spawned by start(); wait for it so the census proves teardown, not a
        # spawn race. Voiceless machines spawn nothing — the census still
        # guards the full quit path.
        assert _wait_until(
            lambda: bool(_marshmellow_processes(started)), timeout_s=20.0
        ), "the app spawned no piper worker to census — TTS build changed?"

        app.bus.emit_threadsafe(TrayAction(kind="quit"))
        assert _wait_until(
            lambda: app.quit_requested, timeout_s=SHUTDOWN_BUDGET_S
        ), "quit_requested never set — the tray quit path is broken"

        # The entry-point poller's unwind (__main__._check_quit): stop on the
        # GUI thread, never from the bus thread.
        app.stop()
        stopped = True

        deadline = time.monotonic() + SHUTDOWN_BUDGET_S
        while time.monotonic() < deadline and _marshmellow_processes(started):
            time.sleep(0.2)
        survivors = _marshmellow_processes(started)
        assert survivors == [], f"processes outlived quit: {survivors}"
    finally:
        if not stopped:
            try:
                app.stop()
            except Exception:  # noqa: BLE001 — teardown of a failed quit is best-effort
                pass
