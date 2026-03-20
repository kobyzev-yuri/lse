from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from report_generator import get_engine, load_trade_history, compute_closed_trade_pnls
from services.recommend_5m import fetch_5m_ohlc
from services.deal_params_5m import normalize_entry_context


@dataclass
class TradeEffect:
    trade_id: int
    ticker: str
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    hold_minutes: float
    qty: float
    entry_price: float
    exit_price: float
    net_pnl: float
    realized_pct: float
    realized_log_return: float
    exit_signal: str
    exit_strategy: str
    potential_best_pct: Optional[float]
    preventable_worst_pct: Optional[float]
    missed_upside_pct: Optional[float]
    avoidable_loss_pct: Optional[float]
    likely_late_polling: bool
    entry_rsi_5m: Optional[float]
    entry_vol_5m_pct: Optional[float]
    entry_momentum_2h_pct: Optional[float]
    entry_prob_up: Optional[float]
    entry_prob_down: Optional[float]
    entry_news_impact: Optional[str]
    entry_advice: Optional[str]


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        if math.isfinite(f):
            return f
    except Exception:
        return None
    return None


def _as_et(ts: pd.Timestamp) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("America/New_York")
    return t.tz_convert("America/New_York")


def _load_closed_trades(days: int, strategy_name: Optional[str]) -> List[Any]:
    engine = get_engine()
    if strategy_name and strategy_name.upper() != "ALL":
        raw = load_trade_history(engine, strategy_name=strategy_name)
    else:
        raw = load_trade_history(engine)
    closed = compute_closed_trade_pnls(raw)
    if not closed:
        return []
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    out = []
    for t in closed:
        ts = pd.Timestamp(t.ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        if ts >= cutoff:
            out.append(t)
    return out


def _prepare_ohlc_cache(tickers: List[str], days: int) -> Dict[str, Optional[pd.DataFrame]]:
    cache: Dict[str, Optional[pd.DataFrame]] = {}
    fetch_days = min(7, max(3, days + 2))
    for t in sorted(set(tickers)):
        try:
            df = fetch_5m_ohlc(t, days=fetch_days)
            if df is None or df.empty:
                cache[t] = None
                continue
            d = df.copy()
            dt = pd.to_datetime(d["datetime"])
            if dt.dt.tz is None:
                dt = dt.dt.tz_localize("America/New_York", ambiguous="infer")
            else:
                dt = dt.dt.tz_convert("America/New_York")
            d["datetime"] = dt
            cache[t] = d.sort_values("datetime").reset_index(drop=True)
        except Exception:
            cache[t] = None
    return cache


def _slice_window(df: Optional[pd.DataFrame], start_et: pd.Timestamp, end_et: pd.Timestamp) -> Optional[pd.DataFrame]:
    if df is None or df.empty or end_et <= start_et:
        return None
    m = (df["datetime"] >= start_et) & (df["datetime"] <= end_et)
    w = df.loc[m]
    if w.empty:
        return None
    return w


def _estimate_trade_effects(closed_trades: List[Any], ohlc_cache: Dict[str, Optional[pd.DataFrame]]) -> List[TradeEffect]:
    effects: List[TradeEffect] = []
    for t in closed_trades:
        if not t.entry_ts:
            continue
        entry_ts = _as_et(pd.Timestamp(t.entry_ts))
        exit_ts = _as_et(pd.Timestamp(t.ts))
        if exit_ts <= entry_ts:
            continue
        qty = float(t.quantity)
        entry = float(t.entry_price)
        exit_p = float(t.exit_price)
        if qty <= 0 or entry <= 0 or exit_p <= 0:
            continue

        cost_basis = qty * entry
        realized_pct = (float(t.net_pnl) / cost_basis) * 100 if cost_basis > 0 else 0.0
        realized_log_return = float(np.log1p(realized_pct / 100.0)) if realized_pct > -100 else -999.0

        window = _slice_window(ohlc_cache.get(t.ticker), entry_ts, exit_ts)
        potential_best_pct = preventable_worst_pct = None
        missed_upside_pct = avoidable_loss_pct = None
        likely_late_polling = False
        if window is not None and not window.empty:
            try:
                mfe_price = float(window["High"].max())
                mae_price = float(window["Low"].min())
                potential_best_pct = ((mfe_price / entry) - 1.0) * 100.0
                preventable_worst_pct = ((mae_price / entry) - 1.0) * 100.0
                missed_upside_pct = max(0.0, potential_best_pct - realized_pct)
                avoidable_loss_pct = max(0.0, realized_pct - preventable_worst_pct)
                likely_late_polling = abs(exit_p - mfe_price) / mfe_price > 0.004 if mfe_price > 0 else False
            except Exception:
                pass

        entry_ctx = normalize_entry_context(getattr(t, "context_json", None))
        effects.append(
            TradeEffect(
                trade_id=int(t.trade_id),
                ticker=str(t.ticker),
                entry_ts=entry_ts,
                exit_ts=exit_ts,
                hold_minutes=float((exit_ts - entry_ts) / pd.Timedelta(minutes=1)),
                qty=qty,
                entry_price=entry,
                exit_price=exit_p,
                net_pnl=float(t.net_pnl),
                realized_pct=realized_pct,
                realized_log_return=realized_log_return,
                exit_signal=str(getattr(t, "signal_type", "") or ""),
                exit_strategy=str(getattr(t, "exit_strategy", "") or ""),
                potential_best_pct=potential_best_pct,
                preventable_worst_pct=preventable_worst_pct,
                missed_upside_pct=missed_upside_pct,
                avoidable_loss_pct=avoidable_loss_pct,
                likely_late_polling=likely_late_polling,
                entry_rsi_5m=_safe_float(entry_ctx.get("rsi_5m")),
                entry_vol_5m_pct=_safe_float(entry_ctx.get("volatility_5m_pct")),
                entry_momentum_2h_pct=_safe_float(entry_ctx.get("momentum_2h_pct")),
                entry_prob_up=_safe_float(entry_ctx.get("prob_up")),
                entry_prob_down=_safe_float(entry_ctx.get("prob_down")),
                entry_news_impact=(entry_ctx.get("kb_news_impact") or None),
                entry_advice=(entry_ctx.get("entry_advice") or None),
            )
        )
    return effects


def _aggregate(effects: List[TradeEffect]) -> Dict[str, Any]:
    if not effects:
        return {"total": 0}
    realized = [e.realized_pct for e in effects]
    net_pnl = [e.net_pnl for e in effects]
    logrets = [e.realized_log_return for e in effects if e.realized_log_return > -900]
    missed = [e.missed_upside_pct for e in effects if e.missed_upside_pct is not None]
    avoidable = [e.avoidable_loss_pct for e in effects if e.avoidable_loss_pct is not None]
    wins = [e for e in effects if e.realized_pct > 0]
    losses = [e for e in effects if e.realized_pct <= 0]

    by_exit: Dict[str, Dict[str, float]] = {}
    for e in effects:
        k = e.exit_signal or "UNKNOWN"
        by_exit.setdefault(k, {"count": 0, "avg_realized_pct": 0.0, "avg_missed_pct": 0.0})
        by_exit[k]["count"] += 1
        by_exit[k]["avg_realized_pct"] += e.realized_pct
        by_exit[k]["avg_missed_pct"] += e.missed_upside_pct or 0.0
    for v in by_exit.values():
        c = max(1, int(v["count"]))
        v["avg_realized_pct"] = round(v["avg_realized_pct"] / c, 3)
        v["avg_missed_pct"] = round(v["avg_missed_pct"] / c, 3)

    late_polling_count = sum(1 for e in effects if e.likely_late_polling and (e.missed_upside_pct or 0) > 0.3)
    high_vol_losses = [e for e in losses if e.entry_vol_5m_pct is not None and e.entry_vol_5m_pct >= 0.6]
    weak_prob_entries = [e for e in losses if e.entry_prob_up is not None and e.entry_prob_up < 0.55]
    neg_news_losses = [e for e in losses if (e.entry_news_impact or "").lower().startswith("негатив")]

    return {
        "total": len(effects),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(effects), 2),
        "sum_net_pnl_usd": round(float(sum(net_pnl)), 2),
        "avg_realized_pct": round(float(np.mean(realized)), 3),
        "median_realized_pct": round(float(np.median(realized)), 3),
        "avg_log_return": round(float(np.mean(logrets)), 5) if logrets else None,
        "sum_missed_upside_pct": round(float(sum(missed)), 3),
        "avg_missed_upside_pct": round(float(np.mean(missed)), 3) if missed else None,
        "sum_avoidable_loss_pct": round(float(sum(avoidable)), 3),
        "avg_avoidable_loss_pct": round(float(np.mean(avoidable)), 3) if avoidable else None,
        "late_polling_signals": late_polling_count,
        "high_vol_losses_count": len(high_vol_losses),
        "weak_prob_up_losses_count": len(weak_prob_entries),
        "negative_news_losses_count": len(neg_news_losses),
        "by_exit_signal": by_exit,
    }


