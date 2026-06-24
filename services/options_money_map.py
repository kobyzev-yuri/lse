"""
Option Money Map — «где сидят деньги»: плиты OI put/call по страйкам, направление потока.
Источник: Polygon snapshot (OI). One-liner — шаблон без LLM.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from services.options_chain_sentiment import _aggregate_by_strike, _filter_contracts_for_analysis


def _top_strikes(
    rows: List[Dict[str, Any]],
    *,
    side: str,
    spot: float,
    n: int = 3,
) -> List[Dict[str, Any]]:
    """side: put_support (strike <= spot) | call_resistance (strike >= spot)."""
    out: List[Dict[str, Any]] = []
    for r in rows:
        k = float(r["strike"])
        if side == "put_support":
            oi = int(r.get("put_oi") or 0)
            if oi <= 0 or k > spot * 1.01:
                continue
            out.append({"strike": k, "oi": oi, "leg": "put"})
        else:
            oi = int(r.get("call_oi") or 0)
            if oi <= 0 or k < spot * 0.99:
                continue
            out.append({"strike": k, "oi": oi, "leg": "call"})
    out.sort(key=lambda x: x["oi"], reverse=True)
    return out[:n]


def _format_strike_band(strikes: List[float]) -> str:
    if not strikes:
        return "—"
    strikes = sorted(set(strikes))
    if len(strikes) == 1:
        return f"${strikes[0]:,.0f}".replace(",", " ")
    return f"${strikes[0]:,.0f}–${strikes[-1]:,.0f}".replace(",", " ")


def _flow_label(pcr_vol: Optional[float]) -> Tuple[str, str]:
    if pcr_vol is None:
        return "NEUTRAL", "баланс put/call по объёму"
    if pcr_vol >= 1.15:
        return "BEARISH", "свежее активнее put (защита / ставки на снижение)"
    if pcr_vol <= 0.87:
        return "BULLISH", "свежее активнее call (ставки на рост)"
    return "NEUTRAL", "баланс put/call по объёму торгов"


def build_summary_one_liner(
    *,
    spot: float,
    support: List[Dict[str, Any]],
    resistance: List[Dict[str, Any]],
    flow_label: str,
    flow_ru: str,
    oi_available: bool,
) -> str:
    if not oi_available:
        return (
            f"Spot ${spot:,.0f}: open interest недоступен в источнике — "
            f"для плит нужен Polygon. Поток: {flow_ru}."
        ).replace(",", " ")
    sup = _format_strike_band([s["strike"] for s in support])
    res = _format_strike_band([s["strike"] for s in resistance])
    bias = {"BULLISH": "ожидание роста", "BEARISH": "ожидание снижения", "NEUTRAL": "без явного перекоса"}.get(
        flow_label, "без явного перекоса"
    )
    return (
        f"Spot ${spot:,.0f} · рынок — {bias}. "
        f"Put-плита (поддержка): {sup}. Call-потолок: {res}. {flow_ru.capitalize()}."
    ).replace(",", " ")


def build_money_map_report(
    ticker: str,
    *,
    expiration_date: Optional[str] = None,
    strike_window_pct: float = 0.20,
) -> Dict[str, Any]:
    """Отчёт для /api/options/map и wireframe UI."""
    from services.polygon_options import (
        fetch_option_expiration_dates,
        fetch_options_chain_snapshot,
        polygon_options_available,
    )

    sym = (ticker or "").strip().upper()
    if not sym:
        return {"status": "error", "error": "ticker required", "ticker": sym}

    if not polygon_options_available():
        return {"status": "error", "error": "POLYGON_API_KEY не настроен", "ticker": sym}

    exps = fetch_option_expiration_dates(sym)
    exp = (expiration_date or "").strip() or (exps[0] if exps else "")
    if not exp:
        return {"status": "error", "error": f"нет дат экспирации для {sym}", "ticker": sym}

    raw = fetch_options_chain_snapshot(sym, expiration_date=exp)
    if raw.get("status") == "error":
        return {"status": "error", "error": raw.get("error"), "ticker": sym}

    contracts = list(raw.get("contracts") or [])
    spot = raw.get("underlying_price")
    if spot is None or float(spot) <= 0:
        return {"status": "error", "error": "spot недоступен", "ticker": sym, "expiration_date": exp}

    spot_f = float(spot)
    filtered, scope = _filter_contracts_for_analysis(
        contracts, spot=spot_f, strike_window_pct=strike_window_pct, drop_zero_oi_volume=False
    )

    call_vol = sum(int(c.get("volume") or 0) for c in filtered if c.get("contract_type") == "call")
    put_vol = sum(int(c.get("volume") or 0) for c in filtered if c.get("contract_type") == "put")
    call_oi = sum(int(c.get("open_interest") or 0) for c in filtered if c.get("contract_type") == "call")
    put_oi = sum(int(c.get("open_interest") or 0) for c in filtered if c.get("contract_type") == "put")
    oi_available = (call_oi + put_oi) > 0
    pcr_vol = (put_vol / call_vol) if call_vol > 0 else None

    by_strike = _aggregate_by_strike(filtered)
    rows = list(by_strike.values())
    for r in rows:
        r["total_oi"] = int(r["call_oi"]) + int(r["put_oi"])

    support = _top_strikes(rows, side="put_support", spot=spot_f, n=3)
    resistance = _top_strikes(rows, side="call_resistance", spot=spot_f, n=3)
    flow_label, flow_ru = _flow_label(pcr_vol)

    chart_bars = sorted(
        [
            {
                "strike": float(r["strike"]),
                "put_oi": int(r["put_oi"]),
                "call_oi": int(r["call_oi"]),
                "total_oi": int(r["total_oi"]),
            }
            for r in rows
            if int(r["put_oi"]) > 0 or int(r["call_oi"]) > 0
        ],
        key=lambda x: x["strike"],
    )

    one_liner = build_summary_one_liner(
        spot=spot_f,
        support=support,
        resistance=resistance,
        flow_label=flow_label,
        flow_ru=flow_ru,
        oi_available=oi_available,
    )

    return {
        "status": "ok",
        "ticker": sym,
        "source": "polygon",
        "expiration_date": exp,
        "available_expirations": exps,
        "spot": round(spot_f, 2),
        "spot_source": raw.get("spot_source"),
        "summary_one_liner_ru": one_liner,
        "flow_label": flow_label,
        "flow_ru": flow_ru,
        "oi_available": oi_available,
        "support_plate": support,
        "resistance_ceiling": resistance,
        "pcr_volume": round(pcr_vol, 3) if pcr_vol is not None else None,
        "totals": {
            "call_volume": call_vol,
            "put_volume": put_vol,
            "call_open_interest": call_oi,
            "put_open_interest": put_oi,
        },
        "analysis_scope": scope,
        "chart_bars": chart_bars,
        "data_quality": {
            "note_ru": (
                "Плиты и потолок — по open interest (Polygon). "
                "Поток — PCR по volume в окне ±{:.0f}% от spot. Не торговый сигнал."
            ).format(strike_window_pct * 100),
        },
    }
