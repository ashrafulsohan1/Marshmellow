"""PyInstaller entry for the bundle's SECOND exe: the isolated piper worker.

The GUI exe (``Marshmellow.exe``, from ``src/marshmellow/__main__.py``) never
imports piper — it talks to this worker over stdin/stdout exactly like the dev
layout does (``python -m marshmellow.tts.piper_worker``), just with the frozen
argv shape ``worker_argv`` produces (M5 Task 7, Review Focus 1). Importing the
worker module here keeps the SP7 GPL boundary intact under freezing: piper is
imported only inside this process.
"""

from marshmellow.tts.piper_worker import main

if __name__ == "__main__":
    raise SystemExit(main())
