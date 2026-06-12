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
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DDL_PATH = "db/knowledge_pg/sql/026_game5m_gap_forecast_daily.sql"
DDL_MIGRATION_TICKER = "db/knowledge_pg/sql/027_game5m_gap_forecast_ticker_pred.sql"
NYSE_OPEN_TIME = time(9, 30)

UPSERT_PREMARKET_SQL = """
INSERT INTO game5m_gap_forecast_daily (
  trade_date, symbol, exchange,
  snapshot_ts_premarket, premarket_last, prev_close, premarket_gap_pct,
  pred_sector_gap_pct, sector_proxy, macro_risk_level, macro_equity_gap_bias,
  macro_indicators_json, source_premarket,
  pred_ticker_gap_pct, pred_ticker_source, pred_ticker_model_version,
  updated_at
)
VALUES (
  :trade_date, :symbol, :exchange,
  :snapshot_ts_premarket, :premarket_last, :prev_close, :premarket_gap_pct,
  :pred_sector_gap_pct, :sector_proxy, :macro_risk_level, :macro_equity_gap_bias,
  CAST(:macro_indicators_json AS jsonb), :source_premarket,
  :pred_ticker_gap_pct, :pred_ticker_source, :pred_ticker_model_version,
  NOW()
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
  pred_ticker_gap_pct = COALESCE(EXCLUDED.pred_ticker_gap_pct, game5m_gap_forecast_daily.pred_ticker_gap_pct),
  pred_ticker_source = COALESCE(EXCLUDED.pred_ticker_source, game5m_gap_forecast_daily.pred_ticker_source),
  pred_ticker_model_version = COALESCE(EXCLUDED.pred_ticker_model_version, game5m_gap_forecast_daily.pred_ticker_model_version),
  updated_at = NOW()
WHERE game5m_gap_forecast_daily.open_gap_pct IS NULL
"""

