"""
Option Money Map — «где сидят деньги»: плиты OI put/call по страйкам, направление потока.
Источник: Polygon snapshot (OI) или история из options_chain_oi_snapshot. One-liner — шаблон без LLM.
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


def _plate_shift_ru(
    *,
    prev_date: str,
    prev_support: List[Dict[str, Any]],
    prev_resistance: List[Dict[str, Any]],
    support: List[Dict[str, Any]],
    resistance: List[Dict[str, Any]],
) -> Optional[str]:
    parts: List[str] = []
    if prev_support and support:
        p = float(prev_support[0]["strike"])
        c = float(support[0]["strike"])
        if abs(p - c) >= 1:
            parts.append(f"put-плита ${p:,.0f} → ${c:,.0f}".replace(",", " "))
    if prev_resistance and resistance:
        p = float(prev_resistance[0]["strike"])
        c = float(resistance[0]["strike"])
        if abs(p - c) >= 1:
            parts.append(f"call-потолок ${p:,.0f} → ${c:,.0f}".replace(",", " "))
    if not parts:
        return None
    return f"Сдвиг с {prev_date}: " + "; ".join(parts) + "."


def _assemble_money_map_report(
    sym: str,
    exp: str,
    *,
    contracts: List[Dict[str, Any]],
    spot_f: float,
    source: str,
    available_expirations: List[str],
    strike_window_pct: float,
    spot_source: Optional[str] = None,
    snapshot_date: Optional[str] = None,
    available_snapshot_dates: Optional[List[str]] = None,
    plate_shift_ru: Optional[str] = None,
) -> Dict[str, Any]:
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

    is_live = snapshot_date is None
    note = (
        "Плиты и потолок — по open interest (Polygon). "
        "Поток — PCR по volume в окне ±{:.0f}% от spot. Не торговый сигнал."
    ).format(strike_window_pct * 100)
    if not is_live:
        note = f"Снимок {snapshot_date} из БД (cron). {note}"

    out: Dict[str, Any] = {
        "status": "ok",
        "ticker": sym,
        "source": source,
        "is_live": is_live,
        "snapshot_date": snapshot_date,
        "expiration_date": exp,
        "available_expirations": available_expirations,
        "available_snapshot_dates": available_snapshot_dates or [],
        "spot": round(spot_f, 2),
        "spot_source": spot_source,
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
        "data_quality": {"note_ru": note},
    }
    if plate_shift_ru:
        out["plate_shift_ru"] = plate_shift_ru
    return out


def list_oi_snapshot_dates(ticker: str, *, expiration_date: Optional[str] = None) -> List[str]:
    """Даты снимков OI в БД (новые первыми)."""
    sym = (ticker or "").strip().upper()
    if not sym:
        return []
    try:
        from sqlalchemy import text
        from report_generator import get_engine

        q = """
            SELECT DISTINCT snapshot_date::text AS d
            FROM options_chain_oi_snapshot
            WHERE ticker = :ticker
        """
        params: Dict[str, Any] = {"ticker": sym}
        if expiration_date:
            q += " AND expiration_date = :exp"
            params["exp"] = expiration_date.strip()
        q += " ORDER BY d DESC"
        with get_engine().connect() as conn:
            rows = conn.execute(text(q), params).fetchall()
        return [str(r[0]) for r in rows]
    except Exception:
        return []


def _load_snapshot_contracts(
    ticker: str,
    snapshot_date: str,
    expiration_date: str,
) -> Dict[str, Any]:
    from sqlalchemy import text
    from report_generator import get_engine

    sym = ticker.strip().upper()
    snap = snapshot_date.strip()
    exp = expiration_date.strip()
    q = text(
        """
        SELECT strike, contract_type, open_interest, volume, spot
        FROM options_chain_oi_snapshot
        WHERE ticker = :ticker
          AND snapshot_date = :snapshot_date
          AND expiration_date = :expiration_date
        ORDER BY strike, contract_type
        """
    )
    with get_engine().connect() as conn:
        rows = conn.execute(
            q,
            {"ticker": sym, "snapshot_date": snap, "expiration_date": exp},
        ).fetchall()
    if not rows:
        return {
            "status": "error",
            "error": f"нет снимка {snap} для {sym} exp {exp}",
            "ticker": sym,
        }

    spot_vals = [float(r[4]) for r in rows if r[4] is not None]
    spot_f = spot_vals[0] if spot_vals else None
    if spot_f is None or spot_f <= 0:
        return {"status": "error", "error": "spot в снимке недоступен", "ticker": sym}

    contracts = [
        {
            "strike": float(r[0]),
            "contract_type": str(r[1]),
            "open_interest": int(r[2] or 0),
            "volume": int(r[3] or 0),
        }
        for r in rows
    ]
    return {"status": "ok", "spot": spot_f, "contracts": contracts}


def _report_from_snapshot_rows(
    sym: str,
    exp: str,
    snapshot_date: str,
    *,
    strike_window_pct: float,
    available_expirations: List[str],
) -> Dict[str, Any]:
    snap_dates = list_oi_snapshot_dates(sym, expiration_date=exp)
    if snapshot_date not in snap_dates:
        return {
            "status": "error",
            "error": f"снимок {snapshot_date} не найден",
            "ticker": sym,
            "available_snapshot_dates": snap_dates,
        }

    loaded = _load_snapshot_contracts(sym, snapshot_date, exp)
    if loaded.get("status") == "error":
        loaded["available_snapshot_dates"] = snap_dates
        return loaded

    contracts = loaded["contracts"]
    spot_f = float(loaded["spot"])

    plate_shift_ru: Optional[str] = None
    idx = snap_dates.index(snapshot_date)
    if idx + 1 < len(snap_dates):
        prev_date = snap_dates[idx + 1]
        prev_loaded = _load_snapshot_contracts(sym, prev_date, exp)
        if prev_loaded.get("status") == "ok":
            prev_filtered, _ = _filter_contracts_for_analysis(
                prev_loaded["contracts"],
                spot=float(prev_loaded["spot"]),
                strike_window_pct=strike_window_pct,
                drop_zero_oi_volume=False,
            )
            prev_rows = list(_aggregate_by_strike(prev_filtered).values())
            prev_spot = float(prev_loaded["spot"])
            prev_support = _top_strikes(prev_rows, side="put_support", spot=prev_spot, n=3)
            prev_resistance = _top_strikes(prev_rows, side="call_resistance", spot=prev_spot, n=3)
            cur_filtered, _ = _filter_contracts_for_analysis(
                contracts, spot=spot_f, strike_window_pct=strike_window_pct, drop_zero_oi_volume=False
            )
            cur_rows = list(_aggregate_by_strike(cur_filtered).values())
            support = _top_strikes(cur_rows, side="put_support", spot=spot_f, n=3)
            resistance = _top_strikes(cur_rows, side="call_resistance", spot=spot_f, n=3)
            plate_shift_ru = _plate_shift_ru(
                prev_date=prev_date,
                prev_support=prev_support,
                prev_resistance=prev_resistance,
                support=support,
                resistance=resistance,
            )

    return _assemble_money_map_report(
        sym,
        exp,
        contracts=contracts,
        spot_f=spot_f,
        source="snapshot",
        available_expirations=available_expirations,
        strike_window_pct=strike_window_pct,
        spot_source="db_snapshot",
        snapshot_date=snapshot_date,
        available_snapshot_dates=snap_dates,
        plate_shift_ru=plate_shift_ru,
    )


def build_money_map_report(
    ticker: str,
    *,
    expiration_date: Optional[str] = None,
    snapshot_date: Optional[str] = None,
    strike_window_pct: float = 0.20,
) -> Dict[str, Any]:
    """Отчёт для /api/options/map и wireframe UI. snapshot_date → история из БД; иначе live Polygon."""
    from services.polygon_options import (
        fetch_option_expiration_dates,
        fetch_options_chain_snapshot,
        polygon_options_available,
    )

    sym = (ticker or "").strip().upper()
    if not sym:
        return {"status": "error", "error": "ticker required", "ticker": sym}

    snap = (snapshot_date or "").strip()
    if snap and snap.lower() in ("live", "now"):
        snap = ""

    exps: List[str] = []
    if polygon_options_available():
        exps = fetch_option_expiration_dates(sym)
    exp = (expiration_date or "").strip() or (exps[0] if exps else "")
    if not exp:
        return {"status": "error", "error": f"нет дат экспирации для {sym}", "ticker": sym}

    snap_dates = list_oi_snapshot_dates(sym, expiration_date=exp)

    if snap:
        if not exps and not snap_dates:
            return {"status": "error", "error": "нет данных для тикера", "ticker": sym}
        return _report_from_snapshot_rows(
            sym, exp, snap, strike_window_pct=strike_window_pct, available_expirations=exps or [exp]
        )

    if not polygon_options_available():
        if snap_dates:
            return _report_from_snapshot_rows(
                sym,
                exp,
                snap_dates[0],
                strike_window_pct=strike_window_pct,
                available_expirations=exps or [exp],
            )
        return {"status": "error", "error": "POLYGON_API_KEY не настроен", "ticker": sym}

    raw = fetch_options_chain_snapshot(sym, expiration_date=exp)
    if raw.get("status") == "error":
        return {"status": "error", "error": raw.get("error"), "ticker": sym}

    contracts = list(raw.get("contracts") or [])
    spot = raw.get("underlying_price")
    if spot is None or float(spot) <= 0:
        return {"status": "error", "error": "spot недоступен", "ticker": sym, "expiration_date": exp}

    return _assemble_money_map_report(
        sym,
        exp,
        contracts=contracts,
        spot_f=float(spot),
        source="polygon",
        available_expirations=exps,
        strike_window_pct=strike_window_pct,
        spot_source=raw.get("spot_source"),
        snapshot_date=None,
        available_snapshot_dates=snap_dates,
    )
