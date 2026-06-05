#!/usr/bin/env bash
# ARCHIVED → scripts/archive/ml/daily_game5m_ml_after_close.sh
# Prefer: python3 scripts/run_daily_game5m_ml_pipeline.py
echo "NOTE: archived wrapper; prefer scripts/run_daily_game5m_ml_pipeline.py" >&2
exec "$(dirname "$0")/archive/ml/daily_game5m_ml_after_close.sh" "$@"
