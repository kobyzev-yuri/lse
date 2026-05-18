"""
Реестр «продуктовых идей» GAME_5m: песочница → телеметрия в context_json → вердикт анализатора.

Статусы:
  sandbox   — в коде, оценка накоплением сделок
  production  — арбитр разрешил (вручную по отчёту)
  retired     — отключить флаг / убрать из UI

Анализатор: services/analyzer_product_ideas_arbiter.py
"""

from __future__ import annotations

from typing import Any, Dict, List

# id → описание идеи (не меняет config автоматически)
PRODUCT_IDEAS: Dict[str, Dict[str, Any]] = {
    "macro_vix_forex_risk": {
        "title_ru": "Макро VIX/Forex/нефть → entry_advice и премаркет-алерт",
        "config_flags": [
            "GAME_5M_MACRO_RISK_ENABLED",
            "PREMARKET_STRESS_USE_MACRO_RISK",
        ],
        "context_fields_entry": [
            "macro_risk_level",
            "macro_equity_gap_bias",
            "macro_indicators",
            "entry_advice",
        ],
        "min_trades_per_bucket": 8,
        "arbiter_metric": "entry_pnl_by_macro_level",
    },
    "macro_predicted_sector_gap": {
        "title_ru": "Числовой прогноз гэпа сектора (SMH) из VIX/Forex/CL",
        "config_flags": ["GAME_5M_MACRO_PREDICT_SECTOR_GAP_ENABLED"],
        "context_fields_entry": ["macro_predicted_sector_gap_pct", "macro_sector_proxy"],
        "min_trades_per_bucket": 12,
        "arbiter_metric": "predicted_gap_calibration",
    },
    "game5m_gap_forecast_log": {
        "title_ru": "Лог pred vs open (game5m_gap_forecast_daily) + калибровка OLS",
        "config_flags": [
            "GAME_5M_GAP_FORECAST_LOG_ENABLED",
            "GAME_5M_MACRO_PREDICT_SECTOR_GAP_ENABLED",
        ],
        "context_fields_entry": ["macro_predicted_sector_gap_pct", "rth_open_gap_pct", "premarket_gap_pct"],
        "min_trades_per_bucket": 12,
        "arbiter_metric": "gap_forecast_arbiter",
    },
    "macro_defer_time_exit_early": {
        "title_ru": "Не закрывать TIME_EXIT_EARLY до open при прогнозе гэпа вверх",
        "config_flags": ["GAME_5M_MACRO_DEFER_EARLY_EXIT_ENABLED"],
        "context_fields_exit": ["macro_defer_early_exit_applied"],
        "min_trades_per_bucket": 10,
        "arbiter_metric": "defer_early_exit_counterfactual",
        "status": "planned",
    },
}


def list_active_ideas() -> List[str]:
    return list(PRODUCT_IDEAS.keys())
