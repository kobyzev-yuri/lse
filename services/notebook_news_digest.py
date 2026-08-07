"""Notebook news digest: notebook groups → SA Finance → knowledge_base → LLM.

Universe = tickers in notebook groups 1/2/3/n (notebook_data.json + UI overlay).
Independent of portfolio / GAME_5M lists (those are fallback only if notebook is empty).
Digest JSON is UI/Telegram cache only.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from config_loader import get_config_value
from services.earnings_intelligence_universe import is_equity_symbol
from services.seeking_alpha_finance import (
    KB_SOURCE,
    fetch_and_save_sa_news,
    load_kb_earnings_items,
    load_kb_news_items,
    notebook_kb_news_provider,
    notebook_news_sheet_only,
)
from services.ticker_groups import get_tickers_for_portfolio_game, get_tickers_game_5m

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = _REPO_ROOT / "local" / "notebook"
DEFAULT_DIGEST_PATH = DEFAULT_OUT_DIR / "digest_latest.json"
DEFAULT_RAW_PATH = DEFAULT_OUT_DIR / "news_raw_latest.json"
DEFAULT_DIGESTS_DIR = DEFAULT_OUT_DIR / "digests"


def digest_retain_days() -> int:
    try:
        n = int((get_config_value("NOTEBOOK_DIGEST_RETAIN_DAYS", "14") or "14").strip())
    except (TypeError, ValueError):
        n = 14
    return max(1, min(n, 90))


def digests_dir(path: Optional[Path] = None) -> Path:
    return path or DEFAULT_DIGESTS_DIR


def _snapshot_payload(
    *,
    digest: Dict[str, Any],
    universe: Dict[str, Any],
    generated_at_utc: str,
    pipeline: Dict[str, Any],
    requested_tickers: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    pipe = dict(pipeline or {})
    if requested_tickers is not None and "requested_ticker_count" not in pipe:
        pipe["requested_ticker_count"] = len(list(requested_tickers))
    return {
        "digest": digest,
        "universe": universe,
        "generated_at_utc": generated_at_utc,
        "pipeline": pipe,
        "requested_tickers": list(requested_tickers or []),
    }


def _stamp_from_generated_at(generated_at_utc: str) -> str:
    """UTC stamp for filename: 20260728T123045Z."""
    s = str(generated_at_utc or "").strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y%m%dT%H%M%SZ")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def prune_digest_snapshots(
    *,
    retain_days: Optional[int] = None,
    directory: Optional[Path] = None,
) -> int:
    """Delete archived digests older than retain_days. Returns number removed."""
    days = int(retain_days if retain_days is not None else digest_retain_days())
    root = digests_dir(directory)
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
            logger.debug("prune digest %s: %s", p, e)
    return removed


def archive_digest_snapshot(
    payload: Dict[str, Any],
    *,
    directory: Optional[Path] = None,
) -> Path:
    """Write one snapshot file and prune old ones. Returns path written."""
    root = digests_dir(directory)
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
    prune_digest_snapshots(directory=root)
    return path


def list_digest_snapshots(
    *,
    directory: Optional[Path] = None,
    limit: int = 60,
) -> List[Dict[str, Any]]:
    """Newest-first list of digests: synthetic `latest` + archived files."""
    root = digests_dir(directory)
    out: List[Dict[str, Any]] = []

    def _meta_from_file(p: Path, *, sid: str, is_latest: bool = False) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        dig = data.get("digest") if isinstance(data.get("digest"), dict) else {}
        if not dig and "signals" in data:
            dig = data
        gen = str(data.get("generated_at_utc") or dig.get("date") or "")
        pipe = data.get("pipeline") if isinstance(data.get("pipeline"), dict) else {}
        return {
            "id": sid,
            "generated_at_utc": gen,
            "date": dig.get("date") or gen,
            "filtered": int(dig.get("filtered") or 0),
            "kept": int(dig.get("kept") or 0),
            "trashed": int(dig.get("trashed") or 0),
            "is_latest": bool(is_latest),
            "lookback_hours": pipe.get("lookback_hours"),
        }

    if DEFAULT_DIGEST_PATH.is_file():
        m = _meta_from_file(DEFAULT_DIGEST_PATH, sid="latest", is_latest=True)
        if m:
            out.append(m)

    if root.is_dir():
        files = sorted(root.glob("*.json"), key=lambda x: x.name, reverse=True)
        for p in files:
            m = _meta_from_file(p, sid=p.stem, is_latest=False)
            if m:
                out.append(m)

    return out[: max(1, int(limit))]


def load_digest_snapshot(
    snapshot_id: str,
    *,
    directory: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Load full snapshot payload by id (`latest` or archive stamp)."""
    sid = str(snapshot_id or "").strip()
    if not sid:
        return None
    if sid == "latest":
        return load_latest_digest_pack()
    # Allow only safe filenames
    if not re.match(r"^[\w.\-]+$", sid) or ".." in sid or "/" in sid or "\\" in sid:
        return None
    root = digests_dir(directory)
    path = root / f"{sid}.json"
    if not path.is_file():
        # try prefix match for stamped variants
        matches = sorted(root.glob(f"{sid}*.json")) if root.is_dir() else []
        if not matches:
            return None
        path = matches[0]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("load digest snapshot %s: %s", sid, e)
        return None
    if not isinstance(data, dict):
        return None
    dig = data.get("digest") if isinstance(data.get("digest"), dict) else None
    if dig is None and "signals" in data:
        dig = data
        data = {
            "digest": dig,
            "universe": {},
            "generated_at_utc": dig.get("date"),
            "pipeline": {},
        }
    if not isinstance(dig, dict):
        return None
    data = dict(data)
    data["id"] = sid
    data["is_latest"] = False
    return data