UPDATE_OPEN_SQL = """
UPDATE game5m_gap_forecast_daily
SET
  open_filled_ts = :open_filled_ts,
  rth_open_price = :rth_open_price,
  open_gap_pct = :open_gap_pct,
  error_pred_vs_open_pct = :error_pred_vs_open_pct,
  error_premarket_vs_open_pct = :error_premarket_vs_open_pct,
  error_pred_ticker_vs_open_pct = :error_pred_ticker_vs_open_pct,
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


def _snapshot_is_preopen(row: Dict[str, Any]) -> bool:
    ts = row.get("snapshot_ts_premarket")
    if ts is None:
        return True
    try:
        from zoneinfo import ZoneInfo

        dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        et = dt.astimezone(ZoneInfo("America/New_York"))
        return et.time() < NYSE_OPEN_TIME
    except Exception:
        return True


def _get_engine():
    from sqlalchemy import create_engine
    from config_loader import get_database_url

    return create_engine(get_database_url())


def ensure_gap_forecast_table(engine=None) -> None:
    eng = engine or _get_engine()
    from pathlib import Path
    from sqlalchemy import text

    root = Path(__file__).resolve().parent.parent

    def _run_sql_file(path: Path) -> None:
        raw = path.read_text(encoding="utf-8")
        with eng.begin() as conn:
            conn.execute(text(raw))

    _run_sql_file(root / DDL_PATH)
    mig_path = root / DDL_MIGRATION_TICKER
    if mig_path.is_file():
        _run_sql_file(mig_path)


def _symbols_for_log() -> List[str]:
    from services.macro_premarket_risk import evaluate_macro_premarket_risk
    from services.ticker_groups import (
        get_tickers_fast,
        get_tickers_for_5m_correlation,
        get_tickers_for_portfolio_game,
        get_tickers_game_5m,
    )

    tickers = (
        list(get_tickers_game_5m() or [])
        + list(get_tickers_fast() or [])
        + list(get_tickers_for_portfolio_game() or [])
        + list(get_tickers_for_5m_correlation() or [])
    )
    game = [str(t).strip().upper() for t in tickers if str(t).strip()]
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
    PRE_MARKET: гэп тикера + OLS sector pred + pred_ticker (v2) для GAME_5m.
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

    from services.ticker_open_gap_predict import (
        get_ticker_gap_model_version,
        predict_ticker_open_gap_pct,
    )

    n_ok = 0
    errors: List[str] = []
    now_utc = datetime.now(timezone.utc)
    model_ver = get_ticker_gap_model_version()
    for sym in _symbols_for_log():
        det = get_indicator_gap_detail(sym)
        pm_gap = det.get("gap_pct")
        try:
            pred_ticker, pred_ticker_src = predict_ticker_open_gap_pct(
                sym,
                macro_risk=macro,
                premarket_gap_pct=float(pm_gap) if pm_gap is not None else None,
            )
        except Exception as e:
            pred_ticker, pred_ticker_src = None, "error"
            errors.append(f"{sym}: predict {e}")
            logger.warning("gap_forecast premarket predict %s: %s", sym, e)
        if sym == proxy and pred_ticker is None and pred is not None:
            pred_ticker, pred_ticker_src = float(pred), "sector_row"
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
                        "premarket_gap_pct": pm_gap,
                        "pred_sector_gap_pct": pred,
                        "sector_proxy": proxy,
                        "macro_risk_level": macro.get("risk_level"),
                        "macro_equity_gap_bias": macro.get("equity_gap_bias"),
                        "macro_indicators_json": indicators_json,
                        "source_premarket": det.get("source") or "none",
                        "pred_ticker_gap_pct": pred_ticker,
                        "pred_ticker_source": pred_ticker_src,
                        "pred_ticker_model_version": model_ver if pred_ticker is not None else None,
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
            g, opx2, prev2 = _compute_rth_open_gap_pct(df, sym, trade_date=td)
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
                SELECT pred_sector_gap_pct, pred_ticker_gap_pct, premarket_gap_pct, open_gap_pct
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
                    "pred_ticker_gap_pct": None,
                    "pred_ticker_source": None,
                    "pred_ticker_model_version": None,
                },
            )
        row = (None, None, None, None)

    if row and row[3] is not None and source_open != "backfill":
        return False

    pred_sec = float(row[0]) if row and row[0] is not None else None
    pred_ticker = float(row[1]) if row and row[1] is not None else None
    pm_gap = float(row[2]) if row and row[2] is not None else None
    err_pred = round(float(gap) - pred_sec, 4) if pred_sec is not None else None
    err_ticker = round(float(gap) - pred_ticker, 4) if pred_ticker is not None else None
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
                "error_pred_ticker_vs_open_pct": err_ticker,
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


def load_frozen_gap_snapshot(
    symbol: str,
    *,
    engine=None,
    trade_date: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """Снимок premarket-прогноза за trade_date (не пересчитывать OLS после open)."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    td = trade_date or _et_trade_date()
    eng = engine or _get_engine()
    from sqlalchemy import text

    try:
        with eng.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT premarket_gap_pct, pred_ticker_gap_pct, pred_ticker_source,
                           pred_ticker_model_version, pred_sector_gap_pct, open_gap_pct,
                           snapshot_ts_premarket, premarket_last, prev_close
                    FROM game5m_gap_forecast_daily
                    WHERE trade_date = :td AND symbol = :sym
                    """
                ),
                {"td": td, "sym": sym},
            ).mappings().first()
        return dict(row) if row else None
    except Exception as e:
        logger.debug("load_frozen_gap_snapshot %s: %s", sym, e)
        return None


def fetch_gap_forecast_rows(
    engine,
    *,
    days: int = 90,
    symbols: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    from sqlalchemy import text

    q = """
        SELECT trade_date, symbol, premarket_gap_pct, pred_sector_gap_pct, sector_proxy,
               pred_ticker_gap_pct, pred_ticker_source, pred_ticker_model_version,
               open_gap_pct, error_pred_vs_open_pct, error_pred_ticker_vs_open_pct,
               error_premarket_vs_open_pct,
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
    Агрегаты для арбитра: sector pred vs open; ticker v2 pred vs open; legacy baseline (sector pred на тикерах).
    """
    proxy = (sector_proxy or "SMH").strip().upper()
    complete_sector: List[Dict[str, Any]] = []
    complete_ticker: List[Dict[str, Any]] = []
    complete_game_sector_baseline: List[Dict[str, Any]] = []
    invalid_post_open_snapshots = 0
    for r in rows:
        og = r.get("open_gap_pct")
        if og is None:
            continue
        if not _snapshot_is_preopen(r):
            invalid_post_open_snapshots += 1
            continue
        sym = str(r.get("symbol") or "").upper()
        if sym == proxy and r.get("pred_sector_gap_pct") is not None:
            complete_sector.append(r)
        elif sym != proxy:
            if r.get("pred_ticker_gap_pct") is not None:
                complete_ticker.append(r)
            if r.get("pred_sector_gap_pct") is not None:
                complete_game_sector_baseline.append(r)

    def _pack(
        sub: List[Dict[str, Any]],
        label: str,
        *,
        pred_key: str = "pred_sector_gap_pct",
        err_key: str = "error_pred_vs_open_pct",
    ) -> Dict[str, Any]:
        if not sub:
            return {"label": label, "n_complete": 0}
        err_pred = []
        for x in sub:
            if x.get(err_key) is not None:
                err_pred.append(float(x[err_key]))
            else:
                p, o = x.get(pred_key), x.get("open_gap_pct")
                if p is not None and o is not None:
                    err_pred.append(float(o) - float(p))
        err_pm = [
            float(x["error_premarket_vs_open_pct"]) for x in sub if x.get("error_premarket_vs_open_pct") is not None
        ]
        preds = [float(x[pred_key]) for x in sub if x.get(pred_key) is not None]
        opens = [float(x["open_gap_pct"]) for x in sub if x.get("open_gap_pct") is not None]
        sign_ok = 0
        for x in sub:
            p, o = x.get(pred_key), x.get("open_gap_pct")
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
    ticker_block = _pack(
        complete_ticker,
        "Ticker v2 pred vs open (GAME_5m)",
        pred_key="pred_ticker_gap_pct",
        err_key="error_pred_ticker_vs_open_pct",
    )
    premarket_baseline = _pack(
        [r for r in rows if r.get("open_gap_pct") is not None and r.get("premarket_gap_pct") is not None],
        "Naive: premarket gap vs open",
        pred_key="premarket_gap_pct",
        err_key="error_premarket_vs_open_pct",
    )
    sector_on_game = _pack(
        complete_game_sector_baseline,
        f"Legacy: sector pred на тикерах ({proxy})",
        pred_key="pred_sector_gap_pct",
        err_key="error_pred_vs_open_pct",
    )
    return {
        "n_rows": len(rows),
        "n_premarket_only": premarket_only,
        "n_invalid_post_open_snapshots": invalid_post_open_snapshots,
        "sector_proxy": proxy,
        "sector": _pack(complete_sector, f"Sector OLS pred vs open ({proxy})"),
        "ticker_v2": ticker_block,
        "game_tickers_pooled": ticker_block,
        "premarket_baseline": premarket_baseline,
        "game_sector_baseline": sector_on_game,
        "ticker_vs_sector_mae_delta_pp": (
            round(
                float(ticker_block["mean_abs_error_pred_pp"]) - float(sector_on_game["mean_abs_error_pred_pp"]),
                4,
            )
            if ticker_block.get("mean_abs_error_pred_pp") is not None
            and sector_on_game.get("mean_abs_error_pred_pp") is not None
            else None
        ),
    }


def _coerce_trade_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _ml_beats_baseline_mae(pooled: Dict[str, Any]) -> Optional[bool]:
    ml = pooled.get("ticker_v2") or pooled.get("game_tickers_pooled") or {}
    pm = pooled.get("premarket_baseline") or {}
    ml_mae = ml.get("mean_abs_error_pred_pp")
    pm_mae = pm.get("mean_abs_error_pred_pp")
    if ml_mae is None or pm_mae is None:
        return None
    return float(ml_mae) < float(pm_mae)


def pool_gap_forecast_metrics_windows(
    rows: Sequence[Dict[str, Any]],
    *,
    sector_proxy: str = "SMH",
    windows: Sequence[int] = (14, 30, 90),
) -> Dict[str, Any]:
    """Rolling pooled metrics per calendar window (trade_date anchor = max in rows)."""
    dated: List[tuple[date, Dict[str, Any]]] = []
    for r in rows:
        td = _coerce_trade_date(r.get("trade_date"))
        if td is not None:
            dated.append((td, r))
    if not dated:
        return {}
    max_d = max(td for td, _ in dated)
    out: Dict[str, Any] = {}
    for w in windows:
        w = int(w)
        cut = max_d - timedelta(days=w)
        sub = [r for td, r in dated if td >= cut]
        pooled = pool_gap_forecast_metrics(sub, sector_proxy=sector_proxy)
        pm = pooled.get("premarket_baseline") or {}
        ml = pooled.get("ticker_v2") or {}
        out[str(w)] = {
            "window_days": w,
            "anchor_trade_date": max_d.isoformat(),
            "n_rows": len(sub),
            "premarket_mae_pp": pm.get("mean_abs_error_pred_pp"),
            "ticker_ml_mae_pp": ml.get("mean_abs_error_pred_pp"),
            "ml_beats_baseline_mae": _ml_beats_baseline_mae(pooled),
            "premarket_sign_rate": pm.get("sign_agreement_rate"),
            "ticker_ml_sign_rate": ml.get("sign_agreement_rate"),
        }
    return out


def query_gap_rolling_mae_sql(engine, *, windows: Sequence[int] = (14, 30)) -> Dict[str, Any]:
    """Lightweight rolling MAE from DB (for daily session review)."""
    from sqlalchemy import text

    out: Dict[str, Any] = {}
    with engine.connect() as conn:
        for w in windows:
            w = int(w)
            row = conn.execute(
                text(
                    """
                    SELECT
                      COUNT(*) AS n,
                      ROUND(AVG(ABS(premarket_gap_pct - open_gap_pct))::numeric, 3) AS pm_mae,
                      ROUND(AVG(ABS(pred_ticker_gap_pct - open_gap_pct))::numeric, 3) AS ml_mae,
                      ROUND(AVG(
                        CASE WHEN ABS(pred_ticker_gap_pct - open_gap_pct)
                               < ABS(premarket_gap_pct - open_gap_pct)
                             THEN 1 ELSE 0 END
                      )::numeric, 3) AS ml_win_rate
                    FROM game5m_gap_forecast_daily
                    WHERE open_gap_pct IS NOT NULL
                      AND premarket_gap_pct IS NOT NULL
                      AND pred_ticker_gap_pct IS NOT NULL
                      AND trade_date >= CURRENT_DATE - CAST(:days AS integer)
                    """
                ),
                {"days": w},
            ).mappings().first()
            if not row:
                continue
            pm_mae = float(row["pm_mae"]) if row["pm_mae"] is not None else None
            ml_mae = float(row["ml_mae"]) if row["ml_mae"] is not None else None
            out[str(w)] = {
                "window_days": w,
                "n": int(row["n"] or 0),
                "premarket_mae_pp": pm_mae,
                "ticker_ml_mae_pp": ml_mae,
                "ml_beats_baseline_mae": (
                    ml_mae < pm_mae if pm_mae is not None and ml_mae is not None else None
                ),
                "ml_win_rate": float(row["ml_win_rate"]) if row["ml_win_rate"] is not None else None,
            }
    return out
