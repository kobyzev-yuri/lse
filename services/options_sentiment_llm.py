"""
LLM-интерпретация отчёта option chain sentiment (ProxyAPI / OPENAI_*).
Маршрутизация — как у анализатора (resolve_analyzer_llm_base_model).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — аналитик опционного рынка для частного инвестора LSE.
Пиши по-русски, кратко и по делу (до 12 предложений + буллеты уровней).
Не давай категоричных торговых приказов («покупай/продавай») — только интерпретация позиционирования.
Учитывай источник данных (yfinance менее надёжен, чем Polygon snapshot).
Различай PCR по окну ±15% от spot и PCR по всей цепочке, если оба есть.
Отдельно: осторожность, ограничения данных, что проверить дополнительно."""


def interpret_options_chain_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Текстовая интерпретация готового JSON-отчёта сентимента."""
    if not report or report.get("status") not in ("ok", None):
        return {
            "status": "error",
            "error": report.get("error") or "нет данных для интерпретации",
        }

    try:
        from services.llm_service import (
            LLMService,
            get_openai_http_timeout_prompt_entry,
            resolve_analyzer_llm_base_model,
        )
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
        "4) Max pain vs spot — что это может значить (эвристика)\n"
        "5) Ограничения источника и что не выводить из одного снимка"
    )

    timeout = get_openai_http_timeout_prompt_entry()
    call_kwargs: Dict[str, Any] = {
        "temperature": 0.2,
        "max_completion_tokens": 900,
    }
    if timeout:
        call_kwargs["http_timeout_sec"] = float(timeout)

    base_url, model = resolve_analyzer_llm_base_model()
    try:
        resp = llm.generate_response_with_model(
            base_url,
            model,
            [{"role": "user", "content": user_prompt}],
            system_prompt=SYSTEM_PROMPT,
            **call_kwargs,
        )
    except Exception as exc:
        logger.exception("options sentiment LLM failed")
        return {"status": "error", "error": str(exc), "model": model}

    if not resp or resp.get("api_error") or resp.get("error"):
        return {
            "status": "error",
            "error": str((resp or {}).get("error") or "LLM API error"),
            "model": model,
        }

    text = (resp.get("response") or "").strip()
    if not text:
        return {"status": "error", "error": "пустой ответ LLM", "model": model}

    return {
        "status": "ok",
        "interpretation_ru": text,
        "model": resp.get("model") or model,
        "usage": resp.get("usage"),
        "ticker": report.get("ticker"),
        "source": report.get("source"),
    }


def _compact_report_for_llm(report: Dict[str, Any]) -> Dict[str, Any]:
    """Урезанный отчёт без тяжёлых таблиц."""
    scope = report.get("analysis_scope") or {}
    out: Dict[str, Any] = {
        "ticker": report.get("ticker"),
        "source": report.get("source"),
        "expiration_date": report.get("expiration_date"),
        "spot": report.get("spot"),
        "spot_source": scope.get("spot_source"),
        "sentiment_label": report.get("sentiment_label"),
        "sentiment_score": report.get("sentiment_score"),
        "sentiment_summary_ru": report.get("sentiment_summary_ru"),
        "max_pain_strike": report.get("max_pain_strike"),
        "totals_window": report.get("totals"),
        "totals_full_chain": report.get("totals_full_chain"),
        "analysis_scope": scope,
        "key_strikes_oi": (report.get("key_strikes_oi") or [])[:6],
        "data_quality": report.get("data_quality"),
    }
    return {k: v for k, v in out.items() if v is not None}
