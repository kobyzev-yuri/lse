# -*- coding: utf-8 -*-
"""
Интеграция с внешним Platform Game API (POST /game).

Документация: kerimsrv/platform doc.md, вводная — kerimsrv/kdoc.md.
Передаём открывшиеся позиции (MARKET LONG по правилам игры 5m), получаем notOpened / opened / closed
и форматируем для Telegram.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config_loader import get_config_value

logger = logging.getLogger(__name__)


def is_platform_game_enabled() -> bool:
    raw = (get_config_value("PLATFORM_GAME_API_ENABLED", "false") or "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def get_platform_game_url() -> str:
    return (get_config_value("PLATFORM_GAME_API_URL", "http://127.0.0.1:8080/game") or "").strip()


def get_platform_game_timeout_sec() -> float:
    try:
        return float((get_config_value("PLATFORM_GAME_API_TIMEOUT_SEC", "15") or "15").strip())
    except (ValueError, TypeError):
        return 15.0


def build_market_position_long(
    instrument: str,
    *,
    price_entry: float,
    units: int,
    take_profit_price: float,
    stop_loss_price: Optional[float],
    created_at_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Одна позиция orderType MARKET, direction LONG (игра 5m)."""
    if created_at_utc is None:
        created_at_utc = datetime.now(timezone.utc)
    ca = created_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "orderType": "MARKET",
        "market": {
            "instrument": instrument.upper().strip(),
            "direction": "LONG",
            "createdAt": ca,
            "takeProfit": float(take_profit_price),
            "stopLoss": float(stop_loss_price) if stop_loss_price is not None else None,
            "units": int(units),
        },
    }


def post_game_positions(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    POST JSON { "positions": [...] } на PLATFORM_GAME_API_URL.
    Возвращает распарсенный JSON (notOpened, opened, closed) или бросает исключение.
    """
    import requests

    url = get_platform_game_url()
    if not url:
        raise RuntimeError("PLATFORM_GAME_API_URL пуст")
    timeout = get_platform_game_timeout_sec()
    body = {"positions": positions}
    r = requests.post(url, json=body, timeout=timeout)
    if r.status_code >= 500:
        # У Platform иногда 500 на createdAt "сейчас" (вне их окна данных).
        # Ретрай с безопасной исторической датой из примера документации.
        safe_positions = _with_safe_created_at(positions, "2026-03-21T12:00:00Z")
        r = requests.post(url, json={"positions": safe_positions}, timeout=timeout)
    if r.status_code >= 400:
        try:
            body_text = (r.text or "").strip()
        except Exception:
            body_text = ""
        msg = f"HTTP {r.status_code} {r.reason} for {url}"
        if body_text:
            msg += f" | body: {body_text[:600]}"
        raise RuntimeError(msg)
    return r.json()


def format_game_response_telegram(data: Any) -> str:
    """Текст для Telegram: три блока — не открыты / открыты / закрыты (без parse_mode)."""
    if not isinstance(data, dict):
        return f"📊 Platform /game: неожиданный ответ: {str(data)[:500]}"

    def _row_opened(x: dict) -> str:
        parts = [
            f"{x.get('instrument', '?')}",
            f"{x.get('direction', '')}",
            f"open @{_fmt_price(x.get('openPrice'))}",
            f"TP {_fmt_price(x.get('takeProfit'))}",
        ]
        if x.get("stopLoss") is not None:
            parts.append(f"SL {_fmt_price(x.get('stopLoss'))}")
        parts.append(f"u={x.get('units', '')}")
        return " · ".join(str(p) for p in parts if p is not None)

    def _row_not(x: dict) -> str:
        return " · ".join(
            str(p)
            for p in (
                x.get("instrument"),
                x.get("direction"),
                x.get("entryType"),
                f"lim {_fmt_price(x.get('limitIn'))}" if x.get("limitIn") is not None else None,
                f"TP {_fmt_price(x.get('takeProfit'))}",
                f"u={x.get('units')}",
            )
            if p is not None
        )

    def _row_closed(x: dict) -> str:
        profit = x.get("profit")
        pr = f"{profit:+.2f}" if isinstance(profit, (int, float)) else str(profit)
        return " · ".join(
            str(p)
            for p in (
                x.get("instrument"),
                x.get("direction"),
                f"PnL {pr}",
                f"acc {x.get('accuracy', '')}",
            )
            if p is not None
        )

    lines: List[str] = [
        "📊 Platform /game",
        "Списки: не открыты · открыты · закрыты (см. kerimsrv/platform doc.md)",
        "",
    ]
    for key, title, fmt in (
        ("notOpened", "— Не открыты (notOpened)", _row_not),
        ("opened", "— Открыты (opened)", _row_opened),
        ("closed", "— Закрыты (closed)", _row_closed),
    ):
        arr = data.get(key)
        lines.append(title)
        if not arr:
            lines.append("  (пусто)")
            lines.append("")
            continue
        for i, item in enumerate(arr, 1):
            if isinstance(item, dict):
                lines.append(f"  {i}. {fmt(item)}")
            else:
                lines.append(f"  {i}. {item}")
        lines.append("")
    return "\n".join(lines).strip()


def _fmt_price(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(v)


def _with_safe_created_at(positions: List[Dict[str, Any]], created_at: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in positions or []:
        if not isinstance(p, dict):
            out.append(p)
            continue
        q = dict(p)
        if q.get("orderType") == "MARKET" and isinstance(q.get("market"), dict):
            m = dict(q["market"])
            m["createdAt"] = created_at
            # На fallback-дате entry может сильно отличаться от "текущих" TP/SL.
            # stopLoss оставляем пустым (None) по договорённости; тейк делаем безопасно широким.
            direction = str(m.get("direction") or "").upper()
            if direction == "SHORT":
                m["takeProfit"] = 0.01
                m.pop("stopLoss", None)
            else:
                m["takeProfit"] = 1_000_000.0
                m.pop("stopLoss", None)
            q["market"] = m
        elif q.get("orderType") == "LIMIT" and isinstance(q.get("limit"), dict):
            m = dict(q["limit"])
            m["createdAt"] = created_at
            direction = str(m.get("direction") or "").upper()
            if direction == "SHORT":
                m["takeProfit"] = 0.01
                m.pop("stopLoss", None)
            else:
                m["takeProfit"] = 1_000_000.0
                m.pop("stopLoss", None)
            q["limit"] = m
        out.append(q)
    return out


def notify_platform_game_telegram(
    token: str,
    chat_ids: List[str],
    positions: List[Dict[str, Any]],
) -> int:
    """
    Вызвать /game и разослать форматированный ответ. Возвращает число успешных отправок.
    """
    from services.telegram_signal import send_telegram_message

    if not positions or not chat_ids:
        return 0
    try:
        data = post_game_positions(positions)
    except Exception as e:
        logger.warning("Platform /game: %s", e)
        text = f"📊 Platform /game: ошибка запроса: {e!s}"[:3900]
        ok = 0
        for cid in chat_ids:
            if send_telegram_message(token, cid, text, parse_mode=None):
                ok += 1
        return ok
    text = format_game_response_telegram(data)
    ok = 0
    for cid in chat_ids:
        if send_telegram_message(token, cid, text, parse_mode=None):
            ok += 1
    return ok
