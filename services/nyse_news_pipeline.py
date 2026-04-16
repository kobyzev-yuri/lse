"""
Новостной уровень 1–3 как в nyse: каналы REG/POL/INC, TF-IDF кластеризация REG для draft_impulse,
scored_from_news_articles, draft_impulse, single_scalar_draft_bias.

Код каналов и кластера — портаж с ``nyse/pipeline/news/channels.py`` и
``nyse/pipeline/news/regime_cluster.py`` (TF-IDF; OpenAI-эмбеддинги в LSE не подключены —
при NYSE_REGIME_CLUSTER_EMBED=openai используется fallback на TF-IDF, как в nyse без ключа).
"""

from __future__ import annotations

import enum
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import pandas as pd

# --- nyse/pipeline/types (минимум для draft) ---


class NewsImpactChannel(str, enum.Enum):
    INCREMENTAL = "incremental"
    REGIME = "regime"
    POLICY_RATES = "policy_rates"


@dataclass(frozen=True)
class DraftImpulse:
    draft_bias_incremental: float = 0.0
    regime_stress: float = 0.0
    policy_stress: float = 0.0
    articles_incremental: int = 0
    articles_regime: int = 0
    articles_policy: int = 0
    weight_sum_incremental: float = 0.0
    weight_sum_regime: float = 0.0
    weight_sum_policy: float = 0.0
    max_abs_regime: float = 0.0
    max_abs_policy: float = 0.0


@dataclass(frozen=True)
class ScoredArticle:
    published_at: datetime
    cheap_sentiment: float
    channel: NewsImpactChannel


@dataclass
class DraftArticle:
    """Минимальные поля как у nyse ``NewsArticle`` для кластера и draft."""

    title: str
    summary: str
    timestamp: datetime
    cheap_sentiment: Optional[float] = None
    display_snippet: str = ""


@dataclass(frozen=True)
class RegimeClusterMeta:
    enabled: bool
    embed_backend: str
    n_reg_in: int
    n_reg_out: int
    n_clusters: int
    threshold: float
    note: str


# --- nyse/pipeline/news/channels.py (дословно по regex) ---

_REGIME_PATTERNS = [
    re.compile(
        r"\b(?:war|sanction|sanctions|embargo|invasion|military|nato|missile)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:ceasefire|truce|armistice|geopolitical|terror|hostilities|"
        r"iran|israeli?|ukraine|gaza|taiwan|north korea)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:persian gulf|middle east|strait of hormuz|red sea)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:brent(?:\s+crude)?|wti|west texas intermediate|crude\s+oil|"
        r"oil\s+prices?|opec|energy\s+prices?)\b",
        re.I,
    ),
    re.compile(r"\b(?:геополит|санкци|война|конфликт|перемири)\b", re.I),
]
_POLICY_PATTERNS = [
    re.compile(
        r"\b(?:fed|fomc|federal reserve|ecb|boe|rate hike|rate cut|interest rate|bps|qe|qt)\b",
        re.I,
    ),
    re.compile(r"\b(?:ставк|центральн\w+ банк|базовая ставка)\b", re.I),
]


def classify_channel(title: str, summary: Optional[str] = None) -> Tuple[NewsImpactChannel, float]:
    text = f"{title} {summary or ''}".strip()
    if not text:
        return NewsImpactChannel.INCREMENTAL, 1.0

    for rx in _REGIME_PATTERNS:
        if rx.search(text):
            return NewsImpactChannel.REGIME, 0.85

    for rx in _POLICY_PATTERNS:
        if rx.search(text):
            return NewsImpactChannel.POLICY_RATES, 0.8

    return NewsImpactChannel.INCREMENTAL, 1.0


# --- nyse/pipeline/news/draft.py ---


def _age_hours(now: datetime, t: datetime) -> float:
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - t).total_seconds() / 3600.0)


