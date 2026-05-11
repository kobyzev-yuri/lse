"""
LLM-оценка единого отчёта ml_data_quality: полнота, риски, применимость к задачам LSE.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def analyze_ml_data_quality_with_llm(bundle: Dict[str, Any], *, http_timeout_sec: Optional[float] = None) -> Dict[str, Any]:
    """
    Возвращает dict с полями status, model, structured (разбор JSON из ответа), raw_response (кратко).
    """
    try:
        from services.llm_service import LLMService, get_openai_http_timeout_prompt_entry
    except Exception as e:
        return {"status": "error", "reason": f"import: {e}"}

    llm = LLMService()
    if not getattr(llm, "client", None):
        return {"status": "disabled", "reason": "LLM client unavailable"}

    timeout = http_timeout_sec
    if timeout is None:
        try:
            timeout = float(get_openai_http_timeout_prompt_entry())
        except Exception:
            timeout = float(getattr(llm, "timeout", 120) or 120)

    system = """Ты ведущий ML-инженер и quant-аналитик в проекте LSE (акции US, GAME_5M, портфельная игра, knowledge_base, CatBoost).
На входе — машинный JSON отчёт о полноте данных и артефактов обучения. Не выдумывай числа: опирайся только на поля входного JSON.
Ответь ОДНИМ JSON-объектом (без markdown), со схемой:
{
  "summary_ru": "2–4 предложения: общее состояние данных для ML",
  "completeness": {"score_0_10": 0, "gaps": ["строка"], "strengths": ["строка"]},
  "quality_risks": ["перекос классов, утечки, плохой context_json, мало EARNINGS с outcome_json, …"],
  "applicability": {
    "game5m_entry_catboost": "not_ready|weak|moderate|good — кратко почему",
    "portfolio_daily": "…",
    "event_earnings_outcome_kb": "…",
    "recovery_ml": "…"
  },
  "recommended_next_steps": ["конкретный шаг 1", "…"],
  "human_labeling": {
    "priority": "low|medium|high",
    "suggestion_ru": "что размечать вручную в первую очередь для event/outcome и для сделок"
  }
}
Число score_0_10 — субъективная оценка полноты относительно типичного retail quant-пайплайна; обоснуй в gaps/strengths."""

    user = json.dumps(bundle, ensure_ascii=False, default=str)
    if len(user) > 120_000:
        user = user[:120_000] + "\n…[truncated]"

    try:
        resp = llm.generate_response(
            [{"role": "user", "content": user}],
            system_prompt=system,
            temperature=0.15,
            max_completion_tokens=2500,
            http_timeout_sec=timeout,
        )
        text = (resp.get("response") or "").strip()
    except Exception as e:
        logger.exception("ml_data_quality LLM: %s", e)
        return {"status": "error", "reason": str(e)}

    structured = _parse_json_object(text)
    out: Dict[str, Any] = {
        "status": "ok",
        "model": resp.get("model"),
        "usage": resp.get("usage"),
        "structured": structured,
        "raw_response_preview": text[:2000] + ("…" if len(text) > 2000 else ""),
    }
    if structured is None:
        out["status"] = "parse_warning"
        out["parse_note"] = "Ответ не распознан как JSON; см. raw_response_preview"
    return out


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return None
    m = re.search(r"\{[\s\S]*\}\s*$", text)
    chunk = m.group(0) if m else text
    try:
        obj = json.loads(chunk)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
