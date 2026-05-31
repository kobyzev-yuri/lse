"""Premarket gap context for GAME_5M entry LLM (PRE_MARKET only)."""
from __future__ import annotations

from typing import Any, Optional


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None  # NaN guard


def _fmt_pct(v: Any, *, signed: bool = True, decimals: int = 2) -> str:
    x = _f(v)
    if x is None:
        return "—"
    if signed:
        return f"{x:+.{decimals}f}%"
    return f"{x:.{decimals}f}%"


def _fmt_price(v: Any) -> str:
    x = _f(v)
    if x is None:
        return "—"
    return f"${x:.2f}"


def build_premarket_entry_context_block(
    ticker: str,
    payload: dict[str, Any],
    *,
    strategy_name: str = "GAME_5M",
) -> str:
    """Human-readable premarket gap block for LLM / HTML reports."""
    t = (ticker or payload.get("ticker") or "?").strip().upper()
    gap = _f(payload.get("premarket_gap_pct"))
    prev = payload.get("prev_close")
    pm_last = payload.get("premarket_last")
    mins = payload.get("minutes_until_open")
    pm_mom = _f(payload.get("premarket_intraday_momentum_pct"))

    lines = [
        f"Контекст премаркета для {t} · игра {strategy_name} · advisory для входа у open, не заменяет техсигнал.",
    ]
    if mins is not None:
        lines.append(f"- До открытия NYSE (9:30 ET): {int(mins)} мин")
    lines.append(f"- prev_close: {_fmt_price(prev)} · premarket_last: {_fmt_price(pm_last)}")
    if gap is not None:
        lines.append(f"- Факт гэп к prev_close: {_fmt_pct(gap)}")
    else:
        lines.append("- Факт гэп к prev_close: — (нет цены премаркета)")

    if pm_mom is not None:
        lines.append(f"- Импульс премаркет (от начала PM-ленты, не гэп): {_fmt_pct(pm_mom)}")

    baseline = payload.get("premarket_gap_baseline")
    if isinstance(baseline, dict) and baseline.get("signal"):
        sig = baseline.get("signal")
        action = baseline.get("action")
        advice = baseline.get("entry_advice")
        reason = baseline.get("reason") or ""
        take_watch = baseline.get("should_take_watch")
        tw = " · TAKE-watch" if take_watch else ""
        lines.append(
            f"- Baseline (observable): signal={sig} · action={action} · entry_advice={advice}{tw}"
        )
        if reason:
            lines.append(f"  {reason}")

    pred = _f(payload.get("ticker_open_gap_predicted_pct"))
    if pred is not None:
        src = payload.get("ticker_open_gap_predicted_source") or "model"
        ver = payload.get("ticker_open_gap_model_version")
        conf = _f(payload.get("ticker_open_gap_confidence"))
        n_train = payload.get("ticker_open_gap_model_n_train")
        unc = _f(payload.get("ticker_open_gap_uncertainty_p80_pp"))
        meta = [f"source={src}"]
        if ver:
            meta.append(f"ver={ver}")
        if conf is not None:
            meta.append(f"conf={conf:.2f}")
        if n_train is not None:
            meta.append(f"n_train={n_train}")
        if unc is not None:
            meta.append(f"unc_p80={unc:.2f}pp")
        lines.append(f"- Прогноз open (ticker OLS): {_fmt_pct(pred)} ({', '.join(meta)})")

    sec_gap = _f(payload.get("macro_predicted_sector_gap_pct"))
    sec_proxy = payload.get("macro_sector_proxy")
    if sec_gap is not None:
        proxy = f" · proxy {sec_proxy}" if sec_proxy else ""
        lines.append(f"- Прогноз секторного гэпа{proxy}: {_fmt_pct(sec_gap)}")

    macro_level = payload.get("macro_risk_level")
    macro_bias = payload.get("macro_equity_gap_bias")
    if macro_level or macro_bias:
        lines.append(f"- Макро-риск: level={macro_level or '—'} · equity_gap_bias={macro_bias or '—'}")

    rec = payload.get("premarket_entry_recommendation")
    if rec:
        lines.append(f"- Рекомендация карточки: {str(rec)[:320]}")
    limit_px = payload.get("premarket_suggested_limit_price")
    if limit_px is not None:
        lines.append(f"- Лимит (ориентир): {_fmt_price(limit_px)}")

    entry_advice = payload.get("entry_advice")
    entry_reason = payload.get("entry_advice_reason")
    if entry_advice:
        tail = f" — {entry_reason}" if entry_reason else ""
        lines.append(f"- entry_advice (карточка): {entry_advice}{tail}")

    lines.append(
        "Учти гэп и baseline в reasoning/key_factors; сильный gap-up ≠ автоматический BUY; "
        "в премаркете ликвидность ниже — не меняй decision только из-за гэпа."
    )
    return "\n".join(lines)


