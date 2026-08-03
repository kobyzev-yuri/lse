"""Notebook Fundament: Yahoo draft ↔ Ex99/IR text reconcile via LLM (ProxyAPI).

Draft only — never persists overlay. Does not invent Nastya judgment fields
(pluses / risks / key_clients).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MIN_CONTENT_CHARS = int(os.environ.get("NOTEBOOK_RECONCILE_MIN_CHARS", "400"))
MAX_CONTENT_CHARS = int(os.environ.get("NOTEBOOK_RECONCILE_MAX_CHARS", "90000"))
DEFAULT_OUTPUT_TOKENS = int(os.environ.get("NOTEBOOK_RECONCILE_OUTPUT_TOKENS", "2500"))

RECONCILE_SYSTEM_PROMPT = """You reconcile Yahoo Finance passport metrics with an earnings material text (Ex99.1 / press / IR).
Do NOT invent numbers. If a figure is not stated in the filing text, mark status missing_in_filing.
Do NOT write investment advice, pluses, risks, or client lists.

Return ONE JSON object (no markdown):
{
  "filing_period": "string or null",
  "metrics_patch": [
    {
      "key": "КЭШ|FCF|Прямой долг|Запас прочности|or Yahoo label",
      "value": "string as in filing or null",
      "note": "short RU note with period/source",
      "tone": "good|bad|mid|",
      "status": "match|conflict|missing_in_filing"
    }
  ],
  "margin_ru": "short RU string or null",
  "financing_ru": "short RU string or null",
  "conflicts": [
    {"field": "string", "yahoo": "string", "filing": "string", "note": "short RU"}
  ],
  "evidence_quotes": [
    {"topic": "cash|fcf|debt|margin|liquidity|other", "quote": "verbatim under 240 chars from text"}
  ]
}

