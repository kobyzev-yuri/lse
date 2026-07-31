"""Unit tests for SEC companyfacts → Fundament draft."""

from __future__ import annotations

from unittest.mock import patch

from services.notebook_fundament_edgar import (
    _fcf_proxy,
    _latest_usd_fact,
    _select_usd_fact,
    _sum_latest_debt,
    resolve_ticker_cik,
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


def test_select_prefer_annual_fcf_over_newer_ytd():
    facts = {
        "NetCashProvidedByUsedInOperatingActivities": {
            "units": {
                "USD": [
                    {"end": "2025-12-31", "val": 1e11, "form": "10-K", "fp": "FY"},
                    {"end": "2026-06-30", "val": 5e10, "form": "10-Q", "fp": "Q2"},
                ]
            }
        },
        "PaymentsToAcquirePropertyPlantAndEquipment": {
            "units": {
                "USD": [
                    {"end": "2025-12-31", "val": 3e10, "form": "10-K", "fp": "FY"},
                    {"end": "2026-06-30", "val": 4e10, "form": "10-Q", "fp": "Q2"},
                ]
            }
        },
    }
    fcf = _fcf_proxy(facts)
    assert fcf is not None
    val, end, form, how = fcf
    assert form == "10-K"
    assert end.startswith("2025-12")
    assert val == 7e10  # 100B - 30B
    assert how == "OCF-|CapEx|"


def test_debt_rejects_stale_long_term_debt():
    facts = {
        "LongTermDebt": {
            "units": {
                "USD": [
                    {"end": "2014-12-31", "val": 0, "form": "10-K"},
                    {"end": "2021-12-31", "val": 1e6, "form": "10-K"},
                ]
            }
        },
        "LongTermDebtNoncurrent": {
            "units": {
                "USD": [{"end": "2025-12-31", "val": 3.2e9, "form": "10-K"}]
            }
        },
        "LongTermDebtCurrent": {
            "units": {
                "USD": [{"end": "2025-12-31", "val": 0.5e9, "form": "10-K"}]
            }
        },
    }
    debt = _sum_latest_debt(facts)
    assert debt is not None
    val, end, form = debt
    assert end.startswith("2025-12")
    assert val == 3.7e9
    assert form == "10-K"


def test_resolve_ticker_cik_uses_static_then_json():
    assert resolve_ticker_cik("MSFT") == "789019"
    with patch(
        "services.notebook_fundament_edgar._sec_ticker_cik_map",
        return_value={"ZZNEW": "9999999"},
    ):
        assert resolve_ticker_cik("ZZNEW") == "9999999"
        assert resolve_ticker_cik("NOSUCH") is None


def test_suggest_edgar_unknown_cik():
    with patch("services.notebook_fundament_edgar._sec_ticker_cik_map", return_value={}):
        out = suggest_fundament_from_edgar("ZZZZNOCIK")
    assert out["source"] == "edgar"
    assert out["filled"] == []
    assert "CIK" in out["note"] or "cik" in out["note"].lower() or "Нет CIK" in out["note"]


def test_suggest_edgar_msft_mocked_prefers_fy_when_present():
    fake = {
        "entityName": "MICROSOFT CORP",
        "facts": {
            "us-gaap": {
                "CashCashEquivalentsAndShortTermInvestments": {
                    "units": {"USD": [{"end": "2026-06-30", "val": 7.67e10, "form": "10-K"}]}
                },
                "CashAndCashEquivalentsAtCarryingValue": {
                    "units": {"USD": [{"end": "2026-06-30", "val": 2e10, "form": "10-K"}]}
                },
                "LongTermDebt": {
                    "units": {"USD": [{"end": "2026-06-30", "val": 4.03e10, "form": "10-K"}]}
                },
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {
                        "USD": [
                            {"end": "2026-06-30", "val": 9e10, "form": "10-K", "fp": "FY"},
                            {"end": "2026-03-31", "val": 2e10, "form": "10-Q", "fp": "Q3"},
                        ]
                    }
                },
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {
                        "USD": [
                            {"end": "2026-06-30", "val": 2.3e10, "form": "10-K", "fp": "FY"},
                            {"end": "2026-03-31", "val": 5e9, "form": "10-Q", "fp": "Q3"},
                        ]
                    }
                },
            }
        },
    }
    with patch("services.notebook_fundament_edgar._load_companyfacts", return_value=fake):
        out = suggest_fundament_from_edgar("MSFT")
    assert out["source"] == "edgar"
    assert "cash" in out["filled"]
    cash_m = next(m for m in out["fundament"]["metrics"] if m["k"] == "КЭШ")
    assert cash_m["v"] == "$76.7B"
    assert "cash+STI" in cash_m["note"]
    fcf_m = next(m for m in out["fundament"]["metrics"] if m["k"] == "FCF")
    assert "FY" in fcf_m["note"]
    assert "сравнимо с Yahoo FY" in fcf_m["note"]
    assert fcf_m["v"] == "$67.0B"
    debt_m = next(m for m in out["fundament"]["metrics"] if m["k"] == "Прямой долг")
    assert "без leases" in debt_m["note"]
    assert "debt" in out["filled"]
    assert "fcf" in out["filled"]
    assert out["fundament"]["pluses"] == []
    assert out["fundament"]["risks"] == []
    assert out["fundament"]["tagline"] == ""
