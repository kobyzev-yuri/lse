"""Public CME FedWatch-style probabilities (no paid CME API).

Uses the open-source ``cme-fedwatch`` calculator over free CME settlements + FRED EFFR.
Not affiliated with CME; may differ slightly from QuikStrike live mid prices.

Public tool (manual): https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

FEDWATCH_TOOL_URL = (
    "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
)

_CACHE: Dict[str, Any] = {"t": 0.0, "payload": None}
_CACHE_TTL_SEC = 3600.0

_RANGE_RE = re.compile(r"([\d.]+)\s*%?\s*-\s*([\d.]+)\s*%?")


def _parse_range_lo(label: str) -> Optional[float]:
    s = str(label or "").strip()
    m = _RANGE_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def bucket_move_probabilities(
    current_target: str,
    probabilities: Dict[str, Any],
) -> Dict[str, float]:
    """Map FedWatch range columns → cut / hold / hike % (sum ≈ 100)."""
    cur_lo = _parse_range_lo(current_target)
    cut = hold = hike = 0.0
    for label, raw in (probabilities or {}).items():
        try:
            pct = float(raw)
        except (TypeError, ValueError):
            continue
        lo = _parse_range_lo(str(label))
        if lo is None or cur_lo is None:
            continue
        # 12.5bp tolerance for float labels
        if abs(lo - cur_lo) < 0.05:
            hold += pct
        elif lo < cur_lo:
            cut += pct
        else:
            hike += pct
    return {
        "cut_pct": round(cut, 1),
        "hold_pct": round(hold, 1),
        "hike_pct": round(hike, 1),
    }


def _format_st(buckets: Dict[str, float], *, meeting_date: str) -> str:
    cut = buckets.get("cut_pct") or 0.0
    hold = buckets.get("hold_pct") or 0.0
    hike = buckets.get("hike_pct") or 0.0
    parts = [f"hold {hold:.0f}%"]
    if cut >= 1:
        parts.append(f"cut {cut:.0f}%")
    if hike >= 1:
        parts.append(f"hike {hike:.0f}%")
    return f"FedWatch {meeting_date}: " + " · ".join(parts)


def fetch_fedwatch_next(*, force: bool = False) -> Optional[Dict[str, Any]]:
    """Next meeting probabilities via public CME settlements + FRED (cme-fedwatch)."""
    now = time.time()
    if not force and _CACHE.get("payload") is not None and (now - float(_CACHE.get("t") or 0)) < _CACHE_TTL_SEC:
        return dict(_CACHE["payload"])

    try:
        from cme_fedwatch import get_probabilities
    except Exception as e:
        logger.info("cme-fedwatch not available: %s", e)
        return None

    try:
        raw = get_probabilities("next")
    except Exception as e:
        logger.warning("FedWatch public fetch failed: %s", e)
        return None

    if not isinstance(raw, dict):
        return None
    meetings = raw.get("meetings") if isinstance(raw.get("meetings"), list) else []
    if not meetings:
        return None
    m0 = meetings[0] if isinstance(meetings[0], dict) else {}
    probs = m0.get("probabilities") if isinstance(m0.get("probabilities"), dict) else {}
    target = str(raw.get("current_target") or "")
    buckets = bucket_move_probabilities(target, probs)
    meeting_date = str(m0.get("date") or "")
    payload: Dict[str, Any] = {
        "effr": raw.get("effr"),
        "current_target": target,
        "meeting_date": meeting_date,
        "contract": m0.get("contract"),
        "probabilities": {str(k): float(v) for k, v in probs.items() if v is not None},
        "buckets": buckets,
        "st_short": _format_st(buckets, meeting_date=meeting_date or "?"),
        "source": "cme-fedwatch · public CME settlements + FRED",
        "url": FEDWATCH_TOOL_URL,
        "schedule_status": raw.get("schedule_status"),
    }
    _CACHE["t"] = now
    _CACHE["payload"] = payload
    return dict(payload)


def enrich_fomc_env_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Append FedWatch line into FOMC env snapshot (best-effort)."""
    out = dict(row or {})
    fw = fetch_fedwatch_next()
    if not fw:
        out.setdefault("fedwatch", None)
        return out

    out["fedwatch"] = fw
    base_st = str(out.get("st") or "").strip()
    fw_st = str(fw.get("st_short") or "").strip()
    if fw_st:
        out["st"] = f"{base_st} · {fw_st}" if base_st else fw_st
    # Uncertainty: max bucket < 55% near a meeting → keep at least mid
    buckets = fw.get("buckets") if isinstance(fw.get("buckets"), dict) else {}
    top = max(
        float(buckets.get("cut_pct") or 0),
        float(buckets.get("hold_pct") or 0),
        float(buckets.get("hike_pct") or 0),
    )
    fomc = out.get("fomc") if isinstance(out.get("fomc"), dict) else {}
    days = fomc.get("days_until")
    try:
        days_i = int(days) if days is not None else None
    except (TypeError, ValueError):
        days_i = None
    if days_i is not None and days_i <= 10 and top < 55 and out.get("state") == "ok":
        out["state"] = "mid"
    src = str(out.get("source") or "")
    if "FedWatch" not in src:
        out["source"] = (src + " + FedWatch public").strip(" +") if src else "live · FedWatch public"
    return out
