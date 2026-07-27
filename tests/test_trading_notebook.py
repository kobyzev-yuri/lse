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


def test_add_and_move_notebook_ticker(tmp_path: Path):
    from services.trading_notebook import (
        add_notebook_ticker,
        apply_ticker_overrides,
        set_notebook_ticker_group,
    )

    ov = tmp_path / "ticker_overrides.json"
    created = add_notebook_ticker(
        "ZZTOP", "n", name="ZZ Top Inc", path=ov, updated_by="test", bootstrap=False
    )
    assert created["ticker"] == "ZZTOP"
    assert created["group"] == "n"
    assert created["created"] is True

    try:
        add_notebook_ticker("ZZTOP", "2", path=ov, bootstrap=False)
        assert False, "expected duplicate error"
    except ValueError as e:
        assert "уже" in str(e).lower() or "ZZTOP" in str(e)

    moved = set_notebook_ticker_group("ZZTOP", "2", path=ov, updated_by="test")
    assert moved["group"] == "2"
    assert moved["from_group"] == "n"

    # Existing base ticker move (MSFT in notebook_data)
    moved_msft = set_notebook_ticker_group("MSFT", "3", path=ov, updated_by="test")
    assert moved_msft["group"] == "3"

    base = {
        "MSFT": {"sym": "MSFT", "group": "1", "levels": {"buyDip": 350, "sell": 450}},
    }
    ov_data = {
        "tickers": {
            "MSFT": {"group": "3"},
            "ZZTOP": {
                "is_new": True,
                "group": "2",
                "sym": "ZZTOP",
                "name": "ZZ Top Inc",
            },
        }
    }
    merged = apply_ticker_overrides(base, ov_data)
    assert merged["MSFT"]["group"] == "3"
    assert merged["ZZTOP"]["group"] == "2"
    assert merged["ZZTOP"]["is_new"] is True
    assert merged["ZZTOP"]["levels"]["buyDip"] is None


def test_bootstrap_notebook_ticker_resources(monkeypatch):
    from services import trading_notebook as tn

    class _Eng:
        def dispose(self):
            return None

    monkeypatch.setattr(
        "sqlalchemy.create_engine",
        lambda *a, **k: _Eng(),
    )
    monkeypatch.setattr(
        "config_loader.get_database_url",
        lambda: "postgresql://x",
    )
    monkeypatch.setattr(
        "update_prices.update_ticker_prices",
        lambda engine, ticker, days_back=30, force_days_back=None: 42,
    )
    monkeypatch.setattr(
        "services.seeking_alpha_finance.rapidapi_key",
        lambda: "k",
    )
    monkeypatch.setattr(
        "services.seeking_alpha_finance.fetch_and_save_sa_news",
        lambda tickers, **kw: {"items": [{"id": 1}], "kb_inserted": 1, "errors": {}},
    )
    monkeypatch.setattr(
        "services.ticker_news_merge_fetcher.fetch_yahoo_news",
        lambda *a, **k: [{"id": "y1"}],
    )
    monkeypatch.setattr(
        "services.ticker_news_merge_fetcher.fetch_marketaux_news",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "services.ticker_news_merge_fetcher.merge_articles",
        lambda *a, **k: [{"id": "y1"}],
    )
    monkeypatch.setattr(
        "services.ticker_news_merge_fetcher.save_articles_to_kb",
        lambda articles: 1,
    )

    out = tn.bootstrap_notebook_ticker_resources("ABC")
    assert out["ticker"] == "ABC"
    assert out["quotes"]["ok"] is True and out["quotes"]["rows_upserted"] == 42
    assert out["news_sa"]["ok"] is True and out["news_sa"]["kb_inserted"] == 1
    assert out["news_yahoo"]["ok"] is True and out["news_yahoo"]["kb_inserted"] == 1
    assert out["ok"] is True


def test_sentiment_label_and_kb_agg(monkeypatch):
    from services import trading_notebook as tn

    assert tn._sentiment_label_01(0.72) == "bullish"
    assert tn._sentiment_label_01(0.3) == "bearish"
    assert tn._sentiment_label_01(0.5) == "neutral"
    assert tn._sentiment_label_01(None) == "—"

    fake = [
        {
            "ticker": "MU",
            "title": "MU up",
            "link": "https://example.com/1",
            "src": "Seeking Alpha Finance",
            "publishOn": "2026-07-27T12:00:00",
            "sentiment_score": 0.8,
        },
        {
            "ticker": "MU",
            "title": "MU flat",
            "link": "",
            "src": "Yahoo",
            "publishOn": "2026-07-26T12:00:00",
            "sentiment_score": None,
        },
        {
            "ticker": "MU",
            "title": "MU soft",
            "link": "https://example.com/2",
            "src": "Yahoo",
            "publishOn": "2026-07-25T12:00:00",
            "sentiment_score": 0.4,
        },
    ]

    monkeypatch.setattr(
        "services.seeking_alpha_finance.load_kb_news_items",
        lambda *a, **k: fake,
    )
    out = tn.get_ticker_kb_news_sentiment("mu", lookback_hours=48, limit=10)
    assert out["ticker"] == "MU"
    assert out["news_count"] == 3
    assert out["scored_count"] == 2
    assert out["avg_sentiment"] == 0.6
    assert out["label"] == "bullish"
    assert len(out["articles"]) == 3


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


