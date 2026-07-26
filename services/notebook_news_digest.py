"""Notebook news digest: portfolio ∪ GAME_5M → SA Finance → LLM (ProxyAPI).

Temporary «group 3» = union of:
  1) portfolio (TRADING_CYCLE / MEDIUM+LONG)
  2) GAME_5M
Overlaps allowed; membership tags kept. Nastya may redefine groups later.

Digest shape matches nastya/notebook digest (signals / risks / macro / newtickers).
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
from services.seeking_alpha_finance import fetch_news_for_tickers
from services.ticker_groups import get_tickers_for_portfolio_game, get_tickers_game_5m

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = _REPO_ROOT / "local" / "notebook"
DEFAULT_DIGEST_PATH = DEFAULT_OUT_DIR / "digest_latest.json"
DEFAULT_RAW_PATH = DEFAULT_OUT_DIR / "news_raw_latest.json"

DIGEST_SYSTEM = """Ты — редактор утреннего дайджеста для торговой «Рабочей тетрадки».
Вход: список новостей Seeking Alpha по тикерам портфеля и игры 5m.
Задача: отсеять шум (кликбейт, повторы без конкретики) и разложить остаток по корзинам ТЗ.

Верни ТОЛЬКО JSON-объект без markdown:
{
  "filtered": <int всего входных>,
  "kept": <int оставленных в дайджесте>,
  "trashed": <int отсеянных>,
  "signals": [{"sym":"MSFT","src":"...","text":"...","tac":"<b>Тактика:</b> ...","link":"...","prem":""}],
  "risks": [тот же формат],
  "macro": [{"sym":"МАКРО · …","src":"...","text":"...","tac":"<b>Влияние:</b> ...","link":"..."}],
  "newtickers": [{"sym":"XYZ","src":"...","text":"...","tac":"<b>Обоснование:</b> ...","link":"..."}],
  "trashNote": "кратко что отсеяно"
}

Правила:
- signals: позитивные/нейтрально-полезные катализаторы по нашим тикерам.
- risks: угрозы, даунсайд, негатив по нашим тикерам.
- macro: ФРС, сектор AI, широкие тренды без одного тикера как якоря.
- newtickers: имена НЕ из watchlist входа — только если явно «кандидат к рассмотрению».
- text: 1–2 предложения по-русски; tac — короткая тактика для тетрадки.
- Не выдумывай новости: только из входа. Если данных мало — пустые массивы ок.
- Отсев 50–70% шума — норма.
"""


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


def build_news_universe(*, equity_only: bool = True) -> Dict[str, Any]:
    """Group1=portfolio, Group2=game_5m, Group3=union (temp until Nastya)."""
    portfolio = _unique_upper(get_tickers_for_portfolio_game())
    game5m = _unique_upper(get_tickers_game_5m())
    if equity_only:
        portfolio = [t for t in portfolio if is_equity_symbol(t)]
        game5m = [t for t in game5m if is_equity_symbol(t)]
    pset, gset = set(portfolio), set(game5m)
    union = _unique_upper(portfolio + game5m)
    membership: Dict[str, List[str]] = {}
    for t in union:
        tags: List[str] = []
        if t in pset:
            tags.append("portfolio")  # group 1
        if t in gset:
            tags.append("game_5m")  # group 2
        membership[t] = tags
    return {
        "group1_portfolio": portfolio,
        "group2_game_5m": game5m,
        "group3_union": union,
        "membership": membership,
        "note_ru": (
            "Временно: группа 3 = объединение portfolio ∪ GAME_5M (пересечения допустимы). "
            "Финальные группы тетрадки — уточнить у Насти."
        ),
    }


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


def _llm_digest(items: List[Dict[str, Any]], *, membership: Dict[str, List[str]]) -> Dict[str, Any]:
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
    user = (
        "Новости для дайджеста (JSON):\n"
        + json.dumps(slim, ensure_ascii=False)[:28000]
        + "\n\nСобери дайджест по схеме."
    )
    llm = LLMService()
    # Optional override model via NOTEBOOK_NEWS_DIGEST_MODEL is handled by env if set on LLMService —
    # we pass temperature low for structured output.
    out = llm.generate_response(
        messages=[{"role": "user", "content": user}],
        system_prompt=DIGEST_SYSTEM,
        temperature=float(get_config_value("NOTEBOOK_NEWS_DIGEST_TEMPERATURE", "0.2") or 0.2),
        max_tokens=int(get_config_value("NOTEBOOK_NEWS_DIGEST_MAX_TOKENS", "3500") or 3500),
    )
    text = (out or {}).get("response") or ""
    parsed = _parse_llm_json(text)
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
    out_digest: Optional[Path] = None,
    out_raw: Optional[Path] = None,
) -> Dict[str, Any]:
    """Fetch SA news for universe and optionally LLM-build notebook digest."""
    uni = build_news_universe(equity_only=True)
    wanted = _unique_upper(tickers) if tickers else list(uni["group3_union"])
    # keep membership for requested set
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

    raw_bundle = fetch_news_for_tickers(
        wanted,
        per_ticker=per,
        sleep_sec=sl,
        max_tickers=mx,
    )
    items = raw_bundle.get("items") or []
    filtered = len(items)

    if use_llm and items:
        try:
            digest_body = _llm_digest(items, membership=membership)
        except Exception as e:
            logger.exception("LLM digest failed: %s", e)
            digest_body = _empty_digest(filtered=filtered, note=f"LLM ошибка: {e}")
            digest_body["llm_error"] = str(e)
    elif use_llm and not items:
        digest_body = _empty_digest(filtered=0, note="Пустой fetch — LLM не вызывался.")
    else:
        # No LLM: naive keep all as signals (debug)
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

    # Normalize counts if model omitted
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
        "requested_tickers": wanted[: mx or len(wanted)],
        "raw": {
            "fetched_at": raw_bundle.get("fetched_at"),
            "item_count": filtered,
            "errors": raw_bundle.get("errors") or {},
            "by_ticker_counts": {k: len(v or []) for k, v in (raw_bundle.get("by_ticker") or {}).items()},
        },
        "digest": digest,
        "items_sample": items[:20],
    }

    if write:
        dpath = out_digest or DEFAULT_DIGEST_PATH
        rpath = out_raw or DEFAULT_RAW_PATH
        dpath.parent.mkdir(parents=True, exist_ok=True)
        rpath.parent.mkdir(parents=True, exist_ok=True)
        dpath.write_text(json.dumps({"digest": digest, "universe": uni, "generated_at_utc": result["generated_at_utc"]}, ensure_ascii=False, indent=2), encoding="utf-8")
        # raw can be large — store compact items + errors
        rpath.write_text(
            json.dumps(
                {
                    "fetched_at": raw_bundle.get("fetched_at"),
                    "items": items,
                    "errors": raw_bundle.get("errors"),
                    "by_ticker_counts": result["raw"]["by_ticker_counts"],
                },
                ensure_ascii=False,
                indent=2,
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
