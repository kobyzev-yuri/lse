"""
Игра по 5m сигналам: симуляция входов/выходов для любой быстрой бумаги.

Использует trade_history с strategy_name='GAME_5M' и ticker (SNDK, NDK, LITE, NBIS и т.д.).
Тикер передаётся явно, без привязки к одному инструменту.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import create_engine, text

from config_loader import get_database_url, get_config_value

logger = logging.getLogger(__name__)

# Исторические подсказки suggested_5m_params отключены (параметры только из config.env).
_SUGGESTED_5M_CACHE: Optional[dict] = None
_SUGGESTED_5M_MAX_AGE_HOURS = 25
_SUGGESTED_5M_DEPRECATION_LOGGED = False

# Таймзона, в которой хранятся ts в trade_history (если naive — считаем Москвой)
TRADE_HISTORY_TZ = "Europe/Moscow"
CHART_DISPLAY_TZ = "America/New_York"


def trade_ts_to_et(ts: Any, source_tz: Optional[str] = None) -> Any:
    """
    Переводит метку времени сделки в ET для отрисовки на графике.
    source_tz — таймзона, в которой хранится ts (например 'Europe/Moscow'); если None, используется TRADE_HISTORY_TZ.
    Возвращает timezone-aware pd.Timestamp в America/New_York или исходный ts при ошибке.
    """
    if ts is None:
        return None
    tz_name = source_tz or TRADE_HISTORY_TZ
    try:
        import pandas as pd
        t = pd.Timestamp(ts)
        if t.tzinfo is None:
            # ambiguous: True = DST, False = non-DST; "infer" не везде поддерживается
            t = t.tz_localize(tz_name, ambiguous=True)
        else:
            t = t.tz_convert(tz_name)
        return t.tz_convert(CHART_DISPLAY_TZ)
    except Exception:
        return ts

GAME_5M_STRATEGY = "GAME_5M"
GAME_NOTIONAL_USD = 10_000.0
COMMISSION_RATE = 0.0  # 0% — оплаты брокеру нет


def trade_plot_time_naive_et(trade_row: dict[str, Any]) -> Any:
    """
    Время сделки для оси X на графике (matplotlib): naive datetime в «стене» ET,
    как колонка _dt_plot у 5m df. Приоритет chart_ts (бар из context_json), иначе ts записи.
    """
    raw = trade_row.get("chart_ts") or trade_row.get("ts")
    if raw is None:
        return None
    try:
        stored_tz = "America/New_York" if trade_row.get("chart_ts") else (trade_row.get("ts_timezone") or TRADE_HISTORY_TZ)
        ts_et = trade_ts_to_et(raw, source_tz=stored_tz)
        if ts_et is None:
            return None
        dt = ts_et.to_pydatetime() if hasattr(ts_et, "to_pydatetime") else ts_et
        if getattr(dt, "tzinfo", None):
            return dt.replace(tzinfo=None)
        return dt
    except Exception:
        logger.debug("trade_plot_time_naive_et: skip %r", raw, exc_info=True)
        return None


def _trade_time_in_5m_bar(
    t_trade: Any,
    t_open: Any,
    *,
    bar_minutes: int = 5,
) -> bool:
    """
    True, если t_trade относится к 5m-бару [t_open, t_end): обычный случай,
    либо t_trade == t_end (метка «закрытия» бара — иначе полуинтервал уводит на следующий бар +1).
    """
    import pandas as pd

    t_end = t_open + pd.Timedelta(minutes=int(bar_minutes))
    if t_open <= t_trade < t_end:
        return True
    try:
        return bool(abs((t_trade - t_end).total_seconds()) < 1e-6)
    except Exception:
        return bool(t_trade == t_end)


def match_trade_to_chart_bar_index(
    bar_times_iso: list[str],
    trade_time_iso: Optional[str],
    *,
    bar_minutes: int = 5,
) -> Optional[int]:
    """
    Индекс 5m-бара в ряду ``times`` графика (тот же df, что OHLC), к которому относится момент сделки.
    Правило (ET): ``bar_open <= t_trade < bar_open + bar_minutes``, плюс ``t_trade == bar_close``
    (правая граница включена в этот бар, чтобы не сдвигать маркер на следующий бар).
    """
    if not bar_times_iso or not trade_time_iso:
        return None
    try:
        import pandas as pd

        t_trade = pd.Timestamp(str(trade_time_iso).strip())
        if t_trade.tzinfo is None:
            t_trade = t_trade.tz_localize(CHART_DISPLAY_TZ, ambiguous=True)
        else:
            t_trade = t_trade.tz_convert(CHART_DISPLAY_TZ)
        for i, tstr in enumerate(bar_times_iso):
            try:
                t_open = pd.Timestamp(str(tstr).strip())
                if t_open.tzinfo is None:
                    t_open = t_open.tz_localize(CHART_DISPLAY_TZ, ambiguous=True)
                else:
                    t_open = t_open.tz_convert(CHART_DISPLAY_TZ)
            except Exception:
                continue
            if _trade_time_in_5m_bar(t_trade, t_open, bar_minutes=int(bar_minutes)):
                return i
    except Exception:
        logger.debug("match_trade_to_chart_bar_index: iteration failed", exc_info=True)
    return None


def refine_bar_index_for_trade_price(
    bar_index_time: Optional[int],
    price: float,
    lows: list,
    highs: list,
    bar_times_iso: list[str],
    trade_time_iso: Optional[str],
    *,
    max_scan: int = 120,
    rel_tol: float = 0.002,
) -> Optional[int]:
    """
    Если ``price`` не лежит в [Low, High] бара ``bar_index_time`` (другой ряд котировок vs время в context),
    ищем среди соседних ±max_scan баров тот, чей диапазон (с допуском) содержит цену,
    с минимальным |t_trade − t_open(bar)|.
    """
    if bar_index_time is None or price is None or price <= 0:
        return bar_index_time
    try:
        n = min(len(lows), len(highs), len(bar_times_iso))
        if n <= 0 or bar_index_time < 0 or bar_index_time >= n:
            return bar_index_time
        tol = max(rel_tol * float(price), 0.5)

        def _contains(idx: int) -> bool:
            if idx < 0 or idx >= n:
                return False
            lo = float(lows[idx])
            hi = float(highs[idx])
            return (lo - tol) <= float(price) <= (hi + tol)

        if _contains(bar_index_time):
            return bar_index_time

        import pandas as pd

        t_trade = None
        if trade_time_iso:
            try:
                t_trade = pd.Timestamp(str(trade_time_iso).strip())
                if t_trade.tzinfo is None:
                    t_trade = t_trade.tz_localize(CHART_DISPLAY_TZ, ambiguous=True)
                else:
                    t_trade = t_trade.tz_convert(CHART_DISPLAY_TZ)
            except Exception:
                t_trade = None

        candidates: list[tuple[int, int, float, int]] = []
        # (i, time_ok_sort, score_sec, abs_index_dist) — сначала бар, в чей 5m-интервал попадает t_trade, потом ближе к индексу по времени
        lo_scan = max(0, int(bar_index_time) - int(max_scan))
        hi_scan = min(n - 1, int(bar_index_time) + int(max_scan))
        for i in range(lo_scan, hi_scan + 1):
            if not _contains(i):
                continue
            time_ok = 0
            score = float(abs(i - bar_index_time) * 300)
            if t_trade is not None:
                try:
                    t_open = pd.Timestamp(str(bar_times_iso[i]).strip())
                    if t_open.tzinfo is None:
                        t_open = t_open.tz_localize(CHART_DISPLAY_TZ, ambiguous=True)
                    else:
                        t_open = t_open.tz_convert(CHART_DISPLAY_TZ)
                    if _trade_time_in_5m_bar(t_trade, t_open, bar_minutes=5):
                        time_ok = 0
                    else:
                        time_ok = 1
                    score = abs((t_trade - t_open).total_seconds())
                except Exception:
                    time_ok = 1
                    score = float(abs(i - bar_index_time) * 300)
            idx_dist = abs(i - int(bar_index_time))
            candidates.append((i, time_ok, score, idx_dist))
        if not candidates:
            return bar_index_time
        candidates.sort(key=lambda x: (x[1], x[3], x[2]))
        return int(candidates[0][0])
    except Exception:
        logger.debug("refine_bar_index_for_trade_price: skip", exc_info=True)
        return bar_index_time


def chart_ts_iso_from_context(context_json: Any) -> Optional[str]:
    """
    Время 5m-бара для привязки маркера на графике (не момент INSERT в БД).
    Берётся из context_json сделки: exit_bar_close_ts / exit_bar_start_et (как в get_decision_5m / merge_close_context).
    Возвращает ISO в America/New_York для фронта.
    """
    import json as _json

    if context_json is None:
        return None
    if isinstance(context_json, str):
        try:
            ctx = _json.loads(context_json)
        except Exception:
            return None
    elif isinstance(context_json, dict):
        ctx = context_json
    else:
        return None
    raw = ctx.get("exit_bar_close_ts") or ctx.get("exit_bar_start_et")
    if not raw:
        return None
    try:
        ts_et = trade_ts_to_et(raw, source_tz="America/New_York")
        if ts_et is not None and hasattr(ts_et, "isoformat"):
            return str(ts_et.isoformat())
    except Exception:
        logger.debug("chart_ts_iso_from_context: skip %r", raw, exc_info=True)
    return None


def parse_game5m_bar_ts_for_db(trade_ts: Optional[Any]) -> Optional[datetime]:
    """
    Метка бара из get_decision_5m (exit_bar_close_ts и т.п., обычно ISO с America/New_York)
    → naive datetime для колонки trade_history.ts (как при записи по Москве: ts_timezone=Europe/Moscow).
    """
    if trade_ts is None:
        return None
    try:
        import pandas as pd

        t = pd.Timestamp(trade_ts)
        if t.tzinfo is None:
            t = t.tz_localize("America/New_York", ambiguous=True)
        t_msk = t.tz_convert(TRADE_HISTORY_TZ)
        return t_msk.to_pydatetime().replace(tzinfo=None)
    except Exception:
        logger.debug("parse_game5m_bar_ts_for_db: не разобрали %r", trade_ts, exc_info=True)
        return None


def _get_suggested_5m_params() -> Optional[dict]:
    """
    Подсказки из local/suggested_5m_params.json (legacy-режим).
    Используются только при USE_SUGGESTED_5M_PARAMS=true и если файл обновлён не более 25 ч назад.
    Возвращает {"take_pct": {ticker: float}, "max_days": {ticker: int}} или None.
    """
    # Автоподмена тейка/дней из daily_5m_params отключена: игра использует только явные значения из config.env.
    # Это защищает от неявного дрейфа параметров между запусками и серверными кронами.
    global _SUGGESTED_5M_DEPRECATION_LOGGED
    raw = (get_config_value("USE_SUGGESTED_5M_PARAMS", "") or "").strip().lower()
    if raw in ("1", "true", "yes") and not _SUGGESTED_5M_DEPRECATION_LOGGED:
        logger.warning(
            "USE_SUGGESTED_5M_PARAMS=true, но автоподхват suggested_5m_params.json отключён. "
            "Используются только GAME_5M_* из config.env."
        )
        _SUGGESTED_5M_DEPRECATION_LOGGED = True
    return None


def _max_position_days(ticker: Optional[str] = None) -> int:
    """Макс. срок удержания позиции (дней). Сначала подсказка из suggested_5m_params (если включено), затем GAME_5M_MAX_POSITION_DAYS_<TICKER>, иначе общий."""
    suggested = _get_suggested_5m_params()
    if ticker and suggested:
        days_map = suggested.get("max_days") or {}
        v = days_map.get(ticker.upper()) or days_map.get(ticker)
        if v is not None:
            try:
                return max(1, int(v))
            except (ValueError, TypeError):
                pass
    if ticker:
        key = f"GAME_5M_MAX_POSITION_DAYS_{ticker.upper()}"
        val = get_config_value(key, "").strip()
        if val:
            try:
                return max(1, int(val))
            except (ValueError, TypeError):
                pass
    try:
        return max(1, int(get_config_value("GAME_5M_MAX_POSITION_DAYS", "2")))
    except (ValueError, TypeError):
        return 2


def _max_position_minutes(ticker: Optional[str] = None) -> Optional[int]:
    """Макс. срок удержания в минутах (если задан) — более точная альтернатива max_position_days."""
    if ticker:
        key_t = f"GAME_5M_MAX_POSITION_MINUTES_{ticker.upper()}"
        raw_t = (get_config_value(key_t, "") or "").strip()
        if raw_t:
            try:
                v_t = int(raw_t)
                return max(15, v_t)
            except (ValueError, TypeError):
                pass
    raw = (get_config_value("GAME_5M_MAX_POSITION_MINUTES", "") or "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
        return max(15, v)
    except (ValueError, TypeError):
        return None


def _game_5m_stop_loss_enabled() -> bool:
    """Стоп-лосс 5m включён (GAME_5M_STOP_LOSS_ENABLED). При false закрытие только по тейку/TIME_EXIT/SELL."""
    raw = (get_config_value("GAME_5M_STOP_LOSS_ENABLED", "true") or "true").strip().lower()
    return raw in ("1", "true", "yes")


def _game_5m_exit_only_take() -> bool:
    """Если true — автовыход только по TAKE_PROFIT (без STOP_LOSS/TIME_EXIT/SELL)."""
    raw = (get_config_value("GAME_5M_EXIT_ONLY_TAKE", "false") or "false").strip().lower()
    return raw in ("1", "true", "yes")


def get_strategy_params() -> dict[str, Any]:
    """Текущие параметры стратегии 5m из config.env (для мониторинга и варьирования)."""
    try:
        stop = float(get_config_value("GAME_5M_STOP_LOSS_PCT", "2.5"))
    except (ValueError, TypeError):
        stop = 2.5
    try:
        take = float(get_config_value("GAME_5M_TAKE_PROFIT_PCT", "5.0"))
    except (ValueError, TypeError):
        take = 5.0
    # Импульсы менее этого % не рассматриваем — тейк берётся из конфига (Alex: «импульсы менее 2% не рассматриваем»)
    try:
        take_min = float(get_config_value("GAME_5M_TAKE_PROFIT_MIN_PCT", "2.0"))
    except (ValueError, TypeError):
        take_min = 2.0
    try:
        stop_to_take_ratio = float(get_config_value("GAME_5M_STOP_TO_TAKE_RATIO", "0.5"))
    except (ValueError, TypeError):
        stop_to_take_ratio = 0.5
    try:
        stop_min = float(get_config_value("GAME_5M_STOP_LOSS_MIN_PCT", "0.5"))
    except (ValueError, TypeError):
        stop_min = 0.5
    stop_enabled = _game_5m_stop_loss_enabled()
    return {
        "stop_loss_enabled": stop_enabled,
        "stop_loss_pct": stop,
        "take_profit_pct": take,
        "take_profit_min_pct": take_min,
        "take_profit_rule": "по импульсу 2ч (если ≥ take_profit_min_pct), иначе take_profit_pct",
        "stop_to_take_ratio": stop_to_take_ratio,
        "stop_loss_min_pct": stop_min,
        "stop_loss_rule": "min(config, тейк×ratio), не меньше stop_loss_min_pct — стоп всегда меньше тейка",
        "max_position_days": _max_position_days(None),
    }


def _engine():
    return create_engine(get_database_url())


def get_open_position(ticker: str) -> Optional[dict[str, Any]]:
    """
    Есть ли открытая позиция по тикеру в игре (GAME_5M).
    Сравнение тикера через UPPER(TRIM(...)), как в get_open_position_any — чтобы график и /pending совпадали.
    """
    ticker_upper = (ticker or "").strip().upper()
    if not ticker_upper:
        return None
    engine = _engine()
    with engine.connect() as conn:
        last_buy = conn.execute(
            text("""
                SELECT id, ts, quantity, price, signal_type
                FROM public.trade_history
                WHERE UPPER(TRIM(ticker)) = :ticker_upper AND strategy_name = :strategy AND side = 'BUY'
                ORDER BY ts DESC, id DESC
                LIMIT 1
            """),
            {"ticker_upper": ticker_upper, "strategy": GAME_5M_STRATEGY},
        ).fetchone()
        if not last_buy:
            return None
        buy_id, buy_ts, qty, price, signal_type = last_buy
        # SELL после этого BUY: позже по времени или тот же ts, но id больше (сделки в одну минуту)
        sell_after = conn.execute(
            text("""
                SELECT 1 FROM public.trade_history
                WHERE UPPER(TRIM(ticker)) = :ticker_upper AND strategy_name = :strategy AND side = 'SELL'
                  AND (ts > :after_ts OR (ts = :after_ts AND id > :buy_id))
                LIMIT 1
            """),
            {"ticker_upper": ticker_upper, "strategy": GAME_5M_STRATEGY, "after_ts": buy_ts, "buy_id": buy_id},
        ).fetchone()
        if sell_after:
            return None
    return {
        "id": buy_id,
        "ticker": ticker,
        "entry_ts": buy_ts,
        "entry_price": float(price),
        "quantity": float(qty),
        "entry_signal_type": signal_type,
    }


def get_open_position_any(ticker: str) -> Optional[dict[str, Any]]:
    """
    Открытая позиция по тикеру по всей trade_history (любая стратегия).
    Как в /pending: последний BUY без полного SELL, средневзвешенная цена входа.
    Нужно, чтобы крон видел позиции, открытые не через GAME_5M (например с другой стратегией).
    Поиск по UPPER(ticker), чтобы находить позиции при разном регистре в БД.
    """
    ticker_upper = (ticker or "").strip().upper()
    if not ticker_upper:
        return None
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT ts, id, side, quantity, price, signal_type, strategy_name
                FROM public.trade_history
                WHERE UPPER(TRIM(ticker)) = :ticker_upper
                ORDER BY ts ASC, id ASC
            """),
            {"ticker_upper": ticker_upper},
        ).fetchall()
    if not rows:
        logger.info("get_open_position_any %s: в trade_history 0 строк (проверьте БД и тикер)", ticker_upper)
        return None
    position_qty = 0.0
    position_cost = 0.0
    position_open_ts = None
    position_last_strategy = "—"
    position_last_signal = None
    for row in rows:
        ts, _id, side, qty, price, signal_type, strategy = row
        qty = float(qty or 0)
        price = float(price or 0)
        strat = (strategy or "").strip() or "—"
        if side and side.upper() == "BUY":
            if position_qty == 0:
                position_open_ts = ts
            position_qty += qty
            position_cost += qty * price
            position_last_strategy = strat
            position_last_signal = signal_type
        elif side and side.upper() == "SELL":
            if position_qty <= 0:
                continue
            avg_entry = position_cost / position_qty
            cost_sold = avg_entry * min(qty, position_qty)
            position_qty -= qty
            position_cost -= cost_sold
            if position_qty <= 0:
                position_open_ts = None
                position_cost = 0.0
    if position_qty <= 0 or position_cost <= 0:
        logger.info(
            "get_open_position_any %s: в trade_history %s строк, после скана position_qty=%.2f — позиция закрыта",
            ticker_upper, len(rows), position_qty,
        )
        return None
    return {
        "id": None,
        "ticker": ticker,
        "entry_ts": position_open_ts,
        "entry_price": position_cost / position_qty,
        "quantity": position_qty,
        "entry_signal_type": position_last_signal,
        "strategy_name": position_last_strategy,
    }


