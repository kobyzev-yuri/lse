"""Unit tests for Fundament ↔ Ex99/IR LLM reconcile."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.notebook_fundament_reconcile import (
    MaterialMissingError,
    ReconcileLlmError,
    _apply_metrics_patch,
    _parse_json_object,
    merge_reconcile_into_fundament,
    reconcile_fundament_with_earnings_llm,
)


def test_parse_json_object_fenced():
    raw = '```json\n{"filing_period": "FY2025", "conflicts": []}\n```'
    got = _parse_json_object(raw)
    assert got is not None
    assert got["filing_period"] == "FY2025"


def test_apply_metrics_patch_conflict_takes_filing_value():
    yahoo = [
        {"k": "КЭШ", "v": "$50B", "note": "Yahoo", "tone": "good"},
        {"k": "FCF", "v": "$10B", "note": "Yahoo", "tone": "good"},
        {"k": "Прямой долг", "v": "$20B", "note": "Yahoo", "tone": ""},
        {"k": "Запас прочности", "v": "current ratio 1.2", "note": "Yahoo", "tone": "good"},
    ]
    patch = [
        {
            "key": "КЭШ",
            "value": "$48B",
            "note": "cash+STI на 2025-12-31",
            "tone": "good",
            "status": "conflict",
        },
        {"key": "FCF", "value": "$10B", "status": "match", "note": "совпало"},
        {"key": "Прямой долг", "status": "missing_in_filing", "note": ""},
    ]
    out = _apply_metrics_patch(yahoo, patch)
    by = {m["k"]: m for m in out}
    assert by["КЭШ"]["v"] == "$48B"
    assert "LLM сверка" in by["КЭШ"]["note"]
    assert by["FCF"]["v"] == "$10B"
    assert "не найдено" in by["Прямой долг"]["note"]


def test_merge_never_copies_nastya_fields():
    yahoo = {
        "exchange": "NASDAQ",
        "hq_ru": "Redmond",
        "tagline": "Cloud",
        "metrics": [{"k": "КЭШ", "v": "$1", "note": "y", "tone": "good"}],
        "margin_ru": "Yahoo margin",
        "financing_ru": "Yahoo fin",
        "key_clients_ru": "SHOULD_NOT_KEEP",
        "pluses": ["SHOULD_NOT"],
        "risks": ["SHOULD_NOT"],
        "filing_url": "https://example.com/old",
    }
    structured = {
        "filing_period": "Q2 2026",
        "metrics_patch": [
            {"key": "КЭШ", "value": "$2", "status": "conflict", "note": "Ex99"}
        ],
        "margin_ru": "Filing margin",
        "financing_ru": "Filing fin",
        "conflicts": [
            {"field": "КЭШ", "yahoo": "$1", "filing": "$2", "note": "расхождение"}
        ],
    }
    out = merge_reconcile_into_fundament(
        yahoo, structured, filing_url="https://sec.example/ex99"
    )
    assert out["key_clients_ru"] == ""
    assert out["pluses"] == []
    assert out["risks"] == []
    assert out["filing_url"] == "https://sec.example/ex99"
    assert out["margin_ru"] == "Filing margin"
    assert "Q2 2026" in out["financing_ru"]
    assert out["metrics"][0]["v"] == "$2"


def test_reconcile_raises_when_no_material():
    with patch(
        "services.notebook_fundament_reconcile.load_best_reconcile_material",
        return_value={"content_text": "", "filing_url": ""},
    ):
        with pytest.raises(MaterialMissingError):
            reconcile_fundament_with_earnings_llm("MSFT")


def test_reconcile_mock_llm_ok():
    import sys
    from types import ModuleType

    yahoo = {
        "ticker": "MSFT",
        "fundament": {
            "exchange": "NASDAQ",
            "hq_ru": "Redmond",
            "listing_origin_ru": "",
            "tagline": "Azure",
            "metrics": [
                {"k": "КЭШ", "v": "$50B", "note": "Yahoo", "tone": "good"},
                {"k": "FCF", "v": "$10B", "note": "Yahoo", "tone": "good"},
                {"k": "Прямой долг", "v": "$20B", "note": "Yahoo", "tone": ""},
                {"k": "Запас прочности", "v": "1.2", "note": "Yahoo", "tone": "mid"},
            ],
            "margin_ru": "gross 70%",
            "financing_ru": "кэш $50B",
            "pluses": ["x"],
            "risks": ["y"],
            "key_clients_ru": "z",
            "filing_url": "",
        },
        "filled": ["cash"],
    }
    material = {
        "id": 1,
        "filing_url": "https://sec.example/ex99.htm",
        "source_name": "Exhibit 99.1",
        "material_type": "press_release",
        "event_date": "2026-07-29",
        "parse_status": "parsed",
        "content_text": "X" * 500 + " Cash and short-term investments $48 billion.",
    }
    llm_json = {
        "filing_period": "Q4 FY26",
        "metrics_patch": [
            {
                "key": "КЭШ",
                "value": "$48B",
                "note": "cash+STI",
                "tone": "good",
                "status": "conflict",
            }
        ],
        "margin_ru": None,
        "financing_ru": "кэш $48B по Ex99",
        "conflicts": [
            {
                "field": "КЭШ",
                "yahoo": "$50B",
                "filing": "$48B",
                "note": "Yahoo выше",
            }
        ],
        "evidence_quotes": [
            {"topic": "cash", "quote": "Cash and short-term investments $48 billion."}
        ],
    }
    mock_llm = MagicMock()
    mock_llm.client = object()
    mock_llm.timeout = 60
    mock_llm.generate_response.return_value = {
        "response": __import__("json").dumps(llm_json),
        "model": "test-model",
        "usage": {},
    }
    fake_llm_mod = ModuleType("services.llm_service")
    fake_llm_mod.LLMService = MagicMock(return_value=mock_llm)
    fake_llm_mod.get_openai_http_timeout_prompt_entry = MagicMock(return_value=90.0)

    with patch(
        "services.notebook_fundament_reconcile.load_best_reconcile_material",
        return_value=material,
    ), patch(
        "services.trading_notebook.suggest_fundament_from_yfinance",
        return_value=yahoo,
    ), patch.dict(sys.modules, {"services.llm_service": fake_llm_mod}):
        out = reconcile_fundament_with_earnings_llm("MSFT")

    assert out["status"] == "ok"
    assert out["fundament"]["pluses"] == []
    assert out["fundament"]["risks"] == []
    assert out["fundament"]["key_clients_ru"] == ""
    assert out["fundament"]["metrics"][0]["v"] == "$48B"
    assert out["fundament"]["filing_url"] == material["filing_url"]
    assert len(out["conflicts"]) == 1
    assert out["conflicts"][0]["field"] == "КЭШ"
    assert out["evidence_quotes"][0]["topic"] == "cash"
    mock_llm.generate_response.assert_called_once()


def test_reconcile_llm_error_on_bad_json():
    import sys
    from types import ModuleType

    material = {
        "id": 1,
        "filing_url": "https://sec.example/ex99.htm",
        "source_name": "Ex99",
        "material_type": "press_release",
        "event_date": "2026-07-29",
        "parse_status": "parsed",
        "content_text": "Y" * 500,
    }
    mock_llm = MagicMock()
    mock_llm.client = object()
    mock_llm.timeout = 60
    mock_llm.generate_response.return_value = {
        "response": "not json at all",
        "model": "test-model",
    }
    fake_llm_mod = ModuleType("services.llm_service")
    fake_llm_mod.LLMService = MagicMock(return_value=mock_llm)
    fake_llm_mod.get_openai_http_timeout_prompt_entry = MagicMock(return_value=90.0)

    with patch(
        "services.notebook_fundament_reconcile.load_best_reconcile_material",
        return_value=material,
    ), patch(
        "services.trading_notebook.suggest_fundament_from_yfinance",
        return_value={"fundament": {"metrics": []}},
    ), patch.dict(sys.modules, {"services.llm_service": fake_llm_mod}):
        with pytest.raises(ReconcileLlmError):
            reconcile_fundament_with_earnings_llm("MSFT")