Rules:
- Compare only cash / FCF / interest-bearing debt / liquidity / margins if present in text.
- metrics_patch: prefer the same keys as Yahoo (КЭШ, FCF, Прямой долг, Запас прочности).
- On conflict: put Yahoo value in conflicts[].yahoo and filing value in conflicts[].filing; still set metrics_patch value to the filing figure when stated.
- evidence_quotes: max 3, must appear in the source text.
- Russian notes for note / margin_ru / financing_ru / conflicts[].note.
"""


class MaterialMissingError(ValueError):
    """No parsed earnings_material content_text suitable for reconcile."""


class ReconcileLlmError(RuntimeError):
    """LLM call failed or returned unusable payload."""


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    body = (text or "").strip()
    if not body:
        return None
    fence = re.match(r"^```(?:json)?\s*\r?\n?", body, flags=re.IGNORECASE)
    if fence:
        body = body[fence.end() :]
        if body.endswith("```"):
            body = body[: -3].strip()
    try:
        obj = json.loads(body)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = body.find("{")
    end = body.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(body[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def load_best_reconcile_material(symbol: str) -> Dict[str, Any]:
    """Best earnings_material row with usable content_text for LLM reconcile."""
    from services.trading_notebook import _earnings_url_relevance_score

    u = str(symbol or "").strip().upper()
    empty: Dict[str, Any] = {
        "filing_url": "",
        "source_name": "",
        "material_type": "",
        "event_date": "",
        "parse_status": "",
        "content_text": "",
        "id": None,
    }
    if not u:
        return empty
    try:
        from sqlalchemy import create_engine, text

        from config_loader import get_database_url

        eng = create_engine(get_database_url(), pool_pre_ping=True)
        try:
            q = text(
                """
                SELECT id, source_url, source_name, material_type, event_date,
                       parse_status, content_text
                FROM earnings_material
                WHERE UPPER(TRIM(symbol)) = :symbol
                  AND COALESCE(source_url, '') <> ''
                  AND COALESCE(parse_status, '') NOT IN ('failed', 'blocked')
                  AND LENGTH(COALESCE(content_text, '')) >= :min_chars
                ORDER BY COALESCE(event_date, DATE '1900-01-01') DESC, id DESC
                LIMIT 40
                """
            )
            with eng.connect() as conn:
                rows = list(
                    conn.execute(
                        q, {"symbol": u, "min_chars": MIN_CONTENT_CHARS}
                    ).mappings().all()
                )
        finally:
            eng.dispose()
        if not rows:
            return empty
        best: Optional[Dict[str, Any]] = None
        best_key = None
        for row in rows:
            item = dict(row)
            url = str(item.get("source_url") or "").strip()[:500]
            if not (url.startswith("http://") or url.startswith("https://")):
                continue
            body = str(item.get("content_text") or "").strip()
            if len(body) < MIN_CONTENT_CHARS:
                continue
            ev = item.get("event_date")
            ev_s = str(ev) if ev is not None else ""
            rel = _earnings_url_relevance_score(
                url,
                str(item.get("material_type") or ""),
                str(item.get("source_name") or ""),
            )
            key = (ev_s, rel, len(body))
            if best_key is None or key > best_key:
                best_key = key
                item["source_url"] = url
                item["content_text"] = body
                best = item
        if not best:
            return empty
        return {
            "id": best.get("id"),
            "filing_url": str(best.get("source_url") or "")[:500],
            "source_name": str(best.get("source_name") or "").strip()[:120],
            "material_type": str(best.get("material_type") or "").strip()[:40],
            "event_date": str(best.get("event_date") or "").strip()[:32],
            "parse_status": str(best.get("parse_status") or "").strip()[:40],
            "content_text": str(best.get("content_text") or ""),
        }
    except Exception as e:
        logger.warning("reconcile material for %s: %s", u, e)
        return empty


def _yahoo_metrics_for_prompt(fundament: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for m in fundament.get("metrics") or []:
        if not isinstance(m, dict):
            continue
        out.append(
            {
                "key": str(m.get("k") or "")[:40],
                "value": str(m.get("v") or "")[:80],
                "note": str(m.get("note") or "")[:160],
            }
        )
    return out


def _apply_metrics_patch(
    yahoo_metrics: List[Dict[str, str]],
    patch: Any,
) -> List[Dict[str, str]]:
    """Merge LLM metrics_patch onto Yahoo metrics by key; keep 4 slots."""
    by_key: Dict[str, Dict[str, str]] = {}
    order: List[str] = []
    for m in yahoo_metrics:
        if not isinstance(m, dict):
            continue
        k = str(m.get("k") or "").strip() or "—"
        by_key[k] = {
            "k": k,
            "v": str(m.get("v") or "—"),
            "note": str(m.get("note") or ""),
            "tone": str(m.get("tone") or ""),
        }
        order.append(k)

    patches = patch if isinstance(patch, list) else []
    for p in patches:
        if not isinstance(p, dict):
            continue
        k = str(p.get("key") or p.get("k") or "").strip()
        if not k:
            continue
        status = str(p.get("status") or "").strip().lower()
        filing_v = p.get("value")
        note = str(p.get("note") or "").strip()
        tone = str(p.get("tone") or "").strip()
        if tone not in ("good", "bad", "mid"):
            tone = ""
        cur = by_key.get(k) or {"k": k, "v": "—", "note": "", "tone": ""}
        if k not in by_key:
            order.append(k)
        if status == "missing_in_filing":
            tag = "LLM сверка: в Ex99/IR не найдено"
            cur["note"] = (f"{cur.get('note') or ''} · {tag}".strip(" ·"))[:160]
            if note:
                cur["note"] = (f"{cur['note']} · {note}".strip(" ·"))[:160]
        elif filing_v is not None and str(filing_v).strip():
            cur["v"] = str(filing_v).strip()[:80]
            status_ru = {
                "match": "совпало с filing",
                "conflict": "взято из filing (конфликт с Yahoo)",
            }.get(status, "из filing")
            base_note = note or status_ru
            cur["note"] = f"LLM сверка · {base_note}"[:160]
            if tone:
                cur["tone"] = tone
        elif note:
            cur["note"] = (f"{cur.get('note') or ''} · LLM: {note}".strip(" ·"))[:160]
        by_key[k] = cur

    # Prefer canonical 4 keys order when present.
    preferred = ["КЭШ", "FCF", "Прямой долг", "Запас прочности"]
    final_keys: List[str] = []
    for k in preferred:
        if k in by_key and k not in final_keys:
            final_keys.append(k)
    for k in order:
        if k not in final_keys:
            final_keys.append(k)
    return [by_key[k] for k in final_keys[:4]]


def _normalize_conflicts(raw: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for c in raw[:12]:
        if not isinstance(c, dict):
            continue
        field = str(c.get("field") or "").strip()[:80]
        yahoo = str(c.get("yahoo") or "").strip()[:120]
        filing = str(c.get("filing") or "").strip()[:120]
        note = str(c.get("note") or "").strip()[:240]
        if not (field or yahoo or filing or note):
            continue
        out.append({"field": field or "—", "yahoo": yahoo, "filing": filing, "note": note})
    return out


def _normalize_evidence(raw: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for e in raw[:3]:
        if not isinstance(e, dict):
            continue
        topic = str(e.get("topic") or "other").strip()[:40]
        quote = str(e.get("quote") or "").strip()[:240]
        if quote:
            out.append({"topic": topic or "other", "quote": quote})
    return out


def build_reconcile_user_prompt(
    *,
    symbol: str,
    yahoo_fundament: Dict[str, Any],
    material: Dict[str, Any],
) -> str:
    body = str(material.get("content_text") or "")
    if len(body) > MAX_CONTENT_CHARS:
        body = body[:MAX_CONTENT_CHARS] + "\n…[truncated]"
    yahoo_payload = {
        "symbol": symbol,
        "metrics": _yahoo_metrics_for_prompt(yahoo_fundament),
        "margin_ru": yahoo_fundament.get("margin_ru") or "",
        "financing_ru": yahoo_fundament.get("financing_ru") or "",
    }
    meta = {
        "material_type": material.get("material_type") or "",
        "event_date": material.get("event_date") or "",
        "source_url": material.get("filing_url") or "",
        "source_name": material.get("source_name") or "",
    }
    return (
        f"Ticker: {symbol}\n"
        f"Material meta: {json.dumps(meta, ensure_ascii=False)}\n"
        f"Yahoo passport draft:\n{json.dumps(yahoo_payload, ensure_ascii=False)}\n\n"
        f"--- earnings material text ---\n{body}\n--- end ---"
    )


def merge_reconcile_into_fundament(
    yahoo_fundament: Dict[str, Any],
    structured: Dict[str, Any],
    *,
    filing_url: str = "",
) -> Dict[str, Any]:
    """Build draft fundament from Yahoo + LLM patch. Never copies pluses/risks/clients."""
    from services.trading_notebook import _normalize_fundament

    base = dict(yahoo_fundament) if isinstance(yahoo_fundament, dict) else {}
    metrics = _apply_metrics_patch(list(base.get("metrics") or []), structured.get("metrics_patch"))
    margin = structured.get("margin_ru")
    financing = structured.get("financing_ru")
    out = {
        "exchange": base.get("exchange") or "",
        "hq_ru": base.get("hq_ru") or "",
        "listing_origin_ru": base.get("listing_origin_ru") or "",
        # Nastya-only — keep empty in reconcile draft merge (UI must not overwrite).
        "key_clients_ru": "",
        "tagline": base.get("tagline") or "",
        "metrics": metrics,
        "margin_ru": (
            str(margin).strip()[:500]
            if margin is not None and str(margin).strip()
            else (base.get("margin_ru") or "")
        ),
        "financing_ru": (
            str(financing).strip()[:500]
            if financing is not None and str(financing).strip()
            else (base.get("financing_ru") or "")
        ),
        "pluses": [],
        "risks": [],
        "filing_url": filing_url or base.get("filing_url") or "",
    }
    period = str(structured.get("filing_period") or "").strip()
    if period:
        # Stamp period onto financing note lightly if not already mentioned.
        fin = str(out.get("financing_ru") or "")
        if period not in fin:
            out["financing_ru"] = (f"{fin} · период filing: {period}".strip(" ·"))[:500]
    return _normalize_fundament(out)


def reconcile_fundament_with_earnings_llm(
    sym: str,
    *,
    model: Optional[str] = None,
    dry_run: bool = False,
    http_timeout_sec: Optional[float] = None,
) -> Dict[str, Any]:
    """Yahoo draft + Ex99/IR text → LLM reconcile draft (not saved)."""
    from services.trading_notebook import suggest_fundament_from_yfinance

    u = str(sym or "").strip().upper()
    if not u:
        raise ValueError("ticker required")

    material = load_best_reconcile_material(u)
    body = str(material.get("content_text") or "")
    if len(body) < MIN_CONTENT_CHARS:
        raise MaterialMissingError(
            "Нет parsed earnings_material с текстом (≥"
            f"{MIN_CONTENT_CHARS} символов). Сначала ingest/parse Ex99/IR "
            "или откройте SEC / filings."
        )

    yahoo = suggest_fundament_from_yfinance(u)
    yahoo_fundament = yahoo.get("fundament") if isinstance(yahoo, dict) else {}
    if not isinstance(yahoo_fundament, dict):
        yahoo_fundament = {}

    user_prompt = build_reconcile_user_prompt(
        symbol=u,
        yahoo_fundament=yahoo_fundament,
        material=material,
    )
    token_plan = {
        "content_chars": len(body),
        "prompt_chars": len(user_prompt),
        "max_content_chars": MAX_CONTENT_CHARS,
    }
    if dry_run:
        return {
            "ticker": u,
            "status": "dry_run",
            "material_meta": {k: material.get(k) for k in (
                "id", "filing_url", "source_name", "material_type",
                "event_date", "parse_status",
            )},
            "token_plan": token_plan,
            "fundament": yahoo_fundament,
            "conflicts": [],
            "evidence_quotes": [],
            "sources": ["yfinance", "earnings_material"],
            "nastya_only": ["key_clients_ru", "pluses", "risks"],
            "note": "dry_run · LLM не вызывался",
        }

    try:
        from services.llm_service import LLMService, get_openai_http_timeout_prompt_entry
    except Exception as exc:
        raise ReconcileLlmError(f"LLM import failed: {exc}") from exc

    llm = LLMService()
    if not getattr(llm, "client", None):
        raise ReconcileLlmError("LLM client unavailable (ProxyAPI / keys)")

    timeout = http_timeout_sec
    if timeout is None:
        try:
            timeout = float(get_openai_http_timeout_prompt_entry())
        except Exception:
            timeout = float(getattr(llm, "timeout", 120) or 120)

    reconcile_model = (
        model
        or os.environ.get("NOTEBOOK_RECONCILE_MODEL")
        or os.environ.get("EARNINGS_EXTRACT_MODEL")
        or ""
    ).strip()
    kwargs: Dict[str, Any] = {
        "temperature": 0.1,
        "max_completion_tokens": DEFAULT_OUTPUT_TOKENS,
        "http_timeout_sec": timeout,
    }
    if reconcile_model:
        kwargs["model"] = reconcile_model

    try:
        resp = llm.generate_response(
            [{"role": "user", "content": user_prompt}],
            system_prompt=RECONCILE_SYSTEM_PROMPT,
            **kwargs,
        )
    except Exception as exc:
        from services.proxyapi_balance import balance_error_payload

        bal = balance_error_payload(exc)
        if bal:
            raise ReconcileLlmError(bal["message"]) from exc
        raise ReconcileLlmError(str(exc)) from exc

    if resp.get("api_error") or resp.get("error"):
        from services.proxyapi_balance import balance_error_payload

        err_text = str(resp.get("error") or resp.get("response") or "")
        bal = balance_error_payload(err_text)
        if bal:
            raise ReconcileLlmError(bal["message"])
        raise ReconcileLlmError(err_text or "LLM api_error")

    text = (resp.get("response") or "").strip()
    structured = _parse_json_object(text)
    if not structured:
        raise ReconcileLlmError("LLM ответ не JSON — повторите или сверьте вручную по filing")

    conflicts = _normalize_conflicts(structured.get("conflicts"))
    evidence = _normalize_evidence(structured.get("evidence_quotes"))
    fundament = merge_reconcile_into_fundament(
        yahoo_fundament,
        structured,
        filing_url=str(material.get("filing_url") or ""),
    )

    n_conflict = len(conflicts) + sum(
        1
        for p in (structured.get("metrics_patch") or [])
        if isinstance(p, dict) and str(p.get("status") or "").lower() == "conflict"
    )
    note = (
        f"черновик сверки LLM · конфликтов: {len(conflicts)}"
        + (f" · период {structured.get('filing_period')}" if structured.get("filing_period") else "")
        + " · сохраните OK"
    )
    return {
        "ticker": u,
        "status": "ok",
        "model": resp.get("model"),
        "usage": resp.get("usage"),
        "token_plan": token_plan,
        "filing_period": structured.get("filing_period"),
        "fundament": fundament,
        "conflicts": conflicts,
        "evidence_quotes": evidence,
        "conflict_count": n_conflict,
        "material_meta": {
            "id": material.get("id"),
            "filing_url": material.get("filing_url"),
            "source_name": material.get("source_name"),
            "material_type": material.get("material_type"),
            "event_date": material.get("event_date"),
            "parse_status": material.get("parse_status"),
            "content_chars": len(body),
        },
        "sources": ["yfinance", "earnings_material", "llm_reconcile"],
        "nastya_only": ["key_clients_ru", "pluses", "risks"],
        "filled": ["metrics", "margin_ru", "financing_ru", "filing_url"],
        "note": note,
    }
