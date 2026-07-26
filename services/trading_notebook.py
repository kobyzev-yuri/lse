"""Рабочая тетрадка Насти: ручные уровни + справочный Close из quotes/yfinance."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_PATH = _REPO_ROOT / "nastya" / "notebook" / "notebook_data.json"


def notebook_data_path() -> Path:
    return DEFAULT_DATA_PATH


def load_notebook_data(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or notebook_data_path()
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("notebook_data.json must be an object")
    return raw


def _normalize_tickers(tickers: Sequence[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for t in tickers:
        u = str(t or "").strip().upper()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def fetch_closes_from_quotes(tickers: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Last two daily closes from public.quotes (for Close + day change)."""
    wanted = _normalize_tickers(tickers)
    if not wanted:
        return {}
    try:
        from sqlalchemy import bindparam, create_engine, text

        from config_loader import get_database_url
    except Exception as e:
        logger.debug("quotes deps unavailable: %s", e)
        return {}

    sql = text(
        """
        SELECT ticker, date, close
        FROM (
            SELECT ticker, date, close,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM quotes
            WHERE ticker IN :tickers AND close IS NOT NULL
        ) x
        WHERE rn <= 2
        ORDER BY ticker ASC, date DESC
        """
    ).bindparams(bindparam("tickers", expanding=True))

    out: Dict[str, Dict[str, Any]] = {}
    try:
        eng = create_engine(get_database_url())
        with eng.connect() as conn:
            rows = conn.execute(sql, {"tickers": wanted}).mappings().all()
    except Exception as e:
        logger.debug("quotes close fetch failed: %s", e)
        return {}

    by_t: Dict[str, List[Any]] = {}
    for r in rows:
        t = str(r["ticker"]).upper()
        by_t.setdefault(t, []).append(r)

    for t, lst in by_t.items():
        if not lst:
            continue
        last = lst[0]
        close = float(last["close"])
        prev = float(lst[1]["close"]) if len(lst) > 1 else None
        chg = None
        chg_pct = None
        if prev is not None and prev != 0:
            chg = close - prev
            chg_pct = (chg / prev) * 100.0
        out[t] = {
            "close": close,
            "prev_close": prev,
            "chg": chg,
            "chg_pct": chg_pct,
            "asof": str(last["date"]),
            "source": "quotes",
        }
    return out


def fetch_closes_yfinance(tickers: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    wanted = _normalize_tickers(tickers)
    if not wanted:
        return {}
    try:
        import yfinance as yf
    except Exception as e:
        logger.debug("yfinance missing: %s", e)
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for t in wanted:
        try:
            hist = yf.Ticker(t).history(period="5d", auto_adjust=True)
            if hist is None or hist.empty or "Close" not in hist.columns:
                continue
            closes = hist["Close"].dropna()
            if closes.empty:
                continue
            close = float(closes.iloc[-1])
            prev = float(closes.iloc[-2]) if len(closes) > 1 else None
            chg = None
            chg_pct = None
            if prev is not None and prev != 0:
                chg = close - prev
                chg_pct = (chg / prev) * 100.0
            asof = closes.index[-1]
            out[t] = {
                "close": close,
                "prev_close": prev,
                "chg": chg,
                "chg_pct": chg_pct,
                "asof": str(getattr(asof, "date", lambda: asof)()),
                "source": "yfinance",
            }
        except Exception as e:
            logger.debug("yfinance close %s: %s", t, e)
    return out


def fetch_closes(tickers: Sequence[str], *, use_yfinance_fallback: bool = True) -> Dict[str, Dict[str, Any]]:
    wanted = _normalize_tickers(tickers)
    out = fetch_closes_from_quotes(wanted)
    missing = [t for t in wanted if t not in out]
    if missing and use_yfinance_fallback:
        yf = fetch_closes_yfinance(missing)
        out.update(yf)
    return out


def _fmt_chg(px: Dict[str, Any]) -> tuple[str, bool]:
    chg = px.get("chg")
    chg_pct = px.get("chg_pct")
    if chg is None or chg_pct is None:
        return ("—", True)
    sign = "+" if chg >= 0 else ""
    return (f"{sign}{chg:.2f} ({sign}{chg_pct:.2f}%)", chg >= 0)


def merge_prices_into_tickers(
    tickers: Dict[str, Any],
    prices: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for sym, row in (tickers or {}).items():
        if not isinstance(row, dict):
            continue
        d = dict(row)
        u = str(sym).upper()
        px = prices.get(u)
        if px and px.get("close") is not None:
            close = float(px["close"])
            chg_s, up = _fmt_chg(px)
            d["px"] = close
            d["chg"] = chg_s
            d["up"] = up
            d["price_source"] = px.get("source")
            d["price_asof"] = px.get("asof")
        else:
            d.setdefault("px", None)
            d.setdefault("chg", "нет Close")
            d.setdefault("up", True)
            d["price_source"] = None
            d["price_asof"] = None
        merged[u] = d
    return merged


def build_notebook_payload(
    *,
    path: Optional[Path] = None,
    with_prices: bool = True,
    tickers_filter: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    data = load_notebook_data(path)
    tickers_raw = data.get("tickers") if isinstance(data.get("tickers"), dict) else {}
    if tickers_filter:
        wanted = set(_normalize_tickers(tickers_filter))
        tickers_raw = {k: v for k, v in tickers_raw.items() if str(k).upper() in wanted}

    prices: Dict[str, Dict[str, Any]] = {}
    if with_prices and tickers_raw:
        prices = fetch_closes(list(tickers_raw.keys()))

    tickers = merge_prices_into_tickers(tickers_raw, prices)
    groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}

    digest = data.get("digest") if isinstance(data.get("digest"), dict) else {}
    # Prefer latest pipeline output if present (local/notebook/digest_latest.json).
    try:
        from services.notebook_news_digest import load_latest_digest

        live = load_latest_digest()
        if isinstance(live, dict) and (
            live.get("signals") is not None or live.get("date")
        ):
            digest = live
    except Exception as e:
        logger.debug("notebook digest overlay skipped: %s", e)

    return {
        "schema_version": int(data.get("schema_version") or SCHEMA_VERSION),
        "asof_label": data.get("asof_label") or "",
        "principle_ru": data.get("principle_ru") or "",
        "groups": groups,
        "tickers": tickers,
        "digest": digest,
        "digest_buckets": data.get("digest_buckets") if isinstance(data.get("digest_buckets"), list) else [],
        "watchlist": data.get("watchlist") if isinstance(data.get("watchlist"), dict) else {},
        "prices": prices,
        "data_path": str(path or notebook_data_path()),
    }
