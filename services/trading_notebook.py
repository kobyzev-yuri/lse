"""Рабочая тетрадка Насти: ручные уровни + справочный Close из quotes/yfinance."""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_PATH = _REPO_ROOT / "nastya" / "notebook" / "notebook_data.json"
DEFAULT_OVERRIDE_PATH = _REPO_ROOT / "local" / "notebook" / "ticker_overrides.json"


def notebook_data_path() -> Path:
    return DEFAULT_DATA_PATH


def notebook_override_path() -> Path:
    return DEFAULT_OVERRIDE_PATH


def load_notebook_overrides(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or notebook_override_path()
    if not p.is_file():
        return {"schema_version": 1, "tickers": {}}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("notebook overrides read failed: %s", e)
        return {"schema_version": 1, "tickers": {}}
    if not isinstance(raw, dict):
        return {"schema_version": 1, "tickers": {}}
    tickers = raw.get("tickers") if isinstance(raw.get("tickers"), dict) else {}
    return {"schema_version": int(raw.get("schema_version") or 1), "tickers": tickers}


def save_notebook_overrides(data: Dict[str, Any], path: Optional[Path] = None) -> Path:
    p = path or notebook_override_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": int(data.get("schema_version") or 1),
        "tickers": data.get("tickers") if isinstance(data.get("tickers"), dict) else {},
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    return p


_ENV_STATES = frozenset({"ok", "mid", "bad"})


def _env_label_key(lbl: str) -> Optional[str]:
    """Normalize env row label to a stable override key; live metrics are never overridable."""
    low = str(lbl or "").lower()
    if "vix" in low:
        return None
    if "ndx" in low or "nasdaq" in low:
        return None
    if "нефт" in low or "oil" in low or "cl=f" in low or "геопол" in low:
        return None
    if "фрс" in low or "fed" in low:
        return "fed"
    if "таргет" in low or low.strip() == "pt" or "price target" in low:
        return "pt"
    return None


def _find_base_ticker(sym: str) -> tuple[str, Dict[str, Any]]:
    u = str(sym or "").strip().upper()
    if not u:
        raise ValueError("empty ticker")
    base = load_notebook_data()
    base_tickers = base.get("tickers") if isinstance(base.get("tickers"), dict) else {}
    for k, v in base_tickers.items():
        if str(k).upper() == u and isinstance(v, dict):
            return u, v
    # Overlay-only (added via UI)
    ov = load_notebook_overrides()
    ov_tickers = ov.get("tickers") if isinstance(ov.get("tickers"), dict) else {}
    for k, v in ov_tickers.items():
        if str(k).upper() != u or not isinstance(v, dict):
            continue
        if v.get("is_new") or v.get("group") is not None or v.get("sym"):
            stub = blank_ticker_card(
                u,
                str(v.get("group") or "n"),
                name=str(v.get("name") or ""),
            )
            merged = dict(stub)
            merged.update({kk: vv for kk, vv in v.items() if kk not in ("updated_at_utc", "updated_by")})
            return u, merged
    raise KeyError(f"ticker {u} not in notebook_data")


def _load_override_ticker_row(
    u: str, path: Optional[Path] = None
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    ov = load_notebook_overrides(path)
    tickers = {
        str(k).upper(): dict(v)
        for k, v in (ov.get("tickers") or {}).items()
        if isinstance(v, dict)
    }
    row = dict(tickers.get(u) or {})
    return ov, tickers, row


def _merge_env_rows(
    base_env: List[Any],
    patch_env: Any,
) -> List[Dict[str, Any]]:
    """Apply overlay env patches by label key (fed/pt). Skip VIX."""
    patches: Dict[str, Dict[str, Any]] = {}
    if isinstance(patch_env, list):
        for item in patch_env:
            if not isinstance(item, dict):
                continue
            key = _env_label_key(str(item.get("lbl") or item.get("key") or ""))
            if not key:
                continue
            patches[key] = item
    elif isinstance(patch_env, dict):
        for k, item in patch_env.items():
            if not isinstance(item, dict):
                continue
            key = _env_label_key(str(k)) or (
                str(k).lower() if str(k).lower() in ("fed", "pt") else None
            )
            if key in ("fed", "pt"):
                patches[key] = item

    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for e in base_env or []:
        if not isinstance(e, dict):
            continue
        e2 = dict(e)
        key = _env_label_key(str(e2.get("lbl") or ""))
        if key and key in patches:
            p = patches[key]
            state = str(p.get("state") or e2.get("state") or "ok").lower()
            if state not in _ENV_STATES:
                state = "ok"
            e2["state"] = state
            if p.get("st") is not None:
                e2["st"] = str(p.get("st"))[:200]
            elif not e2.get("st"):
                e2["st"] = "ручной override"
            e2["live"] = False
            e2["source"] = "manual · overlay"
            e2["manual"] = True
            seen.add(key)
        out.append(e2)

    # Ensure Fed/PT rows exist if only provided via overlay
    defaults = {
        "fed": "Риторика ФРС",
        "pt": "Понижения таргетов (вне earnings)",
    }
    for key, lbl in defaults.items():
        if key in seen or key not in patches:
            continue
        p = patches[key]
        state = str(p.get("state") or "ok").lower()
        if state not in _ENV_STATES:
            state = "ok"
        out.append(
            {
                "lbl": lbl,
                "st": str(p.get("st") or "ручной override")[:200],
                "state": state,
                "live": False,
                "source": "manual · overlay",
                "manual": True,
            }
        )
    return out


def apply_ticker_overrides(
    tickers: Dict[str, Any],
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge local/notebook/ticker_overrides.json onto ticker cards (signals, levels, env, group)."""
    ov_root = overrides if overrides is not None else load_notebook_overrides()
    ov_tickers_raw = ov_root.get("tickers") if isinstance(ov_root.get("tickers"), dict) else {}
    ov_tickers = {
        str(k).upper(): v for k, v in ov_tickers_raw.items() if isinstance(v, dict)
    }
    out: Dict[str, Any] = {}
    for sym, row in (tickers or {}).items():
        if not isinstance(row, dict):
            continue
        u = str(sym).upper()
        d = dict(row)
        patch = ov_tickers.get(u) or {}
        if patch:
            d = _apply_one_ticker_patch(d, patch)
        # Keep buy/sell trigger labels in sync with effective levels.
        if isinstance(d.get("levels"), dict) and isinstance(d.get("triggers"), list):
            d["triggers"] = _merge_triggers(d["triggers"], [], levels=d.get("levels"))
        out[u] = d

    # Overlay-only tickers (added via UI, not in notebook_data.json yet).
    for u, patch in ov_tickers.items():
        if u in out or not isinstance(patch, dict):
            continue
        if not (patch.get("is_new") or patch.get("group") or patch.get("sym")):
            continue
        stub = blank_ticker_card(
            u,
            str(patch.get("group") or "n"),
            name=str(patch.get("name") or ""),
        )
        d = _apply_one_ticker_patch(stub, patch)
        d["is_new"] = True
        if isinstance(d.get("levels"), dict) and isinstance(d.get("triggers"), list):
            d["triggers"] = _merge_triggers(d["triggers"], [], levels=d.get("levels"))
        out[u] = d
    return out


def _apply_one_ticker_patch(d: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a single override patch onto a ticker card dict."""
    if not patch:
        return d
    if patch.get("group") is not None:
        g = str(patch.get("group") or "").strip()
        if g in VALID_NOTEBOOK_GROUPS:
            d["group"] = g
            d["group_override"] = True
    if patch.get("name") is not None and str(patch.get("name") or "").strip():
        d["name"] = str(patch.get("name") or "").strip()[:120]
    if patch.get("is_new"):
        d["is_new"] = True
    if isinstance(patch.get("signals"), dict):
        base_sig = dict(d.get("signals") or {}) if isinstance(d.get("signals"), dict) else {}
        for k, v in patch["signals"].items():
            base_sig[k] = v
        d["signals"] = base_sig
        d["signals_override"] = True
    if isinstance(patch.get("levels"), dict):
        base_lv = dict(d.get("levels") or {}) if isinstance(d.get("levels"), dict) else {}
        for k, v in patch["levels"].items():
            base_lv[k] = v  # None clears over base
        d["levels"] = base_lv
        d["levels_override"] = True
    if isinstance(patch.get("consensus"), dict):
        base_c = dict(d.get("consensus") or {}) if isinstance(d.get("consensus"), dict) else {}
        for k, v in patch["consensus"].items():
            base_c[k] = v
        d["consensus"] = base_c
        d["consensus_override"] = True
    if isinstance(patch.get("houses"), list):
        d["houses"] = [dict(h) for h in patch["houses"] if isinstance(h, dict)]
        d["houses_override"] = True
        if patch.get("houses_source"):
            d["houses_source"] = str(patch.get("houses_source"))[:40]
    if patch.get("houseNote") is not None:
        d["houseNote"] = str(patch.get("houseNote") or "")[:800]
    if isinstance(patch.get("houses_counts"), dict):
        hc = {
            "buy": int(patch["houses_counts"].get("buy") or 0),
            "hold": int(patch["houses_counts"].get("hold") or 0),
            "sell": int(patch["houses_counts"].get("sell") or 0),
            "total": int(patch["houses_counts"].get("total") or 0),
        }
        try:
            pt_total = int(patch["houses_counts"].get("pt_total") or 0)
        except (TypeError, ValueError):
            pt_total = 0
        if pt_total > 0:
            hc["pt_total"] = pt_total
        d["houses_counts"] = hc
        d["houses_override"] = True
    if patch.get("houses_source") and not d.get("houses_source"):
        d["houses_source"] = str(patch.get("houses_source"))[:40]
    if patch.get("horizon") is not None:
        d["horizon"] = str(patch.get("horizon") or "")[:160]
        d["profile_override"] = True
    if isinstance(patch.get("profile"), dict):
        base_pf = dict(d.get("profile") or {}) if isinstance(d.get("profile"), dict) else {}
        for k, v in patch["profile"].items():
            base_pf[k] = v
        d["profile"] = base_pf
        d["profile_override"] = True
    if isinstance(patch.get("entry"), list):
        d["entry"] = [str(x)[:500] for x in patch["entry"] if str(x or "").strip()][:20]
        d["plan_override"] = True
    if isinstance(patch.get("exit"), list):
        d["exit"] = [str(x)[:500] for x in patch["exit"] if str(x or "").strip()][:20]
        d["plan_override"] = True
    if patch.get("macro") is not None:
        d["macro"] = str(patch.get("macro") or "")[:800]
        d["plan_override"] = True
    if isinstance(patch.get("fundament"), dict):
        d["fundament"] = _normalize_fundament(patch.get("fundament"))
        d["fundament_override"] = True
    if isinstance(patch.get("report_expect"), dict):
        d["report_expect"] = _normalize_report_expect(patch.get("report_expect"))
        d["report_expect_override"] = True
    if isinstance(patch.get("triggers"), list):
        d["triggers"] = _merge_triggers(
            list(d.get("triggers") or []) if isinstance(d.get("triggers"), list) else [],
            patch.get("triggers"),
            levels=d.get("levels") if isinstance(d.get("levels"), dict) else None,
        )
        d["triggers_override"] = True
    if patch.get("env") is not None:
        base_env = list(d.get("env") or []) if isinstance(d.get("env"), list) else []
        d["env"] = _merge_env_rows(base_env, patch.get("env"))
        d["env_override"] = True
    if patch.get("updated_at_utc"):
        d["override_updated_at_utc"] = patch.get("updated_at_utc")
    if patch.get("updated_by"):
        d["override_updated_by"] = patch.get("updated_by")
    return d


VALID_NOTEBOOK_GROUPS = frozenset({"1", "2", "3", "n"})


def normalize_notebook_group(group: Any) -> str:
    g = str(group or "").strip().lower()
    if g in ("1", "g1", "group1", "grp1"):
        return "1"
    if g in ("2", "g2", "group2", "grp2"):
        return "2"
    if g in ("3", "g3", "group3", "grp3"):
        return "3"
    if g in ("n", "new", "новые", "novye", "gn"):
        return "n"
    raise ValueError("group must be 1, 2, 3, or n")


def normalize_notebook_symbol(sym: Any) -> str:
    u = str(sym or "").strip().upper()
    # Allow ^VIX / CL=F style later, but notebook equities are A-Z0-9.-
    if not u or len(u) > 12:
        raise ValueError("invalid ticker symbol")
    if not all(ch.isalnum() or ch in ".-^=" for ch in u):
        raise ValueError("invalid ticker symbol")
    return u


def blank_ticker_card(sym: str, group: str, *, name: str = "") -> Dict[str, Any]:
    """Minimal notebook card for a newly added ticker."""
    u = normalize_notebook_symbol(sym)
    g = normalize_notebook_group(group)
    role = {
        "1": "G1 удержание",
        "2": "G2 активное",
        "3": "G3 кандидат",
        "n": "Новые · первичный анализ",
    }.get(g, "тетрадка")
    return {
        "sym": u,
        "name": (name or u).strip()[:120],
        "group": g,
        "tags": ["manual"],
        "horizon": "",
        "profile": {
            "Сектор / слой": "уточнять",
            "Роль в тетрадке": role,
            "Отчёт (след.)": "уточнять",
            "Целевая прибыль": "уровни TBD",
        },
        "triggers": [
            {
                "t": "buy",
                "lvl": "Buy Dip · TBD",
                "manual": True,
                "desc": "Уровень вписать вручную",
                "cond": "после согласования",
            },
            {
                "t": "sell",
                "lvl": "Sell · TBD",
                "manual": True,
                "desc": "Уровень фиксации TBD",
                "cond": "после согласования",
            },
            {
                "t": "watch",
                "lvl": "Наблюдение",
                "manual": False,
                "desc": "Макро / катализаторы",
                "cond": "следить",
            },
        ],
        "entry": ["Техника первой — уровни вписываем вручную.", "Макро поверх техники."],
        "exit": ["Sell-уровень задаём заранее после согласования."],
        "env": [
            {"lbl": "VIX", "st": "авто из quotes", "state": "mid"},
            {"lbl": "Риторика ФРС", "st": "обновлять вручную", "state": "mid"},
            {
                "lbl": "Понижения таргетов (вне earnings)",
                "st": "следить",
                "state": "mid",
            },
        ],
        "consensus": {
            "rating": "—",
            "pt": "—",
            "low": "—",
            "high": "—",
            "n": "—",
            "upd": "вручную",
        },
        "houses": [],
        "houseNote": "Заполнить вручную или обновить с StockAnalysis.",
        "macro": "Макро накладываем поверх техники.",
        "levels": {"buyDip": None, "sell": None, "note": "уровни не вписаны — ждать согласования"},
        "signals": {"macroAlive": True, "sentimentBroken": False},
    }


def _ticker_exists(sym: str, path: Optional[Path] = None) -> bool:
    u = normalize_notebook_symbol(sym)
    try:
        _find_base_ticker(u)
        return True
    except KeyError:
        pass
    ov = load_notebook_overrides(path)
    tickers = ov.get("tickers") if isinstance(ov.get("tickers"), dict) else {}
    return any(str(k).upper() == u for k in tickers)


def add_notebook_ticker(
    sym: str,
    group: str,
    *,
    name: str = "",
    updated_by: str = "notebook-ui",
    path: Optional[Path] = None,
    bootstrap: bool = True,
) -> Dict[str, Any]:
    """Add a new ticker stub into the current group (overlay). Fails if already present.

    When bootstrap=True (default), also pull daily quotes + news into Postgres.
    """
    u = normalize_notebook_symbol(sym)
    g = normalize_notebook_group(group)
    if _ticker_exists(u, path):
        raise ValueError(f"{u} уже в тетрадке — используйте перенос группы")

    stub = blank_ticker_card(u, g, name=name)
    ov, tickers, row = _load_override_ticker_row(u, path)
    now = datetime.now(timezone.utc).isoformat()
    row.update(stub)
    row["is_new"] = True
    row["updated_at_utc"] = now
    row["updated_by"] = (updated_by or "notebook-ui")[:80]
    tickers[u] = row
    ov["tickers"] = tickers
    save_notebook_overrides(ov, path)
    out: Dict[str, Any] = {
        "ticker": u,
        "group": g,
        "created": True,
        "card": stub,
        "updated_at_utc": now,
        "updated_by": row["updated_by"],
    }
    if bootstrap:
        try:
            from config_loader import get_config_value

            raw = (get_config_value("NOTEBOOK_ADD_BOOTSTRAP", "1") or "1").strip().lower()
            do_boot = raw not in ("0", "false", "no", "off")
        except Exception:
            do_boot = True
        if do_boot:
            out["bootstrap"] = bootstrap_notebook_ticker_resources(u)
        else:
            out["bootstrap"] = {"skipped": True, "reason": "NOTEBOOK_ADD_BOOTSTRAP=0"}
    return out


def bootstrap_notebook_ticker_resources(
    sym: str,
    *,
    quotes_days: Optional[int] = None,
    sa_per_ticker: Optional[int] = None,
) -> Dict[str, Any]:
    """Pull daily OHLC into quotes + news into knowledge_base for a newly added ticker."""
    u = normalize_notebook_symbol(sym)
    try:
        from config_loader import get_config_value

        days = int(
            quotes_days
            if quotes_days is not None
            else (get_config_value("NOTEBOOK_ADD_QUOTES_DAYS", "90") or 90)
        )
        sa_n = int(
            sa_per_ticker
            if sa_per_ticker is not None
            else (get_config_value("NOTEBOOK_ADD_SA_PER_TICKER", "10") or 10)
        )
    except Exception:
        days, sa_n = 90, 10
    days = max(10, min(days, 400))
    sa_n = max(1, min(sa_n, 40))

    result: Dict[str, Any] = {
        "ticker": u,
        "quotes": {"ok": False},
        "news_sa": {"ok": False},
        "news_yahoo": {"ok": False},
    }

    # 1) Daily candles → public.quotes
    try:
        from sqlalchemy import create_engine

        from config_loader import get_database_url
        from update_prices import update_ticker_prices

        engine = create_engine(get_database_url())
        try:
            n = int(
                update_ticker_prices(
                    engine, u, days_back=days, force_days_back=days
                )
                or 0
            )
            result["quotes"] = {"ok": True, "rows_upserted": n, "days": days}
        finally:
            engine.dispose()
    except Exception as e:
        logger.warning("notebook bootstrap quotes %s: %s", u, e)
        result["quotes"] = {"ok": False, "error": str(e)[:300], "days": days}

    # 2) Seeking Alpha Finance → knowledge_base
    try:
        from services.seeking_alpha_finance import fetch_and_save_sa_news, rapidapi_key

        if not rapidapi_key():
            result["news_sa"] = {"ok": False, "skipped": "no RAPIDAPI key"}
        else:
            bundle = fetch_and_save_sa_news([u], per_ticker=sa_n, sleep_sec=0.25)
            result["news_sa"] = {
                "ok": True,
                "api_items": len(bundle.get("items") or []),
                "kb_inserted": int(bundle.get("kb_inserted") or 0),
                "errors": bundle.get("errors") or {},
            }
            if bundle.get("kb_error"):
                result["news_sa"]["kb_error"] = str(bundle.get("kb_error"))[:200]
    except Exception as e:
        logger.warning("notebook bootstrap SA news %s: %s", u, e)
        result["news_sa"] = {"ok": False, "error": str(e)[:300]}

    # 3) Yahoo (+ Marketaux if key) → knowledge_base
    try:
        from config_loader import get_config_value
        from services.ticker_news_merge_fetcher import (
            fetch_marketaux_news,
            fetch_yahoo_news,
            merge_articles,
            save_articles_to_kb,
        )

        yahoo = fetch_yahoo_news([u], lookback_hours=72, max_per_ticker=5, exchange="NYSE")
        mx_key = (get_config_value("MARKETAUX_API_KEY", "") or "").strip()
        marketaux = []
        if mx_key:
            marketaux = fetch_marketaux_news(
                mx_key, [u], lookback_hours=72, exchange="NYSE", limit=40
            )
        merged = merge_articles(yahoo, marketaux)
        inserted = int(save_articles_to_kb(merged) or 0)
        result["news_yahoo"] = {
            "ok": True,
            "yahoo": len(yahoo),
            "marketaux": len(marketaux),
            "merged": len(merged),
            "kb_inserted": inserted,
        }
    except Exception as e:
        logger.warning("notebook bootstrap Yahoo news %s: %s", u, e)
        result["news_yahoo"] = {"ok": False, "error": str(e)[:300]}

    result["ok"] = bool(
        result.get("quotes", {}).get("ok")
        or result.get("news_sa", {}).get("ok")
        or result.get("news_yahoo", {}).get("ok")
    )
    return result


def set_notebook_ticker_group(
    sym: str,
    group: str,
    *,
    updated_by: str = "notebook-ui",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Move ticker to another notebook group (overlay group field)."""
    u = normalize_notebook_symbol(sym)
    g = normalize_notebook_group(group)
    if not _ticker_exists(u, path):
        raise KeyError(f"ticker {u} not in notebook")

    prev_group = None
    try:
        _, base = _find_base_ticker(u)
        prev_group = str(base.get("group") or "")
    except KeyError:
        prev_group = None

    ov, tickers, row = _load_override_ticker_row(u, path)
    if prev_group is None and row.get("group"):
        prev_group = str(row.get("group"))
    now = datetime.now(timezone.utc).isoformat()
    row["group"] = g
    row["sym"] = u
    row["updated_at_utc"] = now
    row["updated_by"] = (updated_by or "notebook-ui")[:80]
    # Keep overlay-only stubs usable after move.
    if row.get("is_new") or prev_group is None:
        row.setdefault("is_new", True)
        if "levels" not in row:
            stub = blank_ticker_card(u, g, name=str(row.get("name") or ""))
            for k, v in stub.items():
                row.setdefault(k, v)
    tickers[u] = row
    ov["tickers"] = tickers
    save_notebook_overrides(ov, path)
    return {
        "ticker": u,
        "group": g,
        "from_group": prev_group,
        "updated_at_utc": now,
        "updated_by": row["updated_by"],
    }


def _sentiment_label_01(score: Optional[float]) -> str:
    """Map KB/FinBERT scale 0..1 → short RU label."""
    if score is None:
        return "—"
    s = float(score)
    if s >= 0.6:
        return "bullish"
    if s <= 0.4:
        return "bearish"
    return "neutral"


def get_ticker_kb_news_sentiment(
    sym: str,
    *,
    lookback_hours: int = 168,
    limit: int = 40,
    include_macro: bool = False,
) -> Dict[str, Any]:
    """Average FinBERT/KB sentiment_score for ticker news (+ optional MACRO rows).

    Scores are 0..1 (ProsusAI/finbert via add_sentiment_to_news_cron). Rows without
    sentiment_score are counted but excluded from the mean.
    """
    u = str(sym or "").strip().upper()
    if not u:
        raise ValueError("empty ticker")

    tickers = [u]
    if include_macro:
        tickers.extend(["MACRO", "US_MACRO"])

    hours = max(1, min(int(lookback_hours), 24 * 30))
    lim = max(1, min(int(limit), 100))

    try:
        from services.seeking_alpha_finance import load_kb_news_items

        items = load_kb_news_items(tickers, lookback_hours=hours, source=None, limit=lim)
    except Exception as e:
        logger.warning("KB news sentiment load failed for %s: %s", u, e)
        return {
            "ticker": u,
            "lookback_hours": hours,
            "news_count": 0,
            "scored_count": 0,
            "avg_sentiment": None,
            "label": "—",
            "method": "FinBERT/KB",
            "error": str(e),
            "articles": [],
        }

    # Prefer exact ticker rows for the mean; MACRO only if include_macro and no ticker rows.
    ticker_items = [it for it in items if str(it.get("ticker") or "").upper() == u]
    pool = ticker_items if ticker_items else (items if include_macro else [])

    scores: List[float] = []
    articles: List[Dict[str, Any]] = []
    for it in pool:
        sc = it.get("sentiment_score")
        sc_f: Optional[float] = None
        if sc is not None:
            try:
                sc_f = float(sc)
                scores.append(sc_f)
            except (TypeError, ValueError):
                sc_f = None
        title = str(it.get("title") or it.get("summary_text") or "")[:220]
        articles.append(
            {
                "title": title,
                "link": str(it.get("link") or "").strip(),
                "source": str(it.get("src") or it.get("source") or ""),
                "publishOn": str(it.get("publishOn") or ""),
                "sentiment_score": round(sc_f, 3) if sc_f is not None else None,
                "label": _sentiment_label_01(sc_f),
            }
        )

    avg = round(sum(scores) / len(scores), 3) if scores else None
    return {
        "ticker": u,
        "lookback_hours": hours,
        "news_count": len(pool),
        "scored_count": len(scores),
        "avg_sentiment": avg,
        "label": _sentiment_label_01(avg),
        "method": "FinBERT/KB",
        "articles": articles,
    }


def update_ticker_signals(
    sym: str,
    *,
    macro_alive: Optional[bool] = None,
    sentiment_broken: Optional[bool] = None,
    updated_by: str = "notebook-ui",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist macro gate flags for one ticker into overrides overlay."""
    u, base_row = _find_base_ticker(sym)
    if macro_alive is None and sentiment_broken is None:
        raise ValueError("no signal fields to update")

    ov, tickers, row = _load_override_ticker_row(u, path)
    base_sig = dict(base_row.get("signals") or {}) if isinstance(base_row.get("signals"), dict) else {}
    prev_sig = dict(row.get("signals") or {}) if isinstance(row.get("signals"), dict) else {}
    sig = {**base_sig, **prev_sig}
    if macro_alive is not None:
        sig["macroAlive"] = bool(macro_alive)
    if sentiment_broken is not None:
        sig["sentimentBroken"] = bool(sentiment_broken)

    now = datetime.now(timezone.utc).isoformat()
    row["signals"] = {
        "macroAlive": bool(sig.get("macroAlive", True)),
        "sentimentBroken": bool(sig.get("sentimentBroken", False)),
    }
    row["updated_at_utc"] = now
    row["updated_by"] = (updated_by or "notebook-ui")[:80]
    tickers[u] = row
    ov["tickers"] = tickers
    save_notebook_overrides(ov, path)
    return {
        "ticker": u,
        "signals": dict(row["signals"]),
        "updated_at_utc": now,
        "updated_by": row["updated_by"],
    }


def _parse_level_number(val: Any, *, field: str) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, str) and not val.strip():
        return None
    try:
        n = float(val)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field} must be a number or null") from e
    if not (n == n) or n <= 0:  # NaN / non-positive
        raise ValueError(f"{field} must be a positive number or null")
    return float(n)


def update_ticker_levels(
    sym: str,
    *,
    buy_dip: Any = ...,
    sell: Any = ...,
    note: Any = ...,
    updated_by: str = "notebook-ui",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist Buy Dip / Sell into overrides overlay (null clears a field)."""
    u, base_row = _find_base_ticker(sym)
    if buy_dip is ... and sell is ... and note is ...:
        raise ValueError("no level fields to update")

    ov, tickers, row = _load_override_ticker_row(u, path)
    base_lv = dict(base_row.get("levels") or {}) if isinstance(base_row.get("levels"), dict) else {}
    prev_lv = dict(row.get("levels") or {}) if isinstance(row.get("levels"), dict) else {}
    levels = {**base_lv, **prev_lv}

    if buy_dip is not ...:
        if buy_dip is None or (isinstance(buy_dip, str) and not str(buy_dip).strip()):
            levels["buyDip"] = None
        else:
            levels["buyDip"] = _parse_level_number(buy_dip, field="buyDip")
    if sell is not ...:
        if sell is None or (isinstance(sell, str) and not str(sell).strip()):
            levels["sell"] = None
        else:
            levels["sell"] = _parse_level_number(sell, field="sell")
    if note is not ...:
        if note is None:
            levels["note"] = None
        else:
            levels["note"] = str(note)[:240]

    now = datetime.now(timezone.utc).isoformat()
    # Keep explicit nulls so clear sticks over base JSON.
    row["levels"] = {
        "buyDip": levels.get("buyDip"),
        "sell": levels.get("sell"),
        "note": levels.get("note"),
    }
    row["updated_at_utc"] = now
    row["updated_by"] = (updated_by or "notebook-ui")[:80]
    tickers[u] = row
    ov["tickers"] = tickers
    save_notebook_overrides(ov, path)
    return {
        "ticker": u,
        "levels": dict(row["levels"]),
        "updated_at_utc": now,
        "updated_by": row["updated_by"],
    }


def _consensus_str(val: Any, *, empty_as_dash: bool = True) -> Optional[str]:
    if val is None:
        return None if not empty_as_dash else None
    s = str(val).strip()
    if not s or s in ("—", "-", "–"):
        return None
    return s[:80]


def update_ticker_consensus(
    sym: str,
    *,
    rating: Any = ...,
    pt: Any = ...,
    low: Any = ...,
    high: Any = ...,
    n: Any = ...,
    updated_by: str = "notebook-ui",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist consensus (rating / PT corridor) into overrides overlay."""
    u, base_row = _find_base_ticker(sym)
    if all(x is ... for x in (rating, pt, low, high, n)):
        raise ValueError("no consensus fields to update")

    ov, tickers, row = _load_override_ticker_row(u, path)
    base_c = dict(base_row.get("consensus") or {}) if isinstance(base_row.get("consensus"), dict) else {}
    prev_c = dict(row.get("consensus") or {}) if isinstance(row.get("consensus"), dict) else {}
    cons = {**base_c, **prev_c}

    def _set(key: str, val: Any) -> None:
        if val is ...:
            return
        parsed = _consensus_str(val)
        cons[key] = parsed if parsed is not None else "—"

    _set("rating", rating)
    _set("pt", pt)
    _set("low", low)
    _set("high", high)
    _set("n", n)
    cons["upd"] = "overlay"

    now = datetime.now(timezone.utc).isoformat()
    row["consensus"] = {
        "rating": cons.get("rating") or "—",
        "pt": cons.get("pt") or "—",
        "low": cons.get("low") or "—",
        "high": cons.get("high") or "—",
        "n": cons.get("n") or "—",
        "upd": "overlay",
    }
    row["updated_at_utc"] = now
    row["updated_by"] = (updated_by or "notebook-ui")[:80]
    tickers[u] = row
    ov["tickers"] = tickers
    save_notebook_overrides(ov, path)
    return {
        "ticker": u,
        "consensus": dict(row["consensus"]),
        "updated_at_utc": now,
        "updated_by": row["updated_by"],
    }


def refresh_ticker_houses_from_stockanalysis(
    sym: str,
    *,
    limit: int = 12,
    updated_by: str = "notebook-ui",
    path: Optional[Path] = None,
    client: Any = None,
) -> Dict[str, Any]:
    """Pull analyst ratings from stockanalysis.com → houses + consensus overlay."""
    from services.stockanalysis_ratings import StockAnalysisClient, to_notebook_houses

    u, _base_row = _find_base_ticker(sym)
    lim = max(1, min(int(limit or 12), 40))
    sa = client or StockAnalysisClient()
    bundle = sa.get_analyst_bundle(u)
    mapped = to_notebook_houses(bundle, limit=lim)

    counts = mapped.get("counts") if isinstance(mapped.get("counts"), dict) else {}
    house_note = str(mapped.get("houseNote") or "").strip()
    if not house_note:
        buy = int(counts.get("buy") or 0)
        hold = int(counts.get("hold") or 0)
        sell = int(counts.get("sell") or 0)
        house_note = f"Buy {buy} · Hold {hold} · Sell {sell}"

    ov, tickers, row = _load_override_ticker_row(u, path)
    now = datetime.now(timezone.utc).isoformat()
    houses = mapped.get("houses") if isinstance(mapped.get("houses"), list) else []
    consensus = mapped.get("consensus") if isinstance(mapped.get("consensus"), dict) else {}
    clean_houses: List[Dict[str, Any]] = []
    for h in houses:
        if not isinstance(h, dict):
            continue
        clean_houses.append(
            {
                "firm": str(h.get("firm") or "")[:80],
                "rate": str(h.get("rate") or "—")[:40],
                "pt": str(h.get("pt") or "—")[:40],
                "quote": str(h.get("quote") or "")[:320],
                "tac": "",
            }
        )
    row["houses"] = clean_houses
    row["consensus"] = {
        "rating": str(consensus.get("rating") or "—")[:80],
        "pt": str(consensus.get("pt") or "—")[:80],
        "low": str(consensus.get("low") or "—")[:80],
        "high": str(consensus.get("high") or "—")[:80],
        "n": str(consensus.get("n") or "—")[:80],
        "n_ratings": consensus.get("n_ratings"),
        "n_targets": consensus.get("n_targets"),
        "upd": str(consensus.get("upd") or f"обн. {now[:10]}")[:80],
    }
    row["houseNote"] = house_note[:240]
    row["houses_source"] = "stockanalysis"
    row["houses_counts"] = {
        "buy": int(counts.get("buy") or 0),
        "hold": int(counts.get("hold") or 0),
        "sell": int(counts.get("sell") or 0),
        "total": int(counts.get("total") or 0),
        "strong_buy": int(counts.get("strong_buy") or 0),
        "strong_sell": int(counts.get("strong_sell") or 0),
    }
    pt_n = counts.get("pt_total")
    if pt_n is None:
        pt_n = consensus.get("n_targets")
    try:
        pt_n_i = int(pt_n) if pt_n is not None else 0
    except (TypeError, ValueError):
        pt_n_i = 0
    if pt_n_i > 0:
        row["houses_counts"]["pt_total"] = pt_n_i
    row["updated_at_utc"] = now
    row["updated_by"] = (updated_by or "notebook-ui")[:80]
    tickers[u] = row
    ov["tickers"] = tickers
    save_notebook_overrides(ov, path)
    return {
        "ticker": u,
        "houses": list(row["houses"]),
        "consensus": dict(row["consensus"]),
        "houseNote": row["houseNote"],
        "houses_source": "stockanalysis",
        "houses_counts": dict(row["houses_counts"]),
        "counts": dict(row["houses_counts"]),
        "source": getattr(bundle, "source", "stockanalysis"),
        "asof": getattr(bundle, "asof", None),
        "updated_at_utc": now,
        "updated_by": row["updated_by"],
    }


_PROFILE_KEYS = (
    "Сектор / слой",
    "Роль в тетрадке",
    "Отчёт (след.)",
)

_TRIGGER_TYPES = frozenset({"buy", "sell", "add", "watch"})


def _merge_triggers(
    base: List[Any],
    patch: Any,
    *,
    levels: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    by_t: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for item in base or []:
        if not isinstance(item, dict):
            continue
        t = str(item.get("t") or "").lower()
        if t not in _TRIGGER_TYPES:
            continue
        by_t[t] = dict(item)
        if t not in order:
            order.append(t)
    if isinstance(patch, list):
        for item in patch:
            if not isinstance(item, dict):
                continue
            t = str(item.get("t") or "").lower()
            if t not in _TRIGGER_TYPES:
                continue
            cur = dict(by_t.get(t) or {"t": t})
            if "lvl" in item and item.get("lvl") is not None:
                cur["lvl"] = str(item.get("lvl"))[:120]
            if "desc" in item and item.get("desc") is not None:
                cur["desc"] = str(item.get("desc"))[:500]
            if "cond" in item and item.get("cond") is not None:
                cur["cond"] = str(item.get("cond"))[:240]
            cur["manual"] = True
            by_t[t] = cur
            if t not in order:
                order.append(t)
    # Sync buy/sell labels from levels when present.
    lv = levels or {}
    if "buy" in by_t and lv.get("buyDip") is not None:
        by_t["buy"]["lvl"] = f"${lv['buyDip']} · Buy Dip"
        by_t["buy"]["manual"] = True
    if "sell" in by_t and lv.get("sell") is not None:
        by_t["sell"]["lvl"] = f"${lv['sell']} · Sell"
        by_t["sell"]["manual"] = True
    return [by_t[t] for t in order if t in by_t]


def update_ticker_profile(
    sym: str,
    *,
    horizon: Any = ...,
    profile: Optional[Dict[str, Any]] = None,
    updated_by: str = "notebook-ui",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist horizon + profile fields (not levels — those live on Verdict)."""
    u, base_row = _find_base_ticker(sym)
    if horizon is ... and not profile:
        raise ValueError("no profile fields to update")

    ov, tickers, row = _load_override_ticker_row(u, path)
    base_pf = dict(base_row.get("profile") or {}) if isinstance(base_row.get("profile"), dict) else {}
    prev_pf = dict(row.get("profile") or {}) if isinstance(row.get("profile"), dict) else {}
    pf = {**base_pf, **prev_pf}

    if isinstance(profile, dict):
        for key in _PROFILE_KEYS:
            if key in profile:
                val = profile.get(key)
                if val is None or (isinstance(val, str) and not str(val).strip()):
                    pf[key] = ""
                else:
                    pf[key] = str(val)[:240]

    # Drop duplicate of Verdict levels if present in overlay profile.
    pf.pop("Целевая прибыль", None)

    now = datetime.now(timezone.utc).isoformat()
    if horizon is not ...:
        row["horizon"] = "" if horizon is None else str(horizon).strip()[:160]
    row["profile"] = {k: pf.get(k, "") for k in _PROFILE_KEYS}
    # Keep any other non-target keys from previous overlay (except target profit).
    for k, v in pf.items():
        if k not in row["profile"] and k != "Целевая прибыль":
            row["profile"][k] = v
    row["updated_at_utc"] = now
    row["updated_by"] = (updated_by or "notebook-ui")[:80]
    tickers[u] = row
    ov["tickers"] = tickers
    save_notebook_overrides(ov, path)
    return {
        "ticker": u,
        "horizon": row.get("horizon", base_row.get("horizon")),
        "profile": dict(row["profile"]),
        "updated_at_utc": now,
        "updated_by": row["updated_by"],
    }


def _normalize_fundament(raw: Any) -> Dict[str, Any]:
    """Clamp fundament passport card for overlay storage."""
    if not isinstance(raw, dict):
        return {}
    metrics_out: List[Dict[str, str]] = []
    metrics = raw.get("metrics") if isinstance(raw.get("metrics"), list) else []
    for m in metrics[:8]:
        if not isinstance(m, dict):
            continue
        k = str(m.get("k") or "").strip()[:40]
        v = str(m.get("v") or "").strip()[:80]
        note = str(m.get("note") or "").strip()[:160]
        tone = str(m.get("tone") or "").strip()[:12]
        if not (k or v or note):
            continue
        metrics_out.append(
            {"k": k or "—", "v": v or "—", "note": note, "tone": tone if tone in ("good", "bad", "mid") else ""}
        )

    def _lines(key: str) -> List[str]:
        items = raw.get(key)
        if not isinstance(items, list):
            return []
        out: List[str] = []
        for x in items:
            s = str(x or "").strip()
            if s:
                out.append(s[:240])
            if len(out) >= 12:
                break
        return out

    filing_url = str(raw.get("filing_url") or "").strip()[:500]
    if filing_url and not (
        filing_url.startswith("https://") or filing_url.startswith("http://")
    ):
        filing_url = ""

    return {
        "exchange": str(raw.get("exchange") or "").strip()[:40],
        "hq_ru": str(raw.get("hq_ru") or "").strip()[:160],
        "listing_origin_ru": str(raw.get("listing_origin_ru") or "").strip()[:240],
        "key_clients_ru": str(raw.get("key_clients_ru") or "").strip()[:240],
        "tagline": str(raw.get("tagline") or "")[:500],
        "metrics": metrics_out,
        "margin_ru": str(raw.get("margin_ru") or "")[:500],
        "financing_ru": str(raw.get("financing_ru") or "")[:500],
        "pluses": _lines("pluses"),
        "risks": _lines("risks"),
        "filing_url": filing_url,
    }


_REPORT_EXPECT_WATCH_KEYS = (
    "driver_ru",
    "revenue_arr_ru",
    "leading_ru",
    "capex_ru",
    "margin_path_ru",
    "guidance_ru",
    "tactics_map_ru",
)
_REPORT_EXPECT_LAST_KEYS = (
    "date_verdict_ru",
    "why_ru",
    "risk_shift_ru",
)


def _normalize_report_expect(raw: Any) -> Dict[str, Any]:
    """Clamp earnings-prep card (watch metrics + last punishment)."""
    if not isinstance(raw, dict):
        return {"watch": {}, "last": {}}
    watch_in = raw.get("watch") if isinstance(raw.get("watch"), dict) else {}
    last_in = raw.get("last") if isinstance(raw.get("last"), dict) else {}
    # Flat keys on root also accepted (UI convenience).
    for k in _REPORT_EXPECT_WATCH_KEYS:
        if k not in watch_in and raw.get(k) is not None:
            watch_in[k] = raw.get(k)
    for k in _REPORT_EXPECT_LAST_KEYS:
        if k not in last_in and raw.get(k) is not None:
            last_in[k] = raw.get(k)

    watch = {k: str(watch_in.get(k) or "").strip()[:500] for k in _REPORT_EXPECT_WATCH_KEYS}
    last = {k: str(last_in.get(k) or "").strip()[:500] for k in _REPORT_EXPECT_LAST_KEYS}
    return {"watch": watch, "last": last}


def update_ticker_fundament(
    sym: str,
    *,
    fundament: Dict[str, Any],
    updated_by: str = "notebook-ui",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist optional fundament card into overrides overlay."""
    u, _base_row = _find_base_ticker(sym)
    if not isinstance(fundament, dict):
        raise ValueError("fundament object required")
    cleaned = _normalize_fundament(fundament)
    ov, tickers, row = _load_override_ticker_row(u, path)
    now = datetime.now(timezone.utc).isoformat()
    row["fundament"] = cleaned
    row["updated_at_utc"] = now
    row["updated_by"] = (updated_by or "notebook-ui")[:80]
    tickers[u] = row
    ov["tickers"] = tickers
    save_notebook_overrides(ov, path)
    return {
        "ticker": u,
        "fundament": dict(cleaned),
        "updated_at_utc": now,
        "updated_by": row["updated_by"],
    }


def update_ticker_report_expect(
    sym: str,
    *,
    report_expect: Dict[str, Any],
    updated_by: str = "notebook-ui",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist earnings-prep card into overrides overlay."""
    u, _base_row = _find_base_ticker(sym)
    if not isinstance(report_expect, dict):
        raise ValueError("report_expect object required")
    cleaned = _normalize_report_expect(report_expect)
    ov, tickers, row = _load_override_ticker_row(u, path)
    now = datetime.now(timezone.utc).isoformat()
    row["report_expect"] = cleaned
    row["updated_at_utc"] = now
    row["updated_by"] = (updated_by or "notebook-ui")[:80]
    tickers[u] = row
    ov["tickers"] = tickers
    save_notebook_overrides(ov, path)
    return {
        "ticker": u,
        "report_expect": dict(cleaned),
        "updated_at_utc": now,
        "updated_by": row["updated_by"],
    }


def _join_signal_list(raw: Any, *, limit: int = 3) -> str:
    if not isinstance(raw, list):
        return ""
    bits: List[str] = []
    for item in raw:
        s = str(item or "").strip()
        if s:
            bits.append(s)
        if len(bits) >= limit:
            break
    return " · ".join(bits)


def _fmt_surprise_label(pct: Any) -> str:
    try:
        v = float(pct)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(v):
        return ""
    return "BEAT" if v > 0.25 else ("MISS" if v < -0.25 else "INLINE")


def _map_earnings_brief_to_report_fields(brief: Dict[str, Any]) -> Dict[str, str]:
    """Map IR/SEC→LLM Event Brief into report_expect fact drafts (no Nastya judgment)."""
    out: Dict[str, str] = {}
    if not isinstance(brief, dict) or brief.get("status") not in ("ok", "partial"):
        return out

    g_raw = brief.get("guidance")
    g = g_raw if isinstance(g_raw, dict) else {}
    scen = brief.get("scenario") if isinstance(brief.get("scenario"), dict) else {}
    src_out = brief.get("source_outcomes") if isinstance(brief.get("source_outcomes"), dict) else {}

    ai = _join_signal_list(brief.get("ai_demand_signals"))
    margin_sig = _join_signal_list(brief.get("margin_pressure_signals"))
    supply = _join_signal_list(brief.get("inventory_or_supply_notes"))
    qa = _join_signal_list(brief.get("qa_concerns"), limit=2)
    capex_notes = str(brief.get("capex_notes") or "").strip()
    tone = str(brief.get("management_tone") or "").strip()
    headline = str(brief.get("headline") or "").strip()
    fiscal = str(brief.get("fiscal_period") or "").strip()
    ev_date = str(brief.get("event_date") or "").strip()

    if ai:
        out["driver_ru"] = ai[:500]
    elif scen.get("rationale"):
        out["driver_ru"] = str(scen.get("rationale")).strip()[:500]
    elif headline:
        out["driver_ru"] = headline[:500]

    rev_bits: List[str] = []
    ra, re_ = brief.get("revenue_actual"), brief.get("revenue_estimate")
    if ra is not None:
        try:
            rev_bits.append(f"rev actual {_fmt_usd_compact(float(ra)) or ra}")
        except (TypeError, ValueError):
            rev_bits.append(f"rev actual {ra}")
    if re_ is not None:
        try:
            rev_bits.append(f"est {_fmt_usd_compact(float(re_)) or re_}")
        except (TypeError, ValueError):
            rev_bits.append(f"est {re_}")
    rs = brief.get("revenue_surprise_pct")
    if rs is not None:
        try:
            rev_bits.append(f"surprise {float(rs):+.1f}%")
        except (TypeError, ValueError):
            pass
    ea, ee = brief.get("eps_actual"), brief.get("eps_estimate")
    if ea is not None or ee is not None:
        eps_bit = "EPS"
        if ea is not None:
            eps_bit += f" {ea}"
        if ee is not None:
            eps_bit += f" vs est {ee}"
        es = brief.get("eps_surprise_pct")
        if es is not None:
            try:
                eps_bit += f" ({float(es):+.1f}%)"
            except (TypeError, ValueError):
                pass
        rev_bits.append(eps_bit)
    if rev_bits:
        out["revenue_arr_ru"] = " · ".join(str(x) for x in rev_bits)[:500]

    if supply:
        out["leading_ru"] = supply[:500]
    else:
        quotes = brief.get("evidence_quotes") if isinstance(brief.get("evidence_quotes"), list) else []
        for q in quotes:
            if not isinstance(q, dict):
                continue
            topic = str(q.get("topic") or "").lower()
            txt = str(q.get("quote") or "").strip()
            if txt and any(t in topic for t in ("ai_demand", "other", "margin")):
                if "backlog" in txt.lower() or "book" in txt.lower() or "capacity" in txt.lower() or "rpo" in txt.lower():
                    out["leading_ru"] = txt[:500]
                    break

    capex_bits = [x for x in (str(g.get("capex_outlook") or "").strip(), capex_notes) if x]
    if capex_bits:
        out["capex_ru"] = " · ".join(capex_bits)[:500]

    margin_bits = [x for x in (str(g.get("margin_outlook") or "").strip(), margin_sig) if x]
    if margin_bits:
        out["margin_path_ru"] = " · ".join(margin_bits)[:500]

    g_bits: List[str] = []
    direction = str(g.get("direction") or "").strip()
    if direction and direction not in ("not_disclosed", "null"):
        g_bits.append(direction)
    for key in ("revenue_outlook", "eps_outlook"):
        v = str(g.get(key) or "").strip()
        if v:
            g_bits.append(v)
    if g_bits:
        out["guidance_ru"] = " · ".join(g_bits)[:500]

    # Last quarter verdict from surprises + reaction (log-return → %)
    beat = (
        _fmt_surprise_label(brief.get("eps_surprise_pct"))
        or _fmt_surprise_label(brief.get("revenue_surprise_pct"))
    )
    verdict_bits: List[str] = []
    if fiscal:
        verdict_bits.append(fiscal)
    if ev_date:
        verdict_bits.append(ev_date)
    if beat:
        verdict_bits.append(beat)
    if tone:
        verdict_bits.append(tone)
    if src_out.get("forward_log_ret_1d") is not None:
        try:
            pct = (math.expm1(float(src_out["forward_log_ret_1d"]))) * 100.0
            verdict_bits.append(f"реакция 1d {pct:+.1f}%")
        except (TypeError, ValueError):
            pass
    if verdict_bits:
        out["date_verdict_ru"] = " · ".join(verdict_bits)[:500]

    why_bits: List[str] = []
    if scen.get("id"):
        why_bits.append(str(scen.get("id")))
    if scen.get("rationale"):
        why_bits.append(str(scen.get("rationale")).strip())
    elif headline:
        why_bits.append(headline)
    if qa:
        why_bits.append(f"Q&A: {qa}")
    quotes = brief.get("evidence_quotes") if isinstance(brief.get("evidence_quotes"), list) else []
    for q in quotes[:2]:
        if isinstance(q, dict):
            txt = str(q.get("quote") or "").strip()
            if txt and txt not in why_bits:
                why_bits.append(txt)
    if why_bits:
        out["why_ru"] = " · ".join(why_bits)[:500]

    return out


def _latest_earnings_material_url(symbol: str) -> Dict[str, str]:
    """Best IR/SEC URL from earnings_material for fundament.filing_url draft."""
    empty = {"filing_url": "", "source_name": "", "material_type": ""}
    u = str(symbol or "").strip().upper()
    if not u:
        return empty
    try:
        from sqlalchemy import create_engine, text

        from config_loader import get_database_url

        eng = create_engine(get_database_url(), pool_pre_ping=True)
        try:
            q = text(
                """
                SELECT source_url, source_name, material_type
                FROM earnings_material
                WHERE UPPER(TRIM(symbol)) = :symbol
                  AND COALESCE(source_url, '') <> ''
                  AND COALESCE(parse_status, '') NOT IN ('failed', 'blocked')
                ORDER BY
                  CASE material_type
                    WHEN 'sec_filing' THEN 0
                    WHEN 'press_release' THEN 1
                    WHEN 'presentation' THEN 2
                    WHEN 'transcript' THEN 3
                    ELSE 9
                  END,
                  COALESCE(event_date, DATE '1900-01-01') DESC,
                  id DESC
                LIMIT 1
                """
            )
            with eng.connect() as conn:
                row = conn.execute(q, {"symbol": u}).mappings().first()
        finally:
            eng.dispose()
        if not row:
            return empty
        url = str(row.get("source_url") or "").strip()[:500]
        if not (url.startswith("http://") or url.startswith("https://")):
            return empty
        return {
            "filing_url": url,
            "source_name": str(row.get("source_name") or "").strip()[:120],
            "material_type": str(row.get("material_type") or "").strip()[:40],
        }
    except Exception as e:
        logger.warning("earnings_material url for %s: %s", u, e)
        return empty


def suggest_report_expect_from_sources(sym: str) -> Dict[str, Any]:
    """Draft report_expect fact fields from earnings brief (IR/SEC→LLM) + KB news.

    Priority: structured Event Brief fields, then KB title keywords as fallback.
    Does **not** fill Nastya judgment fields (tactics_map_ru, risk_shift_ru).
    """
    u = str(sym or "").strip().upper()
    if not u:
        raise ValueError("ticker required")

    titles: List[str] = []
    sources_hit: List[str] = []
    try:
        from services.seeking_alpha_finance import load_kb_news_items

        items = load_kb_news_items([u], lookback_hours=24 * 21, source=None, limit=24)
        for it in items or []:
            if not isinstance(it, dict):
                continue
            t = str(it.get("title") or it.get("content") or "").strip()
            if t:
                titles.append(t[:220])
            src = str(it.get("src") or it.get("source") or "").strip()
            if src and src not in sources_hit:
                sources_hit.append(src)
    except Exception as e:
        logger.warning("report_expect KB suggest failed for %s: %s", u, e)

    brief: Dict[str, Any] = {}
    brief_from_struct: Dict[str, str] = {}
    try:
        from sqlalchemy import create_engine

        from config_loader import get_database_url
        from services.earnings_intelligence_api import get_event_brief_payload

        eng = create_engine(get_database_url(), pool_pre_ping=True)
        try:
            payload = get_event_brief_payload(eng, symbol=u)
        finally:
            eng.dispose()
        if isinstance(payload, dict):
            brief = payload
            brief_from_struct = _map_earnings_brief_to_report_fields(brief)
            if brief.get("status") in ("ok", "partial"):
                if "earnings brief (IR/SEC→LLM)" not in sources_hit:
                    sources_hit.append("earnings brief (IR/SEC→LLM)")
    except Exception as e:
        logger.warning("report_expect brief suggest failed for %s: %s", u, e)

    def _pick(*needles: str, fallback: str = "") -> str:
        for title in titles:
            low = title.lower()
            if any(n in low for n in needles):
                return title[:500]
        return fallback[:500] if fallback else ""

    # Brief structured first; KB titles only fill gaps.
    driver = brief_from_struct.get("driver_ru") or _pick(
        "azure", "cloud", "ads", "ai ", "gpu", "demand",
        fallback=(titles[0] if titles else ""),
    )
    revenue = brief_from_struct.get("revenue_arr_ru") or _pick(
        "revenue", "выруч", "sales", "arr", "beat", "miss", "eps",
    )
    leading = brief_from_struct.get("leading_ru") or _pick(
        "rpo", "backlog", "bookings", "contract", "capacity", "sold out",
    )
    capex = brief_from_struct.get("capex_ru") or _pick(
        "capex", "capital expenditure", "капекс",
    )
    margin = brief_from_struct.get("margin_path_ru") or _pick(
        "margin", "fcf", "free cash", "ebitda", "operating income",
    )
    guidance = brief_from_struct.get("guidance_ru") or _pick(
        "guidance", "outlook", "гайд", "forecast", "expects",
    )
    date_verdict = brief_from_struct.get("date_verdict_ru") or (
        f"по KB · {titles[0][:160]}" if titles else ""
    )
    why = brief_from_struct.get("why_ru") or (titles[0][:500] if titles else "")

    draft = _normalize_report_expect(
        {
            "watch": {
                "driver_ru": driver,
                "revenue_arr_ru": revenue,
                "leading_ru": leading,
                "capex_ru": capex,
                "margin_path_ru": margin,
                "guidance_ru": guidance,
                # Nastya-only — leave empty
                "tactics_map_ru": "",
            },
            "last": {
                "date_verdict_ru": date_verdict,
                "why_ru": why,
                "risk_shift_ru": "",
            },
        }
    )

    fact_keys = (
        "driver_ru",
        "revenue_arr_ru",
        "leading_ru",
        "capex_ru",
        "margin_path_ru",
        "guidance_ru",
    )
    facts_filled = sum(1 for k in fact_keys if (draft["watch"].get(k) or "").strip())
    has_kb = bool(titles)
    has_brief = brief.get("status") in ("ok", "partial")
    has_llm = bool(brief.get("management_tone") or brief_from_struct.get("guidance_ru"))
    sufficiency = {
        "kb_news": has_kb,
        "kb_news_n": len(titles),
        "earnings_brief": has_brief,
        "earnings_llm_extract": has_llm,
        "facts_drafted": facts_filled,
        "facts_total": len(fact_keys),
        "enough_for_draft": facts_filled >= 2 and (has_kb or has_brief),
        "needs_nastya": ["tactics_map_ru", "risk_shift_ru"],
        "note_ru": (
            "Черновик: приоритет IR/SEC→LLM Event Brief, пробелы — KB. "
            "Суждение (тактика, смещение риска) — только Настя. Сверьте с IR/10-Q перед OK."
        ),
    }

    return {
        "ticker": u,
        "report_expect": draft,
        "sources": sources_hit[:12],
        "sufficiency": sufficiency,
        "note": sufficiency["note_ru"],
        "filled": [k for k in fact_keys if (draft["watch"].get(k) or "").strip()],
        "brief_status": brief.get("status"),
        "brief_event_date": brief.get("event_date"),
    }


def update_ticker_plan(
    sym: str,
    *,
    entry: Optional[Sequence[Any]] = None,
    exit_plan: Optional[Sequence[Any]] = None,
    macro: Any = ...,
    updated_by: str = "notebook-ui",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist entry/exit bullet plan + macro blurb into overrides overlay."""
    u, base_row = _find_base_ticker(sym)
    if entry is None and exit_plan is None and macro is ...:
        raise ValueError("no plan fields to update")

    def _bullets(raw: Optional[Sequence[Any]]) -> List[str]:
        if raw is None:
            return []
        out: List[str] = []
        for x in raw:
            s = str(x or "").strip()
            if not s:
                continue
            # Strip HTML-ish leftovers from older notebook samples.
            s = s.replace("<b>", "").replace("</b>", "")
            out.append(s[:500])
            if len(out) >= 20:
                break
        return out

    ov, tickers, row = _load_override_ticker_row(u, path)
    now = datetime.now(timezone.utc).isoformat()

    if entry is not None:
        row["entry"] = _bullets(entry)
    if exit_plan is not None:
        row["exit"] = _bullets(exit_plan)
    if macro is not ...:
        row["macro"] = "" if macro is None else str(macro).strip()[:800]

    row["updated_at_utc"] = now
    row["updated_by"] = (updated_by or "notebook-ui")[:80]
    tickers[u] = row
    ov["tickers"] = tickers
    save_notebook_overrides(ov, path)

    eff_entry = row["entry"] if "entry" in row else list(base_row.get("entry") or [])
    eff_exit = row["exit"] if "exit" in row else list(base_row.get("exit") or [])
    eff_macro = row["macro"] if "macro" in row else str(base_row.get("macro") or "")
    return {
        "ticker": u,
        "entry": list(eff_entry) if isinstance(eff_entry, list) else [],
        "exit": list(eff_exit) if isinstance(eff_exit, list) else [],
        "macro": str(eff_macro or ""),
        "updated_at_utc": now,
        "updated_by": row["updated_by"],
    }


def update_ticker_triggers(
    sym: str,
    *,
    triggers: Sequence[Dict[str, Any]],
    updated_by: str = "notebook-ui",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist trigger texts (buy/sell/watch). Buy/Sell $ levels stay on Verdict."""
    u, base_row = _find_base_ticker(sym)
    if not triggers:
        raise ValueError("no triggers to update")

    ov, tickers, row = _load_override_ticker_row(u, path)
    base_tr = list(base_row.get("triggers") or []) if isinstance(base_row.get("triggers"), list) else []
    prev_tr = list(row.get("triggers") or []) if isinstance(row.get("triggers"), list) else []
    # Prefer previous overlay as base for merge so partial saves keep other types.
    seed = prev_tr if prev_tr else base_tr
    levels = None
    if isinstance(row.get("levels"), dict):
        levels = row.get("levels")
    elif isinstance(base_row.get("levels"), dict):
        levels = base_row.get("levels")
    # Also load effective levels from overlay merge with base
    if isinstance(row.get("levels"), dict) or isinstance(base_row.get("levels"), dict):
        bl = dict(base_row.get("levels") or {}) if isinstance(base_row.get("levels"), dict) else {}
        ol = dict(row.get("levels") or {}) if isinstance(row.get("levels"), dict) else {}
        levels = {**bl, **ol}

    merged = _merge_triggers(seed, list(triggers), levels=levels)
    # Persist only editable payload (t/lvl/desc/cond/manual)
    stored = []
    for item in merged:
        t = str(item.get("t") or "")
        entry: Dict[str, Any] = {
            "t": t,
            "desc": str(item.get("desc") or "")[:500],
            "cond": str(item.get("cond") or "")[:240],
            "manual": True,
        }
        # buy/sell lvl is derived from levels on read; still store watch/add lvl
        if t in ("watch", "add") or levels is None:
            entry["lvl"] = str(item.get("lvl") or "")[:120]
        elif t == "buy" and levels.get("buyDip") is None:
            entry["lvl"] = str(item.get("lvl") or "")[:120]
        elif t == "sell" and levels.get("sell") is None:
            entry["lvl"] = str(item.get("lvl") or "")[:120]
        stored.append(entry)

    now = datetime.now(timezone.utc).isoformat()
    row["triggers"] = stored
    row["updated_at_utc"] = now
    row["updated_by"] = (updated_by or "notebook-ui")[:80]
    tickers[u] = row
    ov["tickers"] = tickers
    save_notebook_overrides(ov, path)
    effective = _merge_triggers(base_tr, stored, levels=levels)
    return {
        "ticker": u,
        "triggers": effective,
        "updated_at_utc": now,
        "updated_by": row["updated_by"],
    }


def update_ticker_env(
    sym: str,
    *,
    items: Optional[Sequence[Dict[str, Any]]] = None,
    fed: Optional[Dict[str, Any]] = None,
    pt_cuts: Optional[Dict[str, Any]] = None,
    updated_by: str = "notebook-ui",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist manual Env gate states (Fed / PT cuts). VIX is rejected."""
    u, base_row = _find_base_ticker(sym)

    patch_list: List[Dict[str, Any]] = []
    if items:
        for it in items:
            if isinstance(it, dict):
                patch_list.append(it)
    if isinstance(fed, dict):
        patch_list.append({"lbl": "Риторика ФРС", **fed})
    if isinstance(pt_cuts, dict):
        patch_list.append({"lbl": "Понижения таргетов (вне earnings)", **pt_cuts})
    if not patch_list:
        raise ValueError("no env fields to update")

    normalized: List[Dict[str, Any]] = []
    for it in patch_list:
        key = _env_label_key(str(it.get("lbl") or it.get("key") or ""))
        if key is None:
            low = str(it.get("lbl") or "").lower()
            if "vix" in low:
                raise ValueError("VIX / NDX / нефть — только live, не вручную")
            raise ValueError(f"unknown env label: {it.get('lbl')}")
        state = str(it.get("state") or "").lower()
        if state not in _ENV_STATES:
            raise ValueError("env state must be ok, mid, or bad")
        entry: Dict[str, Any] = {
            "lbl": "Риторика ФРС" if key == "fed" else "Понижения таргетов (вне earnings)",
            "key": key,
            "state": state,
            "live": False,
            "source": "manual · overlay",
        }
        if it.get("st") is not None:
            entry["st"] = str(it.get("st"))[:200]
        else:
            entry["st"] = {
                "ok": "чисто · ручной",
                "mid": "жёлтый · ручной",
                "bad": "красный · ручной",
            }[state]
        normalized.append(entry)

    ov, tickers, row = _load_override_ticker_row(u, path)
    prev = row.get("env")
    # Store as map keyed by fed/pt for stable merges
    env_map: Dict[str, Dict[str, Any]] = {}
    if isinstance(prev, dict):
        for k, v in prev.items():
            if isinstance(v, dict) and k in ("fed", "pt"):
                env_map[k] = dict(v)
    elif isinstance(prev, list):
        for v in prev:
            if isinstance(v, dict):
                k = _env_label_key(str(v.get("lbl") or ""))
                if k:
                    env_map[k] = dict(v)
    for entry in normalized:
        env_map[str(entry["key"])] = entry

    now = datetime.now(timezone.utc).isoformat()
    row["env"] = env_map
    row["updated_at_utc"] = now
    row["updated_by"] = (updated_by or "notebook-ui")[:80]
    tickers[u] = row
    ov["tickers"] = tickers
    save_notebook_overrides(ov, path)

    # Return effective env list for UI (base + override)
    base_env = list(base_row.get("env") or []) if isinstance(base_row.get("env"), list) else []
    effective = _merge_env_rows(base_env, env_map)
    return {
        "ticker": u,
        "env": effective,
        "env_override": env_map,
        "updated_at_utc": now,
        "updated_by": row["updated_by"],
    }


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


def _fmt_usd_compact(n: Any) -> Optional[str]:
    try:
        x = float(n)
    except (TypeError, ValueError):
        return None
    if not (x == x):  # NaN
        return None
    ax = abs(x)
    sign = "-" if x < 0 else ""
    if ax >= 1e12:
        return f"{sign}${ax / 1e12:.1f}T"
    if ax >= 1e9:
        return f"{sign}${ax / 1e9:.1f}B"
    if ax >= 1e6:
        return f"{sign}${ax / 1e6:.0f}M"
    return f"{sign}${ax:,.0f}"


def _fmt_pct_ratio(n: Any) -> Optional[str]:
    try:
        x = float(n)
    except (TypeError, ValueError):
        return None
    if not (x == x):
        return None
    # yfinance margins are usually 0..1 ratios
    if -1.5 <= x <= 1.5:
        x *= 100.0
    return f"{x:.1f}%"


def _yfinance_annual_fcf(ticker: Any) -> Tuple[Optional[float], Optional[str]]:
    """Prefer annual cashflow 'Free Cash Flow' (comparable to SEC FY OCF-CapEx).

    ``info['freeCashflow']`` is often a short window (~1Q) and mismatches SEC FY.
    """
    try:
        cf = ticker.cashflow
    except Exception:
        cf = None
    if cf is not None and not getattr(cf, "empty", True):
        for label in ("Free Cash Flow", "FreeCashFlow"):
            if label not in cf.index:
                continue
            series = cf.loc[label].dropna()
            if series.empty:
                continue
            try:
                val = float(series.iloc[0])
            except (TypeError, ValueError):
                continue
            col = series.index[0]
            try:
                end = col.date().isoformat() if hasattr(col, "date") else str(col)[:10]
            except Exception:
                end = str(col)[:10]
            return val, end
    return None, None


def _yfinance_interest_bearing_debt(ticker: Any) -> Tuple[Optional[float], Optional[str], str]:
    """Balance-sheet interest-bearing debt (excl. operating leases).

    ``info['totalDebt']`` is often inflated vs the annual balance sheet.
    """
    try:
        bs = ticker.balance_sheet
    except Exception:
        bs = None
    if bs is not None and not getattr(bs, "empty", True):
        col = bs.columns[0]
        try:
            end = col.date().isoformat() if hasattr(col, "date") else str(col)[:10]
        except Exception:
            end = str(col)[:10]

        def _row(*names: str) -> Optional[float]:
            for name in names:
                if name not in bs.index:
                    continue
                try:
                    v = float(bs.loc[name].iloc[0])
                except (TypeError, ValueError):
                    continue
                if v == v:  # not NaN
                    return v
            return None

        ltd = _row("Long Term Debt", "LongTermDebt")
        cur = _row("Current Debt", "CurrentDebt", "Other Current Borrowings")
        if ltd is not None and cur is not None:
            return ltd + cur, end, "Yahoo BS LT+current (без leases)"
        if ltd is not None:
            return ltd, end, "Yahoo BS long-term debt"
        # Total Debt on Yahoo BS usually includes capital leases — only as fallback
        total = _row("Total Debt", "TotalDebt")
        if total is not None:
            return total, end, "Yahoo BS totalDebt (может включать leases)"
    return None, None, ""


def _yahoo_exchange_label(info: Dict[str, Any]) -> str:
    """Map Yahoo exchange codes to human labels (NASDAQ / NYSE / …)."""
    raw = str(
        info.get("fullExchangeName")
        or info.get("exchange")
        or info.get("market")
        or ""
    ).strip()
    if not raw:
        return ""
    up = raw.upper()
    # Common Yahoo short codes
    code_map = {
        "NMS": "NASDAQ",
        "NGM": "NASDAQ",
        "NCM": "NASDAQ",
        "NAS": "NASDAQ",
        "NASDAQ": "NASDAQ",
        "NYQ": "NYSE",
        "NYSE": "NYSE",
        "PCX": "NYSE Arca",
        "ASE": "NYSE American",
        "AMEX": "NYSE American",
        "OTC": "OTC",
        "PNK": "OTC",
    }
    if up in code_map:
        return code_map[up]
    if "NASDAQ" in up:
        return "NASDAQ"
    if "NYSE" in up:
        return "NYSE"
    return raw[:40]


def _yahoo_hq_ru(info: Dict[str, Any]) -> str:
    city = str(info.get("city") or "").strip()
    state = str(info.get("state") or "").strip()
    country = str(info.get("country") or "").strip()
    # Prefer RU-friendly country names for common cases
    country_ru = {
        "United States": "США",
        "Netherlands": "Нидерланды",
        "United Kingdom": "Великобритания",
        "Ireland": "Ирландия",
        "Cayman Islands": "Каймановы о-ва",
        "Taiwan": "Тайвань",
        "China": "Китай",
        "Israel": "Израиль",
        "Germany": "Германия",
        "France": "Франция",
        "Canada": "Канада",
        "Japan": "Япония",
        "South Korea": "Южная Корея",
        "Singapore": "Сингапур",
    }.get(country, country)
    bits = [x for x in (city, state, country_ru) if x]
    return ", ".join(bits)[:160]


def _yahoo_listing_origin_ru(info: Dict[str, Any], summary: str = "") -> str:
    """IPO / first trade year + optional founded phrase from summary."""
    parts: List[str] = []
    epoch = info.get("firstTradeDateEpochUtc") or info.get("firstTradeDateEpoch")
    try:
        if epoch is not None:
            from datetime import datetime, timezone

            yr = datetime.fromtimestamp(int(epoch), tz=timezone.utc).year
            if 1900 <= yr <= 2100:
                parts.append(f"листинг с {yr} (Yahoo first trade)")
    except (TypeError, ValueError, OSError):
        pass
    # "founded in 1975" / «основана в 1975»
    import re

    m = re.search(r"founded in (\d{4})", summary or "", flags=re.I)
    if m:
        parts.append(f"основана в {m.group(1)}")
    return " · ".join(parts)[:240]


def suggest_fundament_from_yfinance(sym: str) -> Dict[str, Any]:
    """Gradual autofill draft for Fundament tab (Yahoo info). Does not persist.

    Auto (button): exchange, HQ, listing year, metrics, margin %, financing numbers,
    tagline draft from longBusinessSummary.
    Manual (Nastya): key_clients, pluses/risks, sector/layer on Profile, narrative rewrite.
    """
    u = str(sym or "").strip().upper()
    if not u:
        raise ValueError("ticker required")
    try:
        import yfinance as yf
    except Exception as e:
        raise RuntimeError(f"yfinance unavailable: {e}") from e

    ticker = yf.Ticker(u)
    info: Dict[str, Any] = {}
    try:
        raw = ticker.info
        if isinstance(raw, dict):
            info = raw
    except Exception as e:
        logger.debug("yfinance info %s: %s", u, e)
        info = {}

    if not info:
        mat = _latest_earnings_material_url(u)
        fundament = _normalize_fundament({"filing_url": mat.get("filing_url") or ""})
        filled = ["filing_url"] if fundament.get("filing_url") else []
        return {
            "ticker": u,
            "source": "yfinance+earnings" if filled else "yfinance",
            "fundament": fundament,
            "filled": filled,
            "nastya_only": ["key_clients_ru", "pluses", "risks", "profile.sector"],
            "note": (
                "Yahoo не отдал info — заполните вручную"
                + ("; filing_url из earnings_material" if filled else "")
            ),
            "earnings_material": mat if filled else {},
        }

    short = str(info.get("shortName") or info.get("longName") or u).strip()
    sector = str(info.get("sector") or "").strip()
    industry = str(info.get("industry") or "").strip()
    summary = str(info.get("longBusinessSummary") or "").strip()
    bits = [short]
    if sector or industry:
        bits.append(" · ".join(x for x in (sector, industry) if x))
    if summary:
        # first sentence only, keep short for tagline draft
        cut = summary.split(". ")[0].strip()
        if cut and not cut.endswith("."):
            cut += "."
        if len(cut) > 280:
            cut = cut[:277].rstrip() + "…"
        bits.append(cut)
    tagline = " ".join(bits)[:500]

    exchange = _yahoo_exchange_label(info)
    hq_ru = _yahoo_hq_ru(info)
    listing_origin_ru = _yahoo_listing_origin_ru(info, summary)

    cash = _fmt_usd_compact(info.get("totalCash"))
    fcf_annual, fcf_end = _yfinance_annual_fcf(ticker)
    fcf_note = "Yahoo annual FY cashflow · сравнимо с SEC 10-K FY"
    if fcf_annual is not None:
        fcf = _fmt_usd_compact(fcf_annual)
        if fcf_end:
            fcf_note = (
                f"Yahoo annual FY {fcf_end[:7]} · сравнимо с SEC 10-K FY "
                f"(не с 10-Q YTD)"
            )
    else:
        fcf = _fmt_usd_compact(info.get("freeCashflow"))
        fcf_note = "Yahoo info.freeCashflow (часто TTM/короткое окно ≠ FY)"
    debt_val, debt_end, debt_note = _yfinance_interest_bearing_debt(ticker)
    if debt_val is not None:
        debt = _fmt_usd_compact(debt_val)
        if debt_end:
            debt_note = (
                f"{debt_note} на {debt_end[:7]} · сравнимо с SEC LT debt "
                f"того же периода"
            )
        else:
            debt_note = f"{debt_note} · сравнимо с SEC LT debt того же периода"
    else:
        debt = _fmt_usd_compact(info.get("totalDebt"))
        debt_note = "Yahoo info.totalDebt (часто завышен / +leases)"
    cr = info.get("currentRatio")
    try:
        cr_f = float(cr) if cr is not None else None
    except (TypeError, ValueError):
        cr_f = None
    reserve = f"current ratio {cr_f:.2f}" if cr_f is not None else None

    metrics: List[Dict[str, str]] = []
    filled: List[str] = []
    if exchange:
        filled.append("exchange")
    if hq_ru:
        filled.append("hq_ru")
    if listing_origin_ru:
        filled.append("listing_origin_ru")
    if cash:
        metrics.append({"k": "КЭШ", "v": cash, "note": "Yahoo totalCash (cash+STI) · сравнимо с SEC cash+STI", "tone": "good"})
        filled.append("cash")
    else:
        metrics.append({"k": "КЭШ", "v": "—", "note": "", "tone": ""})
    if fcf:
        tone = "bad" if str(fcf).startswith("-") else "good"
        metrics.append({"k": "FCF", "v": fcf, "note": fcf_note, "tone": tone})
        filled.append("fcf")
    else:
        metrics.append({"k": "FCF", "v": "—", "note": "", "tone": ""})
    if debt:
        metrics.append({"k": "Прямой долг", "v": debt, "note": debt_note, "tone": ""})
        filled.append("debt")
    else:
        metrics.append({"k": "Прямой долг", "v": "—", "note": "", "tone": ""})
    if reserve:
        tone = "good" if cr_f is not None and cr_f >= 1.2 else ("mid" if cr_f is not None else "")
        metrics.append({"k": "Запас прочности", "v": reserve, "note": "Yahoo currentRatio", "tone": tone})
        filled.append("reserve")
    else:
        metrics.append({"k": "Запас прочности", "v": "—", "note": "", "tone": ""})

    gm = _fmt_pct_ratio(info.get("grossMargins"))
    pm = _fmt_pct_ratio(info.get("profitMargins"))
    om = _fmt_pct_ratio(info.get("operatingMargins"))
    margin_bits = []
    if gm:
        margin_bits.append(f"gross {gm}")
    if om:
        margin_bits.append(f"oper {om}")
    if pm:
        margin_bits.append(f"net {pm}")
    margin_ru = ("Маржа (Yahoo): " + " · ".join(margin_bits)) if margin_bits else ""
    if margin_bits:
        filled.append("margin")

    dte = info.get("debtToEquity")
    try:
        dte_f = float(dte) if dte is not None else None
    except (TypeError, ValueError):
        dte_f = None
    fin_bits = []
    if cash:
        fin_bits.append(f"кэш {cash}")
    if debt:
        fin_bits.append(f"долг {debt}")
    if dte_f is not None:
        fin_bits.append(f"D/E {dte_f:.1f}")
    financing_ru = (
        ("Yahoo черновик цифр: " + " · ".join(fin_bits) + ". Настя: кто оплачивает рост / качество долга / точка разрыва.")
        if fin_bits
        else ""
    )
    if fin_bits:
        filled.append("financing")
    if tagline:
        filled.append("tagline")

    # Reuse earnings intelligence materials + last LLM extract (passport draft only).
    mat = _latest_earnings_material_url(u)
    filing_url = mat.get("filing_url") or ""
    earnings_bits: List[str] = []
    try:
        from sqlalchemy import create_engine

        from config_loader import get_database_url
        from services.earnings_intelligence_api import get_event_brief_payload

        eng = create_engine(get_database_url(), pool_pre_ping=True)
        try:
            brief = get_event_brief_payload(eng, symbol=u)
        finally:
            eng.dispose()
        if isinstance(brief, dict) and brief.get("status") in ("ok", "partial"):
            g = brief.get("guidance") if isinstance(brief.get("guidance"), dict) else {}
            m_out = str(g.get("margin_outlook") or "").strip()
            m_sig = _join_signal_list(brief.get("margin_pressure_signals"))
            if m_out or m_sig:
                extra = " · ".join(x for x in (m_out, m_sig) if x)
                if margin_ru:
                    margin_ru = f"{margin_ru} · IR/SEC: {extra}"[:500]
                else:
                    margin_ru = f"IR/SEC: {extra}"[:500]
                if "margin" not in filled:
                    filled.append("margin")
                earnings_bits.append("margin")
            capex_notes = str(brief.get("capex_notes") or "").strip()
            capex_out = str(g.get("capex_outlook") or "").strip()
            capex_line = " · ".join(x for x in (capex_out, capex_notes) if x)
            if capex_line:
                if financing_ru:
                    financing_ru = f"{financing_ru} CapEx/IR: {capex_line}"[:500]
                else:
                    financing_ru = (
                        f"IR/SEC CapEx: {capex_line}. Настя: кто оплачивает рост / точка разрыва."
                    )[:500]
                if "financing" not in filled:
                    filled.append("financing")
                earnings_bits.append("capex")
    except Exception as e:
        logger.debug("fundament earnings enrich %s: %s", u, e)

    if filing_url:
        filled.append("filing_url")
        earnings_bits.append(mat.get("material_type") or "material")

    fundament = _normalize_fundament(
        {
            "exchange": exchange,
            "hq_ru": hq_ru,
            "listing_origin_ru": listing_origin_ru,
            "key_clients_ru": "",  # Nastya — Yahoo rarely has reliable client list
            "tagline": tagline,
            "metrics": metrics,
            "margin_ru": margin_ru,
            "financing_ru": financing_ru,
            "filing_url": filing_url,
            "pluses": [],
            "risks": [],
        }
    )
    note = (
        "кнопка: Yahoo цифры + filing_url/маржа·CapEx из earnings materials (IR/SEC→LLM) · "
        "Настя: клиенты, плюсы/риски, слой на Профиле, правка сути/финансирования · сохраните OK"
    )
    if earnings_bits:
        note = f"{note} · earnings: {', '.join(earnings_bits)}"
    return {
        "ticker": u,
        "source": "yfinance+earnings",
        "name": str(info.get("longName") or info.get("shortName") or "").strip()[:120],
        "yahoo_sector": sector,
        "yahoo_industry": industry,
        "fundament": fundament,
        "filled": filled,
        "nastya_only": [
            "key_clients_ru",
            "pluses",
            "risks",
            "profile.sector_layer",
            "tagline_rewrite",
            "financing_narrative",
        ],
        "note": note,
        "earnings_material": mat if filing_url else {},
    }


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


def _vix_env_thresholds() -> tuple[float, float]:
    """ok below lo, mid until hi, bad at/above hi. TZ mock: норма <20."""
    try:
        from config_loader import get_config_value

        lo = float(get_config_value("NOTEBOOK_VIX_OK_BELOW", "20") or 20)
        hi = float(get_config_value("NOTEBOOK_VIX_BAD_AT", "25") or 25)
    except Exception:
        lo, hi = 20.0, 25.0
    if hi <= lo:
        hi = lo + 5.0
    return lo, hi


def fetch_vix_snapshot() -> Optional[Dict[str, Any]]:
    """Live ^VIX from quotes/yfinance for Environment Check."""
    px = fetch_closes(["^VIX"]).get("^VIX") or fetch_closes(["VIX"]).get("VIX")
    if not px or px.get("close") is None:
        return None
    val = float(px["close"])
    lo, hi = _vix_env_thresholds()
    if val < lo:
        state = "ok"
        label = f"норма · {val:.1f} (<{lo:g})"
    elif val < hi:
        state = "mid"
        label = f"повышен · {val:.1f} ({lo:g}–{hi:g})"
    else:
        state = "bad"
        label = f"высокий · {val:.1f} (≥{hi:g})"
    chg_s, _ = _fmt_chg(px)
    if chg_s and chg_s != "—":
        label = f"{label} · дн. {chg_s}"
    return {
        "lbl": "VIX",
        "st": label,
        "state": state,
        "live": True,
        "source": f"live · {px.get('source') or 'quotes'}",
        "value": val,
        "asof": px.get("asof"),
        "metric": "vix",
    }


def fetch_ndx_snapshot() -> Optional[Dict[str, Any]]:
    """Live Nasdaq-100 (^NDX, QQQ fallback) — index context for Environment Check."""
    bundle = fetch_closes(["^NDX", "QQQ"])
    px = bundle.get("^NDX") or bundle.get("QQQ")
    if not px or px.get("close") is None:
        return None
    val = float(px["close"])
    chg_pct = px.get("chg_pct")
    try:
        from config_loader import get_config_value

        soft = float(get_config_value("NOTEBOOK_NDX_OK_ABS_PCT", "1.2") or 1.2)
        hard = float(get_config_value("NOTEBOOK_NDX_BAD_ABS_PCT", "2.5") or 2.5)
    except Exception:
        soft, hard = 1.2, 2.5
    if hard <= soft:
        hard = soft + 1.0

    if chg_pct is None:
        state = "ok"
        move = "нет дн. %"
    else:
        abs_m = abs(float(chg_pct))
        if abs_m < soft:
            state = "ok"
        elif abs_m < hard:
            state = "mid"
        else:
            state = "bad"
        sign = "+" if float(chg_pct) >= 0 else ""
        move = f"{sign}{float(chg_pct):.2f}%"

    src = px.get("source") or "quotes"
    label = f"{val:,.0f} · дн. {move}"
    return {
        "lbl": "NDX",
        "st": label,
        "state": state,
        "live": True,
        "source": f"live · {src}",
        "value": val,
        "chg_pct": chg_pct,
        "asof": px.get("asof"),
        "metric": "ndx",
    }


def fetch_oil_snapshot() -> Optional[Dict[str, Any]]:
    """Live crude (CL=F) as geopolitics proxy: spike up = stress."""
    try:
        from config_loader import get_config_value

        oil_t = (get_config_value("NOTEBOOK_OIL_TICKER", "CL=F") or "CL=F").strip() or "CL=F"
        soft = float(get_config_value("NOTEBOOK_OIL_STRESS_MID_PCT", "2.0") or 2.0)
        hard = float(get_config_value("NOTEBOOK_OIL_STRESS_BAD_PCT", "3.5") or 3.5)
    except Exception:
        oil_t, soft, hard = "CL=F", 2.0, 3.5
    if hard <= soft:
        hard = soft + 1.0

    wanted = [oil_t]
    if oil_t.upper() != "BZ=F":
        wanted.append("BZ=F")
    bundle = fetch_closes(wanted)
    px = bundle.get(oil_t.upper())
    used = oil_t
    if (not px or px.get("close") is None) and "BZ=F" in bundle:
        px = bundle.get("BZ=F")
        used = "BZ=F"
    if not px or px.get("close") is None:
        return None
    val = float(px["close"])
    chg_pct = px.get("chg_pct")
    # Geopolitics: oil UP is stress; oil down is usually ok.
    if chg_pct is None:
        state = "ok"
        move = "нет дн. %"
    else:
        m = float(chg_pct)
        if m >= hard:
            state = "bad"
        elif m >= soft:
            state = "mid"
        else:
            state = "ok"
        sign = "+" if m >= 0 else ""
        move = f"{sign}{m:.2f}%"

    label = f"${val:.2f} · дн. {move}"
    return {
        "lbl": "Нефть (геополитика)",
        "st": label,
        "state": state,
        "live": True,
        "source": f"live · {used} · {px.get('source') or 'quotes'}",
        "value": val,
        "chg_pct": chg_pct,
        "asof": px.get("asof"),
        "metric": "oil",
    }


def _fed_hint_from_digest(digest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Soft hint for Fed rhetoric from morning digest macro bucket (not a full NLP classifier)."""
    rows = digest.get("macro") if isinstance(digest.get("macro"), list) else []
    hits = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        blob = " ".join(
            str(r.get(k) or "") for k in ("sym", "text", "tac", "src")
        ).lower()
        if any(k in blob for k in ("фрс", "fed", "fomc", "powell", "ястреб", "hawk")):
            hits.append(r)
    if not hits:
        return None
    text = str(hits[0].get("text") or hits[0].get("sym") or "")[:160]
    hawk = any(k in text.lower() for k in ("ястреб", "hawk", "ужесточ", "hike", "higher for longer"))
    dove = any(k in text.lower() for k in ("голуб", "dove", "смягч", "cut", "снижен"))
    if hawk and not dove:
        state, st = "mid", f"по дайджесту · ястребиный тон — {text[:80]}"
    elif dove and not hawk:
        state, st = "ok", f"по дайджесту · мягче — {text[:80]}"
    else:
        state, st = "mid", f"по дайджесту · смотреть — {text[:80]}"
    return {
        "lbl": "Риторика ФРС",
        "st": st,
        "state": state,
        "live": True,
        "source": "live · макро-дайджест KB/LLM",
    }


def _is_live_env_lbl(lbl: str) -> Optional[str]:
    low = str(lbl or "").lower()
    if "vix" in low:
        return "vix"
    if "ndx" in low or "nasdaq" in low:
        return "ndx"
    if "нефт" in low or "oil" in low or "геопол" in low:
        return "oil"
    if "фрс" in low or "fed" in low:
        return "fed"
    return None


def apply_live_env_to_tickers(
    tickers: Dict[str, Any],
    *,
    digest: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Overlay live VIX / NDX / oil / FOMC (+ optional Fed digest hint) onto each ticker env list."""
    vix = fetch_vix_snapshot()
    ndx = fetch_ndx_snapshot()
    oil = fetch_oil_snapshot()
    fed = None
    try:
        from services.macro_events_calendar import fomc_env_snapshot

        fed = fomc_env_snapshot()
    except Exception as e:
        logger.debug("FOMC env snapshot skipped: %s", e)
        fed = None
    if fed is None:
        fed = _fed_hint_from_digest(digest or {}) if digest else None
    live_by_key = {"vix": vix, "ndx": ndx, "oil": oil, "fed": fed}

    out: Dict[str, Any] = {}
    for sym, row in (tickers or {}).items():
        if not isinstance(row, dict):
            continue
        d = dict(row)
        env = list(d.get("env") or []) if isinstance(d.get("env"), list) else []
        new_env: List[Dict[str, Any]] = []
        seen = {k: False for k in live_by_key}
        for e in env:
            if not isinstance(e, dict):
                continue
            e2 = dict(e)
            key = _is_live_env_lbl(str(e2.get("lbl") or ""))
            snap = live_by_key.get(key) if key else None
            if key and snap:
                e2.update(snap)
                seen[key] = True
            elif key == "fed" and not snap:
                # keep manual / placeholder handling below
                st = str(e2.get("st") or "").lower()
                placeholder = any(
                    p in st
                    for p in (
                        "обновлять вручную",
                        "авто из quotes",
                        "следить",
                        "tbd",
                        "вручную",
                    )
                ) and len(st) < 40
                e2.setdefault("live", False)
                e2.setdefault("source", "вручную / макро")
                if placeholder and e2.get("state") == "mid":
                    e2["state"] = "ok"
                    e2["st"] = "нет сигнала · ok/mid/bad на Вердикте"
            elif not key:
                st = str(e2.get("st") or "").lower()
                placeholder = any(
                    p in st
                    for p in (
                        "обновлять вручную",
                        "авто из quotes",
                        "следить",
                        "tbd",
                        "вручную",
                    )
                ) and len(st) < 40
                e2.setdefault("live", False)
                # PT cuts gate: manual only — not auto from StockAnalysis consensus.
                e2.setdefault("source", "вручную · PT cuts вне earnings")
                if placeholder and e2.get("state") == "mid":
                    e2["state"] = "ok"
                    e2["st"] = "нет волны cut PT · ok/mid/bad на Вердикте (таргеты)"
            new_env.append(e2)

        # Ensure live rows exist even if JSON template omitted them.
        missing: List[Dict[str, Any]] = []
        for key in ("vix", "ndx", "oil", "fed"):
            snap = live_by_key.get(key)
            if snap and not seen.get(key):
                missing.append(dict(snap))
        d["env"] = missing + new_env
        out[str(sym).upper()] = d
    return out


def build_notebook_payload(
    *,
    path: Optional[Path] = None,
    with_prices: bool = True,
    tickers_filter: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    data = load_notebook_data(path)
    tickers_raw = data.get("tickers") if isinstance(data.get("tickers"), dict) else {}
    # Overlay-only tickers (UI-added) must be present BEFORE price merge,
    # otherwise GOOGL/TSLA/… get cards with px=None.
    try:
        tickers_raw = apply_ticker_overrides(tickers_raw)
    except Exception as e:
        logger.debug("ticker overrides skipped: %s", e)

    if tickers_filter:
        wanted = set(_normalize_tickers(tickers_filter))
        tickers_raw = {k: v for k, v in tickers_raw.items() if str(k).upper() in wanted}

    prices: Dict[str, Dict[str, Any]] = {}
    if with_prices and tickers_raw:
        prices = fetch_closes(list(tickers_raw.keys()))

    tickers = merge_prices_into_tickers(tickers_raw, prices)
    groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}

    digest = data.get("digest") if isinstance(data.get("digest"), dict) else {}
    digest_pipeline: Dict[str, Any] = {}
    digest_snapshot_id = "latest"
    # Prefer latest pipeline output if present (local/notebook/digest_latest.json).
    try:
        from services.notebook_news_digest import load_latest_digest_pack

        pack = load_latest_digest_pack()
        if isinstance(pack, dict):
            live = pack.get("digest") if isinstance(pack.get("digest"), dict) else None
            if live and (live.get("signals") is not None or live.get("date")):
                digest = live
            if isinstance(pack.get("pipeline"), dict):
                digest_pipeline = dict(pack["pipeline"])
            digest_snapshot_id = str(pack.get("id") or "latest")
    except Exception as e:
        logger.debug("notebook digest overlay skipped: %s", e)

    if with_prices:
        try:
            tickers = apply_live_env_to_tickers(tickers, digest=digest)
        except Exception as e:
            logger.debug("live env overlay skipped: %s", e)

    vix_meta = None
    try:
        vix_meta = fetch_vix_snapshot()
    except Exception:
        vix_meta = None

    calendar: Dict[str, Any] = {"events": [], "days": 21, "asof_utc": "", "counts": {}}
    fomc_next = None
    try:
        from services.macro_events_calendar import build_macro_events, next_fomc_decision

        # Boot: FOMC (+ FRED if key). Earnings are heavier — UI loads via /api/notebook/calendar.
        calendar = build_macro_events(
            days=21, symbols=None, include_earnings=False, include_fred=True, include_fomc=True
        )
        fomc_next = next_fomc_decision()
    except Exception as e:
        logger.debug("macro calendar skipped: %s", e)
        calendar = {
            "events": [],
            "days": 21,
            "asof_utc": "",
            "counts": {},
            "errors": {"calendar": str(e)[:200]},
        }

    watchlist = data.get("watchlist") if isinstance(data.get("watchlist"), dict) else {}
    watchlist = dict(watchlist)
    try:
        from services.notebook_news_digest import watchlist_candidates_from_digest

        cands = watchlist_candidates_from_digest(digest)
        watchlist["candidates"] = cands
        watchlist["candidates_asof"] = str(digest.get("date") or "")[:40]
        watchlist["candidates_via"] = "morning digest · newtickers (SA/KB)"
    except Exception as e:
        logger.debug("watchlist candidates skipped: %s", e)
        watchlist.setdefault("candidates", [])

    news_sources: Dict[str, Any] = {}
    try:
        from services.notebook_news_digest import notebook_news_sources_catalog

        news_sources = notebook_news_sources_catalog(days=14, limit=80)
    except Exception as e:
        logger.debug("news sources catalog skipped: %s", e)
        news_sources = {"ingest_channels": [], "kb_sources_14d": [], "error": str(e)[:200]}

    return {
        "schema_version": int(data.get("schema_version") or SCHEMA_VERSION),
        "asof_label": data.get("asof_label") or "",
        "principle_ru": data.get("principle_ru") or "",
        "groups": groups,
        "tickers": tickers,
        "digest": digest,
        "digest_pipeline": digest_pipeline,
        "digest_snapshot_id": digest_snapshot_id,
        "digest_buckets": data.get("digest_buckets") if isinstance(data.get("digest_buckets"), list) else [],
        "watchlist": watchlist,
        "news_sources": news_sources,
        "prices": prices,
        "env_live": {"vix": vix_meta, "fomc": fomc_next},
        "calendar": calendar,
        "data_path": str(path or notebook_data_path()),
    }
