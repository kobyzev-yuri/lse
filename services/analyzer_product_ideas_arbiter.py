"""
Арбитр эффективности продуктовых идей GAME_5m по закрытым сделкам (context_json на входе).

Вердикты по идее: insufficient_data | keep | caution | remove
overall: not_ready | caution | ready (ни одна идея не remove при достаточных данных)

Не меняет config.env — только отчёт для оператора и LLM.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from services.product_ideas_registry import PRODUCT_IDEAS


def _ctx(entry_ctx: Any) -> Dict[str, Any]:
    if isinstance(entry_ctx, dict):
        return entry_ctx
    return {}


def _mean(xs: List[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 4) if xs else None


def _verdict_from_pnl_delta(
    avoid_mean: Optional[float],
    allow_mean: Optional[float],
    n_avoid: int,
    n_allow: int,
    min_n: int,
) -> tuple[str, str]:
    if n_avoid < min_n or n_allow < min_n:
        return "insufficient_data", f"мало сделок (AVOID n={n_avoid}, ALLOW/CAUTION n={n_allow}, нужно ≥{min_n})"
    if avoid_mean is None or allow_mean is None:
        return "insufficient_data", "нет PnL"
    # AVOID должен отсекать худшие входы: средний PnL у AVOID не должен быть лучше ALLOW
    if avoid_mean > allow_mean + 0.15:
        return "remove", f"AVOID входы в среднем лучше ({avoid_mean:+.2f}% vs {allow_mean:+.2f}%) — сигнал вреден"
    if avoid_mean < allow_mean - 0.25:
        return "keep", f"AVOID входы слабее ({avoid_mean:+.2f}% vs {allow_mean:+.2f}%) — фильтр полезен"
    return "caution", f"различие слабое ({avoid_mean:+.2f}% vs {allow_mean:+.2f}%)"


def _review_macro_vix_forex_risk(
    effects: Sequence[Any],
    entry_by_trade: Dict[int, Dict[str, Any]],
    min_n: int,
) -> Dict[str, Any]:
    pnl_avoid: List[float] = []
    pnl_allow: List[float] = []
    pnl_bias_up: List[float] = []
    pnl_bias_down: List[float] = []
    early_up: List[float] = []
    early_other: List[float] = []
    n_with_macro = 0

    for e in effects:
        ctx = entry_by_trade.get(int(e.trade_id), {})
        lvl = (ctx.get("macro_risk_level") or "").strip().upper()
        bias = (ctx.get("macro_equity_gap_bias") or "").strip().upper()
        if lvl or bias or ctx.get("macro_indicators"):
            n_with_macro += 1
        rp = float(e.realized_pct)
        adv = (ctx.get("entry_advice") or "").strip().upper()
        if lvl == "AVOID" or adv == "AVOID":
            pnl_avoid.append(rp)
        else:
            pnl_allow.append(rp)
        if bias == "UP":
            pnl_bias_up.append(rp)
        elif bias == "DOWN":
            pnl_bias_down.append(rp)
        sig = (getattr(e, "exit_signal", None) or "").strip().upper()
        if sig == "TIME_EXIT_EARLY":
            if bias == "UP":
                early_up.append(rp)
            else:
                early_other.append(rp)

    v, rationale = _verdict_from_pnl_delta(
        _mean(pnl_avoid), _mean(pnl_allow), len(pnl_avoid), len(pnl_allow), min_n
    )
    early_note = ""
    if len(early_up) >= 3 and len(early_other) >= 3:
        mu, mo = _mean(early_up), _mean(early_other)
        if mu is not None and mo is not None and mu > mo + 0.3:
            early_note = (
                f" TIME_EXIT_EARLY при bias UP в среднем лучше ({mu:+.2f}% vs {mo:+.2f}%) — "
                "кандидат на defer до open (идея macro_defer_time_exit_early)."
            )
        rationale += early_note

    return {
        "idea_id": "macro_vix_forex_risk",
        "verdict": v,
        "rationale_ru": rationale,
        "n_trades_with_macro_context": n_with_macro,
        "buckets": {
            "avoid": {"n": len(pnl_avoid), "mean_realized_pct": _mean(pnl_avoid)},
            "allow_caution": {"n": len(pnl_allow), "mean_realized_pct": _mean(pnl_allow)},
            "bias_up": {"n": len(pnl_bias_up), "mean_realized_pct": _mean(pnl_bias_up)},
            "bias_down": {"n": len(pnl_bias_down), "mean_realized_pct": _mean(pnl_bias_down)},
            "time_exit_early_bias_up": {"n": len(early_up), "mean_realized_pct": _mean(early_up)},
            "time_exit_early_other": {"n": len(early_other), "mean_realized_pct": _mean(early_other)},
        },
        "action_ru": (
            "Оставить в sandbox и копить сделки с macro_* в context_json."
            if v == "insufficient_data"
            else (
                "Отключить GAME_5M_MACRO_RISK_ENABLED / упростить пороги."
                if v == "remove"
                else "Можно оставить; ужесточать только при повторном keep на 2+ окнах."
            )
        ),
    }


def _review_macro_predicted_gap(
    effects: Sequence[Any],
    entry_by_trade: Dict[int, Dict[str, Any]],
    min_n: int,
) -> Dict[str, Any]:
    pairs: List[tuple[float, float]] = []
    for e in effects:
        ctx = entry_by_trade.get(int(e.trade_id), {})
        pred = ctx.get("macro_predicted_sector_gap_pct")
        if pred is None:
            continue
        try:
            p = float(pred)
        except (TypeError, ValueError):
            continue
        pairs.append((p, float(e.realized_pct)))

    if len(pairs) < min_n:
        return {
            "idea_id": "macro_predicted_sector_gap",
            "verdict": "insufficient_data",
            "rationale_ru": f"мало сделок с macro_predicted_sector_gap_pct (n={len(pairs)}, нужно ≥{min_n})",
            "n_pairs": len(pairs),
            "action_ru": "Включить GAME_5M_MACRO_PREDICT_SECTOR_GAP_ENABLED и накопить 2–4 недели.",
        }
    preds = [x[0] for x in pairs]
    realized = [x[1] for x in pairs]
    # знак: доля случаев когда sign(pred) совпал со sign(realized)
    agree = sum(1 for p, r in pairs if (p >= 0) == (r >= 0)) / len(pairs)
    hi = [r for p, r in pairs if p >= 0.3]
    lo = [r for p, r in pairs if p <= -0.3]
    rationale = (
        f"n={len(pairs)}, доля совпадения знака pred/realized PnL={agree:.0%}. "
        f"При pred≥+0.3%: mean PnL={_mean(hi)} (n={len(hi)}); при pred≤−0.3%: mean PnL={_mean(lo)} (n={len(lo)})."
    )
    if agree >= 0.55 and len(hi) >= 5 and (_mean(hi) or 0) > (_mean(lo) or -999) + 0.2:
        verdict = "keep"
    elif agree < 0.48:
        verdict = "remove"
    else:
        verdict = "caution"
    return {
        "idea_id": "macro_predicted_sector_gap",
        "verdict": verdict,
        "rationale_ru": rationale,
        "sign_agreement_rate": round(agree, 4),
        "mean_pred": _mean(preds),
        "action_ru": (
            "Показывать на карточке; не блокировать автовход до калибровки."
            if verdict != "remove"
            else "Отключить GAME_5M_MACRO_PREDICT_SECTOR_GAP_ENABLED."
        ),
    }


def build_product_ideas_arbiter(
    report: Dict[str, Any],
    *,
    effects: Sequence[Any],
    closed_trades: Sequence[Any],
) -> Dict[str, Any]:
    """Сводка по идеям из реестра для JSON отчёта анализатора."""
    su = (report.get("meta") or {}).get("strategy") or "GAME_5M"
    if str(su).strip().upper() == "PORTFOLIO":
        return {
            "mode": "skipped",
            "note": "Продуктовые идеи GAME_5m — только для GAME_5M / ALL.",
        }

    from services.deal_params_5m import normalize_entry_context

    entry_by_trade: Dict[int, Dict[str, Any]] = {}
    for t in closed_trades:
        tid = int(getattr(t, "trade_id", 0) or 0)
        if not tid:
            continue
        entry_by_trade[tid] = normalize_entry_context(getattr(t, "context_json", None))

    reviews: List[Dict[str, Any]] = []
    meta_idea = PRODUCT_IDEAS.get("macro_vix_forex_risk") or {}
    reviews.append(
        _review_macro_vix_forex_risk(
            effects,
            entry_by_trade,
            int(meta_idea.get("min_trades_per_bucket") or 8),
        )
    )
    meta_pred = PRODUCT_IDEAS.get("macro_predicted_sector_gap") or {}
    reviews.append(
        _review_macro_predicted_gap(
            effects,
            entry_by_trade,
            int(meta_pred.get("min_trades_per_bucket") or 12),
        )
    )

    planned = [
        {
            "idea_id": "macro_defer_time_exit_early",
            "verdict": "planned",
            "rationale_ru": "Не внедрено; после predicted_gap — контрфакт в time_exit_early_review.",
            "action_ru": "Сначала накопить macro_* + прогноз гэпа; затем пилот defer с логом в exit_context.",
        }
    ]
    reviews.extend(planned)

    lines: List[str] = ["**Арбитр продуктовых идей (песочница → прод только по данным):**"]
    verdicts: Dict[str, str] = {}
    for r in reviews:
        iid = r.get("idea_id", "?")
        v = str(r.get("verdict", "—"))
        verdicts[iid] = v
        lines.append(f"• {iid}: **{v}** — {r.get('rationale_ru', '')}")
        act = r.get("action_ru")
        if act:
            lines.append(f"  → {act}")

    overall = "caution"
    if any(v == "remove" for v in verdicts.values()):
        overall = "not_ready"
    elif all(v in ("keep", "planned", "skipped") for v in verdicts.values()) and any(
        v == "keep" for v in verdicts.values()
    ):
        overall = "ready"
    if all(v == "insufficient_data" for v in verdicts.values() if v != "planned"):
        overall = "not_ready"

    lines.append("")
    lines.append(
        f"Итог: **{overall}**. Идеи со статусом remove/not_ready не переводить в прод; "
        "повторять /analyzer раз в 3–7 дней на растущем n."
    )

    return {
        "mode": "ok",
        "overall_verdict": overall,
        "verdicts": verdicts,
        "reviews": reviews,
        "registry": {k: v.get("title_ru") for k, v in PRODUCT_IDEAS.items()},
        "conclusion_ru": "\n".join(lines),
    }
