"""
Специальный бэктест для Алекса:

Правило входа для SNDK в феврале 2026:
    - Если VIX < 20
    - И после дня падения цена пробивает максимум предыдущего дня
      (ввиду отсутствия high в БД упрощаем до:
         вчера был «красный» день (close_{t-1} < close_{t-2}),
         а сегодня close_t > close_{t-1})
    → открываем BUY.

Выход:
    - либо стоп-лосс 5% (лог-доходность <= ln(0.95)),
    - либо по истечении 10 торговых дней после входа.

Параметры:
    - Начальный капитал: 100 000 USD
    - Размер позиции: 10% от доступного кэша на момент входа
    - Комиссия: 0.1% от объёма сделки (как в ExecutionAgent)

Результат:
    - печатает итоговый PnL и базовую статистику «для Алекса».
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import floor
from typing import Optional

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from config_loader import get_database_url


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


INITIAL_CASH_USD = 100_000.0
COMMISSION_RATE = 0.001  # 0.1%
STOP_LOSS_LEVEL = 0.95   # 5% падение
MAX_HOLDING_DAYS = 10    # 10 торговых дней


@dataclass
class Position:
    ticker: str
    entry_date: datetime
    entry_price: float
    quantity: float


def load_quotes_for_period(
    engine,
    ticker: str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Загружает котировки one‑ticker из quotes (date, close)."""
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                """
                SELECT date, close
                FROM quotes
                WHERE ticker = :ticker
                  AND date >= :start
                  AND date <= :end
                ORDER BY date ASC
                """
            ),
            conn,
            params={"ticker": ticker, "start": start, "end": end},
        )

    if df.empty:
        raise RuntimeError(f"Нет котировок для {ticker} в периоде {start} – {end}")

    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df