def _top_cases(effects: List[TradeEffect], limit: int = 8) -> Dict[str, List[Dict[str, Any]]]:
    def row(e: TradeEffect) -> Dict[str, Any]:
        return {
            "trade_id": e.trade_id,
            "ticker": e.ticker,
            "entry_ts": e.entry_ts.isoformat(),
            "exit_ts": e.exit_ts.isoformat(),
            "exit_signal": e.exit_signal,
            "realized_pct": round(e.realized_pct, 3),
            "missed_upside_pct": round(e.missed_upside_pct or 0.0, 3),
            "avoidable_loss_pct": round(e.avoidable_loss_pct or 0.0, 3),
            "entry_rsi_5m": e.entry_rsi_5m,
            "entry_vol_5m_pct": e.entry_vol_5m_pct,
            "entry_prob_up": e.entry_prob_up,
            "entry_news_impact": e.entry_news_impact,
            "entry_advice": e.entry_advice,
        }

    by_missed = sorted(effects, key=lambda x: x.missed_upside_pct or 0.0, reverse=True)[:limit]
    by_loss = sorted(effects, key=lambda x: x.realized_pct)[:limit]
    return {"top_missed_upside": [row(e) for e in by_missed], "top_losses": [row(e) for e in by_loss]}


def _parse_llm_json_response(text: str) -> Any:
    """
    Парсит JSON из ответа модели: чистый JSON, ```json ... ```, или текст с JSON-объектом внутри.
    """
    if not text or not str(text).strip():
        return {"raw_text": text}
    s = str(text).strip()
    # Блок markdown ```json ... ``` или ``` ... ```
    fence = re.match(r"^```(?:json)?\s*\r?\n?", s, re.IGNORECASE)
    if fence:
        rest = s[fence.end() :]
        end = rest.rfind("```")
        if end != -1:
            s = rest[:end].strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # Первый { ... последний } (если модель добавила пояснения)
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(s[start : end + 1])
        except Exception:
            pass
    return {"raw_text": text}


