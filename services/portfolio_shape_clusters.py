"""
Cluster tickers by ~6m chart *shape* (normalized price path).

v1 metric: correlation of close/close[0] on a common trading calendar.
Number of groups = whatever the threshold yields (hierarchical or components).
Later modes (log-ret, sector, earnings) can plug in as `mode=`.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# Единый текст для UI (/portfolio/shape-clusters) и API.
SHAPE_CLUSTER_METHOD_RU = """Метод расчёта похожести формы графика

1. Источник: дневные close из таблицы quotes за выбранное окно (по умолчанию 126 торговых дней ≈ 6 мес.).
2. Календарь: ряды выравниваются по общим торговым датам (inner join).
3. Нормировка пути: P′_t = close_t / close_first — все тикеры стартуют с 1.0 (сравнивается форма, не абсолютная цена).
4. Похожесть пары A–B: коэффициент корреляции Пирсона corr(P′_A, P′_B) на шкале −1…+1.
   Это не cosine similarity; Pearson ≈ cosine только после центрирования (вычитания среднего).
5. Кластеры (hierarchical, average linkage): расстояние d = 1 − corr.
   Ползунок «порог похожести» S% задаёт cut: distance_threshold = 1 − S/100
   (пример: 88% → d ≤ 0.12 ⇔ corr ≥ 0.88).