def draft_impulse(
    articles: Sequence[ScoredArticle],
    *,
    now: Optional[datetime] = None,
    half_life_hours: float = 12.0,
) -> DraftImpulse:
    now = now or datetime.now(timezone.utc)
    if not articles:
        return DraftImpulse()

    lam = math.log(2) / max(half_life_hours, 1e-6)

    def weighted_mean(pairs: Sequence[Tuple[float, float]]) -> float:
        num = 0.0
        den = 0.0
        for w, x in pairs:
            num += w * x
            den += w
        return num / den if den > 0 else 0.0

    inc: List[Tuple[float, float]] = []
    reg: List[Tuple[float, float]] = []
    pol: List[Tuple[float, float]] = []
    wsum_inc = wsum_reg = wsum_pol = 0.0
    max_abs_reg = max_abs_pol = 0.0

    for a in articles:
        age = _age_hours(now, a.published_at)
        w = math.exp(-lam * age)
        cs = a.cheap_sentiment
        pair = (w, cs)
        if a.channel == NewsImpactChannel.INCREMENTAL:
            inc.append(pair)
            wsum_inc += w
        elif a.channel == NewsImpactChannel.REGIME:
            reg.append(pair)
            wsum_reg += w
            max_abs_reg = max(max_abs_reg, abs(cs))
        else:
            pol.append(pair)
            wsum_pol += w
            max_abs_pol = max(max_abs_pol, abs(cs))

    return DraftImpulse(
        draft_bias_incremental=weighted_mean(inc),
        regime_stress=weighted_mean([(w, abs(x)) for w, x in reg]) if reg else 0.0,
        policy_stress=weighted_mean([(w, abs(x)) for w, x in pol]) if pol else 0.0,
        articles_incremental=len(inc),
        articles_regime=len(reg),
        articles_policy=len(pol),
        weight_sum_incremental=wsum_inc,
        weight_sum_regime=wsum_reg,
        weight_sum_policy=wsum_pol,
        max_abs_regime=max_abs_reg,
        max_abs_policy=max_abs_pol,
    )


def scored_from_news_articles(
    articles: Sequence[DraftArticle],
    *,
    seen_regime_titles: Optional[Set[str]] = None,
) -> List[ScoredArticle]:
    out: List[ScoredArticle] = []
    for a in articles:
        ch, _ = classify_channel(a.title, a.summary)
        if ch == NewsImpactChannel.REGIME and seen_regime_titles is not None:
            if a.title in seen_regime_titles:
                ch = NewsImpactChannel.INCREMENTAL
            else:
                seen_regime_titles.add(a.title)

        cs = a.cheap_sentiment if a.cheap_sentiment is not None else 0.0
        out.append(
            ScoredArticle(
                published_at=a.timestamp,
                cheap_sentiment=float(cs),
                channel=ch,
            )
        )
    return out


def single_scalar_draft_bias(d: DraftImpulse) -> float:
    return (
        d.draft_bias_incremental
        - 0.15 * d.regime_stress
        - 0.1 * d.policy_stress
    )


# --- nyse/pipeline/news/regime_cluster.py (TF-IDF ветка) ---


def _article_text(a: DraftArticle) -> str:
    s = (a.summary or "")[:1500]
    return f"{a.title}\n{s}".strip()


def _tfidf_unit_matrix(texts: List[str]):
    import numpy as np

    if not texts:
        return np.zeros((0, 0))
    docs_tokens = [re.findall(r"[a-z0-9]+", t.lower()) for t in texts]
    vocab: Dict[str, int] = {}
    for tokens in docs_tokens:
        for tok in set(tokens):
            if tok not in vocab:
                vocab[tok] = len(vocab)
    n_docs = len(texts)
    n_terms = len(vocab)
    if n_terms == 0:
        return np.ones((n_docs, 1), dtype=np.float64) / math.sqrt(max(n_docs, 1))

    x = np.zeros((n_docs, n_terms), dtype=np.float64)
    for i, tokens in enumerate(docs_tokens):
        tf = Counter(tokens)
        for tok, c in tf.items():
            j = vocab.get(tok)
            if j is not None:
                x[i, j] = float(c)
    df = (x > 0).sum(axis=0)
    idf = np.log((1.0 + n_docs) / (1.0 + df)) + 1.0
    x = x * idf
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-9)
    return x / norms


