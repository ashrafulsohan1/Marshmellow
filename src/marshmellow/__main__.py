"""Live entry point — `uv run python -m marshmellow` (or the `marshmellow` script).

Starts the real composition: tray + mascot + mic → VAD → STT → planner → piper.
This is the GUI entry the M1 DoD deferred to M2 ("lands with the audio loop"):
headless entries (the simulator, tests) never import this module.

`--smoke` (M5 Task 7, OQ-9): the frozen build's install verification — one
headless start/stop cycle, no Qt windows, no mic, no onboarding wizard, prints
"smoke ok", exits 0. `dist/Marshmellow/Marshmellow.exe --smoke` is what proves
the bundle imports on a machine without a Python install.

Shutdown: the tray's Quit publishes TrayAction → App sets quit_requested; a Qt
timer polls it on the main thread and unwinds — engine teardown happens here,
never from a bus thread.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from marshmellow.app import App
from marshmellow.core.config import default_config_path, load_settings
from marshmellow.ui.onboarding import OnboardingWizard, should_onboard


def main(
    argv: list[str] | None = None, *, app_factory: Callable[..., App] = App
) -> int:
    parser = argparse.ArgumentParser(
        prog="marshmellow", description="Hands-free voice-controlled desktop agent"
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="headless start/stop verification (frozen-build install check)",
    )
    args = parser.parse_args(argv)
    if args.smoke:
        settings = load_settings(default_config_path())
        app = app_factory(settings, headless=True)  # headless: no Qt, no mic, no wizard
        app.start()
        app.stop()
        print("smoke ok")
        return 0

    qapp = QApplication.instance() or QApplication(sys.argv)
    settings = load_settings(default_config_path())
    # First run (no config found anywhere): the one-page onboarding dialog
    # collects the activation mode, API key and autostart BEFORE the engine
    # starts — the wizard mutates `settings` and persists them. Headless
    # entries (simulator, tests) never import this module, so they never see it.
    if should_onboard(default_config_path()):
        OnboardingWizard(settings).run()
    app = app_factory(settings, headless=False)
    app.start()

    poller = QTimer()
    poller.setInterval(200)

    def _check_quit() -> None:
        if app.quit_requested:
            poller.stop()
            app.stop()
            qapp.quit()

    poller.timeout.connect(_check_quit)
    poller.start()
    return qapp.exec()


if __name__ == "__main__":
    raise SystemExit(main())
