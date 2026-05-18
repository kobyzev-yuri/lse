# -*- coding: utf-8 -*-
"""
Лог премаркет-прогноза гэпа vs факт на открытии RTH (GAME_5m + sector proxy).

Запись: premarket_cron / scripts/ingest_game5m_gap_forecast.py --phase premarket
Дозаполнение open: --phase open или лениво из get_decision_5m (первые бары RTH).

Арбитр: build_game5m_gap_forecast_arbiter в analyzer_ml_arbiter.py.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DDL_PATH = "db/knowledge_pg/sql/026_game5m_gap_forecast_daily.sql"

UPSERT_PREMARKET_SQL = """
INSERT INTO game5m_gap_forecast_daily (
  trade_date, symbol, exchange,
  snapshot_ts_premarket, premarket_last, prev_close, premarket_gap_pct,
  pred_sector_gap_pct, sector_proxy, macro_risk_level, macro_equity_gap_bias,
  macro_indicators_json, source_premarket, updated_at
)
VALUES (
  :trade_date, :symbol, :exchange,
  :snapshot_ts_premarket, :premarket_last, :prev_close, :premarket_gap_pct,
  :pred_sector_gap_pct, :sector_proxy, :macro_risk_level, :macro_equity_gap_bias,
  CAST(:macro_indicators_json AS jsonb), :source_premarket, NOW()
)
ON CONFLICT (trade_date, symbol) DO UPDATE SET
  snapshot_ts_premarket = COALESCE(EXCLUDED.snapshot_ts_premarket, game5m_gap_forecast_daily.snapshot_ts_premarket),
  premarket_last = COALESCE(EXCLUDED.premarket_last, game5m_gap_forecast_daily.premarket_last),
  prev_close = COALESCE(EXCLUDED.prev_close, game5m_gap_forecast_daily.prev_close),
  premarket_gap_pct = COALESCE(EXCLUDED.premarket_gap_pct, game5m_gap_forecast_daily.premarket_gap_pct),
  pred_sector_gap_pct = COALESCE(EXCLUDED.pred_sector_gap_pct, game5m_gap_forecast_daily.pred_sector_gap_pct),
  sector_proxy = COALESCE(EXCLUDED.sector_proxy, game5m_gap_forecast_daily.sector_proxy),
  macro_risk_level = COALESCE(EXCLUDED.macro_risk_level, game5m_gap_forecast_daily.macro_risk_level),
  macro_equity_gap_bias = COALESCE(EXCLUDED.macro_equity_gap_bias, game5m_gap_forecast_daily.macro_equity_gap_bias),
  macro_indicators_json = COALESCE(EXCLUDED.macro_indicators_json, game5m_gap_forecast_daily.macro_indicators_json),
  source_premarket = COALESCE(EXCLUDED.source_premarket, game5m_gap_forecast_daily.source_premarket),
  updated_at = NOW()
"""

UPDATE_OPEN_SQL = """
UPDATE game5m_gap_forecast_daily
SET
  open_filled_ts = :open_filled_ts,
  rth_open_price = :rth_open_price,
  open_gap_pct = :open_gap_pct,
  error_pred_vs_open_pct = :error_pred_vs_open_pct,
  error_premarket_vs_open_pct = :error_premarket_vs_open_pct,
  source_open = :source_open,
  updated_at = NOW()
WHERE trade_date = :trade_date AND symbol = :symbol
  AND (open_gap_pct IS NULL OR source_open = 'backfill')
