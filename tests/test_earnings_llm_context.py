from services.earnings_llm_context import (
    format_earnings_entry_context_for_llm,
    spillover_relevance_days,
)


def test_spillover_horizon_by_strategy():
    assert spillover_relevance_days("GAME_5M") == 2
    assert spillover_relevance_days("PORTFOLIO") == 5


def test_format_own_report_always_shown():
    text = format_earnings_entry_context_for_llm(
        {
            "symbol": "NVDA",
            "strategy": "PORTFOLIO",
            "spillover_horizon_days": 5,
            "peer_spillover_model_metrics": {},
            "own_report": {
                "role": "own",
                "source_symbol": "NVDA",
                "event_date": "2026-05-20",
                "days_from_report": 45,
                "management_tone": "bullish",
                "scenario_id": "gap_up_follow_through",
                "regression_ml": {
                    "status": "ok",
                    "direction": "UP",
                    "expected_return_5d_pct": 1.2,
                    "entry_score": 62,
                    "rmse_valid": 0.041,
                },
            },
            "spillover": None,
        }
    )
    assert "Последний отчёт NVDA" in text
    assert "+45 календ.дн." in text
    assert "не фиксирована" in text
    assert "RMSE_valid" in text


def test_format_spillover_with_model_metrics():
    text = format_earnings_entry_context_for_llm(
        {
            "symbol": "MU",
            "strategy": "GAME_5M",
            "spillover_horizon_days": 2,
            "own_report": None,
            "spillover": {
                "role": "peer_spillover",
                "source_symbol": "NVDA",
                "event_date": "2026-05-20",
                "days_from_report": 1,
                "peer_relation": "ai_infra_supply",
                "peer_edge_weight": 0.9,
                "spillover_game_horizon_note": "GAME_5M: только 1d",
                "peer_spillover_fact": {"forward_log_ret_1d_pct": "+0.50%"},
            },
            "peer_spillover_model_metrics": {
                "status": "ok",
                "n_train": 120,
                "sign_accuracy_valid": 0.52,
                "baseline_sign_accuracy_valid": 0.48,
                "rmse_valid": 0.038,
            },
        }
    )
    assert "Spillover от NVDA" in text
    assert "Spillover fact 1d" in text
    assert "sign_acc_valid=52%" in text
    assert "pilot" in text
