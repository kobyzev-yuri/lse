"""
LLM-интерпретация отчёта option chain sentiment (ProxyAPI / OPENAI_*).
Маршрутизация — как у анализатора (resolve_analyzer_llm_base_model + LLM_COMPARE_MODELS fallback).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — аналитик опционного рынка для частного инвестора LSE.
Пиши по-русски, кратко и по делу (до 12 предложений + буллеты уровней).
Не давай категоричных торговых приказов («покупай/продавай») — только интерпретация позиционирования.
Источник: Polygon snapshot или yfinance option_chain — OI из одного clearing (OCC), PCR OI сопоставимы.
Различай totals_window (±15% от spot, score/карточка) и totals_full_chain (вся доска).
Используй calendar_days_to_expiration из JSON — не угадывай срок до экспирации.
Max pain — эвристика; сила сигнала зависит от days_to_expiration (близкая exp ≠ далёкая LEAPS).
Отдельно: осторожность, ограничения данных, что проверить дополнительно."""


def interpret_options_chain_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Текстовая интерпретация готового JSON-отчёта сентимента."""
    if not report or report.get("status") not in ("ok", None):
        return {
            "status": "error",
            "error": report.get("error") or "нет данных для интерпретации",
        }

    try:
        from services.llm_service import LLMService, generate_analyzer_llm_response, get_openai_http_timeout_prompt_entry
    except Exception as exc:
        return {"status": "error", "error": f"LLM import: {exc}"}

    llm = LLMService()
    if not getattr(llm, "api_key", None):
        return {"status": "disabled", "error": "LLM недоступен (OPENAI_GPT_KEY / ProxyAPI)"}

    payload = _compact_report_for_llm(report)
    user_prompt = (
        "Интерпретируй отчёт option chain для инвестора.\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n\n"
        "Структура ответа:\n"
        "1) Суть сентимента (1-2 предложения)\n"
        "2) Насколько согласованы PCR volume/OI и score\n"
        "3) Ключевые страйки-магниты (2-4 буллета)\n"
        "4) Max pain vs spot — что это может значить (эвристика; учти calendar_days_to_expiration)\n"
        "5) Ограничения источника и что не выводить из одного снимка"
    )

    timeout = get_openai_http_timeout_prompt_entry()
    call_kwargs: Dict[str, Any] = {
        "temperature": 0.2,
        "max_completion_tokens": 900,
    }
    if timeout:
        call_kwargs["http_timeout_sec"] = float(timeout)

    resp = generate_analyzer_llm_response(
        llm,
        [{"role": "user", "content": user_prompt}],
        system_prompt=SYSTEM_PROMPT,
        **call_kwargs,
    )

    if resp.get("api_error") or resp.get("error"):
        return {
            "status": "error",
            "error": str(resp.get("error") or "LLM API error"),
            "model": resp.get("model"),
        }

    text = (resp.get("response") or "").strip()
    if not text:
        return {"status": "error", "error": "пустой ответ LLM", "model": resp.get("model")}

    out: Dict[str, Any] = {
        "status": "ok",
        "interpretation_ru": text,
        "model": resp.get("model"),
        "usage": resp.get("usage"),
        "ticker": report.get("ticker"),
        "source": report.get("source"),
    }
    if resp.get("used_fallback_model"):
        out["used_fallback_model"] = True
    return out


def _parse_expiration_date(raw: Any) -> Optional[date]:
    if not raw:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    s = str(raw).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _days_to_expiration(expiration_date: Any, *, as_of: Optional[date] = None) -> Optional[int]:
    exp = _parse_expiration_date(expiration_date)
    if not exp:
        return None
    today = as_of or date.today()
    return (exp - today).days


def _compact_report_for_llm(report: Dict[str, Any]) -> Dict[str, Any]:
    """Урезанный отчёт без тяжёлых таблиц."""
    scope = report.get("analysis_scope") or {}
    as_of = date.today().isoformat()
    exp = report.get("expiration_date")
    dte = _days_to_expiration(exp)
    out: Dict[str, Any] = {
        "ticker": report.get("ticker"),
        "source": report.get("source"),
        "as_of_date": as_of,
        "expiration_date": exp,
        "calendar_days_to_expiration": dte,
        "spot": report.get("spot"),
        "spot_source": scope.get("spot_source"),
        "sentiment_label": report.get("sentiment_label"),
        "sentiment_score": report.get("sentiment_score"),
        "sentiment_summary_ru": report.get("sentiment_summary_ru"),
        "max_pain_strike": report.get("max_pain_strike"),
        "oi_available": report.get("oi_available"),
        "barriers_mode": report.get("barriers_mode"),
        "totals_window": report.get("totals"),
        "totals_full_chain": report.get("totals_full_chain"),
        "analysis_scope": scope,
        "key_strikes_oi": (report.get("key_strikes_oi") or [])[:6],
        "key_strikes_volume": (report.get("key_strikes_volume") or [])[:6],
        "data_quality": report.get("data_quality"),
    }
    if dte is not None:
        if dte <= 0:
            out["expiration_note_ru"] = "Экспирация сегодня или уже прошла — max pain и «магниты» к exp наиболее релевантны."
        elif dte <= 21:
            out["expiration_note_ru"] = (
                f"До экспирации {dte} календ. дн. — ближний горизонт; max pain и OI у текущего spot важнее, "
                "чем для LEAPS."
            )
        else:
            out["expiration_note_ru"] = (
                f"До экспирации {dte} календ. дн. — max pain слабее как «цель к дате», OI может быть стратегическим."
            )
    return {k: v for k, v in out.items() if v is not None}
