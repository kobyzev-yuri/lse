"""Auto-discover earnings material URLs (SEC 8-K, Motley Fool transcripts)."""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from functools import lru_cache
from typing import Iterable

from services.earnings_material_catalog import CatalogMaterial
from services.earnings_material_parser import SEC_USER_AGENT
from services.http_outbound import outbound_session

logger = logging.getLogger(__name__)

# CIK without leading zeros (SEC EDGAR numeric path).
TICKER_CIK: dict[str, str] = {
    "MSFT": "789019",
    "META": "1326801",
    "AMZN": "1018724",
    "GOOGL": "1652044",
    "NVDA": "1045810",
    "AMD": "2488",
    "MU": "723125",
    "INTC": "50863",
    "LITE": "1633978",
    "CIEN": "1067983",
    "NBIS": "2026478",
    "TER": "97210",
    "ALAB": "1967398",
    "ORCL": "1341439",
    "ANET": "1596532",
    "DELL": "1571996",
    "AVGO": "1730168",
    "PLTR": "1321655",
    "SNDK": "2026474",
}

# Motley Fool slug hints: company slug prefix in transcript URL path.
FOOL_SLUG_HINTS: dict[str, tuple[str, ...]] = {
    "MSFT": ("microsoft-msft",),
    "META": ("meta-meta",),
    "AMZN": ("amazon-amzn", "amazoncom-amzn"),
    "NVDA": ("nvidia-nvda",),
    "AMD": ("advanced-micro-devices-amd", "amd-amd"),
    "MU": ("micron-mu", "micron-technology-mu"),
    "INTC": ("intel-intc",),
    "ASML": ("asml-asml",),
    "SNDK": ("sandisk-sndk",),
    "LITE": ("lumentum-lite",),
    "CIEN": ("ciena-cien",),
    "NBIS": ("nebius-nbis",),
    "TER": ("teradyne-ter",),
    "ALAB": ("astera-labs-alab",),
    "ORCL": ("oracle-orcl",),
    "GOOGL": ("alphabet-googl", "alphabet-goog"),
}


def _sec_session():
    s = outbound_session("EARNINGS_SEC_USE_SYSTEM_PROXY")
    s.headers.update(
        {
            "User-Agent": SEC_USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
        }
    )
    return s


def _cik_padded(cik: str) -> str:
    return str(int(cik)).zfill(10)


def _accession_no_dashes(accession: str) -> str:
    return re.sub(r"[^0-9]", "", accession)


@lru_cache(maxsize=64)
def _load_sec_submissions(cik: str) -> dict | None:
    url = f"https://data.sec.gov/submissions/CIK{_cik_padded(cik)}.json"
    try:
        resp = _sec_session().get(url, timeout=25)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("SEC submissions fetch failed cik=%s: %s", cik, e)
        return None


def sec_8k_filings_near_date(
    symbol: str,
    event_date: date,
    *,
    window_days: int = 5,
) -> list[CatalogMaterial]:
    sym = symbol.strip().upper()
    cik = TICKER_CIK.get(sym)
    if not cik:
        return []
    payload = _load_sec_submissions(cik)
    if not payload:
        return []
    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    filing_dates = recent.get("filingDate") or []
    accession_numbers = recent.get("accessionNumber") or []
    primary_docs = recent.get("primaryDocument") or []
    out: list[CatalogMaterial] = []
    lo = event_date - timedelta(days=window_days)
    hi = event_date + timedelta(days=window_days)
    for form, fdate_s, accession, primary in zip(forms, filing_dates, accession_numbers, primary_docs):
        if str(form).upper() != "8-K":
            continue
        try:
            fdate = date.fromisoformat(str(fdate_s)[:10])
        except ValueError:
            continue
        if fdate < lo or fdate > hi:
            continue
        doc = str(primary or "").strip()
        if not doc:
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{_accession_no_dashes(accession)}/{doc}"
        out.append(
            CatalogMaterial(
                symbol=sym,
                event_date=event_date,
                fiscal_period=None,
                material_type="sec_filing",
                source_name="SEC EDGAR",
                source_url=url,
                title=f"{sym} 8-K filed {fdate.isoformat()} (earnings window)",
                meta={"auto_source": "sec_8k", "filing_date": fdate.isoformat(), "accession": accession},
            )
        )
    return out


def _fool_url_candidates(symbol: str, event_date: date) -> list[str]:
    sym = symbol.strip().upper()
    slugs = FOOL_SLUG_HINTS.get(sym, (f"{sym.lower()}-{sym.lower()}",))
    y, m, d = event_date.year, event_date.month, event_date.day
    base = f"https://www.fool.com/earnings/call-transcripts/{y:04d}/{m:02d}/{d:02d}"
    suffixes = (
        "earnings-call-transcript",
        "earnings-transcript",
        "q1-2026-earnings-call-transcript",
        "q2-2026-earnings-call-transcript",
        "q3-2026-earnings-call-transcript",
        "q4-2026-earnings-call-transcript",
    )
    urls: list[str] = []
    for slug in slugs:
        for suffix in suffixes:
            urls.append(f"{base}/{slug}-{suffix}/")
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _url_exists(url: str) -> bool:
    try:
        sess = outbound_session("EARNINGS_FOOL_USE_SYSTEM_PROXY")
        resp = sess.head(url, allow_redirects=True, timeout=15)
        if resp.status_code == 405:
            resp = sess.get(url, allow_redirects=True, timeout=15, stream=True)
        return 200 <= resp.status_code < 400
    except Exception:
        return False


def fool_transcript_near_date(
    symbol: str,
    event_date: date,
    *,
    max_probe: int = 6,
) -> CatalogMaterial | None:
    for url in _fool_url_candidates(symbol, event_date)[: max(1, max_probe)]:
        if not _url_exists(url):
            continue
        sym = symbol.strip().upper()
        return CatalogMaterial(
            symbol=sym,
            event_date=event_date,
            fiscal_period=None,
            material_type="third_party_transcript",
            source_name="The Motley Fool",
            source_url=url,
            title=f"{sym} earnings call transcript ({event_date.isoformat()})",
            meta={"auto_source": "fool_transcript"},
        )
    return None


def auto_materials_for_event(
    symbol: str,
    event_date: date,
    *,
    include_sec: bool = True,
    include_fool: bool = True,
    fool_max_probe: int = 6,
) -> tuple[CatalogMaterial, ...]:
    sym = symbol.strip().upper()
    rows: list[CatalogMaterial] = []
    seen_urls: set[str] = set()

    def add(cm: CatalogMaterial | None) -> None:
        if cm is None:
            return
        if cm.source_url in seen_urls:
            return
        seen_urls.add(cm.source_url)
        rows.append(cm)

    if include_sec:
        for cm in sec_8k_filings_near_date(sym, event_date):
            add(cm)
    if include_fool:
        add(fool_transcript_near_date(sym, event_date, max_probe=fool_max_probe))
    return tuple(rows)


def auto_materials_for_events(
    events: Iterable[tuple[str, date]],
    *,
    include_sec: bool = True,
    include_fool: bool = True,
) -> tuple[CatalogMaterial, ...]:
    out: list[CatalogMaterial] = []
    seen: set[tuple[str, str | None, str]] = set()
    for symbol, event_date in events:
        for cm in auto_materials_for_event(
            symbol,
            event_date,
            include_sec=include_sec,
            include_fool=include_fool,
        ):
            key = (cm.symbol, str(cm.event_date), cm.source_url)
            if key in seen:
                continue
            seen.add(key)
            out.append(cm)
    return tuple(out)
