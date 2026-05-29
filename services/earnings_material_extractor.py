"""LLM structured extraction from parsed earnings materials (ProxyAPI / OpenAI SDK)."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from typing import Any

from services.earnings_material_token_estimator import estimate_tokens, extraction_cycle_tokens

logger = logging.getLogger(__name__)

MAX_COMBINED_MATERIAL_CHARS = int(os.environ.get("EARNINGS_EXTRACT_MAX_CHARS", "90000"))
DEFAULT_OUTPUT_TOKENS = int(os.environ.get("EARNINGS_EXTRACT_OUTPUT_TOKENS", "3500"))

MATERIAL_TYPE_PRIORITY: dict[str, int] = {
    "transcript": 0,
    "third_party_transcript": 1,
    "press_release": 2,
    "sec_filing": 3,
    "other": 4,
    "presentation": 5,
    "follow_up_transcript": 6,
    "ir_event_page": 99,
}

EXTRACTION_SYSTEM_PROMPT = """You are a financial document extractor for an earnings intelligence pipeline.
Read the provided earnings materials (press release, transcript, SEC filing, presentation text).
Do NOT invent numbers or quotes. Use null when a field is not stated in the text.

Return ONE JSON object (no markdown) with this schema:
{
  "symbol": "TICKER",
  "event_date": "YYYY-MM-DD",
  "fiscal_period": "string or null",
  "revenue_actual": number or null,
  "revenue_estimate": number or null,
  "revenue_surprise_pct": number or null,
  "eps_actual": number or null,
  "eps_estimate": number or null,
  "eps_surprise_pct": number or null,
  "guidance": {
    "direction": "raised|lowered|inline|withdrawn|not_disclosed",
    "revenue_outlook": "string or null",
    "eps_outlook": "string or null",
    "capex_outlook": "string or null",
    "margin_outlook": "string or null"
  },
  "capex_notes": "string or null",
  "ai_demand_signals": ["string"],
  "margin_pressure_signals": ["string"],
  "inventory_or_supply_notes": ["string"],
  "management_tone": "bullish|cautious|mixed|defensive|not_clear",
  "qa_concerns": ["string"],
  "affected_tickers": [
    {"ticker": "SYMBOL", "relation": "peer|supplier|customer|competitor|sector_etf", "rationale": "string"}
  ],
  "scenario_hints": [
    {"scenario": "beat_selloff_pullback|beat_revaluation_down|miss_or_guide_breakdown|gap_up_follow_through|gap_up_fade|cross_earnings_contagion|capex_positive_for_infra_peers", "confidence": "low|medium|high", "rationale": "string"}
  ],
  "evidence_quotes": [
    {"topic": "guidance|capex|ai_demand|margin|other", "quote": "verbatim short quote from text"}
  ]
}

