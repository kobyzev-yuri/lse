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
    ts: pd.Timestamp  # exit (close) time
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
    entry_ts: Optional[pd.Timestamp] = None  # open time (MSK) for table /closed
    entry_strategy: Optional[str] = None  # стратегия открытия (первый BUY)
    exit_strategy: Optional[str] = None  # стратегия закрытия (SELL)
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    mfe: Optional[float] = None
    mae: Optional[float] = None
    context_json: Optional[str] = None
    entry_impulse_pct: Optional[float] = None  # импульс при принятии решения об открытии (momentum_2h_pct на момент BUY)


@dataclass
class OpenPosition:
    """Открытая (не закрытая) позиция для отчёта /pending."""
    ticker: str
    quantity: float
    entry_price: float
    entry_ts: Optional[pd.Timestamp]
    strategy_name: Optional[str] = None  # стратегия последнего BUY по этой позиции
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    context_json: Optional[str] = None


def get_engine():
    db_url = get_database_url()
    return create_engine(db_url)


def load_trade_history(engine, strategy_name: Optional[str] = None) -> pd.DataFrame:
    """Загружает историю сделок. strategy_name — опциональный фильтр (например 'GAME_5M')."""
    query = """
        SELECT id, ts, ticker, side, quantity, price,
               commission, signal_type, total_value, sentiment_at_trade, strategy_name,
               take_profit, stop_loss, mfe, mae, context_json
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
    position_open_ts: Dict[str, Optional[pd.Timestamp]] = {}  # дата открытия текущей позиции (для /closed)
    position_open_strategy: Dict[str, str] = {}  # стратегия открытия (первый BUY по позиции)
    position_take_profit: Dict[str, Optional[float]] = {}
    position_stop_loss: Dict[str, Optional[float]] = {}
    position_context_json: Dict[str, Optional[str]] = {}

    for _, row in trades.iterrows():
        ticker = row["ticker"]
        side = row["side"].upper()
        qty = float(row["quantity"])
        price = float(row["price"])
        commission = float(row["commission"]) if row["commission"] is not None else 0.0
        ts = row["ts"]
        trade_id = int(row["id"])
        signal_type = row.get("signal_type") or ""
        strategy = (row.get("strategy_name") or "").strip() or "—"
        sentiment = (
            float(row["sentiment_at_trade"])
            if row["sentiment_at_trade"] is not None
            else None
        )

        if ticker not in position_qty:
            position_qty[ticker] = 0.0
            position_cost[ticker] = 0.0
            position_open_ts[ticker] = None
            position_open_strategy[ticker] = "—"
            position_take_profit[ticker] = None
            position_stop_loss[ticker] = None
            position_context_json[ticker] = None

        if side == "BUY":
            # Покупка: при открытии позиции (с 0) запоминаем дату и стратегию
            if position_qty[ticker] == 0:
                position_open_ts[ticker] = pd.to_datetime(ts)
                position_open_strategy[ticker] = strategy
                position_take_profit[ticker] = float(row["take_profit"]) if pd.notna(row.get("take_profit")) else None
                position_stop_loss[ticker] = float(row["stop_loss"]) if pd.notna(row.get("stop_loss")) else None
                position_context_json[ticker] = row.get("context_json")
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
            entry_ts = position_open_ts.get(ticker)
            entry_strat = position_open_strategy.get(ticker) or "—"
            entry_ctx = position_context_json.get(ticker)
            entry_impulse_pct = None
            if entry_ctx:
                try:
                    from services.deal_params_5m import get_entry_impulse_pct
                    entry_impulse_pct = get_entry_impulse_pct(entry_ctx)
                except Exception:
                    pass
            position_qty[ticker] -= qty
            position_cost[ticker] -= cost_for_sold
            if position_qty[ticker] <= 0:
                position_open_ts[ticker] = None
                position_open_strategy[ticker] = "—"

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
                    entry_ts=entry_ts,
                    entry_strategy=entry_strat,
                    exit_strategy=strategy,
                    take_profit=position_take_profit.get(ticker),
                    stop_loss=position_stop_loss.get(ticker),
                    mfe=float(row.get("mfe")) if pd.notna(row.get("mfe")) else None,
                    mae=float(row.get("mae")) if pd.notna(row.get("mae")) else None,
                    context_json=entry_ctx,
                    entry_impulse_pct=entry_impulse_pct,
                )
            )

    return results


def _canonical_ticker_for_positions(ticker) -> str:
    """Единый ключ для агрегации позиций (как UPPER(TRIM) в SQL). Иначе SNDK и «SNDK » дают разные корзины и /pending теряет строку."""
    return str(ticker or "").strip().upper()


def compute_open_positions(trades: pd.DataFrame) -> List[OpenPosition]:
    """
    Список открытых позиций (есть BUY без полного SELL).
    Использует ту же модель средневзвешенной цены входа.
    Тикер нормализуется (strip + upper), чтобы BUY/SELL с разным написанием сходились.
    """
    result: List[OpenPosition] = []

    if trades.empty:
        return result

    trades = trades.copy()
    trades["quantity"] = trades["quantity"].astype(float)
    trades["price"] = trades["price"].astype(float)
    trades["commission"] = trades["commission"].astype(float)

    position_qty: Dict[str, float] = {}
    position_cost: Dict[str, float] = {}
    position_open_ts: Dict[str, Optional[pd.Timestamp]] = {}
    position_last_strategy: Dict[str, str] = {}
    position_take_profit: Dict[str, Optional[float]] = {}
    position_stop_loss: Dict[str, Optional[float]] = {}
    position_context_json: Dict[str, Optional[str]] = {}

    for _, row in trades.iterrows():
        ticker = _canonical_ticker_for_positions(row["ticker"])
        if not ticker:
            continue
        side = row["side"].upper()
        qty = float(row["quantity"])
        price = float(row["price"])
        commission = float(row["commission"]) if row["commission"] is not None else 0.0
        ts = row["ts"]
        strategy = (row.get("strategy_name") or "").strip() or "—"

        if ticker not in position_qty:
            position_qty[ticker] = 0.0
            position_cost[ticker] = 0.0
            position_open_ts[ticker] = None
            position_last_strategy[ticker] = "—"
            position_take_profit[ticker] = None
            position_stop_loss[ticker] = None
            position_context_json[ticker] = None

        if side == "BUY":
            if position_qty[ticker] == 0:
                position_open_ts[ticker] = pd.to_datetime(ts)
                position_take_profit[ticker] = float(row["take_profit"]) if pd.notna(row.get("take_profit")) else None
                position_stop_loss[ticker] = float(row["stop_loss"]) if pd.notna(row.get("stop_loss")) else None
                position_context_json[ticker] = row.get("context_json")
            position_qty[ticker] += qty
            position_cost[ticker] += qty * price + commission
            position_last_strategy[ticker] = strategy
        elif side == "SELL":
            if position_qty[ticker] <= 0:
                continue
            avg_entry = position_cost[ticker] / position_qty[ticker]
            cost_for_sold = avg_entry * qty
            position_qty[ticker] -= qty
            position_cost[ticker] -= cost_for_sold
            if position_qty[ticker] <= 0:
                position_open_ts[ticker] = None

    for ticker, qty in position_qty.items():
        if qty > 0 and position_cost.get(ticker, 0) > 0:
            result.append(
                OpenPosition(
                    ticker=ticker,
                    quantity=qty,
                    entry_price=position_cost[ticker] / qty,
                    entry_ts=position_open_ts.get(ticker),
                    strategy_name=position_last_strategy.get(ticker) or "—",
                    take_profit=position_take_profit.get(ticker),
                    stop_loss=position_stop_loss.get(ticker),
                    context_json=position_context_json.get(ticker),
                )
            )

    return sorted(result, key=lambda p: (p.entry_ts or pd.Timestamp.min), reverse=True)


def get_last_closed_for_ticker(engine, ticker: str, strategy_name: str = "GAME_5M") -> Optional[TradePnL]:
    """
    Последняя закрытая сделка по тикеру (как в /closed).
    Нужно для уведомления о закрытии: брать entry_price/exit_price из БД, чтобы совпадало с /closed.
    """
    trades = load_trade_history(engine, strategy_name=strategy_name)
    if trades.empty:
        return None
    closed = compute_closed_trade_pnls(trades)
    by_ticker = [t for t in closed if (t.ticker or "").strip().upper() == (ticker or "").strip().upper()]
    if not by_ticker:
        return None
    by_ticker.sort(key=lambda t: t.ts, reverse=True)
    return by_ticker[0]


def compute_win_rate(trade_pnls: List[TradePnL]) -> float:
    if not trade_pnls:
        return 0.0
    wins = sum(1 for t in trade_pnls if t.net_pnl > 0)
    return wins / len(trade_pnls)


def get_strategy_outcome_stats(engine, limit_days: Optional[int] = None) -> str:
    """
    Статистика по исходам закрытых сделок по стратегиям (entry_strategy).
    Для контекста LLM: «в похожих ситуациях стратегия X давала N сделок, K в плюс».
    limit_days: учитывать только сделки за последние N дней; None — вся история.
    """
    trades = load_trade_history(engine)
    if trades.empty:
        return ""
    closed = compute_closed_trade_pnls(trades)
    if not closed:
        return ""

    if limit_days is not None and limit_days > 0:
        try:
            cutoff = pd.Timestamp.now(tz=closed[0].ts.tzinfo if closed[0].ts.tzinfo else None) - pd.Timedelta(days=limit_days)
            closed = [t for t in closed if t.ts >= cutoff]
        except Exception:
            pass
    if not closed:
        return ""

    stats: Dict[str, Dict[str, float]] = {}
    for t in closed:
        name = (t.entry_strategy or "").strip() or "—"
        if name not in stats:
            stats[name] = {"count": 0, "wins": 0, "pnl_sum": 0.0}
        stats[name]["count"] += 1
        if t.net_pnl > 0:
            stats[name]["wins"] += 1
        stats[name]["pnl_sum"] += t.net_pnl

    lines = []
    for name in sorted(stats.keys(), key=lambda x: -stats[x]["count"]):
        c = int(stats[name]["count"])
        w = int(stats[name]["wins"])
        pct = (100.0 * w / c) if c else 0
        total_pnl = stats[name]["pnl_sum"]
        lines.append(f"{name}: {c} сделок, {w} в плюс ({pct:.0f}%), суммарный PnL ${total_pnl:+.2f}")
    return "История по стратегиям (закрытые сделки): " + "; ".join(lines)


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


def get_rolling_corr_with_benchmark(
    engine,
    ticker: str,
    benchmark: str = "MU",
    window_days: int = 14,
) -> tuple[Optional[float], str]:
    """
    Скользящая корреляция лог-доходностей ticker vs benchmark за последние window_days.
    Возвращает (corr_float | None, label: "Independent" | "In-Sync" | "Unknown").
    Для контекста LLM: высокая корреляция — бумага движется с сектором/бенчмарком.
    """
    if ticker == benchmark:
        return (1.0, "In-Sync")
    quotes = load_quotes(engine, [ticker, benchmark])
    if quotes.empty or quotes["date"].nunique() < 2:
        return (None, "Unknown")
    prices = quotes.pivot_table(index="date", columns="ticker", values="close").sort_index()
    if ticker not in prices.columns or benchmark not in prices.columns:
        return (None, "Unknown")
    log_ret = np.log(prices / prices.shift(1)).dropna(how="all")
    if log_ret.shape[0] < window_days:
        return (None, "Unknown")
    rolling = log_ret[ticker].rolling(window=window_days).corr(log_ret[benchmark])
    last_corr = rolling.dropna().iloc[-1] if not rolling.dropna().empty else None
    if last_corr is None:
        return (None, "Unknown")
    corr_f = float(last_corr)
    if abs(corr_f) < 0.3:
        label = "Independent"
    else:
        label = "In-Sync"
    return (corr_f, label)


def get_latest_prices(engine, tickers: List[str]) -> Dict[str, float]:
    """Последняя цена (close) по каждому тикеру из quotes. Нет данных → тикер не в словаре."""
    if not tickers:
        return {}
    with engine.connect() as conn:
        placeholders = ", ".join([f":t{i}" for i in range(len(tickers))])
        params = {f"t{i}": t for i, t in enumerate(tickers)}
        # PostgreSQL: последняя по date строка по каждому тикеру
        df = pd.read_sql(
            text(
                f"""
                SELECT DISTINCT ON (ticker) ticker, close
                FROM quotes
                WHERE ticker IN ({placeholders})
                ORDER BY ticker, date DESC
                """
            ),
            conn,
            params=params,
        )
    return dict(zip(df["ticker"], df["close"].astype(float))) if not df.empty else {}


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


