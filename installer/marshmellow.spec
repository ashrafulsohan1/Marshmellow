# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — the M5 installer bundle (OQ-9 decision (a), M5 Task 7).

ONE-DIR bundle, TWO exes (both console=False):

- ``dist/Marshmellow/Marshmellow.exe``  ← ``src/marshmellow/__main__.py``
  (tray + mascot GUI; ``--smoke`` runs it headless for the install check)
- ``dist/Marshmellow/piper_worker.exe`` ← ``installer/piper_worker_entry.py``
  (the SP7 GPL-isolated piper subprocess; ``worker_argv``'s frozen branch
  points here — the second exe sits in the dist root next to the main one)

Datas (build-time fetched by ``installer/fetch_bundle_inputs.py`` — never
committed; see .gitignore):

- ``assets/wake``                     → ``_internal/wake`` (D8 self-trained model,
  Apache-2.0 feature extractors — license check recorded in assets/wake/README.md,
  so the wake path SHIPS; a hotkey-only bundle would drop this directory)
- ``voices/<voice>.onnx(+.json)``     → ``_internal/voices`` (piper voice)
- ``models/faster-whisper-base``      → ``_internal/models/faster-whisper-base``
  (ct2 — a clean account installs offline-capable except the BYOK call)

In one-dir mode ``sys._MEIPASS`` is ``dist/Marshmellow/_internal`` — exactly
what ``core/assets.py::asset_dir`` resolves. The one-dir layout keeps the two
exes' shared runtimes (onnxruntime, numpy, …) in a single ``_internal`` tree
(one-dir also starts far faster than one-file's extraction, which matters for
the ≤ 10 s cold-start DoD).
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).resolve().parent  # installer/ → the repo root
VOICE = "en_US-lessac-medium"  # M0-SP7 default (config.tts_voice); must match fetch_bundle_inputs
VOICES = ROOT / "voices"
STT_DIR = ROOT / "models" / "faster-whisper-base"


def _require(label: str, paths: list[Path]) -> None:
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise SystemExit(
            f"bundle input(s) missing for {label}: {missing}\n"
            "  → run `uv run python installer/fetch_bundle_inputs.py` first "
            "(re-downloadable binaries stay out of git)"
        )


_voice_onnx = VOICES / f"{VOICE}.onnx"
_voice_json = VOICES / f"{VOICE}.onnx.json"
_require("piper voice", [_voice_onnx, _voice_json])
_require("faster-whisper ct2 model", [STT_DIR / "model.bin"])

datas = [
    # D8: the self-trained wake model ships (assets/wake/README.md license check)
    (str(ROOT / "assets" / "wake"), "wake"),
    (str(_voice_onnx), "voices"),
    (str(_voice_json), "voices"),
    (str(STT_DIR), "models/faster-whisper-base"),
]
a_main = Analysis(
    [str(ROOT / "src" / "marshmellow" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    excludes=["marshmellow.tts.piper_worker", "tkinter"],  # SP7: no piper import in the GUI graph
)
pyz_main = PYZ(a_main.pure)

# piper 1.8 ships espeak-ng-data / tashkeel / hebrew / templates as package
# DATA (45 MB): PyInstaller collects the .pyd bridge but not these directories,
# and the worker cannot phonemize without them. Worker-side need; in one-dir
# mode _internal is shared, so the GUI exe sees them without importing them.
a_worker = Analysis(
    [str(ROOT / "installer" / "piper_worker_entry.py")],
    pathex=[str(ROOT / "src")],
    datas=datas + collect_data_files("piper"),
)
pyz_worker = PYZ(a_worker.pure)

exe_main = EXE(
    pyz_main,
    a_main.scripts,
    [],
    name="Marshmellow",
    console=False,
    exclude_binaries=True,
)
exe_worker = EXE(
    pyz_worker,
    a_worker.scripts,
    [],
    name="piper_worker",
    console=False,
    exclude_binaries=True,
)

coll = COLLECT(
    exe_main,
    exe_worker,
    a_main.binaries,
    a_main.datas,
    a_worker.binaries,
    a_worker.datas,
    name="Marshmellow",
)