def get_open_position_game5m_vwap(ticker: str) -> Optional[dict[str, Any]]:
    """
    Нетто-позиция только GAME_5M по тикеру: средневзвешенная цена и суммарный quantity
    по хронологии BUY/SELL этой стратегии (как агрегат на графике).

    Нужна для закрытия в кроне при нескольких открытых BUY: ``get_open_position`` даёт только последний лот,
    а hanger_tune и реальный PnL позиции считаются от **склейки** лотов.
    """
    ticker_upper = (ticker or "").strip().upper()
    if not ticker_upper:
        return None
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT ts, id, side, quantity, price, signal_type
                FROM public.trade_history
                WHERE UPPER(TRIM(ticker)) = :ticker_upper AND strategy_name = :strategy
                ORDER BY ts ASC, id ASC
                """
            ),
            {"ticker_upper": ticker_upper, "strategy": GAME_5M_STRATEGY},
        ).fetchall()
    if not rows:
        return None
    position_qty = 0.0
    position_cost = 0.0
    position_open_ts = None
    position_last_signal = None
    last_buy_id: Optional[int] = None
    for row in rows:
        ts, _id, side, qty, price, signal_type = row
        qty = float(qty or 0)
        price = float(price or 0)
        if side and side.upper() == "BUY":
            if position_qty == 0:
                position_open_ts = ts
            position_qty += qty
            position_cost += qty * price
            position_last_signal = signal_type
            try:
                last_buy_id = int(_id)
            except (TypeError, ValueError):
                pass
        elif side and side.upper() == "SELL":
            if position_qty <= 0:
                continue
            avg_entry = position_cost / position_qty
            cost_sold = avg_entry * min(qty, position_qty)
            position_qty -= qty
            position_cost -= cost_sold
            if position_qty <= 0:
                position_open_ts = None
                position_cost = 0.0
    if position_qty <= 0 or position_cost <= 0:
        return None
    return {
        "id": last_buy_id,
        "ticker": ticker,
        "entry_ts": position_open_ts,
        "entry_price": position_cost / position_qty,
        "quantity": position_qty,
        "entry_signal_type": position_last_signal,
        "strategy_name": GAME_5M_STRATEGY,
        "aggregate_game5m_vwap": True,
    }


def resolve_open_position_for_game5m_close(ticker: str) -> Optional[dict[str, Any]]:
    """
    Позиция для тейка/стопа в кроне 5m: приоритет — нетто GAME_5M (VWAP по стратегии),
    иначе ``get_open_position_any`` / ``get_open_position`` (другая стратегия на тикере).

    Отключить VWAP GAME_5M: ``GAME_5M_CLOSE_USE_GAME5M_VWAP=false``.
    """
    use = (get_config_value("GAME_5M_CLOSE_USE_GAME5M_VWAP", "true") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if use:
        p = get_open_position_game5m_vwap(ticker)
        if p is not None:
            return p
    return get_open_position_any(ticker) or get_open_position(ticker)


def record_entry(
    ticker: str,
    price: float,
    signal_type: str,
    reasoning: Optional[str] = None,
    *,
    entry_context: Optional[dict[str, Any]] = None,
    trade_ts: Optional[Any] = None,
) -> Optional[int]:
    """Фиксирует бумажный вход: INSERT BUY в trade_history (strategy_name=GAME_5M).
    entry_context: контекст на момент входа (momentum_2h_pct и др.) — сохраняется в context_json для /closed_impulse (импульс при решении об открытии).
    trade_ts: время 5m-бара решения (как exit_bar_close_ts из get_decision_5m); иначе CURRENT_TIMESTAMP."""
    if price <= 0:
        logger.warning("game_5m: record_entry %s с ценой <= 0, пропуск", ticker)
        return None

    quantity = max(1, int(GAME_NOTIONAL_USD / price))
    notional = quantity * price
    commission = notional * COMMISSION_RATE
    import json as _json
    context_str = _json.dumps(entry_context) if entry_context else None
    ts_db = parse_game5m_bar_ts_for_db(trade_ts)
    engine = _engine()
    with engine.begin() as conn:
        if ts_db is not None:
            conn.execute(
                text("""
                    INSERT INTO public.trade_history (ts, ticker, side, quantity, price, commission, signal_type, total_value, sentiment_at_trade, strategy_name, ts_timezone, context_json)
                    VALUES (:ts, :ticker, 'BUY', :qty, :price, :commission, :signal_type, :total_value, NULL, :strategy, :ts_tz, :context_json)
                """),
                {
                    "ts": ts_db,
                    "ticker": ticker,
                    "qty": quantity,
                    "price": price,
                    "commission": commission,
                    "signal_type": signal_type,
                    "total_value": notional,
                    "strategy": GAME_5M_STRATEGY,
                    "ts_tz": TRADE_HISTORY_TZ,
                    "context_json": context_str,
                },
            )
        else:
            conn.execute(
                text("""
                    INSERT INTO public.trade_history (ts, ticker, side, quantity, price, commission, signal_type, total_value, sentiment_at_trade, strategy_name, ts_timezone, context_json)
                    VALUES (CURRENT_TIMESTAMP, :ticker, 'BUY', :qty, :price, :commission, :signal_type, :total_value, NULL, :strategy, :ts_tz, :context_json)
                """),
                {
                    "ticker": ticker,
                    "qty": quantity,
                    "price": price,
                    "commission": commission,
                    "signal_type": signal_type,
                    "total_value": notional,
                    "strategy": GAME_5M_STRATEGY,
                    "ts_tz": TRADE_HISTORY_TZ,
                    "context_json": context_str,
                },
            )
        row = conn.execute(text("SELECT LASTVAL()")).fetchone()
        new_id = row[0] if row else None
    logger.info("game_5m: вход %s id=%s @ %.2f qty=%s %s", ticker, new_id, price, quantity, signal_type)
    return new_id


def get_latest_buy_context_json(
    ticker: str,
    strategy: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """
    context_json последней записи BUY по тикеру (для связки «вход → выход» в одном narrative).
    """
    strat = (strategy or GAME_5M_STRATEGY).strip() or GAME_5M_STRATEGY
    ticker_upper = (ticker or "").strip().upper()
    if not ticker_upper:
        return None
    engine = _engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT context_json FROM public.trade_history
                WHERE UPPER(TRIM(ticker)) = :ticker_upper AND strategy_name = :strategy AND side = 'BUY'
                ORDER BY ts DESC, id DESC
                LIMIT 1
            """),
            {"ticker_upper": ticker_upper, "strategy": strat},
        ).fetchone()
    if not row or row[0] is None:
        return None
    raw = row[0]
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if isinstance(raw, str) else None
    except Exception:
        return None


