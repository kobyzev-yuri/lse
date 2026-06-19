"""Auto-discover earnings material URLs (SEC 8-K, Motley Fool transcripts)."""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from config_loader import get_config_value
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
    "DELL": ("dell-dell", "dell-technologies-dell"),
    "AVGO": ("broadcom-avgo", "broadcom-inc-avgo"),
    "PLTR": ("palantir-pltr", "palantir-technologies-pltr"),
    "ANET": ("arista-networks-anet", "arista-anet"),
    "ARM": ("arm-holdings-arm", "arm-arm"),
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
    # SEC 8-K filing dates around an "earnings event" can drift by weeks:
    # e.g., CIEN earnings calendar date (2026-06-04) has nearest 8-K at 2026-05-07.
    # A wider window increases material discovery coverage and unblocks LLM label accumulation.
    window_days: int = 40,
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


def _fool_day_window() -> int:
    try:
        return max(0, int((get_config_value("EARNINGS_FOOL_PROBE_DAY_WINDOW", "1") or "1").strip()))
    except (TypeError, ValueError):
        return 1


def _fool_calendar_dates(event_date: date) -> list[date]:
    """Fool paths often use publication date (event_date + 0..1 day), not always report day."""
    w = _fool_day_window()
    days = [event_date + timedelta(days=offset) for offset in range(-w, w + 1)]
    seen: set[date] = set()
    out: list[date] = []
    for d in days:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _fiscal_quarter_suffixes(event_date: date) -> tuple[str, ...]:
    """Best-effort US earnings-season quarter slug (Apr–Jun ≈ Q1 for many tech names)."""
    m = event_date.month
    y = event_date.year
    if m in (1, 2, 3):
        q, fy = 4, y - 1
    elif m in (4, 5, 6):
        q, fy = 1, y
    elif m in (7, 8, 9):
        q, fy = 2, y
    else:
        q, fy = 3, y
    return (
        f"q{q}-{fy}-earnings-call-transcript",
        f"q{q}-{fy}-earnings-transcript",
        "earnings-call-transcript",
        "earnings-transcript",
    )


def _fool_url_candidates(symbol: str, event_date: date) -> list[str]:
    sym = symbol.strip().upper()
    slugs = FOOL_SLUG_HINTS.get(sym, (f"{sym.lower()}-{sym.lower()}",))
    suffixes = _fiscal_quarter_suffixes(event_date)
    urls: list[str] = []
    for ev_d in _fool_calendar_dates(event_date):
        y, m, d = ev_d.year, ev_d.month, ev_d.day
        base = f"https://www.fool.com/earnings/call-transcripts/{y:04d}/{m:02d}/{d:02d}"
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


_EXHIBIT_LINK_RE = re.compile(
    r'href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_EXHIBIT_HINTS = (
    "transcript",
    "earnings-call",
    "earnings_call",
    "earnings call",
    "conference-call",
    "confcall",
    "ex99",
    "ex-99",
    "exhibit99",
    "exhibit-99",
    "exhibit 99",
    "earnings release",
    "financial results",
    "press-release",
)


def _sec_filing_index_url(cik: str, accession: str) -> str | None:
    acc = (accession or "").strip()
    if not acc:
        return None
    acc_flat = _accession_no_dashes(acc)
    folder = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_flat}/"
    if "-" in acc:
        return folder + acc + "-index.htm"
    return None


