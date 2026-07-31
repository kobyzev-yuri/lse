"""SEC EDGAR companyfacts → notebook Fundament draft (US filers)."""
from __future__ import annotations

import logging
from datetime import date, timedelta
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.earnings_material_auto_sources import TICKER_CIK, _cik_padded, _sec_session

logger = logging.getLogger(__name__)

# Prefer "cash + short-term investments" (aligns with Yahoo totalCash), then pure cash.
_CASH_TAGS: Sequence[str] = (
    "CashCashEquivalentsAndShortTermInvestments",
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
)
_CUR_DEBT_TAGS: Sequence[str] = (
    "LongTermDebtCurrent",
    "DebtCurrent",
    "ShortTermBorrowings",
)
_OCF_TAGS: Sequence[str] = ("NetCashProvidedByUsedInOperatingActivities",)
_CAPEX_TAGS: Sequence[str] = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
)
_PREF_FORMS = ("10-Q", "10-K", "20-F", "6-K")
_ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A"})
# Reject stale BS / debt facts (e.g. TER LongTermDebt from 2014).
_MAX_FACT_AGE_DAYS = 800  # ~26 months — covers late 10-K after FY end


def _is_annual_form(form: str) -> bool:
    f = str(form or "").strip().upper()
    return f in _ANNUAL_FORMS or f.startswith("10-K") or f.startswith("20-F")


def _is_annual_row(form: str, fp: str) -> bool:
    if _is_annual_form(form):
        return True
    return str(fp or "").strip().upper() == "FY"


def _parse_end(end: str) -> Optional[date]:
    try:
        return date.fromisoformat(str(end or "").strip()[:10])
    except ValueError:
        return None


def _iter_usd_rows(
    facts_us_gaap: dict,
    tags: Sequence[str],
):
    for tag in tags:
        node = facts_us_gaap.get(tag)
        if not isinstance(node, dict):
            continue
        units = node.get("units") if isinstance(node.get("units"), dict) else {}
        rows = units.get("USD")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            end = str(row.get("end") or "").strip()
            end_d = _parse_end(end)
            if end_d is None:
                continue
            try:
                val = float(row.get("val"))
            except (TypeError, ValueError):
                continue
            form = str(row.get("form") or "").strip().upper() or "?"
            fp = str(row.get("fp") or "").strip().upper()
            yield tag, val, end, end_d, form, fp


def _select_usd_fact(
    facts_us_gaap: dict,
    tags: Sequence[str],
    *,
    prefer_annual: bool = False,
    max_age_days: Optional[int] = None,
    as_of: Optional[date] = None,
) -> Optional[Tuple[float, str, str, str]]:
    """Return (value, end_iso, form, tag).

    prefer_annual: rank 10-K / fp=FY ahead of newer 10-Q (for FCF vs Yahoo FY).
    max_age_days: drop facts older than as_of - max_age_days.
    """
    today = as_of or date.today()
    best: Optional[Tuple[Tuple[int, int, int], float, str, str, str]] = None
    for tag, val, end, end_d, form, fp in _iter_usd_rows(facts_us_gaap, tags):
        if max_age_days is not None and end_d < today - timedelta(days=max_age_days):
            continue
        form_ok = 0 if form in _PREF_FORMS or form.startswith("10-") or form.startswith("20-") else 1
        if prefer_annual:
            annual_rank = 0 if _is_annual_row(form, fp) else 1
            # Prefer annual even if a newer interim exists; within same class, newest end.
            key = (annual_rank, form_ok, -end_d.toordinal())
        else:
            # Newest preferred-form first (BS / cash).
            key = (form_ok, -end_d.toordinal(), 0)
        if best is None or key < best[0]:
            best = (key, val, end, form, tag)
    if best is None:
        return None
    _key, val, end, form, tag = best
    return val, end, form, tag


def _latest_usd_fact(
    facts_us_gaap: dict,
    tags: Sequence[str],
) -> Optional[Tuple[float, str, str, str]]:
    """Newest preferred-form USD fact (balance-sheet default)."""
    return _select_usd_fact(facts_us_gaap, tags, prefer_annual=False)


def _bs_period_note(form: str, end: str) -> str:
    ym = (end or "")[:7]
    if _is_annual_form(form):
        return f"SEC {form} на {ym} (FY BS)"
    return f"SEC {form} на {ym} (промежуточный BS)"


def _flow_period_note(form: str, end: str, how: str) -> str:
    ym = (end or "")[:7]
    if _is_annual_form(form):
        return f"SEC {form} FY {ym} · {how} · сравнимо с Yahoo FY"
    return f"SEC {form} YTD до {ym} · {how} · ≠ Yahoo FY (бери 10-K)"


