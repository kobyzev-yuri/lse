#!/usr/bin/env python3
"""ARCHIVED → scripts/archive/ml/. Use run_recovery_ml_refresh.py (dispatcher)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _archive_stub import run_archived

if __name__ == "__main__":
    run_archived(
        "ml/run_daily_game5m_recovery_pipeline.py",
        note="Legacy recovery pipeline. Prefer: scripts/run_recovery_ml_refresh.py --full",
    )
