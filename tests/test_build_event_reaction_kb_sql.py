"""Tests for KB → ERD build SQL helpers."""
from __future__ import annotations

from scripts.build_event_reaction_dataset import _kb_base_where, _kb_from_clause


def test_kb_base_where_past_only():
    where = _kb_base_where(past_only=True)
    assert "kb.ts::date <= CURRENT_DATE" in where


def test_kb_from_clause_dedup_prefers_non_yfinance():
    sql = _kb_from_clause(dedup_kb=True, past_only=True)
    assert "DISTINCT ON" in sql
    assert "yfinance" in sql
    assert "earnings_event_detail" in sql


def test_kb_from_clause_plain():
    sql = _kb_from_clause(dedup_kb=False, past_only=False)
    assert sql.startswith("FROM knowledge_base kb")
    assert "DISTINCT ON" not in sql