def run_backtest_february_2026() -> None:
    """Запускает бэктест по правилу Алекса для SNDK в феврале 2026."""
    db_url = get_database_url()
    engine = create_engine(db_url)

    # Берём расширенное окно, чтобы:
    # - иметь предыдущее значение перед 1 февраля,
    # - корректно отработать 10‑дневный выход за пределы февраля.
    feb_start = datetime(2026, 2, 1)
    feb_end = datetime(2026, 2, 28, 23, 59, 59)
    load_start = feb_start - timedelta(days=10)
    load_end = feb_end + timedelta(days=15)

    logger.info("📥 Загрузка котировок SNDK и ^VIX для бэктеста февраля 2026")
    sndk = load_quotes_for_period(engine, "SNDK", load_start, load_end)
    vix = load_quotes_for_period(engine, "^VIX", load_start, load_end)

    # Выравниваем даты VIX к датам SNDK (используем последнее доступное значение VIX)
    vix_aligned = vix.reindex(sndk.index, method="ffill")

    cash = INITIAL_CASH_USD
    position: Optional[Position] = None
    equity_curve = []

    # Для определения "падения" нам нужно как минимум два предыдущих дня
    dates = sndk.index.to_list()

    logger.info("🚀 Старт бэктеста SNDK по правилу Алекса на февраль 2026")
    for i, current_date in enumerate(dates):
        price_t = float(sndk.loc[current_date, "close"])
        vix_t = float(vix_aligned.loc[current_date, "close"])

        # Текущая стоимость портфеля (для записи equity)
        if position is not None:
            equity = cash + position.quantity * price_t
        else:
            equity = cash
        equity_curve.append((current_date, equity))

        # Обработка только дат внутри февраля 2025 по правилам входа/выхода
        in_february = feb_start <= current_date <= feb_end

        # --- Управление открытой позицией ---
        if position is not None:
            # Проверка стоп‑лосса по лог‑доходности
            log_ret = float(np.log(price_t / position.entry_price))
            stop_log_threshold = float(np.log(STOP_LOSS_LEVEL))

            holding_days = sum(
                1
                for d in dates
                if position.entry_date < d <= current_date
            )

            exit_reason = None
            if log_ret <= stop_log_threshold:
                exit_reason = f"STOP (log_ret={log_ret:.4f})"
            elif holding_days >= MAX_HOLDING_DAYS and in_february:
                # Закрываем по времени только если всё ещё в рамках "основного" окна;
                # выход может случиться и после февраля, т.к. мы смотрим holding_days
                exit_reason = f"TIME (holding_days={holding_days})"

            if exit_reason:
                notional = position.quantity * price_t
                commission = notional * COMMISSION_RATE
                proceeds = notional - commission

                cash += proceeds

                logger.info(
                    "🔴 EXIT %s on %s @ %.2f | reason=%s | qty=%.0f | PnL=%.2f (log_ret=%.4f)",
                    position.ticker,
                    current_date.date(),
                    price_t,
                    exit_reason,
                    position.quantity,
                    proceeds - position.quantity * position.entry_price,
                    log_ret,
                )

                position = None
                # после выхода не открываем новую позицию в тот же день — переходим к следующей дате
                continue

        # --- Входы только внутри февраля ---
        if not in_february:
            continue

        # Если позиция уже открыта — новых входов не делаем
        if position is not None:
            continue

        # Для проверки условия "после падения" и "пробой вчерашнего High"
        if i < 2:
            continue  # не хватает истории

        # Вчерашний и позавчерашний закрытия
        date_t1 = dates[i - 1]
        date_t2 = dates[i - 2]
        close_t1 = float(sndk.loc[date_t1, "close"])
        close_t2 = float(sndk.loc[date_t2, "close"])

        # Условие "падения вчера": close_{t-1} < close_{t-2}
        was_drop_yesterday = close_t1 < close_t2

        # Условие "пробой вчерашнего High":
        # В БД нет High, поэтому используем упрощение: сегодняшнее закрытие выше вчерашнего close.
        breakout_today = price_t > close_t1

        # Новое правило: VIX < 20 + breakout после падения
        if vix_t < 20 and was_drop_yesterday and breakout_today:
            # Размер позиции: 10% от доступного кэша
            allocation = cash * 0.10
            if allocation <= 0:
                logger.info(
                    "⚠️ Нет свободного кэша для входа %s на %s",
                    "SNDK",
                    current_date.date(),
                )
                continue

            quantity = floor(allocation / price_t)
            if quantity <= 0:
                logger.info(
                    "⚠️ Слишком маленькая аллокация для входа %s на %s (allocation=%.2f, price=%.2f)",
                    "SNDK",
                    current_date.date(),
                    allocation,
                    price_t,
                )
                continue

            notional = quantity * price_t
            commission = notional * COMMISSION_RATE
            total_cost = notional + commission

            if total_cost > cash:
                logger.info(
                    "⚠️ Недостаточно кэша для входа %s на %s (cash=%.2f, required=%.2f)",
                    "SNDK",
                    current_date.date(),
                    cash,
                    total_cost,
                )
                continue

            cash -= total_cost
            position = Position(
                ticker="SNDK",
                entry_date=current_date,
                entry_price=price_t,
                quantity=float(quantity),
            )

            logger.info(
                "🟢 ENTRY SNDK on %s @ %.2f | qty=%.0f | notional=%.2f | fee=%.2f | VIX=%.2f "
                "(VIX<20 & breakout after drop)",
                current_date.date(),
                price_t,
                quantity,
                notional,
                commission,
                vix_t,
            )

    # Если после цикла позиция осталась открытой — закрываем её по последней доступной цене
    if position is not None:
        last_price = float(sndk.iloc[-1]["close"])
        notional = position.quantity * last_price
        commission = notional * COMMISSION_RATE
        proceeds = notional - commission
        cash += proceeds
        log_ret = float(np.log(last_price / position.entry_price))

        logger.info(
            "🔴 FINAL EXIT %s on %s @ %.2f | qty=%.0f | PnL=%.2f (log_ret=%.4f)",
            position.ticker,
            sndk.index[-1].date(),
            last_price,
            position.quantity,
            proceeds - position.quantity * position.entry_price,
            log_ret,
        )
        position = None

    # Итоговая статистика
    initial = INITIAL_CASH_USD
    final = cash
    total_pnl = final - initial
    pnl_pct = (total_pnl / initial) * 100 if initial > 0 else 0.0

    logger.info("\n===== Результаты бэктеста для Алекса (SNDK, февраль 2026) =====")
    logger.info("Начальный капитал: %.2f USD", initial)
    logger.info("Финальный капитал: %.2f USD", final)
    logger.info("Итоговый PnL: %.2f USD (%.2f%%)", total_pnl, pnl_pct)

    print("\n📊 Итог для Алекса:")
    print(f"  Начальный капитал: {initial:,.2f} USD")
    print(f"  Финальный капитал: {final:,.2f} USD")
    print(f"  Итоговый PnL: {total_pnl:,.2f} USD ({pnl_pct:.2f}%)")


if __name__ == "__main__":
    run_backtest_february_2026()