def close_position(
    ticker: str,
    exit_price: float,
    exit_signal_type: str,
    position: Optional[dict[str, Any]] = None,
    *,
    bar_high: Optional[float] = None,
    bar_low: Optional[float] = None,
    context_json: Optional[dict[str, Any]] = None,
    trade_ts: Optional[Any] = None,
) -> Optional[float]:
    """Закрывает открытую позицию: INSERT SELL. Возвращает PnL в %.
    position — если передан (например от get_open_position_any), SELL пишется с strategy_name из позиции;
    иначе берётся позиция только GAME_5M.
    bar_high/bar_low — опционально: при TAKE_PROFIT / TAKE_PROFIT_SUSPEND цена не выше bar_high, при STOP_LOSS не ниже bar_low.
    Если не заданы — применяется ограничение по entry (макс. +15% тейк / −15% стоп), чтобы в БД не попали нереальные цифры из глючных котировок.
    trade_ts: время 5m-бара выхода (exit_bar_close_ts из build_5m_close_context); иначе CURRENT_TIMESTAMP — для графика лучше передавать бар."""
    if position is None:
        position = get_open_position(ticker)
        strategy = GAME_5M_STRATEGY
    else:
        strategy = position.get("strategy_name") or GAME_5M_STRATEGY
    if not position:
        logger.info("game_5m: по %s нет открытой позиции для закрытия", ticker)
        return None

    entry_price = float(position["entry_price"])
    quantity = position["quantity"]
    if entry_price <= 0 or exit_price <= 0:
        return None

    # Защита от нереальной цены в БД (глюк 5m/quotes или ручное закрытие с опечаткой)
    original_exit = exit_price
    if exit_signal_type in ("TAKE_PROFIT", "TAKE_PROFIT_SUSPEND"):
        if bar_high is not None and bar_high > 0:
            exit_price = min(exit_price, bar_high)
        else:
            cap = entry_price * 1.15
            if exit_price > cap:
                logger.warning(
                    "game_5m: %s %s exit_price=%.2f выше разумного (entry=%.2f, cap +15%%=%.2f) — записываем %.2f",
                    ticker, exit_signal_type, original_exit, entry_price, cap, cap,
                )
                exit_price = cap
    elif exit_signal_type == "STOP_LOSS":
        if bar_low is not None and bar_low > 0:
            exit_price = max(exit_price, bar_low)
        else:
            cap = entry_price * 0.85
            if exit_price < cap:
                logger.warning(
                    "game_5m: %s STOP_LOSS exit_price=%.2f ниже разумного (entry=%.2f, cap −15%%=%.2f) — записываем %.2f",
                    ticker, original_exit, entry_price, cap, cap,
                )
                exit_price = cap
    if abs(exit_price - entry_price) / entry_price > 0.25:
        logger.warning(
            "game_5m: %s закрытие @ %.2f (entry=%.2f) даёт PnL ~%.0f%% — проверьте источник цены",
            ticker, exit_price, entry_price, (exit_price / entry_price - 1.0) * 100.0,
        )

    notional = quantity * exit_price
    commission = notional * COMMISSION_RATE
    log_return = math.log(exit_price / entry_price)
    pnl_pct = float(log_return * 100.0 - 2 * COMMISSION_RATE * 100.0)

    import json
    ts_db = parse_game5m_bar_ts_for_db(trade_ts)
    engine = _engine()
    with engine.begin() as conn:
        if ts_db is not None:
            conn.execute(
                text("""
                    INSERT INTO public.trade_history (ts, ticker, side, quantity, price, commission, signal_type, total_value, sentiment_at_trade, strategy_name, ts_timezone, context_json)
                    VALUES (:ts, :ticker, 'SELL', :qty, :price, :commission, :signal_type, :total_value, NULL, :strategy, :ts_tz, :context_json)
                """),
                {
                    "ts": ts_db,
                    "ticker": ticker,
                    "qty": quantity,
                    "price": exit_price,
                    "commission": commission,
                    "signal_type": exit_signal_type,
                    "total_value": notional,
                    "strategy": strategy,
                    "ts_tz": TRADE_HISTORY_TZ,
                    "context_json": json.dumps(context_json) if context_json else None,
                },
            )
        else:
            conn.execute(
                text("""
                    INSERT INTO public.trade_history (ts, ticker, side, quantity, price, commission, signal_type, total_value, sentiment_at_trade, strategy_name, ts_timezone, context_json)
                    VALUES (CURRENT_TIMESTAMP, :ticker, 'SELL', :qty, :price, :commission, :signal_type, :total_value, NULL, :strategy, :ts_tz, :context_json)
                """),
                {
                    "ticker": ticker,
                    "qty": quantity,
                    "price": exit_price,
                    "commission": commission,
                    "signal_type": exit_signal_type,
                    "total_value": notional,
                    "strategy": strategy,
                    "ts_tz": TRADE_HISTORY_TZ,
                    "context_json": json.dumps(context_json) if context_json else None,
                },
            )
    logger.info("game_5m: %s закрыта @ %.2f %s (strategy=%s), PnL=%.2f%%", ticker, exit_price, exit_signal_type, strategy, pnl_pct)
    return pnl_pct