def _greedy_clusters_from_cosine(
    embeddings,
    threshold: float,
    process_order: List[int],
) -> List[List[int]]:
    import numpy as np

    clusters: List[List[int]] = []
    for i in process_order:
        v = embeddings[i]
        placed = False
        for cl in clusters:
            sims = [float(np.dot(v, embeddings[j])) for j in cl]
            if sims and max(sims) >= threshold:
                cl.append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])
    return clusters


def _pick_representative(cluster_row_indices: List[int], articles_reg: List[DraftArticle]) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def ts_key(a: DraftArticle) -> datetime:
        t = a.timestamp
        if isinstance(t, datetime):
            return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        return epoch

    best_local = cluster_row_indices[0]
    best = articles_reg[best_local]
    best_cs = abs(best.cheap_sentiment or 0.0)
    best_ts = ts_key(best)
    for loc in cluster_row_indices[1:]:
        a = articles_reg[loc]
        cs = abs(a.cheap_sentiment or 0.0)
        ts = ts_key(a)
        if cs > best_cs + 1e-9 or (abs(cs - best_cs) < 1e-9 and ts > best_ts):
            best_local = loc
            best_cs = cs
            best_ts = ts
    return best_local


def apply_regime_cluster_for_draft(
    articles: Sequence[DraftArticle],
    *,
    now: datetime,
    enabled: Optional[bool] = None,
    similarity_threshold: Optional[float] = None,
    embed_backend: Optional[str] = None,
    openai_settings: Any = None,
) -> Tuple[List[DraftArticle], Optional[RegimeClusterMeta]]:
    from config_loader import get_config_value

    if enabled is None:
        raw = (
            get_config_value("NYSE_REGIME_CLUSTER")
            or os.environ.get("NYSE_REGIME_CLUSTER")
            or "1"
        ).strip().lower()
        enabled = raw not in ("0", "false", "no", "off")

    if not enabled or len(articles) < 2:
        return list(articles), None

    if similarity_threshold is None:
        similarity_threshold = float(
            get_config_value("NYSE_REGIME_CLUSTER_THRESHOLD")
            or os.environ.get("NYSE_REGIME_CLUSTER_THRESHOLD")
            or "0.88"
        )

    if embed_backend is None:
        embed_backend = (
            get_config_value("NYSE_REGIME_CLUSTER_EMBED")
            or os.environ.get("NYSE_REGIME_CLUSTER_EMBED")
            or "tfidf"
        ).strip().lower()

    reg_idx: List[int] = []
    non_reg: List[DraftArticle] = []
    for i, a in enumerate(articles):
        ch, _ = classify_channel(a.title, a.summary)
        if ch == NewsImpactChannel.REGIME:
            reg_idx.append(i)
        else:
            non_reg.append(a)

    articles_reg = [articles[i] for i in reg_idx]
    n_reg_in = len(articles_reg)
    if n_reg_in < 2:
        return list(articles), None

    texts = [_article_text(a) for a in articles_reg]
    embed_backend_resolved = embed_backend

    if embed_backend == "openai" and openai_settings is not None:
        embed_backend_resolved = "tfidf"

    vectors = _tfidf_unit_matrix(texts)

    process_order = sorted(
        range(n_reg_in),
        key=lambda k: articles_reg[k].timestamp or now,
        reverse=True,
    )
    clusters_rows = _greedy_clusters_from_cosine(vectors, similarity_threshold, process_order)

    reps: List[DraftArticle] = []
    for cl in clusters_rows:
        local_best = _pick_representative(cl, articles_reg)
        reps.append(articles_reg[local_best])

    merged = non_reg + reps
    meta = RegimeClusterMeta(
        enabled=True,
        embed_backend=embed_backend_resolved,
        n_reg_in=n_reg_in,
        n_reg_out=len(reps),
        n_clusters=len(clusters_rows),
        threshold=similarity_threshold,
        note=f"REG: {n_reg_in} статей → {len(reps)} тем ({len(clusters_rows)} кластеров), backend={embed_backend_resolved}",
    )
    return merged, meta


