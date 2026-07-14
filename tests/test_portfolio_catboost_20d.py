"""Portfolio CatBoost 20d advisory (log_only)."""

from services.portfolio_catboost_signal import portfolio_ml_20d_regime_hint


def test_regime_hint_align_uptrend():
    assert portfolio_ml_20d_regime_hint(70.0, "melt_up") == "align_uptrend"
    assert portfolio_ml_20d_regime_hint(35.0, "breakdown") == "align_breakdown"
    assert portfolio_ml_20d_regime_hint(70.0, "breakdown") == "conflict_long_in_breakdown"
    assert portfolio_ml_20d_regime_hint(None, "neutral") == "no_score"


def test_predict_20d_disabled_or_missing():
    from unittest.mock import patch

    from services.portfolio_catboost_signal import predict_portfolio_expected_return_20d

    with patch(
        "services.portfolio_catboost_signal.get_config_value",
        side_effect=lambda k, d="": "false" if "ENABLED" in k else d,
    ):
        out = predict_portfolio_expected_return_20d("MU")
    assert out.get("portfolio_ml_20d_status") == "disabled"
    assert out.get("portfolio_ml_20d_horizon_days") == 20
