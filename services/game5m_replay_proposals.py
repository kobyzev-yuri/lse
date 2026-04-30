"""Offline replay proposal builder for GAME_5M tuning.

The builder tests a bounded grid of high-impact exit parameters against recent
closed trades. It does not change config.env; candidate values are applied only
through temporary process environment overrides during replay.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

import numpy as np
import pandas as pd
from sqlalchemy.engine import Engine

from config_loader import get_config_value
from report_generator import compute_closed_trade_pnls, load_trade_history
from services.game_5m import trade_ts_to_et
from services.game_5m_take_replay import (
    load_bars_5m_for_replay,
    log_return_pnl,
    momentum_2h_pct_from_closes,
    replay_game5m_on_bars,
)
from services.game5m_tuning_policy import coerce_float, validate_game5m_update


GAME_5M = "GAME_5M"
FALSE_TAKE_TOLERANCE_PCT = 0.05
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplayTrade:
    trade_id: int
    ticker: str
    entry_ts: Any
    exit_ts: Any
    entry_price: float
    exit_price: float
    actual_log_ret: float


@contextmanager
def env_overrides(updates: Dict[str, str]) -> Iterator[None]:
    saved: Dict[str, Optional[str]] = {k: os.environ.get(k) for k in updates}
    try:
        for k, v in updates.items():
            os.environ[k] = str(v)
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _target_take_pct_from_exit_context(ctx: dict[str, Any]) -> Optional[float]:
    text = str(ctx.get("exit_condition") or "")
    match = re.search(r"цель\s*~\s*([0-9]+(?:\.[0-9]+)?)%", text)
    if match:
        return coerce_float(match.group(1))
    return None


def is_false_take_profit_by_session_high(trade_pnl: Any) -> bool:
    ctx = _json_dict(getattr(trade_pnl, "exit_context_json", None))
    if not ctx.get("bar_high_session_lifted"):
        return False
    exit_signal = str(ctx.get("exit_signal") or getattr(trade_pnl, "signal_type", "") or "").upper()
    if exit_signal not in ("TAKE_PROFIT", "TAKE_PROFIT_SUSPEND"):
        return False
    target_pct = _target_take_pct_from_exit_context(ctx)
    entry_price = coerce_float(getattr(trade_pnl, "entry_price", None))
    recent_high = coerce_float(ctx.get("bar_high_recent_max") or ctx.get("recent_bars_high_max"))
    if target_pct is None or entry_price is None or entry_price <= 0 or recent_high is None or recent_high <= 0:
        return False
    recent_high_pct = (recent_high / entry_price - 1.0) * 100.0
    return recent_high_pct < (target_pct - FALSE_TAKE_TOLERANCE_PCT)


def _round_candidate(value: float) -> str:
    rounded = round(float(value), 4)
    if abs(rounded - round(rounded)) < 1e-9:
        return str(int(round(rounded)))
    return f"{rounded:.4f}".rstrip("0").rstrip(".")


def _candidate_values(current: float, deltas: Iterable[float], *, min_value: float, max_value: float) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for d in deltas:
        val = max(min_value, min(max_value, current + float(d)))
        s = _round_candidate(val)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _proposal_id(env_key: str, proposed: str) -> str:
    raw = f"{env_key}={proposed}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _recent_replay_trades(
    engine: Engine,
    *,
    days: int,
    max_trades: int,
    include_false_takes: bool,
) -> tuple[List[ReplayTrade], Dict[str, Any]]:
    raw = load_trade_history(engine, strategy_name=GAME_5M)
    closed = compute_closed_trade_pnls(raw)
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=max(1, int(days)))
    trades: List[ReplayTrade] = []
    skipped_false_take = 0
    skipped_no_context = 0
    for t in closed:
        if (getattr(t, "entry_strategy", "") or "").strip().upper() != GAME_5M:
            continue
        if not getattr(t, "entry_ts", None):
            continue
        ts = pd.Timestamp(getattr(t, "ts", None))
        if ts.tzinfo is None:
            ts = ts.tz_localize("Europe/Moscow", ambiguous=True).tz_convert("UTC")
        else:
            ts = ts.tz_convert("UTC")
        if ts < cutoff:
            continue
        if not include_false_takes and is_false_take_profit_by_session_high(t):
            skipped_false_take += 1
            continue
        if not getattr(t, "context_json", None):
            skipped_no_context += 1
        entry_price = float(getattr(t, "entry_price", 0) or 0)
        exit_price = float(getattr(t, "exit_price", 0) or 0)
        if entry_price <= 0 or exit_price <= 0:
            continue
        trades.append(
            ReplayTrade(
                trade_id=int(getattr(t, "trade_id", 0) or 0),
                ticker=str(getattr(t, "ticker", "") or "").strip().upper(),
                entry_ts=getattr(t, "entry_ts"),
                exit_ts=getattr(t, "ts"),
                entry_price=entry_price,
                exit_price=exit_price,
                actual_log_ret=float(getattr(t, "log_return", 0.0) or 0.0),
            )
        )
    trades = sorted(trades, key=lambda x: pd.Timestamp(x.exit_ts))[-max(1, int(max_trades)) :]
    return trades, {
        "closed_trades_selected": len(trades),
        "skipped_false_take_profit_by_session_high": skipped_false_take,
        "skipped_missing_context_json": skipped_no_context,
    }


def _build_candidate_updates(trades: Sequence[ReplayTrade]) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(key: str, values: Iterable[str]) -> None:
        for val in values:
            vr = validate_game5m_update(key, val)
            if not vr.ok:
                continue
            tup = (key, vr.proposed)
            if tup not in seen:
                seen.add(tup)
                candidates.append({"env_key": key, "proposed": vr.proposed})

    base_take = coerce_float(get_config_value("GAME_5M_TAKE_PROFIT_PCT", "5.0")) or 5.0
    add("GAME_5M_TAKE_PROFIT_PCT", _candidate_values(base_take, (-1.0, -0.5, 0.5, 1.0), min_value=1.5, max_value=8.0))

    base_factor = coerce_float(get_config_value("GAME_5M_TAKE_MOMENTUM_FACTOR", "1.0")) or 1.0
    add("GAME_5M_TAKE_MOMENTUM_FACTOR", _candidate_values(base_factor, (-0.1, 0.1), min_value=0.3, max_value=2.0))

    base_min = coerce_float(get_config_value("GAME_5M_TAKE_PROFIT_MIN_PCT", "2.0")) or 2.0
    add("GAME_5M_TAKE_PROFIT_MIN_PCT", _candidate_values(base_min, (-1.0, -0.5, 0.5, 1.0), min_value=0.0, max_value=8.0))

    base_days = coerce_float(get_config_value("GAME_5M_MAX_POSITION_DAYS", "2")) or 2.0
    add("GAME_5M_MAX_POSITION_DAYS", _candidate_values(base_days, (-1.0, 1.0), min_value=1.0, max_value=7.0))

    tickers = sorted({t.ticker for t in trades if t.ticker})
    for ticker in tickers:
        key = f"GAME_5M_TAKE_PROFIT_PCT_{ticker}"
        current = coerce_float(get_config_value(key, "")) or base_take
        add(key, _candidate_values(current, (-1.0, -0.5, 0.5, 1.0), min_value=1.5, max_value=8.0))
    return candidates


def _load_trade_bars(
    engine: Engine,
    trade: ReplayTrade,
    *,
    exchange: str,
    horizon_tail_days: int,
) -> pd.DataFrame:
    entry_et = pd.Timestamp(trade_ts_to_et(trade.entry_ts))
    exit_et = pd.Timestamp(trade_ts_to_et(trade.exit_ts))
    start_utc = (entry_et.tz_convert("UTC") - pd.Timedelta(days=8)).floor("s")
    end_utc = (exit_et.tz_convert("UTC") + pd.Timedelta(days=max(0, int(horizon_tail_days)))).ceil("s")
    return load_bars_5m_for_replay(engine, trade.ticker, exchange, start_utc, end_utc)


def _replay_one_trade_on_bars(trade: ReplayTrade, df5: pd.DataFrame) -> Optional[float]:
    if df5 is None or df5.empty:
        return None
    entry_et = pd.Timestamp(trade_ts_to_et(trade.entry_ts))
    ex = replay_game5m_on_bars(
        df5,
        entry_ts_et=entry_et,
        entry_price=trade.entry_price,
        ticker=trade.ticker,
        bar_minutes=5,
        momentum_fn=lambda slice_df: momentum_2h_pct_from_closes(slice_df["Close"], 24),
    )
    if ex is None:
        return None
    return log_return_pnl(trade.entry_price, ex.exit_fill_price)


def build_game5m_replay_proposals(
    engine: Engine,
    *,
    days: int = 30,
    max_trades: int = 120,
    exchange: str = "US",
    horizon_tail_days: int = 1,
    include_false_takes: bool = False,
    top_n: int = 12,
) -> Dict[str, Any]:
    trades, selection_meta = _recent_replay_trades(
        engine,
        days=days,
        max_trades=max_trades,
        include_false_takes=include_false_takes,
    )
    candidates = _build_candidate_updates(trades)
    bars_by_trade_id: Dict[int, pd.DataFrame] = {}
    for trade in trades:
        try:
            bars_by_trade_id[trade.trade_id] = _load_trade_bars(
                engine,
                trade,
                exchange=exchange,
                horizon_tail_days=horizon_tail_days,
            )
        except Exception as e:
            logger.debug("load bars failed trade_id=%s %s: %s", trade.trade_id, trade.ticker, e)
            bars_by_trade_id[trade.trade_id] = pd.DataFrame()

    proposals: List[Dict[str, Any]] = []
    for cand in candidates:
        key = cand["env_key"]
        proposed = cand["proposed"]
        deltas: List[float] = []
        evidence: List[Dict[str, Any]] = []
        replayed = 0
        no_exit = 0
        with env_overrides({key: proposed}):
            for trade in trades:
                lr = _replay_one_trade_on_bars(trade, bars_by_trade_id.get(trade.trade_id, pd.DataFrame()))
                if lr is None:
                    no_exit += 1
                    continue
                replayed += 1
                delta = float(lr) - float(trade.actual_log_ret)
                deltas.append(delta)
                if abs(delta) >= 0.001:
                    evidence.append(
                        {
                            "trade_id": trade.trade_id,
                            "ticker": trade.ticker,
                            "actual_log_ret": round(float(trade.actual_log_ret), 6),
                            "replay_log_ret": round(float(lr), 6),
                            "delta_log_ret": round(delta, 6),
                        }
                    )
        if not deltas:
            continue
        total_delta = float(np.sum(deltas))
        mean_delta = float(np.mean(deltas))
        improved = sum(1 for d in deltas if d > 0.0005)
        worsened = sum(1 for d in deltas if d < -0.0005)
        sample_penalty = 0.002 * max(0, 8 - replayed)
        score = total_delta - sample_penalty - max(0, worsened - improved) * 0.001
        proposals.append(
            {
                "proposal_id": _proposal_id(key, proposed),
                "env_key": key,
                "proposed": proposed,
                "current": get_config_value(key, ""),
                "score": round(score, 6),
                "metrics": {
                    "replayed_trades": replayed,
                    "no_exit_trades": no_exit,
                    "total_delta_log_ret": round(total_delta, 6),
                    "mean_delta_log_ret": round(mean_delta, 6),
                    "improved_count": improved,
                    "worsened_count": worsened,
                    "unchanged_count": max(0, replayed - improved - worsened),
                },
                "evidence": sorted(evidence, key=lambda x: x["delta_log_ret"], reverse=True)[:8],
            }
        )
    proposals = sorted(
        proposals,
        key=lambda p: (
            float(p["score"]),
            int(p["metrics"]["improved_count"]) - int(p["metrics"]["worsened_count"]),
            int(p["metrics"]["replayed_trades"]),
        ),
        reverse=True,
    )
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "game5m_replay_proposals",
        "params": {
            "days": int(days),
            "max_trades": int(max_trades),
            "exchange": exchange,
            "horizon_tail_days": int(horizon_tail_days),
            "include_false_takes": bool(include_false_takes),
        },
        "selection": selection_meta,
        "candidate_count": len(candidates),
        "proposal_count": len(proposals),
        "proposals": proposals[: max(1, int(top_n))],
    }