def _chart_range_et_to_msk(dt_min: datetime, dt_max: datetime, margin_days: int = 1) -> tuple[datetime, datetime]:
    """Переводит диапазон графика (ET) в московское время для запроса к БД. Добавляет margin_days с обеих сторон."""
    try:
        import pandas as pd
        t_lo = pd.Timestamp(dt_min)
        t_hi = pd.Timestamp(dt_max)
        if t_lo.tzinfo is None:
            t_lo = t_lo.tz_localize(CHART_DISPLAY_TZ)
        else:
            t_lo = t_lo.tz_convert(CHART_DISPLAY_TZ)
        if t_hi.tzinfo is None:
            t_hi = t_hi.tz_localize(CHART_DISPLAY_TZ)
        else:
            t_hi = t_hi.tz_convert(CHART_DISPLAY_TZ)
        t_lo_msk = t_lo.tz_convert(TRADE_HISTORY_TZ)
        t_hi_msk = t_hi.tz_convert(TRADE_HISTORY_TZ)
        lo = (t_lo_msk - pd.Timedelta(days=margin_days)).tz_localize(None)
        hi = (t_hi_msk + pd.Timedelta(days=margin_days)).tz_localize(None)
        return lo.to_pydatetime() if hasattr(lo, "to_pydatetime") else lo, hi.to_pydatetime() if hasattr(hi, "to_pydatetime") else hi
    except Exception:
        return dt_min, dt_max


def get_trades_for_chart(
    ticker: str,
    dt_min: datetime,
    dt_max: datetime,
) -> list[dict[str, Any]]:
    """Сделки GAME_5M по тикеру в заданном диапазоне времени (для нанесения на график 5m).
    dt_min, dt_max — диапазон графика в ET. В БД ts хранятся в Moscow, поэтому диапазон переводится в MSK.
    Возвращает список dict: ts, price, quantity, side ('BUY'|'SELL'), signal_type, ts_timezone (если есть),
    опционально chart_ts — ISO ET времени бара из context_json (для маркера по оси X), без сырого context_json."""
    # Широкий запас по ts в SQL: старые SELL могли иметь ts=момент INSERT, а не бар — иначе строка не попадёт в выборку.
    dt_lo, dt_hi = _chart_range_et_to_msk(dt_min, dt_max, margin_days=3)
    engine = _engine()
    ticker_upper = (ticker or "").strip().upper()
    params = {"ticker_upper": ticker_upper, "strategy": GAME_5M_STRATEGY, "dt_min": dt_lo, "dt_max": dt_hi}
    with engine.connect() as conn:
        try:
            rows = conn.execute(
                text("""
                    SELECT id, ts, side, price, quantity, signal_type, ts_timezone, context_json
                    FROM public.trade_history
                    WHERE UPPER(TRIM(ticker)) = :ticker_upper AND strategy_name = :strategy
                      AND ts >= :dt_min AND ts <= :dt_max
                    ORDER BY ts ASC, id ASC
                """),
                params,
            ).fetchall()
            raw = []
            for r in rows:
                ctx = r[7]
                chart_iso = chart_ts_iso_from_context(ctx)
                row = {
                    "id": int(r[0]),
                    "ts": r[1],
                    "price": float(r[3]),
                    "quantity": float(r[4] or 0),
                    "side": r[2],
                    "signal_type": r[5] or "",
                    "ts_timezone": r[6],
                }
                if chart_iso:
                    row["chart_ts"] = chart_iso
                raw.append(row)
        except Exception:
            rows = conn.execute(
                text("""
                    SELECT id, ts, side, price, quantity, signal_type
                    FROM public.trade_history
                    WHERE UPPER(TRIM(ticker)) = :ticker_upper AND strategy_name = :strategy
                      AND ts >= :dt_min AND ts <= :dt_max
                    ORDER BY ts ASC, id ASC
                """),
                params,
            ).fetchall()
            raw = [
                {
                    "id": int(r[0]),
                    "ts": r[1],
                    "price": float(r[3]),
                    "quantity": float(r[4] or 0),
                    "side": r[2],
                    "signal_type": r[5] or "",
                    "ts_timezone": None,
                }
                for r in rows
            ]

    # SQL-запрос берёт небольшой запас из-за разных форматов хранения ts.
    # Для графика возвращаем только сделки, реально попавшие в видимое окно ET,
    # иначе frontend может приклеить вчерашний SELL к ближайшей сегодняшней свече.
    # Учитываем и chart_ts (бар из context_json), если ts записи вне окна из-за старого CURRENT_TIMESTAMP.
    try:
        import pandas as pd

        t_lo = pd.Timestamp(dt_min)
        t_hi = pd.Timestamp(dt_max)
        if t_lo.tzinfo is None:
            t_lo = t_lo.tz_localize(CHART_DISPLAY_TZ)
        else:
            t_lo = t_lo.tz_convert(CHART_DISPLAY_TZ)
        if t_hi.tzinfo is None:
            t_hi = t_hi.tz_localize(CHART_DISPLAY_TZ)
        else:
            t_hi = t_hi.tz_convert(CHART_DISPLAY_TZ)

        def _in_et_window(ts_et: Any) -> bool:
            if ts_et is None:
                return False
            t = pd.Timestamp(ts_et)
            if t.tzinfo is None:
                t = t.tz_localize(CHART_DISPLAY_TZ)
            else:
                t = t.tz_convert(CHART_DISPLAY_TZ)
            return bool(t_lo <= t <= t_hi)

        filtered = []
        for item in raw:
            ts_et_db = trade_ts_to_et(item.get("ts"), source_tz=item.get("ts_timezone"))
            in_win = _in_et_window(ts_et_db)
            chart_iso = item.get("chart_ts")
            if not in_win and chart_iso:
                in_win = _in_et_window(trade_ts_to_et(chart_iso, source_tz="America/New_York"))
            if not in_win:
                continue
            filtered.append(item)
        return filtered
    except Exception:
        logger.debug("get_trades_for_chart: не удалось отфильтровать сделки по ET-окну", exc_info=True)
        return raw


