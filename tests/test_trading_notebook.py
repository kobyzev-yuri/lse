"""Smoke tests for trading notebook data + merge."""

from __future__ import annotations

from pathlib import Path

from services.trading_notebook import (
    apply_ticker_overrides,
    build_notebook_payload,
    load_notebook_data,
    merge_prices_into_tickers,
    notebook_data_path,
    update_ticker_env,
    update_ticker_levels,
    update_ticker_signals,
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


def test_ticker_signal_overrides_roundtrip(tmp_path: Path):
    ov = tmp_path / "ticker_overrides.json"
    out = update_ticker_signals(
        "MSFT",
        macro_alive=False,
        sentiment_broken=True,
        updated_by="test",
        path=ov,
    )
    assert out["signals"]["macroAlive"] is False
    assert out["signals"]["sentimentBroken"] is True
    assert ov.is_file()

    base = {
        "MSFT": {
            "sym": "MSFT",
            "signals": {"macroAlive": True, "sentimentBroken": False},
        }
    }
    merged = apply_ticker_overrides(base, {"tickers": {"MSFT": {"signals": out["signals"]}}})
    assert merged["MSFT"]["signals"]["macroAlive"] is False
    assert merged["MSFT"]["signals_override"] is True


def test_ticker_levels_overrides_roundtrip(tmp_path: Path):
    ov = tmp_path / "ticker_overrides.json"
    out = update_ticker_levels(
        "MSFT",
        buy_dip=340,
        sell=460,
        note="test levels",
        updated_by="test",
        path=ov,
    )
    assert out["levels"]["buyDip"] == 340
    assert out["levels"]["sell"] == 460
    base = {
        "MSFT": {
            "sym": "MSFT",
            "levels": {"buyDip": 350, "sell": 450, "note": "base"},
            "env": [
                {"lbl": "VIX", "state": "ok", "st": "live", "live": True},
                {"lbl": "Риторика ФРС", "state": "ok", "st": "base"},
            ],
        }
    }
    merged = apply_ticker_overrides(
        base, {"tickers": {"MSFT": {"levels": out["levels"]}}}
    )
    assert merged["MSFT"]["levels"]["buyDip"] == 340
    assert merged["MSFT"]["levels_override"] is True

    cleared = update_ticker_levels(
        "MSFT", buy_dip=None, sell=460, path=ov, updated_by="test"
    )
    assert cleared["levels"]["buyDip"] is None
    merged2 = apply_ticker_overrides(
        base, {"tickers": {"MSFT": {"levels": cleared["levels"]}}}
    )
    assert merged2["MSFT"]["levels"]["buyDip"] is None


def test_ticker_env_overrides_skip_vix(tmp_path: Path):
    ov = tmp_path / "ticker_overrides.json"
    out = update_ticker_env(
        "MSFT",
        fed={"state": "bad"},
        pt_cuts={"state": "mid"},
        updated_by="test",
        path=ov,
    )
    assert any(e["state"] == "bad" and "ФРС" in e["lbl"] for e in out["env"])
    assert any(e["state"] == "mid" and "таргет" in e["lbl"].lower() for e in out["env"])

    base = {
        "MSFT": {
            "sym": "MSFT",
            "env": [
                {"lbl": "VIX", "state": "ok", "st": "12", "live": True, "value": 12},
                {"lbl": "Риторика ФРС", "state": "ok", "st": "digest", "live": True},
                {"lbl": "Понижения таргетов (вне earnings)", "state": "ok", "st": "ok"},
            ],
        }
    }
    merged = apply_ticker_overrides(
        base, {"tickers": {"MSFT": {"env": out["env_override"]}}}
    )
    vix = next(e for e in merged["MSFT"]["env"] if "VIX" in e["lbl"])
    fed = next(e for e in merged["MSFT"]["env"] if "ФРС" in e["lbl"])
    assert vix["live"] is True and vix["state"] == "ok" and vix.get("value") == 12
    assert fed["live"] is False and fed["state"] == "bad"
    assert merged["MSFT"]["env_override"] is True

    try:
        update_ticker_env("MSFT", items=[{"lbl": "VIX", "state": "bad"}], path=ov)
        assert False, "expected ValueError for VIX"
    except ValueError as e:
        assert "VIX" in str(e)


def test_ticker_consensus_overrides_roundtrip(tmp_path: Path):
    ov = tmp_path / "ticker_overrides.json"
    from services.trading_notebook import update_ticker_consensus

    out = update_ticker_consensus(
        "MSFT",
        rating="Buy",
        pt="480",
        low="400",
        high="520",
        updated_by="test",
        path=ov,
    )
    assert out["consensus"]["rating"] == "Buy"
    merged = apply_ticker_overrides(
        {"MSFT": {"consensus": {"rating": "—", "pt": "—"}}},
        {"tickers": {"MSFT": {"consensus": out["consensus"]}}},
    )
    assert merged["MSFT"]["consensus"]["pt"] == "480"
    assert merged["MSFT"]["consensus_override"] is True
