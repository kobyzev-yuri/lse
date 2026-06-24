"""
Shadow-отчёт по options_sentiment gate (фаза 4).

Сравнивает CORE=BUY/STRONG_BUY с gate_hint would_downgrade / would_signal
на закрытых GAME_5M сделках и в текущем live-срезе тикеров.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from services.deal_params_5m import normalize_entry_context

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BULL_CORE = frozenset({"BUY", "STRONG_BUY"})
DEFAULT_FOCUS_TICKERS = ("SNDK", "MU", "LITE")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%M:%SZ")


def _core_decision_from_context(ctx: Dict[str, Any]) -> str:
    core = ctx.get("technical_decision_core") or ctx.get("decision")
    snap = ctx.get("decision_snapshot")
    if isinstance(snap, dict):
        core = snap.get("core_decision") or core
        legacy = snap.get("legacy")
        if not core and isinstance(legacy, dict):
            core = legacy.get("technical_decision_core")
    return str(core or "HOLD").strip().upper()


def extract_options_gate_from_context(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """gate_hint из options_sentiment или contribution decision_snapshot."""
    opts = ctx.get("options_sentiment")
    if isinstance(opts, dict):
        hint = opts.get("gate_hint")
        if hint or opts.get("status"):
            return {
                "has_context": True,
                "source": "options_sentiment",
                "status": opts.get("status"),
                "gate_hint": hint,
                "sentiment_label": opts.get("sentiment_label"),
                "sentiment_score": opts.get("sentiment_score"),
                "pcr_volume": opts.get("pcr_volume"),
            }

    snap = ctx.get("decision_snapshot")
    if isinstance(snap, dict):
        contribs = snap.get("contributions") if isinstance(snap.get("contributions"), list) else []
        opt = next((c for c in contribs if c.get("contour_id") == "options_sentiment"), None)
        if isinstance(opt, dict):
            metrics = opt.get("metrics") if isinstance(opt.get("metrics"), dict) else {}
            hint = metrics.get("gate_hint")
            return {
                "has_context": True,
                "source": "decision_snapshot",
                "status": metrics.get("status") or "ok",
                "gate_hint": hint,
                "sentiment_label": metrics.get("sentiment_label"),
                "sentiment_score": metrics.get("sentiment_score"),
                "pcr_volume": metrics.get("pcr_volume"),
                "would_downgrade": metrics.get("would_downgrade"),
                "would_signal": metrics.get("would_signal"),
            }

    return {
        "has_context": False,
        "source": None,
        "status": None,
        "gate_hint": None,
        "sentiment_label": None,
        "sentiment_score": None,
        "pcr_volume": None,
    }


def _rate(num: int, den: int) -> Optional[float]:
    if den <= 0:
        return None
    return round(num / den, 4)


def _classify_downgrade_outcome(realized_pct: Optional[float], gate_hint: Optional[str]) -> str:
    if gate_hint != "would_downgrade":
        return "n/a"
    if realized_pct is None:
        return "unknown"
    if realized_pct > 0:
        return "false_positive"  # gate отрезал бы хороший BUY
    if realized_pct <= 0:
        return "true_positive"  # gate совпал с убытком
    return "unknown"


def _aggregate_ticker_stats(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        tkr = str(row.get("ticker") or "?").upper()
        bucket = out.setdefault(
            tkr,
            {
                "trades": 0,
                "with_options_context": 0,
                "bull_core": 0,
                "would_downgrade": 0,
                "would_signal": 0,
                "false_positive_downgrade": 0,
                "true_positive_downgrade": 0,
            },
        )
        bucket["trades"] += 1
        if row.get("has_options_context"):
            bucket["with_options_context"] += 1
        if row.get("core_decision") in BULL_CORE:
            bucket["bull_core"] += 1
        gh = row.get("gate_hint")
        if gh == "would_downgrade":
            bucket["would_downgrade"] += 1
            oc = row.get("downgrade_outcome")
            if oc == "false_positive":
                bucket["false_positive_downgrade"] += 1
            elif oc == "true_positive":
                bucket["true_positive_downgrade"] += 1
        elif gh == "would_signal":
            bucket["would_signal"] += 1
    for tkr, b in out.items():
        b["bull_downgrade_rate"] = _rate(b["would_downgrade"], b["bull_core"])
        b["false_positive_rate_on_downgrades"] = _rate(
            b["false_positive_downgrade"],
            max(1, b["false_positive_downgrade"] + b["true_positive_downgrade"]),
        )
        out[tkr] = b
    return out


def _build_closed_trades_section(
    closed: List[Any],
    effects_by_id: Dict[int, Any],
    *,
    limit_rows: int,
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for t in closed:
        es = (getattr(t, "entry_strategy", None) or "").strip().upper()
        if es != "GAME_5M":
            continue
        try:
            tid = int(getattr(t, "trade_id", 0) or 0)
        except (TypeError, ValueError):
            tid = 0
        ctx = normalize_entry_context(getattr(t, "context_json", None))
        core = _core_decision_from_context(ctx)
        gate = extract_options_gate_from_context(ctx)
        eff = effects_by_id.get(tid)
        realized = round(float(eff.realized_pct), 4) if eff else None
        gh = gate.get("gate_hint")
        row = {
            "trade_id": tid or None,
            "ticker": getattr(t, "ticker", None),
            "entry_ts": getattr(t, "ts", None),
            "core_decision": core,
            "effective_decision": ctx.get("technical_decision_effective") or ctx.get("decision"),
            "has_options_context": bool(gate.get("has_context")),
            "options_source": gate.get("source"),
            "gate_hint": gh,
            "sentiment_label": gate.get("sentiment_label"),
            "sentiment_score": gate.get("sentiment_score"),
            "realized_pct": realized,
            "win": bool(realized > 0) if realized is not None else None,
            "downgrade_outcome": _classify_downgrade_outcome(realized, gh),
        }
        rows.append(row)

    bull_rows = [r for r in rows if r["core_decision"] in BULL_CORE]
    with_ctx = [r for r in rows if r["has_options_context"]]
    bull_down = [r for r in bull_rows if r["gate_hint"] == "would_downgrade"]
    fp = [r for r in bull_down if r["downgrade_outcome"] == "false_positive"]
    tp = [r for r in bull_down if r["downgrade_outcome"] == "true_positive"]

    rows_sorted = sorted(
        rows,
        key=lambda r: (str(r.get("entry_ts") or ""), int(r.get("trade_id") or 0)),
        reverse=True,
    )

    return {
        "total_closed": len(rows),
        "with_options_context": len(with_ctx),
        "missing_options_context": len(rows) - len(with_ctx),
        "bull_core_total": len(bull_rows),
        "bull_with_would_downgrade": len(bull_down),
        "bull_with_would_signal": sum(1 for r in bull_rows if r["gate_hint"] == "would_signal"),
        "bull_would_downgrade_rate": _rate(len(bull_down), len(bull_rows)),
        "downgrade_false_positive": len(fp),
        "downgrade_true_positive": len(tp),
        "downgrade_unknown": len(bull_down) - len(fp) - len(tp),
        "false_positive_rate_on_downgrades": _rate(len(fp), len(bull_down)),
        "by_ticker": _aggregate_ticker_stats(rows),
        "recent_rows": rows_sorted[: max(0, limit_rows)],
        "description": (
            "Закрытые GAME_5M: CORE=BUY/STRONG_BUY на входе vs options gate_hint в context_json. "
            "false_positive = would_downgrade, но сделка в плюсе (ложное отсечение)."
        ),
    }


def _build_live_scan_section(
    tickers: List[str],
    *,
    days: int,
    include_no_data: bool,
) -> Dict[str, Any]:
    from services.recommend_5m import get_decision_5m

    rows: List[Dict[str, Any]] = []
    for tkr in tickers:
        sym = (tkr or "").strip().upper()
        if not sym:
            continue
        try:
            d5 = get_decision_5m(sym, days=days, use_llm_news=False)
        except Exception as e:
            logger.debug("live scan %s: %s", sym, e)
            d5 = None
        if not d5:
            if include_no_data:
                rows.append({"ticker": sym, "status": "no_data"})
            continue
        core = _core_decision_from_context(d5)
        opts = d5.get("options_sentiment") if isinstance(d5.get("options_sentiment"), dict) else {}
        gate = extract_options_gate_from_context(d5)
        rows.append(
            {
                "ticker": sym,
                "status": "ok",
                "core_decision": core,
                "effective_decision": d5.get("technical_decision_effective") or d5.get("decision"),
                "gate_hint": gate.get("gate_hint") or opts.get("gate_hint"),
                "sentiment_label": gate.get("sentiment_label") or opts.get("sentiment_label"),
                "sentiment_score": gate.get("sentiment_score") or opts.get("sentiment_score"),
                "options_status": opts.get("status"),
            }
        )

    bull = [r for r in rows if r.get("core_decision") in BULL_CORE]
    down = [r for r in bull if r.get("gate_hint") == "would_downgrade"]
    sig = [r for r in bull if r.get("gate_hint") == "would_signal"]

    return {
        "tickers_scanned": len(rows),
        "bull_core_now": len(bull),
        "would_downgrade_now": len(down),
        "would_signal_now": len(sig),
        "bull_would_downgrade_rate": _rate(len(down), len(bull)),
        "rows": rows,
        "description": (
            "Текущий срез get_decision_5m по watchlist: сколько CORE=BUY/STRONG_BUY "
            "получили бы shadow-downgrade от options gate (без изменения prod-входа)."
        ),
    }


def _recommendation(
    closed: Dict[str, Any],
    *,
    focus_tickers: Tuple[str, ...],
    min_trades_with_context: int,
    max_false_positive_rate: float,
) -> Dict[str, Any]:
    reasons: List[str] = []
    ready = True

    with_ctx = int(closed.get("with_options_context") or 0)
    if with_ctx < min_trades_with_context:
        ready = False
        reasons.append(
            f"Мало сделок с options в context_json ({with_ctx} < {min_trades_with_context}); "
            "нужно накопить cron/входы после деплоя фаз 1–2."
        )

    fp_rate = closed.get("false_positive_rate_on_downgrades")
    if fp_rate is not None and fp_rate > max_false_positive_rate:
        ready = False
        reasons.append(
            f"Высокая доля ложных downgrade на закрытых ({fp_rate:.0%} > {max_false_positive_rate:.0%})."
        )

    by_ticker = closed.get("by_ticker") if isinstance(closed.get("by_ticker"), dict) else {}
    for tkr in focus_tickers:
        b = by_ticker.get(tkr.upper())
        if not b:
            continue
        fp = int(b.get("false_positive_downgrade") or 0)
        wd = int(b.get("would_downgrade") or 0)
        if wd >= 2 and fp >= wd:
            ready = False
            reasons.append(f"{tkr}: доминируют ложные downgrade ({fp}/{wd}).")

    if ready and not reasons:
        reasons.append("Пороги в норме для обсуждения apply; финальное решение — вручную.")

    return {
        "ready_for_apply_discussion": ready,
        "gate_mode_note": "DECISION_STACK_OPTIONS_SENTIMENT_GATE_MODE должен оставаться log_only до apply.",
        "reasons": reasons,
    }


def build_options_gate_shadow_report(
    *,
    days: int = 28,
    focus_tickers: Optional[List[str]] = None,
    limit_rows: int = 30,
    live_scan: bool = True,
    live_days: int = 5,
) -> Dict[str, Any]:
    from services.ticker_groups import get_tickers_game_5m
    from services.trade_effectiveness_analyzer import (
        _estimate_trade_effects,
        _load_closed_trades,
        _prepare_ohlc_cache,
    )

    days = max(1, min(90, int(days)))
    focus = tuple((t or "").strip().upper() for t in (focus_tickers or DEFAULT_FOCUS_TICKERS) if t)

    closed_raw = _load_closed_trades(days=days, strategy_name="GAME_5M")
    tickers = sorted({str(getattr(t, "ticker", "")) for t in closed_raw if getattr(t, "ticker", None)})
    cache = _prepare_ohlc_cache(tickers=tickers, days=days + 5)
    effects = _estimate_trade_effects(closed_raw, cache)
    by_id = {}
    for e in effects:
        try:
            by_id[int(e.trade_id)] = e
        except (TypeError, ValueError):
            continue

    closed_sec = _build_closed_trades_section(closed_raw, by_id, limit_rows=limit_rows)

    live_sec: Optional[Dict[str, Any]] = None
    if live_scan:
        scan_tickers = list(get_tickers_game_5m() or [])
        live_sec = _build_live_scan_section(scan_tickers, days=live_days, include_no_data=False)

    rec = _recommendation(
        closed_sec,
        focus_tickers=focus,
        min_trades_with_context=5,
        max_false_positive_rate=0.5,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": _utc_now_iso(),
        "days": days,
        "focus_tickers": list(focus),
        "closed_trades": closed_sec,
        "live_scan": live_sec,
        "recommendation": rec,
    }


def default_report_path(project_root: Optional[Any] = None) -> Any:
    from pathlib import Path

    if Path("/app/logs/ml/ml_data_quality").exists():
        return Path("/app/logs/ml/ml_data_quality/last_options_gate_shadow.json")
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "last_options_gate_shadow.json"
