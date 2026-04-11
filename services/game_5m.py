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
from typing import Any, Optional

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


def record_entry(
    ticker: str,
    price: float,
    signal_type: str,
    reasoning: Optional[str] = None,
    *,
    entry_context: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """Фиксирует бумажный вход: INSERT BUY в trade_history (strategy_name=GAME_5M).
    entry_context: контекст на момент входа (momentum_2h_pct и др.) — сохраняется в context_json для /closed_impulse (импульс при решении об открытии)."""
    if price <= 0:
        logger.warning("game_5m: record_entry %s с ценой <= 0, пропуск", ticker)
        return None

    quantity = max(1, int(GAME_NOTIONAL_USD / price))
    notional = quantity * price
    commission = notional * COMMISSION_RATE
    import json as _json
    context_str = _json.dumps(entry_context) if entry_context else None
    engine = _engine()
    with engine.begin() as conn:
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


def close_position(
    ticker: str,
    exit_price: float,
    exit_signal_type: str,
    position: Optional[dict[str, Any]] = None,
    *,
    bar_high: Optional[float] = None,
    bar_low: Optional[float] = None,
    context_json: Optional[dict[str, Any]] = None,
) -> Optional[float]:
    """Закрывает открытую позицию: INSERT SELL. Возвращает PnL в %.
    position — если передан (например от get_open_position_any), SELL пишется с strategy_name из позиции;
    иначе берётся позиция только GAME_5M.
    bar_high/bar_low — опционально: при TAKE_PROFIT цена не выше bar_high, при STOP_LOSS не ниже bar_low.
    Если не заданы — применяется ограничение по entry (макс. +15% тейк / −15% стоп), чтобы в БД не попали нереальные цифры из глючных котировок."""
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
    if exit_signal_type == "TAKE_PROFIT":
        if bar_high is not None and bar_high > 0:
            exit_price = min(exit_price, bar_high)
        else:
            cap = entry_price * 1.15
            if exit_price > cap:
                logger.warning(
                    "game_5m: %s TAKE_PROFIT exit_price=%.2f выше разумного (entry=%.2f, cap +15%%=%.2f) — записываем %.2f",
                    ticker, original_exit, entry_price, cap, cap,
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
    engine = _engine()
    with engine.begin() as conn:
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
    Возвращает список dict: ts, price, quantity, side ('BUY'|'SELL'), signal_type, ts_timezone (если есть в таблице)."""
    dt_lo, dt_hi = _chart_range_et_to_msk(dt_min, dt_max, margin_days=1)
    engine = _engine()
    params = {"ticker": ticker, "strategy": GAME_5M_STRATEGY, "dt_min": dt_lo, "dt_max": dt_hi}
    with engine.connect() as conn:
        try:
            rows = conn.execute(
                text("""
                    SELECT id, ts, side, price, quantity, signal_type, ts_timezone
                    FROM public.trade_history
                    WHERE ticker = :ticker AND strategy_name = :strategy
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
                    "ts_timezone": r[6],
                }
                for r in rows
            ]
        except Exception:
            rows = conn.execute(
                text("""
                    SELECT id, ts, side, price, quantity, signal_type
                    FROM public.trade_history
                    WHERE ticker = :ticker AND strategy_name = :strategy
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
    # Не фильтруем по ET здесь: диапазон уже переведён в MSK с запасом, отрисовка по сессиям сама отсечёт лишнее
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


def _take_profit_cap_pct(ticker: Optional[str] = None) -> float:
    """Потолок тейка (%): сначала подсказка из suggested_5m_params (если включено), затем GAME_5M_TAKE_PROFIT_PCT_<TICKER>, иначе общий."""
    suggested = _get_suggested_5m_params()
    if ticker and suggested:
        take_map = suggested.get("take_pct") or {}
        v = take_map.get(ticker.upper()) or take_map.get(ticker)
        if v is not None:
            try:
                return max(2.0, min(10.0, float(v)))
            except (ValueError, TypeError):
                pass
    if ticker:
        key = f"GAME_5M_TAKE_PROFIT_PCT_{ticker.upper()}"
        raw = get_config_value(key, "").strip()
        if raw:
            try:
                return float(raw)
            except (ValueError, TypeError):
                pass
    params = get_strategy_params()
    return params["take_profit_pct"]


def _effective_take_profit_pct(
    momentum_2h_pct: Optional[float],
    ticker: Optional[str] = None,
) -> float:
    """Тейк-профит по импульсу 2ч или из конфига.

    Формула:
    - Если импульс_2ч >= GAME_5M_TAKE_PROFIT_MIN_PCT (4%): тейк = min(импульс_2ч × GAME_5M_TAKE_MOMENTUM_FACTOR, потолок).
    - Иначе: тейк = потолок (GAME_5M_TAKE_PROFIT_PCT или GAME_5M_TAKE_PROFIT_PCT_<TICKER>).
    Потолок не даёт тейку превысить заданный %; при импульсе 6% и factor=1 тейк = 6% (поэтому сработало на 6%, а не 7%).
    """
    cap = _take_profit_cap_pct(ticker)
    try:
        min_take = float(get_config_value("GAME_5M_TAKE_PROFIT_MIN_PCT", "2.0"))
    except (ValueError, TypeError):
        min_take = 2.0
    try:
        momentum_factor = float(get_config_value("GAME_5M_TAKE_MOMENTUM_FACTOR", "1.0"))
        momentum_factor = max(0.3, min(1.0, momentum_factor))
    except (ValueError, TypeError):
        momentum_factor = 1.0
    if momentum_2h_pct is not None and momentum_2h_pct >= min_take:
        effective_momentum = float(momentum_2h_pct) * momentum_factor
        return min(effective_momentum, cap)
    return cap


def _effective_stop_loss_pct(
    momentum_2h_pct: Optional[float],
    ticker: Optional[str] = None,
) -> float:
    """Стоп-лосс: меньше тейка, прогнозируется как доля от эффективного тейка (тейк от импульса 2ч)."""
    params = get_strategy_params()
    config_stop = params["stop_loss_pct"]
    take_pct = _effective_take_profit_pct(momentum_2h_pct, ticker=ticker)
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


def should_close_position(
    open_position: dict,
    current_decision: str,
    current_price: Optional[float],
    momentum_2h_pct: Optional[float] = None,
    bar_high: Optional[float] = None,
    bar_low: Optional[float] = None,
    rsi_5m: Optional[float] = None,
) -> tuple[bool, str]:
    """Закрывать ли позицию: по тейку/стопу (цена), по сигналу SELL, по истечении GAME_5M_MAX_POSITION_DAYS,
    либо в последние N минут сессии с минимальным профитом (TIME_EXIT). При сигнале STRONG_BUY выход в конец сессии не делаем — остаёмся в позиции.
    Тейк считается от импульса 2ч (как у модели), если он >= GAME_5M_TAKE_PROFIT_MIN_PCT, иначе из конфига.
    bar_high/bar_low — макс. High / мин. Low по последним свечам (до 30 мин): не пропускаем фазу подъёма
    при запуске крона каждые 5 мин (отскок в начале сессии и т.п.)."""
    if current_price is None or current_price <= 0:
        return False, ""

    entry_price = open_position.get("entry_price")
    if isinstance(entry_price, (int, float)) and entry_price > 0:
        tkr = open_position.get("ticker")
        take_pct = _effective_take_profit_pct(momentum_2h_pct, ticker=tkr)
        stop_pct = _effective_stop_loss_pct(momentum_2h_pct, ticker=tkr)
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
            return True, "TAKE_PROFIT"
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
        if not exit_only_take and _game_5m_stop_loss_enabled() and pnl_stop_pct <= -stop_pct:
            return True, "STOP_LOSS"
        if exit_only_take:
            return False, ""

        # В последние N минут сессии: закрыть с минимальным профитом, чтобы не уходить через день в минус.
        # Исключение: если текущий сигнал STRONG_BUY — остаёмся (рискуем перейти в следующую сессию).
        try:
            exit_min = int(get_config_value("GAME_5M_SESSION_END_EXIT_MINUTES", "30"))
            min_profit = float(get_config_value("GAME_5M_SESSION_END_MIN_PROFIT_PCT", "0.3"))
        except (ValueError, TypeError):
            exit_min, min_profit = 30, 0.3
        if exit_min > 0 and min_profit >= 0 and current_decision != "STRONG_BUY":
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
                            return True, "TIME_EXIT"
            except Exception as e:
                logger.debug("GAME_5M session-end exit check: %s", e)

    entry_ts = open_position.get("entry_ts")
    if isinstance(entry_ts, datetime):
        age = datetime.now() - entry_ts
    else:
        age = timedelta(0)
    max_min = _max_position_minutes(open_position.get("ticker"))
    if _game_5m_exit_only_take():
        return False, ""
    if max_min is not None and age > timedelta(minutes=max_min):
        return True, "TIME_EXIT"
    if age > timedelta(days=_max_position_days(open_position.get("ticker"))):
        return True, "TIME_EXIT"

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
            return True, "TIME_EXIT_EARLY"
    # Важное правило твоего сценария:
    # SELL используем только как рекомендацию (в момент входа), но НЕ как причину выхода
    # для уже открытой позиции. Автозакрытие происходит только по TAKE_PROFIT / TIME_EXIT
    # (и STOP_LOSS, если он включён).
    if current_decision == "SELL":
        return False, ""
    return False, ""
