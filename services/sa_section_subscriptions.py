"""Seeking Alpha tipsters subscriptions: sections + ticker mute → knowledge_base.

UI feed and notebook digest read SA rows from KB. File snapshots are optional CLI archive.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from config_loader import get_config_value
from services.seeking_alpha_finance import (
    KB_SOURCE,
    fetch_and_save_sa_news,
    fetch_section_pages,
    rapidapi_key,
    save_sa_items_to_kb,
)

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = _REPO_ROOT / "local" / "notebook" / "sa_sections"
DEFAULT_SUBS_PATH = _REPO_ROOT / "local" / "notebook" / "sa_section_subscriptions.json"
DEFAULT_LATEST_PATH = DEFAULT_OUT_DIR / "latest.json"
DEFAULT_SNAPSHOTS_DIR = DEFAULT_OUT_DIR / "snapshots"

SECTION_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "articles.latest-articles",
        "title": "Articles · Latest",
        "group": "articles",
        "path": "/v1/articles/list",
        "params": {"category": "latest-articles"},
        "item_kind": "articles",
        "available": True,
        "note": "Широкая лента статей SA (не тикерный news).",
    },
    {
        "id": "articles.market-outlook",
        "title": "Articles · Market Outlook",
        "group": "articles",
        "path": "/v1/articles/list",
        "params": {"category": "market-outlook"},
        "item_kind": "articles",
        "available": True,
        "note": "Макро / market outlook.",
    },
    {
        "id": "articles.stock-ideas",
        "title": "Articles · Stock Ideas",
        "group": "articles",
        "path": "/v1/articles/list",
        "params": {"category": "stock-ideas"},
        "item_kind": "articles",
        "available": True,
        "note": "Идеи по акциям / секторам.",
    },
    {
        "id": "articles.editors-picks",
        "title": "Articles · Editors' Picks",
        "group": "articles",
        "path": "/v1/articles/list",
        "params": {"category": "editors-picks"},
        "item_kind": "articles",
        "available": True,
        "note": "Редакторский отбор.",
    },
    {
        "id": "articles.investing-strategy",
        "title": "Articles · Investing Strategy",
        "group": "articles",
        "path": "/v1/articles/list",
        "params": {"category": "investing-strategy"},
        "item_kind": "articles",
        "available": True,
        "note": "Стратегия / аллокация.",
    },
    {
        "id": "markets.day-watch",
        "title": "Markets · Day Watch",
        "group": "markets",
        "path": "/v1/markets/day-watch",
        "params": {},
        "item_kind": "day_watch",
        "available": True,
        "note": "Гейнеры/лузеры / in-the-news (не статьи).",
    },
    {
        "id": "news.market-news",
        "title": "News · Market (list)",
        "group": "news",
        "path": "/v1/news/list",
        "params": {"category": "market-news"},
        "item_kind": "articles",
        "available": False,
        "note": "tipsters /v1/news/list → 422 (probe).",
    },
    {
        "id": "news.economy",
        "title": "News · Economy",
        "group": "news",
        "path": "/v1/news/list",
        "params": {"category": "economy"},
        "item_kind": "articles",
        "available": False,
        "note": "tipsters /v1/news/list → 422 (probe).",
    },
]


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def default_per_group_limit() -> int:
    try:
        n = int((get_config_value("NOTEBOOK_SA_SECTION_LIMIT", "40") or "40").strip())
    except (TypeError, ValueError):
        n = 40
    return max(1, min(n, 200))


def snapshot_retain_days() -> int:
    try:
        n = int((get_config_value("NOTEBOOK_SA_SECTION_RETAIN_DAYS", "14") or "14").strip())
    except (TypeError, ValueError):
        n = 14
    return max(1, min(n, 90))


def catalog_by_id() -> Dict[str, Dict[str, Any]]:
    return {str(r["id"]): dict(r) for r in SECTION_CATALOG if r.get("id")}


def section_kb_symbol(section_id: str) -> str:
    """Synthetic KB symbol for a tipsters section, e.g. SA:articles.market-outlook."""
    sid = str(section_id or "").strip()
    if not sid:
        return "SA:UNKNOWN"
    if sid.upper().startswith("SA:"):
        return sid.upper() if sid.startswith("SA:") else f"SA:{sid}"
    return f"SA:{sid}"


def parse_section_id_from_symbol(symbol: str) -> Optional[str]:
    s = str(symbol or "").strip()
    if not s.upper().startswith("SA:"):
        return None
    rest = s[3:]
    return rest or None


def subscriptions_path(path: Optional[Path] = None) -> Path:
    return path or DEFAULT_SUBS_PATH


def snapshots_dir(path: Optional[Path] = None) -> Path:
    return path or DEFAULT_SNAPSHOTS_DIR


def latest_path(path: Optional[Path] = None) -> Path:
    return path or DEFAULT_LATEST_PATH


def _default_subs_doc() -> Dict[str, Any]:
    return {
        "schema_version": 2,
        "per_group_limit": default_per_group_limit(),
        "sections": {},
        "tickers": {},
        "tickers_default_on": True,
        # legacy alias kept in-memory for older callers
        "subscriptions": {},
        "updated_at_utc": "",
    }


def _clean_bool_map(raw: Any) -> Dict[str, bool]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, bool] = {}
    for k, v in raw.items():
        kid = str(k).strip()
        if not kid:
            continue
        out[kid] = bool(v)
    return out


def load_subscriptions(path: Optional[Path] = None) -> Dict[str, Any]:
    p = subscriptions_path(path)
    if not p.is_file():
        return _default_subs_doc()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("sa section subs load failed: %s", e)
        return _default_subs_doc()
    if not isinstance(data, dict):
        return _default_subs_doc()
    out = _default_subs_doc()
    try:
        out["per_group_limit"] = max(1, min(int(data.get("per_group_limit") or default_per_group_limit()), 200))
    except (TypeError, ValueError):
        out["per_group_limit"] = default_per_group_limit()
    # Migrate v1 `subscriptions` → `sections`
    sections = _clean_bool_map(data.get("sections"))
    if not sections:
        sections = _clean_bool_map(data.get("subscriptions"))
    out["sections"] = sections
    out["subscriptions"] = dict(sections)  # back-compat
    out["tickers"] = {
        str(k).strip().upper(): bool(v)
        for k, v in _clean_bool_map(data.get("tickers")).items()
        if str(k).strip()
    }
    if "tickers_default_on" in data:
        out["tickers_default_on"] = bool(data.get("tickers_default_on"))
    out["updated_at_utc"] = str(data.get("updated_at_utc") or "")
    try:
        out["schema_version"] = int(data.get("schema_version") or 2)
    except (TypeError, ValueError):
        out["schema_version"] = 2
    return out


def save_subscriptions(doc: Dict[str, Any], *, path: Optional[Path] = None) -> Dict[str, Any]:
    p = subscriptions_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    sections = _clean_bool_map(doc.get("sections") or doc.get("subscriptions"))
    # Drop explicit False for sections (enabled = presence True)
    sections = {k: True for k, v in sections.items() if v}
    tickers = {
        str(k).strip().upper(): bool(v)
        for k, v in (doc.get("tickers") or {}).items()
        if str(k).strip()
    }
    payload = {
        "schema_version": 2,
        "per_group_limit": max(1, min(int(doc.get("per_group_limit") or default_per_group_limit()), 200)),
        "sections": sections,
        "tickers": tickers,
        "tickers_default_on": bool(doc.get("tickers_default_on", True)),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Keep in-memory shape compatible
    payload["subscriptions"] = dict(sections)
    return payload


def set_subscription(
    section_id: str,
    enabled: bool,
    *,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    sid = str(section_id or "").strip()
    if not sid:
        raise ValueError("section_id required")
    cat = catalog_by_id().get(sid)
    if not cat:
        raise KeyError(f"unknown section: {sid}")
    if enabled and not cat.get("available"):
        raise ValueError(f"section unavailable: {sid}")
    doc = load_subscriptions(path)
    secs = dict(doc.get("sections") or {})
    if enabled:
        secs[sid] = True
    else:
        secs.pop(sid, None)
    doc["sections"] = secs
    return save_subscriptions(doc, path=path)


def set_subscriptions_bulk(
    mapping: Optional[Dict[str, bool]] = None,
    *,
    sections: Optional[Dict[str, bool]] = None,
    tickers: Optional[Dict[str, bool]] = None,
    tickers_default_on: Optional[bool] = None,
    per_group_limit: Optional[int] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Update section and/or ticker subscription maps."""
    doc = load_subscriptions(path)
    catalog = catalog_by_id()
    secs = dict(doc.get("sections") or {})
    sec_map = sections if sections is not None else mapping
    if sec_map:
        for raw_id, raw_en in sec_map.items():
            sid = str(raw_id or "").strip()
            if not sid or sid not in catalog:
                continue
            en = bool(raw_en)
            if en and not catalog[sid].get("available"):
                continue
            if en:
                secs[sid] = True
            else:
                secs.pop(sid, None)
    doc["sections"] = secs
    if tickers is not None:
        tmap = dict(doc.get("tickers") or {})
        for raw_sym, raw_en in tickers.items():
            sym = str(raw_sym or "").strip().upper()
            if not sym or sym.startswith("SA:"):
                continue
            tmap[sym] = bool(raw_en)
        doc["tickers"] = tmap
    if tickers_default_on is not None:
        doc["tickers_default_on"] = bool(tickers_default_on)
    if per_group_limit is not None:
        try:
            doc["per_group_limit"] = max(1, min(int(per_group_limit), 200))
        except (TypeError, ValueError):
            pass
    return save_subscriptions(doc, path=path)


