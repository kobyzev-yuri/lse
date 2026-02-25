import logging
from dataclasses import dataclass
from typing import Optional, Dict, List

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from config_loader import get_database_url


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class TradePnL:
    trade_id: int
    ticker: str
    ts: pd.Timestamp
    side: str
    quantity: float
    entry_price: float
    exit_price: float
    gross_pnl: float
    net_pnl: float
    log_return: float
    commission: float
    signal_type: str
    sentiment_at_trade: Optional[float]


def get_engine():
    db_url = get_database_url()
    return create_engine(db_url)


def load_trade_history(engine, strategy_name: Optional[str] = None) -> pd.DataFrame:
    """Загружает историю сделок. strategy_name — опциональный фильтр (например 'GAME_5M')."""
    query = """
        SELECT id, ts, ticker, side, quantity, price,
               commission, signal_type, total_value, sentiment_at_trade, strategy_name
        FROM public.trade_history
    """
    if strategy_name:
        query += " WHERE strategy_name = :strategy_name"
    query += " ORDER BY ts ASC, id ASC"
    with engine.connect() as conn:
        if strategy_name:
            df = pd.read_sql(text(query), conn, params={"strategy_name": strategy_name})
        else:
            df = pd.read_sql(text(query), conn)
    return df


def compute_closed_trade_pnls(trades: pd.DataFrame) -> List[TradePnL]:
    """
    Строим PnL по каждой закрытой сделке.
    Используем модель средневзвешенной цены входа и лог-доходности.
    """
    results: List[TradePnL] = []

    if trades.empty:
        return results

    # Убедимся в правильных типах
    trades = trades.copy()
    trades["quantity"] = trades["quantity"].astype(float)
    trades["price"] = trades["price"].astype(float)
    trades["commission"] = trades["commission"].astype(float)

    # Состояние по тикерам
    position_qty: Dict[str, float] = {}
    position_cost: Dict[str, float] = {}  # суммарный cost basis (включая комиссии)

    for _, row in trades.iterrows():
        ticker = row["ticker"]
        side = row["side"].upper()
        qty = float(row["quantity"])
        price = float(row["price"])
        commission = float(row["commission"]) if row["commission"] is not None else 0.0
        ts = row["ts"]
        trade_id = int(row["id"])
        signal_type = row.get("signal_type") or ""
        sentiment = (
            float(row["sentiment_at_trade"])
            if row["sentiment_at_trade"] is not None
            else None
        )

        if ticker not in position_qty:
            position_qty[ticker] = 0.0
            position_cost[ticker] = 0.0

        if side == "BUY":
            # Покупка: увеличиваем позицию и cost basis
            position_qty[ticker] += qty
            position_cost[ticker] += qty * price + commission
        elif side == "SELL":
            if position_qty[ticker] <= 0:
                # Нет позиции — считаем PnL неизвестным, но фиксируем сделку
                logger.warning(
                    "⚠️ Продажа без позиции: %s, qty=%.2f, price=%.2f", ticker, qty, price
                )
                continue

            # Средняя цена входа по проданным лотам
            avg_entry = position_cost[ticker] / position_qty[ticker]
            cost_for_sold = avg_entry * qty

            proceeds = qty * price - commission

            gross_pnl = qty * (price - avg_entry)
            net_pnl = proceeds - cost_for_sold

            # Лог-доходность по проданной части
            try:
                log_ret = float(np.log(price / avg_entry))
            except Exception:
                log_ret = 0.0

            # Обновляем состояние позиции
            position_qty[ticker] -= qty
            position_cost[ticker] -= cost_for_sold

            results.append(
                TradePnL(
                    trade_id=trade_id,
                    ticker=ticker,
                    ts=pd.to_datetime(ts),
                    side=side,
                    quantity=qty,
                    entry_price=avg_entry,
                    exit_price=price,
                    gross_pnl=gross_pnl,
                    net_pnl=net_pnl,
                    log_return=log_ret,
                    commission=commission,
                    signal_type=signal_type,
                    sentiment_at_trade=sentiment,
                )
            )

    return results


