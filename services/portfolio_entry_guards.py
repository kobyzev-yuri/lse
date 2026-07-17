"""
Портфельная игра: CatBoost-фильтр входа и снимок ML для context_json на BUY.
Выход по тейку — ExecutionAgent.check_stop_losses (стратегия / PORTFOLIO_TAKE_PROFIT_PCT).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from config_loader import get_config_value

logger = logging.getLogger(__name__)


def _truthy(raw: str) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def portfolio_ml_snapshot(ticker: str) -> Dict[str, Any]:
    """Поля CatBoost 5d + 20d + prospect для context_json (без исключений наружу)."""
    out: Dict[str, Any] = {}
    try:
        from services.portfolio_catboost_signal import (
            predict_portfolio_expected_return,
            predict_portfolio_expected_return_20d,
            portfolio_ml_20d_regime_hint,
        )

        out.update(dict(predict_portfolio_expected_return(ticker) or {}))
        out.update(dict(predict_portfolio_expected_return_20d(ticker) or {}))
        try:
            from services.portfolio_trend_regime import (
                compute_portfolio_prospect_priority,
                portfolio_trend_regime_snapshot,
            )

            snap = portfolio_trend_regime_snapshot(ticker)
            reg = snap.get("portfolio_trend_regime")
            hint = portfolio_ml_20d_regime_hint(
                out.get("portfolio_ml_20d_entry_score"),
                str(reg) if reg is not None else None,
            )
            out["portfolio_ml_20d_regime_hint"] = hint
            out["portfolio_ml_20d_rule_regime"] = reg
            out.update(
                compute_portfolio_prospect_priority(
                    regime=str(reg) if reg is not None else None,
                    ret_20d_pct=snap.get("portfolio_trend_ret_20d_pct"),
                    score_20d=out.get("portfolio_ml_20d_entry_score"),
                    exp_20d_pct=out.get("portfolio_ml_20d_expected_return_pct"),
                    hint=hint,
                )
            )
            try:
                from services.portfolio_peer_rank import portfolio_peer_relative_rank

                out.update(
                    portfolio_peer_relative_rank(
                        ticker,
                        ret_20d_pct=snap.get("portfolio_trend_ret_20d_pct"),
                    )
                )
            except Exception as e:
                logger.debug("portfolio peer rank %s: %s", ticker, e)
        except Exception:
            out.setdefault("portfolio_ml_20d_regime_hint", "no_regime")
        try:
            from services.options_card_context import build_options_card_context

            opts = build_options_card_context(ticker)
            if isinstance(opts, dict):
                out["portfolio_options_status"] = opts.get("status")
                out["portfolio_options_gate_hint"] = opts.get("gate_hint")
                out["portfolio_options_structure_gate_hint"] = opts.get("structure_gate_hint")
                out["portfolio_options_sentiment_label"] = opts.get("sentiment_label")
                out["portfolio_options_sentiment_score"] = opts.get("sentiment_score")
        except Exception as e:
            logger.debug("portfolio options snapshot %s: %s", ticker, e)
    except Exception as e:
        logger.debug("portfolio_ml_snapshot %s: %s", ticker, e)
        out.setdefault("portfolio_ml_status", "error")
        out.setdefault("portfolio_ml_note", str(e))
    return out


def portfolio_options_sentiment_blocks_buy(ticker: str) -> Tuple[bool, str]:
    """
    Options → portfolio. Default log_only (не блокирует), apply только при
    DECISION_STACK_PORTFOLIO_OPTIONS_GATE_MODE=apply.
    """
    try:
        from services.decision_stack._types import gate_mode
        from services.options_card_context import build_options_card_context

        opts = build_options_card_context(ticker)
        hint = str(opts.get("gate_hint") or "")
        struct = str(opts.get("structure_gate_hint") or "")
        would = hint == "would_downgrade" or struct == "would_downgrade"
        if not would:
            return False, ""
        reason = (
            f"options gate_hint={hint} structure={struct} "
            f"label={opts.get('sentiment_label')} score={opts.get('sentiment_score')}"
        )
        if gate_mode("DECISION_STACK_PORTFOLIO_OPTIONS_GATE_MODE", "log_only") != "apply":
            return False, f"log_only: {reason}"
        return True, reason
    except Exception as e:
        logger.debug("portfolio_options_sentiment_blocks_buy %s: %s", ticker, e)
        return False, ""


def portfolio_catboost_blocks_buy(ticker: str) -> Tuple[bool, str]:
    """
    True — не открывать новый portfolio BUY (докуп при открытой позиции не вызывается).
    """
    if not _truthy(get_config_value("PORTFOLIO_CATBOOST_BLOCK_BUY_ON_WEAK", "true")):
        return False, ""
    try:
        from services.decision_stack._types import READINESS_PRODUCTION, stack_readiness

        readiness = stack_readiness("portfolio_catboost")
        if readiness != READINESS_PRODUCTION:
            return False, f"CatBoost readiness={readiness}: runtime block disabled until production gate"
    except Exception:
        pass
    try:
        min_score = float((get_config_value("PORTFOLIO_CATBOOST_HOLD_BELOW_SCORE", "48") or "48").strip())
    except (ValueError, TypeError):
        min_score = 48.0

    snap = portfolio_ml_snapshot(ticker)
    status = (snap.get("portfolio_ml_status") or "").strip()
    if status != "ok":
        return False, ""
    score = snap.get("portfolio_ml_entry_score")
    if score is None:
        return False, ""
    try:
        sc = float(score)
    except (TypeError, ValueError):
        return False, ""
    if sc < min_score:
        exp = snap.get("portfolio_ml_expected_return_pct")
        return True, (
            f"CatBoost entry_score={sc:.1f} < {min_score:.1f} "
            f"(expected_5d_pct={exp}, PORTFOLIO_CATBOOST_BLOCK_BUY_ON_WEAK)"
        )
    return False, ""


def portfolio_indicator_blocks_buy(ticker: str) -> Tuple[bool, str]:
    """Тикер только индикатор (металлы/нефть/VIX) — не открываем portfolio BUY."""
    try:
        from services.ticker_groups import get_tickers_indicator_only

        ind = {t.strip().upper() for t in get_tickers_indicator_only()}
        if ticker.strip().upper() in ind:
            return True, f"{ticker} в TICKERS_INDICATOR_ONLY (только корреляция)"
    except Exception:
        pass
    return False, ""


def portfolio_trend_blocks_buy(ticker: str) -> Tuple[bool, str]:
    """Late-chase + 20d prospect apply-гейт (+ options apply if enabled)."""
    try:
        from services.portfolio_trend_regime import (
            portfolio_trend_20d_blocks_buy,
            portfolio_trend_late_chase_blocks_buy,
        )

        blocked, reason = portfolio_trend_late_chase_blocks_buy(ticker)
        if blocked:
            return True, reason
        blocked, reason = portfolio_trend_20d_blocks_buy(ticker)
        if blocked:
            return True, reason
        return portfolio_options_sentiment_blocks_buy(ticker)
    except Exception as e:
        logger.debug("portfolio_trend_blocks_buy %s: %s", ticker, e)
        return False, ""


def merge_portfolio_buy_context(
    context_json: Optional[Dict[str, Any]],
    ticker: str,
    *,
    base_take_profit: Optional[float] = None,
    strategy_decision: Optional[Dict[str, Any]] = None,
    portfolio_ml: Optional[Dict[str, Any]] = None,
    event_reaction: Optional[Dict[str, Any]] = None,
    cluster_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    base = dict(context_json) if isinstance(context_json, dict) else {}
    ml = dict(portfolio_ml) if isinstance(portfolio_ml, dict) else portfolio_ml_snapshot(ticker)
    for k, v in ml.items():
        if (
            k.startswith("portfolio_ml_")
            or k.startswith("portfolio_prospect_")
            or k.startswith("portfolio_peer_")
            or k.startswith("portfolio_options_")
        ):
            base[k] = v
    try:
        from services.portfolio_multiday_signal import portfolio_multiday_snapshot

        md = portfolio_multiday_snapshot(ticker)
        for k, v in md.items():
            if k.startswith(("portfolio_multiday_", "multiday_lr_", "log_return_multiday")):
                base[k] = v
    except Exception as e:
        logger.debug("portfolio multiday snapshot %s: %s", ticker, e)
    try:
        from services.event_reaction_entry_guards import event_reaction_ml_snapshot

        er = dict(event_reaction) if isinstance(event_reaction, dict) else event_reaction_ml_snapshot(ticker)
        for k, v in er.items():
            if k.startswith("event_reaction_ml_"):
                base[k] = v
    except Exception as e:
        logger.debug("event_reaction snapshot %s: %s", ticker, e)
    try:
        from services.portfolio_trend_regime import portfolio_trend_regime_snapshot

        tr = portfolio_trend_regime_snapshot(ticker)
        for k, v in tr.items():
            if k.startswith("portfolio_trend_"):
                base[k] = v
    except Exception as e:
        logger.debug("portfolio trend snapshot %s: %s", ticker, e)
    if base_take_profit is not None and base_take_profit > 0:
        try:
            from services.portfolio_exit_policy import compute_entry_effective_take_for_ticker

            eff, note = compute_entry_effective_take_for_ticker(ticker, float(base_take_profit), base)
            if eff is not None and eff > 0:
                base["portfolio_effective_take_pct_at_entry"] = round(eff, 3)
                base["portfolio_effective_take_note"] = note
        except Exception as e:
            logger.debug("entry effective take %s: %s", ticker, e)
    if strategy_decision is not None:
        try:
            from services.decision_stack import finalize_portfolio_decision_stack

            row = dict(strategy_decision)
            finalize_portfolio_decision_stack(
                row,
                ticker=ticker,
                portfolio_ml=ml,
                event_reaction=er if "er" in locals() else None,
                cluster_context=cluster_context,
            )
            for k in (
                "decision_snapshot",
                "decision_effective",
                "decision_stack_projected_effective",
                "decision_stack_version",
                "decision_verdict",
            ):
                if row.get(k) is not None:
                    base[k] = row[k]
        except Exception as e:
            logger.debug("portfolio decision_stack %s: %s", ticker, e)
    return base