def _build_llm_recommendations(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        from services.llm_service import get_llm_service

        llm = get_llm_service()
        if not getattr(llm, "client", None):
            return {"status": "disabled", "reason": "LLM client unavailable"}

        system_prompt = (
            "Ты quant-аналитик. По статистике сделок предложи 5-8 конкретных улучшений "
            "для роста прибыльности и снижения убытков. Укажи приоритеты. "
            "Опирайся на метрики, не фантазируй. "
            "Верни только валидный JSON (без markdown, без ```, без пояснений до/после) "
            "с ключами: priorities, threshold_changes, new_features, monitoring_fixes, expected_impact."
        )
        out = llm.generate_response(
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}],
            system_prompt=system_prompt,
            temperature=0.1,
            max_tokens=1600,
        )
        text = out.get("response") or ""
        parsed = _parse_llm_json_response(text)
        return {
            "status": "ok",
            "model": out.get("model"),
            "usage": out.get("usage"),
            "analysis": parsed,
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def analyze_trade_effectiveness(days: int = 7, strategy: str = "GAME_5M", use_llm: bool = False) -> Dict[str, Any]:
    days = max(1, min(30, int(days)))
    closed = _load_closed_trades(days=days, strategy_name=strategy)
    if not closed:
        return {"meta": {"days": days, "strategy": strategy, "trades_analyzed": 0}, "summary": {"total": 0}}

    tickers = [str(t.ticker) for t in closed if getattr(t, "ticker", None)]
    cache = _prepare_ohlc_cache(tickers=tickers, days=days)
    effects = _estimate_trade_effects(closed, cache)
    summary = _aggregate(effects)
    tops = _top_cases(effects)
    payload: Dict[str, Any] = {
        "meta": {"days": days, "strategy": strategy, "trades_analyzed": len(effects)},
        "summary": summary,
        "top_cases": tops,
    }
    if use_llm:
        payload["llm"] = _build_llm_recommendations(payload)
    return payload


def format_trade_effectiveness_text(report: Dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    if summary.get("total", 0) == 0:
        return "За выбранный период закрытых сделок не найдено."
    top = report.get("top_cases") or {}
    lines = [
        "📊 Анализатор эффективности сделок",
        f"Период: {report.get('meta', {}).get('days', '—')} дн. | Стратегия: {report.get('meta', {}).get('strategy', '—')}",
        f"Сделок: {summary['total']} | Win rate: {summary['win_rate_pct']:.2f}% | Net PnL: ${summary['sum_net_pnl_usd']:+.2f}",
        f"Средний результат: {summary['avg_realized_pct']:+.3f}% | Медиана: {summary['median_realized_pct']:+.3f}%",
        f"Упущенный upside: Σ {summary['sum_missed_upside_pct']:+.3f}% | Избежимый loss: Σ {summary['sum_avoidable_loss_pct']:+.3f}%",
        f"Сигналы риска: late polling={summary['late_polling_signals']}, high-vol losses={summary['high_vol_losses_count']}, weak P(up) losses={summary['weak_prob_up_losses_count']}",
        "",
        "Top losses:",
    ]
    for r in (top.get("top_losses") or [])[:4]:
        lines.append(f"• {r['ticker']} #{r['trade_id']}: {r['realized_pct']:+.2f}% (exit={r['exit_signal']})")
    if report.get("llm"):
        llm = report["llm"]
        lines.append("")
        lines.append("LLM:")
        if llm.get("status") == "ok":
            ana = llm.get("analysis")
            if isinstance(ana, dict):
                lines.append(json.dumps(ana, ensure_ascii=False, indent=2)[:1800])
            else:
                lines.append(str(ana)[:1800])
        else:
            lines.append(f"{llm.get('status')}: {llm.get('reason')}")
    return "\n".join(lines)
