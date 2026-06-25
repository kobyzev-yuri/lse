"""
Option Money Map — «где сидят деньги»: плиты OI put/call по страйкам, направление потока.
Источник: Polygon snapshot (OI) или история из options_chain_oi_snapshot. One-liner — шаблон без LLM.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from services.options_chain_sentiment import _aggregate_by_strike, _filter_contracts_for_analysis

# Эвристики one-liner / плит (документируются в one_liner_breakdown для UI)
PCR_VOL_BULLISH_MAX = 0.87
PCR_VOL_BEARISH_MIN = 1.15
PUT_PLATE_SPOT_MAX_RATIO = 1.01
CALL_CEILING_SPOT_MIN_RATIO = 0.99
TOP_STRIKES_N = 3


def default_pcr_vol_thresholds() -> Dict[str, float]:
    """Глобальные дефолты: константы → config.env (OPTIONS_MAP_PCR_VOL_*)."""
    try:
        from services.decision_stack._types import _cfg_float

        return {
            "pcr_volume_bullish_max": _cfg_float("OPTIONS_MAP_PCR_VOL_BULLISH_MAX", PCR_VOL_BULLISH_MAX),
            "pcr_volume_bearish_min": _cfg_float("OPTIONS_MAP_PCR_VOL_BEARISH_MIN", PCR_VOL_BEARISH_MIN),
        }
    except Exception:
        return {
            "pcr_volume_bullish_max": PCR_VOL_BULLISH_MAX,
            "pcr_volume_bearish_min": PCR_VOL_BEARISH_MIN,
        }


def resolve_pcr_vol_thresholds(
    *,
    ticker: str = "",
    pcr_volume_bullish_max: Optional[float] = None,
    pcr_volume_bearish_min: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Пороги PCR volume для Money Map one-liner.
    override (UI query) > config.env > wireframe-дефолты 0.87 / 1.15.
    """
    defaults = default_pcr_vol_thresholds()
    has_override = pcr_volume_bullish_max is not None or pcr_volume_bearish_min is not None
    bull = float(pcr_volume_bullish_max if pcr_volume_bullish_max is not None else defaults["pcr_volume_bullish_max"])
    bear = float(pcr_volume_bearish_min if pcr_volume_bearish_min is not None else defaults["pcr_volume_bearish_min"])
    bull = max(0.35, min(1.0, bull))
    bear = max(1.0, min(3.0, bear))
    if bull >= bear - 0.02:
        bear = min(3.0, bull + 0.05)
    sym = (ticker or "").strip().upper()
    return {
        "ticker": sym or None,
        "pcr_volume_bullish_max": round(bull, 4),
        "pcr_volume_bearish_min": round(bear, 4),
        "defaults": {
            "pcr_volume_bullish_max": round(float(defaults["pcr_volume_bullish_max"]), 4),
            "pcr_volume_bearish_min": round(float(defaults["pcr_volume_bearish_min"]), 4),
        },
        "source": "ui_override" if has_override else "default",
        "calibrated": False,
        "note_ru": (
            "Стартовые пороги wireframe (≈±15% от PCR=1), не подобраны по исходам сделок. "
            "В UI — ручная подстройка per ticker (localStorage). "
            "GAME_5M gate использует отдельные OPTIONS_SENTIMENT_PCR_VOL_*."
        ),
    }


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
            if oi <= 0 or k > spot * PUT_PLATE_SPOT_MAX_RATIO:
                continue
            out.append({"strike": k, "oi": oi, "leg": "put"})
        else:
            oi = int(r.get("call_oi") or 0)
            if oi <= 0 or k < spot * CALL_CEILING_SPOT_MIN_RATIO:
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


def _is_atm_strike(strike: float, spot: float, *, atm_pct: float = 0.008) -> bool:
    return spot > 0 and abs(float(strike) - float(spot)) < float(spot) * atm_pct


