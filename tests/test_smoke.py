import tomllib
from pathlib import Path

import marshmellow


def test_package_imports_and_version() -> None:
    """__version__ must track pyproject.toml — the release bump edits only one file."""
    pyproject_version = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    assert marshmellow.__version__ == pyproject_version
