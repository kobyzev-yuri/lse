"""Portfolio maintenance: indicator legacy closes, portfolio_state reconcile."""

from __future__ import annotations

import logging
from typing import List

from sqlalchemy import text

logger = logging.getLogger(__name__)


def close_indicator_legacy_positions(*, dry_run: bool = False) -> int:
    from config_loader import get_config_value
    from execution_agent import ExecutionAgent, Position
    from report_generator import compute_open_positions, get_engine, get_latest_prices, load_trade_history
    from services.game_5m import GAME_5M_STRATEGY
    from services.ticker_groups import get_tickers_indicator_only

    if not (get_config_value("PORTFOLIO_CLOSE_INDICATOR_LEGACY", "true") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return 0

    indicators = {t.strip().upper() for t in get_tickers_indicator_only() if t.strip()}
    if not indicators:
        return 0

    engine = get_engine()
    trades = load_trade_history(engine)
    pending = [
        p
        for p in compute_open_positions(trades)
        if (p.strategy_name or "").strip() != GAME_5M_STRATEGY and p.ticker.strip().upper() in indicators
    ]
    if not pending:
        return 0

    prices = get_latest_prices(engine, [p.ticker for p in pending])
    agent = ExecutionAgent()
    closed = 0
    for p in pending:
        t = p.ticker.strip().upper()
        if t not in prices:
            logger.warning("Нет котировки %s — пропуск INDICATOR_LEGACY_CLOSE", t)
            continue
        if dry_run:
            logger.info("[dry-run] would close indicator position %s", t)
            closed += 1
            continue
        pos = Position(
            ticker=t,
            quantity=float(p.quantity),
            entry_price=float(p.entry_price),
            entry_ts=p.entry_ts,
        )
        agent._execute_sell(t, pos, "INDICATOR_LEGACY_CLOSE (TICKERS_INDICATOR_ONLY)", strategy_name=p.strategy_name)
        closed += 1
        logger.info("Закрыта индикаторная позиция %s", t)
    return closed


def reconcile_portfolio_state(*, dry_run: bool = False) -> int:
    from report_generator import compute_open_positions, get_engine, load_trade_history
    from services.game_5m import GAME_5M_STRATEGY

    engine = get_engine()
    trades = load_trade_history(engine)
    ledger = [p for p in compute_open_positions(trades) if (p.strategy_name or "").strip() != GAME_5M_STRATEGY]
    ledger_map = {p.ticker.strip().upper(): p for p in ledger}
    fixes = 0

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT ticker, quantity FROM portfolio_state WHERE ticker != 'CASH'")
        ).fetchall()
        state_tickers = {str(r[0]).strip().upper(): float(r[1] or 0) for r in rows}

    ops: List[tuple] = []
    for t, qty in state_tickers.items():
        if qty <= 0:
            continue
        lp = ledger_map.get(t)
        if lp is None:
            ops.append(("delete", t, None))
        elif abs(float(lp.quantity) - qty) > 1e-6:
            ops.append(("update", t, lp))

    for t, lp in ledger_map.items():
        if t not in state_tickers or state_tickers.get(t, 0) <= 0:
            ops.append(("insert", t, lp))

    for kind, t, lp in ops:
        if dry_run:
            logger.info("[dry-run] reconcile %s %s", kind, t)
            fixes += 1
            continue
        with engine.begin() as conn:
            if kind == "delete":
                conn.execute(text("DELETE FROM portfolio_state WHERE ticker = :t"), {"t": t})
            elif kind == "update" and lp is not None:
                conn.execute(
                    text(
                        "UPDATE portfolio_state SET quantity = :q, avg_entry_price = :ep, "
                        "last_updated = CURRENT_TIMESTAMP WHERE ticker = :t"
                    ),
                    {"q": float(lp.quantity), "ep": float(lp.entry_price), "t": t},
                )
            elif kind == "insert" and lp is not None:
                conn.execute(
                    text(
                        """
                        INSERT INTO portfolio_state (ticker, quantity, avg_entry_price, last_updated)
                        VALUES (:t, :q, :ep, CURRENT_TIMESTAMP)
                        ON CONFLICT (ticker) DO UPDATE SET
                          quantity = EXCLUDED.quantity,
                          avg_entry_price = EXCLUDED.avg_entry_price,
                          last_updated = CURRENT_TIMESTAMP
                        """
                    ),
                    {"t": t, "q": float(lp.quantity), "ep": float(lp.entry_price)},
                )
        fixes += 1
        logger.info("reconcile portfolio_state %s %s", kind, t)
    return fixes


def run_portfolio_maintenance(*, dry_run: bool = False) -> int:
    n = close_indicator_legacy_positions(dry_run=dry_run)
    n += reconcile_portfolio_state(dry_run=dry_run)
    return n