def compute_win_rate(trade_pnls: List[TradePnL]) -> float:
    if not trade_pnls:
        return 0.0
    wins = sum(1 for t in trade_pnls if t.net_pnl > 0)
    return wins / len(trade_pnls)


def load_quotes(engine, tickers: List[str]) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame()
    with engine.connect() as conn:
        placeholders = ", ".join([f":t{i}" for i in range(len(tickers))])
        params = {f"t{i}": t for i, t in enumerate(tickers)}
        df = pd.read_sql(
            text(
                f"""
                SELECT date, ticker, close
                FROM quotes
                WHERE ticker IN ({placeholders})
                ORDER BY date ASC
                """
            ),
            conn,
            params=params,
        )
    return df


def compute_correlation_impact(engine, trade_pnls: List[TradePnL]) -> None:
    """
    Простая оценка impact:
    - корреляция между log-рендами тикеров и GBPUSD=X
    - влияние макро новостей (MACRO) на распределение PnL.
    """
    if not trade_pnls:
        logger.info("ℹ️ Нет закрытых сделок для анализа корреляций")
        return

    tickers = sorted({t.ticker for t in trade_pnls})
    all_tickers = tickers + ["GBPUSD=X"]

    quotes = load_quotes(engine, all_tickers)
    if quotes.empty:
        logger.info("ℹ️ Нет котировок для анализа корреляций")
        return

    # Пивот: дата x тикер
    prices = quotes.pivot_table(
        index="date", columns="ticker", values="close"
    ).sort_index()

    # Лог-ренды
    log_returns = np.log(prices / prices.shift(1)).dropna(how="all")

    if "GBPUSD=X" not in log_returns.columns:
        logger.info("ℹ️ Нет данных по GBPUSD=X для анализа FX impact")
    else:
        logger.info("\n📈 Корреляция лог-доходностей с GBPUSD=X:")
        gbp_ret = log_returns["GBPUSD=X"]
        for t in tickers:
            if t in log_returns.columns:
                corr = gbp_ret.corr(log_returns[t])
                logger.info("   Corr(%s, GBPUSD=X) = %.3f", t, corr)

    # Анализ макро-новостей: средний PnL при высоком и низком sentiment
    df_pnl = pd.DataFrame([t.__dict__ for t in trade_pnls])
    if "sentiment_at_trade" in df_pnl.columns:
        high = df_pnl[df_pnl["sentiment_at_trade"] > 0.5]
        low = df_pnl[df_pnl["sentiment_at_trade"] <= 0.5]
        if not high.empty:
            logger.info(
                "📊 Средний PnL при sentiment > 0.5: %.2f",
                high["net_pnl"].mean(),
            )
        if not low.empty:
            logger.info(
                "📊 Средний PnL при sentiment <= 0.5: %.2f",
                low["net_pnl"].mean(),
            )


def main():
    engine = get_engine()
    trades = load_trade_history(engine)

    if trades.empty:
        logger.info("ℹ️ В trade_history ещё нет сделок")
        return

    trade_pnls = compute_closed_trade_pnls(trades)

    if not trade_pnls:
        logger.info("ℹ️ Пока нет закрытых сделок (SELL), PnL не рассчитан")
        return

    # PnL по сделкам
    df_pnl = pd.DataFrame([t.__dict__ for t in trade_pnls])
    logger.info("\n===== PnL по закрытым сделкам =====")
    for _, row in df_pnl.iterrows():
        logger.info(
            "ID=%d | %s | qty=%.2f | entry=%.2f | exit=%.2f | netPnL=%.2f | logR=%.4f | signal=%s | sentiment=%.2f",
            row["trade_id"],
            row["ticker"],
            row["quantity"],
            row["entry_price"],
            row["exit_price"],
            row["net_pnl"],
            row["log_return"],
            row["signal_type"],
            row["sentiment_at_trade"] if row["sentiment_at_trade"] is not None else 0.0,
        )

    # Win rate
    win_rate = compute_win_rate(trade_pnls)
    logger.info("\n🏆 Win Rate: %.2f%%", win_rate * 100)

    # Correlation impact
    logger.info("\n===== Correlation Impact =====")
    compute_correlation_impact(engine, trade_pnls)


if __name__ == "__main__":
    main()


