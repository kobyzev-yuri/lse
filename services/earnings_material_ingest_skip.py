"""Permanent skip rules for earnings material ingest (repeat failures)."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

# URLs that repeatedly fail parse (ARM media-server shells, etc.) — do not re-fetch.
_GLOBAL_URL_SUBSTRINGS: tuple[str, ...] = (
    "edge.media-server.com/mmc/",
)

_SYMBOL_URL_SUBSTRINGS: dict[str, tuple[str, ...]] = {
    "ARM": (
        "edge.media-server.com/",
    ),
}


def permanent_ingest_skip_reason(
    *,
    symbol: str,
    source_url: str,
    parse_status: str | None = None,
    parse_error: str | None = None,
) -> str | None:
    """
    Return skip reason if URL should not be fetched again.

    Applies to known junk hosts and repeat short_text failures on blocked patterns.
    """
    sym = str(symbol or "").strip().upper()
    url = str(source_url or "").strip()
    if not url:
        return None
    url_l = url.lower()
    host = (urlparse(url).netloc or "").lower()

    for sub in _GLOBAL_URL_SUBSTRINGS:
        if sub in url_l:
            return f"ingest_skip:pattern:{sub}"

    for sub in _SYMBOL_URL_SUBSTRINGS.get(sym, ()):
        if sub in url_l or sub in host:
            return f"ingest_skip:symbol_pattern:{sym}:{sub}"

    err = str(parse_error or "")
    if str(parse_status or "") == "failed" and err.startswith("short_text:"):
        if url_l.endswith(".pdf") or ".pdf?" in url_l:
            return f"ingest_skip:short_text_pdf:{sym or 'unknown'}"
        for sub in _GLOBAL_URL_SUBSTRINGS:
            if sub in url_l:
                return f"ingest_skip:repeat_short_text:{sub}"
        for sub in _SYMBOL_URL_SUBSTRINGS.get(sym, ()):
            if sub in url_l:
                return f"ingest_skip:repeat_short_text:{sym}"

    return None


def row_should_skip_ingest(row: dict[str, Any]) -> str | None:
    return permanent_ingest_skip_reason(
        symbol=str(row.get("symbol") or ""),
        source_url=str(row.get("source_url") or ""),
        parse_status=str(row.get("parse_status") or ""),
        parse_error=str(row.get("parse_error") or "") if row.get("parse_error") else None,
    )