"""


def _cfg_bool(key: str, default: bool = True) -> bool:
    from config_loader import get_config_value

    raw = (get_config_value(key, "true" if default else "false") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _et_trade_date() -> date:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).date()
    except ImportError:
        return datetime.now(timezone.utc).date()


def _get_engine():
    from sqlalchemy import create_engine
    from config_loader import get_database_url

    return create_engine(get_database_url())


def ensure_gap_forecast_table(engine=None) -> None:
    eng = engine or _get_engine()
    from pathlib import Path
    from sqlalchemy import text

    root = Path(__file__).resolve().parent.parent
    ddl = (root / DDL_PATH).read_text(encoding="utf-8")
    with eng.begin() as conn:
        for part in [p.strip() for p in ddl.split(";") if p.strip()]:
            conn.execute(text(part + ";"))


def _symbols_for_log() -> List[str]:
    from services.macro_premarket_risk import evaluate_macro_premarket_risk
    from services.ticker_groups import get_tickers_game_5m

    game = [str(t).strip().upper() for t in (get_tickers_game_5m() or []) if str(t).strip()]
    macro = evaluate_macro_premarket_risk()
    proxy = (macro.get("macro_sector_proxy") or "SMH").strip().upper()
    out: List[str] = []
    seen: set[str] = set()
    for t in game + ([proxy] if proxy else []):
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def record_premarket_gap_snapshots(
    *,
    engine=None,
    trade_date: Optional[date] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    PRE_MARKET: гэп тикера + OLS sector pred (одинаковый pred на все строки дня).
    """
    if not _cfg_bool("GAME_5M_GAP_FORECAST_LOG_ENABLED", True):
        return {"mode": "skipped", "note": "GAME_5M_GAP_FORECAST_LOG_ENABLED=false"}

    from services.market_session import get_market_session_context
    from services.macro_premarket_risk import evaluate_macro_premarket_risk, get_indicator_gap_detail

    phase = (get_market_session_context().get("session_phase") or "").strip()
    if not force and phase != "PRE_MARKET":
        return {"mode": "skipped", "note": f"session_phase={phase!r}, нужен PRE_MARKET"}

    td = trade_date or _et_trade_date()
    macro = evaluate_macro_premarket_risk()
    pred = macro.get("macro_predicted_sector_gap_pct")
    proxy = (macro.get("macro_sector_proxy") or "SMH").strip().upper()
    indicators_json = json.dumps(macro.get("indicators") or {}, ensure_ascii=False)

    eng = engine or _get_engine()
    ensure_gap_forecast_table(eng)
    from sqlalchemy import text

    n_ok = 0
    errors: List[str] = []
    now_utc = datetime.now(timezone.utc)
    for sym in _symbols_for_log():
        det = get_indicator_gap_detail(sym)
        try:
            with eng.begin() as conn:
                conn.execute(
                    text(UPSERT_PREMARKET_SQL),
                    {
                        "trade_date": td,
                        "symbol": sym,
                        "exchange": "US",
                        "snapshot_ts_premarket": now_utc,
                        "premarket_last": det.get("premarket_last"),
                        "prev_close": det.get("prev_close"),
                        "premarket_gap_pct": det.get("gap_pct"),
                        "pred_sector_gap_pct": pred,
                        "sector_proxy": proxy,
                        "macro_risk_level": macro.get("risk_level"),
                        "macro_equity_gap_bias": macro.get("equity_gap_bias"),
                        "macro_indicators_json": indicators_json,
                        "source_premarket": det.get("source") or "none",
                    },
                )
            n_ok += 1
        except Exception as e:
            errors.append(f"{sym}: {e}")
            logger.warning("gap_forecast premarket %s: %s", sym, e)

    return {
        "mode": "ok",
        "trade_date": str(td),
        "symbols_written": n_ok,
        "pred_sector_gap_pct": pred,
        "sector_proxy": proxy,
        "errors": errors[:10],
    }


def record_open_gap_for_symbol(
    symbol: str,
    *,
    engine=None,
    trade_date: Optional[date] = None,
    open_gap_pct: Optional[float] = None,
    rth_open_price: Optional[float] = None,
    prev_close: Optional[float] = None,
    source_open: str = "rth_5m",
) -> bool:
    """Записать факт open_gap для одного тикера (если строка премаркета уже есть или создаётся)."""
    if not _cfg_bool("GAME_5M_GAP_FORECAST_LOG_ENABLED", True):
        return False
    sym = (symbol or "").strip().upper()
    if not sym:
        return False

    td = trade_date or _et_trade_date()
    gap = open_gap_pct
    opx = rth_open_price
    prev = prev_close

    if gap is None or opx is None:
        try:
            from services.recommend_5m import _compute_rth_open_gap_pct, fetch_5m_ohlc

            df = fetch_5m_ohlc(sym, days=3)
            g, opx2, prev2 = _compute_rth_open_gap_pct(df, sym)
            if gap is None:
                gap = g
            if opx is None:
                opx = opx2
            if prev is None:
                prev = prev2
        except Exception as e:
            logger.debug("record_open_gap fetch %s: %s", sym, e)
            return False

    if gap is None:
        return False

    eng = engine or _get_engine()
    ensure_gap_forecast_table(eng)
    from sqlalchemy import text

    with eng.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT pred_sector_gap_pct, premarket_gap_pct, open_gap_pct
                FROM game5m_gap_forecast_daily
                WHERE trade_date = :td AND symbol = :sym
                """
            ),
            {"td": td, "sym": sym},
        ).fetchone()
    if row is None:
        with eng.begin() as conn:
            conn.execute(
                text(UPSERT_PREMARKET_SQL),
                {
                    "trade_date": td,
                    "symbol": sym,
                    "exchange": "US",
                    "snapshot_ts_premarket": None,
                    "premarket_last": None,
                    "prev_close": prev,
                    "premarket_gap_pct": None,
                    "pred_sector_gap_pct": None,
                    "sector_proxy": None,
                    "macro_risk_level": None,
                    "macro_equity_gap_bias": None,
                    "macro_indicators_json": "{}",
                    "source_premarket": "open_only",
                },
            )
        row = (None, None, None)

    if row and row[2] is not None and source_open != "backfill":
        return False

    pred = float(row[0]) if row and row[0] is not None else None
    pm_gap = float(row[1]) if row and row[1] is not None else None
    err_pred = round(float(gap) - pred, 4) if pred is not None else None
    err_pm = round(float(gap) - pm_gap, 4) if pm_gap is not None else None

    with eng.begin() as conn:
        conn.execute(
            text(UPDATE_OPEN_SQL),
            {
                "trade_date": td,
                "symbol": sym,
                "open_filled_ts": datetime.now(timezone.utc),
                "rth_open_price": opx,
                "open_gap_pct": gap,
                "error_pred_vs_open_pct": err_pred,
                "error_premarket_vs_open_pct": err_pm,
                "source_open": source_open,
            },
        )
    return True


def record_open_gaps_all(
    *,
    engine=None,
    trade_date: Optional[date] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Заполнить open_gap по всем символам лога за trade_date."""
    if not _cfg_bool("GAME_5M_GAP_FORECAST_LOG_ENABLED", True):
        return {"mode": "skipped", "note": "logging disabled"}
    td = trade_date or _et_trade_date()
    n_ok = 0
    for sym in _symbols_for_log():
        if record_open_gap_for_symbol(sym, engine=engine, trade_date=td, source_open="backfill" if force else "rth_5m"):
            n_ok += 1
    return {"mode": "ok", "trade_date": str(td), "open_filled": n_ok}


