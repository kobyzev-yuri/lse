"""Unit tests for SEC companyfacts → Fundament draft."""

from __future__ import annotations

from unittest.mock import patch

from services.notebook_fundament_edgar import (
    _latest_usd_fact,
    suggest_fundament_from_edgar,
)


def test_latest_usd_fact_prefers_10q_newer():
    facts = {
        "CashAndCashEquivalentsAtCarryingValue": {
            "units": {
                "USD": [
                    {"end": "2025-12-31", "val": 100, "form": "10-K"},
                    {"end": "2026-03-31", "val": 120, "form": "10-Q"},
                    {"end": "2026-06-30", "val": 999, "form": "OTHER"},
                ]
            }
        }
    }
    got = _latest_usd_fact(facts, ("CashAndCashEquivalentsAtCarryingValue",))
    assert got is not None
    val, end, form, _tag = got
    assert val == 120
    assert end.startswith("2026-03")
    assert form == "10-Q"


def test_suggest_edgar_unknown_cik():
    out = suggest_fundament_from_edgar("ZZZZNOCIK")
    assert out["source"] == "edgar"
    assert out["filled"] == []
    assert "CIK" in out["note"] or "cik" in out["note"].lower() or "Нет CIK" in out["note"]


def test_suggest_edgar_msft_mocked():
    fake = {
        "entityName": "MICROSOFT CORP",
        "facts": {
            "us-gaap": {
                "CashCashEquivalentsAndShortTermInvestments": {
                    "units": {"USD": [{"end": "2026-03-31", "val": 7.6e10, "form": "10-Q"}]}
                },
                "CashAndCashEquivalentsAtCarryingValue": {
                    "units": {"USD": [{"end": "2026-03-31", "val": 2e10, "form": "10-Q"}]}
                },
                "LongTermDebt": {
                    "units": {"USD": [{"end": "2026-03-31", "val": 4e10, "form": "10-Q"}]}
                },
                "LongTermDebtCurrent": {
                    "units": {"USD": [{"end": "2026-03-31", "val": 1e10, "form": "10-Q"}]}
                },
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {"USD": [{"end": "2026-03-31", "val": 2e10, "form": "10-Q"}]}
                },
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {"USD": [{"end": "2026-03-31", "val": 5e9, "form": "10-Q"}]}
                },
            }
        },
    }
    with patch("services.notebook_fundament_edgar._load_companyfacts", return_value=fake):
        out = suggest_fundament_from_edgar("MSFT")
    assert out["source"] == "edgar"
    assert "cash" in out["filled"]
    cash_m = next(m for m in out["fundament"]["metrics"] if m["k"] == "КЭШ")
    assert cash_m["v"] == "$76.0B"
    assert "cash+STI" in cash_m["note"]
    assert "промежуточный BS" in cash_m["note"]
    fcf_m = next(m for m in out["fundament"]["metrics"] if m["k"] == "FCF")
    assert "YTD" in fcf_m["note"]
    assert "≠ Yahoo FY" in fcf_m["note"]
    debt_m = next(m for m in out["fundament"]["metrics"] if m["k"] == "Прямой долг")
    assert "без leases" in debt_m["note"]
    assert "debt" in out["filled"]
    assert "fcf" in out["filled"]
    assert out["fundament"]["pluses"] == []
    assert out["fundament"]["risks"] == []
    assert out["fundament"]["tagline"] == ""