Rules:
- affected_tickers: only tickers explicitly mentioned or clearly implied (supply chain, peers, hyperscalers).
- evidence_quotes: max 5, each under 240 chars, must appear in the source text.
- scenario_hints: max 3; prefer evidence-based hints over speculation.
"""


def _parse_json_object(text: str) -> dict[str, Any] | None:
    body = (text or "").strip()
    if not body:
        return None
    fence = re.match(r"^```(?:json)?\s*\r?\n?", body, re.IGNORECASE)
    if fence:
        rest = body[fence.end() :]
        end = rest.rfind("```")
        if end != -1:
            body = rest[:end].strip()
    try:
        obj = json.loads(body)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = body.find("{"), body.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(body[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _material_sort_key(row: dict[str, Any]) -> tuple[int, int, int]:
    mtype = str(row.get("material_type") or "other")
    priority = MATERIAL_TYPE_PRIORITY.get(mtype, 50)
    text_len = len(str(row.get("content_text") or ""))
    return (priority, -text_len, int(row.get("id") or 0))


def select_materials_for_event(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    useful = [
        r
        for r in rows
        if str(r.get("parse_status") or "") in ("parsed", "extracted")
        and len(str(r.get("content_text") or "")) >= 400
    ]
    sorted_rows = sorted(useful, key=_material_sort_key)
    has_official_transcript = any(r.get("material_type") == "transcript" for r in sorted_rows)
    filtered: list[dict[str, Any]] = []
    for row in sorted_rows:
        mtype = str(row.get("material_type") or "")
        if has_official_transcript and mtype == "third_party_transcript":
            continue
        filtered.append(row)
    return filtered


def build_event_prompt(
    *,
    symbol: str,
    event_date: date | None,
    fiscal_period: str | None,
    materials: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Build user prompt and return (prompt, materials included)."""
    included: list[dict[str, Any]] = []
    chunks: list[str] = [
        f"Extract structured earnings facts for {symbol.upper()}",
        f"event_date={event_date.isoformat() if event_date else 'unknown'}",
        f"fiscal_period={fiscal_period or 'unknown'}",
        "",
    ]
    budget = MAX_COMBINED_MATERIAL_CHARS
    for row in materials:
        body = str(row.get("content_text") or "").strip()
        if not body:
            continue
        mtype = str(row.get("material_type") or "material")
        # Keep one long transcript; add compact factual sources (press/SEC/CFO).
        if mtype in ("transcript", "third_party_transcript") and any(
            str(r.get("material_type") or "") in ("transcript", "third_party_transcript") for r in included
        ):
            continue
        header = (
            f"=== {row.get('material_type', 'material').upper()} "
            f"({row.get('source_name') or 'source'}) ==="
        )
        block = f"{header}\n{body}\n"
        if len(block) > budget and budget < 2000:
            break
        if len(block) > budget:
            block = block[:budget] + "\n…[truncated]\n"
            budget = 0
        else:
            budget -= len(block)
        chunks.append(block)
        included.append(row)
        if budget <= 0:
            break
    return "\n".join(chunks), included


def plan_event_extraction_tokens(
    *,
    symbol: str,
    event_date: date | None,
    fiscal_period: str | None,
    materials: list[dict[str, Any]],
    output_tokens: int = DEFAULT_OUTPUT_TOKENS,
) -> dict[str, Any]:
    selected = select_materials_for_event(materials)
    user_prompt, included = build_event_prompt(
        symbol=symbol,
        event_date=event_date,
        fiscal_period=fiscal_period,
        materials=selected,
    )
    sys_tok = estimate_tokens(EXTRACTION_SYSTEM_PROMPT)
    user_tok = estimate_tokens(user_prompt)
    system_basis = sys_tok.get("tokens_exact") or sys_tok["tokens_est_primary"]
    user_basis = user_tok.get("tokens_exact") or user_tok["tokens_est_primary"]
    cycle = extraction_cycle_tokens(
        user_basis,
        system_prompt_tokens=system_basis,
        output_tokens=output_tokens,
    )
    per_material = []
    for row in included:
        tok = estimate_tokens(str(row.get("content_text") or ""))
        per_material.append(
            {
                "material_id": row.get("id"),
                "material_type": row.get("material_type"),
                "text_chars": len(str(row.get("content_text") or "")),
                "tokens_est": tok.get("tokens_exact") or tok["tokens_est_primary"],
            }
        )
    return {
        "symbol": symbol.upper(),
        "event_date": event_date.isoformat() if event_date else None,
        "fiscal_period": fiscal_period,
        "materials_available": len(materials),
        "materials_included": len(included),
        "included_material_ids": [r.get("id") for r in included],
        "per_material": per_material,
        "system_prompt_tokens": system_basis,
        "user_prompt_tokens": user_basis,
        "output_tokens_est": output_tokens,
        "total_tokens_est": cycle["total_tokens_est"],
        "input_tokens_est": system_basis + user_basis,
    }