def load_latest_digest_pack(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Full latest file: digest + pipeline + universe (for boot / API)."""
    p = path or DEFAULT_DIGEST_PATH
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("load digest pack failed: %s", e)
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("digest"), dict):
        out = dict(data)
        out["id"] = "latest"
        out["is_latest"] = True
        return out
    if "signals" in data:
        return {
            "id": "latest",
            "is_latest": True,
            "digest": data,
            "universe": {},
            "generated_at_utc": data.get("date"),
            "pipeline": {},
        }
    return None


def _json_default(obj: Any):
    """Serialize Decimal/date from KB rows for digest cache files."""
    try:
        from decimal import Decimal

        if isinstance(obj, Decimal):
            return float(obj)
    except Exception:
        pass
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            pass
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _clip_digest_field(val: Any, limit: int) -> str:
    s = str(val or "").strip()
    if len(s) <= limit:
        return s
    cut = s[: max(0, limit - 1)].rstrip(" ,.;:·")
    return cut + "…"


def clamp_digest_rows_brief(
    rows: Any,
    *,
    max_rows: int = 12,
    text_limit: int = 160,
    tac_limit: int = 100,
) -> List[Dict[str, Any]]:
    """Enforce NBIS-style brevity on digest bucket rows (post-LLM)."""
    if not isinstance(rows, list):
        return []
    out: List[Dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        row["sym"] = str(row.get("sym") or "").strip()[:40]
        row["src"] = str(row.get("src") or "").strip()[:80]
        row["link"] = str(row.get("link") or "").strip()[:400]
        row["date"] = str(row.get("date") or "").strip()[:40]
        row["text"] = _clip_digest_field(row.get("text"), text_limit)
        tac = str(row.get("tac") or "").strip()
        # Keep a simple HTML prefix if model used it; still hard-cap length.
        row["tac"] = _clip_digest_field(tac, tac_limit)
        if not row["text"] and not row["sym"]:
            continue
        out.append(row)
        if len(out) >= max_rows:
            break
    return out

# Extra SA RapidAPI tickers beyond notebook universe (macro / peers from RTA email overlap).
# Override via NOTEBOOK_NEWS_SA_EXTRA=SPY,QQQ,... or empty to disable.
DEFAULT_SA_EXTRA_TICKERS: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "NVDA",
    "INTC",
    "AAPL",
    "PYPL",
    "KEYS",
    "VZ",
)


def _format_article_date(raw: Any) -> str:
    """Human UTC stamp for digest cards, e.g. 2026-07-28 14:30 UTC."""
    s = str(raw or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        # Already short / human — keep as-is (trim noise).
        return s[:32]


def _link_match_keys(url: str) -> List[str]:
    """Keys for fuzzy article match: full norm link, path slug, trailing numeric id."""
    lk = _norm_link(url)
    if not lk:
        return []
    keys: List[str] = [lk]
    path = lk.split("/", 1)[-1] if "/" in lk else lk
    last = path.rsplit("/", 1)[-1]
    last = re.sub(r"\.(html?|htm)$", "", last, flags=re.I)
    if last:
        keys.append(f"slug:{last}")
        # Truncated LLM links often drop the final letter(s) of the slug.
        if len(last) >= 16:
            keys.append(f"slugprefix:{last[:16]}")
        m = re.search(r"(\d{8,})", last)
        if m:
            keys.append(f"id:{m.group(1)}")
    return keys


def enrich_digest_rows_with_dates(
    rows: Sequence[Dict[str, Any]],
    source_items: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Fill missing date/prem from KB/API publishOn matched by link (or title)."""
    by_key: Dict[str, str] = {}
    for it in source_items:
        if not isinstance(it, dict):
            continue
        stamp = _format_article_date(it.get("publishOn") or it.get("ts"))
        if not stamp:
            continue
        for k in _link_match_keys(str(it.get("link") or "")):
            by_key.setdefault(k, stamp)
        tk = _norm_title(str(it.get("title") or ""))
        if tk:
            by_key.setdefault(f"title:{tk}", stamp)

    def _lookup(link: str, text: str = "") -> str:
        for k in _link_match_keys(link):
            if k in by_key:
                return by_key[k]
        lk = _norm_link(link)
        if lk:
            for k, stamp in by_key.items():
                if not k.startswith("slug:") and not k.startswith("id:") and not k.startswith("title:"):
                    if k.startswith(lk) or lk.startswith(k):
                        return stamp
            # slug prefix vs full slug
            for k, stamp in by_key.items():
                if k.startswith("slug:") and lk:
                    slug = k[5:]
                    last = lk.rsplit("/", 1)[-1]
                    last = re.sub(r"\.(html?|htm)$", "", last, flags=re.I)
                    if slug.startswith(last) or last.startswith(slug[: max(12, len(last))]):
                        return stamp
        tk = _norm_title(text)
        if tk:
            return by_key.get(f"title:{tk}", "")
        return ""

    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        r = dict(row)
        existing = _format_article_date(r.get("date") or r.get("prem"))
        if not existing:
            existing = _lookup(str(r.get("link") or ""), str(r.get("text") or r.get("title") or ""))
        if existing:
            r["date"] = existing
            if not r.get("prem"):
                r["prem"] = existing
        out.append(r)
    return out


def _unique_upper(seq: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for t in seq:
        u = str(t or "").strip().upper()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _truthy_cfg(name: str, default: str = "0") -> bool:
    return (get_config_value(name, default) or default).strip().lower() in ("1", "true", "yes", "on")


def _cfg_int(name: str, default: int) -> int:
    try:
        return int((get_config_value(name, str(default)) or str(default)).strip())
    except (TypeError, ValueError):
        return int(default)


def digest_output_limits() -> Dict[str, int]:
    """UI/LLM output caps for morning digest buckets (env-overridable)."""
    return {
        "signals": max(1, min(_cfg_int("NOTEBOOK_DIGEST_MAX_SIGNALS", 12), 24)),
        "risks": max(1, min(_cfg_int("NOTEBOOK_DIGEST_MAX_RISKS", 12), 24)),
        "macro": max(1, min(_cfg_int("NOTEBOOK_DIGEST_MAX_MACRO", 6), 16)),
        "newtickers": max(1, min(_cfg_int("NOTEBOOK_DIGEST_MAX_NEWTICKERS", 5), 12)),
        "text_chars": max(60, min(_cfg_int("NOTEBOOK_DIGEST_TEXT_CHARS", 160), 280)),
        "tac_chars": max(40, min(_cfg_int("NOTEBOOK_DIGEST_TAC_CHARS", 100), 160)),
    }


def digest_system_prompt(*, limits: Optional[Dict[str, int]] = None) -> str:
    lim = limits or digest_output_limits()
    return f"""Ты — редактор утреннего дайджеста для торговой «Рабочей тетрадки».
Стиль — как паспорт NBIS: коротко, по делу, без доклада и без «стены текста».
Вход: (1) новости KB (SA/Yahoo/Investing/…); (2) earnings-факты, если переданы.
Дубли одной истории схлопывай.

Верни ТОЛЬКО JSON без markdown:
{{
  "filtered": <int>,
  "kept": <int>,
  "trashed": <int>,
  "signals": [{{"sym":"MSFT","src":"...","text":"...","tac":"<b>Тактика:</b> …","link":"...","date":"2026-07-28 09:15 UTC"}}],
  "risks": [тот же формат],
  "macro": [{{"sym":"МАКРО · …","src":"...","text":"...","tac":"<b>Влияние:</b> …","link":"...","date":"..."}}],
  "newtickers": [{{"sym":"XYZ","src":"...","text":"...","tac":"<b>Обоснование:</b> …","link":"...","date":"..."}}],
  "trashNote": "1 фраза: что отсеяно"
}}

Лимиты (жёстко):
- signals ≤ {lim["signals"]}, risks ≤ {lim["risks"]}, macro ≤ {lim["macro"]}, newtickers ≤ {lim["newtickers"]}.
- text: ОДНО короткое предложение, ≤{lim["text_chars"]} символов (факт + цифра). Не эссе.
- tac: ≤{lim["tac_chars"]} символов; только действие тетрадки (Buy Dip / Hold / пауза / ждать уровень / не докупать).
- Одна история → один пункт; src — главный источник; date из входа, не выдумывай.

Корзины:
- signals: катализатор по нашим тикерам.
- risks: даунсайд / отчёт скоро / peer spillover (MU↔SNDK, LITE↔CIEN, hyperscalers).
- macro: ФРС / сектор / гео → влияние на Environment.
- newtickers: НЕ из текущего списка групп тетрадки.

Не выдумывай новости. Отсев шума — норма; при широком входе держи корзины ближе к лимитам, если есть смысл. Мало данных → пустые массивы ок.
"""


# Back-compat alias for imports/tests that expect a string constant.
DIGEST_SYSTEM = digest_system_prompt()


_MACRO_SYMS = frozenset({"MACRO", "US_MACRO"})


def news_quota_config() -> Dict[str, int]:
    """Resolved per-group / MACRO / LLM caps for ingest + fair-sample."""
    fallback = max(0, _cfg_int("NOTEBOOK_NEWS_PER_TICKER", 40))
    try:
        from services.sa_section_subscriptions import default_per_group_limit

        section_default = default_per_group_limit()
    except Exception:
        section_default = 40
    return {
        "fallback": fallback,
        "g1": max(0, _cfg_int("NOTEBOOK_NEWS_PER_TICKER_G1", fallback)),
        "g2": max(0, _cfg_int("NOTEBOOK_NEWS_PER_TICKER_G2", fallback)),
        "g3": max(0, _cfg_int("NOTEBOOK_NEWS_PER_TICKER_G3", fallback)),
        "new": max(0, _cfg_int("NOTEBOOK_NEWS_PER_TICKER_NEW", fallback)),
        "extra": max(0, _cfg_int("NOTEBOOK_NEWS_PER_TICKER_EXTRA", fallback)),
        "macro_limit": max(0, _cfg_int("NOTEBOOK_NEWS_MACRO_LIMIT", 100)),
        "section_limit": max(0, _cfg_int("NOTEBOOK_SA_SECTION_LIMIT", section_default)),
        # ~25 tickers × 40 + MACRO/sections; raise further if universe grows past ~30.
        "llm_max_items": max(1, _cfg_int("NOTEBOOK_NEWS_LLM_MAX_ITEMS", 1000)),
    }


def per_ticker_limit_for(
    sym: str,
    membership: Optional[Dict[str, List[str]]] = None,
    *,
    quotas: Optional[Dict[str, int]] = None,
    is_extra: bool = False,
) -> int:
    """Max articles for one symbol (ingest or fair-sample). Multi-group → max of caps."""
    q = quotas or news_quota_config()
    if is_extra:
        return int(q.get("extra") or q.get("fallback") or 40)
    u = str(sym or "").strip().upper()
    if u in _MACRO_SYMS:
        return int(q.get("macro_limit") or 0)
    if u.startswith("SA:"):
        return int(q.get("section_limit") or q.get("fallback") or 40)
    tags = list((membership or {}).get(u) or [])
    tag_to_key = {"g1": "g1", "g2": "g2", "g3": "g3", "new": "new"}
    caps = [int(q[tag_to_key[t]]) for t in tags if t in tag_to_key and tag_to_key[t] in q]
    if caps:
        return max(caps)
    return int(q.get("fallback") or 40)


def per_ticker_limits_map(
    *,
    membership: Dict[str, List[str]],
    sa_extra: Optional[Sequence[str]] = None,
    quotas: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    """Symbol → SA fetch / sample limit for cron + digest ingest."""
    q = quotas or news_quota_config()
    out: Dict[str, int] = {}
    for sym, tags in (membership or {}).items():
        u = str(sym or "").strip().upper()
        if not u or u in _MACRO_SYMS:
            continue
        out[u] = per_ticker_limit_for(u, {u: list(tags or [])}, quotas=q)
    for t in sa_extra or []:
        u = str(t or "").strip().upper()
        if not u or u in _MACRO_SYMS:
            continue
        if u not in out:
            out[u] = per_ticker_limit_for(u, membership, quotas=q, is_extra=True)
    return out


def _item_publish_key(it: Dict[str, Any]) -> str:
    return str(it.get("publishOn") or it.get("ts") or "")


def fair_sample_for_digest(
    items: Sequence[Dict[str, Any]],
    membership: Dict[str, List[str]],
    *,
    quotas: Optional[Dict[str, int]] = None,
    include_macro: bool = True,
) -> Dict[str, Any]:
    """Cap per ticker by group + MACRO budget, then pack to llm_max_items (newest first)."""
    q = quotas or news_quota_config()
    macro_lim = int(q.get("macro_limit") or 0) if include_macro else 0
    llm_max = int(q.get("llm_max_items") or 1000)

    by_sym: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        sym = str(it.get("ticker") or "").strip().upper() or "UNKNOWN"
        by_sym.setdefault(sym, []).append(it)

    for rows in by_sym.values():
        rows.sort(key=_item_publish_key, reverse=True)

    picked: List[Dict[str, Any]] = []
    per_counts: Dict[str, int] = {}
    macro_n = 0

    for sym, rows in by_sym.items():
        if sym in _MACRO_SYMS:
            take = rows[:macro_lim]
            macro_n += len(take)
            picked.extend(take)
            per_counts[sym] = len(take)
            continue
        lim = per_ticker_limit_for(sym, membership, quotas=q)
        take = rows[:lim]
        per_counts[sym] = len(take)
        picked.extend(take)

    picked.sort(key=_item_publish_key, reverse=True)
    before_pack = len(picked)
    if len(picked) > llm_max:
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for it in picked:
            sym = str(it.get("ticker") or "").strip().upper() or "UNKNOWN"
            buckets.setdefault(sym, []).append(it)
        for rows in buckets.values():
            rows.sort(key=_item_publish_key, reverse=True)
        packed: List[Dict[str, Any]] = []
        while len(packed) < llm_max and buckets:
            progressed = False
            for sym in list(buckets.keys()):
                if len(packed) >= llm_max:
                    break
                rows = buckets.get(sym) or []
                if not rows:
                    buckets.pop(sym, None)
                    continue
                packed.append(rows.pop(0))
                progressed = True
                if not rows:
                    buckets.pop(sym, None)
            if not progressed:
                break
        packed.sort(key=_item_publish_key, reverse=True)
        picked = packed

    return {
        "items": picked,
        "after_fair_sample": len(picked),
        "before_pack": before_pack,
        "per_ticker_counts": per_counts,
        "macro_count": macro_n,
        "macro_limit": macro_lim,
        "llm_max_items": llm_max,
        "quotas": {
            "g1": q.get("g1"),
            "g2": q.get("g2"),
            "g3": q.get("g3"),
            "new": q.get("new"),
            "extra": q.get("extra"),
            "fallback": q.get("fallback"),
        },
    }


_SOURCE_RANK = {
    "seeking alpha finance": 0,
    "seeking alpha": 1,
    "reuters": 2,
    "bloomberg": 3,
    "the wall street journal": 4,
    "barrons.com": 5,
    "yahoo finance": 10,
    "investing.com": 11,
    "investing.com news": 11,
}


def _norm_link(url: str) -> str:
    s = (url or "").strip().lower()
    if not s:
        return ""
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    if s.startswith("www."):
        s = s[4:]
    s = s.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return s


def _norm_title(title: str) -> str:
    t = (title or "").strip().lower()
    t = re.sub(r"[^\w\s]+", " ", t, flags=re.UNICODE)
    return " ".join(t.split())


def _source_rank(src: str) -> int:
    return _SOURCE_RANK.get((src or "").strip().lower(), 50)


def dedupe_news_items(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop cross-source repeats: same link, or same ticker+normalized title.

    Keeps the better source (SA/wire preferred) and longer summary when tied.
    """

    def _score(it: Dict[str, Any]) -> tuple:
        return (
            -_source_rank(str(it.get("src") or "")),
            len(str(it.get("summary_text") or "")),
        )

    # Pass 1: group by keys, pick winner per group, then merge overlapping groups.
    groups: List[Dict[str, Any]] = []  # {keys: set, item: dict}

    for it in items:
        if not isinstance(it, dict):
            continue
        link_k = _norm_link(str(it.get("link") or ""))
        sym = str(it.get("ticker") or "").strip().upper()
        title_k = _norm_title(str(it.get("title") or ""))
        keys: Set[str] = set()
        if link_k:
            keys.add(f"url:{link_k}")
        if sym and title_k:
            keys.add(f"tt:{sym}|{title_k}")
        if not keys:
            keys.add(f"id:{it.get('id') or it.get('kb_id') or id(it)}")

        hit_idx = None
        for i, g in enumerate(groups):
            if keys & g["keys"]:
                hit_idx = i
                break
        if hit_idx is None:
            groups.append({"keys": set(keys), "item": dict(it)})
            continue
        g = groups[hit_idx]
        g["keys"] |= keys
        if _score(it) > _score(g["item"]):
            g["item"] = dict(it)
        # Merge any other groups that now overlap (rare title/url chain)
        changed = True
        while changed:
            changed = False
            for j in range(len(groups) - 1, -1, -1):
                if j == hit_idx:
                    continue
                if groups[j]["keys"] & groups[hit_idx]["keys"]:
                    other = groups.pop(j)
                    if j < hit_idx:
                        hit_idx -= 1
                    groups[hit_idx]["keys"] |= other["keys"]
                    if _score(other["item"]) > _score(groups[hit_idx]["item"]):
                        groups[hit_idx]["item"] = other["item"]
                    changed = True

    return [g["item"] for g in groups]


def _tickers_from_notebook_data() -> Dict[str, List[str]]:
    """Group key → equities from notebook JSON + UI overlay (not portfolio / GAME_5M)."""
    try:
        from services.trading_notebook import (
            apply_ticker_overrides,
            load_notebook_data,
            load_notebook_overrides,
            normalize_notebook_group,
        )

        data = load_notebook_data()
        base = data.get("tickers") if isinstance(data.get("tickers"), dict) else {}
        merged = apply_ticker_overrides(base, overrides=load_notebook_overrides())
    except Exception as e:
        logger.debug("notebook tickers load for universe: %s", e)
        return {"1": [], "2": [], "3": [], "n": []}
    by_g: Dict[str, List[str]] = {"1": [], "2": [], "3": [], "n": []}
    for sym, row in merged.items():
        if not isinstance(row, dict):
            continue
        u = str(sym or row.get("sym") or "").strip().upper()
        if not u or not is_equity_symbol(u):
            continue
        try:
            g = normalize_notebook_group(row.get("group"))
        except Exception:
            g = "3"
        if u not in by_g[g]:
            by_g[g].append(u)
    return by_g


def build_news_universe(*, equity_only: bool = True) -> Dict[str, Any]:
    """Notebook groups (base+overlay) first; portfolio∪5m only if notebook empty or forced."""
    mode = (get_config_value("NOTEBOOK_NEWS_UNIVERSE", "notebook") or "notebook").strip().lower()
    nb = _tickers_from_notebook_data()
    g1, g2, g3, gn = nb.get("1") or [], nb.get("2") or [], nb.get("3") or [], nb.get("n") or []
    notebook_union = _unique_upper(g1 + g2 + g3 + gn)

    use_config_fallback = mode in ("config", "portfolio_5m", "legacy") or (
        mode == "notebook" and not notebook_union
    )
    if use_config_fallback and not notebook_union:
        portfolio = _unique_upper(get_tickers_for_portfolio_game())
        game5m = _unique_upper(get_tickers_game_5m())
        if equity_only:
            portfolio = [t for t in portfolio if is_equity_symbol(t)]
            game5m = [t for t in game5m if is_equity_symbol(t)]
        g1, g2 = portfolio, game5m
        g3, gn = [], []
        union = _unique_upper(g1 + g2)
        note = (
            "Fallback: в тетрадке нет тикеров → portfolio ∪ GAME_5M. "
            "Добавьте тикеры в группы на /notebook (или notebook_data.json)."
        )
        source = "config_fallback"
    elif mode in ("config", "portfolio_5m", "legacy"):
        portfolio = _unique_upper(get_tickers_for_portfolio_game())
        game5m = _unique_upper(get_tickers_game_5m())
        if equity_only:
            portfolio = [t for t in portfolio if is_equity_symbol(t)]
            game5m = [t for t in game5m if is_equity_symbol(t)]
        g1, g2 = portfolio, game5m
        g3, gn = [], []
        union = _unique_upper(g1 + g2)
        note = "NOTEBOOK_NEWS_UNIVERSE=config: portfolio ∪ GAME_5M (тетрадка игнорируется)."
        source = "config"
    else:
        union = notebook_union
        note = (
            "Universe = тикеры групп тетрадки 1/2/3/n (notebook_data + overlay UI). "
            "Не зависит от portfolio / GAME_5M."
        )
        source = "notebook"

    pset, gset, cset, nset = set(g1), set(g2), set(g3), set(gn)
    membership: Dict[str, List[str]] = {}
    for t in union:
        tags: List[str] = []
        if t in pset:
            tags.append("g1")
        if t in gset:
            tags.append("g2")
        if t in cset:
            tags.append("g3")
        if t in nset:
            tags.append("new")
        if not tags:
            tags = ["notebook"]
        membership[t] = tags

    return {
        "group1_portfolio": list(g1),
        "group2_game_5m": list(g2),
        "group3_candidates": list(g3),
        "group_new": list(gn),
        "group3_union": union,
        "membership": membership,
        "source": source,
        "note_ru": note,
    }


def sa_extra_tickers_from_config() -> List[str]:
    """NOTEBOOK_NEWS_SA_EXTRA — comma list; unset → DEFAULT_SA_EXTRA_TICKERS; empty string → []."""
    raw = get_config_value("NOTEBOOK_NEWS_SA_EXTRA", None)
    if raw is None:
        return list(DEFAULT_SA_EXTRA_TICKERS)
    s = str(raw).strip()
    if not s or s.lower() in ("0", "none", "off", "-"):
        return []
    return _unique_upper(x.strip() for x in s.split(",") if x.strip())


def build_sa_fetch_tickers(*, equity_only: bool = True) -> Dict[str, Any]:
    """
    Tickers for Seeking Alpha Finance ingest cron.

    Notebook universe ∪ NOTEBOOK_NEWS_SA_EXTRA (macro/peers: SPY, INTC, …).
    Extras are NOT added to digest group membership — only widen SA API coverage.
    Keep ETFs like SPY/QQQ in extras even when equity_only=True (macro proxies).
    """
    uni = build_news_universe(equity_only=equity_only)
    base = list(uni.get("group3_union") or [])
    extra = sa_extra_tickers_from_config()
    merged = _unique_upper(base + extra)
    return {
        **uni,
        "sa_extra": extra,
        "sa_fetch_tickers": merged,
        "note_ru": (
            f"{uni.get('note_ru') or ''} SA fetch = notebook ({len(base)}) "
            f"+ extras ({len(extra)}): {', '.join(extra) or '—'}"
        ).strip(),
    }


def format_digest_telegram(digest: Optional[Dict[str, Any]] = None, *, max_items: int = 6) -> str:
    """Compact Telegram text from digest_latest (no LLM)."""
    d = digest
    if d is None:
        d = load_latest_digest()
    if not isinstance(d, dict):
        return "Дайджест тетрадки ещё не собран. Дождитесь утреннего cron (~08:30 ET) или UI /notebook."
    lines = [
        f"Тетрадка · дайджест ({d.get('date') or '—'})",
        f"filtered={d.get('filtered')} kept={d.get('kept')} trashed={d.get('trashed')}",
        "",
    ]

    def _sec(title: str, key: str) -> None:
        rows = d.get(key) if isinstance(d.get(key), list) else []
        lines.append(f"{title} ({len(rows)})")
        if not rows:
            lines.append("  —")
            return
        for row in rows[:max_items]:
            if not isinstance(row, dict):
                continue
            sym = row.get("sym") or "?"
            src = str(row.get("src") or "").strip()
            text = str(row.get("text") or "").strip()
            tac = re.sub(r"<[^>]+>", "", str(row.get("tac") or "")).strip()
            head = f"  • {sym}"
            if src:
                head += f" [{src}]"
            lines.append(f"{head}: {text[:180]}")
            if tac:
                lines.append(f"    → {tac[:160]}")
        if len(rows) > max_items:
            lines.append(f"  … ещё {len(rows) - max_items}")
        lines.append("")

    _sec("Сигналы", "signals")
    _sec("Риски", "risks")
    _sec("Макро", "macro")
    _sec("Новые", "newtickers")
    trash = str(d.get("trashNote") or "").strip()
    if trash:
        lines.append(f"Отсев: {trash[:300]}")
    lines.append("Полный вид: /notebook → Дайджесты")
    return "\n".join(lines).strip()


def _parse_llm_json(text: str) -> Dict[str, Any]:
    if not text or not str(text).strip():
        return {}
    s = str(text).strip()
    fence = re.match(r"^```(?:json)?\s*\r?\n?", s, re.IGNORECASE)
    if fence:
        rest = s[fence.end() :]
        end = rest.rfind("```")
        if end != -1:
            s = rest[:end].strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {"raw": obj}
    except Exception:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(s[start : end + 1])
            return obj if isinstance(obj, dict) else {}
        except Exception:
            pass
    return {"raw_text": text}


def _llm_digest(
    items: List[Dict[str, Any]],
    *,
    membership: Dict[str, List[str]],
    earnings: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    from services.llm_service import LLMService

    slim = []
    for it in items:
        slim.append(
            {
                "ticker": it.get("ticker"),
                "groups": membership.get(str(it.get("ticker") or "").upper(), []),
                "id": it.get("id"),
                "publishOn": it.get("publishOn"),
                "title": it.get("title"),
                "text": it.get("summary_text"),
                "link": it.get("link"),
                "src": it.get("src"),
            }
        )
    earn_slim = []
    for it in earnings or []:
        earn_slim.append(
            {
                "ticker": it.get("ticker"),
                "groups": membership.get(str(it.get("ticker") or "").upper(), []),
                "date": it.get("publishOn"),
                "title": it.get("title"),
                "text": it.get("summary_text"),
                "src": it.get("src"),
            }
        )
    known = sorted({str(t).upper() for t in membership.keys()})
    news_json = json.dumps(slim, ensure_ascii=False)
    earn_json = json.dumps(earn_slim, ensure_ascii=False)
    # Large fair-sample payloads; keep a high ceiling so we do not silently drop half the input.
    try:
        news_cap = int(get_config_value("NOTEBOOK_NEWS_DIGEST_INPUT_CHARS", "400000") or 400000)
    except (TypeError, ValueError):
        news_cap = 400000
    news_cap = max(24000, news_cap)
    user = (
        "Тикеры уже в тетрадке (НЕ класть в newtickers): "
        + ", ".join(known)
        + "\n\nНовости для дайджеста (JSON):\n"
        + news_json[:news_cap]
        + "\n\nEARNINGS календарь Yahoo/yfinance (JSON):\n"
        + earn_json[:8000]
        + "\n\nСобери дайджест по схеме."
    )
    out_lim = digest_output_limits()
    llm = LLMService()
    out = llm.generate_response(
        messages=[{"role": "user", "content": user}],
        system_prompt=digest_system_prompt(limits=out_lim),
        temperature=float(get_config_value("NOTEBOOK_NEWS_DIGEST_TEMPERATURE", "0.2") or 0.2),
        max_tokens=int(get_config_value("NOTEBOOK_NEWS_DIGEST_MAX_TOKENS", "20000") or 20000),
    )
    text = (out or {}).get("response") or ""
    parsed = _parse_llm_json(text)
    if not any(parsed.get(k) for k in ("signals", "risks", "macro", "newtickers")) and not parsed.get("trashNote"):
        logger.warning(
            "LLM digest parse empty/weak (len=%s keys=%s head=%s)",
            len(text),
            list(parsed.keys())[:12],
            (text or "")[:240].replace("\n", " "),
        )
        if parsed.get("raw_text") or parsed.get("raw"):
            parsed = _empty_digest(filtered=len(items), note="LLM вернул неразборчивый JSON (возможен обрез max_tokens).")
            parsed["llm_parse_error"] = True
    parsed["_llm"] = {
        "model": (out or {}).get("model"),
        "usage": (out or {}).get("usage"),
    }
    return parsed


def _empty_digest(*, filtered: int = 0, note: str = "") -> Dict[str, Any]:
    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "filtered": filtered,
        "kept": 0,
        "trashed": filtered,
        "signals": [],
        "risks": [],
        "macro": [],
        "newtickers": [],
        "trashNote": note or "Нет новостей для дайджеста.",
    }


def run_notebook_news_digest(
    *,
    tickers: Optional[Sequence[str]] = None,
    per_ticker: Optional[int] = None,
    max_tickers: Optional[int] = None,
    sleep_sec: Optional[float] = None,
    use_llm: bool = True,
    write: bool = True,
    fetch_sa: bool = True,
    save_kb: bool = True,
    from_kb: bool = True,
    lookback_hours: Optional[int] = None,
    out_digest: Optional[Path] = None,
    out_raw: Optional[Path] = None,
) -> Dict[str, Any]:
    """SA fetch → knowledge_base → LLM digest (JSON cache for /notebook UI)."""
    uni = build_news_universe(equity_only=True)
    wanted = _unique_upper(tickers) if tickers else list(uni["group3_union"])
    membership = {t: uni["membership"].get(t, []) for t in wanted}
    for t in wanted:
        if t not in membership or not membership[t]:
            membership[t] = ["manual"]

    quotas = news_quota_config()
    if per_ticker is not None:
        quotas = dict(quotas)
        quotas["fallback"] = int(per_ticker)
        for k in ("g1", "g2", "g3", "new", "extra"):
            quotas[k] = int(per_ticker)
    per = int(quotas.get("fallback") or 40)
    sa_limits = per_ticker_limits_map(membership=membership, quotas=quotas)
    mx = max_tickers
    if mx is None:
        raw_mx = (get_config_value("NOTEBOOK_NEWS_MAX_TICKERS", "") or "").strip()
        mx = int(raw_mx) if raw_mx.isdigit() else None
    sl = float(sleep_sec if sleep_sec is not None else (get_config_value("NOTEBOOK_NEWS_SLEEP_SEC", "0.35") or 0.35))
    # Default 72h = last 3 days (daily morning digest over Sheet NEWS).
    lb = int(
        lookback_hours
        if lookback_hours is not None
        else (get_config_value("NOTEBOOK_NEWS_KB_LOOKBACK_HOURS", "72") or 72)
    )
    lb = max(1, min(lb, 24 * 14))
    # Notebook digest: SA-only by default (Yahoo/Investing stay in KB for other LSE).
    # NOTEBOOK_NEWS_KB_ALL_SOURCES=1 → all NEWS sources.
    if _truthy_cfg("NOTEBOOK_NEWS_KB_ALL_SOURCES", "0"):
        kb_src: Optional[str] = None
    else:
        kb_src = (get_config_value("NOTEBOOK_NEWS_KB_SOURCE", KB_SOURCE) or KB_SOURCE).strip() or KB_SOURCE
    # Notebook NEWS corpus: Google Sheet by default (NOTEBOOK_NEWS_KB_PROVIDER).
    kb_provider = notebook_kb_news_provider()
    sheet_only = notebook_news_sheet_only()

    requested = wanted[: mx or len(wanted)]
    kb_tickers = list(requested)
    include_macro = _truthy_cfg("NOTEBOOK_NEWS_INCLUDE_MACRO", "1")
    if include_macro and not sheet_only:
        for m in ("MACRO", "US_MACRO"):
            if m not in kb_tickers:
                kb_tickers.append(m)
    # Tipsters sections (SA:<id>) — skip when digest is sheet-only.
    if not sheet_only:
        try:
            from services.sa_section_subscriptions import enabled_section_kb_symbols

            for sa_sym in enabled_section_kb_symbols():
                if sa_sym and sa_sym not in kb_tickers:
                    kb_tickers.append(sa_sym)
        except Exception as e:
            logger.debug("sa section symbols for digest skipped: %s", e)

    fetch_meta: Dict[str, Any] = {"skipped": not fetch_sa}
    kb_inserted = 0
    api_items: List[Dict[str, Any]] = []

    if fetch_sa:
        if save_kb:
            bundle = fetch_and_save_sa_news(
                requested,
                per_ticker=per,
                per_ticker_limits=sa_limits,
                sleep_sec=sl,
                max_tickers=None,
            )
            kb_inserted = int(bundle.get("kb_inserted") or 0)
            api_items = list(bundle.get("items") or [])
            fetch_meta = {
                "fetched_at": bundle.get("fetched_at"),
                "api_item_count": len(api_items),
                "errors": bundle.get("errors") or {},
                "kb_inserted": kb_inserted,
                "kb_error": bundle.get("kb_error"),
            }
        else:
            from services.seeking_alpha_finance import fetch_news_for_tickers

            bundle = fetch_news_for_tickers(
                requested, per_ticker=per, per_ticker_limits=sa_limits, sleep_sec=sl
            )
            api_items = list(bundle.get("items") or [])
            fetch_meta = {
                "fetched_at": bundle.get("fetched_at"),
                "api_item_count": len(api_items),
                "errors": bundle.get("errors") or {},
                "kb_inserted": 0,
                "note": "save_kb=false",
            }

    raw_count = 0
    deduped_drop = 0
    items: List[Dict[str, Any]] = []
    if from_kb:
        try:
            # Load enough rows for fair-sample (per-ticker × groups + MACRO).
            lim = max(
                int(quotas.get("llm_max_items") or 1000),
                per * max(1, len(kb_tickers) or 1) * (3 if kb_src is None else 1),
            )
            lim = min(lim, 3000)
            items = load_kb_news_items(
                kb_tickers if not sheet_only else [],
                lookback_hours=lb,
                source=kb_src,
                limit=lim,
                provider=kb_provider,
                any_symbol=sheet_only,
            )
        except Exception as e:
            logger.exception("KB load for digest failed: %s", e)
            fetch_meta["kb_load_error"] = str(e)
            items = api_items  # fallback to API payload
    else:
        items = api_items

    raw_count = len(items)
    items = dedupe_news_items(items)
    deduped_drop = max(0, raw_count - len(items))
    after_dedupe = len(items)
    fetch_meta["raw_item_count"] = raw_count
    fetch_meta["deduped_drop"] = deduped_drop
    fetch_meta["item_count_after_dedupe"] = after_dedupe
    fetch_meta["kb_provider"] = kb_provider or "ALL"
    fetch_meta["sheet_only"] = sheet_only

    sample = fair_sample_for_digest(
        items, membership, quotas=quotas, include_macro=include_macro
    )
    llm_items = list(sample["items"])
    filtered = int(sample["after_fair_sample"])
    fetch_meta["item_count_after_fair_sample"] = filtered
    fetch_meta["fair_sample_before_pack"] = sample.get("before_pack")
    fetch_meta["fair_sample_per_ticker"] = sample.get("per_ticker_counts")
    fetch_meta["fair_sample_macro_count"] = sample.get("macro_count")

    earnings_items: List[Dict[str, Any]] = []
    # Sheet-only morning digest: no Yahoo earnings overlay — corpus is the table alone.
    include_earnings = (not sheet_only) and _truthy_cfg("NOTEBOOK_NEWS_INCLUDE_EARNINGS", "1")
    if from_kb and include_earnings:
        try:
            earnings_items = load_kb_earnings_items(requested, days_back=7, days_ahead=45, limit=40)
        except Exception as e:
            logger.exception("KB earnings load for digest failed: %s", e)
            fetch_meta["kb_earnings_error"] = str(e)
    fetch_meta["earnings_count"] = len(earnings_items)

    if use_llm and llm_items:
        try:
            digest_body = _llm_digest(llm_items, membership=membership, earnings=earnings_items)
            if digest_body.get("llm_parse_error") or (
                not any(digest_body.get(k) for k in ("signals", "risks", "macro", "newtickers"))
                and int(digest_body.get("kept") or 0) == 0
                and filtered > 0
            ):
                logger.warning("LLM digest empty despite %s items — retry once with smaller input", filtered)
                digest_body = _llm_digest(
                    llm_items[: max(60, filtered // 2)],
                    membership=membership,
                    earnings=earnings_items[:20],
                )
        except Exception as e:
            logger.exception("LLM digest failed: %s", e)
            digest_body = _empty_digest(filtered=filtered, note=f"LLM ошибка: {e}")
            digest_body["llm_error"] = str(e)
    elif use_llm and not llm_items:
        digest_body = _empty_digest(filtered=0, note="Пустой KB/fetch — LLM не вызывался.")
    else:
        digest_body = _empty_digest(filtered=filtered, note="LLM отключён — сырой список в signals.")
        digest_body["kept"] = filtered
        digest_body["trashed"] = 0
        digest_body["signals"] = [
            {
                "sym": it.get("ticker"),
                "src": it.get("src"),
                "text": it.get("title"),
                "tac": "",
                "link": it.get("link"),
                "date": _format_article_date(it.get("publishOn")),
                "prem": _format_article_date(it.get("publishOn")),
            }
            for it in llm_items
        ]

    # Prefer pipeline truth over LLM's self-reported filtered count.
    digest_body["filtered"] = filtered
    digest = {
        "date": digest_body.get("date")
        or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "filtered": filtered,
        "kept": int(digest_body.get("kept") or 0),
        "trashed": int(digest_body.get("trashed") or 0),
        "signals": digest_body.get("signals") if isinstance(digest_body.get("signals"), list) else [],
        "risks": digest_body.get("risks") if isinstance(digest_body.get("risks"), list) else [],
        "macro": digest_body.get("macro") if isinstance(digest_body.get("macro"), list) else [],
        "newtickers": digest_body.get("newtickers") if isinstance(digest_body.get("newtickers"), list) else [],
        "trashNote": digest_body.get("trashNote") or "",
    }
    out_lim = digest_output_limits()
    for _bucket, _key in (
        ("signals", "signals"),
        ("risks", "risks"),
        ("macro", "macro"),
        ("newtickers", "newtickers"),
    ):
        digest[_bucket] = clamp_digest_rows_brief(
            enrich_digest_rows_with_dates(digest[_bucket], llm_items),
            max_rows=int(out_lim[_key]),
            text_limit=int(out_lim["text_chars"]),
            tac_limit=int(out_lim["tac_chars"]),
        )
    kept_n = sum(len(digest[k]) for k in ("signals", "risks", "macro", "newtickers"))
    digest["kept"] = kept_n
    digest["trashed"] = max(0, filtered - kept_n)
    digest["trashNote"] = _clip_digest_field(digest.get("trashNote"), 160)
    if digest_body.get("_llm"):
        digest["_llm"] = digest_body["_llm"]
    if digest_body.get("llm_error"):
        digest["llm_error"] = digest_body["llm_error"]

    pipe_base = {
        "fetch_sa": fetch_sa,
        "save_kb": save_kb,
        "from_kb": from_kb,
        "kb_source": kb_src or "ALL",
        "kb_provider": kb_provider or "ALL",
        "sheet_only": sheet_only,
        "include_macro": include_macro,
        "include_earnings": bool(earnings_items) if sheet_only else (
            bool(earnings_items) or _truthy_cfg("NOTEBOOK_NEWS_INCLUDE_EARNINGS", "1")
        ),
        "earnings_count": len(earnings_items),
        "lookback_hours": lb,
        "kb_inserted": kb_inserted,
        "deduped_drop": deduped_drop,
        "raw_item_count": raw_count,
        "item_count_after_dedupe": after_dedupe,
        "after_fair_sample": filtered,
        "per_ticker_g1": quotas.get("g1"),
        "per_ticker_g2": quotas.get("g2"),
        "per_ticker_g3": quotas.get("g3"),
        "per_ticker_new": quotas.get("new"),
        "per_ticker_extra": quotas.get("extra"),
        "macro_limit": quotas.get("macro_limit"),
        "llm_max_items": quotas.get("llm_max_items"),
        "fair_sample_macro_count": sample.get("macro_count"),
        "digest_max_tokens": int(get_config_value("NOTEBOOK_NEWS_DIGEST_MAX_TOKENS", "20000") or 20000),
        "digest_bucket_limits": digest_output_limits(),
    }

    result = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe": uni,
        "requested_tickers": requested,
        "pipeline": pipe_base,
        "raw": {
            **fetch_meta,
            "item_count": filtered,
            "items_from": "knowledge_base" if from_kb and not fetch_meta.get("kb_load_error") else "api",
            "earnings_sample": earnings_items[:15],
        },
        "digest": digest,
        "items_sample": llm_items[:20],
    }

    if write:
        dpath = out_digest or DEFAULT_DIGEST_PATH
        rpath = out_raw or DEFAULT_RAW_PATH
        dpath.parent.mkdir(parents=True, exist_ok=True)
        rpath.parent.mkdir(parents=True, exist_ok=True)
        snap = _snapshot_payload(
            digest=digest,
            universe=uni,
            generated_at_utc=result["generated_at_utc"],
            pipeline=result["pipeline"],
            requested_tickers=requested,
        )
        snap["pipeline"] = {
            **snap["pipeline"],
            "requested_ticker_count": len(requested),
        }
        result["pipeline"] = snap["pipeline"]
        dpath.write_text(
            json.dumps(snap, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        rpath.write_text(
            json.dumps(
                {
                    "raw": result["raw"],
                    "items": llm_items,
                    "items_before_fair_sample": items,
                },
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            ),
            encoding="utf-8",
        )
        arch_path = None
        try:
            arch_path = archive_digest_snapshot(snap)
        except Exception as e:
            logger.warning("digest archive failed: %s", e)
        result["wrote"] = {
            "digest": str(dpath),
            "raw": str(rpath),
            "archive": str(arch_path) if arch_path else None,
        }

    return result


def load_latest_digest(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    pack = load_latest_digest_pack(path)
    if not pack:
        return None
    dig = pack.get("digest")
    return dig if isinstance(dig, dict) else None


# Ingest channels (LSE KB). Notebook NEWS UI/digest uses Sheet only by default.
NOTEBOOK_NEWS_INGEST_CHANNELS: List[Dict[str, str]] = [
    {"id": "SA Google Sheet", "via": "sa_sheet_feed → KB", "role": "notebook NEWS (digest + UI)"},
    {"id": "Seeking Alpha Finance", "via": "RapidAPI tipsters → KB", "role": "LSE/UI tipsters (not notebook digest)"},
    {"id": "Yahoo Finance", "via": "TickerNews merge", "role": "LSE ticker headlines"},
    {"id": "Marketaux", "via": "TickerNews merge (if key)", "role": "LSE ticker headlines"},
    {"id": "Investing.com News", "via": "fetch_news_cron investing", "role": "LSE market / ticker"},
    {"id": "Investing.com Economic Calendar", "via": "calendar cron", "role": "macro calendar (notebook OK)"},
    {"id": "NewsAPI", "via": "fetch_news_cron", "role": "wire / general"},
    {"id": "Alpha Vantage", "via": "fetch_news_cron core", "role": "sentiment / headlines"},
    {"id": "RSS", "via": "fetch_news_cron core", "role": "feeds"},
    {"id": "Yahoo Earnings (yfinance)", "via": "earnings calendar", "role": "earnings dates (notebook OK)"},
]


def notebook_news_sources_catalog(*, days: int = 14, limit: int = 80) -> Dict[str, Any]:
    """Ingest channel list + live KB source counts (for Дайджест UI)."""
    live: List[Dict[str, Any]] = []
    err = ""
    try:
        from news_importer import get_news_sources_stats
        from sqlalchemy import create_engine
        from config_loader import get_database_url

        eng = create_engine(get_database_url())
        live = get_news_sources_stats(eng, days=max(1, int(days)))[: max(1, int(limit))]
    except Exception as e:
        err = str(e)[:240]
        logger.debug("notebook news sources stats: %s", e)
    return {
        "ingest_channels": list(NOTEBOOK_NEWS_INGEST_CHANNELS),
        "kb_sources_14d": live,
        "days": int(days),
        "error": err or None,
        "note_ru": (
            "Новости тетрадки (дайджест + лента) — только Google Sheet "
            "(NOTEBOOK_NEWS_KB_PROVIDER=sa_google_sheet). Календарь/earnings — отдельно. "
            "Ниже — каналы ingest cron и source в KB за 14д."
        ),
        "notebook_kb_provider": notebook_kb_news_provider() or "ALL",
        "sheet_only": notebook_news_sheet_only(),
    }


def watchlist_candidates_from_digest(digest: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Map digest «новые тикеры» → Watchlist candidates (SA/KB via morning LLM)."""
    out: List[Dict[str, Any]] = []
    if not isinstance(digest, dict):
        return out
    seen = set()
    for it in digest.get("newtickers") or []:
        if not isinstance(it, dict):
            continue
        sym = str(it.get("sym") or "").strip().upper()
        if not sym or sym in seen:
            continue
        # Skip macro-ish labels
        if "МАКРО" in sym or sym.startswith("MACRO"):
            continue
        seen.add(sym)
        text = str(it.get("text") or "").strip()
        tac = str(it.get("tac") or "").strip()
        # Prefer plain rationale without HTML tags for watchlist card
        tac_plain = re.sub(r"<[^>]+>", "", tac).strip()
        out.append(
            {
                "sym": sym,
                "src": str(it.get("src") or "Seeking Alpha / KB")[:80],
                "text": text[:400],
                "rationale": (tac_plain or text)[:400],
                "link": str(it.get("link") or "")[:300],
                "date": str(it.get("date") or it.get("prem") or "")[:40],
                "via": "digest_newtickers",
            }
        )
    return out