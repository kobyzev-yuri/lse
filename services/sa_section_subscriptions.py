"""Seeking Alpha tipsters section subscriptions + raw snapshots (notebook).

Not mixed into morning LLM digest. File-backed like digest snapshots.
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
from services.seeking_alpha_finance import fetch_section_pages, rapidapi_key

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = _REPO_ROOT / "local" / "notebook" / "sa_sections"
DEFAULT_SUBS_PATH = _REPO_ROOT / "local" / "notebook" / "sa_section_subscriptions.json"
DEFAULT_LATEST_PATH = DEFAULT_OUT_DIR / "latest.json"
DEFAULT_SNAPSHOTS_DIR = DEFAULT_OUT_DIR / "snapshots"

# Tipsters probe (2026-08-04): /v1/news/list → 422; working article categories + day-watch.
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
    # Documented as unavailable after probe — shown disabled in UI.
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


def subscriptions_path(path: Optional[Path] = None) -> Path:
    return path or DEFAULT_SUBS_PATH


def snapshots_dir(path: Optional[Path] = None) -> Path:
    return path or DEFAULT_SNAPSHOTS_DIR


def latest_path(path: Optional[Path] = None) -> Path:
    return path or DEFAULT_LATEST_PATH


def _default_subs_doc() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "per_group_limit": default_per_group_limit(),
        "subscriptions": {},
        "updated_at_utc": "",
    }


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
    subs = data.get("subscriptions") if isinstance(data.get("subscriptions"), dict) else {}
    clean: Dict[str, bool] = {}
    for k, v in subs.items():
        kid = str(k).strip()
        if not kid:
            continue
        clean[kid] = bool(v)
    out["subscriptions"] = clean
    out["updated_at_utc"] = str(data.get("updated_at_utc") or "")
    out["schema_version"] = int(data.get("schema_version") or 1)
    return out


def save_subscriptions(doc: Dict[str, Any], *, path: Optional[Path] = None) -> Dict[str, Any]:
    p = subscriptions_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "per_group_limit": max(1, min(int(doc.get("per_group_limit") or default_per_group_limit()), 200)),
        "subscriptions": {
            str(k): bool(v)
            for k, v in (doc.get("subscriptions") or {}).items()
            if str(k).strip()
        },
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
    subs = dict(doc.get("subscriptions") or {})
    if enabled:
        subs[sid] = True
    else:
        subs.pop(sid, None)
        # also clear explicit false
        for k in list(subs.keys()):
            if k == sid:
                del subs[k]
    doc["subscriptions"] = subs
    return save_subscriptions(doc, path=path)


def set_subscriptions_bulk(
    mapping: Dict[str, bool],
    *,
    per_group_limit: Optional[int] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Update many subscription flags; unknown ids ignored; unavailable cannot enable."""
    doc = load_subscriptions(path)
    catalog = catalog_by_id()
    subs = dict(doc.get("subscriptions") or {})
    for raw_id, raw_en in (mapping or {}).items():
        sid = str(raw_id or "").strip()
        if not sid or sid not in catalog:
            continue
        en = bool(raw_en)
        if en and not catalog[sid].get("available"):
            continue
        if en:
            subs[sid] = True
        else:
            subs.pop(sid, None)
    doc["subscriptions"] = subs
    if per_group_limit is not None:
        try:
            doc["per_group_limit"] = max(1, min(int(per_group_limit), 200))
        except (TypeError, ValueError):
            pass
    return save_subscriptions(doc, path=path)


def enabled_section_ids(doc: Optional[Dict[str, Any]] = None) -> List[str]:
    d = doc if isinstance(doc, dict) else load_subscriptions()
    catalog = catalog_by_id()
    out: List[str] = []
    for sid, en in (d.get("subscriptions") or {}).items():
        if not en:
            continue
        cat = catalog.get(str(sid))
        if cat and cat.get("available"):
            out.append(str(sid))
    return out


