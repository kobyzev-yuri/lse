"""Seeking Alpha news from Google Sheet (external scraper) → knowledge_base.

Column layout (tradebook Go adapter): A time, B URL, C title, D text, E symbols.
Service account JSON + spreadsheet id via NOTEBOOK_SA_SHEET_* env.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from config_loader import get_config_value

logger = logging.getLogger(__name__)

COL_TIME = 0
COL_URL = 1
COL_TITLE = 2
COL_TEXT = 3
COL_SYMBOLS = 4

NO_SYMBOLS = "-"
DEFAULT_SHEET_ID = "15Vt-P0kffD9ERl17XxgEJgcEebJTu20UXALxzERlrzA"
DEFAULT_CREDENTIALS = "config/sa-news-reader-0540e7c65520.json"
SHEETS_SCOPE = ("https://www.googleapis.com/auth/spreadsheets.readonly",)
_TS_FMT = "%b %d, %Y, %I:%M %p"
_EASTERN_SUFFIXES = (" EST", " EDT", " ET")
_HEADER_RE = re.compile(r"^(time|date|timestamp|published)$", re.I)


def sheet_enabled() -> bool:
    raw = (get_config_value("NOTEBOOK_SA_SHEET_ENABLED", "1") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


def sheet_id() -> str:
    return (get_config_value("NOTEBOOK_SA_SHEET_ID", DEFAULT_SHEET_ID) or DEFAULT_SHEET_ID).strip()


def credentials_path() -> Path:
    raw = (get_config_value("NOTEBOOK_SA_SHEET_CREDENTIALS", "") or "").strip()
    if not raw:
        raw = DEFAULT_CREDENTIALS
    p = Path(raw)
    if p.is_absolute():
        return p
    # Relative to project root (parent of services/)
    root = Path(__file__).resolve().parent.parent
    return (root / p).resolve()


def cell(row: Sequence[Any], idx: int) -> str:
    if idx < 0 or idx >= len(row):
        return ""
    return str(row[idx] if row[idx] is not None else "").strip()


def row_complete(row: Sequence[Any]) -> bool:
    for col in range(COL_TIME, COL_SYMBOLS + 1):
        if not cell(row, col):
            return False
    return True


def parse_symbols(raw: str) -> List[str]:
    parts = str(raw or "").split(",")
    out: List[str] = []
    for p in parts:
        sym = p.strip().upper()
        if not sym or sym == NO_SYMBOLS:
            continue
        # Keep simple ticker tokens (letters/digits/./-), drop junk
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", sym):
            logger.warning("sa_sheet: dropping unparseable symbol %r", sym)
            continue
        out.append(sym)
    return out


def symbols_for_row(raw: str) -> List[str]:
    s = (raw or "").strip()
    if not s or s == NO_SYMBOLS:
        return ["MACRO"]
    tickers = parse_symbols(s)
    return tickers if tickers else ["MACRO"]


def parse_timestamp(s: str, loc: ZoneInfo | None = None) -> datetime:
    """Parse cells like 'Aug 04, 2026, 6:45 AM ET' in America/New_York → aware datetime."""
    loc = loc or ZoneInfo("America/New_York")
    trimmed = (s or "").strip()
    for suffix in _EASTERN_SUFFIXES:
        if trimmed.endswith(suffix):
            trimmed = trimmed[: -len(suffix)].rstrip()
            break
    dt = datetime.strptime(trimmed, _TS_FMT).replace(tzinfo=loc)
    return dt


def _is_header_row(row: Sequence[Any]) -> bool:
    return bool(_HEADER_RE.match(cell(row, COL_TIME)))


def parse_rows(rows: Sequence[Sequence[Any]], *, loc: ZoneInfo | None = None) -> List[Dict[str, Any]]:
    """Parse sheet rows → items (stop at first incomplete row; skip bad timestamps)."""
    loc = loc or ZoneInfo("America/New_York")
    items: List[Dict[str, Any]] = []
    for i, row in enumerate(rows):
        if i == 0 and _is_header_row(row):
            continue
        if not row_complete(row):
            break
        try:
            published = parse_timestamp(cell(row, COL_TIME), loc)
        except Exception as e:
            logger.warning("sa_sheet: skipping row %s with unparseable timestamp: %s", i + 1, e)
            continue
        url = cell(row, COL_URL)
        title = cell(row, COL_TITLE)
        text = cell(row, COL_TEXT)
        tickers = symbols_for_row(cell(row, COL_SYMBOLS))
        items.append(
            {
                "publishOn": published.isoformat(),
                "published_at": published,
                "link": url,
                "title": title,
                "summary_text": text[:4000],
                "tickers": tickers,
            }
        )
    return items


def load_sheets_service(credentials_file: Path | None = None):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    path = credentials_file or credentials_path()
    if not path.is_file():
        raise FileNotFoundError(f"SA sheet credentials not found: {path}")
    creds = service_account.Credentials.from_service_account_file(
        str(path), scopes=list(SHEETS_SCOPE)
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def fetch_sheet_values(
    *,
    spreadsheet_id: Optional[str] = None,
    credentials_file: Path | None = None,
    range_a1: str = "A:E",
) -> List[List[Any]]:
    sid = (spreadsheet_id or sheet_id()).strip()
    if not sid:
        raise ValueError("NOTEBOOK_SA_SHEET_ID is empty")
    service = load_sheets_service(credentials_file)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sid, range=range_a1)
        .execute()
    )
    values = result.get("values") or []
    return [list(r) for r in values if isinstance(r, (list, tuple))]


def fetch_sheet_items(
    *,
    spreadsheet_id: Optional[str] = None,
    credentials_file: Path | None = None,
) -> List[Dict[str, Any]]:
    rows = fetch_sheet_values(
        spreadsheet_id=spreadsheet_id, credentials_file=credentials_file
    )
    return parse_rows(rows)


def expand_items_for_kb(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One sheet article → one KB item per ticker (MACRO if none)."""
    from services.seeking_alpha_finance import parse_sa_news_id

    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        link = str(it.get("link") or "").strip()
        if not title or not link:
            continue
        tickers = it.get("tickers") or ["MACRO"]
        if not isinstance(tickers, (list, tuple)):
            tickers = ["MACRO"]
        sa_id = parse_sa_news_id(link) or ""
        published = it.get("published_at")
        publish_on = str(it.get("publishOn") or "")
        if isinstance(published, datetime):
            publish_on = published.astimezone(timezone.utc).isoformat()
        summary = str(it.get("summary_text") or "").strip()
        for sym in tickers:
            ticker = str(sym or "").strip().upper() or "MACRO"
            out.append(
                {
                    "id": sa_id,
                    "ticker": ticker,
                    "publishOn": publish_on,
                    "title": title,
                    "summary_text": summary,
                    "link": link,
                    "provider": "sa_google_sheet",
                }
            )
    return out