def _filter_chart_bars_for_display(
    chart_bars: List[Dict[str, Any]],
    *,
    spot: Optional[float] = None,
    atm_pct: float = 0.008,
    min_oi_abs: int = 200,
    min_oi_frac_of_max: float = 0.05,
    min_bars: int = 3,
    max_bars: int = 24,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Убирает «мусорные» страйки с крошечным OI — остаются крупные пики для графика.
    Плиты (support/resistance) считаются по полной выборке до фильтра.
    ATM (±atm_pct от spot) всегда оставляем на графике, даже при малом OI.
    """
    raw = list(chart_bars or [])
    meta: Dict[str, Any] = {"bars_raw": len(raw), "bars_shown": len(raw), "oi_threshold": 0}
    if not raw:
        return [], meta

    max_total = max(int(b.get("total_oi") or 0) for b in raw)
    if max_total <= 0:
        return [], meta

    threshold = max(int(min_oi_abs), int(max_total * min_oi_frac_of_max))
    meta["oi_threshold"] = threshold
    kept = [b for b in raw if int(b.get("total_oi") or 0) >= threshold]

    if len(kept) < min_bars:
        kept = sorted(raw, key=lambda x: int(x.get("total_oi") or 0), reverse=True)[:max_bars]
        kept.sort(key=lambda x: float(x["strike"]))
        meta["fallback"] = "top_by_oi"
    elif len(kept) > max_bars:
        kept = sorted(kept, key=lambda x: int(x.get("total_oi") or 0), reverse=True)[:max_bars]
        kept.sort(key=lambda x: float(x["strike"]))
        meta["capped"] = max_bars

    spot_f = float(spot or 0.0)
    if spot_f > 0:
        meta["atm_pct"] = atm_pct
        meta["atm_lo"] = round(spot_f * (1.0 - atm_pct), 2)
        meta["atm_hi"] = round(spot_f * (1.0 + atm_pct), 2)
        kept_strikes = {float(b["strike"]) for b in kept}
        atm_forced: List[float] = []
        for b in raw:
            k = float(b["strike"])
            if k not in kept_strikes and _is_atm_strike(k, spot_f, atm_pct=atm_pct):
                kept.append(b)
                kept_strikes.add(k)
                atm_forced.append(k)
        if atm_forced:
            kept.sort(key=lambda x: float(x["strike"]))
            meta["atm_strikes_forced"] = atm_forced

    meta["bars_shown"] = len(kept)
    return kept, meta


def _flow_label(
    pcr_vol: Optional[float],
    *,
    pcr_volume_bullish_max: float,
    pcr_volume_bearish_min: float,
) -> Tuple[str, str]:
    if pcr_vol is None:
        return "NEUTRAL", "баланс put/call по объёму"
    if pcr_vol >= pcr_volume_bearish_min:
        return "BEARISH", "свежее активнее put (защита / ставки на снижение)"
    if pcr_vol <= pcr_volume_bullish_max:
        return "BULLISH", "свежее активнее call (ставки на рост)"
    return "NEUTRAL", "баланс put/call по объёму торгов"


def _money_fmt_usd(value: float) -> str:
    return f"${value:,.0f}".replace(",", " ")


def build_one_liner_breakdown(
    *,
    sym: str,
    exp: str,
    spot_f: float,
    spot_source: Optional[str],
    source: str,
    snapshot_date: Optional[str],
    support: List[Dict[str, Any]],
    resistance: List[Dict[str, Any]],
    flow_label: str,
    flow_ru: str,
    pcr_vol: Optional[float],
    call_vol: int,
    put_vol: int,
    call_oi: int,
    put_oi: int,
    scope: Dict[str, Any],
    strike_window_pct: float,
    summary_one_liner_ru: str,
    pcr_thresholds: Optional[Dict[str, Any]] = None,
    cron_pcr_recommendation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Пошаговый разбор шаблонного one-liner для вкладки «Расчёт» в Money Map."""
    th = pcr_thresholds or resolve_pcr_vol_thresholds()
    bull_max = float(th.get("pcr_volume_bullish_max") or PCR_VOL_BULLISH_MAX)
    bear_min = float(th.get("pcr_volume_bearish_min") or PCR_VOL_BEARISH_MIN)
    bias_ru = {
        "BULLISH": "ожидание роста",
        "BEARISH": "ожидание снижения",
        "NEUTRAL": "без явного перекоса",
    }.get(flow_label, "без явного перекоса")

    sup_strikes = [float(s["strike"]) for s in support]
    res_strikes = [float(s["strike"]) for s in resistance]
    sup_band = _format_strike_band(sup_strikes)
    res_band = _format_strike_band(res_strikes)

    data_source = "Polygon live snapshot"
    if snapshot_date:
        data_source = f"БД options_chain_oi_snapshot, snapshot_date={snapshot_date}"
    elif source == "snapshot":
        data_source = "БД options_chain_oi_snapshot"

    pcr_formula = None
    pcr_rule = "Недостаточно call volume в окне — PCR не считается"
    pcr_source_ru = "дефолт config/код"
    if th.get("source") == "ui_override":
        pcr_source_ru = "слайдеры UI (localStorage для тикера)"
    if call_vol > 0 and pcr_vol is not None:
        pcr_formula = f"{put_vol:,} / {call_vol:,} = {pcr_vol:.3f}".replace(",", " ")
        if flow_label == "BULLISH":
            pcr_rule = f"PCR ≤ {bull_max} → «{bias_ru}» (bullish)"
        elif flow_label == "BEARISH":
            pcr_rule = f"PCR ≥ {bear_min} → «{bias_ru}» (bearish)"
        else:
            pcr_rule = f"{bull_max} < PCR < {bear_min} → «{bias_ru}» (нейтральная полоса)"

    def_d = th.get("defaults") or {}
    def_bull = def_d.get("pcr_volume_bullish_max", PCR_VOL_BULLISH_MAX)
    def_bear = def_d.get("pcr_volume_bearish_min", PCR_VOL_BEARISH_MIN)

    return {
        "summary_one_liner_ru": summary_one_liner_ru,
        "is_template_not_llm": True,
        "intro_ru": (
            "Строка над картой — детерминированный шаблон из одного снимка опционной доски. "
            "Склеиваются spot, три зоны (put-плита / call-потолок по OI) и оценка потока по PCR volume. "
            "Не прогноз цены и не рекомендация входа."
        ),
        "caveats_ru": [
            "Плиты и потолок — open interest (накопленные позиции); «рынок — ожидание …» — только PCR по volume за день.",
            "Диапазоны $950–$1 100 — min…max из топ-3 страйков по OI, не непрерывная «зона» на графике.",
            "График OI может показывать меньше страйков (фильтр мелкого OI); плиты считаются по полной выборке в окне ±20%.",
            f"Пороги PCR ({bull_max} / {bear_min}): {pcr_source_ru}. Стартовый дефолт {def_bull}/{def_bear} — wireframe, без калибровки на GAME_5M.",
            "Sentiment на /options/tools — другая формула (score ±0.35, окно ±15%); цифры могут не совпадать с картой.",
        ],
        "data_source_ru": data_source,
        "expiration_date": exp,
        "ticker": sym,
        "thresholds": {
            "strike_window_pct": strike_window_pct,
            "put_plate_spot_max_ratio": PUT_PLATE_SPOT_MAX_RATIO,
            "call_ceiling_spot_min_ratio": CALL_CEILING_SPOT_MIN_RATIO,
            "top_strikes_n": TOP_STRIKES_N,
            "pcr_volume_bullish_max": bull_max,
            "pcr_volume_bearish_min": bear_min,
            "pcr_thresholds_source": th.get("source"),
            "pcr_thresholds_defaults": th.get("defaults"),
            "pcr_thresholds_note_ru": th.get("note_ru"),
        },
        "cron_pcr_recommendation": cron_pcr_recommendation,
        "steps": [
            {
                "id": "spot",
                "title_ru": "Spot — цена акции",
                "result_ru": _money_fmt_usd(spot_f),
                "method_ru": (
                    "Базовый актив на момент снимка. Live: Polygon (часто stocks snapshot); "
                    f"архив: поле spot из таблицы options_chain_oi_snapshot. Источник: {spot_source or source}."
                ),
            },
            {
                "id": "window",
                "title_ru": "Окно страйков (фильтр доски)",
                "result_ru": (
                    f"{_money_fmt_usd(float(scope.get('strike_lo') or 0))} – "
                    f"{_money_fmt_usd(float(scope.get('strike_hi') or 0))}"
                ),
                "method_ru": (
                    f"В расчёт попадают только контракты с strike в ±{strike_window_pct * 100:.0f}% от spot. "
                    f"Сейчас: {scope.get('contracts_in_window', '—')} строк из {scope.get('contracts_raw', '—')} "
                    "в исходном ответе Polygon/БД. Все метрики ниже — внутри этого окна."
                ),
            },
            {
                "id": "put_plate",
                "title_ru": "Put-плита (поддержка) — по OI",
                "result_ru": sup_band,
                "method_ru": (
                    f"Среди страйков ниже spot (strike ≤ spot×{PUT_PLATE_SPOT_MAX_RATIO}) выбираем "
                    f"топ-{TOP_STRIKES_N} с наибольшим put open interest. "
                    "В тексте — диапазон от минимального до максимального страйка из списка (не «сплошная плита»). "
                    "Интерпретация: где рынок накопил put-позиции — условные уровни интереса, не гарантия поддержки цены."
                ),
                "candidates": [
                    {
                        "strike": s["strike"],
                        "put_oi": s["oi"],
                        "rank": i + 1,
                    }
                    for i, s in enumerate(support)
                ],
            },
            {
                "id": "call_ceiling",
                "title_ru": "Call-потолок — по OI",
                "result_ru": res_band,
                "method_ru": (
                    f"Среди страйков выше spot (strike ≥ spot×{CALL_CEILING_SPOT_MIN_RATIO}) — "
                    f"топ-{TOP_STRIKES_N} по call open interest. "
                    "Диапазон в one-liner = min…max страйков из списка. "
                    "Крупный call OI выше spot часто читают как зону сопротивления / covered call, не как цель роста."
                ),
                "candidates": [
                    {
                        "strike": s["strike"],
                        "call_oi": s["oi"],
                        "rank": i + 1,
                    }
                    for i, s in enumerate(resistance)
                ],
            },
            {
                "id": "flow",
                "title_ru": "«рынок — …» и поток (PCR volume)",
                "result_ru": f"рынок — {bias_ru}; {flow_ru.capitalize()}.",
                "method_ru": (
                    "Суммируем put_volume и call_volume по всем контрактам в окне ±20%. "
                    "PCR = put_volume / call_volume (паритет 1.0). "
                    "Это внутридневной поток сделок, не open interest. "
                    f"Пороги bullish≤{bull_max} / bearish≥{bear_min} ({pcr_source_ru}). "
                    "Между порогами — NEUTRAL («без явного перекоса»)."
                ),
                "inputs": {
                    "put_volume": put_vol,
                    "call_volume": call_vol,
                    "pcr_volume": round(pcr_vol, 4) if pcr_vol is not None else None,
                    "call_open_interest": call_oi,
                    "put_open_interest": put_oi,
                },
                "inputs_labels_ru": {
                    "put_volume": "объём put",
                    "call_volume": "объём call",
                    "pcr_volume": "PCR volume",
                    "put_open_interest": "OI put (справочно)",
                    "call_open_interest": "OI call (справочно)",
                },
                "formula_ru": pcr_formula,
                "rule_ru": pcr_rule,
                "flow_label": flow_label,
            },
            {
                "id": "assemble",
                "title_ru": "Сборка one-liner",
                "result_ru": summary_one_liner_ru,
                "method_ru": (
                    "Фиксированный шаблон из шагов 1–5: spot, формулировка bias по PCR, "
                    "две полосы страйков по OI и фраза про поток; в конце — фактический PCR vol и "
                    "применённые пороги. LLM не участвует."
                ),
            },
        ],
        "assembled_ru": (
            f"Spot {_money_fmt_usd(spot_f)} · рынок — {bias_ru}. "
            f"Put-плита (поддержка): {sup_band}. Call-потолок: {res_band}. {flow_ru.capitalize()}."
            + (
                f" · PCR vol {float(pcr_vol):.2f} · пороги ≤{bull_max:.2f} / ≥{bear_min:.2f}"
                if pcr_vol is not None
                else ""
            )
        ),
    }


def build_summary_one_liner(
    *,
    spot: float,
    support: List[Dict[str, Any]],
    resistance: List[Dict[str, Any]],
    flow_label: str,
    flow_ru: str,
    oi_available: bool,
    pcr_volume: Optional[float] = None,
    pcr_volume_bullish_max: Optional[float] = None,
    pcr_volume_bearish_min: Optional[float] = None,
) -> str:
    pcr_tail = ""
    if pcr_volume is not None and pcr_volume_bullish_max is not None and pcr_volume_bearish_min is not None:
        pcr_tail = (
            f" · PCR vol {float(pcr_volume):.2f} · пороги ≤{float(pcr_volume_bullish_max):.2f} / "
            f"≥{float(pcr_volume_bearish_min):.2f}"
        )
    if not oi_available:
        return (
            f"Spot ${spot:,.0f}: open interest недоступен в источнике — "
            f"для плит нужен Polygon. Поток: {flow_ru}.{pcr_tail}"
        ).replace(",", " ")
    sup = _format_strike_band([s["strike"] for s in support])
    res = _format_strike_band([s["strike"] for s in resistance])
    bias = {"BULLISH": "ожидание роста", "BEARISH": "ожидание снижения", "NEUTRAL": "без явного перекоса"}.get(
        flow_label, "без явного перекоса"
    )
    return (
        f"Spot ${spot:,.0f} · рынок — {bias}. "
        f"Put-плита (поддержка): {sup}. Call-потолок: {res}. {flow_ru.capitalize()}.{pcr_tail}"
    ).replace(",", " ")


def _load_cron_pcr_recommendation(sym: str, exp: str) -> Dict[str, Any]:
    try:
        from services.options_map_cron_stats import lookup_cron_pcr_recommendation

        return lookup_cron_pcr_recommendation(sym, exp)
    except Exception:
        return {
            "status": "missing",
            "ticker": sym,
            "requested_expiration_date": exp,
            "reason_ru": "Не удалось загрузить cron-артефакт.",
        }


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
    pcr_thresholds: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    th = pcr_thresholds or resolve_pcr_vol_thresholds(ticker=sym)
    bull_max = float(th["pcr_volume_bullish_max"])
    bear_min = float(th["pcr_volume_bearish_min"])
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
    flow_label, flow_ru = _flow_label(
        pcr_vol,
        pcr_volume_bullish_max=bull_max,
        pcr_volume_bearish_min=bear_min,
    )

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
    chart_bars, chart_scope = _filter_chart_bars_for_display(chart_bars, spot=spot_f)

    one_liner = build_summary_one_liner(
        spot=spot_f,
        support=support,
        resistance=resistance,
        flow_label=flow_label,
        flow_ru=flow_ru,
        oi_available=oi_available,
        pcr_volume=pcr_vol,
        pcr_volume_bullish_max=bull_max,
        pcr_volume_bearish_min=bear_min,
    )
    breakdown = build_one_liner_breakdown(
        sym=sym,
        exp=exp,
        spot_f=spot_f,
        spot_source=spot_source,
        source=source,
        snapshot_date=snapshot_date,
        support=support,
        resistance=resistance,
        flow_label=flow_label,
        flow_ru=flow_ru,
        pcr_vol=pcr_vol,
        call_vol=call_vol,
        put_vol=put_vol,
        call_oi=call_oi,
        put_oi=put_oi,
        scope=scope,
        strike_window_pct=strike_window_pct,
        summary_one_liner_ru=one_liner,
        pcr_thresholds=th,
        cron_pcr_recommendation=_load_cron_pcr_recommendation(sym, exp),
    )

    is_live = snapshot_date is None
    note = (
        "Плиты и потолок — open interest (Polygon/БД). Поток — PCR volume в окне ±{:.0f}% spot. "
        "Пороги PCR настраиваются в UI; не торговый сигнал. Подробнее — вкладка «Расчёт one-liner»."
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
        "one_liner_breakdown": breakdown,
        "flow_label": flow_label,
        "flow_ru": flow_ru,
        "oi_available": oi_available,
        "support_plate": support,
        "resistance_ceiling": resistance,
        "pcr_volume": round(pcr_vol, 3) if pcr_vol is not None else None,
        "pcr_thresholds": th,
        "totals": {
            "call_volume": call_vol,
            "put_volume": put_vol,
            "call_open_interest": call_oi,
            "put_open_interest": put_oi,
        },
        "analysis_scope": scope,
        "chart_bars": chart_bars,
        "chart_scope": chart_scope,
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
    pcr_thresholds: Optional[Dict[str, Any]] = None,
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
        pcr_thresholds=pcr_thresholds,
    )


def build_money_map_report(
    ticker: str,
    *,
    expiration_date: Optional[str] = None,
    snapshot_date: Optional[str] = None,
    strike_window_pct: float = 0.20,
    pcr_volume_bullish_max: Optional[float] = None,
    pcr_volume_bearish_min: Optional[float] = None,
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

    pcr_thresholds = resolve_pcr_vol_thresholds(
        ticker=sym,
        pcr_volume_bullish_max=pcr_volume_bullish_max,
        pcr_volume_bearish_min=pcr_volume_bearish_min,
    )

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
            sym,
            exp,
            snap,
            strike_window_pct=strike_window_pct,
            available_expirations=exps or [exp],
            pcr_thresholds=pcr_thresholds,
        )

    if not polygon_options_available():
        if snap_dates:
            return _report_from_snapshot_rows(
                sym,
                exp,
                snap_dates[0],
                strike_window_pct=strike_window_pct,
                available_expirations=exps or [exp],
                pcr_thresholds=pcr_thresholds,
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
        pcr_thresholds=pcr_thresholds,
    )