def catalog_with_subscriptions(
    *,
    subs_path: Optional[Path] = None,
) -> Dict[str, Any]:
    doc = load_subscriptions(subs_path)
    enabled = set(enabled_section_ids(doc))
    rows = []
    for cat in SECTION_CATALOG:
        row = dict(cat)
        sid = str(row.get("id") or "")
        row["subscribed"] = sid in enabled
        rows.append(row)
    latest = load_latest_snapshot()
    return {
        "catalog": rows,
        "subscriptions": doc.get("subscriptions") or {},
        "per_group_limit": int(doc.get("per_group_limit") or default_per_group_limit()),
        "updated_at_utc": doc.get("updated_at_utc") or "",
        "has_api_key": bool(rapidapi_key()),
        "latest_snapshot": {
            "id": latest.get("id") if latest else None,
            "generated_at_utc": latest.get("generated_at_utc") if latest else None,
            "section_count": len((latest or {}).get("groups") or {}),
            "item_count": int((latest or {}).get("item_count") or 0),
        }
        if latest
        else None,
    }


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
            "item_count": int(data.get("item_count") or sum(int(g.get("count") or 0) for g in groups.values() if isinstance(g, dict))),
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
        return load_latest_snapshot(latest)
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
    except Exception as e:
        logger.debug("load sa section snap %s: %s", sid, e)
        return None
    if not isinstance(data, dict):
        return None
    data = dict(data)
    data["id"] = sid
    data["is_latest"] = False
    return data


def load_latest_snapshot(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    p = latest_path(path)
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


def run_section_snapshot(
    *,
    section_ids: Optional[Sequence[str]] = None,
    all_available: bool = False,
    limit: Optional[int] = None,
    sleep_sec: float = 0.3,
    write: bool = True,
    subs_path: Optional[Path] = None,
    latest: Optional[Path] = None,
    archive_dir: Optional[Path] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch subscribed (or given) sections → raw snapshot file."""
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

    # Guard RapidAPI quota: hard cap sections per run.
    max_sections = 12
    try:
        max_sections = max(1, min(int(get_config_value("NOTEBOOK_SA_SECTION_MAX_PER_RUN", "12") or 12), 30))
    except (TypeError, ValueError):
        max_sections = 12
    if len(wanted) > max_sections:
        wanted = wanted[:max_sections]

    generated = datetime.now(timezone.utc).isoformat()
    groups: Dict[str, Any] = {}
    total = 0
    for i, sid in enumerate(wanted):
        cat = catalog.get(sid)
        if not cat or not cat.get("available"):
            groups[sid] = {
                "status": "unavailable",
                "count": 0,
                "items": [],
                "endpoint": (cat or {}).get("path"),
                "error": "unknown or unavailable section",
            }
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
            items = bundle.get("items") or []
            err = "; ".join(bundle.get("errors") or [])
            status = "ok" if int(bundle.get("status") or 0) == 200 and items else (
                "error" if bundle.get("errors") else "empty"
            )
            groups[sid] = {
                "status": status,
                "count": len(items),
                "items": items,
                "endpoint": path,
                "params": params,
                "urls": bundle.get("urls") or [],
                "error": err or None,
                "title": cat.get("title"),
            }
            total += len(items)
        except Exception as e:
            logger.warning("sa section %s fetch failed: %s", sid, e)
            groups[sid] = {
                "status": "error",
                "count": 0,
                "items": [],
                "endpoint": path,
                "params": params,
                "error": str(e),
                "title": cat.get("title"),
            }
        if sleep_sec > 0 and i + 1 < len(wanted):
            time.sleep(float(sleep_sec))

    stamp = _stamp_from_generated_at(generated)
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "id": stamp,
        "generated_at_utc": generated,
        "per_group_limit": per_limit,
        "requested_sections": wanted,
        "item_count": total,
        "groups": groups,
    }

    if write:
        lp = latest_path(latest)
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        arch = archive_section_snapshot(payload, directory=archive_dir)
        payload["archive_path"] = str(arch)
        payload["id"] = "latest"
        payload["is_latest"] = True
        payload["archive_id"] = arch.stem

    return payload
