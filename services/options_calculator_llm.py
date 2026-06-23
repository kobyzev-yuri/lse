"""
LLM-интерпретация результата калькулятора Put / Put Spread.
Маршрутизация — как у анализатора (generate_analyzer_llm_response).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — наставник по опционам для частного инвестора LSE.
Пиши по-русски, кратко и понятно (до 14 предложений + буллеты по сценариям).
Объясни смысл стратегии, breakeven, max loss/profit и ключевые строки таблицы сценариев.
Подчеркни: расчёт только intrinsic на экспирацию — IV, время и IV crush не моделируются.
Не давай категоричных торговых приказов — только образовательная интерпретация рисков."""


def interpret_calculator_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Текстовая интерпретация JSON-ответа POST /api/options/calculator."""
    if not result or result.get("error"):
        return {
            "status": "error",
            "error": result.get("error") or "нет данных для интерпретации",
        }

    try:
        from services.llm_service import LLMService, generate_analyzer_llm_response, get_openai_http_timeout_prompt_entry
    except Exception as exc:
        return {"status": "error", "error": f"LLM import: {exc}"}

    llm = LLMService()
    if not getattr(llm, "api_key", None):
        return {"status": "disabled", "error": "LLM недоступен (OPENAI_GPT_KEY / ProxyAPI)"}

    payload = _compact_calculator_for_llm(result)
    user_prompt = (
        "Интерпретируй расчёт опционной стратегии для инвестора.\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n\n"
        "Структура ответа:\n"
        "1) Что за стратегия и зачем (1-2 предложения)\n"
        "2) Вход, breakeven, max loss/profit — простыми словами\n"
        "3) При каком падении spot начинается прибыль (по таблице)\n"
        "4) 2-3 буллета: главные риски (IV crush, неверный страйк, earnings)\n"
        "5) Ограничения intrinsic-калькулятора"
    )

    timeout = get_openai_http_timeout_prompt_entry()
    call_kwargs: Dict[str, Any] = {
        "temperature": 0.2,
        "max_completion_tokens": 1000,
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
        "ticker": result.get("ticker"),
        "strategy": result.get("strategy"),
    }
    if resp.get("used_fallback_model"):
        out["used_fallback_model"] = True
    return out


def _compact_calculator_for_llm(result: Dict[str, Any]) -> Dict[str, Any]:
    inputs = {
        k: result.get(k)
        for k in (
            "ticker",
            "strategy",
            "spot",
            "contracts",
            "earnings_date",
            "expiration_date",
            "long_strike",
            "long_premium",
            "short_strike",
            "short_premium",
            "prefill_source",
        )
        if result.get(k) is not None
    }
    summary = {
        k: result.get(k)
        for k in (
            "entry_cost_usd",
            "breakeven",
            "breakeven_drop_pct",
            "max_loss_usd",
            "max_profit_usd",
            "spread_width",
            "note_ru",
        )
        if result.get(k) is not None
    }
    scenarios = result.get("scenarios") or []
    return {
        "inputs": inputs,
        "summary": summary,
        "scenarios": scenarios,
    }
