"""Unit tests for stockanalysis ratings parser (offline fixtures)."""

from __future__ import annotations

from services.stockanalysis_ratings import (
    format_analyst_report,
    parse_counts_html,
    parse_forecast_counts,
    parse_ratings_html,
    parse_ratings_payload,
    to_notebook_houses,
    AnalystBundle,
    AnalystRating,
    AnalystRatingCounts,
    AnalystConsensus,
)


def _ratings_fixture() -> dict:
    # Minimal SvelteKit-shaped payload mirroring live /ratings/__data.json
    data = [
        {"widget": 1, "ratings": 2},
        {"all": 20},
        [7, 14],
        2,
        "Buy",
        500.0,
        "USD",
        {
            "action_rt": 8,
            "pt_now": 9,
            "pt_old": 10,
            "firm": 11,
            "date": 12,
            "rating_new": 13,
        },
        "Maintains",
        500.0,
        400.0,
        "Barclays",
        "2026-07-24",
        "Buy",
        {
            "action_rt": 15,
            "pt_now": 16,
            "pt_old": 17,
            "firm": 18,
            "date": 19,
            "rating_new": 13,
        },
        "Reiterates",
        502.0,
        None,
        "Morgan Stanley",
        "2026-07-14",
        {"count": 3, "consensus": 4, "price_target": 5, "currency": 6},
    ]
    return {
        "type": "data",
        "nodes": [
            {"type": "data", "data": [{"session": 1}, None]},
            {
                "type": "data",
                "data": [
                    {"info": 1},
                    {
                        "quote": 2,
                        "symbol": 3,
                        "ticker": 3,
                    },
                    {"c": 5, "p": 4, "cl": 4},
                    "NVDA",
                    382.0,
                    -8.5,
                ],
            },
            {"type": "data", "data": data},
        ],
    }


def _forecast_fixture() -> dict:
    data = [
        {"currentRatings": 1, "priceTargets": 2},
        {
            "strongBuy": 3,
            "buy": 4,
            "hold": 5,
            "sell": 6,
            "strongSell": 6,
            "consensus": 7,
            "score": 8,
            "count": 9,
        },
        {"avg": 10, "low": 11, "high": 12, "numPriceTargets": 9},
        10,
        34,
        4,
        0,
        "Buy",
        1.2,
        48,
        556.75,
        400.0,
        870.0,
    ]
    return {"type": "data", "nodes": [{"type": "data", "data": data}]}


def test_parse_ratings_payload_resolves_firms_and_targets():
    ratings, consensus, last = parse_ratings_payload(_ratings_fixture())
    assert last == 382.0
    assert consensus.rating == "Buy"
    assert consensus.price_target == 500.0
    assert consensus.count == 2
    assert len(ratings) == 2
    assert ratings[0].firm == "Barclays"
    assert ratings[0].action == "Maintains"
    assert ratings[0].price_target == "$400 → $500"
    assert ratings[0].upside_downside == "+30.89%"  # 500/382 - 1
    assert ratings[1].firm == "Morgan Stanley"
    assert ratings[1].price_target == "$502"


def test_parse_forecast_counts_aggregates_buy_bucket():
    counts, cons = parse_forecast_counts(_forecast_fixture())
    assert counts.strong_buy == 10
    assert counts.buy == 44  # 10+34
    assert counts.hold == 4
    assert counts.sell == 0
    assert counts.consensus == "Buy"
    assert cons.low == 400.0
    assert cons.high == 870.0
    assert cons.price_target == 556.75


def test_format_analyst_report_matches_bullet_style():
    bundle = AnalystBundle(
        ticker="NVDA",
        ratings=[
            AnalystRating(
                date="2026-07-24",
                firm="Barclays",
                position="Buy",
                action="Maintains",
                price_target="$500",
                upside_downside="+30.92%",
            ),
            AnalystRating(
                date="2026-07-14",
                firm="Morgan Stanley",
                position="Buy",
                action="Reiterates",
                price_target="$502",
                upside_downside="+31.44%",
            ),
        ],
        counts=AnalystRatingCounts(buy=44, hold=4, sell=0),
        consensus=AnalystConsensus(rating="Buy", price_target=556.75, count=48),
    )
    text = format_analyst_report(bundle)
    assert "• 2026-07-24 — Barclays" in text
    assert "Buy · Maintains · Target: $500 · +30.92%" in text
    assert "Buy: 44" in text
    assert "Hold: 4" in text
    assert "Sell: 0" in text


def test_to_notebook_houses_shape():
    bundle = AnalystBundle(
        ticker="MSFT",
        ratings=[
            AnalystRating(
                date="2026-07-23",
                firm="Wedbush",
                position="Outperform",
                action="Maintains",
                price_target="$625",
            )
        ],
        counts=AnalystRatingCounts(buy=40, hold=5, sell=1, total=46, consensus="Strong Buy"),
        consensus=AnalystConsensus(
            rating="Strong Buy",
            price_target=556.75,
            low=400,
            high=870,
            count=46,
        ),
    )
    out = to_notebook_houses(bundle)
    assert out["houses"][0]["firm"] == "Wedbush"
    assert out["consensus"]["rating"] == "Strong Buy"
    assert out["consensus"]["n_ratings"] == 46
    assert out["consensus"]["n_targets"] == 46
    assert "рейтинги SA" in out["consensus"]["n"]
    assert out["counts"]["pt_total"] == 46


def test_html_fallback_parsers():
    ratings_html = """
    <table class="rating-table"><tbody>
      <tr>
        <td class="desktop-only">Barclays</td>
        <td class="desktop-only">Buy</td>
        <td class="desktop-only">Maintains</td>
        <td class="desktop-only">$500</td>
        <td class="desktop-only">+30.92%</td>
        <td class="mobile-only">x</td>
        <td>Jul 24, 2026</td>
      </tr>
    </tbody></table>
    """
    ratings = parse_ratings_html(ratings_html)
    assert ratings[0].firm == "Barclays"
    assert ratings[0].date == "2026-07-24"

    counts_html = """
    <table><thead><tr><th>Rating</th><th>Current</th></tr></thead>
    <tbody>
      <tr><td>Strong Buy</td><td>10</td></tr>
      <tr><td>Buy</td><td>34</td></tr>
      <tr><td>Hold</td><td>4</td></tr>
      <tr><td>Sell</td><td>0</td></tr>
      <tr><td>Strong Sell</td><td>0</td></tr>
      <tr><td>Total</td><td>48</td></tr>
    </tbody></table>
    """
    counts = parse_counts_html(counts_html)
    assert counts.buy == 44
    assert counts.hold == 4
    assert counts.sell == 0
