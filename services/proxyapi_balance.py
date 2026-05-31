"""ProxyAPI balance / quota error detection for ops alerts."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BALANCE_MARKERS = (
    "402",
    "insufficient balance",
    "insufficient_balance",
    "недостаточный баланс",
    "payment required",
    "quota",
)


def is_proxyapi_insufficient_balance(error: str | BaseException | None) -> bool:
    if error is None:
        return False
    blob = str(error).lower()
    return any(m in blob for m in BALANCE_MARKERS)


def proxyapi_balance_user_message(error: str | BaseException | None) -> str:
    return (
        "ProxyAPI 402: недостаточный баланс для LLM (часто Anthropic/claude-sonnet). "
        "Пополните https://proxyapi.ru или задайте EARNINGS_EXTRACT_MODEL / ANALYZER_LLM_MODEL на более дешёвую OpenAI-модель."
    )


def balance_error_payload(error: str | BaseException | None) -> dict[str, Any] | None:
    if not is_proxyapi_insufficient_balance(error):
        return None
    raw = str(error).strip()[:1200]
    return {
        "error_code": 402,
        "message": proxyapi_balance_user_message(error),
        "raw_error": raw,
    }


def default_balance_alert_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/last_earnings_llm_balance_alert.json")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "last_earnings_llm_balance_alert.json"


def write_earnings_llm_balance_alert(
    payload: dict[str, Any],
    *,
    project_root: Path | None = None,
) -> Path:
    out = default_balance_alert_path(project_root)
    out.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "active": True,
        "detected_at_utc": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    out.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out


def clear_earnings_llm_balance_alert(*, project_root: Path | None = None) -> None:
    out = default_balance_alert_path(project_root)
    if not out.is_file():
        return
    out.write_text(
        json.dumps(
            {"active": False, "cleared_at_utc": datetime.now(timezone.utc).isoformat()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def read_earnings_llm_balance_alert(*, project_root: Path | None = None) -> dict[str, Any] | None:
    out = default_balance_alert_path(project_root)
    if not out.is_file():
        return None
    try:
        raw = json.loads(out.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None
