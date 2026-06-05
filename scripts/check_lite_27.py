#!/usr/bin/env python3
"""ARCHIVED → scripts/archive/incidents/check_lite_27.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _archive_stub import run_archived

if __name__ == "__main__":
    run_archived("incidents/check_lite_27.py")