def _cash_fact(facts_us_gaap: dict) -> Optional[Tuple[float, str, str, str]]:
    """Prefer cash+STI on a recent date; fall back to cash-only."""
    # Try cash+STI first (even if slightly older than cash-only).
    sti = _select_usd_fact(
        facts_us_gaap,
        ("CashCashEquivalentsAndShortTermInvestments",),
        prefer_annual=False,
        max_age_days=_MAX_FACT_AGE_DAYS,
    )
    if sti:
        return sti
    return _select_usd_fact(
        facts_us_gaap,
        _CASH_TAGS,
        prefer_annual=False,
        max_age_days=_MAX_FACT_AGE_DAYS,
    )


def _sum_latest_debt(facts_us_gaap: dict) -> Optional[Tuple[float, str, str]]:
    """Interest-bearing debt; reject stale LongTermDebt (multi-year-old zeros)."""
    total = _select_usd_fact(
        facts_us_gaap,
        ("LongTermDebt",),
        prefer_annual=False,
        max_age_days=_MAX_FACT_AGE_DAYS,
    )
    if total:
        return float(total[0]), total[1], total[2]

    nc = _select_usd_fact(
        facts_us_gaap,
        ("LongTermDebtNoncurrent",),
        prefer_annual=False,
        max_age_days=_MAX_FACT_AGE_DAYS,
    )
    cur = _select_usd_fact(
        facts_us_gaap,
        _CUR_DEBT_TAGS,
        prefer_annual=False,
        max_age_days=_MAX_FACT_AGE_DAYS,
    )
    if nc and cur and nc[1][:7] == cur[1][:7]:
        return float(nc[0]) + float(cur[0]), nc[1], nc[2]
    if nc:
        return float(nc[0]), nc[1], nc[2]
    if cur:
        return float(cur[0]), cur[1], cur[2]
    return None


def _matched_capex(
    facts_us_gaap: dict,
    ocf_end: str,
    *,
    prefer_annual: bool,
) -> Optional[Tuple[float, str, str, str]]:
    capex = _select_usd_fact(
        facts_us_gaap,
        _CAPEX_TAGS,
        prefer_annual=prefer_annual,
        max_age_days=None if prefer_annual else _MAX_FACT_AGE_DAYS,
    )
    if not capex:
        # When preferring annual, still allow any CapEx with same end month.
        for tag, val, end, _end_d, form, _fp in _iter_usd_rows(facts_us_gaap, _CAPEX_TAGS):
            if end[:7] == ocf_end[:7]:
                return val, end, form, tag
        return None
    if capex[1][:7] == ocf_end[:7]:
        return capex
    # Same fiscal end month search among all CapEx rows.
    for tag, val, end, _end_d, form, _fp in _iter_usd_rows(facts_us_gaap, _CAPEX_TAGS):
        if end[:7] == ocf_end[:7]:
            return val, end, form, tag
    return None


def _fcf_proxy(facts_us_gaap: dict) -> Optional[Tuple[float, str, str, str]]:
    """Prefer annual (10-K/FY) OCF-|CapEx| to align with Yahoo annual FCF."""
    ocf = _select_usd_fact(
        facts_us_gaap,
        _OCF_TAGS,
        prefer_annual=True,
        max_age_days=None,
    )
    if not ocf:
        return None
    prefer_annual = _is_annual_form(ocf[2])
    capex = _matched_capex(facts_us_gaap, ocf[1], prefer_annual=prefer_annual)
    if capex:
        fcf = float(ocf[0]) - abs(float(capex[0]))
        return fcf, ocf[1], ocf[2], "OCF-|CapEx|"
    return float(ocf[0]), ocf[1], ocf[2], "OCF (без CapEx)"