6. Medoid группы — тикер с максимальной суммой corr к остальным участникам.
7. Строка на карточке вида KLAC-LRCX (0.977) — фактический Pearson для топ-3 пар внутри кластера, не порог отсечения.
"""


def shape_cluster_method_ru() -> str:
    return SHAPE_CLUSTER_METHOD_RU.strip()


def downsample_closes(values: Sequence[Any], max_points: int = 80) -> List[Optional[float]]:
    """Sparse close path for SVG sparklines (keeps endpoints)."""
    pts = min(max(20, int(max_points or 80)), 260)
    cleaned: List[Optional[float]] = []
    for v in values:
        try:
            f = float(v)
            if math.isfinite(f) and f > 0:
                cleaned.append(round(f, 4))
            else:
                cleaned.append(None)
        except (TypeError, ValueError):
            cleaned.append(None)
    n = len(cleaned)
    if n <= pts:
        return cleaned
    if pts <= 2:
        return [cleaned[0], cleaned[-1]]
    out = [cleaned[0]]
    step = (n - 1) / (pts - 1)
    for i in range(1, pts - 1):
        out.append(cleaned[int(round(i * step))])
    out.append(cleaned[-1])
    return out


def spark_closes_from_series(
    series: Dict[str, pd.Series],
    *,
    max_points: int = 80,
) -> Dict[str, List[Optional[float]]]:
    """Compact closes for UI — avoids a second charts fetch on flaky clients."""
    out: Dict[str, List[Optional[float]]] = {}
    for t, s in (series or {}).items():
        if s is None or getattr(s, "empty", True):
            continue
        try:
            vals = [float(x) for x in s.astype(float).tolist()]
        except Exception:
            continue
        out[str(t).strip().upper()] = downsample_closes(vals, max_points=max_points)
    return out


def fetch_close_series(
    engine,
    tickers: Sequence[str],
    *,
    trading_days: int = 126,
) -> Dict[str, pd.Series]:
    """Oldest→newest close series keyed by ticker (DatetimeIndex). One SQL for all tickers."""
    from sqlalchemy import bindparam, text

    wanted = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not wanted:
        return {}
    lim = int(trading_days) + 5
    min_bars = max(40, int(trading_days) // 3)
    sql = text(
        """
        SELECT ticker, date, close
        FROM (
            SELECT ticker, date, close,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM quotes
            WHERE ticker IN :tickers
        ) x
        WHERE rn <= :n
        ORDER BY ticker ASC, date ASC
        """
    ).bindparams(bindparam("tickers", expanding=True))

    buckets: Dict[str, List[Tuple[Any, float]]] = {}
    with engine.connect() as conn:
        rows = conn.execute(sql, {"tickers": wanted, "n": lim}).fetchall()
    for r in rows:
        t = str(r[0] or "").strip().upper()
        try:
            c = float(r[2])
        except (TypeError, ValueError):
            continue
        if not (t and c > 0 and math.isfinite(c)):
            continue
        buckets.setdefault(t, []).append((r[1], c))

    out: Dict[str, pd.Series] = {}
    for t, pairs in buckets.items():
        if len(pairs) < min_bars:
            continue
        if len(pairs) > trading_days + 1:
            pairs = pairs[-(trading_days + 1) :]
        dates = [pd.Timestamp(d) for d, _ in pairs]
        closes = [c for _, c in pairs]
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


def _clusters_hierarchical(
    corr: pd.DataFrame,
    *,
    distance_threshold: Optional[float] = None,
    max_clusters: Optional[int] = None,
) -> List[List[str]]:
    """Average-linkage on distance=1-corr; cut by max_clusters or distance threshold."""
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import squareform

    tickers = [str(c) for c in corr.columns]
    mat = np.array(corr.reindex(index=tickers, columns=tickers).to_numpy(dtype=float), copy=True)
    np.fill_diagonal(mat, 1.0)
    dist = np.clip(1.0 - mat, 0.0, 2.0)
    dist = np.array((dist + dist.T) / 2.0, copy=True)
    np.fill_diagonal(dist, 0.0)
    condensed = squareform(dist, checks=False)
    z = linkage(condensed, method="average")
    # max_clusters <= 0 → cut by distance threshold (UI slider); else fixed k groups.
    if max_clusters is not None and int(max_clusters) >= 2:
        k = min(int(max_clusters), max(2, len(tickers)))
        labels = fcluster(z, t=k, criterion="maxclust")
    else:
        thr = float(distance_threshold if distance_threshold is not None else 0.12)
        labels = fcluster(z, t=thr, criterion="distance")
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


def pairs_from_corr(corr: pd.DataFrame, tickers: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """All unordered pairs with Pearson corr and distance d=1−corr (sorted by corr desc)."""
    if corr is None or corr.empty:
        return []
    cols = [str(t).strip().upper() for t in (tickers or list(corr.columns)) if str(t).strip()]
    cols = [c for c in cols if c in corr.columns]
    out: List[Dict[str, Any]] = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            a, b = cols[i], cols[j]
            try:
                v = float(corr.loc[a, b])
            except Exception:
                continue
            if not math.isfinite(v):
                continue
            d = float(np.clip(1.0 - v, 0.0, 2.0))
            out.append(
                {
                    "a": a,
                    "b": b,
                    "corr": round(v, 3),
                    "distance": round(d, 3),
                }
            )
    out.sort(key=lambda x: -float(x["corr"]))
    return out


def pairwise_shape_correlations(
    engine,
    tickers: Sequence[str],
    *,
    lookback_trading_days: int = 126,
) -> Dict[str, Any]:
    """
    Same metric as shape clusters: Pearson on close/close[0] over aligned calendar.
    For the manual compare page (2–3 tickers).
    """
    wanted = [str(t).strip().upper() for t in tickers if str(t).strip()]
    series = fetch_close_series(engine, wanted, trading_days=int(lookback_trading_days))
    missing = [t for t in wanted if t not in series]
    norm = normalize_paths(series)
    corr = correlation_matrix(norm)
    pairs = pairs_from_corr(corr, [t for t in wanted if t in (corr.columns if not corr.empty else [])])
    return {
        "lookback_trading_days": int(lookback_trading_days),
        "tickers": wanted,
        "n_bars_aligned": int(norm.shape[0]) if not norm.empty else 0,
        "missing_or_short": missing,
        "pairs": pairs,
        "metric": "pearson_shape",
        "method_ru": (
            "Pearson corr по нормированной форме P′=close/close_first; "
            "distance d=1−corr. Не cosine. Тот же метод, что на странице кластеров."
        ),
    }


def build_shape_clusters(
    engine,
    tickers: Sequence[str],
    *,
    lookback_trading_days: int = 126,
    mode: str = "shape",
    method: str = "hierarchical",
    corr_min: float = 0.88,
    distance_threshold: float = 0.12,
    max_clusters: int = 8,
) -> Dict[str, Any]:
    """
    mode=shape: corr of normalized prices (path similarity).
    method=hierarchical (default): average linkage.
      max_clusters>=2 → fixed k; max_clusters<=0 → cut by distance_threshold (1−corr).
    method=components: union-find on corr>=corr_min (transitivity → megaclusters).
    """
    mode_u = (mode or "shape").strip().lower()
    method_u = (method or "hierarchical").strip().lower()
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
            "max_clusters": int(max_clusters),
            "cut": "maxclust" if int(max_clusters) >= 2 else "distance",
            "n_tickers_requested": len([t for t in tickers if str(t).strip()]),
            "n_tickers_ok": 0,
            "n_clusters": 0,
            "missing_or_short": missing,
            "clusters": [],
            "spark_closes": {},
            "method_ru": shape_cluster_method_ru(),
            "note_ru": "Недостаточно overlapping quotes для кластеризации.",
        }

    if method_u == "components":
        groups = _clusters_from_corr_threshold(corr, float(corr_min))
    else:
        try:
            mc = int(max_clusters)
            groups = _clusters_hierarchical(
                corr,
                distance_threshold=float(distance_threshold),
                max_clusters=mc if mc >= 2 else None,
            )
            method_u = "hierarchical"
        except Exception:
            groups = _clusters_from_corr_threshold(corr, float(corr_min))
            method_u = "components_fallback"

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

    cut = "maxclust" if int(max_clusters) >= 2 else "distance"
    # Embed sparklines in map JSON so UI click needs no second /charts fetch
    # (secondary fetches intermittently hang >20s for this client even at ~1KB).
    # 24 points keeps API/HTML payloads small for flaky browser downloads.
    spark_closes = spark_closes_from_series(series, max_points=24)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": mode_u,
        "method": method_u,
        "lookback_trading_days": int(lookback_trading_days),
        "corr_min": float(corr_min),
        "distance_threshold": float(distance_threshold),
        "max_clusters": int(max_clusters),
        "cut": cut,
        "n_tickers_requested": len([t for t in tickers if str(t).strip()]),
        "n_tickers_ok": int(norm.shape[1]),
        "n_bars_aligned": int(norm.shape[0]),
        "missing_or_short": missing,
        "n_clusters": len(clusters),
        "clusters": clusters,
        "top_pairs": top_pairs[:15],
        "spark_closes": spark_closes,
        "method_ru": shape_cluster_method_ru(),
        "note_ru": (
            "Кластеры по похожести формы (норм. цена, Pearson, average-linkage). "
            "Порог = distance cut d=1−corr (ниже порог похожести → строже группы). "
            "Клик по карточке → оверлей + дневные."
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


_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL_SEC = 300.0


def default_shape_clusters_path(project_root: Path | None = None) -> Path:
    root = project_root or Path(__file__).resolve().parents[1]
    app = Path("/app/logs/ml/ml_data_quality/last_portfolio_shape_clusters.json")
    if app.parent.exists() or Path("/app/logs").exists():
        return app
    return root / "local" / "logs" / "ml_data_quality" / "last_portfolio_shape_clusters.json"


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    import time

    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, payload = hit
    if time.time() - ts > _CACHE_TTL_SEC:
        return None
    return payload


def _cache_put(key: str, payload: Dict[str, Any]) -> None:
    import time

    _CACHE[key] = (time.time(), payload)


def _disk_load(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


def _disk_save(path: Path, payload: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def _empty_shape_cluster_report(
    *,
    tickers: Sequence[str],
    lookback_trading_days: int,
    corr_min: float,
    method: str,
    mode: str,
    max_clusters: int,
    distance_threshold: float,
    cache_source: str = "none",
    note_ru: str = "",
) -> Dict[str, Any]:
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "method": method,
        "lookback_trading_days": int(lookback_trading_days),
        "corr_min": float(corr_min),
        "distance_threshold": float(distance_threshold),
        "max_clusters": int(max_clusters),
        "cut": "maxclust" if int(max_clusters) >= 2 else "distance",
        "n_tickers_requested": len([t for t in tickers if str(t).strip()]),
        "n_tickers_ok": 0,
        "n_clusters": 0,
        "clusters": [],
        "spark_closes": {},
        "top_pairs": [],
        "missing_or_short": [],
        "cache_hit": False,
        "cache_source": cache_source,
        "method_ru": shape_cluster_method_ru(),
        "note_ru": note_ru
        or "Карта ещё не в кэше — клиент догрузит через API (без блокировки HTML).",
    }


def build_shape_cluster_page_payload(
    engine,
    *,
    lookback_trading_days: int = 126,
    corr_min: float = 0.88,
    method: str = "hierarchical",
    mode: str = "shape",
    cluster_id: Optional[int] = None,
    include_overlay: bool = False,
    max_clusters: int = 8,
    distance_threshold: float = 0.12,
    force_refresh: bool = False,
    cache_only: bool = False,
) -> Dict[str, Any]:
    from services.shape_cluster_universe import shape_cluster_tickers

    tickers = shape_cluster_tickers()
    # v3: spark_closes max_points=24 (smaller API body for flaky clients)
    cache_key = (
        f"v3spark24|{mode}|{method}|{lookback_trading_days}|"
        f"{corr_min:.4f}|{max_clusters}|{distance_threshold:.4f}"
    )
    disk_path = default_shape_clusters_path()
    report: Optional[Dict[str, Any]] = None

    def _map_ok(payload: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(payload, dict):
            return False
        if int(payload.get("n_tickers_ok") or 0) <= 0:
            return False
        if not isinstance(payload.get("clusters"), list) or not payload.get("clusters"):
            return False
        # Require spark embeds so UI never falls back to flaky /charts
        sc = payload.get("spark_closes")
        return isinstance(sc, dict) and len(sc) > 0

    if not force_refresh:
        cached = _cache_get(cache_key)
        if _map_ok(cached):
            report = dict(cached)  # type: ignore[arg-type]
            report["cache_hit"] = True
            report["cache_source"] = "memory"
        else:
            disk = _disk_load(disk_path)
            if disk and disk.get("cache_key") == cache_key and _map_ok(disk):
                report = dict(disk)
                report.pop("cache_key", None)
                report["cache_hit"] = True
                report["cache_source"] = "disk"
                _cache_put(cache_key, {k: v for k, v in report.items() if k not in ("cache_hit", "cache_source")})
            # иначе ниже пересчитаем; disk без spark — не используем как hit

    if report is None and cache_only:
        # HTML boot path: never block page SSR on live SQL (~1s+).
        report = _empty_shape_cluster_report(
            tickers=tickers,
            lookback_trading_days=lookback_trading_days,
            corr_min=corr_min,
            method=method,
            mode=mode,
            max_clusters=max_clusters,
            distance_threshold=distance_threshold,
            cache_source="cache_miss",
        )

    if report is None:
        try:
            report = build_shape_clusters(
                engine,
                tickers,
                lookback_trading_days=lookback_trading_days,
                mode=mode,
                method=method,
                corr_min=corr_min,
                max_clusters=max_clusters,
                distance_threshold=distance_threshold,
            )
            if _map_ok(report):
                _cache_put(cache_key, report)
                to_disk = dict(report)
                to_disk["cache_key"] = cache_key
                _disk_save(disk_path, to_disk)
            report = dict(report)
            report["cache_hit"] = False
            report["cache_source"] = "live"
        except Exception:
            # После рестарта/нагрузки — отдать последний disk, чтобы UI не «висел»
            disk = _disk_load(disk_path)
            if _map_ok(disk):
                report = dict(disk)  # type: ignore[arg-type]
                report.pop("cache_key", None)
                report["cache_hit"] = True
                report["cache_source"] = "disk_fallback"
            else:
                raise

    clusters = report.get("clusters") or []
    selected = None
    if cluster_id is not None:
        for c in clusters:
            if int(c.get("cluster_id") or 0) == int(cluster_id):
                selected = c
                break

    overlay = {"labels": [], "series": []}
    if include_overlay and selected:
        series = fetch_close_series(
            engine,
            selected.get("tickers") or [],
            trading_days=lookback_trading_days,
        )
        norm = normalize_paths(series)
        # Cap overlay series for huge groups (UI remains readable)
        members = list(selected.get("tickers") or [])
        if len(members) > 12:
            med = selected.get("medoid")
            strong = []
            for p in selected.get("strong_pairs") or []:
                strong.extend([p.get("a"), p.get("b")])
            prefer = []
            for x in [med] + strong + members:
                u = str(x or "").upper()
                if u and u not in prefer:
                    prefer.append(u)
            members = prefer[:12]
        overlay = normalized_overlay_payload(norm, members)

    report["selected_cluster"] = selected
    report["overlay"] = overlay
    report["overlay_included"] = bool(include_overlay)
    if not (report.get("method_ru") or "").strip():
        report["method_ru"] = shape_cluster_method_ru()
    return report