"""
Relative rank of a ticker vs peers in its shape-cluster (shadow / context fields).

Uses last_portfolio_shape_clusters.json cache when present; otherwise empty status.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _load_shape_map() -> Optional[Dict[str, Any]]:
    try:
        from services.portfolio_shape_clusters import default_shape_clusters_path

        path = default_shape_clusters_path()
    except Exception:
        path = Path("/app/logs/ml/ml_data_quality/last_portfolio_shape_clusters.json")
        if not path.is_file():
            root = Path(__file__).resolve().parents[1]
            path = root / "local" / "logs" / "ml_data_quality" / "last_portfolio_shape_clusters.json"
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.debug("shape map load: %s", e)
        return None


def _cluster_members_for_ticker(report: Dict[str, Any], ticker: str) -> List[str]:
    t = (ticker or "").strip().upper()
    clusters = report.get("clusters") or report.get("groups") or []
    if isinstance(clusters, dict):
        clusters = list(clusters.values())
    for cl in clusters:
        if not isinstance(cl, dict):
            continue
        members = cl.get("tickers") or cl.get("members") or []
        names: List[str] = []
        for m in members:
            if isinstance(m, str):
                names.append(m.strip().upper())
            elif isinstance(m, dict):
                names.append(str(m.get("ticker") or "").strip().upper())
        if t in names:
            return [x for x in names if x]
    # flat membership map
    mem = report.get("ticker_to_cluster") or report.get("membership") or {}
    if isinstance(mem, dict) and t in mem:
        cid = mem[t]
        for cl in clusters:
            if isinstance(cl, dict) and (cl.get("id") == cid or cl.get("cluster_id") == cid):
                members = cl.get("tickers") or cl.get("members") or []
                out = []
                for m in members:
                    if isinstance(m, str):
                        out.append(m.strip().upper())
                    elif isinstance(m, dict) and m.get("ticker"):
                        out.append(str(m["ticker"]).strip().upper())
                return out
    return []


def portfolio_peer_relative_rank(
    ticker: str,
    *,
    ret_20d_pct: Optional[float] = None,
    engine=None,
) -> Dict[str, Any]:
    """
    Shadow fields: rank 1 = best ret_20d in cluster.
    Does not block BUY.
    """
    t = (ticker or "").strip().upper()
    out: Dict[str, Any] = {
        "portfolio_peer_status": "unavailable",
        "portfolio_peer_rank": None,
        "portfolio_peer_n": None,
        "portfolio_peer_ret_vs_medoid_pct": None,
        "portfolio_peer_cluster_id": None,
    }
    report = _load_shape_map()
    if not report:
        out["portfolio_peer_note_ru"] = "Нет кэша shape-clusters"
        return out

    members = _cluster_members_for_ticker(report, t)
    if len(members) < 2:
        out["portfolio_peer_status"] = "singleton_or_missing"
        out["portfolio_peer_n"] = len(members) or None
        out["portfolio_peer_note_ru"] = "Нет peers в кластере"
        return out

    # Collect ret_20d for members
    from services.portfolio_trend_regime import portfolio_trend_regime_snapshot

    scored: List[tuple[str, float]] = []
    medoid = None
    # try medoid from cluster record
    clusters = report.get("clusters") or report.get("groups") or []
    if isinstance(clusters, dict):
        clusters = list(clusters.values())
    for cl in clusters:
        if not isinstance(cl, dict):
            continue
        mt = cl.get("medoid") or cl.get("medoid_ticker")
        mems = _cluster_members_for_ticker(report, t)
        if t in mems and mt:
            medoid = str(mt).strip().upper()
            out["portfolio_peer_cluster_id"] = cl.get("id") or cl.get("cluster_id")
            break

    for m in members:
        if m == t and ret_20d_pct is not None:
            rv = _f(ret_20d_pct)
        else:
            snap = portfolio_trend_regime_snapshot(m, engine=engine)
            rv = _f(snap.get("portfolio_trend_ret_20d_pct"))
        if rv is not None:
            scored.append((m, rv))

    if len(scored) < 2:
        out["portfolio_peer_status"] = "insufficient_rets"
        out["portfolio_peer_n"] = len(members)
        return out

    scored.sort(key=lambda x: x[1], reverse=True)
    rank = next((i + 1 for i, (name, _) in enumerate(scored) if name == t), None)
    my_ret = next((r for name, r in scored if name == t), _f(ret_20d_pct))
    med_ret = None
    if medoid:
        med_ret = next((r for name, r in scored if name == medoid), None)
    elif scored:
        med_ret = scored[len(scored) // 2][1]

    vs_med = None
    if my_ret is not None and med_ret is not None:
        vs_med = round(my_ret - med_ret, 3)

    out.update(
        {
            "portfolio_peer_status": "ok",
            "portfolio_peer_rank": rank,
            "portfolio_peer_n": len(scored),
            "portfolio_peer_ret_vs_medoid_pct": vs_med,
            "portfolio_peer_medoid": medoid,
            "portfolio_peer_note_ru": (
                f"Rank {rank}/{len(scored)} по ret_20d в shape-cluster (shadow)"
                if rank
                else "Peer rank shadow"
            ),
        }
    )
    return out
