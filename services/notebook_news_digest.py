"""Notebook news digest: notebook groups → SA Finance → knowledge_base → LLM.

Universe primary source: tickers in nastya/notebook/notebook_data.json (by group).
Fallback: portfolio ∪ GAME_5M if notebook has no equities (until Nastya fills lists).
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
)
from services.ticker_groups import get_tickers_for_portfolio_game, get_tickers_game_5m

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = _REPO_ROOT / "local" / "notebook"
DEFAULT_DIGEST_PATH = DEFAULT_OUT_DIR / "digest_latest.json"
DEFAULT_RAW_PATH = DEFAULT_OUT_DIR / "news_raw_latest.json"


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

DIGEST_SYSTEM = """Ты — редактор утреннего дайджеста для торговой «Рабочей тетрадки».
Вход: (1) новости из knowledge_base LSE (Seeking Alpha, Yahoo, Investing, Reuters и др.);
(2) календарь/факты EARNINGS из Yahoo/yfinance по тем же тикерам (если переданы).
Повторы между источниками уже частично сняты кодом; оставшиеся дубли одной истории тоже схлопывай.
Задача: отсеять шум (кликбейт, повторы без конкретики) и разложить остаток по корзинам ТЗ.

Верни ТОЛЬКО JSON-объект без markdown:
{
  "filtered": <int всего входных новостей>,
  "kept": <int оставленных в дайджесте>,
  "trashed": <int отсеянных>,
  "signals": [{"sym":"MSFT","src":"...","text":"...","tac":"<b>Тактика:</b> ...","link":"...","prem":""}],
  "risks": [тот же формат],
  "macro": [{"sym":"МАКРО · …","src":"...","text":"...","tac":"<b>Влияние:</b> ...","link":"..."}],
  "newtickers": [{"sym":"XYZ","src":"...","text":"...","tac":"<b>Обоснование:</b> ...","link":"..."}],
  "trashNote": "кратко что отсеяно"
}

