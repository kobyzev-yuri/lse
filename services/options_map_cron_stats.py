"""
Статистика PCR / OI из cron options_chain_oi_snapshot для калибровки порогов Money Map.

Считает PCR volume (и OI) в том же окне ±strike_window_pct, что и Money Map,
по каждому дневному снимку (ticker, expiration_date), затем квантили по истории.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.options_chain_sentiment import _filter_contracts_for_analysis
from services.options_money_map import default_pcr_vol_thresholds

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _quantiles(values: Sequence[float], ps: Sequence[float]) -> Dict[str, Optional[float]]:
    xs = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not xs:
        return {f"p{int(p * 100)}": None for p in ps}
    out: Dict[str, Optional[float]] = {}
    n = len(xs)
    for p in ps:
        if n == 1:
            out[f"p{int(p * 100)}"] = round(xs[0], 4)
            continue
        idx = p * (n - 1)
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            val = xs[lo]
        else:
            frac = idx - lo
            val = xs[lo] * (1 - frac) + xs[hi] * frac
        out[f"p{int(p * 100)}"] = round(float(val), 4)
    return out


def compute_snapshot_flow_metrics(
    contracts: List[Dict[str, Any]],
    *,
    spot: float,
    strike_window_pct: float = 0.20,
) -> Dict[str, Any]:
    """PCR volume/OI для одного снимка — та же логика окна, что у Money Map."""
    if spot <= 0:
        return {"status": "error", "error": "spot invalid"}
    filtered, scope = _filter_contracts_for_analysis(
        contracts, spot=spot, strike_window_pct=strike_window_pct, drop_zero_oi_volume=False
    )
    call_vol = sum(int(c.get("volume") or 0) for c in filtered if c.get("contract_type") == "call")
    put_vol = sum(int(c.get("volume") or 0) for c in filtered if c.get("contract_type") == "put")
    call_oi = sum(int(c.get("open_interest") or 0) for c in filtered if c.get("contract_type") == "call")
    put_oi = sum(int(c.get("open_interest") or 0) for c in filtered if c.get("contract_type") == "put")
    pcr_vol = (put_vol / call_vol) if call_vol > 0 else None
    pcr_oi = (put_oi / call_oi) if call_oi > 0 else None
    return {
        "status": "ok",
        "spot": round(float(spot), 4),
        "pcr_volume": round(float(pcr_vol), 4) if pcr_vol is not None else None,
        "pcr_open_interest": round(float(pcr_oi), 4) if pcr_oi is not None else None,
        "put_volume": put_vol,
        "call_volume": call_vol,
        "put_open_interest": put_oi,
        "call_open_interest": call_oi,
        "contracts_in_window": scope.get("contracts_in_window"),
        "contracts_raw": scope.get("contracts_raw"),
        "strike_lo": scope.get("strike_lo"),
        "strike_hi": scope.get("strike_hi"),
    }


def suggest_pcr_thresholds_from_quantiles(
    pcr_volumes: Sequence[float],
    *,
    min_samples: int = 10,
    bullish_quantile: float = 0.25,
    bearish_quantile: float = 0.75,
) -> Dict[str, Any]:
    """p25/p75 PCR vol → предложенные bullish_max / bearish_min."""
    wire = default_pcr_vol_thresholds()
    xs = [float(v) for v in pcr_volumes if v is not None and math.isfinite(float(v))]
    n = len(xs)
    if n < max(1, int(min_samples)):
        return {
            "ready": False,
            "snapshot_count": n,
            "min_samples": int(min_samples),
            "source": "wireframe_fallback",
            "pcr_volume_bullish_max": wire["pcr_volume_bullish_max"],
            "pcr_volume_bearish_min": wire["pcr_volume_bearish_min"],
            "reason_ru": f"Мало снимков ({n} < {min_samples}); используйте wireframe или UI.",
        }
    qs = _quantiles(xs, (bullish_quantile, 0.5, bearish_quantile))
    bull_key = f"p{int(bullish_quantile * 100)}"
    bear_key = f"p{int(bearish_quantile * 100)}"
    bull = float(qs[bull_key])
    bear = float(qs[bear_key])
    if bull >= bear - 0.02:
        bear = min(3.0, bull + 0.05)
    return {
        "ready": True,
        "snapshot_count": n,
        "min_samples": int(min_samples),
        "source": f"quantile_p{int(bullish_quantile * 100)}_p{int(bearish_quantile * 100)}",
        "pcr_volume_bullish_max": round(bull, 4),
        "pcr_volume_bearish_min": round(bear, 4),
        "median_pcr_volume": qs.get("p50"),
        "reason_ru": (
            f"По {n} cron-снимкам: bullish≤p{int(bullish_quantile * 100)}, "
            f"bearish≥p{int(bearish_quantile * 100)} PCR volume в окне."
        ),
    }


def _aggregate_group_metrics(daily: List[Dict[str, Any]], *, min_samples: int = 10) -> Dict[str, Any]:
    pcr_vols = [d["pcr_volume"] for d in daily if d.get("pcr_volume") is not None]
    pcr_ois = [d["pcr_open_interest"] for d in daily if d.get("pcr_open_interest") is not None]
    vol_stats = _quantiles(pcr_vols, (0.1, 0.25, 0.5, 0.75, 0.9))
    oi_stats = _quantiles(pcr_ois, (0.1, 0.25, 0.5, 0.75, 0.9))
    if pcr_vols:
        mean_v = sum(pcr_vols) / len(pcr_vols)
        var_v = sum((x - mean_v) ** 2 for x in pcr_vols) / max(1, len(pcr_vols) - 1) if len(pcr_vols) > 1 else 0.0
        vol_stats["mean"] = round(mean_v, 4)
        vol_stats["std"] = round(math.sqrt(var_v), 4) if var_v > 0 else 0.0
        vol_stats["min"] = round(min(pcr_vols), 4)
        vol_stats["max"] = round(max(pcr_vols), 4)
    return {
        "snapshot_count": len(daily),
        "pcr_volume_stats": vol_stats,
        "pcr_oi_stats": oi_stats,
        "suggested_thresholds": suggest_pcr_thresholds_from_quantiles(pcr_vols, min_samples=min_samples),
    }


def _rows_to_daily_metrics(
    rows: List[Any],
    *,
    strike_window_pct: float,
) -> List[Dict[str, Any]]:
    """rows: SQL tuples (snapshot_date, strike, contract_type, oi, vol, spot)."""
    if not rows:
        return []
    spot_vals = [float(r[7]) for r in rows if r[7] is not None]
    spot_f = spot_vals[0] if spot_vals else 0.0
    contracts = [
        {
            "strike": float(r[3]),
            "contract_type": str(r[4]),
            "open_interest": int(r[5] or 0),
            "volume": int(r[6] or 0),
        }
        for r in rows
    ]
    snap = rows[0][0]
    snap_s = snap.isoformat() if hasattr(snap, "isoformat") else str(snap)[:10]
    m = compute_snapshot_flow_metrics(contracts, spot=spot_f, strike_window_pct=strike_window_pct)
    if m.get("status") != "ok":
        return []
    return [
        {
            "snapshot_date": snap_s,
            **{k: m[k] for k in ("spot", "pcr_volume", "pcr_open_interest", "put_volume", "call_volume")},
        }
    ]


def load_cron_snapshot_groups(
    *,
    days: int = 90,
    tickers: Optional[List[str]] = None,
) -> List[Tuple[str, str, str, List[Any]]]:
    """
    Возвращает [(ticker, expiration_date, snapshot_date, rows), ...].
    rows — сырые строки БД для одного снимка.
    """
    from sqlalchemy import text
    from report_generator import get_engine

    since = date.today() - timedelta(days=max(1, int(days)))
    q = """
        SELECT snapshot_date, ticker, expiration_date::text, strike, contract_type,
               open_interest, volume, spot
        FROM options_chain_oi_snapshot
        WHERE snapshot_date >= :since
    """
    params: Dict[str, Any] = {"since": since}
    if tickers:
        q += " AND ticker = ANY(:tickers)"
        params["tickers"] = [t.strip().upper() for t in tickers if t.strip()]
    q += " ORDER BY ticker, expiration_date, snapshot_date DESC, strike, contract_type"

    with get_engine().connect() as conn:
        raw = conn.execute(text(q), params).fetchall()

    grouped: Dict[Tuple[str, str, str], List[Any]] = {}
    for r in raw:
        snap = r[0]
        snap_s = snap.isoformat() if hasattr(snap, "isoformat") else str(snap)[:10]
        key = (str(r[1]).upper(), str(r[2]), snap_s)
        grouped.setdefault(key, []).append(r)

    return [(k[0], k[1], k[2], v) for k, v in sorted(grouped.items())]


def build_options_map_cron_stats_report(
    *,
    days: int = 90,
    tickers: Optional[List[str]] = None,
    strike_window_pct: float = 0.20,
    min_samples: int = 10,
    daily_series_limit: int = 45,
) -> Dict[str, Any]:
    """Отчёт для scripts/analyze_options_map_cron_stats.py."""
    wire = default_pcr_vol_thresholds()
    try:
        groups = load_cron_snapshot_groups(days=days, tickers=tickers)
    except Exception as e:
        logger.exception("load cron snapshots: %s", e)
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at_utc": _utc_now_iso(),
            "status": "error",
            "error": str(e),
        }

    by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for sym, exp, _snap, rows in groups:
        daily = _rows_to_daily_metrics(rows, strike_window_pct=strike_window_pct)
        if not daily:
            continue
        by_key.setdefault((sym, exp), []).extend(daily)

    series: List[Dict[str, Any]] = []
    for (sym, exp), daily in sorted(by_key.items()):
        daily_sorted = sorted(daily, key=lambda d: str(d.get("snapshot_date") or ""), reverse=True)
        agg = _aggregate_group_metrics(daily_sorted, min_samples=min_samples)
        series.append(
            {
                "ticker": sym,
                "expiration_date": exp,
                **agg,
                "daily_series": daily_sorted[: max(0, daily_series_limit)],
                "wireframe_comparison": {
                    "wireframe_bullish_max": wire["pcr_volume_bullish_max"],
                    "wireframe_bearish_min": wire["pcr_volume_bearish_min"],
                    "delta_bullish_vs_wireframe": (
                        round(
                            float(agg["suggested_thresholds"]["pcr_volume_bullish_max"])
                            - float(wire["pcr_volume_bullish_max"]),
                            4,
                        )
                        if agg["suggested_thresholds"].get("ready")
                        else None
                    ),
                    "delta_bearish_vs_wireframe": (
                        round(
                            float(agg["suggested_thresholds"]["pcr_volume_bearish_min"])
                            - float(wire["pcr_volume_bearish_min"]),
                            4,
                        )
                        if agg["suggested_thresholds"].get("ready")
                        else None
                    ),
                },
            }
        )

    ready = [s for s in series if s.get("suggested_thresholds", {}).get("ready")]
    by_ticker: Dict[str, List[Dict[str, Any]]] = {}
    for s in series:
        by_ticker.setdefault(s["ticker"], []).append(s)

    ticker_rollup: List[Dict[str, Any]] = []
    for sym, items in sorted(by_ticker.items()):
        best = max(items, key=lambda x: int(x.get("snapshot_count") or 0))
        ticker_rollup.append(
            {
                "ticker": sym,
                "best_expiration_date": best["expiration_date"],
                "snapshot_count": best["snapshot_count"],
                "suggested_thresholds": best["suggested_thresholds"],
                "pcr_volume_stats": best.get("pcr_volume_stats"),
            }
        )

    reasons: List[str] = []
    if not series:
        reasons.append("Нет строк в options_chain_oi_snapshot за окно — дождитесь cron snapshot_options_chain_oi.")
    elif not ready:
        reasons.append(
            f"Ни одна пара ticker+exp не набрала ≥{min_samples} снимков; квантили пока не применяются."
        )
    else:
        reasons.append(
            f"Готово {len(ready)}/{len(series)} серий с квантильными порогами (p25/p75 PCR vol)."
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": _utc_now_iso(),
        "status": "ok",
        "days": int(days),
        "strike_window_pct": float(strike_window_pct),
        "min_samples_for_quantile_thresholds": int(min_samples),
        "wireframe_defaults": wire,
        "method_ru": (
            "По каждому cron-снимку: PCR volume = put_vol/call_vol в окне ±{:.0f}% spot (как Money Map). "
            "Пороги: p25 → bullish_max, p75 → bearish_min; иначе wireframe."
        ).format(strike_window_pct * 100),
        "snapshot_groups_loaded": len(groups),
        "ticker_exp_series": series,
        "ticker_rollup": ticker_rollup,
        "summary": {
            "series_total": len(series),
            "series_ready_for_quantiles": len(ready),
            "tickers": sorted(by_ticker.keys()),
        },
        "recommendation_ru": reasons,
    }


def default_report_path(project_root: Optional[Any] = None) -> Any:
    from pathlib import Path

    if Path("/app/logs/ml/ml_data_quality").exists():
        return Path("/app/logs/ml/ml_data_quality/last_options_map_cron_stats.json")
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "last_options_map_cron_stats.json"


def load_cron_stats_artifact(*, path: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """Читает last_options_map_cron_stats.json; None если файла нет или JSON битый."""
    import json
    from pathlib import Path

    p = Path(path) if path is not None else default_report_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.debug("load_cron_stats_artifact %s: %s", p, e)
        return None


def _recommendation_from_series(
    series: Dict[str, Any],
    report: Dict[str, Any],
    *,
    match_type: str,
    requested_expiration_date: Optional[str] = None,
) -> Dict[str, Any]:
    st = series.get("suggested_thresholds") if isinstance(series.get("suggested_thresholds"), dict) else {}
    vol_stats = series.get("pcr_volume_stats") if isinstance(series.get("pcr_volume_stats"), dict) else {}
    wire = series.get("wireframe_comparison") if isinstance(series.get("wireframe_comparison"), dict) else {}
    eff_exp = str(series.get("expiration_date") or "")
    ready = bool(st.get("ready"))
    status = "ok" if ready else "not_ready"
    reason = st.get("reason_ru") or (
        f"Недостаточно cron-снимков ({series.get('snapshot_count', 0)}); пороги wireframe."
    )
    if match_type == "ticker_rollup" and requested_expiration_date and eff_exp != requested_expiration_date:
        reason = (
            f"Для exp {requested_expiration_date} мало истории; показана серия с max снимков: {eff_exp}. "
            + str(reason)
        )
    return {
        "status": status,
        "generated_at_utc": report.get("generated_at_utc"),
        "artifact_days": report.get("days"),
        "strike_window_pct": report.get("strike_window_pct"),
        "match_type": match_type,
        "ticker": series.get("ticker"),
        "requested_expiration_date": requested_expiration_date,
        "effective_expiration_date": eff_exp,
        "snapshot_count": int(series.get("snapshot_count") or 0),
        "min_samples_for_quantile_thresholds": report.get("min_samples_for_quantile_thresholds"),
        "suggested_thresholds": st,
        "pcr_volume_stats": vol_stats,
        "wireframe_comparison": wire,
        "method_ru": report.get("method_ru"),
        "reason_ru": reason,
    }


def lookup_cron_pcr_recommendation(
    ticker: str,
    expiration_date: str,
    *,
    artifact: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Рекомендованные пороги PCR из cron-артефакта для Money Map UI.
    exact_exp → ticker_exp_series; иначе ticker_rollup (серия с max снимков).
    """
    sym = (ticker or "").strip().upper()
    exp = (expiration_date or "").strip()
    report = artifact if artifact is not None else load_cron_stats_artifact()
    if not report or report.get("status") != "ok":
        return {
            "status": "missing",
            "ticker": sym or None,
            "requested_expiration_date": exp or None,
            "reason_ru": (
                "Артефакт cron ещё не создан. Запустите scripts/analyze_options_map_cron_stats.py "
                "или дождитесь cron после OI snapshot."
            ),
        }
    if not sym or not exp:
        return {
            "status": "missing",
            "ticker": sym or None,
            "requested_expiration_date": exp or None,
            "generated_at_utc": report.get("generated_at_utc"),
            "reason_ru": "Нужны ticker и expiration_date.",
        }

    for s in report.get("ticker_exp_series") or []:
        if str(s.get("ticker", "")).upper() == sym and str(s.get("expiration_date", "")) == exp:
            return _recommendation_from_series(
                s, report, match_type="exact_exp", requested_expiration_date=exp
            )

    for r in report.get("ticker_rollup") or []:
        if str(r.get("ticker", "")).upper() == sym:
            return _recommendation_from_series(
                {
                    "ticker": sym,
                    "expiration_date": r.get("best_expiration_date"),
                    "snapshot_count": r.get("snapshot_count"),
                    "suggested_thresholds": r.get("suggested_thresholds"),
                    "pcr_volume_stats": r.get("pcr_volume_stats"),
                    "wireframe_comparison": None,
                },
                report,
                match_type="ticker_rollup",
                requested_expiration_date=exp,
            )

    return {
        "status": "missing",
        "ticker": sym,
        "requested_expiration_date": exp,
        "generated_at_utc": report.get("generated_at_utc"),
        "reason_ru": f"Нет cron-статистики для {sym} в артефакте.",
    }