def items_to_sheet_kb_articles(items: Sequence[Dict[str, Any]], *, exchange: str = "NYSE"):
    """Map expanded sheet items → Article with provider=sa_google_sheet."""
    from services.kb_extended_fields import kb_content_sha256
    from services.seeking_alpha_finance import KB_SOURCE, _parse_publish_on
    from services.ticker_news_merge_fetcher import Article

    sid = sheet_id()
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sym = str(it.get("ticker") or "").strip().upper()
        title = str(it.get("title") or "").strip()
        if not sym or not title:
            continue
        link = str(it.get("link") or "").strip()
        summary = str(it.get("summary_text") or "").strip()
        sa_id = str(it.get("id") or "").strip()
        ext_raw = kb_content_sha256(f"sa_sheet|{sym}|{sa_id}|{link}|{title}")
        raw_payload: Dict[str, Any] = {
            "provider": "sa_google_sheet",
            "sheet_id": sid,
            "item": it,
        }
        out.append(
            Article(
                ts=_parse_publish_on(str(it.get("publishOn") or "")),
                symbol=sym,
                exchange=(exchange or "NYSE").strip().upper()[:16],
                source=KB_SOURCE[:120],
                title=title[:2000],
                summary=summary[:4000],
                url=link[:2000],
                external_id_raw=ext_raw,
                raw_payload=raw_payload,
            )
        )
    return out


def ingest_sheet_to_kb(
    *,
    spreadsheet_id: Optional[str] = None,
    credentials_file: Path | None = None,
    exchange: str = "NYSE",
) -> Dict[str, Any]:
    """Fetch full A:E, expand per ticker, upsert into knowledge_base."""
    from services.ticker_news_merge_fetcher import save_articles_to_kb

    if not sheet_enabled():
        return {
            "skipped": True,
            "reason": "NOTEBOOK_SA_SHEET_ENABLED off",
            "rows": 0,
            "items": 0,
            "kb_inserted": 0,
        }

    sheet_items = fetch_sheet_items(
        spreadsheet_id=spreadsheet_id, credentials_file=credentials_file
    )
    expanded = expand_items_for_kb(sheet_items)
    articles = items_to_sheet_kb_articles(expanded, exchange=exchange)
    inserted = int(save_articles_to_kb(articles) or 0) if articles else 0
    return {
        "skipped": False,
        "sheet_id": spreadsheet_id or sheet_id(),
        "rows": len(sheet_items),
        "items": len(expanded),
        "kb_inserted": inserted,
    }