def extract_event_facts_with_llm(
    *,
    symbol: str,
    event_date: date | None,
    fiscal_period: str | None,
    materials: list[dict[str, Any]],
    dry_run: bool = False,
    model: str | None = None,
    http_timeout_sec: float | None = None,
) -> dict[str, Any]:
    selected = select_materials_for_event(materials)
    if not selected:
        return {"status": "skipped", "reason": "no_parsed_materials"}

    plan = plan_event_extraction_tokens(
        symbol=symbol,
        event_date=event_date,
        fiscal_period=fiscal_period,
        materials=selected,
    )
    if dry_run:
        return {"status": "dry_run", "token_plan": plan}

    try:
        from services.llm_service import LLMService, get_openai_http_timeout_prompt_entry
    except Exception as exc:
        return {"status": "error", "reason": f"import: {exc}", "token_plan": plan}

    llm = LLMService()
    if not getattr(llm, "client", None):
        return {"status": "disabled", "reason": "LLM client unavailable", "token_plan": plan}

    user_prompt, included = build_event_prompt(
        symbol=symbol,
        event_date=event_date,
        fiscal_period=fiscal_period,
        materials=selected,
    )
    timeout = http_timeout_sec
    if timeout is None:
        try:
            timeout = float(get_openai_http_timeout_prompt_entry())
        except Exception:
            timeout = float(getattr(llm, "timeout", 120) or 120)

    extract_model = (model or os.environ.get("EARNINGS_EXTRACT_MODEL") or "").strip()
    kwargs: dict[str, Any] = {
        "temperature": 0.1,
        "max_completion_tokens": DEFAULT_OUTPUT_TOKENS,
        "http_timeout_sec": timeout,
    }
    if extract_model:
        kwargs["model"] = extract_model

    try:
        resp = llm.generate_response(
            [{"role": "user", "content": user_prompt}],
            system_prompt=EXTRACTION_SYSTEM_PROMPT,
            **kwargs,
        )
    except Exception as exc:
        logger.exception("earnings extract LLM failed: %s", exc)
        return {"status": "error", "reason": str(exc), "token_plan": plan}

    text = (resp.get("response") or "").strip()
    structured = _parse_json_object(text)
    out: dict[str, Any] = {
        "status": "ok" if structured else "parse_warning",
        "model": resp.get("model"),
        "usage": resp.get("usage"),
        "token_plan": plan,
        "structured": structured,
        "included_material_ids": [r.get("id") for r in included],
        "raw_response_preview": text[:2000] + ("…" if len(text) > 2000 else ""),
    }
    if structured is None:
        out["parse_note"] = "Response is not valid JSON"
    return out


def map_extraction_to_event_detail(structured: dict[str, Any]) -> dict[str, Any]:
    """Map LLM JSON to earnings_event_detail column payloads."""
    guidance = structured.get("guidance")
    if not isinstance(guidance, dict):
        guidance = {}
    affected = structured.get("affected_tickers")
    tickers_json: list[Any] = affected if isinstance(affected, list) else []

    def _num(key: str) -> float | None:
        val = structured.get(key)
        if val is None or val == "":
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    return {
        "fiscal_period": structured.get("fiscal_period"),
        "revenue_actual": _num("revenue_actual"),
        "revenue_estimate": _num("revenue_estimate"),
        "eps_actual": _num("eps_actual"),
        "eps_estimate": _num("eps_estimate"),
        "guidance_summary": {
            "guidance": guidance,
            "capex_notes": structured.get("capex_notes"),
            "ai_demand_signals": structured.get("ai_demand_signals") or [],
            "margin_pressure_signals": structured.get("margin_pressure_signals") or [],
            "inventory_or_supply_notes": structured.get("inventory_or_supply_notes") or [],
            "management_tone": structured.get("management_tone"),
            "qa_concerns": structured.get("qa_concerns") or [],
            "scenario_hints": structured.get("scenario_hints") or [],
            "evidence_quotes": structured.get("evidence_quotes") or [],
            "revenue_surprise_pct": structured.get("revenue_surprise_pct"),
            "eps_surprise_pct": structured.get("eps_surprise_pct"),
        },
        "affected_tickers": tickers_json,
    }
