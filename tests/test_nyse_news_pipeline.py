"""TF-IDF REG-кластер и draft_impulse как в nyse (портаж в services/nyse_news_pipeline)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.nyse_news_pipeline import (
    DraftArticle,
    apply_regime_cluster_for_draft,
    draft_impulse,
    scored_from_news_articles,
    single_scalar_draft_bias,
)


def test_regime_cluster_merges_near_duplicate_reg_themes():
    now = datetime(2026, 4, 8, 14, 0, 0, tzinfo=timezone.utc)
    inc = DraftArticle("Sandisk NAND pricing update for investors", "company flash", now, 0.4)
    shared = "iran ceasefire persian gulf oil micron memory sector"
    title = "Middle East ceasefire moves oil and semiconductor names"
    r1 = DraftArticle(title, shared, now - timedelta(hours=2), 0.1)
    r2 = DraftArticle(title, shared, now - timedelta(hours=1), -0.3)
    merged, meta = apply_regime_cluster_for_draft(
        [inc, r1, r2],
        now=now,
        enabled=True,
        similarity_threshold=0.88,
        embed_backend="tfidf",
    )
    assert meta is not None
    assert meta.n_reg_in == 2
    assert meta.n_reg_out == 1
    assert meta.n_clusters == 1
    assert len(merged) == 2


def test_regime_cluster_disabled_passes_through():
    now = datetime(2026, 4, 8, 14, 0, 0, tzinfo=timezone.utc)
    a = DraftArticle("Iran war risk and oil surge", "brent crude opec", now, 0.2)
    b = DraftArticle("Gaza tensions and energy prices move", "middle east oil", now, -0.1)
    merged, meta = apply_regime_cluster_for_draft([a, b], now=now, enabled=False)
    assert meta is None
    assert len(merged) == 2


def test_draft_impulse_regime_stress_weighted():
    now = datetime(2026, 4, 8, 14, 0, 0, tzinfo=timezone.utc)
    inc = DraftArticle("x", "y", now, 0.5)
    reg = DraftArticle("Iran talks", "strait hormuz", now, -0.4)
    merged, _ = apply_regime_cluster_for_draft([inc, reg], now=now, enabled=True, embed_backend="tfidf")
    sc = scored_from_news_articles(merged)
    di = draft_impulse(sc, now=now)
    assert di.articles_regime >= 1
    assert di.regime_stress >= 0.0
    sb = single_scalar_draft_bias(di)
    assert isinstance(sb, float)
