"""
Калькулятор long Put и bear Put Spread (дебетовый) на экспирацию — intrinsic P/L.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

Strategy = Literal["pure_put", "put_spread"]

SCENARIO_DROP_PCTS = (0.0, -2.0, -3.0, -5.0, -7.0, -8.0, -10.0, -12.0, -15.0, -20.0)


def _put_intrinsic(strike: float, spot: float) -> float:
    return max(0.0, strike - spot)


def _spread_intrinsic(long_k: float, short_k: float, spot: float) -> float:
    return max(0.0, long_k - spot) - max(0.0, short_k - spot)


def compute_put_strategy(
    *,
    strategy: Strategy,
    spot: float,
    contracts: int,
    long_strike: float,
    long_premium: float,
    short_strike: Optional[float] = None,
    short_premium: Optional[float] = None,
) -> Dict[str, Any]:
    """
    long_premium / short_premium — цена за 1 акцию (как bid/ask на доске).
    contracts — число контрактов (множитель 100).
    """
    n = max(1, int(contracts))
    spot = float(spot)
    long_k = float(long_strike)
    long_prem = float(long_premium)
    mult = 100.0 * n

    if strategy == "pure_put":
        entry = long_prem * mult
        max_loss = entry
        max_profit = None  # unbounded
        breakeven = long_k - long_prem
        width = None
    else:
        if short_strike is None or short_premium is None:
            raise ValueError("put_spread requires short_strike and short_premium")
        short_k = float(short_strike)
        short_prem = float(short_premium)
        if long_k <= short_k:
            raise ValueError("long_strike must be > short_strike for bear put spread")
        net_debit = long_prem - short_prem
        entry = net_debit * mult
        width = long_k - short_k
        max_loss = max(0.0, entry)
        max_profit = max(0.0, (width - net_debit) * mult)
        breakeven = long_k - net_debit

    scenarios = _build_scenarios(
        strategy=strategy,
        spot=spot,
        contracts=n,
        long_k=long_k,
        long_prem=long_prem,
        entry=entry,
        max_loss=max_loss if strategy == "pure_put" else max_loss,
        max_profit=max_profit,
        breakeven=breakeven,
        short_k=float(short_strike) if short_strike is not None else None,
        short_prem=float(short_premium) if short_premium is not None else None,
        width=width,
    )

    return {
        "strategy": strategy,
        "spot": round(spot, 2),
        "contracts": n,
        "entry_cost_usd": round(entry, 2),
        "breakeven": round(breakeven, 2),
        "max_loss_usd": round(max_loss if strategy == "pure_put" else max_loss, 2),
        "max_profit_usd": round(max_profit, 2) if max_profit is not None else None,
        "spread_width": round(width, 2) if width is not None else None,
        "scenarios": scenarios,
    }


def _build_scenarios(
    *,
    strategy: Strategy,
    spot: float,
    contracts: int,
    long_k: float,
    long_prem: float,
    entry: float,
    max_loss: float,
    max_profit: Optional[float],
    breakeven: float,
    short_k: Optional[float],
    short_prem: Optional[float],
    width: Optional[float],
) -> List[Dict[str, Any]]:
    mult = 100.0 * contracts
    out: List[Dict[str, Any]] = []
    for drop_pct in SCENARIO_DROP_PCTS:
        price = spot * (1.0 + drop_pct / 100.0)
        if strategy == "pure_put":
            value = _put_intrinsic(long_k, price) * mult
        else:
            assert short_k is not None
            value = _spread_intrinsic(long_k, short_k, price) * mult

        pnl = value - entry
        roi = (pnl / entry * 100.0) if entry > 0 else 0.0
        status = _position_status(
            strategy=strategy,
            price=price,
            pnl=pnl,
            entry=entry,
            long_k=long_k,
            breakeven=breakeven,
            max_loss=max_loss,
            max_profit=max_profit,
            short_k=short_k,
            width=width,
        )
        out.append(
            {
                "drop_pct": drop_pct,
                "stock_price": round(price, 2),
                "position_value_usd": round(value, 2),
                "pnl_usd": round(pnl, 2),
                "roi_pct": round(roi, 2),
                "status_ru": status,
            }
        )
    return out


def _position_status(
    *,
    strategy: Strategy,
    price: float,
    pnl: float,
    entry: float,
    long_k: float,
    breakeven: float,
    max_loss: float,
    max_profit: Optional[float],
    short_k: Optional[float],
    width: Optional[float],
) -> str:
    eps = 0.01 * entry if entry > 0 else 0.01

    if strategy == "pure_put":
        if price >= long_k:
            return "Максимальный убыток"
        if abs(pnl) <= eps:
            return "Безубыток"
        if pnl < -eps:
            return "Убыток"
        return "Прибыль"

    assert short_k is not None and width is not None and max_profit is not None
    if price >= long_k:
        return "Максимальный убыток"
    if price <= short_k and pnl >= max_profit - eps:
        return "Максимальная прибыль"
    if abs(pnl) <= eps:
        return "Безубыток"
    if pnl < -eps:
        return "Убыток"
    return "Прибыль"
