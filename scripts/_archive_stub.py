"""Helper: run archived script from stub in scripts/."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path


def run_archived(relpath: str, *, note: str = "") -> None:
    root = Path(__file__).resolve().parent
    target = root / "archive" / relpath
    if not target.is_file():
        print(f"Archived script not found: {target}", file=sys.stderr)
        raise SystemExit(1)
    if note:
        print(f"NOTE: {note}", file=sys.stderr)
    print(f"Running archived: scripts/archive/{relpath}", file=sys.stderr)
    sys.argv[0] = str(target)
    runpy.run_path(str(target), run_name="__main__")