def sec_8k_exhibit_materials_near_date(
    symbol: str,
    event_date: date,
    *,
    window_days: int = 40,
) -> list[CatalogMaterial]:
    """Parse SEC 8-K filing index for exhibit .htm/.txt transcripts (not only primary 8-K body)."""
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
    lo = event_date - timedelta(days=window_days)
    hi = event_date + timedelta(days=window_days)
    out: list[CatalogMaterial] = []
    seen_urls: set[str] = set()
    for form, fdate_s, accession, primary in zip(forms, filing_dates, accession_numbers, primary_docs):
        if str(form).upper() != "8-K":
            continue
        try:
            fdate = date.fromisoformat(str(fdate_s)[:10])
        except ValueError:
            continue
        if fdate < lo or fdate > hi:
            continue
        index_url = _sec_filing_index_url(cik, str(accession))
        if not index_url:
            continue
        try:
            resp = _sec_session().get(index_url, timeout=25)
            if resp.status_code != 200:
                continue
            html = resp.text or ""
        except Exception as e:
            logger.debug("SEC index fetch failed %s: %s", index_url, e)
            continue
        base_folder = index_url.rsplit("/", 1)[0] + "/"
        for m in _EXHIBIT_LINK_RE.finditer(html):
            href = (m.group(1) or "").strip()
            if not href or href.startswith("#"):
                continue
            low = href.lower()
            if not any(h in low for h in _EXHIBIT_HINTS):
                continue
            if not (low.endswith(".htm") or low.endswith(".html") or low.endswith(".txt")):
                continue
            if low.startswith("http"):
                url = href
            elif href.startswith("/"):
                url = "https://www.sec.gov" + href
            else:
                url = base_folder + href.lstrip("/")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            out.append(
                CatalogMaterial(
                    symbol=sym,
                    event_date=event_date,
                    fiscal_period=None,
                    material_type="transcript",
                    source_name="SEC EDGAR exhibit",
                    source_url=url,
                    title=f"{sym} 8-K exhibit transcript ({fdate.isoformat()})",
                    meta={"auto_source": "sec_8k_exhibit", "filing_date": fdate.isoformat(), "accession": accession},
                )
            )
    return out


def _fool_cooldown_file() -> Path:
    root = Path(__file__).resolve().parents[1]
    if Path("/app/logs").exists():
        return Path("/app/logs") / ".fool_transcript_cooldown_until"
    return root / "logs" / ".fool_transcript_cooldown_until"


def fool_rate_limit_active() -> bool:
    p = _fool_cooldown_file()
    if not p.is_file():
        return False
    try:
        until = datetime.fromisoformat(p.read_text(encoding="utf-8").strip())
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < until
    except Exception:
        return False


def _set_fool_rate_limit_cooldown() -> None:
    try:
        hours = float((get_config_value("FOOL_TRANSCRIPT_COOLDOWN_AFTER_429_HOURS", "6") or "6").strip())
    except (TypeError, ValueError):
        hours = 6.0
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    path = _fool_cooldown_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(until.isoformat(), encoding="utf-8")
    logger.warning("Motley Fool: 429 — cooldown until %s UTC (%s h)", until.strftime("%Y-%m-%d %H:%M"), hours)


def _probe_fool_url(url: str) -> tuple[bool, bool]:
    """Return (exists, rate_limited)."""
    try:
        sess = outbound_session("EARNINGS_FOOL_USE_SYSTEM_PROXY")
        resp = sess.head(url, allow_redirects=True, timeout=15)
        if resp.status_code == 429:
            return False, True
        if resp.status_code == 405:
            resp = sess.get(url, allow_redirects=True, timeout=15, stream=True)
            if resp.status_code == 429:
                return False, True
        return 200 <= resp.status_code < 400, False
    except Exception:
        return False, False


def _default_fool_max_probe() -> int:
    try:
        return max(6, int((get_config_value("EARNINGS_FOOL_MAX_PROBE", "18") or "18").strip()))
    except (TypeError, ValueError):
        return 18


def fool_transcript_near_date(
    symbol: str,
    event_date: date,
    *,
    max_probe: int | None = None,
) -> CatalogMaterial | None:
    if fool_rate_limit_active():
        return None
    probe_limit = max_probe if max_probe is not None else _default_fool_max_probe()
    pause_s = 0.35
    for url in _fool_url_candidates(symbol, event_date)[: max(1, probe_limit)]:
        ok, limited = _probe_fool_url(url)
        if limited:
            _set_fool_rate_limit_cooldown()
            return None
        if not ok:
            time.sleep(pause_s)
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
    include_sec_exhibits: bool = True,
    include_catalog: bool = True,
    fool_max_probe: int | None = None,
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

    if include_catalog:
        from services.earnings_material_catalog import catalog_for_event

        for cm in catalog_for_event(sym, event_date):
            add(cm)
    if include_sec:
        for cm in sec_8k_filings_near_date(sym, event_date):
            add(cm)
    if include_sec_exhibits:
        for cm in sec_8k_exhibit_materials_near_date(sym, event_date):
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