@lru_cache(maxsize=1)
def _sec_ticker_cik_map() -> Dict[str, str]:
    """Ticker → CIK from SEC company_tickers.json (best-effort cache)."""
    out: Dict[str, str] = {}
    try:
        resp = _sec_session().get(
            "https://www.sec.gov/files/company_tickers.json",
            timeout=45,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("SEC company_tickers.json failed: %s", e)
        return out
    if not isinstance(data, dict):
        return out
    for row in data.values():
        if not isinstance(row, dict):
            continue
        t = str(row.get("ticker") or "").strip().upper()
        cik = str(row.get("cik_str") or "").strip()
        if t and cik.isdigit():
            out[t] = cik
    return out


def resolve_ticker_cik(sym: str) -> Optional[str]:
    """Static map first, then SEC company_tickers.json."""
    u = str(sym or "").strip().upper()
    if not u:
        return None
    if u in TICKER_CIK:
        return TICKER_CIK[u]
    return _sec_ticker_cik_map().get(u)


@lru_cache(maxsize=64)
def _load_companyfacts(cik: str) -> Optional[dict]:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{_cik_padded(cik)}.json"
    try:
        resp = _sec_session().get(url, timeout=45)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("SEC companyfacts failed cik=%s: %s", cik, e)
        return None


def suggest_fundament_from_edgar(sym: str) -> Dict[str, Any]:
    """
    Draft Fundament metrics from SEC companyfacts (US-GAAP).
    Does not persist; never fills pluses/risks; tagline stays empty.
    """
    from services.trading_notebook import _fmt_usd_compact, _normalize_fundament

    u = str(sym or "").strip().upper()
    if not u:
        raise ValueError("ticker required")

    cik = resolve_ticker_cik(u)
    if not cik:
        return {
            "ticker": u,
            "source": "edgar",
            "fundament": _normalize_fundament({}),
            "filled": [],
            "note": (
                f"Нет CIK для {u} (карта + company_tickers) — "
                "откройте SEC/IR вручную или используйте Yahoo-черновик"
            ),
        }

    raw = _load_companyfacts(cik)
    if not raw:
        return {
            "ticker": u,
            "source": "edgar",
            "cik": cik,
            "fundament": _normalize_fundament({}),
            "filled": [],
            "note": "SEC companyfacts недоступны — попробуйте позже или Yahoo",
        }

    facts = raw.get("facts") if isinstance(raw.get("facts"), dict) else {}
    us_gaap = facts.get("us-gaap") if isinstance(facts.get("us-gaap"), dict) else {}
    if not us_gaap:
        return {
            "ticker": u,
            "source": "edgar",
            "cik": cik,
            "fundament": _normalize_fundament({}),
            "filled": [],
            "note": "В companyfacts нет us-gaap — сверка только через filing/IR",
        }

    filled: List[str] = []
    metrics: List[Dict[str, str]] = []

    cash = _cash_fact(us_gaap)
    if cash:
        v, end, form, tag = cash
        cash_scope = (
            "cash+STI"
            if "ShortTermInvestments" in tag
            else "cash only"
        )
        metrics.append(
            {
                "k": "КЭШ",
                "v": _fmt_usd_compact(v) or "—",
                "note": f"{_bs_period_note(form, end)} · {cash_scope}",
                "tone": "good",
            }
        )
        filled.append("cash")
    else:
        metrics.append({"k": "КЭШ", "v": "—", "note": "", "tone": ""})

    fcf = _fcf_proxy(us_gaap)
    if fcf:
        v, end, form, how = fcf
        metrics.append(
            {
                "k": "FCF",
                "v": _fmt_usd_compact(v) or "—",
                "note": _flow_period_note(form, end, how),
                "tone": "bad" if v < 0 else "good",
            }
        )
        filled.append("fcf")
    else:
        metrics.append({"k": "FCF", "v": "—", "note": "", "tone": ""})

    debt = _sum_latest_debt(us_gaap)
    if debt:
        v, end, form = debt
        # Zero debt on a recent filing can be real (ALAB); keep it.
        metrics.append(
            {
                "k": "Прямой долг",
                "v": _fmt_usd_compact(v) or "—",
                "note": f"{_bs_period_note(form, end)} · interest-bearing без leases",
                "tone": "",
            }
        )
        filled.append("debt")
    else:
        metrics.append(
            {
                "k": "Прямой долг",
                "v": "—",
                "note": "нет свежего LongTermDebt в XBRL (<26м) — Yahoo/IR",
                "tone": "",
            }
        )

    metrics.append({"k": "Запас прочности", "v": "—", "note": "вручную после сверки", "tone": ""})

    entity = str(raw.get("entityName") or u)
    filing_url = (
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={_cik_padded(cik)}"
        f"&owner=exclude&count=40"
    )

    fundament = _normalize_fundament(
        {
            "tagline": "",
            "metrics": metrics,
            "margin_ru": "",
            "financing_ru": (
                f"SEC companyfacts CIK {cik} ({entity}) — цифры-черновик, плюсы/риски вручную"
            ),
            "pluses": [],
            "risks": [],
            "filing_url": filing_url,
        }
    )

    comparable = bool(fcf and _is_annual_form(fcf[2]))
    note = (
        "черновик SEC XBRL · FCF=FY (сравнимо с Yahoo)"
        if comparable
        else "черновик SEC XBRL · FCF не FY — лучше Yahoo annual / 10-K PDF"
    )

    return {
        "ticker": u,
        "source": "edgar",
        "cik": cik,
        "fundament": fundament,
        "filled": filled,
        "filing_url": filing_url,
        "note": f"{note} · сохраните",
    }