def partition_trades_for_chart_pnl(trades: list[dict[str, Any]]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Разнести сделки для маркеров на графике: BUY; SELL с P/L ≥ 0; SELL с P/L < 0; SELL без базы.

    Учёт средней цены входа по нарастающей (как агрегированная открытая позиция), не строгий FIFO по лотам.
    При полном закрытии позиции суммарный реализованный P/L совпадает с FIFO; распределение по частичным SELL может отличаться.
    """
    sorted_trades = sorted(
        trades,
        key=lambda t: (t.get("ts"), int(t.get("id") or 0)),
    )
    buy_trades: list[dict] = []
    sell_win: list[dict] = []
    sell_loss: list[dict] = []
    sell_neutral: list[dict] = []
    pos_qty = 0.0
    pos_cost = 0.0
    for t in sorted_trades:
        try:
            price = float(t.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            continue
        try:
            q = float(t.get("quantity") or 0)
        except (TypeError, ValueError):
            q = 0.0
        if q <= 0:
            q = 1.0
        side = (t.get("side") or "").strip().upper()
        if side == "BUY":
            pos_qty += q
            pos_cost += q * price
            buy_trades.append(t)
        elif side == "SELL":
            pnl_pct: Optional[float] = None
            if pos_qty > 0 and pos_cost > 0:
                avg = pos_cost / pos_qty
                pnl_pct = ((price - avg) / avg) * 100.0
                sq = min(q, pos_qty)
                pos_cost -= avg * sq
                pos_qty -= sq
            if pnl_pct is None or not math.isfinite(pnl_pct):
                sell_neutral.append(t)
            elif pnl_pct >= 0:
                sell_win.append(t)
            else:
                sell_loss.append(t)
    return buy_trades, sell_win, sell_loss, sell_neutral


def get_recent_results(ticker: str, limit: int = 20) -> list[dict[str, Any]]:
    """Последние закрытые пары BUY→SELL по тикеру."""
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, ts, side, quantity, price, signal_type
                FROM public.trade_history
                WHERE ticker = :ticker AND strategy_name = :strategy
                ORDER BY ts ASC, id ASC
            """),
            {"ticker": ticker, "strategy": GAME_5M_STRATEGY},
        ).fetchall()

    result = []
    i = 0
    while i < len(rows):
        r = rows[i]
        if r[2] != "BUY":
            i += 1
            continue
        buy_id, buy_ts, _, qty, entry_price, entry_signal = r
        entry_price = float(entry_price)
        qty = float(qty)
        j = i + 1
        while j < len(rows) and rows[j][2] != "SELL":
            j += 1
        if j >= len(rows):
            break
        sell_row = rows[j]
        exit_ts = sell_row[1]
        exit_price = float(sell_row[4])
        exit_signal = sell_row[5]
        try:
            log_ret = math.log(exit_price / entry_price)
            pnl_pct = float(log_ret * 100.0 - 2 * COMMISSION_RATE * 100.0)
        except Exception:
            pnl_pct = None
        try:
            pnl_usd = (exit_price - entry_price) * qty - 2 * COMMISSION_RATE * (entry_price + exit_price) * qty / 2
        except Exception:
            pnl_usd = None
        result.append({
            "id": buy_id,
            "entry_ts": buy_ts,
            "entry_price": entry_price,
            "quantity": qty,
            "entry_signal_type": entry_signal,
            "exit_ts": exit_ts,
            "exit_price": exit_price,
            "exit_signal_type": exit_signal,
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
        })
        i = j + 1

    result.reverse()
    return result[:limit]


_HANGER_TUNE_CACHE: dict[str, Any] = {"path": "", "mtime": 0.0, "per_ticker": {}}


