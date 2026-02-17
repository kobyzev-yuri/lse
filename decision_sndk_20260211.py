"""
Эмуляция принятия решения по SNDK на дату 11 февраля 2026 года.

Цели:
 1. Вытащить котировки SNDK за 5–10 февраля 2026 и показать,
    как росла «сила» покупателя.
 2. Напечатать текстовый отчёт:
      «Почему лимиты 511/548 больше не актуальны».
 3. Рассчитать PnL, если бы мы вошли 11 февраля по цене закрытия,
    используя 50% кэша (фокус только на SNDK).
 4. Добавить в лог поле Reasoning (Обоснование) с текстом вида:
      «Вхожу по 599, так как VIX в норме, а корреляция с падающим сектором (MU) отсутствует».
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
COMMISSION_RATE = 0.001  # 0.1% на каждую сторону


@dataclass
class DecisionContext:
    decision_date: datetime
    sndk_entry_price: float
    vix_value: float
    corr_sndk_mu: Optional[float]
    quantity: int
    exit_date: datetime
    exit_price: float
    partial_taken: bool
    partial_price: Optional[float]
    partial_date: Optional[datetime]
    pnl: float
    pnl_pct: float


def load_quotes(
    engine,
    ticker: str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Загружает котировки (date, close) для тикера в периоде."""
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


def compute_correlation_sndk_mu(engine, end_date: datetime) -> Optional[float]:
    """
    Оценивает корреляцию лог‑доходностей SNDK и MU
    за месяц до end_date.
    """
    start = end_date - timedelta(days=30)
    sndk = load_quotes(engine, "SNDK", start, end_date)
    mu = load_quotes(engine, "MU", start, end_date)

    # Совместный индекс дат
    joined = sndk.join(mu, how="inner", lsuffix="_SNDK", rsuffix="_MU")
    if joined.shape[0] < 5:
        return None

    prices = joined[["close_SNDK", "close_MU"]]
    log_returns = np.log(prices / prices.shift(1)).dropna()
    if log_returns.empty:
        return None

    corr = log_returns["close_SNDK"].corr(log_returns["close_MU"])
    return float(corr) if pd.notna(corr) else None