Правила:
- signals: позитивные/нейтрально-полезные катализаторы по нашим тикерам; tac в терминах тетрадки (Buy Dip / Hold / пауза / ждать уровень).
- risks: угрозы, даунсайд, отчёт в ближайшие дни; tac часто Hold / не докупать / стоп-наблюдение / пауза до earnings.
- macro: ФРС, сектор AI, геополитика, широкие тренды; tac = влияние на Environment Check.
- newtickers: имена НЕ из текущего списка тетрадки (не ALAB/AMD/… уже в groups) — кандидат «к рассмотрению».
- Одна история из нескольких источников → ОДИН пункт; в src — основной источник.
- Если в блоке earnings указан ближайший отчёт — обязательно учти в risks или signals (пауза/не наращивать до цифр; peer spillover: память MU↔SNDK, оптика LITE↔CIEN, hyperscalers).
- text: 1–2 предложения по-русски.
- Не выдумывай новости: только из входа. Если данных мало — пустые массивы ок.
- Отсев 50–70% шума — норма.
"""

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
    """Group key → tickers from nastya/notebook/notebook_data.json."""
    try:
        from services.trading_notebook import load_notebook_data

        data = load_notebook_data()
    except Exception as e:
        logger.debug("notebook_data load for universe: %s", e)
        return {"1": [], "2": [], "3": [], "n": []}
    by_g: Dict[str, List[str]] = {"1": [], "2": [], "3": [], "n": []}
    tickers = data.get("tickers") if isinstance(data.get("tickers"), dict) else {}
    for sym, row in tickers.items():
        if not isinstance(row, dict):
            continue
        u = str(sym or row.get("sym") or "").strip().upper()
        if not u or not is_equity_symbol(u):
            continue
        g = str(row.get("group") or "").strip().lower()
        if g not in by_g:
            g = "3"
        if u not in by_g[g]:
            by_g[g].append(u)
    return by_g


def build_news_universe(*, equity_only: bool = True) -> Dict[str, Any]:
    """Notebook JSON groups first; optional config fallback for empty notebook."""
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
            "Fallback: notebook_data без тикеров → portfolio ∪ GAME_5M. "
            "Заполните группы в nastya/notebook/notebook_data.json (ответы Насти)."
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
        note = "NOTEBOOK_NEWS_UNIVERSE=config: portfolio ∪ GAME_5M."
        source = "config"
    else:
        union = notebook_union
        note = (
            "Universe из notebook_data.json (группы 1/2/3/n). "
            "Списки уточняются у Насти — см. nastya/NOTEBOOK_QUESTIONS_FOR_NASTYA.md."
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


def format_digest_telegram(digest: Optional[Dict[str, Any]] = None, *, max_items: int = 4) -> str:
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
            text = str(row.get("text") or "").strip()
            tac = re.sub(r"<[^>]+>", "", str(row.get("tac") or "")).strip()
            lines.append(f"  • {sym}: {text[:180]}")
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
    user = (
        "Тикеры уже в тетрадке (НЕ класть в newtickers): "
        + ", ".join(known)
        + "\n\nНовости для дайджеста (JSON):\n"
        + json.dumps(slim, ensure_ascii=False)[:24000]
        + "\n\nEARNINGS календарь Yahoo/yfinance (JSON):\n"
        + json.dumps(earn_slim, ensure_ascii=False)[:4000]
        + "\n\nСобери дайджест по схеме."
    )
    llm = LLMService()
    out = llm.generate_response(
        messages=[{"role": "user", "content": user}],
        system_prompt=DIGEST_SYSTEM,
        temperature=float(get_config_value("NOTEBOOK_NEWS_DIGEST_TEMPERATURE", "0.2") or 0.2),
        max_tokens=int(get_config_value("NOTEBOOK_NEWS_DIGEST_MAX_TOKENS", "6000") or 6000),
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

    per = int(per_ticker if per_ticker is not None else (get_config_value("NOTEBOOK_NEWS_PER_TICKER", "5") or 5))
    mx = max_tickers
    if mx is None:
        raw_mx = (get_config_value("NOTEBOOK_NEWS_MAX_TICKERS", "") or "").strip()
        mx = int(raw_mx) if raw_mx.isdigit() else None
    sl = float(sleep_sec if sleep_sec is not None else (get_config_value("NOTEBOOK_NEWS_SLEEP_SEC", "0.35") or 0.35))
    lb = int(
        lookback_hours
        if lookback_hours is not None
        else (get_config_value("NOTEBOOK_NEWS_KB_LOOKBACK_HOURS", "72") or 72)
    )
    # Default: all NEWS sources in KB (Yahoo/Marketaux/Investing/SA/…).
    # NOTEBOOK_NEWS_KB_ALL_SOURCES=0 → only NOTEBOOK_NEWS_KB_SOURCE (default SA).
    if _truthy_cfg("NOTEBOOK_NEWS_KB_ALL_SOURCES", "1"):
        kb_src: Optional[str] = None
    else:
        kb_src = (get_config_value("NOTEBOOK_NEWS_KB_SOURCE", KB_SOURCE) or KB_SOURCE).strip() or KB_SOURCE

    requested = wanted[: mx or len(wanted)]
    kb_tickers = list(requested)
    include_macro = _truthy_cfg("NOTEBOOK_NEWS_INCLUDE_MACRO", "1")
    if include_macro and "MACRO" not in kb_tickers:
        kb_tickers.append("MACRO")

    fetch_meta: Dict[str, Any] = {"skipped": not fetch_sa}
    kb_inserted = 0
    api_items: List[Dict[str, Any]] = []

    if fetch_sa:
        if save_kb:
            bundle = fetch_and_save_sa_news(
                requested,
                per_ticker=per,
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

            bundle = fetch_news_for_tickers(requested, per_ticker=per, sleep_sec=sl)
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
            # Wider cap when multi-source: more rows before dedupe.
            lim = max(40, per * max(1, len(kb_tickers)) * (3 if kb_src is None else 1))
            lim = min(lim, 400)
            items = load_kb_news_items(
                kb_tickers,
                lookback_hours=lb,
                source=kb_src,
                limit=lim,
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
    filtered = len(items)
    fetch_meta["raw_item_count"] = raw_count
    fetch_meta["deduped_drop"] = deduped_drop
    fetch_meta["item_count_after_dedupe"] = filtered

    earnings_items: List[Dict[str, Any]] = []
    if from_kb and _truthy_cfg("NOTEBOOK_NEWS_INCLUDE_EARNINGS", "1"):
        try:
            earnings_items = load_kb_earnings_items(requested, days_back=7, days_ahead=45, limit=40)
        except Exception as e:
            logger.exception("KB earnings load for digest failed: %s", e)
            fetch_meta["kb_earnings_error"] = str(e)
    fetch_meta["earnings_count"] = len(earnings_items)

    if use_llm and items:
        try:
            # Cap input size so completion is not truncated (earnings block also consumes tokens).
            llm_items = items[:120]
            digest_body = _llm_digest(llm_items, membership=membership, earnings=earnings_items)
            if digest_body.get("llm_parse_error") or (
                not any(digest_body.get(k) for k in ("signals", "risks", "macro", "newtickers"))
                and int(digest_body.get("kept") or 0) == 0
                and filtered > 0
            ):
                logger.warning("LLM digest empty despite %s items — retry once with smaller input", filtered)
                digest_body = _llm_digest(
                    llm_items[:60],
                    membership=membership,
                    earnings=earnings_items[:20],
                )
        except Exception as e:
            logger.exception("LLM digest failed: %s", e)
            digest_body = _empty_digest(filtered=filtered, note=f"LLM ошибка: {e}")
            digest_body["llm_error"] = str(e)
    elif use_llm and not items:
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
                "prem": it.get("publishOn"),
            }
            for it in items
        ]

    if "filtered" not in digest_body:
        digest_body["filtered"] = filtered
    digest = {
        "date": digest_body.get("date")
        or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "filtered": int(digest_body.get("filtered") or filtered),
        "kept": int(digest_body.get("kept") or 0),
        "trashed": int(digest_body.get("trashed") or 0),
        "signals": digest_body.get("signals") if isinstance(digest_body.get("signals"), list) else [],
        "risks": digest_body.get("risks") if isinstance(digest_body.get("risks"), list) else [],
        "macro": digest_body.get("macro") if isinstance(digest_body.get("macro"), list) else [],
        "newtickers": digest_body.get("newtickers") if isinstance(digest_body.get("newtickers"), list) else [],
        "trashNote": digest_body.get("trashNote") or "",
    }
    if digest_body.get("_llm"):
        digest["_llm"] = digest_body["_llm"]
    if digest_body.get("llm_error"):
        digest["llm_error"] = digest_body["llm_error"]

    result = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe": uni,
        "requested_tickers": requested,
        "pipeline": {
            "fetch_sa": fetch_sa,
            "save_kb": save_kb,
            "from_kb": from_kb,
            "kb_source": kb_src or "ALL",
            "include_macro": include_macro,
            "include_earnings": bool(earnings_items) or _truthy_cfg("NOTEBOOK_NEWS_INCLUDE_EARNINGS", "1"),
            "earnings_count": len(earnings_items),
            "lookback_hours": lb,
            "kb_inserted": kb_inserted,
            "deduped_drop": deduped_drop,
            "raw_item_count": raw_count,
        },
        "raw": {
            **fetch_meta,
            "item_count": filtered,
            "items_from": "knowledge_base" if from_kb and not fetch_meta.get("kb_load_error") else "api",
            "earnings_sample": earnings_items[:15],
        },
        "digest": digest,
        "items_sample": items[:20],
    }

    if write:
        dpath = out_digest or DEFAULT_DIGEST_PATH
        rpath = out_raw or DEFAULT_RAW_PATH
        dpath.parent.mkdir(parents=True, exist_ok=True)
        rpath.parent.mkdir(parents=True, exist_ok=True)
        dpath.write_text(
            json.dumps(
                {
                    "digest": digest,
                    "universe": uni,
                    "generated_at_utc": result["generated_at_utc"],
                    "pipeline": result["pipeline"],
                },
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            ),
            encoding="utf-8",
        )
        rpath.write_text(
            json.dumps(
                {
                    "raw": result["raw"],
                    "items": items,
                },
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            ),
            encoding="utf-8",
        )
        result["wrote"] = {"digest": str(dpath), "raw": str(rpath)}

    return result


def load_latest_digest(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    p = path or DEFAULT_DIGEST_PATH
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("load digest failed: %s", e)
        return None
    if isinstance(data, dict) and isinstance(data.get("digest"), dict):
        return data["digest"]
    if isinstance(data, dict) and "signals" in data:
        return data
    return None
