"""
Геополитический контекст для LLM-промптов: ключевые слова и выдержки из полного списка
новостей (не только топ-5), иначе Ближний Восток / санкции / Тайвань часто не попадают в промпт.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Подстроки в нижнем регистре (совпадение по in); англ + рус.
GEO_KEYWORDS: Tuple[str, ...] = (
    "israel", "israeli", "iran", "iranian", "gaza", "hamas", "hezbollah",
    "lebanon", "syria", "yemen", "middle east", "middle-east",
    "ukraine", "ukrainian", "russia", "russian", "kremlin", "moscow", "kyiv", "kiev", "nato",
    "taiwan", "taiwanese", "strait of", "south china", "beijing", "xi jinping",
    "north korea", "pyongyang", "kim jong",
    "sanction", "embargo", "tariff", "trade war", "decoupling",
    "military", "invasion", "occupation", "troops", "border",
    "escalat", "ceasefire", "retaliat",
    "missile", "drone strike", "airstrike", "air strike", "bombing",
    "nuclear", "pentagon", "defense secretary",
    "geopolit", "geopolitical",
    "израил", "израиль", "иран", "газа", "хамас", "ливан", "сирия", "йемен",
    "украин", "росси", "кремл", "москв", "киев", "нато",
    "тайван", "китай", "пекин",
    "санкц", "эмбарго", "пошлин", "торговая война",
    "военн", "эскалац", "конфликт", "агресс", "удар", "ракет", "дрон",
    "геополит", "ближневост", "ближний восток",
    "risk-off", "risk off", "safe haven", "flight to quality",
)

DEEESC_KEYWORDS: Tuple[str, ...] = (
    "de-escalat",
    "deescalat",
    "stabiliz",
    "calm",
    "market priced",
    "уже учт",
    "стабилизац",
    "снижени",
    "переговор",
    "diplomatic",
    "talks progress",
)


def _item_blob(item: Dict[str, Any]) -> str:
    parts = [
        str(item.get("source") or ""),
        str(item.get("content") or ""),
        str(item.get("title") or ""),
    ]
    return "\n".join(parts).lower()


def is_geopolitical_item(item: Dict[str, Any]) -> bool:
    blob = _item_blob(item)
    return any(kw in blob for kw in GEO_KEYWORDS)


def build_geopolitical_block(
    news_data: List[Dict[str, Any]],
    max_items: int = 6,
    max_chars: int = 320,
) -> Tuple[str, str]:
    """
    Текст секции для user-промпта и нижний регистр объединённых выдержек (для подсказки деэскалации).

    Returns:
        (block, combined_snippets_lower) — block пустой, если нет совпадений.
    """
    if not news_data:
        return "", ""

    bullets: List[str] = []
    seen: set = set()
    combined: List[str] = []

    for item in news_data:
        if not is_geopolitical_item(item):
            continue
        src = (str(item.get("source") or "Unknown")).strip()
        raw = (str(item.get("content") or "")).strip().replace("\n", " ")
        if not raw:
            continue
        if len(raw) > max_chars:
            raw = raw[: max_chars - 3] + "..."
        sig = raw[:100]
        if sig in seen:
            continue
        seen.add(sig)

        ts = item.get("ts")
        ts_prefix = ""
        if ts is not None:
            try:
                ts_prefix = str(ts)[:19] + " "
            except Exception:
                ts_prefix = ""

        bullets.append(f"- [{ts_prefix}{src}] {raw}")
        combined.append(raw.lower())
        if len(bullets) >= max_items:
            break

    if not bullets:
        return "", ""

    block = (
        "Геополитика и глобальные риски (выдержки из новостей за период; проверь влияние на сектор и risk-off):\n"
        + "\n".join(bullets)
    )
    return block, " ".join(combined)


def geopolitical_followup_hint(full_text_lower: str) -> str:
    """
    Доп. инструкция в user_message, если в тексте новостей/геоблока есть маркеры геополитики.
    """
    if not full_text_lower or not any(kw in full_text_lower for kw in GEO_KEYWORDS):
        return ""
    has_deesc = any(d in full_text_lower for d in DEEESC_KEYWORDS)
    out = "\n\nВ новостях за период есть упоминания геополитики, санкций, военной эскалации или глобального risk-off."
    if has_deesc:
        out += (
            " Вместе с тем звучат деэскалация или стабилизация — учти: риски могут быть терпимы, "
            "рынок мог частично учесть сценарий; не исключай вход или удержание без необходимости."
        )
    else:
        out += (
            " Явно отрази это в key_factors и risks; при открытых позициях рассмотри превентивный выход "
            "при сильном risk-off, если это согласуется с техникой."
        )
    return out


def geo_cluster_bridge_hint(cluster_note: Optional[str], full_text_lower: str) -> str:
    """
    Если есть блок кластера и в новостях есть геополитика — просим LLM связать риски с совместными трендами цен кластера.
    """
    if not (cluster_note and str(cluster_note).strip()):
        return ""
    if not full_text_lower or not any(kw in full_text_lower for kw in GEO_KEYWORDS):
        return ""
    return (
        "\n\nСвязка геополитики и кластера (обязательно отрази в reasoning и хотя бы в одном key_factor): "
        "сопоставь геополитические риски из новостей с **трендами цен и тех. сигналами тикеров кластера** в блоке "
        "«Кластер и корреляция» выше. При сильной корреляции sector-wide risk-off часто тянет несколько имён вместе: "
        "если часть кластера уже показывает стресс (слабее цена, HOLD/SELL по сигналам), оцени, не входишь ли в отстающий "
        "актив под продолжение давления или наоборот в относительно устойчивое имя. Укажи, как гео-шок мог бы пройти "
        "по цепочке (например полупроводники, память, AI-инфра, энергия/логистика), если это уместно к данным тикерам."
    )