def attach_premarket_entry_context(
    ticker: str,
    technical_data: dict[str, Any],
    *,
    decision_5m: dict[str, Any] | None = None,
    strategy_name: str = "GAME_5M",
) -> dict[str, Any]:
    """Attach premarket gap block when session is PRE_MARKET."""
    td = dict(technical_data or {})
    if td.get("premarket_entry_context_block"):
        return td
    payload: dict[str, Any] = dict(decision_5m or {})

    session = (payload.get("session_phase") or "").strip()
    if not session:
        try:
            from services.market_session import get_market_session_context

            session = (get_market_session_context().get("session_phase") or "").strip()
        except Exception:
            session = ""

    if session != "PRE_MARKET":
        return td

    if not payload.get("premarket_gap_pct") and payload.get("premarket_last") is None:
        try:
            from services.premarket import get_premarket_context
            from services.market_session import get_market_session_context

            pm = get_premarket_context(ticker)
            if not pm.get("error"):
                payload.setdefault("premarket_last", pm.get("premarket_last"))
                payload.setdefault("premarket_gap_pct", pm.get("premarket_gap_pct"))
                payload.setdefault("prev_close", pm.get("prev_close"))
                payload.setdefault(
                    "minutes_until_open",
                    pm.get("minutes_until_open")
                    or get_market_session_context().get("minutes_until_open"),
                )
        except Exception:
            pass

    if payload.get("premarket_gap_pct") is not None and not payload.get("premarket_gap_baseline"):
        try:
            from services.premarket_gap_baseline import evaluate_premarket_gap_baseline

            bl = evaluate_premarket_gap_baseline(
                payload.get("premarket_gap_pct"),
                macro_risk_level=payload.get("macro_risk_level"),
                macro_equity_gap_bias=payload.get("macro_equity_gap_bias"),
                multiday_horizon_1d_pct=(
                    (payload.get("multiday_lr_forecast") or {}).get("horizon_1d_pct")
                    if isinstance(payload.get("multiday_lr_forecast"), dict)
                    else payload.get("multiday_lr_horizon_1d_pct")
                ),
            )
            if bl:
                payload["premarket_gap_baseline"] = bl
        except Exception:
            pass

    block = build_premarket_entry_context_block(ticker, payload, strategy_name=strategy_name)
    td["premarket_entry_context_block"] = block
    td["session_phase"] = "PRE_MARKET"

    gap = payload.get("premarket_gap_pct")
    mins = payload.get("minutes_until_open")
    parts = ["Сейчас премаркет NYSE."]
    if mins is not None:
        parts.append(f"До открытия US: {mins} мин.")
    if payload.get("premarket_last") is not None:
        parts.append(f"Цена премаркета: {payload.get('premarket_last')}")
    if gap is not None:
        parts.append(f"Гэп к вчерашнему закрытию: {_fmt_pct(gap)}")
    if payload.get("prev_close") is not None:
        parts.append(f"Закрытие вчера: {payload.get('prev_close')}")
    td["premarket_note"] = " ".join(parts)

    for key in (
        "premarket_gap_pct",
        "premarket_last",
        "prev_close",
        "minutes_until_open",
        "premarket_intraday_momentum_pct",
        "premarket_gap_baseline",
        "premarket_gap_baseline_signal",
        "premarket_gap_baseline_reason",
        "ticker_open_gap_predicted_pct",
        "ticker_open_gap_predicted_source",
        "macro_predicted_sector_gap_pct",
        "macro_sector_proxy",
        "premarket_entry_recommendation",
        "premarket_suggested_limit_price",
    ):
        if payload.get(key) is not None:
            td[key] = payload[key]

    return td
