"""
Аналитика option chain: PCR, ключевые страйки, max pain, индикатор сентимента.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def _aggregate_by_strike(contracts: List[Dict[str, Any]]) -> Dict[float, Dict[str, Any]]:
    by: Dict[float, Dict[str, Any]] = {}
    for c in contracts:
        k = float(c["strike"])
        side = c["contract_type"]
        row = by.setdefault(
            k,
            {
                "strike": k,
                "call_volume": 0,
                "put_volume": 0,
                "call_oi": 0,
                "put_oi": 0,
            },
        )
        vol = int(c.get("volume") or 0)
        oi = int(c.get("open_interest") or 0)
        if side == "call":
            row["call_volume"] += vol
            row["call_oi"] += oi
        else:
            row["put_volume"] += vol
            row["put_oi"] += oi
    return by


def _max_pain_strike(by_strike: Dict[float, Dict[str, Any]], spot: float) -> Tuple[Optional[float], float]:
    """Страйк с минимальной суммарной выплатой держателям (classic max pain)."""
    strikes = sorted(by_strike.keys())
    if not strikes:
        return None, 0.0
    best_s = strikes[0]
    best_cost = float("inf")
    for s in strikes:
        total = 0.0
        for k, row in by_strike.items():
            call_oi = int(row.get("call_oi") or 0)
            put_oi = int(row.get("put_oi") or 0)
            total += max(0.0, s - k) * call_oi * 100.0
            total += max(0.0, k - s) * put_oi * 100.0
        if total < best_cost:
            best_cost = total
            best_s = s
    return best_s, best_cost


def analyze_options_chain(
    contracts: List[Dict[str, Any]],
    *,
    spot: Optional[float] = None,
    near_money_pct: float = 0.08,
) -> Dict[str, Any]:
    """
    Сентимент и барьеры по цепочке одной экспирации.
    """
    if not contracts:
        return {"status": "empty", "sentiment_label": "NO_DATA"}

    for c in contracts:
        if spot is None and c.get("underlying_price") is not None:
            spot = float(c["underlying_price"])
    spot = float(spot or 0.0)

    call_vol = sum(int(c.get("volume") or 0) for c in contracts if c.get("contract_type") == "call")
    put_vol = sum(int(c.get("volume") or 0) for c in contracts if c.get("contract_type") == "put")
    call_oi = sum(int(c.get("open_interest") or 0) for c in contracts if c.get("contract_type") == "call")
    put_oi = sum(int(c.get("open_interest") or 0) for c in contracts if c.get("contract_type") == "put")

    pcr_vol = (put_vol / call_vol) if call_vol > 0 else None
    pcr_oi = (put_oi / call_oi) if call_oi > 0 else None

    by_strike = _aggregate_by_strike(contracts)
    rows = list(by_strike.values())
    for r in rows:
        r["total_oi"] = int(r["call_oi"]) + int(r["put_oi"])
        r["total_volume"] = int(r["call_volume"]) + int(r["put_volume"])
        r["oi_put_share"] = (r["put_oi"] / r["total_oi"]) if r["total_oi"] > 0 else 0.5

    # Ключевые барьеры — топ по OI
    barriers_oi = sorted(rows, key=lambda x: x["total_oi"], reverse=True)[:8]
    # Активность — топ по volume
    barriers_vol = sorted(rows, key=lambda x: x["total_volume"], reverse=True)[:8]

    max_pain, _ = _max_pain_strike(by_strike, spot)

    # Near-the-money subset
    if spot > 0:
        lo, hi = spot * (1.0 - near_money_pct), spot * (1.0 + near_money_pct)
        ntm = [r for r in rows if lo <= r["strike"] <= hi]
    else:
        ntm = rows

    ntm_put_oi = sum(r["put_oi"] for r in ntm)
    ntm_call_oi = sum(r["call_oi"] for r in ntm)
    ntm_pcr = (ntm_put_oi / ntm_call_oi) if ntm_call_oi > 0 else None

    score, label, bias_ru = _sentiment_score(pcr_vol, pcr_oi, ntm_pcr)

    return {
        "status": "ok",
        "spot": round(spot, 2) if spot else None,
        "totals": {
            "call_volume": call_vol,
            "put_volume": put_vol,
            "call_open_interest": call_oi,
            "put_open_interest": put_oi,
            "pcr_volume": round(pcr_vol, 3) if pcr_vol is not None else None,
            "pcr_open_interest": round(pcr_oi, 3) if pcr_oi is not None else None,
            "ntm_pcr_open_interest": round(ntm_pcr, 3) if ntm_pcr is not None else None,
        },
        "sentiment_score": round(score, 3),
        "sentiment_label": label,
        "sentiment_summary_ru": bias_ru,
        "max_pain_strike": max_pain,
        "key_strikes_oi": [
            {
                "strike": r["strike"],
                "total_oi": r["total_oi"],
                "call_oi": r["call_oi"],
                "put_oi": r["put_oi"],
                "put_share": round(r["oi_put_share"], 3),
            }
            for r in barriers_oi
        ],
        "key_strikes_volume": [
            {
                "strike": r["strike"],
                "total_volume": r["total_volume"],
                "call_volume": r["call_volume"],
                "put_volume": r["put_volume"],
            }
            for r in barriers_vol
        ],
        "strikes_table": sorted(rows, key=lambda x: x["strike"]),
    }


def _sentiment_score(
    pcr_vol: Optional[float],
    pcr_oi: Optional[float],
    ntm_pcr: Optional[float],
) -> Tuple[float, str, str]:
    """
    Score ∈ [-1, 1]: отрицательный = рынок ставит на падение (put-heavy), положительный = call-heavy.
  PCR > 1 обычно интерпретируют как hedging/bearish positioning.
    """
    parts: List[float] = []
    if pcr_vol is not None and pcr_vol > 0:
        parts.append(-_clamp((pcr_vol - 1.0) / 0.8, -1.0, 1.0))
    if pcr_oi is not None and pcr_oi > 0:
        parts.append(-_clamp((pcr_oi - 1.0) / 0.8, -1.0, 1.0))
    if ntm_pcr is not None and ntm_pcr > 0:
        parts.append(-_clamp((ntm_pcr - 1.0) / 0.6, -1.0, 1.0))

    if not parts:
        return 0.0, "NEUTRAL", "Недостаточно данных по объёмам/OI."

    score = sum(parts) / len(parts)
    if score <= -0.35:
        label = "BEARISH"
        ru = (
            "Перевес put по volume/OI — рынок активнее хеджирует/ставит на снижение; "
            "ключевые страйки put могут выступать поддержкой только после «сброса» OI."
        )
    elif score >= 0.35:
        label = "BULLISH"
        ru = "Перевес call — спрос на рост/коллы выше; сопротивления на call-heavy страйках."
    else:
        label = "NEUTRAL"
        ru = "Баланс call/put без явного перекоса; смотрите топ страйков по OI как магниты цены."

    return score, label, ru


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def compute_chain_totals(contracts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Суммарные volume/OI и PCR по списку контрактов."""
    call_vol = sum(int(c.get("volume") or 0) for c in contracts if c.get("contract_type") == "call")
    put_vol = sum(int(c.get("volume") or 0) for c in contracts if c.get("contract_type") == "put")
    call_oi = sum(int(c.get("open_interest") or 0) for c in contracts if c.get("contract_type") == "call")
    put_oi = sum(int(c.get("open_interest") or 0) for c in contracts if c.get("contract_type") == "put")
    pcr_vol = (put_vol / call_vol) if call_vol > 0 else None
    pcr_oi = (put_oi / call_oi) if call_oi > 0 else None
    return {
        "call_volume": call_vol,
        "put_volume": put_vol,
        "call_open_interest": call_oi,
        "put_open_interest": put_oi,
        "pcr_volume": round(pcr_vol, 3) if pcr_vol is not None else None,
        "pcr_open_interest": round(pcr_oi, 3) if pcr_oi is not None else None,
        "contract_rows": len(contracts),
    }


