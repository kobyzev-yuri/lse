"""SEC EDGAR companyfacts → notebook Fundament draft (US filers)."""
from __future__ import annotations

import logging
from datetime import date
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
_LT_DEBT_TAGS: Sequence[str] = (
    "LongTermDebt",  # often already includes current portion
    "LongTermDebtNoncurrent",
)
_CUR_DEBT_TAGS: Sequence[str] = (
    "LongTermDebtCurrent",
    "DebtCurrent",
)
_OCF_TAGS: Sequence[str] = ("NetCashProvidedByUsedInOperatingActivities",)
_CAPEX_TAGS: Sequence[str] = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
)
_PREF_FORMS = ("10-Q", "10-K", "20-F", "6-K")


def _latest_usd_fact(
    facts_us_gaap: dict,
    tags: Sequence[str],
) -> Optional[Tuple[float, str, str, str]]:
    """Return (value, end_iso, form, tag) for newest preferred-form USD fact."""
    best: Optional[Tuple[Tuple[int, int], float, str, str, str]] = None
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
            if not end:
                continue
            try:
                end_d = date.fromisoformat(end[:10])
            except ValueError:
                continue
            try:
                val = float(row.get("val"))
            except (TypeError, ValueError):
                continue
            form = str(row.get("form") or "").strip().upper() or "?"
            rank = 0 if form in _PREF_FORMS else 1
            key = (rank, -end_d.toordinal())
            if best is None or key < best[0]:
                best = (key, val, end, form, tag)
    if best is None:
        return None
    _key, val, end, form, tag = best
    return val, end, form, tag


def _sum_latest_debt(facts_us_gaap: dict) -> Optional[Tuple[float, str, str]]:
    """Interest-bearing debt (no operating leases). Prefer LongTermDebt total."""
    total = _latest_usd_fact(facts_us_gaap, ("LongTermDebt",))
    if total:
        return float(total[0]), total[1], f"{total[2]} interest-bearing"
    nc = _latest_usd_fact(facts_us_gaap, ("LongTermDebtNoncurrent",))
    cur = _latest_usd_fact(facts_us_gaap, _CUR_DEBT_TAGS)
    if nc and cur and nc[1] == cur[1]:
        return float(nc[0]) + float(cur[0]), nc[1], f"{nc[2]}+current interest-bearing"
    if nc:
        return float(nc[0]), nc[1], f"{nc[2]} interest-bearing"
    if cur:
        return float(cur[0]), cur[1], f"{cur[2]} interest-bearing"
    return None


def _fcf_proxy(facts_us_gaap: dict) -> Optional[Tuple[float, str, str]]:
    ocf = _latest_usd_fact(facts_us_gaap, _OCF_TAGS)
    if not ocf:
        return None
    capex = _latest_usd_fact(facts_us_gaap, _CAPEX_TAGS)
    if capex and ocf[1][:7] == capex[1][:7]:
        fcf = float(ocf[0]) - abs(float(capex[0]))
        return fcf, ocf[1], f"{ocf[2]} OCF-|CapEx|"
    return float(ocf[0]), ocf[1], f"{ocf[2]} OCF (без CapEx)"


@lru_cache(maxsize=64)
def _load_companyfacts(cik: str) -> Optional[dict]:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{_cik_padded(cik)}.json"
    try:
        resp = _sec_session().get(url, timeout=30)
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

    cik = TICKER_CIK.get(u)
    if not cik:
        return {
            "ticker": u,
            "source": "edgar",
            "fundament": _normalize_fundament({}),
            "filled": [],
            "note": (
                f"Нет CIK для {u} в карте SEC (часто иностранный эмитент) — "
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

    cash = _latest_usd_fact(us_gaap, _CASH_TAGS)
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
                "note": f"SEC {form} {end[:7]} · {cash_scope}",
                "tone": "good",
            }
        )
        filled.append("cash")
    else:
        metrics.append({"k": "КЭШ", "v": "—", "note": "", "tone": ""})

    fcf = _fcf_proxy(us_gaap)
    if fcf:
        v, end, form = fcf
        metrics.append(
            {
                "k": "FCF",
                "v": _fmt_usd_compact(v) or "—",
                "note": f"SEC FY {form} {end[:7]} · OCF-|CapEx|",
                "tone": "bad" if v < 0 else "good",
            }
        )
        filled.append("fcf")
    else:
        metrics.append({"k": "FCF", "v": "—", "note": "", "tone": ""})

    debt = _sum_latest_debt(us_gaap)
    if debt:
        v, end, form = debt
        metrics.append(
            {
                "k": "Прямой долг",
                "v": _fmt_usd_compact(v) or "—",
                "note": f"SEC FY {form} {end[:7]} · без leases",
                "tone": "",
            }
        )
        filled.append("debt")
    else:
        metrics.append({"k": "Прямой долг", "v": "—", "note": "", "tone": ""})

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

    return {
        "ticker": u,
        "source": "edgar",
        "cik": cik,
        "fundament": fundament,
        "filled": filled,
        "filing_url": filing_url,
        "note": (
            "черновик из SEC XBRL · tagline/плюсы/риски не тронуты · "
            "сверьте период формы · нажмите «сохранить»"
        ),
    }
