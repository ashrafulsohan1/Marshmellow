"""Shared test configuration: Qt must run headless in CI and in dev test runs."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