def test_live_ndx_oil_injected(monkeypatch):
    from services import trading_notebook as tn

    monkeypatch.setattr(
        tn,
        "fetch_vix_snapshot",
        lambda: {
            "lbl": "VIX",
            "st": "17",
            "state": "ok",
            "live": True,
            "value": 17.0,
            "metric": "vix",
        },
    )
    monkeypatch.setattr(
        tn,
        "fetch_ndx_snapshot",
        lambda: {
            "lbl": "NDX",
            "st": "22000 · дн. -0.4%",
            "state": "ok",
            "live": True,
            "value": 22000.0,
            "chg_pct": -0.4,
            "metric": "ndx",
        },
    )
    monkeypatch.setattr(
        tn,
        "fetch_oil_snapshot",
        lambda: {
            "lbl": "Нефть (геополитика)",
            "st": "$78 · дн. +2.5%",
            "state": "mid",
            "live": True,
            "value": 78.0,
            "chg_pct": 2.5,
            "metric": "oil",
        },
    )
    monkeypatch.setattr(tn, "_fed_hint_from_digest", lambda d: None)

    base = {
        "MU": {
            "sym": "MU",
            "env": [
                {"lbl": "VIX", "state": "mid", "st": "stub"},
                {"lbl": "Понижения таргетов (вне earnings)", "state": "mid", "st": "следить"},
            ],
        }
    }
    out = tn.apply_live_env_to_tickers(base, digest={})
    env = out["MU"]["env"]
    labels = [e["lbl"] for e in env]
    assert "VIX" in labels and "NDX" in labels
    assert any("Нефть" in x for x in labels)
    oil = next(e for e in env if "Нефть" in e["lbl"])
    assert oil["state"] == "mid" and oil["live"] is True
    pt = next(e for e in env if "таргет" in e["lbl"].lower())
    assert pt["state"] == "ok"  # placeholder mid → ok


def test_oil_spike_is_stress(monkeypatch):
    from services import trading_notebook as tn

    monkeypatch.setattr(
        tn,
        "fetch_closes",
        lambda tickers, **kw: {
            "CL=F": {"close": 85.0, "chg_pct": 4.0, "chg": 3.2, "source": "test", "asof": "2026-07-27"}
        },
    )
    oil = tn.fetch_oil_snapshot()
    assert oil is not None
    assert oil["state"] == "bad"
    assert oil["metric"] == "oil"


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


def test_refresh_houses_from_stockanalysis_writes_overlay(tmp_path: Path):
    from dataclasses import dataclass

    from services.stockanalysis_ratings import (
        AnalystBundle,
        AnalystConsensus,
        AnalystRating,
        AnalystRatingCounts,
    )
    from services.trading_notebook import refresh_ticker_houses_from_stockanalysis

    @dataclass
    class _FakeClient:
        def get_analyst_bundle(self, ticker: str) -> AnalystBundle:
            return AnalystBundle(
                ticker=ticker.upper(),
                ratings=[
                    AnalystRating(
                        date="2026-07-24",
                        firm="Goldman Sachs",
                        position="Buy",
                        action="Maintains",
                        price_target="$2200",
                        upside_downside="+10%",
                    )
                ],
                counts=AnalystRatingCounts(
                    buy=20, hold=3, sell=1, total=24, consensus="Buy"
                ),
                consensus=AnalystConsensus(
                    rating="Buy",
                    price_target=2100.0,
                    low=1000.0,
                    high=3250.0,
                    count=24,
                ),
                source="test",
                asof="2026-07-27T00:00:00Z",
            )

    ov = tmp_path / "ticker_overrides.json"
    out = refresh_ticker_houses_from_stockanalysis(
        "MSFT",
        limit=5,
        updated_by="test",
        path=ov,
        client=_FakeClient(),
    )
    assert out["houses"][0]["firm"] == "Goldman Sachs"
    assert out["consensus"]["rating"] == "Buy"
    assert out["counts"]["buy"] == 20
    merged = apply_ticker_overrides(
        {"MSFT": {"houses": [], "consensus": {"rating": "—"}}},
        {
            "tickers": {
                "MSFT": {
                    "houses": out["houses"],
                    "consensus": out["consensus"],
                    "houseNote": out["houseNote"],
                    "houses_source": "stockanalysis",
                }
            }
        },
    )
    assert merged["MSFT"]["houses_override"] is True
    assert merged["MSFT"]["houses"][0]["pt"] == "$2200"
    assert "Buy 20" in (merged["MSFT"].get("houseNote") or out["houseNote"])
