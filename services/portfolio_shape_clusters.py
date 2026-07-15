"""
Cluster tickers by ~6m chart *shape* (normalized price path).

v1 metric: correlation of close/close[0] on a common trading calendar.
Number of groups = whatever the threshold yields (hierarchical or components).
Later modes (log-ret, sector, earnings) can plug in as `mode=`.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd


def fetch_close_series(
    engine,
    tickers: Sequence[str],
    *,
    trading_days: int = 126,
) -> Dict[str, pd.Series]:
    """Oldest→newest close series keyed by ticker (DatetimeIndex)."""
    from sqlalchemy import text

    out: Dict[str, pd.Series] = {}
    lim = int(trading_days) + 5
    with engine.connect() as conn:
        for raw in tickers:
            t = str(raw).strip().upper()
            if not t:
                continue
            rows = conn.execute(
                text(
                    """
                    SELECT date, close FROM quotes
                    WHERE ticker = :t
                    ORDER BY date DESC
                    LIMIT :n
                    """
                ),
                {"t": t, "n": lim},
            ).fetchall()
            if not rows:
                continue
            dates = []
            closes = []
            for r in reversed(rows):
                try:
                    c = float(r[1])
                except (TypeError, ValueError):
                    continue
                if not (c > 0 and math.isfinite(c)):
                    continue
                d = r[0]
                dates.append(pd.Timestamp(d))
                closes.append(c)
            if len(closes) < max(40, trading_days // 3):
                continue
            # keep last trading_days+1 points if longer
            if len(closes) > trading_days + 1:
                dates = dates[-(trading_days + 1) :]
                closes = closes[-(trading_days + 1) :]
            out[t] = pd.Series(closes, index=pd.DatetimeIndex(dates), name=t)
    return out


def normalize_paths(series_map: Dict[str, pd.Series]) -> pd.DataFrame:
    """Align on inner join of dates; columns = close / first close."""
    if not series_map:
        return pd.DataFrame()
    df = pd.DataFrame(series_map).sort_index()
    df = df.dropna(how="any")
    if df.empty or len(df) < 20:
        # fallback: forward-fill then drop remaining NaN rows with enough coverage
        df = pd.DataFrame(series_map).sort_index().ffill().bfill()
        df = df.dropna(how="any")
    if df.empty:
        return df
    first = df.iloc[0]
    norm = df.divide(first.replace(0, np.nan))
    return norm.dropna(axis=1, how="any")


def correlation_matrix(norm: pd.DataFrame) -> pd.DataFrame:
    if norm.shape[1] < 2 or norm.shape[0] < 10:
        return pd.DataFrame()
    return norm.corr(method="pearson")


def _clusters_from_corr_threshold(corr: pd.DataFrame, corr_min: float) -> List[List[str]]:
    """Connected components: edge if corr >= corr_min."""
    tickers = [str(c) for c in corr.columns]
    parent = {t: t for t in tickers}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    n = len(tickers)
    thr = float(corr_min)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = tickers[i], tickers[j]
            try:
                v = float(corr.loc[a, b])
            except Exception:
                continue
            if math.isfinite(v) and v >= thr:
                union(a, b)

    buckets: Dict[str, List[str]] = {}
    for t in tickers:
        buckets.setdefault(find(t), []).append(t)
    groups = [sorted(v) for v in buckets.values()]
    groups.sort(key=lambda g: (-len(g), g[0]))
    return groups


def _clusters_hierarchical(corr: pd.DataFrame, distance_threshold: float) -> List[List[str]]:
    """Average-linkage on distance=1-corr; needs scipy."""
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import squareform

    tickers = [str(c) for c in corr.columns]
    mat = corr.reindex(index=tickers, columns=tickers).to_numpy(dtype=float)
    np.fill_diagonal(mat, 1.0)
    dist = np.clip(1.0 - mat, 0.0, 2.0)
    np.fill_diagonal(dist, 0.0)
    # numerical symmetry
    dist = (dist + dist.T) / 2.0
    condensed = squareform(dist, checks=False)
    z = linkage(condensed, method="average")
    labels = fcluster(z, t=float(distance_threshold), criterion="distance")
    buckets: Dict[int, List[str]] = {}
    for t, lab in zip(tickers, labels):
        buckets.setdefault(int(lab), []).append(t)
    groups = [sorted(v) for v in buckets.values()]
    groups.sort(key=lambda g: (-len(g), g[0]))
    return groups


def _medoid(group: Sequence[str], corr: pd.DataFrame) -> str:
    if len(group) == 1:
        return group[0]
    best, best_s = group[0], -1e9
    for a in group:
        s = 0.0
        for b in group:
            if a == b:
                continue
            try:
                s += float(corr.loc[a, b])
            except Exception:
                pass
        if s > best_s:
            best_s, best = s, a
    return best


def _pair_examples(group: Sequence[str], corr: pd.DataFrame) -> List[Dict[str, Any]]:
    if len(group) < 2:
        return []
    pairs: List[Tuple[float, str, str]] = []
    g = list(group)
    for i in range(len(g)):
        for j in range(i + 1, len(g)):
            a, b = g[i], g[j]
            try:
                v = float(corr.loc[a, b])
            except Exception:
                continue
            if math.isfinite(v):
                pairs.append((v, a, b))
    pairs.sort(reverse=True)
    return [{"a": a, "b": b, "corr": round(v, 3)} for v, a, b in pairs[:3]]


def build_shape_clusters(
    engine,
    tickers: Sequence[str],
    *,
    lookback_trading_days: int = 126,
    mode: str = "shape",
    method: str = "components",
    corr_min: float = 0.75,
    distance_threshold: float = 0.35,
) -> Dict[str, Any]:
    """
    mode=shape: corr of normalized prices (path similarity).
    method=components: union-find on corr>=corr_min (N groups free).
    method=hierarchical: scipy average linkage cut on 1-corr.
    """
    mode_u = (mode or "shape").strip().lower()
    method_u = (method or "components").strip().lower()
    series = fetch_close_series(engine, tickers, trading_days=int(lookback_trading_days))
    missing = [str(t).upper() for t in tickers if str(t).strip().upper() not in series]

    if mode_u != "shape":
        # stub for later logret / sector — currently same as shape path
        pass

    norm = normalize_paths(series)
    corr = correlation_matrix(norm)
    if corr.empty:
        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "mode": mode_u,
            "method": method_u,
            "lookback_trading_days": int(lookback_trading_days),
            "corr_min": float(corr_min),
            "distance_threshold": float(distance_threshold),
            "n_tickers_ok": 0,
            "missing_or_short": missing,
            "clusters": [],
            "note_ru": "Недостаточно overlapping quotes для кластеризации.",
        }

    if method_u == "hierarchical":
        try:
            groups = _clusters_hierarchical(corr, float(distance_threshold))
        except Exception:
            groups = _clusters_from_corr_threshold(corr, float(corr_min))
            method_u = "components_fallback"
    else:
        groups = _clusters_from_corr_threshold(corr, float(corr_min))

    clusters: List[Dict[str, Any]] = []
    for i, g in enumerate(groups, start=1):
        med = _medoid(g, corr)
        clusters.append(
            {
                "cluster_id": i,
                "size": len(g),
                "tickers": g,
                "medoid": med,
                "label": f"C{i} · {med}" + (f" +{len(g)-1}" if len(g) > 1 else " (solo)"),
                "strong_pairs": _pair_examples(g, corr),
            }
        )

    # top global pairs for sanity (LRCX-KLAC should rank high)
    top_pairs: List[Dict[str, Any]] = []
    cols = list(corr.columns)
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            a, b = str(cols[i]), str(cols[j])
            try:
                v = float(corr.loc[a, b])
            except Exception:
                continue
            if math.isfinite(v):
                top_pairs.append({"a": a, "b": b, "corr": round(v, 3)})
    top_pairs.sort(key=lambda x: -float(x["corr"]))

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": mode_u,
        "method": method_u,
        "lookback_trading_days": int(lookback_trading_days),
        "corr_min": float(corr_min),
        "distance_threshold": float(distance_threshold),
        "n_tickers_requested": len([t for t in tickers if str(t).strip()]),
        "n_tickers_ok": int(norm.shape[1]),
        "n_bars_aligned": int(norm.shape[0]),
        "missing_or_short": missing,
        "n_clusters": len(clusters),
        "clusters": clusters,
        "top_pairs": top_pairs[:15],
        "note_ru": (
            "Кластеры по похожести формы: корреляция нормированной цены close/close₀ "
            "за lookback. Порог corr_min — связные компоненты (транзитивно); "
            "для более мелких групп поднимайте порог (0.88–0.93). "
            "Лог-ret / сектор / earnings — следующие режимы."
        ),
    }


def normalized_overlay_payload(norm: pd.DataFrame, tickers: Sequence[str]) -> Dict[str, Any]:
    cols = [t for t in tickers if t in norm.columns]
    if not cols:
        return {"labels": [], "series": []}
    sub = norm[cols]
    labels = [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10] for d in sub.index]
    series = []
    for t in cols:
        series.append(
            {
                "ticker": t,
                "values": [None if (v is None or (isinstance(v, float) and not math.isfinite(v))) else round(float(v), 4) for v in sub[t].tolist()],
            }
        )
    return {"labels": labels, "series": series}


def build_shape_cluster_page_payload(
    engine,
    *,
    lookback_trading_days: int = 126,
    corr_min: float = 0.75,
    method: str = "components",
    mode: str = "shape",
    cluster_id: Optional[int] = None,
) -> Dict[str, Any]:
    from services.shape_cluster_universe import shape_cluster_tickers

    tickers = shape_cluster_tickers()
    report = build_shape_clusters(
        engine,
        tickers,
        lookback_trading_days=lookback_trading_days,
        mode=mode,
        method=method,
        corr_min=corr_min,
    )
    clusters = report.get("clusters") or []
    selected = None
    if cluster_id is not None:
        for c in clusters:
            if int(c.get("cluster_id") or 0) == int(cluster_id):
                selected = c
                break
    if selected is None and clusters:
        # Prefer multi-name cluster containing LRCX/KLAC if present, else largest
        preferred = None
        for c in clusters:
            ts = set(c.get("tickers") or [])
            if "LRCX" in ts or "KLAC" in ts:
                preferred = c
                break
        selected = preferred or clusters[0]

    overlay = {"labels": [], "series": []}
    if selected:
        series = fetch_close_series(engine, tickers, trading_days=lookback_trading_days)
        norm = normalize_paths(series)
        overlay = normalized_overlay_payload(norm, selected.get("tickers") or [])

    report["selected_cluster"] = selected
    report["overlay"] = overlay
    return report