def _filter_contracts_for_analysis(
    contracts: List[Dict[str, Any]],
    *,
    spot: Optional[float],
    strike_window_pct: float,
    drop_zero_oi_volume: bool,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Фильтр: ликвидные строки + окно страйков вокруг spot."""
    meta: Dict[str, Any] = {"contracts_raw": len(contracts)}
    working = list(contracts)
    if drop_zero_oi_volume:
        working = [c for c in working if int(c.get("volume") or 0) > 0 or int(c.get("open_interest") or 0) > 0]
        meta["contracts_after_liquidity_filter"] = len(working)
        meta["dropped_zero_oi_volume"] = meta["contracts_raw"] - len(working)
    if spot and strike_window_pct > 0:
        lo, hi = float(spot) * (1.0 - strike_window_pct), float(spot) * (1.0 + strike_window_pct)
        meta["strike_window_pct"] = strike_window_pct
        meta["strike_lo"] = round(lo, 2)
        meta["strike_hi"] = round(hi, 2)
        working = [c for c in working if lo <= float(c["strike"]) <= hi]
        meta["contracts_in_window"] = len(working)
    return working, meta


def build_chain_sentiment_report(
    ticker: str,
    *,
    expiration_date: Optional[str] = None,
    strike_window_pct: float = 0.15,
) -> Dict[str, Any]:
    """Полный отчёт: Polygon chain → аналитика."""
    from services.polygon_options import (
        fetch_option_expiration_dates,
        fetch_options_chain_snapshot,
        list_expiration_dates,
        polygon_options_available,
    )

    if not polygon_options_available():
        return {
            "status": "error",
            "error": "POLYGON_API_KEY не настроен",
            "ticker": ticker,
        }

    exps: List[str] = []
    if not expiration_date:
        exps = fetch_option_expiration_dates(ticker)
        if exps:
            expiration_date = exps[0]
        raw = fetch_options_chain_snapshot(ticker, expiration_date=expiration_date)
        contracts = raw.get("contracts") or []
        if not exps:
            exps = list_expiration_dates(contracts)
    else:
        raw = fetch_options_chain_snapshot(ticker, expiration_date=expiration_date)
        contracts = raw.get("contracts") or []
        exps = fetch_option_expiration_dates(ticker)

    if raw.get("status") == "error":
        return {"status": "error", "error": raw.get("error"), "ticker": ticker}

    all_contracts = list(raw.get("contracts") or [])
    spot = raw.get("underlying_price")
    totals_full = compute_chain_totals(all_contracts)
    contracts, scope_meta = _filter_contracts_for_analysis(
        all_contracts, spot=spot, strike_window_pct=strike_window_pct, drop_zero_oi_volume=False
    )
    if raw.get("spot_source"):
        scope_meta["spot_source"] = raw.get("spot_source")

    analysis = analyze_options_chain(contracts, spot=spot)
    return {
        "ticker": ticker.strip().upper(),
        "expiration_date": expiration_date,
        "source": "polygon",
        "contract_count": len(contracts),
        "available_expirations": exps,
        "totals_full_chain": totals_full,
        "analysis_scope": scope_meta,
        "data_quality": {
            "note_ru": "Score и PCR в карточке — по страйкам ±{:.0f}% от spot; totals_full_chain — вся доска.".format(
                strike_window_pct * 100
            ),
        },
        **analysis,
    }


def build_yfinance_chain_sentiment_report(
    ticker: str,
    *,
    expiration_date: Optional[str] = None,
    strike_window_pct: float = 0.15,
) -> Dict[str, Any]:
    """Полный отчёт: yfinance option_chain → та же аналитика, что Polygon."""
    from services.yfinance_options import (
        fetch_yfinance_option_chain,
        fetch_yfinance_option_expirations,
    )

    sym = ticker.strip().upper()
    exps = fetch_yfinance_option_expirations(sym)
    if not expiration_date:
        expiration_date = exps[0] if exps else None
    if not expiration_date:
        return {
            "status": "error",
            "error": f"yfinance: нет дат экспирации для {sym}",
            "ticker": sym,
            "source": "yfinance",
        }

    raw = fetch_yfinance_option_chain(sym, expiration_date=expiration_date)
    if raw.get("status") == "error":
        return {"status": "error", "error": raw.get("error"), "ticker": sym, "source": "yfinance"}

    all_contracts = list(raw.get("contracts") or [])
    spot = raw.get("underlying_price")
    totals_full = compute_chain_totals(all_contracts)
    contracts, scope_meta = _filter_contracts_for_analysis(
        all_contracts, spot=spot, strike_window_pct=strike_window_pct, drop_zero_oi_volume=False
    )
    if raw.get("spot_source"):
        scope_meta["spot_source"] = raw.get("spot_source")
    if raw.get("dropped_zero_oi_volume"):
        scope_meta["dropped_zero_oi_volume"] = raw.get("dropped_zero_oi_volume")

    analysis = analyze_options_chain(contracts, spot=spot)
    return {
        "ticker": sym,
        "expiration_date": expiration_date,
        "source": "yfinance",
        "contract_count": len(contracts),
        "available_expirations": exps,
        "chain_calls_puts": {
            "calls": raw.get("calls_count"),
            "puts": raw.get("puts_count"),
        },
        "totals_full_chain": totals_full,
        "analysis_scope": scope_meta,
        "data_quality": {
            "note_ru": (
                "yfinance: score/PCR по ±{:.0f}% spot; удалены строки без OI и volume; "
                "задержка Yahoo — сверяйте с Polygon после Options Starter."
            ).format(strike_window_pct * 100),
            "source": "yfinance",
        },
        **analysis,
    }
