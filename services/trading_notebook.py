"""Рабочая тетрадка Насти: ручные уровни + справочный Close из quotes/yfinance."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

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
    """Normalize env row label to a stable override key; VIX is never overridable."""
    low = str(lbl or "").lower()
    if "vix" in low:
        return None
    if "фрс" in low or "fed" in low:
        return "fed"
    if "таргет" in low or "pt" in low:
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
    """Merge local/notebook/ticker_overrides.json onto ticker cards (signals, levels, env)."""
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
            if patch.get("horizon") is not None:
                d["horizon"] = str(patch.get("horizon") or "")[:160]
                d["profile_override"] = True
            if isinstance(patch.get("profile"), dict):
                base_pf = dict(d.get("profile") or {}) if isinstance(d.get("profile"), dict) else {}
                for k, v in patch["profile"].items():
                    base_pf[k] = v
                d["profile"] = base_pf
                d["profile_override"] = True
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
        # Keep buy/sell trigger labels in sync with effective levels.
        if isinstance(d.get("levels"), dict) and isinstance(d.get("triggers"), list):
            d["triggers"] = _merge_triggers(d["triggers"], [], levels=d.get("levels"))
        out[u] = d
    return out


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
                raise ValueError("VIX is live-only and cannot be overridden")
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

    try:
        tickers = apply_ticker_overrides(tickers)
    except Exception as e:
        logger.debug("ticker overrides skipped: %s", e)

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
