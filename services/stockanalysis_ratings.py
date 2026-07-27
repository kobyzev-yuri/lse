"""
Analyst ratings / recommendation counts from stockanalysis.com.

Official public API: none. Pages are SvelteKit SSR; we read the
undocumented `__data.json` payloads (same data the site hydrates with).

Endpoints used:
  /stocks/{ticker}/ratings/__data.json
  /stocks/{ticker}/forecast/__data.json

HTML table scraping (legacy Go adapter) is kept as a fallback only.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from services.http_outbound import outbound_session

logger = logging.getLogger(__name__)

BASE_URL = "https://stockanalysis.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class AnalystRating:
    date: str  # YYYY-MM-DD
    firm: Optional[str] = None
    position: Optional[str] = None  # rating_new
    action: Optional[str] = None
    price_target: Optional[str] = None  # display string
    upside_downside: Optional[str] = None
    pt_now: Optional[float] = None
    pt_old: Optional[float] = None
    analyst: Optional[str] = None


@dataclass
class AnalystRatingCounts:
    buy: int = 0
    hold: int = 0
    sell: int = 0
    strong_buy: int = 0
    strong_sell: int = 0
    consensus: Optional[str] = None
    score: Optional[float] = None
    total: Optional[int] = None


@dataclass
class AnalystConsensus:
    rating: Optional[str] = None
    price_target: Optional[float] = None
    currency: str = "USD"
    count: Optional[int] = None
    low: Optional[float] = None
    high: Optional[float] = None


@dataclass
class AnalystBundle:
    ticker: str
    ratings: List[AnalystRating] = field(default_factory=list)
    counts: AnalystRatingCounts = field(default_factory=AnalystRatingCounts)
    consensus: AnalystConsensus = field(default_factory=AnalystConsensus)
    last_price: Optional[float] = None
    source: str = "stockanalysis"
    asof: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "ratings": [asdict(r) for r in self.ratings],
            "counts": asdict(self.counts),
            "consensus": asdict(self.consensus),
            "last_price": self.last_price,
            "source": self.source,
            "asof": self.asof,
        }


def _clean_text(value: str) -> str:
    return " ".join((value or "").split())


def _na_to_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = _clean_text(value)
    if not s or s.lower() in ("n/a", "na", "—", "-", "–"):
        return None
    return s


def _fmt_money(value: Optional[float], currency: str = "USD") -> Optional[str]:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    prefix = "$" if currency.upper() == "USD" else f"{currency} "
    if abs(v - round(v)) < 1e-9:
        return f"{prefix}{int(round(v))}"
    return f"{prefix}{v:,.2f}"


def _fmt_pct(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def _upside_pct(pt: Optional[float], last: Optional[float]) -> Optional[float]:
    if pt is None or last is None or last == 0:
        return None
    try:
        return (float(pt) / float(last) - 1.0) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _resolve_svelte_ref(data: Sequence[Any], ref: Any, seen: Optional[Dict[int, Any]] = None) -> Any:
    """Resolve SvelteKit / devalue-style integer pointers into `data`."""
    if seen is None:
        seen = {}
    if not isinstance(ref, int):
        return ref
    if ref in seen:
        return seen[ref]
    if ref < 0 or ref >= len(data):
        return None
    seen[ref] = None  # cycle guard
    item = data[ref]
    if item is None or isinstance(item, (str, int, float, bool)):
        seen[ref] = item
        return item
    if isinstance(item, list):
        out = [_resolve_svelte_ref(data, x, seen) for x in item]
        seen[ref] = out
        return out
    if isinstance(item, dict):
        out = {k: _resolve_svelte_ref(data, v, seen) for k, v in item.items()}
        seen[ref] = out
        return out
    seen[ref] = item
    return item


def _page_data_node(payload: Dict[str, Any]) -> List[Any]:
    nodes = payload.get("nodes") or []
    # Layout / stock shell / page: page payload is usually the last data node.
    for node in reversed(nodes):
        if not isinstance(node, dict) or node.get("type") != "data":
            continue
        data = node.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data
    raise ValueError("stockanalysis __data.json: page data node not found")


def _resolve_root(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = _page_data_node(payload)
    root = _resolve_svelte_ref(data, 0)
    if not isinstance(root, dict):
        raise ValueError("stockanalysis __data.json: root is not an object")
    return root


def _quote_last_from_shell(payload: Dict[str, Any]) -> Optional[float]:
    """Best-effort last/close from the shared stock shell node."""
    nodes = payload.get("nodes") or []
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") != "data":
            continue
        data = node.get("data")
        if not isinstance(data, list) or not data:
            continue
        try:
            root = _resolve_svelte_ref(data, 0)
        except Exception:
            continue
        if not isinstance(root, dict):
            continue
        info = root.get("info")
        if not isinstance(info, dict):
            continue
        quote = info.get("quote")
        if not isinstance(quote, dict):
            continue
        # c = day change, p = last/price, cl = previous close — never use c.
        for key in ("p", "cl"):
            val = quote.get(key)
            if isinstance(val, (int, float)) and float(val) > 0:
                return float(val)
    return None


def _target_display(
    pt_now: Optional[float],
    pt_old: Optional[float],
    currency: str = "USD",
) -> Optional[str]:
    now_s = _fmt_money(pt_now, currency)
    old_s = _fmt_money(pt_old, currency)
    if now_s and old_s and pt_old is not None and pt_now is not None and abs(pt_old - pt_now) > 1e-9:
        return f"{old_s} → {now_s}"
    return now_s


def _rating_from_raw(
    raw: Dict[str, Any],
    *,
    last_price: Optional[float],
    currency: str = "USD",
) -> AnalystRating:
    pt_now = raw.get("pt_now")
    pt_old = raw.get("pt_old")
    if not isinstance(pt_now, (int, float)):
        pt_now = None
    else:
        pt_now = float(pt_now)
    if not isinstance(pt_old, (int, float)):
        pt_old = None
    else:
        pt_old = float(pt_old)

    upside = _fmt_pct(_upside_pct(pt_now, last_price))
    return AnalystRating(
        date=str(raw.get("date") or "")[:10],
        firm=_na_to_none(raw.get("firm")),
        position=_na_to_none(raw.get("rating_new")),
        action=_na_to_none(raw.get("action_rt")),
        price_target=_target_display(pt_now, pt_old, currency),
        upside_downside=upside,
        pt_now=pt_now,
        pt_old=pt_old,
        analyst=_na_to_none(raw.get("analyst")),
    )


def parse_ratings_payload(
    payload: Dict[str, Any],
    *,
    last_price: Optional[float] = None,
) -> Tuple[List[AnalystRating], AnalystConsensus, Optional[float]]:
    root = _resolve_root(payload)
    if last_price is None:
        last_price = _quote_last_from_shell(payload)

    widget = root.get("widget") if isinstance(root.get("widget"), dict) else {}
    currency = str(widget.get("currency") or "USD")
    consensus = AnalystConsensus(
        rating=_na_to_none(widget.get("consensus")),
        price_target=float(widget["price_target"])
        if isinstance(widget.get("price_target"), (int, float))
        else None,
        currency=currency,
        count=int(widget["count"]) if isinstance(widget.get("count"), (int, float)) else None,
    )

    ratings_raw = root.get("ratings") or []
    ratings: List[AnalystRating] = []
    if isinstance(ratings_raw, list):
        for item in ratings_raw:
            if isinstance(item, dict):
                ratings.append(_rating_from_raw(item, last_price=last_price, currency=currency))
    return ratings, consensus, last_price


def parse_forecast_counts(payload: Dict[str, Any]) -> Tuple[AnalystRatingCounts, AnalystConsensus]:
    root = _resolve_root(payload)
    cur = root.get("currentRatings") if isinstance(root.get("currentRatings"), dict) else {}

    def _i(*keys: str) -> int:
        for k in keys:
            v = cur.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    strong_buy = _i("strongBuy", "strong_buy")
    buy = _i("buy")
    hold = _i("hold")
    sell = _i("sell")
    strong_sell = _i("strongSell", "strong_sell")
    total = cur.get("count")
    if not isinstance(total, (int, float)):
        total = strong_buy + buy + hold + sell + strong_sell

    counts = AnalystRatingCounts(
        buy=strong_buy + buy,
        hold=hold,
        sell=sell + strong_sell,
        strong_buy=strong_buy,
        strong_sell=strong_sell,
        consensus=_na_to_none(cur.get("consensus")),
        score=float(cur["score"]) if isinstance(cur.get("score"), (int, float)) else None,
        total=int(total) if total is not None else None,
    )

    pt_block = root.get("priceTargets") if isinstance(root.get("priceTargets"), dict) else {}
    consensus = AnalystConsensus(
        rating=counts.consensus,
        price_target=float(pt_block["average"])
        if isinstance(pt_block.get("average"), (int, float))
        else (
            float(pt_block["consensus"])
            if isinstance(pt_block.get("consensus"), (int, float))
            else None
        ),
        currency="USD",
        count=counts.total,
        low=float(pt_block["low"]) if isinstance(pt_block.get("low"), (int, float)) else None,
        high=float(pt_block["high"]) if isinstance(pt_block.get("high"), (int, float)) else None,
    )
    # Some payloads nest low/high differently — scan common keys.
    if consensus.low is None or consensus.high is None:
        for key, attr in (("low", "low"), ("high", "high"), ("min", "low"), ("max", "high")):
            v = pt_block.get(key)
            if isinstance(v, (int, float)) and getattr(consensus, attr) is None:
                setattr(consensus, attr, float(v))
    return counts, consensus


# --- HTML fallback (legacy Go selectors) ------------------------------------


def _parse_trading_date(value: str) -> str:
    value = _clean_text(value)
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"invalid date {value!r}")


def parse_ratings_html(html: str, *, last_price: Optional[float] = None) -> List[AnalystRating]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.rating-table")
    if table is None:
        raise ValueError("rating table not found")

    ratings: List[AnalystRating] = []
    for row in table.select("tbody tr"):
        desktop = row.select("td.desktop-only")
        if len(desktop) != 5:
            raise ValueError(f"expected 5 desktop cells, got {len(desktop)}")
        tds = row.select("td")
        if not tds:
            continue
        date_s = _parse_trading_date(tds[-1].get_text(" ", strip=True))
        firm = _na_to_none(desktop[0].get_text(" ", strip=True))
        position = _na_to_none(desktop[1].get_text(" ", strip=True))
        action = _na_to_none(desktop[2].get_text(" ", strip=True))
        pt = _na_to_none(desktop[3].get_text(" ", strip=True))
        upside = _na_to_none(desktop[4].get_text(" ", strip=True))
        ratings.append(
            AnalystRating(
                date=date_s,
                firm=firm,
                position=position,
                action=action,
                price_target=pt,
                upside_downside=upside,
            )
        )
    return ratings


def parse_counts_html(html: str) -> AnalystRatingCounts:
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for candidate in soup.find_all("table"):
        headers = [_clean_text(th.get_text(" ", strip=True)) for th in candidate.select("thead th")]
        if headers and headers[0] == "Rating":
            table = candidate
            break
    if table is None:
        raise ValueError("recommendation trends table not found")

    buy = hold = sell = 0
    for row in table.select("tbody tr"):
        cells = [_clean_text(td.get_text(" ", strip=True)) for td in row.select("td")]
        if len(cells) < 2:
            raise ValueError(f"expected at least 2 cells, got {len(cells)}")
        name, latest = cells[0], cells[-1]
        if name == "Total":
            continue
        try:
            value = int(latest.replace(",", ""))
        except ValueError as e:
            raise ValueError(f"invalid count {latest!r} for {name!r}") from e
        if name in ("Strong Buy", "Buy"):
            buy += value
        elif name == "Hold":
            hold = value
        elif name in ("Sell", "Strong Sell"):
            sell += value
        else:
            raise ValueError(f"unknown rating count row {name!r}")
    return AnalystRatingCounts(buy=buy, hold=hold, sell=sell, total=buy + hold + sell)


def format_analyst_report(bundle: AnalystBundle, *, limit: Optional[int] = None) -> str:
    """Human-readable report matching the notebook-style bullet list."""
    lines: List[str] = [f"Analyst ratings — {bundle.ticker.upper()}"]
    ratings = bundle.ratings if limit is None else bundle.ratings[:limit]
    if not ratings:
        lines.append("(no recent ratings)")
    for r in ratings:
        firm = r.firm or "—"
        lines.append(f"• {r.date} — {firm}")
        bits: List[str] = []
        if r.position:
            bits.append(r.position)
        if r.action:
            bits.append(r.action)
        if r.price_target:
            bits.append(f"Target: {r.price_target}")
        if r.upside_downside:
            bits.append(r.upside_downside)
        lines.append("  " + (" · ".join(bits) if bits else "—"))

    c = bundle.counts
    lines.append("")
    lines.append("Analyst rating summary")
    lines.append(f"Buy: {c.buy}")
    lines.append(f"Hold: {c.hold}")
    lines.append(f"Sell: {c.sell}")
    if bundle.consensus.rating or bundle.consensus.price_target is not None:
        pt = _fmt_money(bundle.consensus.price_target, bundle.consensus.currency or "USD")
        cons_bits = []
        if bundle.consensus.rating:
            cons_bits.append(bundle.consensus.rating)
        if pt:
            cons_bits.append(f"avg PT {pt}")
        if bundle.consensus.low is not None and bundle.consensus.high is not None:
            cons_bits.append(
                f"range {_fmt_money(bundle.consensus.low)}–{_fmt_money(bundle.consensus.high)}"
            )
        if bundle.consensus.count:
            cons_bits.append(f"n={bundle.consensus.count}")
        if cons_bits:
            lines.append("Consensus: " + " · ".join(cons_bits))
    return "\n".join(lines)


def to_notebook_houses(
    bundle: AnalystBundle,
    *,
    limit: int = 12,
) -> Dict[str, Any]:
    """Map scrape result into notebook `houses` + `consensus` shapes."""
    houses: List[Dict[str, str]] = []
    for r in bundle.ratings[:limit]:
        if not r.firm:
            continue
        quote_bits = [x for x in (r.action, r.upside_downside) if x]
        houses.append(
            {
                "firm": r.firm,
                "rate": r.position or "—",
                "pt": r.price_target or "—",
                "quote": " · ".join(quote_bits) if quote_bits else (r.date or ""),
                "tac": f"<b>Источник:</b> stockanalysis · {r.date}",
            }
        )

    cons = bundle.consensus
    counts = bundle.counts
    rating = cons.rating or counts.consensus or "—"
    pt = _fmt_money(cons.price_target) or "—"
    low = _fmt_money(cons.low) or "—"
    high = _fmt_money(cons.high) or "—"
    n = counts.total or cons.count
    n_s = f"{n} аналитиков" if n else "—"
    today = date.today().isoformat()
    return {
        "houses": houses,
        "consensus": {
            "rating": rating,
            "pt": pt,
            "low": low,
            "high": high,
            "n": n_s,
            "upd": f"stockanalysis {today}",
        },
        "counts": asdict(counts),
    }


class StockAnalysisClient:
    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or outbound_session("STOCKANALYSIS_USE_SYSTEM_PROXY")
        self.session.headers.setdefault("User-Agent", _UA)

    def stock_url(self, ticker: str, page: str, *, data_json: bool = False) -> str:
        path = f"/stocks/{ticker.strip().lower()}/{page.strip('/')}"
        if data_json:
            path = path.rstrip("/") + "/__data.json"
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _get_json(self, url: str) -> Dict[str, Any]:
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"stockanalysis status {resp.status_code} for {url}")
        payload = resp.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"unexpected JSON type from {url}")
        return payload

    def _get_html(self, url: str) -> str:
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"stockanalysis status {resp.status_code} for {url}")
        return resp.text

    def get_analyst_ratings(self, ticker: str) -> List[AnalystRating]:
        bundle = self.get_analyst_bundle(ticker, include_counts=False)
        return bundle.ratings

    def get_analyst_counts(self, ticker: str) -> AnalystRatingCounts:
        bundle = self.get_analyst_bundle(ticker, include_ratings=False)
        return bundle.counts

    def get_analyst_bundle(
        self,
        ticker: str,
        *,
        include_ratings: bool = True,
        include_counts: bool = True,
    ) -> AnalystBundle:
        sym = ticker.strip().upper()
        slug = sym.lower()
        ratings: List[AnalystRating] = []
        counts = AnalystRatingCounts()
        consensus = AnalystConsensus()
        last_price: Optional[float] = None
        source = "stockanalysis:__data.json"

        if include_ratings:
            try:
                payload = self._get_json(self.stock_url(slug, "ratings", data_json=True))
                ratings, consensus, last_price = parse_ratings_payload(payload)
            except Exception as e:
                logger.warning("ratings __data.json failed for %s: %s — HTML fallback", sym, e)
                html = self._get_html(self.stock_url(slug, "ratings"))
                ratings = parse_ratings_html(html, last_price=last_price)
                source = "stockanalysis:html"

        if include_counts:
            try:
                payload = self._get_json(self.stock_url(slug, "forecast", data_json=True))
                counts, fc_cons = parse_forecast_counts(payload)
                if last_price is None:
                    last_price = _quote_last_from_shell(payload)
                # Prefer forecast corridor / consensus when richer.
                if fc_cons.rating and not consensus.rating:
                    consensus.rating = fc_cons.rating
                if fc_cons.price_target is not None:
                    consensus.price_target = fc_cons.price_target
                if fc_cons.low is not None:
                    consensus.low = fc_cons.low
                if fc_cons.high is not None:
                    consensus.high = fc_cons.high
                if fc_cons.count is not None:
                    consensus.count = fc_cons.count
                if counts.consensus and not consensus.rating:
                    consensus.rating = counts.consensus
            except Exception as e:
                logger.warning("forecast __data.json failed for %s: %s — HTML fallback", sym, e)
                html = self._get_html(self.stock_url(slug, "forecast"))
                counts = parse_counts_html(html)
                source = "stockanalysis:html"

        # Recompute upside if we learned last_price after ratings parse.
        if last_price is not None:
            for r in ratings:
                if r.upside_downside is None and r.pt_now is not None:
                    r.upside_downside = _fmt_pct(_upside_pct(r.pt_now, last_price))

        return AnalystBundle(
            ticker=sym,
            ratings=ratings,
            counts=counts,
            consensus=consensus,
            last_price=last_price,
            source=source,
            asof=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        )


def fetch_and_format(ticker: str, *, limit: Optional[int] = None) -> str:
    client = StockAnalysisClient()
    bundle = client.get_analyst_bundle(ticker)
    return format_analyst_report(bundle, limit=limit)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="StockAnalysis analyst ratings → text report")
    parser.add_argument("tickers", nargs="+", help="Ticker symbols, e.g. AAPL MSFT SNDK")
    parser.add_argument("--limit", type=int, default=None, help="Max ratings lines per ticker")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text")
    parser.add_argument("--houses", action="store_true", help="Print notebook houses/consensus JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    client = StockAnalysisClient()
    for i, t in enumerate(args.tickers):
        bundle = client.get_analyst_bundle(t)
        if i:
            print()
        if args.json:
            import json

            print(json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2))
        elif args.houses:
            import json

            print(json.dumps(to_notebook_houses(bundle, limit=args.limit or 12), ensure_ascii=False, indent=2))
        else:
            print(format_analyst_report(bundle, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