# --- KB → DraftArticle ---


def _cheap_from_kb_sentiment(sentiment_score: Any) -> float:
    if sentiment_score is None or (isinstance(sentiment_score, float) and math.isnan(sentiment_score)):
        return 0.0
    try:
        x = float(sentiment_score)
    except (TypeError, ValueError):
        return 0.0
    return (x - 0.5) * 2.0


def kb_row_to_draft_article(row: Any) -> DraftArticle:
    """Строка KB (Series или dict) → DraftArticle."""
    if hasattr(row, "get"):
        get = row.get
    else:
        get = dict(row).get  # type: ignore[arg-type]
    content = str(get("content") or get("title") or get("insight") or "").strip()
    lines = content.split("\n", 1)
    title = (lines[0][:200] if lines else "") or content[:200]
    summary = lines[1][:1500] if len(lines) > 1 else (content[200:1700] if len(content) > 200 else "")
    ts_raw = get("ts") or get("ingested_at")
    tdt = pd.to_datetime(ts_raw, errors="coerce", utc=True)
    if pd.isna(tdt):
        tdt = pd.Timestamp.now(tz="UTC")
    ts = tdt.to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    cheap = _cheap_from_kb_sentiment(get("sentiment_score"))
    return DraftArticle(
        title=title or "—",
        summary=summary,
        timestamp=ts,
        cheap_sentiment=cheap,
        display_snippet=content[:100].replace("\n", " "),
    )


def kb_dataframe_to_draft_articles(disp: pd.DataFrame) -> List[DraftArticle]:
    if disp is None or disp.empty:
        return []
    return [kb_row_to_draft_article(row) for _, row in disp.iterrows()]


def run_kb_nyse_draft_bundle(
    disp: pd.DataFrame,
    *,
    now: Optional[datetime] = None,
) -> Tuple[DraftImpulse, Optional[RegimeClusterMeta], List[ScoredArticle], float, float, List[DraftArticle]]:
    """
    Returns:
        di, rmeta, scored, row_mean_bias, draft_scalar, merged_articles (для выдержек REG).
    """
    now = now or datetime.now(timezone.utc)
    articles = kb_dataframe_to_draft_articles(disp)
    if not articles:
        return DraftImpulse(), None, [], 0.0, 0.0, []

    cheap_list = [a.cheap_sentiment or 0.0 for a in articles]
    row_mean_bias = float(sum(cheap_list) / len(cheap_list))

    merged, rmeta = apply_regime_cluster_for_draft(articles, now=now, openai_settings=None)
    scored = scored_from_news_articles(merged)
    di = draft_impulse(scored, now=now)
    draft_scalar = single_scalar_draft_bias(di)
    return di, rmeta, scored, row_mean_bias, draft_scalar, merged


def summarize_kb_regime_geopolitical(
    disp: pd.DataFrame,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Контекст REG как в nyse после TF-IDF кластера: n_geo = число REG-статей после merge (темы),
    geo_stress = di.regime_stress (draft_impulse по каналу REG, exp-затухание).
    """
    out: Dict[str, Any] = {
        "n_geo": 0,
        "geo_stress": 0.0,
        "summary_short": "",
        "lines": [],
        "regime_cluster_note": "",
    }
    if disp is None or disp.empty:
        return out

    di, rmeta, _, _, _, merged = run_kb_nyse_draft_bundle(disp, now=now)
    out["geo_stress"] = float(di.regime_stress)
    out["n_geo"] = int(di.articles_regime)
    if rmeta:
        out["regime_cluster_note"] = rmeta.note

    lines: List[str] = []
    for a in merged:
        ch, _ = classify_channel(a.title, a.summary)
        if ch != NewsImpactChannel.REGIME:
            continue
        sn = (a.display_snippet or a.title or "").strip()
        if sn and sn not in lines and len(lines) < 12:
            lines.append(sn[:100])
    out["lines"] = lines
    if lines:
        out["summary_short"] = lines[0][:120] + ("…" if len(lines[0]) > 120 else "")
    return out