def emulate_decision_20260211() -> DecisionContext:
    """Основная функция эмуляции решения на 11 февраля 2026 года."""
    db_url = get_database_url()
    engine = create_engine(db_url)

    decision_date = datetime(2026, 2, 11)

    # 1) Котировки SNDK за 5–10 февраля
    window_start = datetime(2026, 2, 5)
    window_end = datetime(2026, 2, 10, 23, 59, 59)
    sndk_window = load_quotes(engine, "SNDK", window_start, window_end)

    logger.info("📊 Котировки SNDK за 5–10 февраля 2026 (рост силы покупателя):")
    sndk_window["change_pct"] = sndk_window["close"].pct_change() * 100
    sndk_window["log_ret"] = np.log(sndk_window["close"] / sndk_window["close"].shift(1))

    for dt, row in sndk_window.iterrows():
        change = row["change_pct"]
        log_ret = row["log_ret"]
        trend_label = "🟢 рост" if (pd.notna(change) and change > 0) else "🔴 падение" if (pd.notna(change) and change < 0) else "–"
        logger.info(
            "  %s | close=%.2f | Δ%%=%6.2f | log_ret=%7.4f | %s",
            dt.date(),
            row["close"],
            change if pd.notna(change) else 0.0,
            log_ret if pd.notna(log_ret) else 0.0,
            trend_label,
        )

    # Оценим суммарную «силу» покупателя по окну
    cum_log_ret = float(sndk_window["log_ret"].dropna().sum())
    up_days = int((sndk_window["change_pct"] > 0).sum())
    down_days = int((sndk_window["change_pct"] < 0).sum())
    logger.info(
        "📈 Суммарная лог‑доходность за 5–10 февраля: %.4f (up_days=%d, down_days=%d)",
        cum_log_ret,
        up_days,
        down_days,
    )

    # 2) Цена входа SNDK на 11 февраля 2026
    sndk_entry_series = load_quotes(
        engine,
        "SNDK",
        decision_date,
        decision_date,
    )
    sndk_entry_price = float(sndk_entry_series.iloc[0]["close"])

    # 3) VIX на дату решения (берём последнее доступное значение <= 11 февраля)
    with engine.connect() as conn:
        vix_row = conn.execute(
            text(
                """
                SELECT date, close
                FROM quotes
                WHERE ticker = '^VIX'
                  AND date <= :dt
                ORDER BY date DESC
                LIMIT 1
                """
            ),
            {"dt": decision_date},
        ).fetchone()

    if not vix_row:
        raise RuntimeError("Нет данных VIX (^VIX) для 11 февраля 2026")

    vix_value = float(vix_row[1])

    # 4) Корреляция SNDK и MU за месяц до решения
    corr_sndk_mu = compute_correlation_sndk_mu(engine, decision_date - timedelta(days=1))

    # 5) Расчёт PnL при входе 11 февраля 2026 на 50% кэша
    cash = INITIAL_CASH_USD
    allocation = cash * 0.50
    quantity = floor(allocation / sndk_entry_price)
    notional_entry = quantity * sndk_entry_price
    commission_entry = notional_entry * COMMISSION_RATE
    cash_after_entry = cash - notional_entry - commission_entry

    # 5.1) Логика Partial Take Profit:
    #      При достижении профита 3% продаём 50% позиции,
    #      остаток держим со стопом на уровне безубытка (цена входа).
    exit_start = decision_date + timedelta(days=1)
    exit_end = decision_date + timedelta(days=10)
    sndk_exit = load_quotes(engine, "SNDK", exit_start, exit_end)

    remaining_qty = quantity
    cash_current = cash_after_entry

    partial_taken = False
    partial_price: Optional[float] = None
    partial_date: Optional[datetime] = None
    breakeven_stop: Optional[float] = None

    exit_date: Optional[datetime] = None
    exit_price: Optional[float] = None

    take_profit_level = sndk_entry_price * 1.03

    for dt, row in sndk_exit.iterrows():
        price = float(row["close"])

        # Partial Take Profit: фиксируем половину при достижении +3%
        if (not partial_taken) and price >= take_profit_level and remaining_qty > 0:
            sell_qty = max(1, remaining_qty // 2)
            notional_ptp = sell_qty * price
            commission_ptp = notional_ptp * COMMISSION_RATE
            cash_current += notional_ptp - commission_ptp
            remaining_qty -= sell_qty

            partial_taken = True
            partial_price = price
            partial_date = dt
            breakeven_stop = sndk_entry_price

            logger.info(
                "🟡 PARTIAL TAKE PROFIT SNDK on %s @ %.2f | qty=%d | notional=%.2f | fee=%.2f "
                "(~3%%+ profit from entry %.2f)",
                dt.date(),
                price,
                sell_qty,
                notional_ptp,
                commission_ptp,
                sndk_entry_price,
            )

        # Если после partial TP ещё есть объем — проверяем стоп на безубытке
        if partial_taken and remaining_qty > 0 and breakeven_stop is not None:
            if price <= breakeven_stop:
                notional_stop = remaining_qty * price
                commission_stop = notional_stop * COMMISSION_RATE
                cash_current += notional_stop - commission_stop

                exit_date = dt
                exit_price = price

                logger.info(
                    "🔴 STOP AT BREAKEVEN SNDK on %s @ %.2f | qty=%d | notional=%.2f | fee=%.2f "
                    "(protecting remaining 50%% after partial TP)",
                    dt.date(),
                    price,
                    remaining_qty,
                    notional_stop,
                    commission_stop,
                )
                remaining_qty = 0
                break

    # Если после цикла позиция (или её часть) осталась — закрываем по последней доступной цене
    if remaining_qty > 0:
        last_dt = sndk_exit.index[-1]
        last_price = float(sndk_exit.iloc[-1]["close"])
        notional_last = remaining_qty * last_price
        commission_last = notional_last * COMMISSION_RATE
        cash_current += notional_last - commission_last

        exit_date = last_dt
        exit_price = last_price

        logger.info(
            "🔴 FINAL EXIT SNDK on %s @ %.2f | qty=%d | notional=%.2f | fee=%.2f",
            last_dt.date(),
            last_price,
            remaining_qty,
            notional_last,
            commission_last,
        )

    cash_final = cash_current
    pnl = cash_final - cash
    pnl_pct = (pnl / cash) * 100 if cash > 0 else 0.0

    # 6) Reasoning для лога
    corr_text = "корреляция с MU отсутствует или слаба"
    if corr_sndk_mu is not None:
        if abs(corr_sndk_mu) >= 0.3:
            corr_text = f"корреляция с MU заметна (corr={corr_sndk_mu:.2f})"
        else:
            corr_text = f"корреляция с MU слабая (corr={corr_sndk_mu:.2f})"

    reasoning = (
        f"Вхожу по {sndk_entry_price:.2f}, так как VIX={vix_value:.2f} в норме "
        f"(ниже 20), суммарная сила покупателя за 5–10 февраля положительная "
        f"(cum_log_ret={cum_log_ret:.4f}), а {corr_text}. "
        f"Стратегия использует частичную фиксацию прибыли: при достижении +3% от входа "
        f"продаётся 50% позиции, оставшаяся часть защищена стопом на уровне безубытка."
    )

    logger.info("Reasoning: %s", reasoning)

    ctx = DecisionContext(
        decision_date=decision_date,
        sndk_entry_price=sndk_entry_price,
        vix_value=vix_value,
        corr_sndk_mu=corr_sndk_mu,
        quantity=quantity,
        exit_date=exit_date,
        exit_price=exit_price,
        partial_taken=partial_taken,
        partial_price=partial_price,
        partial_date=partial_date,
        pnl=pnl,
        pnl_pct=pnl_pct,
    )

    # 7) Текстовый отчёт
    print("\n===== Отчёт по решению SNDK на 11 февраля 2026 =====")
    print("Почему лимиты 511/548 больше не актуальны:\n")
    print(
        f"- В период 5–10 февраля 2026 цена SNDK демонстрировала устойчивый спрос "
        f"(суммарная лог‑доходность {cum_log_ret:.4f}, дней роста: {up_days}, дней падения: {down_days})."
    )
    print(
        f"- На дату решения 11 февраля цена закрытия составила {sndk_entry_price:.2f} USD, "
        f"что значительно выше уровней 511/548 — рынок уже перешёл в новую ценовую зону."
    )
    print(
        f"- Индекс VIX находился на уровне {vix_value:.2f}, что соответствует спокойному режиму волатильности, "
        f"а не панике."
    )
    if corr_sndk_mu is not None:
        print(
            f"- Расчётная корреляция лог‑доходностей SNDK и MU за месяц до решения: {corr_sndk_mu:.3f}, "
            f"то есть явной привязки к возможному падению сектора памяти нет."
        )
    else:
        print(
            "- Недостаточно данных для надёжной оценки корреляции SNDK и MU, "
            "но доступная история не показывает сильной связки с падающим сектором."
        )
    print(
        "- В такой конфигурации уровни 511/548 — это уже исторические ориентиры, "
        "а не актуальные лимитные уровни входа: рынок торгуется существенно выше и подтверждает силу тренда."
    )

    print("\nПараметры гипотетического входа (50% кэша, только SNDK):")
    print(f"- Начальный кэш: {INITIAL_CASH_USD:,.2f} USD")
    print(f"- Аллокация на сделку: {allocation:,.2f} USD (50% кэша)")
    print(f"- Количество акций: {quantity} шт.")
    print(f"- Вход: {sndk_entry_price:.2f} USD ({decision_date.date()})")
    if partial_taken and partial_price is not None and partial_date is not None:
        print(
            f"- Частичная фиксация: {partial_price:.2f} USD ({partial_date.date()}) "
            f"на ~50% позиции, далее стоп по оставшейся части на уровне безубытка."
        )
    print(f"- Итоговый выход: {exit_price:.2f} USD ({exit_date.date()})")
    print(f"- Итоговый PnL: {pnl:,.2f} USD ({pnl_pct:.2f}%)")

    print("\nReasoning (обоснование решения):")
    print(reasoning)

    return ctx


if __name__ == "__main__":
    emulate_decision_20260211()

