"""
Polygon (Massive) Options Snapshot API — цепочка опционов по underlying.

Endpoint: GET /v3/snapshot/options/{underlyingAsset}
Документация: https://polygon.io/docs/options/get_v3_snapshot_options__underlyingasset

Требует POLYGON_API_KEY и подписку Options (Starter+ для OI/volume/Greeks).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

POLYGON_API_BASE = "https://api.polygon.io"


def _api_key() -> str:
    from config_loader import get_config_value

    return (get_config_value("POLYGON_API_KEY", "") or "").strip()


def polygon_options_available() -> bool:
    return bool(_api_key())


def _format_polygon_error(resp: requests.Response) -> str:
    """Человекочитаемая ошибка Polygon (без утечки apiKey в URL)."""
    try:
        data = resp.json()
        msg = (data.get("message") or data.get("status") or "").strip()
        if msg:
            if resp.status_code == 403 and "NOT_AUTHORIZED" in str(data.get("status", "")).upper():
                return (
                    f"{msg} "
                    "(ключ валиден, но нет продукта Options на этом аккаунте — "
                    "Stocks и Options оплачиваются отдельно: massive.com/pricing?product=options)"
                )
            return msg
    except (ValueError, TypeError):
        pass
    return f"HTTP {resp.status_code}"


def fetch_option_expiration_dates(ticker: str, *, limit: int = 1000) -> List[str]:
    """Список дат экспирации из reference API (легче полного snapshot)."""
    key = _api_key()
    if not key:
        return []
    underlying = (ticker or "").strip().upper()
    url = f"{POLYGON_API_BASE}/v3/reference/options/contracts"
    params = {
        "apiKey": key,
        "underlying_ticker": underlying,
        "limit": min(limit, 1000),
        "sort": "expiration_date",
        "order": "asc",
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        if not resp.ok:
            logger.warning("Polygon expirations %s: %s", underlying, _format_polygon_error(resp))
            return []
        data = resp.json()
    except requests.RequestException as e:
        logger.warning("Polygon expirations %s: %s", underlying, e)
        return []
    seen: set[str] = set()
    out: List[str] = []
    for row in data.get("results") or []:
        d = row.get("expiration_date")
        if d and d not in seen:
            seen.add(d)
            out.append(str(d))
    return sorted(out)


def fetch_options_chain_snapshot(
    ticker: str,
    *,
    expiration_date: Optional[str] = None,
    contract_type: Optional[str] = None,
    strike_price_gte: Optional[float] = None,
    strike_price_lte: Optional[float] = None,
    limit: int = 250,
    max_pages: int = 20,
) -> Dict[str, Any]:
    """
    Загружает snapshot цепочки (все контракты на дату экспирации / диапазон страйков).
    Пагинация через next_url.
    """
    key = _api_key()
    if not key:
        return {
            "status": "error",
            "error": "POLYGON_API_KEY не задан в config.env",
            "contracts": [],
        }

    underlying = (ticker or "").strip().upper()
    params: Dict[str, Any] = {"apiKey": key, "limit": min(max(int(limit), 1), 250)}
    if expiration_date:
        params["expiration_date"] = expiration_date
    if contract_type:
        params["contract_type"] = contract_type.strip().lower()
    if strike_price_gte is not None:
        params["strike_price.gte"] = strike_price_gte
    if strike_price_lte is not None:
        params["strike_price.lte"] = strike_price_lte

    url = f"{POLYGON_API_BASE}/v3/snapshot/options/{underlying}"
    contracts: List[Dict[str, Any]] = []
    pages = 0
    last_status = "OK"

    while url and pages < max_pages:
        pages += 1
        try:
            if pages == 1:
                resp = requests.get(url, params=params, timeout=45)
            else:
                sep = "&" if "?" in url else "?"
                resp = requests.get(f"{url}{sep}apiKey={key}", timeout=45)
            if not resp.ok:
                err = _format_polygon_error(resp)
                logger.warning("Polygon options chain %s: %s", underlying, err)
                return {
                    "status": "error",
                    "error": err,
                    "http_status": resp.status_code,
                    "contracts": contracts,
                    "underlying": underlying,
                }
            data = resp.json()
        except requests.RequestException as e:
            logger.warning("Polygon options chain %s: %s", underlying, e)
            return {
                "status": "error",
                "error": str(e),
                "contracts": contracts,
                "underlying": underlying,
            }

        last_status = (data.get("status") or "OK").upper()
        for row in data.get("results") or []:
            norm = _normalize_contract_row(row, underlying)
            if norm:
                contracts.append(norm)

        next_url = data.get("next_url")
        url = next_url if next_url else None
        if url and "apiKey=" not in url:
            pass  # append on next iteration

    spot = None
    for c in contracts:
        if c.get("underlying_price") is not None:
            spot = c["underlying_price"]
            break

    return {
        "status": "ok" if contracts else "empty",
        "polygon_status": last_status,
        "underlying": underlying,
        "underlying_price": spot,
        "expiration_date": expiration_date,
        "contract_count": len(contracts),
        "contracts": contracts,
        "source": "polygon",
    }


def _normalize_contract_row(row: Dict[str, Any], underlying: str) -> Optional[Dict[str, Any]]:
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    day = row.get("day") if isinstance(row.get("day"), dict) else {}
    ua = row.get("underlying_asset") if isinstance(row.get("underlying_asset"), dict) else {}
    lq = row.get("last_quote") if isinstance(row.get("last_quote"), dict) else {}
    lt = row.get("last_trade") if isinstance(row.get("last_trade"), dict) else {}

    strike = details.get("strike_price")
    exp = details.get("expiration_date")
    ctype = (details.get("contract_type") or "").strip().lower()
    if strike is None or not exp or ctype not in ("call", "put"):
        return None

    try:
        strike_f = float(strike)
    except (TypeError, ValueError):
        return None

    vol = day.get("volume")
    if vol is None:
        vol = row.get("volume")
    oi = row.get("open_interest")
    if oi is None:
        oi = day.get("open_interest")

    bid = lq.get("bid")
    ask = lq.get("ask")
    last = lt.get("price") if lt else None
    if last is None:
        last = day.get("close")

    ua_price = ua.get("price")
    try:
        ua_price_f = float(ua_price) if ua_price is not None else None
    except (TypeError, ValueError):
        ua_price_f = None

    return {
        "ticker": details.get("ticker") or row.get("ticker"),
        "underlying": underlying,
        "underlying_price": ua_price_f,
        "expiration_date": str(exp),
        "strike": strike_f,
        "contract_type": ctype,
        "volume": int(vol) if vol is not None else 0,
        "open_interest": int(oi) if oi is not None else 0,
        "bid": float(bid) if bid is not None else None,
        "ask": float(ask) if ask is not None else None,
        "last": float(last) if last is not None else None,
        "implied_volatility": row.get("implied_volatility"),
        "delta": (row.get("greeks") or {}).get("delta") if isinstance(row.get("greeks"), dict) else None,
    }


def list_expiration_dates(contracts: List[Dict[str, Any]]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for c in contracts:
        d = c.get("expiration_date")
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return sorted(out)
