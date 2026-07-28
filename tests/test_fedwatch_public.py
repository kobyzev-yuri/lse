"""Unit tests for public FedWatch bucketing (no live CME/FRED)."""

from __future__ import annotations

from services.fedwatch_public import bucket_move_probabilities, enrich_fomc_env_row


def test_bucket_hold_cut_hike():
    buckets = bucket_move_probabilities(
        "3.50%-3.75%",
        {
            "3.25%-3.50%": 10.0,
            "3.50%-3.75%": 70.0,
            "3.75%-4.00%": 20.0,
        },
    )
    assert buckets["cut_pct"] == 10.0
    assert buckets["hold_pct"] == 70.0
    assert buckets["hike_pct"] == 20.0


def test_enrich_appends_fedwatch(monkeypatch):
    import services.fedwatch_public as fw

    monkeypatch.setattr(
        fw,
        "fetch_fedwatch_next",
        lambda force=False: {
            "st_short": "FedWatch 2026-07-29: hold 64% · hike 36%",
            "buckets": {"cut_pct": 0.0, "hold_pct": 64.0, "hike_pct": 36.0},
            "url": fw.FEDWATCH_TOOL_URL,
        },
    )
    row = {
        "lbl": "Риторика ФРС",
        "st": "через 1д · FOMC decision (2026-07-29)",
        "state": "bad",
        "live": True,
        "source": "live · federalreserve.gov FOMC",
        "metric": "fed",
        "fomc": {"days_until": 1, "date": "2026-07-29"},
    }
    out = enrich_fomc_env_row(row)
    assert "FedWatch" in out["st"]
    assert out["fedwatch"]
    assert "FedWatch" in out["source"]
