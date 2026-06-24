"""
Компактный контекст опционного сентимента (Polygon) для карточек и decision_stack.
Без полной доски — score, PCR, max pain, плиты, gate_hint.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from services.options_chain_sentiment import (
    _aggregate_by_strike,
    _filter_contracts_for_analysis,
    analyze_options_chain,
)
from services.options_money_map import _flow_label, _top_strikes, build_summary_one_liner

logger = logging.getLogger(__name__)

_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_STRIKE_WINDOW_PCT = 0.20


def _cache_ttl_sec() -> int:
    from config_loader import get_config_value

    try:
        return int((get_config_value("OPTIONS_CARD_CONTEXT_CACHE_SEC", "900") or "900").strip())
    except (TypeError, ValueError):
        return 900


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    row = _CACHE.get(key)
    if not row:
        return None
    ts, payload = row
    if time.monotonic() - ts > _cache_ttl_sec():
        _CACHE.pop(key, None)
        return None
    return dict(payload)


def _cache_put(key: str, payload: Dict[str, Any]) -> None:
    _CACHE[key] = (time.monotonic(), dict(payload))


def clear_options_card_context_cache() -> None:
    """Для тестов."""
    _CACHE.clear()


def _gate_hint(
    *,
    sentiment_label: str,
    sentiment_score: float,
    pcr_volume: Optional[float],
) -> str:
    from services.decision_stack._types import _cfg_float

    bear_score = _cfg_float("OPTIONS_SENTIMENT_BEARISH_SCORE", -0.35)
    bull_score = _cfg_float("OPTIONS_SENTIMENT_BULLISH_SCORE", 0.35)
    pcr_bear = _cfg_float("OPTIONS_SENTIMENT_PCR_VOL_BEARISH", 1.15)
    pcr_bull = _cfg_float("OPTIONS_SENTIMENT_PCR_VOL_BULLISH", 0.87)

    label = (sentiment_label or "").strip().upper()
    if (
        label == "BEARISH"
        and sentiment_score <= bear_score
        and pcr_volume is not None
        and pcr_volume >= pcr_bear
    ):
        return "would_downgrade"
    if (
        label == "BULLISH"
        and sentiment_score >= bull_score
        and pcr_volume is not None
        and pcr_volume <= pcr_bull
    ):
        return "would_signal"
    return "neutral"


def _compact_from_chain(
    sym: str,
    exp: str,
    *,
    contracts: List[Dict[str, Any]],
    spot_f: float,
    spot_source: Optional[str] = None,
) -> Dict[str, Any]:
    filtered, _scope = _filter_contracts_for_analysis(
        contracts, spot=spot_f, strike_window_pct=_STRIKE_WINDOW_PCT, drop_zero_oi_volume=False
    )
    analysis = analyze_options_chain(filtered, spot=spot_f)
    totals = analysis.get("totals") if isinstance(analysis.get("totals"), dict) else {}

    by_strike = _aggregate_by_strike(filtered)
    rows = list(by_strike.values())
    support = _top_strikes(rows, side="put_support", spot=spot_f, n=3)
    resistance = _top_strikes(rows, side="call_resistance", spot=spot_f, n=3)

    call_vol = sum(int(c.get("volume") or 0) for c in filtered if c.get("contract_type") == "call")
    put_vol = sum(int(c.get("volume") or 0) for c in filtered if c.get("contract_type") == "put")
    pcr_vol = (put_vol / call_vol) if call_vol > 0 else totals.get("pcr_volume")
    flow_label, flow_ru = _flow_label(float(pcr_vol) if pcr_vol is not None else None)
    oi_available = bool(analysis.get("oi_available"))

    score = float(analysis.get("sentiment_score") or 0.0)
    label = str(analysis.get("sentiment_label") or "NEUTRAL")
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
        "source": "polygon",
        "data_as_of": "live",
        "ticker": sym,
        "expiration_date": exp,
        "spot": round(spot_f, 2),
        "spot_source": spot_source,
        "sentiment_label": label,
        "sentiment_score": score,
        "pcr_volume": totals.get("pcr_volume"),
        "pcr_open_interest": totals.get("pcr_open_interest"),
        "max_pain_strike": analysis.get("max_pain_strike"),
        "support_plate_strikes": [s["strike"] for s in support],
        "resistance_ceiling_strikes": [s["strike"] for s in resistance],
        "one_liner_ru": one_liner,
        "gate_hint": _gate_hint(
            sentiment_label=label,
            sentiment_score=score,
            pcr_volume=totals.get("pcr_volume"),
        ),
        "oi_available": oi_available,
    }


def build_options_card_context(
    ticker: str,
    *,
    expiration_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Компактный снимок Polygon для карточек / decision_stack.
    Ошибка API → status=error; gate не блокирует торговлю.
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return {"status": "error", "error": "ticker required", "ticker": sym, "gate_hint": "unavailable"}

    exp_key = (expiration_date or "").strip() or "*"
    cache_key = f"{sym}:{exp_key}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    from services.polygon_options import (
        fetch_option_expiration_dates,
        fetch_options_chain_snapshot,
        polygon_options_available,
    )

    if not polygon_options_available():
        return {
            "status": "error",
            "error": "POLYGON_API_KEY не настроен",
            "ticker": sym,
            "gate_hint": "unavailable",
        }

    exps = fetch_option_expiration_dates(sym)
    exp = (expiration_date or "").strip() or (exps[0] if exps else "")
    if not exp:
        return {
            "status": "error",
            "error": f"нет дат экспирации для {sym}",
            "ticker": sym,
            "gate_hint": "unavailable",
        }

    try:
        raw = fetch_options_chain_snapshot(sym, expiration_date=exp)
    except Exception as e:
        logger.debug("options_card_context fetch %s: %s", sym, e)
        return {"status": "error", "error": str(e), "ticker": sym, "gate_hint": "unavailable"}

    if raw.get("status") == "error":
        return {
            "status": "error",
            "error": raw.get("error") or "polygon error",
            "ticker": sym,
            "gate_hint": "unavailable",
        }

    spot = raw.get("underlying_price")
    if spot is None or float(spot) <= 0:
        return {
            "status": "error",
            "error": "spot недоступен",
            "ticker": sym,
            "expiration_date": exp,
            "gate_hint": "unavailable",
        }

    contracts = list(raw.get("contracts") or [])
    out = _compact_from_chain(
        sym,
        exp,
        contracts=contracts,
        spot_f=float(spot),
        spot_source=raw.get("spot_source"),
    )
    _cache_put(cache_key, out)
    return out


def format_gate_hint_ru(hint: Optional[str]) -> str:
    m = {
        "would_downgrade": "shadow: ослабил бы BUY",
        "would_signal": "shadow: поддержка BUY",
        "neutral": "нейтрально",
        "unavailable": "нет данных",
    }
    return m.get(str(hint or "").strip(), str(hint or "—"))


def format_options_card_context_lines_ru(opts: Dict[str, Any]) -> List[str]:
    """3–4 строки для карточек / prompt_entry (plain text)."""
    if not isinstance(opts, dict):
        return []
    if opts.get("status") != "ok":
        err = opts.get("error") or opts.get("status") or "нет данных"
        return [f"Options (Polygon): {err}"]
    lines = [
        f"{opts.get('sentiment_label') or '—'} · score {float(opts.get('sentiment_score') or 0):.2f}",
    ]
    pcr = opts.get("pcr_volume")
    if pcr is not None:
        lines.append(f"PCR volume {float(pcr):.2f}")
    mp = opts.get("max_pain_strike")
    if mp is not None:
        lines.append(f"Max pain ${float(mp):,.0f}".replace(",", " "))
    hint = format_gate_hint_ru(opts.get("gate_hint"))
    lines.append(f"Gate: {hint}")
    one = opts.get("one_liner_ru")
    if one:
        lines.append(str(one))
    return lines


def format_options_card_context_html_block(opts: Dict[str, Any]) -> str:
    """HTML-блок для prompt_entry game5m."""
    from services.game_report_html import esc

    if not isinstance(opts, dict):
        return ""
    lines = format_options_card_context_lines_ru(opts)
    if not lines:
        return ""
    meta = []
    if opts.get("expiration_date"):
        meta.append(f"exp {opts['expiration_date']}")
    if opts.get("data_as_of"):
        meta.append(f"as-of {opts['data_as_of']}")
    if opts.get("spot") is not None:
        meta.append(f"spot ${float(opts['spot']):,.2f}".replace(",", " "))
    head = "Options (Polygon)"
    if meta:
        head += " · " + " · ".join(meta)
    body = "<br>".join(esc(ln) for ln in lines)
    link = ""
    ticker = (opts.get("ticker") or "").strip().upper()
    if ticker:
        link = f' <a href="/options/map?ticker={esc(ticker)}">Money Map →</a>'
    return f"<h3>{esc(head)}</h3><p class=\"meta\">{body}{link}</p>"


def attach_options_polygon_to_brief(
    brief: Dict[str, Any],
    *,
    symbol: str,
    event_date: Optional[Any] = None,
) -> None:
    """Добавляет live options_polygon в earnings brief (фаза 3 UI)."""
    sym = (symbol or "").strip().upper()
    exp: Optional[str] = None
    if event_date is not None:
        try:
            from datetime import date as date_cls

            from services.options_calculator_prefill import _suggest_expiration

            ev_s = event_date.isoformat() if isinstance(event_date, date_cls) else str(event_date)[:10]
            exp, _ = _suggest_expiration(sym, ev_s)
        except Exception as e:
            logger.debug("attach_options_polygon exp %s: %s", sym, e)
    try:
        brief["options_polygon"] = build_options_card_context(sym, expiration_date=exp)
    except Exception as e:
        logger.debug("attach_options_polygon %s: %s", sym, e)
        brief["options_polygon"] = {"status": "error", "error": str(e), "gate_hint": "unavailable", "ticker": sym}