def subscribe_all_available(*, path: Optional[Path] = None) -> Dict[str, Any]:
    mapping = {str(c["id"]): True for c in SECTION_CATALOG if c.get("available") and c.get("id")}
    return set_subscriptions_bulk(sections=mapping, path=path)


def enabled_section_ids(doc: Optional[Dict[str, Any]] = None) -> List[str]:
    d = doc if isinstance(doc, dict) else load_subscriptions()
    catalog = catalog_by_id()
    out: List[str] = []
    for sid, en in (d.get("sections") or d.get("subscriptions") or {}).items():
        if not en:
            continue
        cat = catalog.get(str(sid))
        if cat and cat.get("available"):
            out.append(str(sid))
    return out


def enabled_section_kb_symbols(doc: Optional[Dict[str, Any]] = None) -> List[str]:
    return [section_kb_symbol(sid) for sid in enabled_section_ids(doc)]


def enabled_tickers(
    candidates: Sequence[str],
    *,
    doc: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Filter SA-fetch candidates by ticker mute map."""
    d = doc if isinstance(doc, dict) else load_subscriptions()
    default_on = bool(d.get("tickers_default_on", True))
    overrides = d.get("tickers") if isinstance(d.get("tickers"), dict) else {}
    out: List[str] = []
    seen: set[str] = set()
    for raw in candidates:
        sym = str(raw or "").strip().upper()
        if not sym or sym in seen or sym.startswith("SA:"):
            continue
        if sym in overrides:
            if not bool(overrides[sym]):
                continue
        elif not default_on:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def sa_ticker_candidates() -> Dict[str, Any]:
    """Notebook universe ∪ extras for SA ticker checkbox UI."""
    from services.notebook_news_digest import build_sa_fetch_tickers

    uni = build_sa_fetch_tickers(equity_only=True)
    base = [str(t).upper() for t in (uni.get("group3_union") or []) if str(t).strip()]
    extra = [str(t).upper() for t in (uni.get("sa_extra") or []) if str(t).strip()]
    all_syms = list(dict.fromkeys(base + extra))
    return {
        "universe": base,
        "extras": extra,
        "all": all_syms,
        "membership": uni.get("membership") or {},
    }


def catalog_with_subscriptions(
    *,
    subs_path: Optional[Path] = None,
) -> Dict[str, Any]:
    doc = load_subscriptions(subs_path)
    enabled_secs = set(enabled_section_ids(doc))
    rows = []
    for cat in SECTION_CATALOG:
        row = dict(cat)
        sid = str(row.get("id") or "")
        row["subscribed"] = sid in enabled_secs
        row["kb_symbol"] = section_kb_symbol(sid) if sid else ""
        rows.append(row)

    cand = sa_ticker_candidates()
    enabled_t = set(enabled_tickers(cand["all"], doc=doc))
    ticker_rows = []
    extra_set = set(cand["extras"])
    for sym in cand["all"]:
        ticker_rows.append(
            {
                "symbol": sym,
                "subscribed": sym in enabled_t,
                "is_extra": sym in extra_set,
                "group": (cand.get("membership") or {}).get(sym),
            }
        )

    return {
        "catalog": rows,
        "sections": doc.get("sections") or {},
        "subscriptions": doc.get("sections") or {},  # back-compat
        "tickers": doc.get("tickers") or {},
        "tickers_default_on": bool(doc.get("tickers_default_on", True)),
        "ticker_rows": ticker_rows,
        "per_group_limit": int(doc.get("per_group_limit") or default_per_group_limit()),
        "updated_at_utc": doc.get("updated_at_utc") or "",
        "has_api_key": bool(rapidapi_key()),
        "kb_source": KB_SOURCE,
    }


def prepare_section_items_for_kb(items: Sequence[Dict[str, Any]], *, section_id: str) -> List[Dict[str, Any]]:
    """Stamp synthetic ticker SA:<section_id> onto flattened section items."""
    sym = section_kb_symbol(section_id)
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        row = dict(it)
        row["ticker"] = sym
        row["section_id"] = section_id
        # Keep primary equity tickers in payload if present
        out.append(row)
    return out


def ingest_sections_to_kb(
    *,
    section_ids: Optional[Sequence[str]] = None,
    all_available: bool = False,
    limit: Optional[int] = None,
    sleep_sec: float = 0.3,
    api_key: Optional[str] = None,
    write_archive: bool = False,
    subs_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Fetch subscribed (or given) sections and insert into knowledge_base."""
    catalog = catalog_by_id()
    doc = load_subscriptions(subs_path)
    per_limit = int(limit if limit is not None else (doc.get("per_group_limit") or default_per_group_limit()))
    per_limit = max(1, min(per_limit, 200))

    if all_available:
        wanted = [str(c["id"]) for c in SECTION_CATALOG if c.get("available")]
    elif section_ids is not None:
        wanted = [str(s).strip() for s in section_ids if str(s).strip()]
    else:
        wanted = enabled_section_ids(doc)

    max_sections = 12
    try:
        max_sections = max(1, min(int(get_config_value("NOTEBOOK_SA_SECTION_MAX_PER_RUN", "12") or 12), 30))
    except (TypeError, ValueError):
        max_sections = 12
    wanted = wanted[:max_sections]

    groups: Dict[str, Any] = {}
    all_kb_items: List[Dict[str, Any]] = []
    for i, sid in enumerate(wanted):
        cat = catalog.get(sid)
        if not cat or not cat.get("available"):
            groups[sid] = {"status": "unavailable", "count": 0, "kb_inserted": 0, "error": "unknown/unavailable"}
            continue
        path = str(cat.get("path") or "")
        params = dict(cat.get("params") or {})
        kind = str(cat.get("item_kind") or "articles")
        try:
            bundle = fetch_section_pages(
                path,
                section_id=sid,
                params=params,
                limit=per_limit,
                item_kind=kind,
                api_key=api_key,
            )
            items = prepare_section_items_for_kb(bundle.get("items") or [], section_id=sid)
            err = "; ".join(bundle.get("errors") or [])
            status_code = int(bundle.get("status") or 0)
            status = "ok" if status_code == 200 and items else ("error" if bundle.get("errors") else "empty")
            groups[sid] = {
                "status": status,
                "count": len(items),
                "kb_symbol": section_kb_symbol(sid),
                "endpoint": path,
                "params": params,
                "error": err or None,
                "title": cat.get("title"),
                "items_sample": items[:5],
            }
            all_kb_items.extend(items)
        except Exception as e:
            logger.warning("sa section %s fetch failed: %s", sid, e)
            groups[sid] = {
                "status": "error",
                "count": 0,
                "kb_symbol": section_kb_symbol(sid),
                "endpoint": path,
                "error": str(e),
                "title": cat.get("title"),
            }
        if sleep_sec > 0 and i + 1 < len(wanted):
            time.sleep(float(sleep_sec))

    kb_inserted = 0
    kb_error = None
    try:
        kb_inserted = save_sa_items_to_kb(all_kb_items)
    except Exception as e:
        logger.exception("SA sections → KB failed: %s", e)
        kb_error = str(e)

    generated = datetime.now(timezone.utc).isoformat()
    payload: Dict[str, Any] = {
        "schema_version": 2,
        "generated_at_utc": generated,
        "per_group_limit": per_limit,
        "requested_sections": wanted,
        "item_count": len(all_kb_items),
        "kb_inserted": kb_inserted,
        "kb_error": kb_error,
        "groups": groups,
    }

    if write_archive:
        # Optional debug archive (not UI source of truth)
        try:
            snap = {
                **payload,
                "groups": {
                    sid: {**g, "items": []}  # do not duplicate full items in archive by default
                    for sid, g in groups.items()
                },
            }
            # attach sample items for debugging
            for sid, g in groups.items():
                snap["groups"][sid]["items"] = list(g.get("items_sample") or [])
            lp = latest_path()
            lp.parent.mkdir(parents=True, exist_ok=True)
            lp.write_text(json.dumps(snap, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
            archive_section_snapshot(snap)
        except Exception as e:
            logger.debug("sa section archive skip: %s", e)

    return payload


def run_sa_ingest(
    *,
    include_tickers: bool = True,
    include_sections: bool = True,
    all_sections: bool = False,
    limit: Optional[int] = None,
    sleep_sec: Optional[float] = None,
    subs_path: Optional[Path] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Cron/API entry: enabled tickers + sections → knowledge_base."""
    from services.notebook_news_digest import (
        build_sa_fetch_tickers,
        news_quota_config,
        per_ticker_limits_map,
    )

    doc = load_subscriptions(subs_path)
    try:
        sl = float(
            sleep_sec
            if sleep_sec is not None
            else (get_config_value("NOTEBOOK_NEWS_SLEEP_SEC", "0.35") or 0.35)
        )
    except (TypeError, ValueError):
        sl = 0.35

    ticker_part: Dict[str, Any] = {"skipped": not include_tickers}
    if include_tickers:
        uni = build_sa_fetch_tickers(equity_only=True)
        candidates = list(uni.get("sa_fetch_tickers") or uni.get("group3_union") or [])
        tickers = enabled_tickers(candidates, doc=doc)
        quotas = news_quota_config()
        per = int(limit if limit is not None else (quotas.get("fallback") or 40))
        sa_limits = per_ticker_limits_map(
            membership=uni.get("membership") or {},
            sa_extra=uni.get("sa_extra") or [],
            quotas=quotas,
        )
        # Drop muted from limits map noise
        sa_limits = {k: v for k, v in sa_limits.items() if k in set(tickers)}
        raw_mx = (get_config_value("NOTEBOOK_NEWS_MAX_TICKERS", "") or "").strip()
        max_t = int(raw_mx) if raw_mx.isdigit() else None
        if tickers:
            bundle = fetch_and_save_sa_news(
                tickers,
                per_ticker=per,
                per_ticker_limits=sa_limits,
                sleep_sec=sl,
                max_tickers=max_t,
                api_key=api_key,
            )
            ticker_part = {
                "tickers": tickers,
                "kb_inserted": int(bundle.get("kb_inserted") or 0),
                "api_items": len(bundle.get("items") or []),
                "errors": bundle.get("errors") or {},
                "extras_n": len(uni.get("sa_extra") or []),
            }
        else:
            ticker_part = {"tickers": [], "kb_inserted": 0, "api_items": 0, "errors": {}, "note": "no enabled tickers"}

    section_part: Dict[str, Any] = {"skipped": not include_sections}
    if include_sections:
        section_part = ingest_sections_to_kb(
            all_available=all_sections,
            limit=limit if limit is not None else int(doc.get("per_group_limit") or default_per_group_limit()),
            sleep_sec=sl,
            api_key=api_key,
            write_archive=False,
            subs_path=subs_path,
        )

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "tickers": ticker_part,
        "sections": section_part,
        "kb_inserted_total": int(ticker_part.get("kb_inserted") or 0)
        + int(section_part.get("kb_inserted") or 0),
    }


def load_sa_feed(
    *,
    hours: int = 72,
    limit: int = 200,
    section_id: Optional[str] = None,
    symbol: Optional[str] = None,
) -> Dict[str, Any]:
    """Load Seeking Alpha Finance NEWS rows from knowledge_base for UI feed."""
    from sqlalchemy import create_engine, text

    from config_loader import get_database_url

    hours = max(1, min(int(hours), 720))
    lim = max(1, min(int(limit), 500))
    want_sym = str(symbol or "").strip().upper() or None
    if section_id and not want_sym:
        want_sym = section_kb_symbol(section_id)

    sql = """
        SELECT id, ts, ticker, source, content, link,
               COALESCE(NULLIF(symbol, ''), ticker) AS sym,
               raw_payload
        FROM knowledge_base
        WHERE event_type = 'NEWS'
          AND source = :src
          AND ts >= (NOW() AT TIME ZONE 'utc') - make_interval(hours => :hours)
    """
    params: Dict[str, Any] = {"src": KB_SOURCE, "hours": hours, "lim": lim}
    if want_sym:
        sql += " AND (UPPER(TRIM(COALESCE(symbol,''))) = :sym OR UPPER(TRIM(ticker)) = :sym_legacy)"
        params["sym"] = want_sym
        from services.kb_extended_fields import kb_legacy_ticker

        params["sym_legacy"] = kb_legacy_ticker(want_sym)
    sql += " ORDER BY ts DESC LIMIT :lim"

    engine = create_engine(get_database_url())
    rows: List[Dict[str, Any]] = []
    try:
        with engine.connect() as conn:
            for r in conn.execute(text(sql), params).mappings():
                content = str(r.get("content") or "")
                title = content.split("\n\n", 1)[0].strip()[:500]
                body = ""
                if "\n\n" in content:
                    parts = content.split("\n\n")
                    body = " ".join(parts[1:]).strip()
                    if body and body.rsplit("\n", 1)[-1].startswith("http"):
                        body = "\n".join(body.split("\n")[:-1]).strip()
                sym = str(r.get("sym") or r.get("ticker") or "").strip()
                ts = r.get("ts")
                publish = ts.isoformat() if hasattr(ts, "isoformat") else str(ts or "")
                sid = parse_section_id_from_symbol(sym)
                rows.append(
                    {
                        "id": str(r.get("id") or ""),
                        "symbol": sym,
                        "section_id": sid,
                        "title": title,
                        "summary_text": (body or title)[:900],
                        "link": str(r.get("link") or ""),
                        "publishOn": publish,
                        "src": str(r.get("source") or KB_SOURCE),
                    }
                )
    finally:
        engine.dispose()

    return {
        "hours": hours,
        "limit": lim,
        "filter_symbol": want_sym,
        "count": len(rows),
        "items": rows,
        "source": KB_SOURCE,
    }


# --- optional file archive helpers (debug / CLI) ---


def _stamp_from_generated_at(generated_at_utc: str) -> str:
    s = str(generated_at_utc or "").strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y%m%dT%H%M%SZ")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def prune_section_snapshots(
    *,
    retain_days: Optional[int] = None,
    directory: Optional[Path] = None,
) -> int:
    days = int(retain_days if retain_days is not None else snapshot_retain_days())
    root = snapshots_dir(directory)
    if not root.is_dir():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
    removed = 0
    for p in root.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                removed += 1
        except Exception as e:
            logger.debug("prune sa section snap %s: %s", p, e)
    return removed


def archive_section_snapshot(
    payload: Dict[str, Any],
    *,
    directory: Optional[Path] = None,
) -> Path:
    root = snapshots_dir(directory)
    root.mkdir(parents=True, exist_ok=True)
    stamp = _stamp_from_generated_at(str(payload.get("generated_at_utc") or ""))
    path = root / f"{stamp}.json"
    n = 1
    while path.is_file():
        path = root / f"{stamp}_{n}.json"
        n += 1
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    prune_section_snapshots(directory=root)
    return path


def list_section_snapshots(
    *,
    directory: Optional[Path] = None,
    latest: Optional[Path] = None,
    limit: int = 60,
) -> List[Dict[str, Any]]:
    root = snapshots_dir(directory)
    out: List[Dict[str, Any]] = []

    def _meta(p: Path, *, sid: str, is_latest: bool = False) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}
        return {
            "id": sid,
            "generated_at_utc": str(data.get("generated_at_utc") or ""),
            "per_group_limit": data.get("per_group_limit"),
            "section_count": len(groups),
            "item_count": int(data.get("item_count") or 0),
            "kb_inserted": data.get("kb_inserted"),
            "is_latest": bool(is_latest),
        }

    lp = latest_path(latest)
    if lp.is_file():
        m = _meta(lp, sid="latest", is_latest=True)
        if m:
            out.append(m)
    if root.is_dir():
        for p in sorted(root.glob("*.json"), key=lambda x: x.name, reverse=True):
            m = _meta(p, sid=p.stem, is_latest=False)
            if m:
                out.append(m)
    return out[: max(1, int(limit))]


def load_section_snapshot(
    snapshot_id: str,
    *,
    directory: Optional[Path] = None,
    latest: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    sid = str(snapshot_id or "").strip()
    if not sid:
        return None
    if sid == "latest":
        p = latest_path(latest)
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        data = dict(data)
        data["id"] = "latest"
        data["is_latest"] = True
        return data
    if not re.match(r"^[\w.\-]+$", sid) or ".." in sid or "/" in sid or "\\" in sid:
        return None
    root = snapshots_dir(directory)
    path = root / f"{sid}.json"
    if not path.is_file():
        matches = sorted(root.glob(f"{sid}*.json")) if root.is_dir() else []
        if not matches:
            return None
        path = matches[0]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    data = dict(data)
    data["id"] = sid
    data["is_latest"] = False
    return data


# Back-compat alias used by older CLI/API
def run_section_snapshot(**kwargs: Any) -> Dict[str, Any]:
    """Deprecated: prefer ingest_sections_to_kb / run_sa_ingest. Writes KB + optional archive."""
    kwargs.setdefault("write_archive", True)
    return ingest_sections_to_kb(**kwargs)
