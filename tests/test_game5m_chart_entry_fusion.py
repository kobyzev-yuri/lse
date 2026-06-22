"""Tests for chart entry LSTM+tabular fusion helpers."""
from __future__ import annotations

from services.game5m_chart_entry_fusion import fusion_tab_keys, tabular_vector_from_row
from services.game5m_tabular_ablation import ENTRY_ABLATION_TRACKS


def test_fusion_tab_e2_keys_match_ablation():
    assert fusion_tab_keys("e2") == ENTRY_ABLATION_TRACKS["E2_T_time_KB"]


def test_fusion_tab_e3_keys_match_ablation():
    assert fusion_tab_keys("e3") == ENTRY_ABLATION_TRACKS["E3_full_TNC"]


def test_tabular_vector_from_row():
    keys = ("rsi_5m", "kb_news_count")
    vec = tabular_vector_from_row({"rsi_5m": 45.5, "kb_news_count": 2}, keys)
    assert vec == [45.5, 2.0]
