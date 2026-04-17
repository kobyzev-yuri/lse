"""
Карточки портфельной игры (trading_cycle): дневные котировки из БД quotes,
AnalystAgent + StrategyManager — отдельно от интрадей GAME_5M.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from config_loader import get_config_value

logger = logging.getLogger(__name__)


def get_portfolio_trade_tickers() -> List[str]:
    """Тикеры, по которым trading_cycle открывает позиции (MEDIUM+LONG без индикаторов)."""
    from services.ticker_groups import get_tickers_for_portfolio_game, get_tickers_indicator_only

    full = get_tickers_for_portfolio_game()
    ind = set(get_tickers_indicator_only())
    return [t for t in full if t not in ind]


def get_portfolio_cluster_context(days: int = 30) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """
    Кластер корреляции как в trading_cycle_cron: при наличии индикаторов в списке —
    матрица по полному списку; иначе только по торгуемым.
    Возвращает (cluster_context | None, trade_tickers).
    """
    from services.cluster_recommend import get_correlation_matrix
    from services.ticker_groups import get_tickers_for_portfolio_game, get_tickers_indicator_only

    full_list = get_tickers_for_portfolio_game()
    trade = [t for t in full_list if t not in set(get_tickers_indicator_only())]
    indicator_only = set(get_tickers_indicator_only())
    list_for_corr = full_list if indicator_only else trade
    if len(list_for_corr) < 2:
        return None, trade
    try:
        corr = get_correlation_matrix(list_for_corr, days=days)
        if not corr:
            return None, trade
        return {"tickers": list_for_corr, "correlation": corr, "other_signals": {}}, trade
    except Exception as e:
        logger.debug("portfolio cluster: %s", e)
        return None, trade


def _truncate(s: Optional[str], max_len: int) -> Optional[str]:
    if s is None:
        return None
    t = str(s).strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def portfolio_card_payload(
    ticker: str,
    analyst_result: Dict[str, Any],
    *,
    fallback_take_pct: float,
) -> Dict[str, Any]:
    """
    Плоский payload для веб-карточки: решение стратегии, цели take/stop, дневные тех. поля.
    analyst_result — выход AnalystAgent.get_decision_with_llm(...).
    """
    td = analyst_result.get("technical_data") if isinstance(analyst_result.get("technical_data"), dict) else {}
    sr = analyst_result.get("strategy_result")
    if sr is not None and not isinstance(sr, dict):
        sr = None
    strat_take = sr.get("take_profit") if sr else None
    strat_stop = sr.get("stop_loss") if sr else None
    try:
        eff_take = float(strat_take) if strat_take is not None else float(fallback_take_pct)
    except (TypeError, ValueError):
        eff_take = float(fallback_take_pct) if fallback_take_pct else None
    try:
        eff_stop = float(strat_stop) if strat_stop is not None else None
    except (TypeError, ValueError):
        eff_stop = None

    take_source = "strategy" if strat_take is not None else "PORTFOLIO_TAKE_PROFIT_PCT"

    out: Dict[str, Any] = {
        "ticker": ticker,
        "horizon": "daily",
        "horizon_note": "Дневные свечи и индикаторы из таблицы quotes (не 5m).",
        "decision": analyst_result.get("decision"),
        "base_decision": analyst_result.get("base_decision"),
        "selected_strategy": analyst_result.get("selected_strategy"),
        "technical_signal": analyst_result.get("technical_signal"),
        "close": td.get("close"),
        "sma_5": td.get("sma_5"),
        "rsi": td.get("rsi"),
        "volatility_5": td.get("volatility_5"),
        "avg_volatility_20": td.get("avg_volatility_20"),
        "vix_regime": td.get("vix_regime"),
        "vix_value": td.get("vix_value"),
        "open_price": td.get("open_price"),
        "prev_day_return_pct": td.get("prev_day_return_pct"),
        "current_day_return_pct": td.get("current_day_return_pct"),
        "sentiment_normalized": analyst_result.get("sentiment_normalized"),
        "news_count": analyst_result.get("news_count"),
        "strategy_take_profit_pct": strat_take,
        "strategy_stop_loss_pct": strat_stop,
        "effective_take_profit_pct": eff_take,
        "effective_stop_loss_pct": eff_stop,
        "take_profit_source": take_source,
        "strategy_reasoning": _truncate((sr or {}).get("reasoning"), 500) if sr else None,
        "strategy_insight": _truncate((sr or {}).get("insight"), 400) if sr else None,
        "cluster_note": _truncate(td.get("cluster_note"), 450),
    }
    try:
        from utils.risk_manager import get_risk_manager

        rm = get_risk_manager()
        out["risk_limits"] = {
            "max_position_usd": rm.get_max_position_size(ticker),
            "max_ticker_exposure_pct": rm.get_max_single_ticker_exposure(),
            "stop_loss_pct_default": rm.get_stop_loss_percent(),
            "take_profit_pct_default": rm.get_take_profit_percent(),
        }
    except Exception as e:
        logger.debug("risk_limits for portfolio card: %s", e)
        out["risk_limits"] = None
    return out


def portfolio_llm_public_payload(full: Dict[str, Any]) -> Dict[str, Any]:
    """Убираем огромные промпты из ответа API (по желанию можно запросить отдельно)."""
    out = {k: v for k, v in full.items() if k not in ("prompt_system", "prompt_user", "llm_response_raw")}
    return out


def load_fallback_portfolio_take_pct() -> float:
    try:
        return float((get_config_value("PORTFOLIO_TAKE_PROFIT_PCT", "0") or "0").strip() or "0")
    except (ValueError, TypeError):
        return 0.0
