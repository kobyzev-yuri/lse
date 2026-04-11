"""Общая эвристика: снимок JSON от старого анализатора vs текущая ветка."""
from __future__ import annotations

from typing import Any, Dict, List


def snapshot_staleness_warnings(data: Dict[str, Any]) -> List[str]:
    """Если отчёт писал старый код, поля и auto_config_override.updates могут быть неактуальны."""
    w: List[str] = []
    if "game_5m_config_hints" not in data:
        w.append(
            "В JSON нет ключа game_5m_config_hints — отчёт снят анализатором старее ветки с глобальными hints "
            "(или это не полный ответ /api/analyzer)."
        )
    practical = data.get("practical_parameter_suggestions")
    if isinstance(practical, list) and any(
        isinstance(p, dict) and str(p.get("parameter") or "").strip() == "take_profit_management"
        for p in practical
    ):
        w.append(
            "В practical есть take_profit_management (только текст) — в новой версии для missed upside "
            "добавлен числовой take_momentum_factor → GAME_5M_TAKE_MOMENTUM_FACTOR в auto_config_override."
        )
    if w:
        w.append(
            "Переснять снимок: если в shell задан ANALYZER_SNAPSHOT_URL, скрипт по умолчанию ходит по HTTP "
            "и ответ будет от контейнера (часто без вашего git pull) — используйте "
            "`python3 scripts/snapshot_analyzer_report.py --local --days 7` или `env -u ANALYZER_SNAPSHOT_URL ...`. "
            "Иначе: HTTP после деплоя образа с новым кодом."
        )
    return w
