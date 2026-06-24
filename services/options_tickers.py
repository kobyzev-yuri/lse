"""
Тикеры для UI опционов и cron OI: 5m + портфель (акции), плюс фактические снимки в БД.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set

# Явный override в config.env (если пусто — авто из 5m + portfolio).
DEFAULT_OPTIONS_OI_WATCHLIST: tuple[str, ...] = ()


def _is_options_underlying_symbol(ticker: str) -> bool:
    """Исключаем индексы, forex и фьючерсы Yahoo (^VIX, GBPUSD=X, CL=F)."""
    t = (ticker or "").strip().upper()
    if not t or t in {"MACRO", "US_MACRO"}:
        return False
    if t.startswith("^"):
        return False
    if "=" in t:
        return False
    return True


def _merge_unique_tickers(*groups: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for group in groups:
        for raw in group:
            t = (raw or "").strip().upper()
            if not _is_options_underlying_symbol(t) or t in seen:
                continue
            seen.add(t)
            out.append(t)
    return sorted(out)


def get_options_oi_watchlist_sources() -> Dict[str, List[str]]:
    """Разбивка watchlist по источникам (для API / документации)."""
    try:
        from config_loader import get_config_value

        raw = (get_config_value("OPTIONS_OI_WATCHLIST", "") or "").strip()
        if raw:
            manual = sorted({t.strip().upper() for t in raw.split(",") if t.strip()})
            return {"manual": manual, "game_5m": [], "portfolio": []}
    except Exception:
        pass

    from services.ticker_groups import get_tickers_for_portfolio_game, get_tickers_game_5m

    game = _merge_unique_tickers(get_tickers_game_5m())
    portfolio = _merge_unique_tickers(get_tickers_for_portfolio_game())
    return {"manual": [], "game_5m": game, "portfolio": portfolio}


def get_options_oi_watchlist() -> List[str]:
    """Список тикеров для ежедневного cron-снимка OI."""
    sources = get_options_oi_watchlist_sources()
    if sources["manual"]:
        return sources["manual"]
    return _merge_unique_tickers(sources["game_5m"], sources["portfolio"])


def _snapshot_ticker_stats() -> Dict[str, Dict[str, Any]]:
    try:
        from sqlalchemy import text
        from report_generator import get_engine

        q = text(
            """
            SELECT ticker,
                   COUNT(DISTINCT snapshot_date) AS snapshot_days,
                   MAX(snapshot_date)::text AS last_snapshot
            FROM options_chain_oi_snapshot
            GROUP BY ticker
            """
        )
        with get_engine().connect() as conn:
            rows = conn.execute(q).fetchall()
        return {
            str(r[0]).upper(): {
                "snapshot_days": int(r[1] or 0),
                "last_snapshot": r[2],
            }
            for r in rows
        }
    except Exception:
        return {}


def list_options_ui_tickers() -> Dict[str, Any]:
    """Тикеры для listbox на /options/map и /options/tools."""
    sources = get_options_oi_watchlist_sources()
    watchlist = get_options_oi_watchlist()
    snap = _snapshot_ticker_stats()
    merged = sorted(set(watchlist) | set(snap.keys()))
    watch_set = set(watchlist)
    game_set = set(sources.get("game_5m") or [])
    pf_set = set(sources.get("portfolio") or [])
    by_ticker: Dict[str, Dict[str, Any]] = {}
    with_snapshot: List[str] = []
    for t in merged:
        s = snap.get(t, {})
        days = int(s.get("snapshot_days") or 0)
        groups: List[str] = []
        if t in game_set:
            groups.append("game_5m")
        if t in pf_set:
            groups.append("portfolio")
        if sources.get("manual"):
            groups = ["manual"]
        entry = {
            "watchlist": t in watch_set,
            "groups": groups,
            "snapshot_days": days,
            "last_snapshot": s.get("last_snapshot"),
            "has_snapshot": days > 0,
        }
        by_ticker[t] = entry
        if days > 0:
            with_snapshot.append(t)
    return {
        "status": "ok",
        "tickers": merged,
        "watchlist": watchlist,
        "sources": sources,
        "with_snapshot": sorted(with_snapshot),
        "by_ticker": by_ticker,
    }
