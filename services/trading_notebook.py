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


def apply_live_env_to_tickers(
    tickers: Dict[str, Any],
    *,
    digest: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Overlay live VIX (+ optional Fed hint from digest) onto each ticker env list."""
    vix = fetch_vix_snapshot()
    fed = _fed_hint_from_digest(digest or {}) if digest else None
    out: Dict[str, Any] = {}
    for sym, row in (tickers or {}).items():
        if not isinstance(row, dict):
            continue
        d = dict(row)
        env = list(d.get("env") or []) if isinstance(d.get("env"), list) else []
        new_env: List[Dict[str, Any]] = []
        seen_vix = seen_fed = False
        for e in env:
            if not isinstance(e, dict):
                continue
            e2 = dict(e)
            lbl = str(e2.get("lbl") or "")
            low = lbl.lower()
            if vix and "vix" in low:
                e2.update(vix)
                seen_vix = True
            elif fed and ("фрс" in low or "fed" in low):
                e2.update(fed)
                seen_fed = True
            else:
                # Placeholder stubs must not force yellow Environment gate.
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
                e2.setdefault("source", "заглушка · вручную / новости PT")
                if placeholder and e2.get("state") == "mid":
                    e2["state"] = "ok"
                    e2["st"] = (str(e2.get("st") or "нет сигнала") + " · ждать ручного апдейта").strip()
            new_env.append(e2)
        if vix and not seen_vix:
            new_env.insert(0, dict(vix))
        if fed and not seen_fed:
            # after VIX
            idx = 1 if new_env and "vix" in str(new_env[0].get("lbl") or "").lower() else 0
            new_env.insert(idx, dict(fed))
        d["env"] = new_env
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
        "env_live": {"vix": vix_meta},
        "data_path": str(path or notebook_data_path()),
    }
