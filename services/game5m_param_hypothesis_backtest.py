"""
Офлайн-подбор GAME_5M_* (в первую очередь по **не закрытым** BUY в trade_history).

  1) Открытые позиции: BUY без последующего SELL по тому же тикеру → реплей 5m + сетка
     ``GAME_5M_TAKE_PROFIT_PCT_<TICKER>`` (см. ``run_hanger_tune_for_open_trades``).
  2) Опционально — кандидаты из сохранённого JSON сверки (legacy, ``run_hanger_tune_from_take_json``).
  3) В составе анализатора — недобор по missed_upside + «старое» окно BUY (``run_game5m_hypothesis_bundle``).

Имплементация в прод не выполняется автоматически — только рекомендации в JSON.
"""

from __future__ import annotations

import json
import math
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import partial
from typing import Any, Dict, Iterator, List, Optional, Sequence

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from config_loader import get_config_value, get_database_url
from services.game_5m import _take_profit_cap_pct, trade_ts_to_et
from services.game_5m_take_replay import (
    load_bars_5m_for_replay,
    log_return_pnl,
    momentum_from_5m_slice,
    replay_game5m_on_bars,
)


@contextmanager
def _env_overrides(updates: Dict[str, str]) -> Iterator[None]:
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


def fetch_open_game5m_buys(engine: Engine) -> List[Dict[str, Any]]:
    """
    Все ``GAME_5M`` BUY, после которых в истории **нет** SELL по этому тикеру (позиция ещё открыта).
    """
    q = text(
        """
        SELECT b.id, b.ts, b.ticker, b.price, b.quantity
        FROM trade_history b
        WHERE b.strategy_name = 'GAME_5M' AND b.side = 'BUY'
          AND NOT EXISTS (
            SELECT 1
            FROM trade_history s
            WHERE s.strategy_name = 'GAME_5M'
              AND s.side = 'SELL'
              AND s.ticker = b.ticker
              AND (s.ts > b.ts OR (s.ts = b.ts AND s.id > b.id))
          )
        ORDER BY b.ts ASC, b.id ASC
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q).mappings().all()
    return [dict(r) for r in rows]


def fetch_open_game5m_buys_aggregate_by_ticker(engine: Engine) -> List[Dict[str, Any]]:
    """
    Одна строка на **тикер**: все «висящие» BUY GAME_5M (как в ``fetch_open_game5m_buys``) склеены:
    дата входа для окна — **первая** по времени, цена — **VWAP**, quantity — сумма лотов.
    """
    rows = fetch_open_game5m_buys(engine)
    by_ticker: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        try:
            t = str(r.get("ticker") or "").strip().upper()
            if not t:
                continue
            by_ticker.setdefault(t, []).append(dict(r))
        except (TypeError, ValueError):
            continue
    out: List[Dict[str, Any]] = []
    for t in sorted(by_ticker.keys()):
        lst = sorted(by_ticker[t], key=lambda x: (x.get("ts"), int(x.get("id") or 0)))
        first = lst[0]
        tot_q = 0.0
        tot_c = 0.0
        buy_ids: List[int] = []
        for r in lst:
            try:
                q = float(r.get("quantity") or 1.0)
                p = float(r.get("price") or 0.0)
            except (TypeError, ValueError):
                q, p = 1.0, 0.0
            tot_q += max(1e-9, q)
            tot_c += q * p
            try:
                buy_ids.append(int(r["id"]))
            except (KeyError, TypeError, ValueError):
                continue
        if tot_q <= 0 or not buy_ids:
            continue
        out.append(
            {
                "buy_id": int(first["id"]),
                "ticker": t,
                "entry_ts": first["ts"],
                "entry_price": tot_c / tot_q,
                "quantity": tot_q,
                "aggregate_buy_ids": buy_ids,
            }
        )
    return out


def _fetch_game5m_buys_between(engine: Engine, ts_start: datetime, ts_end: datetime) -> List[Dict[str, Any]]:
    q = text(
        """
        SELECT id, ts, ticker, price, quantity
        FROM trade_history
        WHERE strategy_name = 'GAME_5M' AND side = 'BUY'
          AND ts >= :t0 AND ts < :t1
        ORDER BY ts ASC, id ASC
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q, {"t0": ts_start, "t1": ts_end}).mappings().all()
    return [dict(r) for r in rows]