def fetch_gap_forecast_rows(
    engine,
    *,
    days: int = 90,
    symbols: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    from sqlalchemy import text

    q = """
        SELECT trade_date, symbol, premarket_gap_pct, pred_sector_gap_pct, sector_proxy,
               open_gap_pct, error_pred_vs_open_pct, error_premarket_vs_open_pct,
               macro_equity_gap_bias, snapshot_ts_premarket, open_filled_ts
        FROM game5m_gap_forecast_daily
        WHERE trade_date >= CURRENT_DATE - CAST(:days AS integer)
    """
    params: Dict[str, Any] = {"days": max(1, int(days))}
    if symbols:
        q += " AND symbol = ANY(:symbols)"
        params["symbols"] = list(symbols)
    q += " ORDER BY trade_date DESC, symbol"
    with engine.connect() as conn:
        rows = conn.execute(text(q), params).mappings().all()
    return [dict(r) for r in rows]


def _mean(xs: List[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 4) if xs else None


def _rmse(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    return round(math.sqrt(sum(x * x for x in xs) / len(xs)), 4)


def pool_gap_forecast_metrics(rows: Sequence[Dict[str, Any]], *, sector_proxy: str = "SMH") -> Dict[str, Any]:
    """
    Агрегаты для арбитра: sector row pred vs open; game tickers pooled.
    """
    proxy = (sector_proxy or "SMH").strip().upper()
    complete_sector: List[Dict[str, Any]] = []
    complete_game: List[Dict[str, Any]] = []
    for r in rows:
        og = r.get("open_gap_pct")
        if og is None:
            continue
        sym = str(r.get("symbol") or "").upper()
        pred = r.get("pred_sector_gap_pct")
        if sym == proxy and pred is not None:
            complete_sector.append(r)
        elif sym != proxy and pred is not None:
            complete_game.append(r)

    def _pack(sub: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
        if not sub:
            return {"label": label, "n_complete": 0}
        err_pred = [float(x["error_pred_vs_open_pct"]) for x in sub if x.get("error_pred_vs_open_pct") is not None]
        err_pm = [
            float(x["error_premarket_vs_open_pct"]) for x in sub if x.get("error_premarket_vs_open_pct") is not None
        ]
        preds = [float(x["pred_sector_gap_pct"]) for x in sub if x.get("pred_sector_gap_pct") is not None]
        opens = [float(x["open_gap_pct"]) for x in sub if x.get("open_gap_pct") is not None]
        sign_ok = 0
        for x in sub:
            p, o = x.get("pred_sector_gap_pct"), x.get("open_gap_pct")
            if p is not None and o is not None and (float(p) >= 0) == (float(o) >= 0):
                sign_ok += 1
        n = len(sub)
        return {
            "label": label,
            "n_complete": n,
            "mean_abs_error_pred_pp": _mean([abs(e) for e in err_pred]) if err_pred else None,
            "rmse_pred_pp": _rmse(err_pred) if err_pred else None,
            "mean_signed_error_pred_pp": _mean(err_pred) if err_pred else None,
            "mean_abs_drift_premarket_pp": _mean([abs(e) for e in err_pm]) if err_pm else None,
            "sign_agreement_rate": round(sign_ok / n, 4) if n else None,
            "mean_pred_pp": _mean(preds),
            "mean_open_pp": _mean(opens),
        }

    premarket_only = sum(1 for r in rows if r.get("premarket_gap_pct") is not None and r.get("open_gap_pct") is None)
    return {
        "n_rows": len(rows),
        "n_premarket_only": premarket_only,
        "sector_proxy": proxy,
        "sector": _pack(complete_sector, f"OLS pred vs open ({proxy})"),
        "game_tickers_pooled": _pack(complete_game, "OLS pred vs open (тикеры GAME_5m)"),
    }
