#!/usr/bin/env python3
"""ARCHIVED → scripts/archive/ml/. Use run_gap_forecast_refresh.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _archive_stub import run_archived

if __name__ == "__main__":
    run_archived(
        "ml/train_premarket_gap_model.py",
        note="Superseded by gap_forecast contour (ingest + run_gap_forecast_refresh.py)",
    )
