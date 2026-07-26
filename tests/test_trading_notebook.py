"""Smoke tests for trading notebook data + merge."""

from __future__ import annotations

from pathlib import Path

from services.trading_notebook import (
    build_notebook_payload,
    load_notebook_data,
    merge_prices_into_tickers,
    notebook_data_path,
)


def test_notebook_data_file_exists_and_samples():
    p = notebook_data_path()
    assert p.is_file(), f"missing {p}"
    data = load_notebook_data(p)
    assert int(data.get("schema_version") or 0) >= 1
    tickers = data["tickers"]
    assert "MSFT" in tickers
    levels = tickers["MSFT"]["levels"]
    assert levels["buyDip"] == 350
    assert levels["sell"] == 450
    assert "SNDK" in tickers and "LITE" in tickers
    assert tickers["NBIS"].get("fundament")
    assert tickers["NBIS"]["fundament"]["metrics"]


def test_merge_prices_sets_px_for_verdict():
    tickers = {
        "MSFT": {
            "sym": "MSFT",
            "group": "1",
            "levels": {"buyDip": 350, "sell": 450},
        }
    }
    prices = {
        "MSFT": {
            "close": 400.0,
            "prev_close": 390.0,
            "chg": 10.0,
            "chg_pct": 2.564,
            "asof": "2026-07-24",
            "source": "quotes",
        }
    }
    merged = merge_prices_into_tickers(tickers, prices)
    assert merged["MSFT"]["px"] == 400.0
    assert merged["MSFT"]["up"] is True
    assert "2.56" in merged["MSFT"]["chg"] or "2.56" in merged["MSFT"]["chg"].replace(",", ".")


def test_build_payload_without_prices():
    payload = build_notebook_payload(with_prices=False)
    assert "groups" in payload and "tickers" in payload
    assert "MSFT" in payload["tickers"]
    assert payload["tickers"]["MSFT"]["levels"]["sell"] == 450
    assert Path(payload["data_path"]).name == "notebook_data.json"