def _fetch_buy_quantity(engine: Engine, buy_id: int) -> float:
    q = text("SELECT quantity FROM trade_history WHERE id = :id LIMIT 1")
    with engine.connect() as conn:
        row = conn.execute(q, {"id": int(buy_id)}).mappings().first()
    if not row:
        return 1.0
    try:
        return max(1e-9, float(row.get("quantity") or 1.0))
    except (TypeError, ValueError):
        return 1.0


def candidates_from_game5m_take_json(
    path: str | Path,
    *,
    require_both_replays_null: bool = False,
) -> List[Dict[str, Any]]:
    """
    Legacy: строки JSON, где реплей **5m** не нашёл выход (``replay_5m is None``).

    При ``require_both_replays_null=True`` — дополнительно требуется ``replay_30m is None`` (старое поле в файле).
    """
    raw = Path(path).expanduser().read_text(encoding="utf-8")
    data = json.loads(raw)
    rows = data.get("rows") or []
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        r5 = r.get("replay_5m")
        r30 = r.get("replay_30m")
        if require_both_replays_null:
            if r5 is not None or r30 is not None:
                continue
        else:
            if r5 is not None:
                continue
        try:
            bid = int(r["buy_id"])
            sym = str(r.get("ticker") or "").strip().upper()
            ep = float(r.get("entry_price") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if not sym or ep <= 0:
            continue
        out.append(
            {
                "buy_id": bid,
                "ticker": sym,
                "entry_ts": r.get("entry_ts"),
                "entry_price": ep,
            }
        )
    return out


def _hanger_tune_for_buy_candidates(
    *,
    engine: Engine,
    candidates: List[Dict[str, Any]],
    meta: Dict[str, Any],
    exchange: str,
    hanger_calendar_days: int,
    sag_epsilon_log: float,
    skip_sag_check: bool,
    bar_horizon_days_after_entry: int,
    merge_note: str,
) -> Dict[str, Any]:
    n_cand = len(candidates)
    src = str((meta or {}).get("source", "?"))
    print(
        f"hanger tune [{src}]: кандидатов={n_cand}, "
        f"горизонт баров после входа {int(bar_horizon_days_after_entry)} дн. — считаем…",
        flush=True,
    )
    hanger_cases: List[Dict[str, Any]] = []
    merge_hints: List[Dict[str, Any]] = []
    step = max(1, n_cand // 5) if n_cand > 10 else 0
    for i, c in enumerate(candidates, start=1):
        if step and (i % step == 0 or i == n_cand):
            print(f"hanger tune: {i}/{n_cand}", flush=True)
        buy_id = int(c["buy_id"])
        ticker = str(c["ticker"]).strip().upper()
        entry_price = float(c["entry_price"])
        entry_ts = c["entry_ts"]
        try:
            try:
                qty = float(c.get("quantity")) if c.get("quantity") is not None else _fetch_buy_quantity(engine, buy_id)
            except (TypeError, ValueError):
                qty = _fetch_buy_quantity(engine, buy_id)
            entry_et = pd.Timestamp(trade_ts_to_et(entry_ts))
            start_utc = (entry_et.tz_convert("UTC") - pd.Timedelta(days=8)).floor("s")
            end_utc = (
                entry_et.tz_convert("UTC")
                + pd.Timedelta(days=max(int(bar_horizon_days_after_entry), hanger_calendar_days))
            ).ceil("s")
            df5 = load_bars_5m_for_replay(engine, ticker, exchange, start_utc, end_utc)
            diag = diagnose_hanger(
                df5,
                entry_ts_et=entry_et,
                entry_price=entry_price,
                ticker=ticker,
                hanger_calendar_days=hanger_calendar_days,
                sag_epsilon_log=sag_epsilon_log,
                skip_sag_check=skip_sag_check,
            )
            if diag is None:
                hanger_cases.append(
                    {
                        "buy_id": buy_id,
                        "ticker": ticker,
                        "entry_ts": str(entry_ts),
                        "entry_price": entry_price,
                        "skipped": True,
                        "reason": "not_hanger_by_rules_or_insufficient_bars",
                    }
                )
                continue
            notional = _notional_usd(entry_price, qty)
            ag = _hanger_aggression(notional, float(hanger_calendar_days))
            sweep = sweep_hanger_take_cap(
                df5,
                entry_ts_et=entry_et,
                entry_price=entry_price,
                ticker=ticker,
                hanger_calendar_days=hanger_calendar_days,
                aggression=ag,
            )
            row = {
                "buy_id": buy_id,
                "ticker": ticker,
                "entry_ts": str(entry_ts),
                "entry_price": entry_price,
                "notional_usd_approx": round(notional, 2),
                "hanger_aggression": round(ag, 4),
                "diagnosis": diag,
                "remediation_take_cap": sweep,
                "skipped": False,
            }
            agg_ids = c.get("aggregate_buy_ids")
            if isinstance(agg_ids, list) and agg_ids:
                row["aggregate_buy_ids"] = [int(x) for x in agg_ids if x is not None]
            hanger_cases.append(row)
            if sweep:
                ev_ids = row.get("aggregate_buy_ids") or [buy_id]
                merge_hints.append(
                    {
                        "theme": "hanger_take_cap",
                        "env_key": sweep["env_key"],
                        "direction": "decrease_cap",
                        "evidence_buy_ids": ev_ids,
                        "proposed_value": sweep["proposed_cap_pct"],
                        "note": merge_note,
                    }
                )
        except Exception as ex:
            hanger_cases.append(
                {
                    "buy_id": buy_id,
                    "ticker": ticker,
                    "entry_ts": str(entry_ts),
                    "entry_price": entry_price,
                    "skipped": True,
                    "error": True,
                    "error_type": type(ex).__name__,
                    "reason": str(ex)[:500],
                }
            )
    meta_out = {**meta, "hanger_calendar_days": int(hanger_calendar_days), "exchange": exchange}
    meta_out["row_error_count"] = sum(1 for h in hanger_cases if isinstance(h, dict) and h.get("error"))
    return {
        "meta": meta_out,
        "hanger_hypotheses": hanger_cases,
        "underprofit_hypotheses": [],
        "mergeable_recommendations": merge_hints,
    }


def run_hanger_tune_for_open_trades(
    *,
    engine: Engine,
    exchange: str = "US",
    hanger_calendar_days: int = 6,
    sag_epsilon_log: float = 0.0,
    skip_sag_check: bool = True,
    bar_horizon_days_after_entry: int = 10,
) -> Dict[str, Any]:
    """Подбор потолка тейка по **открытым** GAME_5M BUY (нет SELL в trade_history)."""
    print("Загрузка открытых GAME_5M BUY из trade_history…", flush=True)
    rows = fetch_open_game5m_buys(engine)
    print(f"Открытых BUY (строк из БД): {len(rows)}", flush=True)
    candidates: List[Dict[str, Any]] = []
    for r in rows:
        try:
            candidates.append(
                {
                    "buy_id": int(r["id"]),
                    "ticker": str(r["ticker"]).strip().upper(),
                    "entry_ts": r["ts"],
                    "entry_price": float(r["price"]),
                    "quantity": r.get("quantity"),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    meta = {
        "source": "trade_history_open_buys",
        "open_buys_count": len(candidates),
        "sag_epsilon_log": float(sag_epsilon_log),
        "skip_sag_check": bool(skip_sag_check),
        "bar_horizon_days_after_entry": int(bar_horizon_days_after_entry),
        "module": "services.game5m_param_hypothesis_backtest.run_hanger_tune_for_open_trades",
    }
    return _hanger_tune_for_buy_candidates(
        engine=engine,
        candidates=candidates,
        meta=meta,
        exchange=exchange,
        hanger_calendar_days=hanger_calendar_days,
        sag_epsilon_log=sag_epsilon_log,
        skip_sag_check=skip_sag_check,
        bar_horizon_days_after_entry=bar_horizon_days_after_entry,
        merge_note="Открытый BUY в trade_history; подтвердить walk-forward.",
    )


def run_hanger_tune_for_open_trades_aggregate(
    *,
    engine: Engine,
    exchange: str = "US",
    hanger_calendar_days: int = 6,
    sag_epsilon_log: float = 0.0,
    skip_sag_check: bool = True,
    bar_horizon_days_after_entry: int = 10,
) -> Dict[str, Any]:
    """Подбор тейка по **агрегированной** открытой позиции GAME_5M на тикер (первая дата входа + VWAP)."""
    print("Загрузка открытых GAME_5M BUY и агрегация по тикерам…", flush=True)
    candidates = fetch_open_game5m_buys_aggregate_by_ticker(engine)
    print(f"Тикеров с открытой нетто-позицией: {len(candidates)}", flush=True)
    meta = {
        "source": "trade_history_open_buys_aggregate_by_ticker",
        "open_buys_count": sum(len((c.get("aggregate_buy_ids") or [c.get("buy_id")])) for c in candidates),
        "tickers_count": len(candidates),
        "aggregate": True,
        "sag_epsilon_log": float(sag_epsilon_log),
        "skip_sag_check": bool(skip_sag_check),
        "bar_horizon_days_after_entry": int(bar_horizon_days_after_entry),
        "module": "services.game5m_param_hypothesis_backtest.run_hanger_tune_for_open_trades_aggregate",
    }
    return _hanger_tune_for_buy_candidates(
        engine=engine,
        candidates=candidates,
        meta=meta,
        exchange=exchange,
        hanger_calendar_days=hanger_calendar_days,
        sag_epsilon_log=sag_epsilon_log,
        skip_sag_check=skip_sag_check,
        bar_horizon_days_after_entry=bar_horizon_days_after_entry,
        merge_note="Агрегат открытых BUY по тикеру (VWAP, первая дата); walk-forward обязателен.",
    )


def run_hanger_tune_from_take_json(
    *,
    engine: Engine,
    json_path: str | Path,
    exchange: str = "US",
    hanger_calendar_days: int = 6,
    sag_epsilon_log: float = 0.0,
    skip_sag_check: bool = False,
    require_both_replays_null: bool = False,
    bar_horizon_days_after_entry: int = 10,
) -> Dict[str, Any]:
    """Legacy: кандидаты из JSON файла сверки (строки без реплея 5m)."""
    print(f"Чтение кандидатов из JSON: {json_path!s}…", flush=True)
    candidates = candidates_from_game5m_take_json(
        json_path, require_both_replays_null=require_both_replays_null
    )
    meta = {
        "source": "take_json_file",
        "source_json": str(Path(json_path).expanduser().resolve()),
        "candidates_in_json": len(candidates),
        "sag_epsilon_log": float(sag_epsilon_log),
        "skip_sag_check": bool(skip_sag_check),
        "require_both_replays_null": bool(require_both_replays_null),
        "bar_horizon_days_after_entry": int(bar_horizon_days_after_entry),
        "module": "services.game5m_param_hypothesis_backtest.run_hanger_tune_from_take_json",
    }
    return _hanger_tune_for_buy_candidates(
        engine=engine,
        candidates=candidates,
        meta=meta,
        exchange=exchange,
        hanger_calendar_days=hanger_calendar_days,
        sag_epsilon_log=sag_epsilon_log,
        skip_sag_check=skip_sag_check,
        bar_horizon_days_after_entry=bar_horizon_days_after_entry,
        merge_note="Кандидат из JSON (без реплея 5m); подтвердить walk-forward.",
    )


def _slice_df_through_calendar_days(
    df: pd.DataFrame,
    *,
    entry_ts_et: pd.Timestamp,
    calendar_days: float,
) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    end_et = entry_ts_et + pd.Timedelta(days=float(calendar_days))
    d = pd.to_datetime(df["datetime"])
    if d.dt.tz is None:
        d = d.dt.tz_localize("America/New_York", ambiguous=True)
    else:
        d = d.dt.tz_convert("America/New_York")
    m = d <= end_et
    return df.loc[m.values].reset_index(drop=True)


def _replay_until_ts(
    df: pd.DataFrame,
    *,
    entry_ts_et: pd.Timestamp,
    entry_price: float,
    ticker: str,
    until_et: pd.Timestamp,
) -> Any:
    """Реплей только по барам с open <= until_et (грубая привязка к фактическому выходу)."""
    if df is None or df.empty:
        return None
    d = pd.to_datetime(df["datetime"])
    if d.dt.tz is None:
        d = d.dt.tz_localize("America/New_York", ambiguous=True)
    else:
        d = d.dt.tz_convert("America/New_York")
    sub = df.loc[d <= until_et].reset_index(drop=True)
    if sub.empty:
        return None
    return replay_game5m_on_bars(
        sub,
        entry_ts_et=entry_ts_et,
        entry_price=entry_price,
        ticker=ticker,
        bar_minutes=5,
        momentum_fn=partial(momentum_from_5m_slice, ticker=ticker),
    )


def _sagging_market(
    df_window: pd.DataFrame,
    *,
    entry_price: float,
    epsilon_log: float,
) -> bool:
    """Вариант A из docs: log(close_last/entry) ≤ epsilon."""
    if df_window is None or df_window.empty or entry_price <= 0:
        return False
    last = float(df_window.iloc[-1]["Close"])
    if last <= 0:
        return False
    return math.log(last / entry_price) <= float(epsilon_log)


def _take_mult_grid(aggression: float) -> List[float]:
    """Множители к потолку тейка для висяков: чем выше aggression, тем глубже сетка вниз."""
    a = max(0.0, min(2.0, float(aggression)))
    floor_m = max(0.55, 1.0 - 0.45 * min(1.0, a))
    n = max(5, min(14, int(5 + round(9 * min(1.0, a)))))
    return list(np.linspace(1.0, floor_m, n))


def _factor_add_grid(aggression: float) -> List[float]:
    """Приращения к GAME_5M_TAKE_MOMENTUM_FACTOR для недобора."""
    a = max(0.0, min(2.0, float(aggression)))
    n = max(2, min(6, int(2 + round(4 * min(1.0, a)))))
    return list(np.linspace(0.0, min(0.15, 0.05 + 0.05 * a), n))


def _notional_usd(entry_price: float, qty: Any) -> float:
    try:
        q = float(qty)
    except (TypeError, ValueError):
        q = 1.0
    return max(0.0, float(entry_price) * q)


def _hanger_aggression(notional: float, calendar_days: float) -> float:
    ref_n = 25_000.0
    return min(2.0, (notional / ref_n) ** 0.5 * (0.6 + float(calendar_days) / 6.0))


def diagnose_hanger(
    df5: pd.DataFrame,
    *,
    entry_ts_et: pd.Timestamp,
    entry_price: float,
    ticker: str,
    hanger_calendar_days: int,
    sag_epsilon_log: float,
    skip_sag_check: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Возвращает dict с полями классификации или None, если это не висяк по правилам v1.

    ``skip_sag_check=True`` — для кандидатов из JSON (replay null), когда на выходных
    хотят прогнать подбор тейка без фильтра «провисание по close».
    """
    dfw = _slice_df_through_calendar_days(df5, entry_ts_et=entry_ts_et, calendar_days=float(hanger_calendar_days))
    if dfw is None or len(dfw) < 3:
        return None
    ex = replay_game5m_on_bars(
        dfw,
        entry_ts_et=entry_ts_et,
        entry_price=entry_price,
        ticker=ticker,
        bar_minutes=5,
        momentum_fn=partial(momentum_from_5m_slice, ticker=ticker),
    )
    if ex is not None and ex.signal_type == "TAKE_PROFIT":
        return None
    # Ранний TIME_EXIT/STOP — не «висяк на тейке» для этого инструмента
    if ex is not None:
        return None
    sag_ok = skip_sag_check or _sagging_market(dfw, entry_price=entry_price, epsilon_log=sag_epsilon_log)
    if not sag_ok:
        return None
    return {
        "kind": "hanger",
        "ticker": ticker,
        "calendar_days_window": int(hanger_calendar_days),
        "bars_in_window": len(dfw),
        "replay_in_window_signal": None,
        "sag_ok": sag_ok,
        "skip_sag_check": bool(skip_sag_check),
        "log_close_over_entry_end": round(math.log(float(dfw.iloc[-1]["Close"]) / entry_price), 6)
        if entry_price > 0
        else None,
    }


def live_aggregate_hanger_diagnosis(
    *,
    engine: Engine,
    ticker: str,
    open_position: Dict[str, Any],
    exchange: str = "US",
    hanger_calendar_days: int = 6,
    sag_epsilon_log: float = 0.0,
    skip_sag_check: bool = False,
    bar_horizon_days_after_entry: int = 10,
) -> Optional[Dict[str, Any]]:
    """
    Live: одна нетто-позиция GAME_5M (``entry_price`` в ``open_position`` — обычно VWAP) и те же правила,
    что ``diagnose_hanger``: окно **hanger_calendar_days** календарных дней от **самого раннего** открытого BUY.
    """
    ticker_u = (ticker or "").strip().upper()
    if not ticker_u:
        return None
    rows = fetch_open_game5m_buys(engine)
    first_ts = None
    for r in rows:
        if str(r.get("ticker") or "").strip().upper() != ticker_u:
            continue
        ts = r.get("ts")
        if ts is None:
            continue
        if first_ts is None or ts < first_ts:
            first_ts = ts
    if first_ts is None:
        return None
    try:
        entry_price = float(open_position.get("entry_price") or 0.0)
    except (TypeError, ValueError):
        return None
    if entry_price <= 0:
        return None
    entry_et = pd.Timestamp(trade_ts_to_et(first_ts))
    start_utc = (entry_et.tz_convert("UTC") - pd.Timedelta(days=8)).floor("s")
    end_utc = (
        entry_et.tz_convert("UTC")
        + pd.Timedelta(days=max(int(bar_horizon_days_after_entry), int(hanger_calendar_days)))
    ).ceil("s")
    df5 = load_bars_5m_for_replay(engine, ticker_u, exchange, start_utc, end_utc)
    return diagnose_hanger(
        df5,
        entry_ts_et=entry_et,
        entry_price=entry_price,
        ticker=ticker_u,
        hanger_calendar_days=hanger_calendar_days,
        sag_epsilon_log=sag_epsilon_log,
        skip_sag_check=skip_sag_check,
    )


def sweep_hanger_take_cap(
    df5: pd.DataFrame,
    *,
    entry_ts_et: pd.Timestamp,
    entry_price: float,
    ticker: str,
    hanger_calendar_days: int,
    aggression: float,
) -> Optional[Dict[str, Any]]:
    """Подбор снижения потолка тейка (per-ticker env), чтобы в окне появился TAKE_PROFIT."""
    base_cap = _take_profit_cap_pct(ticker)
    key = f"GAME_5M_TAKE_PROFIT_PCT_{ticker.upper()}"
    best: Optional[Dict[str, Any]] = None
    for mult in _take_mult_grid(aggression):
        new_cap = max(1.5, round(base_cap * float(mult), 3))
        if new_cap >= base_cap - 1e-9 and mult < 0.999:
            continue
        with _env_overrides({key: str(new_cap)}):
            dfw = _slice_df_through_calendar_days(
                df5, entry_ts_et=entry_ts_et, calendar_days=float(hanger_calendar_days)
            )
            ex = replay_game5m_on_bars(
                dfw,
                entry_ts_et=entry_ts_et,
                entry_price=entry_price,
                ticker=ticker,
                bar_minutes=5,
                momentum_fn=partial(momentum_from_5m_slice, ticker=ticker),
            )
        if ex is not None and ex.signal_type == "TAKE_PROFIT":
            lr = log_return_pnl(entry_price, ex.exit_fill_price)
            cand = {
                "env_key": key,
                "baseline_cap_pct": base_cap,
                "proposed_cap_pct": new_cap,
                "cap_multiplier": round(mult, 4),
                "exit_bar_end_et": str(ex.bar_end_et),
                "replay_log_ret": None if lr is None else round(float(lr), 6),
            }
            if best is None or (cand["proposed_cap_pct"] > best["proposed_cap_pct"]):
                best = cand
    return best


def sweep_underprofit_momentum_factor(
    df5: pd.DataFrame,
    *,
    entry_ts_et: pd.Timestamp,
    entry_price: float,
    ticker: str,
    exit_ts_et: pd.Timestamp,
    actual_log_ret: float,
    aggression: float,
) -> Optional[Dict[str, Any]]:
    """Увеличение GAME_5M_TAKE_MOMENTUM_FACTOR до потолка 2.0 в game_5m — ищем лучший log_ret реплея до выхода."""
    raw = (get_config_value("GAME_5M_TAKE_MOMENTUM_FACTOR", "1.0") or "1.0").strip().replace(",", ".")
    try:
        base_f = float(raw)
    except (ValueError, TypeError):
        base_f = 1.0
    best: Optional[Dict[str, Any]] = None
    for add in _factor_add_grid(aggression):
        new_f = round(min(2.0, base_f + float(add)), 4)
        if new_f <= base_f + 1e-9:
            continue
        with _env_overrides({"GAME_5M_TAKE_MOMENTUM_FACTOR": str(new_f)}):
            ex = _replay_until_ts(
                df5,
                entry_ts_et=entry_ts_et,
                entry_price=entry_price,
                ticker=ticker,
                until_et=exit_ts_et,
            )
        if ex is None:
            continue
        lr = log_return_pnl(entry_price, ex.exit_fill_price)
        if lr is None:
            continue
        if lr <= actual_log_ret + 1e-6:
            continue
        cand = {
            "env_key": "GAME_5M_TAKE_MOMENTUM_FACTOR",
            "baseline": base_f,
            "proposed": new_f,
            "replay_log_ret_until_exit": round(float(lr), 6),
            "actual_log_ret": round(float(actual_log_ret), 6),
            "delta_log_ret": round(float(lr) - float(actual_log_ret), 6),
            "replay_exit_signal": ex.signal_type,
            "replay_exit_bar_end_et": str(ex.bar_end_et),
        }
        if best is None or cand["delta_log_ret"] > best["delta_log_ret"]:
            best = cand
    return best


def run_game5m_hypothesis_bundle(
    *,
    engine: Optional[Engine] = None,
    buy_window_end_offset_days: int = 7,
    buy_window_width_days: int = 7,
    hanger_calendar_days: int = 6,
    sag_epsilon_log: float = 0.0,
    exchange: str = "US",
    underprofit_min_missed_pct: float = 0.8,
    effects: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """
    Собирает гипотезы по BUY в «старом» окне (по умолчанию от 14 до 7 дней назад) и по закрытым сделкам
    из ``effects`` (missed upside), если передан список объектов с нужными атрибутами.
    """
    now = datetime.now(timezone.utc)
    end = now - timedelta(days=max(0, int(buy_window_end_offset_days)))
    start = end - timedelta(days=max(1, int(buy_window_width_days)))

    eng = engine or create_engine(get_database_url())
    buys = _fetch_game5m_buys_between(eng, start, end)

    hanger_cases: List[Dict[str, Any]] = []
    underprofit_cases: List[Dict[str, Any]] = []
    merge_hints: List[Dict[str, Any]] = []

    for b in buys:
        buy_id = int(b["id"])
        ticker = str(b["ticker"]).strip().upper()
        entry_price = float(b["price"])
        entry_ts = b["ts"]
        qty = b.get("quantity", 1.0)
        entry_et = pd.Timestamp(trade_ts_to_et(entry_ts))
        start_utc = (entry_et.tz_convert("UTC") - pd.Timedelta(days=8)).floor("s")
        end_utc = (entry_et.tz_convert("UTC") + pd.Timedelta(days=max(hanger_calendar_days, 8))).ceil("s")
        df5 = load_bars_5m_for_replay(eng, ticker, exchange, start_utc, end_utc)
        diag = diagnose_hanger(
            df5,
            entry_ts_et=entry_et,
            entry_price=entry_price,
            ticker=ticker,
            hanger_calendar_days=hanger_calendar_days,
            sag_epsilon_log=sag_epsilon_log,
        )
        if diag is None:
            continue
        notional = _notional_usd(entry_price, qty)
        ag = _hanger_aggression(notional, float(hanger_calendar_days))
        sweep = sweep_hanger_take_cap(
            df5,
            entry_ts_et=entry_et,
            entry_price=entry_price,
            ticker=ticker,
            hanger_calendar_days=hanger_calendar_days,
            aggression=ag,
        )
        row = {
            "buy_id": buy_id,
            "ticker": ticker,
            "entry_ts": str(entry_ts),
            "entry_price": entry_price,
            "notional_usd_approx": round(notional, 2),
            "hanger_aggression": round(ag, 4),
            "diagnosis": diag,
            "remediation_take_cap": sweep,
        }
        hanger_cases.append(row)
        if sweep:
            merge_hints.append(
                {
                    "theme": "hanger_take_cap",
                    "env_key": sweep["env_key"],
                    "direction": "decrease_cap",
                    "evidence_buy_ids": [buy_id],
                    "proposed_value": sweep["proposed_cap_pct"],
                    "note": "Снижение потолка тейка на тикер — только если подтверждено на другой неделе walk-forward.",
                }
            )

    if effects:
        for e in effects:
            missed = getattr(e, "missed_upside_pct", None)
            if missed is None or float(missed) < float(underprofit_min_missed_pct):
                continue
            ticker = str(getattr(e, "ticker", "") or "").strip().upper()
            if not ticker:
                continue
            entry_ts = getattr(e, "entry_ts", None)
            exit_ts = getattr(e, "exit_ts", None)
            entry_price = float(getattr(e, "entry_price", 0) or 0)
            if entry_ts is None or exit_ts is None or entry_price <= 0:
                continue
            et = pd.Timestamp(entry_ts)
            xt = pd.Timestamp(exit_ts)
            if et.tzinfo is None:
                et = et.tz_localize("America/New_York", ambiguous=True)
            else:
                et = et.tz_convert("America/New_York")
            if xt.tzinfo is None:
                xt = xt.tz_localize("America/New_York", ambiguous=True)
            else:
                xt = xt.tz_convert("America/New_York")
            win_lo = pd.Timestamp(start).tz_convert("America/New_York")
            win_hi = pd.Timestamp(end).tz_convert("America/New_York")
            if not (win_lo <= et < win_hi):
                continue
            rl = getattr(e, "realized_log_return", None)
            if rl is None:
                ep = float(getattr(e, "entry_price", 0) or 0)
                xp = float(getattr(e, "exit_price", 0) or 0)
                if ep <= 0 or xp <= 0:
                    continue
                rl = float(np.log(xp / ep))
            notional = _notional_usd(entry_price, getattr(e, "qty", 1.0))
            ag = _hanger_aggression(notional, 5.0)
            start_utc = (et.tz_convert("UTC") - pd.Timedelta(days=5)).floor("s")
            end_utc = (xt.tz_convert("UTC") + pd.Timedelta(days=2)).ceil("s")
            df5 = load_bars_5m_for_replay(eng, ticker, exchange, start_utc, end_utc)
            sw = sweep_underprofit_momentum_factor(
                df5,
                entry_ts_et=et,
                entry_price=entry_price,
                ticker=ticker,
                exit_ts_et=xt,
                actual_log_ret=float(rl),
                aggression=ag,
            )
            underprofit_cases.append(
                {
                    "trade_id": int(getattr(e, "trade_id", 0) or 0),
                    "ticker": ticker,
                    "missed_upside_pct": round(float(missed), 4),
                    "remediation_momentum_factor": sw,
                }
            )
            if sw:
                merge_hints.append(
                    {
                        "theme": "underprofit_momentum",
                        "env_key": sw["env_key"],
                        "direction": "increase_factor",
                        "evidence_trade_ids": [int(getattr(e, "trade_id", 0) or 0)],
                        "proposed_value": sw["proposed"],
                        "note": "Повышение factor — проверить, что не режет слишком много ранних тейков на walk-forward.",
                    }
                )

    return {
        "meta": {
            "buy_window_utc": {"start": start.isoformat(), "end": end.isoformat()},
            "hanger_calendar_days": int(hanger_calendar_days),
            "sag_epsilon_log": float(sag_epsilon_log),
            "underprofit_min_missed_pct": float(underprofit_min_missed_pct),
            "exchange": exchange,
            "module": "services.game5m_param_hypothesis_backtest",
        },
        "hanger_hypotheses": hanger_cases,
        "underprofit_hypotheses": underprofit_cases,
        "mergeable_recommendations": merge_hints,
    }


__all__ = [
    "run_game5m_hypothesis_bundle",
    "fetch_open_game5m_buys",
    "fetch_open_game5m_buys_aggregate_by_ticker",
    "run_hanger_tune_for_open_trades",
    "run_hanger_tune_for_open_trades_aggregate",
    "run_hanger_tune_from_take_json",
    "candidates_from_game5m_take_json",
    "diagnose_hanger",
    "live_aggregate_hanger_diagnosis",
    "sweep_hanger_take_cap",
    "sweep_underprofit_momentum_factor",
    "_env_overrides",
]