def _hanger_tune_min_cap_pct(ticker_upper: str, *, apply_hanger_json: Optional[bool] = None) -> Optional[float]:
    """
    Минимальный proposed_cap_pct по тикеру из ``hanger_hypotheses`` (где есть remediation_take_cap).
    Консервативно для выхода: самый низкий кап = раньше/легче срабатывание тейка на агрегате.

    ``apply_hanger_json=False`` — не читать JSON (основной алгоритм).
    ``None`` — уважать ``GAME_5M_HANGER_TUNE_APPLY_TAKE`` в config.
    ``True`` — применять JSON, если файл задан (режим «только висок»).
    """
    global _HANGER_TUNE_CACHE
    if apply_hanger_json is False:
        return None
    if apply_hanger_json is None:
        apply_raw = (get_config_value("GAME_5M_HANGER_TUNE_APPLY_TAKE", "false") or "false").strip().lower()
        if apply_raw not in ("1", "true", "yes"):
            return None
    raw_path = (get_config_value("GAME_5M_HANGER_TUNE_JSON", "") or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_file():
        return None
    try:
        mtime = float(path.stat().st_mtime)
    except OSError:
        return None
    key_path = str(path.resolve())
    if _HANGER_TUNE_CACHE.get("path") != key_path or float(_HANGER_TUNE_CACHE.get("mtime") or 0) != mtime:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return None
        per: dict[str, float] = {}
        for h in data.get("hanger_hypotheses") or []:
            if not isinstance(h, dict):
                continue
            cap_obj = h.get("remediation_take_cap")
            if not isinstance(cap_obj, dict):
                continue
            try:
                prop = float(cap_obj.get("proposed_cap_pct"))
            except (TypeError, ValueError):
                continue
            t = str(h.get("ticker") or "").strip().upper()
            if not t:
                continue
            prev = per.get(t)
            if prev is None or prop < prev:
                per[t] = prop
        _HANGER_TUNE_CACHE = {"path": key_path, "mtime": mtime, "per_ticker": per}
    caps = _HANGER_TUNE_CACHE.get("per_ticker") or {}
    if not isinstance(caps, dict):
        return None
    v = caps.get(ticker_upper.strip().upper())
    return float(v) if v is not None else None


def _apply_hanger_take_cap_to_base(
    ticker: Optional[str], base: float, *, apply_hanger_json: Optional[bool] = None
) -> float:
    if not ticker:
        return base
    hang = _hanger_tune_min_cap_pct(str(ticker).strip().upper(), apply_hanger_json=apply_hanger_json)
    if hang is None:
        return base
    return min(float(base), float(hang))


def _take_profit_cap_pct(ticker: Optional[str] = None, *, apply_hanger_json: Optional[bool] = None) -> float:
    """Потолок тейка (%): suggested → GAME_5M_TAKE_PROFIT_PCT_<T> → общий; опционально сужение из hanger JSON."""
    suggested = _get_suggested_5m_params()
    if ticker and suggested:
        take_map = suggested.get("take_pct") or {}
        v = take_map.get(ticker.upper()) or take_map.get(ticker)
        if v is not None:
            try:
                return _apply_hanger_take_cap_to_base(
                    ticker, max(2.0, min(10.0, float(v))), apply_hanger_json=apply_hanger_json
                )
            except (ValueError, TypeError):
                pass
    if ticker:
        key = f"GAME_5M_TAKE_PROFIT_PCT_{ticker.upper()}"
        raw = get_config_value(key, "").strip()
        if raw:
            try:
                return _apply_hanger_take_cap_to_base(ticker, float(raw), apply_hanger_json=apply_hanger_json)
            except (ValueError, TypeError):
                pass
    params = get_strategy_params()
    return _apply_hanger_take_cap_to_base(ticker, float(params["take_profit_pct"]), apply_hanger_json=apply_hanger_json)


def _effective_take_profit_pct(
    momentum_2h_pct: Optional[float],
    ticker: Optional[str] = None,
    *,
    apply_hanger_json: Optional[bool] = None,
) -> float:
    """Тейк-профит по импульсу 2ч или из конфига.

    Формула:
    - Если импульс_2ч >= GAME_5M_TAKE_PROFIT_MIN_PCT (4%): тейк = min(импульс_2ч × GAME_5M_TAKE_MOMENTUM_FACTOR, потолок).
    - Иначе: тейк = потолок (GAME_5M_TAKE_PROFIT_PCT или GAME_5M_TAKE_PROFIT_PCT_<TICKER>).
    Потолок не даёт тейку превысить заданный %; при импульсе 6% и factor=1 тейк = 6% (поэтому сработало на 6%, а не 7%).
    Фактор может быть >1.0 (например 1.05): цель от импульса выше сырого %% до упора в потолок; в коде ограничен сверху константой ниже.
    """
    cap = _take_profit_cap_pct(ticker, apply_hanger_json=apply_hanger_json)
    try:
        min_take = float(get_config_value("GAME_5M_TAKE_PROFIT_MIN_PCT", "2.0"))
    except (ValueError, TypeError):
        min_take = 2.0
    try:
        momentum_factor = float(get_config_value("GAME_5M_TAKE_MOMENTUM_FACTOR", "1.0"))
        # >1.0 допустим (агрессивнее тейк от 2h-импульса); trade_effectiveness_analyzer предлагает до ~1.35.
        momentum_factor = max(0.3, min(2.0, momentum_factor))
    except (ValueError, TypeError):
        momentum_factor = 1.0
    if momentum_2h_pct is not None and momentum_2h_pct >= min_take:
        effective_momentum = float(momentum_2h_pct) * momentum_factor
        return min(effective_momentum, cap)
    return cap


def _effective_stop_loss_pct(
    momentum_2h_pct: Optional[float],
    ticker: Optional[str] = None,
    *,
    apply_hanger_json: Optional[bool] = None,
) -> float:
    """Стоп-лосс: меньше тейка, прогнозируется как доля от эффективного тейка (тейк от импульса 2ч)."""
    params = get_strategy_params()
    config_stop = params["stop_loss_pct"]
    take_pct = _effective_take_profit_pct(momentum_2h_pct, ticker=ticker, apply_hanger_json=apply_hanger_json)
    try:
        ratio = float(get_config_value("GAME_5M_STOP_TO_TAKE_RATIO", "0.5"))
    except (ValueError, TypeError):
        ratio = 0.5
    try:
        min_stop = float(get_config_value("GAME_5M_STOP_LOSS_MIN_PCT", "0.5"))
    except (ValueError, TypeError):
        min_stop = 0.5
    # стоп = min(config, тейк×ratio), не меньше min_stop — всегда меньше тейка
    from_ratio = take_pct * ratio
    effective = min(config_stop, from_ratio)
    return max(min_stop, effective)


def _game_5m_soft_take_near_high_params() -> tuple[bool, float, float]:
    """Мягкий тейк у хая в NEAR_OPEN: вкл, мин. PnL %, макс. откат от session high %."""
    raw = (get_config_value("GAME_5M_SOFT_TAKE_NEAR_HIGH_ENABLED", "true") or "true").strip().lower()
    enabled = raw in ("1", "true", "yes")
    try:
        min_pnl = float((get_config_value("GAME_5M_SOFT_TAKE_NEAR_HIGH_MIN_PCT", "2.0") or "2.0").strip())
    except (ValueError, TypeError):
        min_pnl = 2.0
    try:
        max_pull = float((get_config_value("GAME_5M_SOFT_TAKE_MAX_PULLBACK_FROM_HIGH_PCT", "0.35") or "0.35").strip())
    except (ValueError, TypeError):
        max_pull = 0.35
    return enabled, max(0.5, min_pnl), max(0.05, max_pull)


def _game_5m_stale_reversal_exit_params(ticker: Optional[str] = None) -> dict[str, Any]:
    """Time-and-signal invalidation for old GAME_5M longs that stopped behaving like quick trades."""
    raw_enabled = (get_config_value("GAME_5M_STALE_REVERSAL_EXIT_ENABLED", "false") or "false").strip().lower()
    enabled = raw_enabled in ("1", "true", "yes")

    def _int_param(key: str, default: int, min_value: int) -> int:
        raw = (get_config_value(key, str(default)) or str(default)).strip()
        try:
            return max(min_value, int(raw))
        except (ValueError, TypeError):
            return max(min_value, default)

    def _float_param(key: str, default: float) -> float:
        raw = (get_config_value(key, str(default)) or str(default)).strip().replace(",", ".")
        try:
            return float(raw)
        except (ValueError, TypeError):
            return default

    ticker_u = str(ticker or "").strip().upper()
    min_age = _int_param("GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES", 390, 15)
    if ticker_u:
        raw_t = (get_config_value(f"GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES_{ticker_u}", "") or "").strip()
        if raw_t:
            try:
                min_age = max(15, int(raw_t))
            except (ValueError, TypeError):
                pass
    return {
        "enabled": enabled,
        "min_age_minutes": min_age,
        "max_pnl_pct": _float_param("GAME_5M_STALE_REVERSAL_MAX_PNL_PCT", -1.5),
        "momentum_below": _float_param("GAME_5M_STALE_REVERSAL_MOMENTUM_BELOW", 0.0),
    }


def _game_5m_position_age_minutes(open_position: dict, *, ref: Optional[datetime] = None) -> Optional[float]:
    entry_ts = open_position.get("entry_ts") if open_position else None
    if entry_ts is None:
        return None
    try:
        import pandas as pd

        et = pd.Timestamp(entry_ts)
        rt = pd.Timestamp(ref or datetime.now())
        if et.tzinfo is None:
            et = et.tz_localize(TRADE_HISTORY_TZ, ambiguous=True).tz_convert(CHART_DISPLAY_TZ)
        else:
            et = et.tz_convert(CHART_DISPLAY_TZ)
        if rt.tzinfo is None:
            rt = rt.tz_localize(CHART_DISPLAY_TZ, ambiguous=True)
        else:
            rt = rt.tz_convert(CHART_DISPLAY_TZ)
        return max(0.0, float((rt - et).total_seconds() / 60.0))
    except Exception:
        return None


def classify_game5m_position_state_v2(
    open_position: dict,
    *,
    current_price: Optional[float],
    current_decision: str,
    momentum_2h_pct: Optional[float],
    take_pct: Optional[float],
    ref_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Rule-based Hanger Definition v2 diagnostics.

    This is intentionally explainable and suitable for context_json logging. It does not execute trades by itself.
    """
    raw_enabled = (get_config_value("GAME_5M_HANGER_V2_ENABLED", "false") or "false").strip().lower()
    enabled = raw_enabled in ("1", "true", "yes")

    def _cfg_float(key: str, default: float) -> float:
        raw = (get_config_value(key, str(default)) or str(default)).strip().replace(",", ".")
        try:
            return float(raw)
        except (ValueError, TypeError):
            return default

    def _cfg_int(key: str, default: int, min_value: int = 1) -> int:
        raw = (get_config_value(key, str(default)) or str(default)).strip()
        try:
            return max(min_value, int(raw))
        except (ValueError, TypeError):
            return max(min_value, default)

    params = {
        "enabled": enabled,
        "recoverable_min_age_minutes": _cfg_int("GAME_5M_HANGER_V2_RECOVERABLE_MIN_AGE_MINUTES", 390, 15),
        "recoverable_min_pnl_pct": _cfg_float("GAME_5M_HANGER_V2_RECOVERABLE_MIN_PNL_PCT", -1.0),
        "recoverable_max_pnl_pct": _cfg_float("GAME_5M_HANGER_V2_RECOVERABLE_MAX_PNL_PCT", 1.0),
        "weak_momentum_below": _cfg_float("GAME_5M_HANGER_V2_WEAK_MOMENTUM_BELOW", 0.5),
        "stale_max_pnl_pct": _cfg_float("GAME_5M_HANGER_V2_STALE_MAX_PNL_PCT", -1.5),
        "stale_momentum_below": _cfg_float("GAME_5M_HANGER_V2_STALE_MOMENTUM_BELOW", 0.0),
    }

    out: Dict[str, Any] = {
        "enabled": enabled,
        "state": "unknown",
        "score": 0,
        "params": params,
    }
    if not enabled:
        out["state"] = "disabled"
        return out
    try:
        entry = float(open_position.get("entry_price") or 0)
        price = float(current_price or 0)
    except (TypeError, ValueError):
        entry, price = 0.0, 0.0
    if entry <= 0 or price <= 0:
        out["reason"] = "missing_entry_or_price"
        return out

    age_min = _game_5m_position_age_minutes(open_position, ref=ref_time)
    pnl_pct = (price / entry - 1.0) * 100.0
    try:
        mom = None if momentum_2h_pct is None else float(momentum_2h_pct)
    except (TypeError, ValueError):
        mom = None
    try:
        take_f = None if take_pct is None else float(take_pct)
    except (TypeError, ValueError):
        take_f = None
    distance_to_take = (take_f - pnl_pct) if take_f is not None else None
    decision_norm = str(current_decision or "").strip().upper()
    weak_decision = decision_norm in ("HOLD", "SELL")
    weak_momentum = mom is None or mom <= params["weak_momentum_below"]
    stale_momentum = mom is None or mom <= params["stale_momentum_below"]
    old_enough = age_min is not None and age_min >= params["recoverable_min_age_minutes"]

    components = {
        "age_old": int(bool(old_enough)),
        "pnl_negative": int(pnl_pct < 0),
        "weak_momentum": int(bool(weak_momentum)),
        "weak_decision": int(bool(weak_decision)),
        "far_from_take": int(distance_to_take is not None and distance_to_take > 1.0),
    }
    score = int(sum(components.values()))

    if old_enough and pnl_pct <= params["stale_max_pnl_pct"] and stale_momentum and weak_decision:
        state = "stale_reversal"
    elif (
        old_enough
        and params["recoverable_min_pnl_pct"] <= pnl_pct <= params["recoverable_max_pnl_pct"]
        and weak_momentum
        and (distance_to_take is None or distance_to_take > 0)
    ):
        state = "recoverable_hanger"
    else:
        state = "normal_hold"

    out.update(
        {
            "state": state,
            "score": score,
            "components": components,
            "age_minutes": None if age_min is None else round(age_min, 2),
            "pnl_pct": round(pnl_pct, 4),
            "momentum_2h_pct": None if mom is None else round(mom, 4),
            "decision": decision_norm or "—",
            "take_pct": None if take_f is None else round(take_f, 4),
            "distance_to_take_pct": None if distance_to_take is None else round(distance_to_take, 4),
        }
    )
    return out


def evaluate_game5m_continuation_gate(
    *,
    ticker: Optional[str],
    pnl_pct: Optional[float],
    momentum_2h_pct: Optional[float],
    rsi_5m: Optional[float],
    volume_vs_avg_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """Rule-based log-only continuation gate for TAKE_PROFIT underprofit analysis."""
    raw_enabled = (get_config_value("GAME_5M_CONTINUATION_GATE_ENABLED", "false") or "false").strip().lower()
    enabled = raw_enabled in ("1", "true", "yes")
    raw_log_only = (get_config_value("GAME_5M_CONTINUATION_GATE_LOG_ONLY", "true") or "true").strip().lower()
    log_only = raw_log_only in ("1", "true", "yes")

    def _cfg_float(key: str, default: float) -> float:
        raw = (get_config_value(key, str(default)) or str(default)).strip().replace(",", ".")
        try:
            return float(raw)
        except (ValueError, TypeError):
            return default

    params = {
        "enabled": enabled,
        "log_only": log_only,
        "min_pnl_pct": _cfg_float("GAME_5M_CONTINUATION_MIN_PNL_PCT", 2.0),
        "min_momentum_2h_pct": _cfg_float("GAME_5M_CONTINUATION_MIN_MOMENTUM_2H_PCT", 1.0),
        "max_rsi_5m": _cfg_float("GAME_5M_CONTINUATION_MAX_RSI_5M", 72.0),
        "min_volume_vs_avg_pct": _cfg_float("GAME_5M_CONTINUATION_MIN_VOLUME_VS_AVG_PCT", 0.0),
        "extend_take_add_pct": _cfg_float("GAME_5M_CONTINUATION_EXTEND_TAKE_ADD_PCT", 1.0),
        "max_extra_minutes": _cfg_float("GAME_5M_CONTINUATION_MAX_EXTRA_MINUTES", 120.0),
        "trail_pullback_pct": _cfg_float("GAME_5M_CONTINUATION_TRAIL_PULLBACK_PCT", 0.7),
    }
    out: Dict[str, Any] = {
        "enabled": enabled,
        "log_only": log_only,
        "decision": "disabled",
        "would_extend_take": False,
        "params": params,
        "ticker": ticker,
    }
    if not enabled:
        return out
    try:
        pnl = None if pnl_pct is None else float(pnl_pct)
        mom = None if momentum_2h_pct is None else float(momentum_2h_pct)
        rsi = None if rsi_5m is None else float(rsi_5m)
        vol = None if volume_vs_avg_pct is None else float(volume_vs_avg_pct)
    except (TypeError, ValueError):
        pnl = mom = rsi = vol = None

    checks = {
        "pnl_ok": pnl is not None and pnl >= params["min_pnl_pct"],
        "momentum_ok": mom is not None and mom >= params["min_momentum_2h_pct"],
        "rsi_ok": rsi is None or rsi <= params["max_rsi_5m"],
        "volume_ok": params["min_volume_vs_avg_pct"] <= 0 or (vol is not None and vol >= params["min_volume_vs_avg_pct"]),
    }
    would = all(bool(v) for v in checks.values())
    out.update(
        {
            "decision": "extend_take_candidate" if would else "close_now",
            "would_extend_take": bool(would),
            "checks": checks,
            "pnl_pct": None if pnl is None else round(pnl, 4),
            "momentum_2h_pct": None if mom is None else round(mom, 4),
            "rsi_5m": None if rsi is None else round(rsi, 4),
            "volume_vs_avg_pct": None if vol is None else round(vol, 4),
        }
    )
    return out


def should_close_position(
    open_position: dict,
    current_decision: str,
    current_price: Optional[float],
    momentum_2h_pct: Optional[float] = None,
    bar_high: Optional[float] = None,
    bar_low: Optional[float] = None,
    rsi_5m: Optional[float] = None,
    *,
    pullback_from_high_pct: Optional[float] = None,
    session_phase: Optional[str] = None,
    simulation_time: Optional[datetime] = None,
    apply_hanger_json: Optional[bool] = None,
) -> Tuple[bool, str, str]:
    """Закрывать ли позицию: по тейку/стопу (цена), по истечении срока/сессии (TIME_EXIT), early de-risk (TIME_EXIT_EARLY).

    Возвращает (should_close, signal_type, exit_detail). exit_detail — уточнение для TIME_EXIT:
    session_end | max_hold_minutes | max_hold_days; для TIME_EXIT_EARLY — early_derisk; иначе пустая строка.
    Для мягкого тейка у хая: exit_detail = soft_near_high_open.

    simulation_time: если задан (например момент закрытия бара при бэктесте), возраст позиции и TIME_EXIT
    считаются относительно него; выход «конец сессии» по живому get_market_session_context не вызывается.

    apply_hanger_json: None — как в config (``GAME_5M_HANGER_TUNE_APPLY_TAKE``); False — не сужать тейк JSON;
    True — сужать (режим «только висок», см. ``GAME_5M_HANGER_DUAL_MODE`` в кроне).
    При ``apply_hanger_json=True`` и срабатывании порога по цене в БД уходит ``TAKE_PROFIT_SUSPEND`` (тейк по алгоритму висяка), иначе ``TAKE_PROFIT``.
    """
    if current_price is None or current_price <= 0:
        return False, "", ""

    entry_price = open_position.get("entry_price")
    if isinstance(entry_price, (int, float)) and entry_price > 0:
        tkr = open_position.get("ticker")
        take_pct = _effective_take_profit_pct(momentum_2h_pct, ticker=tkr, apply_hanger_json=apply_hanger_json)
        stop_pct = _effective_stop_loss_pct(momentum_2h_pct, ticker=tkr, apply_hanger_json=apply_hanger_json)
        # Для тейка учитываем High последней свечи (отскок вверх при открытии сессии)
        price_for_take = max(current_price, bar_high) if bar_high is not None and bar_high > 0 else current_price
        # Для стопа учитываем Low последней свечи
        price_for_stop = min(current_price, bar_low) if bar_low is not None and bar_low > 0 else current_price
        pnl_take_pct = (price_for_take - entry_price) / entry_price * 100.0
        pnl_stop_pct = (price_for_stop - entry_price) / entry_price * 100.0
        ticker = open_position.get("ticker", "?")
        # Допуск 0.05%: в pending может показываться 2.7%, а в кроне (5m/quotes) получается 2.67% — чтобы тейк сработал
        take_threshold = take_pct - 0.05
        if pnl_take_pct >= take_threshold:
            exit_sig = "TAKE_PROFIT_SUSPEND" if apply_hanger_json is True else "TAKE_PROFIT"
            return True, exit_sig, ""
        exit_only_take = _game_5m_exit_only_take()
        # DEBUG: всегда пишем pnl vs порог; INFO — только когда до тейка осталось ≤0.5%
        logger.debug(
            "GAME_5M %s: тейк не сработал — pnl=%.2f%%, порог тейка=%.2f%% (>= %.2f%% с допуском 0.05%%)",
            ticker, pnl_take_pct, take_pct, take_threshold,
        )
        if 0 < pnl_take_pct < take_threshold and take_pct - pnl_take_pct <= 0.5:
            logger.info(
                "GAME_5M %s: тейк не достигнут — pnl=%.2f%%, порог=%.2f%% (с допуском 0.05%% сработает при >= %.2f%%)",
                ticker, pnl_take_pct, take_pct, take_threshold,
            )
        # Мягкий тейк: в первый час RTH цена у дневного high, PnL уже есть, но до полного тейка не дотянули —
        # фиксируем, чтобы не ловить разворот после «погони» у хая (см. NEAR_OPEN в market_session).
        st_en, st_min, st_pb = _game_5m_soft_take_near_high_params()
        phase = (session_phase or "").strip()
        if (
            st_en
            and not exit_only_take
            and phase == "NEAR_OPEN"
            and pullback_from_high_pct is not None
            and pnl_take_pct >= st_min
            and pnl_take_pct < take_threshold
        ):
            try:
                pb = float(pullback_from_high_pct)
            except (TypeError, ValueError):
                pb = 999.0
            if pb <= st_pb:
                logger.info(
                    "GAME_5M %s: мягкий тейк у хая (NEAR_OPEN) — pnl=%.2f%% ≥ %.2f%%, откат от high=%.3f%% ≤ %.2f%%",
                    ticker,
                    pnl_take_pct,
                    st_min,
                    pb,
                    st_pb,
                )
                return True, "TAKE_PROFIT", "soft_near_high_open"
        if not exit_only_take and _game_5m_stop_loss_enabled() and pnl_stop_pct <= -stop_pct:
            return True, "STOP_LOSS", ""

        # В последние N минут сессии: закрыть с минимальным профитом, чтобы не уходить через день в минус.
        # Исключение: если текущий сигнал STRONG_BUY — остаёмся (рискуем перейти в следующую сессию).
        try:
            exit_min = int(get_config_value("GAME_5M_SESSION_END_EXIT_MINUTES", "30"))
            min_profit = float(get_config_value("GAME_5M_SESSION_END_MIN_PROFIT_PCT", "0.3"))
        except (ValueError, TypeError):
            exit_min, min_profit = 30, 0.3
        if not exit_only_take and simulation_time is None and exit_min > 0 and min_profit >= 0 and current_decision != "STRONG_BUY":
            try:
                from services.market_session import get_market_session_context
                ctx = get_market_session_context()
                if ctx.get("session_phase") in ("REGULAR", "NEAR_CLOSE"):
                    mins_left = ctx.get("minutes_until_close")
                    if mins_left is not None and mins_left <= exit_min:
                        pnl_current_pct = (current_price - entry_price) / entry_price * 100.0
                        if pnl_current_pct >= min_profit:
                            logger.info(
                                "GAME_5M %s: конец сессии через %s мин, PnL=%.2f%% >= %.2f%% — выход TIME_EXIT",
                                ticker, mins_left, pnl_current_pct, min_profit,
                            )
                            return True, "TIME_EXIT", "session_end"
            except Exception as e:
                logger.debug("GAME_5M session-end exit check: %s", e)

    entry_ts = open_position.get("entry_ts")
    if entry_ts is not None:
        import pandas as pd

        ref = simulation_time if simulation_time is not None else datetime.now()
        et = pd.Timestamp(entry_ts)
        rt = pd.Timestamp(ref)
        if et.tzinfo is None:
            et = et.tz_localize(TRADE_HISTORY_TZ, ambiguous=True).tz_convert(CHART_DISPLAY_TZ)
        else:
            et = et.tz_convert(CHART_DISPLAY_TZ)
        if rt.tzinfo is None:
            rt = rt.tz_localize(CHART_DISPLAY_TZ, ambiguous=True)
        else:
            rt = rt.tz_convert(CHART_DISPLAY_TZ)
        age = rt - et
        if hasattr(age, "to_pytimedelta"):
            age = age.to_pytimedelta()
    else:
        age = timedelta(0)
    max_min = _max_position_minutes(open_position.get("ticker"))

    # Stale/reversal exit: explicit protection for old longs whose setup degraded.
    # It is checked before EXIT_ONLY_TAKE so that enabling this flag can override the broad legacy mode.
    stale_params = _game_5m_stale_reversal_exit_params(open_position.get("ticker"))
    if stale_params.get("enabled") and isinstance(entry_price, (int, float)) and entry_price > 0:
        pnl_current_pct = (current_price - entry_price) / entry_price * 100.0
        try:
            mom_current = None if momentum_2h_pct is None else float(momentum_2h_pct)
        except (TypeError, ValueError):
            mom_current = None
        weak_mom = mom_current is None or mom_current <= float(stale_params["momentum_below"])
        decision_norm = str(current_decision or "").strip().upper()
        weak_decision = decision_norm in ("HOLD", "SELL")
        min_age = timedelta(minutes=int(stale_params["min_age_minutes"]))
        if (
            age >= min_age
            and pnl_current_pct <= float(stale_params["max_pnl_pct"])
            and weak_mom
            and weak_decision
        ):
            logger.info(
                "GAME_5M %s: stale/reversal exit — age=%s мин, PnL=%.2f%% <= %.2f%%, "
                "mom2h=%s <= %.2f%%, decision=%s",
                open_position.get("ticker", "?"),
                int(age.total_seconds() // 60),
                pnl_current_pct,
                float(stale_params["max_pnl_pct"]),
                "—" if mom_current is None else f"{mom_current:+.2f}%",
                float(stale_params["momentum_below"]),
                decision_norm or "—",
            )
            return True, "TIME_EXIT_EARLY", "stale_reversal"

    if _game_5m_exit_only_take():
        return False, "", ""
    if max_min is not None and age > timedelta(minutes=max_min):
        return True, "TIME_EXIT", "max_hold_minutes"
    if age > timedelta(days=_max_position_days(open_position.get("ticker"))):
        return True, "TIME_EXIT", "max_hold_days"

    # Early de-risk: если позиция долго в просадке и импульс/сигнал не поддерживают — закрываем раньше TIME_EXIT.
    # По умолчанию выключено.
    try:
        dr_enabled = (get_config_value("GAME_5M_EARLY_DERISK_ENABLED", "false") or "false").strip().lower() in (
            "1",
            "true",
            "yes",
        )
    except Exception:
        dr_enabled = False
    if dr_enabled and isinstance(entry_price, (int, float)) and entry_price > 0:
        try:
            min_age_min = int((get_config_value("GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES", "180") or "180").strip())
        except (ValueError, TypeError):
            min_age_min = 180
        try:
            max_loss_pct = float((get_config_value("GAME_5M_EARLY_DERISK_MAX_LOSS_PCT", "-2.0") or "-2.0").strip())
        except (ValueError, TypeError):
            max_loss_pct = -2.0
        try:
            weak_mom_below = float((get_config_value("GAME_5M_EARLY_DERISK_MOMENTUM_BELOW", "0.0") or "0.0").strip())
        except (ValueError, TypeError):
            weak_mom_below = 0.0
        pnl_current_pct = (current_price - entry_price) / entry_price * 100.0
        weak_mom = momentum_2h_pct is None or float(momentum_2h_pct) <= weak_mom_below
        weak_decision = current_decision in ("HOLD", "SELL")
        if age >= timedelta(minutes=max(15, min_age_min)) and pnl_current_pct <= max_loss_pct and weak_mom and weak_decision:
            logger.info(
                "GAME_5M %s: early de-risk — age=%s мин, PnL=%.2f%% <= %.2f%%, mom2h=%s, decision=%s",
                open_position.get("ticker", "?"),
                int(age.total_seconds() // 60),
                pnl_current_pct,
                max_loss_pct,
                "—" if momentum_2h_pct is None else f"{float(momentum_2h_pct):+.2f}%",
                current_decision,
            )
            return True, "TIME_EXIT_EARLY", "early_derisk"
    # Важное правило твоего сценария:
    # SELL используем только как рекомендацию (в момент входа), но НЕ как причину выхода
    # для уже открытой позиции. Автозакрытие происходит только по TAKE_PROFIT / TIME_EXIT
    # (и STOP_LOSS, если он включён).
    if current_decision == "SELL":
        return False, "", ""
    return False, "", ""
