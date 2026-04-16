"""
Telegram Bot для LSE Trading System
Основной класс бота для работы с независимыми инструментами (золото, валютные пары)
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import asyncio
import json
import html
import logging
import math
import re
import uuid
from io import BytesIO
from typing import Optional, Dict, Any, List, Set, Tuple
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

from analyst_agent import AnalystAgent
from services.vector_kb import VectorKB
from config_loader import get_config_value, get_closed_positions_report_limits, get_use_llm_for_analyst

logger = logging.getLogger(__name__)

# Лимит длины одного сообщения Telegram (API допускает 4096)
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
HELP_CHUNK_SIZE = 4000


def _telegram_closed_report_limits() -> tuple[int, int]:
    """Лимиты для /closed и /closed_impulse. Совпадают с веб /reports/closed (TELEGRAM_CLOSED_REPORT_*)."""
    return get_closed_positions_report_limits()


def _split_message_chunks(text: str, max_len: int = HELP_CHUNK_SIZE) -> List[str]:
    """Разбивает текст на части не длиннее max_len, по возможности по переносам строк."""
    if not text or len(text) <= max_len:
        return [text] if text else []
    chunks = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        block = rest[:max_len]
        last_nl = block.rfind("\n")
        if last_nl > max_len // 2:
            cut = last_nl + 1
        else:
            cut = max_len
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip()
    return chunks


def _escape_markdown(text: str) -> str:
    """Экранирует символы, ломающие Telegram Markdown (* _ [ ] `)."""
    if not text:
        return ""
    s = str(text)
    for c in ("\\", "_", "*", "[", "]", "`"):
        s = s.replace(c, "\\" + c)
    return s


def _normalize_ticker(ticker: str) -> str:
    """
    Нормализует тикер: исправляет распространённые ошибки (GC-F -> GC=F, GBPUSD-X -> GBPUSD=X).
    """
    if not ticker:
        return ticker
    ticker = ticker.upper().strip()
    # Исправляем дефис на = для фьючерсов и валют
    if ticker.endswith("-F") or ticker.endswith("-X"):
        ticker = ticker[:-2] + "=" + ticker[-1]
    # Исправляем дефис в середине для валютных пар (GBP-USD -> GBPUSD=X)
    if "-" in ticker and len(ticker) >= 6:
        parts = ticker.split("-")
        if len(parts) == 2 and len(parts[0]) == 3 and len(parts[1]) == 3:
            ticker = parts[0] + parts[1] + "=X"
    return ticker


def _unique_report_filename(title: str) -> str:
    """Уникальное имя файла отчёта: видно в Telegram как заголовок, по нажатию открывается file://.../имя.html"""
    ts = datetime.now().strftime("%Y-%m-%d %H-%M")
    short_id = uuid.uuid4().hex[:6]
    return f"{title} {ts} {short_id}.html"


def _build_help_html(help_text: str) -> str:
    """Преобразует текст справки (markdown-подобный) в HTML для отправки файлом (как /closed)."""
    if not help_text or not help_text.strip():
        return "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Справка</title></head><body><p>Нет содержимого.</p></body></html>"
    raw = help_text.strip()
    # Убираем экранирование Markdown для Telegram (в HTML не нужно)
    raw = raw.replace("\\_", "_").replace("\\.", ".")
    # Плейсхолдеры, чтобы не экранировать разметку
    raw = raw.replace("**", "\x01")
    raw = raw.replace("`", "\x02")
    escaped = html.escape(raw)
    # Чередование тегов для ** (bold)
    parts = escaped.split("\x01")
    buf = []
    for i, p in enumerate(parts):
        if i % 2 == 1:
            buf.append("<strong>" + p + "</strong>")
        else:
            buf.append(p)
    escaped = "".join(buf)
    # Чередование тегов для ` (code)
    parts = escaped.split("\x02")
    buf = []
    for i, p in enumerate(parts):
        if i % 2 == 1:
            buf.append("<code>" + p + "</code>")
        else:
            buf.append(p)
    escaped = "".join(buf)
    # Абзацы и переносы
    escaped = escaped.replace("\n\n", "</p><p>").replace("\n", "<br>\n")
    body = "<p>" + escaped + "</p>"
    return (
        "<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'><title>Справка по командам</title>"
        "<style>body{font-family:sans-serif;max-width:720px;margin:1em auto;padding:0 1em;line-height:1.4}"
        "code{background:#f0f0f0;padding:2px 4px;border-radius:3px}"
        "p{margin:0.6em 0}</style></head><body><h1>Справка по командам</h1>"
        + body +
        "</body></html>"
    )


def _ts_msk(ts) -> str:
    if ts is None:
        return "—"
    try:
        import pandas as pd
        t = pd.Timestamp(ts)
        if t.tzinfo is not None:
            t = t.tz_convert("Europe/Moscow")
        return t.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(ts)[:16] if ts else "—"


def _build_platform_game_status_html(
    status_key: str,
    title: str,
    items: List[Dict[str, Any]],
    *,
    generated_at: Optional[str] = None,
) -> str:
    """HTML-отчёт по одному статусу ответа Platform /game (notOpened/opened/closed)."""
    generated = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    rows: List[str] = []
    # Сортировка как просили: instrument, createdAt.
    if isinstance(items, list):
        try:
            items = sorted(
                items,
                key=lambda x: (
                    str((x or {}).get("instrument") or ""),
                    str((x or {}).get("createdAt") or ""),
                ),
            )
        except Exception:
            pass
    if status_key == "closed":
        headers = ("instrument", "direction", "entryType", "createdAt", "units", "openPrice", "openTime", "closePrice", "closeTime", "profit", "accuracy")
    elif status_key == "opened":
        headers = ("instrument", "direction", "entryType", "createdAt", "openPrice", "openTime", "takeProfit", "stopLoss", "units")
    else:
        headers = ("instrument", "direction", "entryType", "createdAt", "limitIn", "takeProfit", "stopLoss", "units")

    if not items:
        rows.append(f"<tr><td colspan='{len(headers)}'>Пусто</td></tr>")
    else:
        for it in items:
            if not isinstance(it, dict):
                rows.append(f"<tr><td colspan='{len(headers)}'>{html.escape(str(it))}</td></tr>")
                continue
            tds = []
            for h in headers:
                v = it.get(h)
                if isinstance(v, float):
                    s = f"{v:.4f}".rstrip("0").rstrip(".")
                else:
                    s = "—" if v is None else str(v)
                tds.append(f"<td>{html.escape(s)}</td>")
            rows.append("<tr>" + "".join(tds) + "</tr>")

    th = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body_rows = "".join(rows)
    return (
        "<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:Arial,sans-serif;margin:14px}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #ddd;padding:6px 8px;font-size:12px}th{background:#f6f6f6;text-align:left}"
        "h1{font-size:18px;margin:0 0 6px 0}.meta{color:#666;font-size:12px;margin:0 0 10px 0}</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
        f"<p class='meta'>Сформировано: {html.escape(generated)} · Статус: {html.escape(status_key)}</p>"
        f"<table><thead><tr>{th}</tr></thead><tbody>{body_rows}</tbody></table>"
        "</body></html>"
    )


def _build_prompt_entry_html(payload: Dict[str, Any]) -> str:
    """Шаблон принятия решения: отчёт для человека; для портфеля/тикера — в отчёте промпт для LLM (system, user, ответ)."""
    ticker = payload.get("ticker")
    game_label = payload.get("game_label")
    if game_label is None and ticker:
        try:
            from services.ticker_groups import get_tickers_game_5m
            game_label = "игра 5m" if ticker in (get_tickers_game_5m() or []) else "портфель"
        except Exception:
            game_label = "портфель"
    if game_label is None:
        game_label = "портфель"
    title = f"Шаблон решения ({game_label}) — {ticker}" if ticker else "Шаблон решения о входе (пустой шаблон)"
    note = payload.get("note")
    system = (payload.get("system_prompt") or "").strip()
    user = (payload.get("user_prompt") or payload.get("user_template") or "").strip()
    llm_response = (payload.get("llm_response") or "").strip()
    llm_analysis = payload.get("llm_analysis")
    decision = payload.get("decision")
    technical_signal = payload.get("technical_signal")

    def _pre(s: str) -> str:
        return html.escape(s) if s else "—"

    parts = [
        "<!DOCTYPE html>",
        '<html lang="ru"><head><meta charset="utf-8">',
        f"<title>{html.escape(title)}</title>",
        "<style>",
        "body { font-family: sans-serif; max-width: 900px; margin: 1em auto; padding: 0 1em; }",
        "h1 { font-size: 1.2em; }",
        "h2 { font-size: 1em; margin-top: 1.2em; color: #333; }",
        "pre { white-space: pre-wrap; word-break: break-word; background: #f8f8f8; padding: 0.8em; border-radius: 4px; border: 1px solid #eee; overflow-x: auto; }",
        ".note { background: #fff3cd; padding: 0.6em; border-radius: 4px; margin-bottom: 1em; }",
        ".meta { color: #666; font-size: 0.9em; margin-top: 0.5em; }",
        "ul { margin: 0.3em 0; }",
        "</style></head><body>",
        f"<h1>{html.escape(title)}</h1>",
    ]
    if note:
        parts.append(f'<p class="note">{_pre(note)}</p>')
    if decision is not None or technical_signal is not None:
        meta_parts = []
        if decision is not None:
            meta_parts.append(f"Решение: {html.escape(str(decision))}")
        if technical_signal is not None:
            meta_parts.append(f"Тех. сигнал: {html.escape(str(technical_signal))}")
        parts.append(f'<p class="meta">{"; ".join(meta_parts)}</p>')

    parts.append("<h2>System prompt</h2>")
    parts.append(f"<pre>{_pre(system)}</pre>")
    parts.append("<h2>User prompt</h2>")
    parts.append(f"<pre>{_pre(user)}</pre>")

    if llm_response:
        parts.append("<h2>Ответ LLM</h2>")
        parts.append(f"<pre>{_pre(llm_response)}</pre>")
    if llm_analysis and isinstance(llm_analysis, dict):
        parts.append("<h2>LLM analysis (parsed)</h2>")
        parts.append("<ul>")
        for k, v in llm_analysis.items():
            if v is None:
                continue
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            parts.append(f"<li><strong>{html.escape(str(k))}:</strong> {html.escape(str(v))}</li>")
        parts.append("</ul>")

    parts.append("</body></html>")
    return "\n".join(parts)


# Общие стили для отчётов prompt_entry (portfolio и game5m) — единый формат
_PROMPT_ENTRY_REPORT_CSS = """
body { font-family: sans-serif; max-width: 900px; margin: 1em auto; padding: 0 1em; }
h1,h2,h3 { font-size: 1.1em; margin-top: 1em; color: #333; }
h2 { border-bottom: 1px solid #ddd; padding-bottom: 0.2em; }
h3 { font-size: 1em; margin-top: 0.8em; color: #444; }
pre { white-space: pre-wrap; word-break: break-word; background: #f8f8f8; padding: 0.6em; border-radius: 4px; font-size: 0.9em; }
.cluster { background: #e8f4f8; padding: 0.8em; border-radius: 4px; margin-bottom: 1em; }
.ticker-section { margin-top: 1.5em; }
.meta { color: #666; font-size: 0.9em; margin: 0.3em 0; }
.corr { font-size: 0.85em; color: #555; margin: 0.2em 0; }
.context-block { background: #fafafa; border-left: 3px solid #ccc; padding: 0.5em 0.8em; margin: 0.5em 0; font-size: 0.9em; white-space: pre-wrap; word-break: break-word; }
.intro { color: #555; font-size: 0.9em; margin-bottom: 1em; }
"""

# Компактный отчёт recommend5m: таблица технический + краткий LLM, без списков корреляций
_RECOMMEND5M_COMPACT_CSS = """
body { font-family: sans-serif; max-width: 820px; margin: 1em auto; padding: 0 1em; }
h1 { font-size: 1.15em; color: #333; margin-bottom: 0.5em; }
table { border-collapse: collapse; width: 100%; font-size: 0.9em; }
th, td { border: 1px solid #ddd; padding: 0.4em 0.6em; text-align: left; }
th { background: #e8f4f8; font-weight: 600; }
tr:nth-child(even) { background: #fafafa; }
.llm-cell { font-size: 0.85em; color: #555; max-width: 28em; }
.decision-buy { color: #0a6; }
.decision-hold { color: #666; }
.decision-sell { color: #c33; }
"""


def _build_recommend5m_compact_html(per_ticker_results: List[Dict[str, Any]], days: int = 5) -> str:
    """Компактный HTML для /recommend5m: таблица технический сигнал + краткий вывод LLM при наличии."""
    def _pre(s: str) -> str:
        return html.escape(str(s)) if s is not None and s != "" else "—"

    parts = [
        "<!DOCTYPE html>",
        '<html lang="ru"><head><meta charset="utf-8">',
        "<title>Рекомендация 5m</title>",
        "<style>", _RECOMMEND5M_COMPACT_CSS, "</style></head><body>",
        f"<h1>Рекомендация 5m (технический сигнал + LLM)</h1>",
        f"<p>Период: {days} дн. Подробный отчёт: /prompt_entry game5m</p>",
        "<table><thead><tr>",
        "<th>Тикер</th><th>Решение</th><th>Цена</th><th>RSI</th><th>Qwen критерии</th><th>Имп. 2ч%</th><th>Откат%</th><th>Вол%</th><th>Upside%</th><th>Downside%</th><th>P(up/down)</th><th>Период</th><th>Вход</th>",
        "</tr></thead><tbody>",
    ]
    n_cols = 13
    for r in per_ticker_results:
        ticker = r.get("ticker") or "—"
        decision = r.get("decision") or "—"
        price = r.get("price")
        rsi = r.get("rsi_5m")
        mom = r.get("momentum_2h_pct")
        vol = r.get("volatility_5m_pct")
        qwen_verdict = r.get("qwen_checklist_verdict")
        pullback = r.get("pullback_from_high_pct")
        upside = r.get("estimated_upside_pct_day")
        upside_raw = r.get("estimated_upside_forecast_raw_pct")
        downside = r.get("estimated_downside_pct_day")
        prob_up = r.get("prob_up")
        prob_down = r.get("prob_down")
        period = r.get("period_str")
        entry = r.get("entry_advice") or "—"
        if decision == "NO_DATA":
            parts.append(f"<tr><td>{_pre(ticker)}</td><td colspan=\"{n_cols - 1}\">Нет 5m данных</td></tr>")
            continue
        dec_cls = "decision-buy" if decision in ("BUY", "STRONG_BUY") else ("decision-sell" if decision == "SELL" else "decision-hold")
        price_s = f"{price:.2f}" if price is not None else "—"
        rsi_s = f"{rsi:.1f}" if rsi is not None else "—"
        mom_s = f"{mom:+.2f}%" if mom is not None else "—"
        vol_s = f"{vol:.2f}%" if vol is not None else "—"
        qwen_s = _pre(qwen_verdict)
        pullback_s = f"{pullback:.2f}%" if pullback is not None else "—"
        if upside is not None:
            try:
                ur = float(upside_raw) if upside_raw is not None else None
                if ur is not None and abs(ur - float(upside)) > 0.02:
                    upside_s = f"{upside:+.1f}% (прогн. {ur:+.1f}%)"
                else:
                    upside_s = f"{upside:+.1f}%"
            except (TypeError, ValueError):
                upside_s = f"{upside:+.1f}%"
        else:
            upside_s = "—"
        downside_s = f"−{downside:.1f}%" if downside is not None else "—"
        prob_s = f"{prob_up:.2f}/{prob_down:.2f}" if (prob_up is not None and prob_down is not None) else "—"
        parts.append(
            f"<tr><td>{_pre(ticker)}</td>"
            f"<td class=\"{dec_cls}\">{_pre(decision)}</td>"
            f"<td>{price_s}</td>"
            f"<td>{rsi_s}</td>"
            f"<td>{qwen_s}</td>"
            f"<td>{mom_s}</td>"
            f"<td>{pullback_s}</td>"
            f"<td>{vol_s}</td>"
            f"<td>{upside_s}</td>"
            f"<td>{downside_s}</td>"
            f"<td>{prob_s}</td>"
            f"<td>{_pre(period)}</td>"
            f"<td>{_pre(entry)}</td></tr>"
        )
        llm_reasoning = r.get("llm_reasoning") or r.get("llm_correlation_reasoning")
        llm_factors = r.get("llm_key_factors")
        if llm_reasoning or llm_factors:
            llm_text = (llm_reasoning or "")[:400] if llm_reasoning else ""
            if llm_factors and isinstance(llm_factors, list):
                llm_text += " " + ", ".join(str(f)[:80] for f in llm_factors[:5])
            if llm_text.strip():
                parts.append(f'<tr><td colspan="{n_cols}" class="llm-cell">LLM: {_pre(llm_text.strip())}</td></tr>')
    parts.append("</tbody></table></body></html>")
    return "\n".join(parts)


def _build_prompt_entry_all_html(
    cluster_tickers: List[str],
    correlation_note: Optional[str],
    per_ticker_payloads: List[Dict[str, Any]],
) -> str:
    """HTML для /prompt_entry portfolio: тот же формат, что и game5m (тикер — решение, контекст, ответ)."""
    def _pre(s: str) -> str:
        return html.escape(s) if s else "—"

    parts = [
        "<!DOCTYPE html>",
        '<html lang="ru"><head><meta charset="utf-8">',
        "<title>Шаблон решения: игра Портфель</title>",
        "<style>", _PROMPT_ENTRY_REPORT_CSS, "</style></head><body>",
        "<h1>Шаблон принятия решения: игра «Портфель»</h1>",
        '<p class="intro">Контекст по параметрам и тикерам портфельной игры. <strong>Отчёт для человека:</strong> ниже по каждому тикеру — контекст (вход в LLM) и ответ модели. Вход/выход и тейк/стоп — портфельные.</p>',
        f'<p class="cluster"><strong>Кластер:</strong> {html.escape(", ".join(cluster_tickers))}</p>',
    ]
    if correlation_note:
        parts.append(f'<p class="cluster">{_pre(correlation_note)}</p>')

    for p in per_ticker_payloads:
        ticker = p.get("ticker") or "—"
        decision = p.get("decision")
        user = (p.get("user_prompt") or "").strip()
        llm_response = (p.get("llm_response") or "").strip()
        note = p.get("note")
        parts.append(f'<div class="ticker-section"><h2>{html.escape(str(ticker))} — {html.escape(str(decision) if decision is not None else "—")}</h2>')
        if note:
            parts.append(f'<p class="meta">{_pre(note)}</p>')
        parts.append("<h3>Контекст (входные данные для решения)</h3>")
        parts.append(f'<div class="context-block">{_pre(user)}</div>')
        parts.append("<h3>Ответ LLM</h3>")
        parts.append(f"<pre>{_pre(llm_response) if llm_response else _pre('—')}</pre>")
        parts.append("</div>")

    parts.append("</body></html>")
    return "\n".join(parts)


def _build_cluster_note_for_5m_llm(
    ticker: str,
    full_list: List[str],
    correlation_matrix: Optional[Dict[str, Dict[str, float]]],
    tech_by_ticker: Dict[str, Dict[str, Any]],
) -> Optional[str]:
    """Алиас к общей функции (services.cluster_recommend.build_cluster_note_for_5m_llm)."""
    from services.cluster_recommend import build_cluster_note_for_5m_llm as _build
    return _build(ticker, full_list, correlation_matrix, tech_by_ticker)


def _build_prompt_entry_game5m_html(
    cluster_tickers: List[str],
    correlation_note: Optional[str],
    per_ticker_results: List[Dict[str, Any]],
    correlation_matrix: Optional[Dict[str, Dict[str, float]]] = None,
    correlation_tickers: Optional[List[str]] = None,
    extra_tech_by_ticker: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """HTML для /prompt_entry game5m: тот же формат, что и portfolio.
    correlation_tickers: полный список для матрицы. extra_tech_by_ticker: цена/RSI для тикеров не из игры 5m (MEDIUM/LONG)."""
    from services.llm_service import format_entry_fusion_news_influence_explanation_ru

    def _pre(s: str) -> str:
        return html.escape(s) if s else "—"

    def _corr_row(ticker: str, others: List[str], corr: Dict[str, Dict[str, float]]) -> List[str]:
        out = []
        for o in others:
            c = corr.get(ticker, {}).get(o) or corr.get(o, {}).get(ticker)
            if c is not None:
                try:
                    out.append(f"{o} {float(c):+.2f}")
                except (TypeError, ValueError):
                    pass
        return out[:12]

    parts = [
        "<!DOCTYPE html>",
        '<html lang="ru"><head><meta charset="utf-8">',
        "<title>Шаблон решения: игра 5m</title>",
        "<style>", _PROMPT_ENTRY_REPORT_CSS, "</style></head><body>",
        "<h1>Шаблон принятия решения: игра «5m»</h1>",
        '<p class="intro">Контекст по параметрам и тикерам игры 5m. <strong>Отчёт для человека:</strong> ниже по каждому тикеру — контекст (входные данные), строка «Слияние (диагностика)» (tech+KB, всегда при наличии данных), обоснование по правилам и блок «С учётом корреляции (LLM)» при USE_LLM=true и успешном контексте корреляции (LLM получает тот же контекст). Решение по правилам и контексту (KB). Вход/выход и тейк/стоп из GAME_5M_*.</p>',
        f'<p class="cluster"><strong>Кластер:</strong> {html.escape(", ".join(cluster_tickers))}</p>',
    ]
    if correlation_note:
        parts.append(f'<p class="cluster">{_pre(correlation_note)}</p>')

    tech_by_ticker = {r.get("ticker"): {"price": r.get("price"), "rsi": r.get("rsi_5m")} for r in per_ticker_results if r.get("ticker")}
    if extra_tech_by_ticker:
        tech_by_ticker.update(extra_tech_by_ticker)

    for r in per_ticker_results:
        ticker = r.get("ticker") or "—"
        decision = r.get("decision")
        reasoning = r.get("reasoning") or ""
        price = r.get("price")
        rsi = r.get("rsi_5m")
        mom = r.get("momentum_2h_pct")
        vol = r.get("volatility_5m_pct")
        sl = r.get("stop_loss_pct")
        tp = r.get("take_profit_pct")
        stop_enabled = r.get("stop_loss_enabled", True)
        period_str = r.get("period_str")
        kb_news_impact = r.get("kb_news_impact")
        kb_news_summary = r.get("kb_news_summary")
        llm_news_content = r.get("llm_news_content")
        llm_sentiment = r.get("llm_sentiment")
        entry_advice = r.get("entry_advice")
        entry_advice_reason = r.get("entry_advice_reason")
        meta_parts = []
        if price is not None:
            meta_parts.append(f"Цена: {price:.2f}")
        if rsi is not None:
            meta_parts.append(f"RSI(5m): {rsi:.1f}")
        if mom is not None:
            meta_parts.append(f"Импульс 2ч: {mom:+.2f}%")
        if vol is not None:
            meta_parts.append(f"Волатильность: {vol:.2f}%")
        if stop_enabled and sl is not None and tp is not None:
            meta_parts.append(f"Стоп: −{sl:.1f}%  ·  Тейк: +{tp:.1f}%")
        elif tp is not None:
            meta_parts.append("Стоп: выкл.  ·  Тейк: +{:.1f}%".format(tp))
        parts.append(f'<div class="ticker-section"><h2>{html.escape(str(ticker))} — {html.escape(str(decision) if decision else "—")}</h2>')
        if meta_parts:
            parts.append(f'<p class="meta">{"  ·  ".join(meta_parts)}</p>')
        # Полный контекст: тикеры матрицы (FAST + MEDIUM + LONG при успешном расчёте)
        full_list = correlation_tickers if correlation_tickers else cluster_tickers
        if correlation_matrix and ticker in cluster_tickers:
            others = [x for x in full_list if x != ticker]
            corr_pairs = _corr_row(ticker, others, correlation_matrix)
            if corr_pairs:
                parts.append(f'<p class="corr">Корреляция с другими (30 дн.): {", ".join(corr_pairs)}</p>')
        # Только тикеры с известной корреляцией (из полного списка матрицы), по убыванию коэффициента
        other_rows: List[Tuple[str, Optional[float], Optional[float], float]] = []
        for t in full_list:
            if t == ticker:
                continue
            tech = tech_by_ticker.get(t) or {}
            pr = tech.get("price")
            rsi_o = tech.get("rsi")
            c_val: Optional[float] = None
            if correlation_matrix:
                raw = correlation_matrix.get(ticker, {}).get(t) or correlation_matrix.get(t, {}).get(ticker)
                if raw is not None:
                    try:
                        f_c = float(raw)
                        if math.isfinite(f_c):
                            c_val = f_c
                    except (TypeError, ValueError):
                        pass
            if c_val is not None and math.isfinite(c_val):
                other_rows.append((t, pr, rsi_o, c_val))
        other_rows.sort(key=lambda r: r[3], reverse=True)
        cluster_context_lines = []
        for t, pr, rsi_o, c_val in other_rows:
            seg = [t]
            if pr is not None:
                seg.append(f"${pr:.2f}")
            if rsi_o is not None:
                seg.append(f"RSI {rsi_o:.1f}")
            seg.append(f"corr {c_val:+.2f}")
            cluster_context_lines.append(" ".join(seg))
        if cluster_context_lines:
            parts.append(f'<p class="corr">По тикерам с известной корреляцией (по убыванию корр.): {", ".join(cluster_context_lines)}</p>')
        context_parts = []
        if period_str:
            context_parts.append(f"Период данных: {_pre(period_str)}")
        if kb_news_impact:
            context_parts.append(f"Влияние новостей (KB): {_pre(kb_news_impact)}")
        if kb_news_summary:
            context_parts.append(f"Новости из KB: {_pre(kb_news_summary)}")
        ef = r.get("entry_fusion_metrics")
        if isinstance(ef, dict) and ef.get("fused_bias_neg1") is not None:
            context_parts.append(
                f"Слияние (диагностика): tech_bias {ef.get('tech_bias_neg1'):+.3f}, "
                f"news_bias_kb {ef.get('news_bias_kb'):+.3f}, fused {ef.get('fused_bias_neg1'):+.3f}"
                + (f", KB gate {ef.get('gate_mode_kb')}" if ef.get("gate_mode_kb") else "")
            )
            context_parts.append(_pre(format_entry_fusion_news_influence_explanation_ru(ef)))
        if llm_news_content:
            context_parts.append("LLM-новости (по обучению модели, не в реальном времени; даты могут быть старыми):")
            context_parts.append(_pre(llm_news_content[:500]) + ('…' if len(llm_news_content or '') > 500 else ''))
        if llm_sentiment is not None:
            context_parts.append(f"LLM sentiment: {llm_sentiment}")
        if entry_advice and entry_advice != "ALLOW":
            context_parts.append(f"Совет по входу: {entry_advice}" + (f" — {_pre(entry_advice_reason)}" if entry_advice_reason else ""))
        parts.append("<h3>Контекст (входные данные для решения)</h3>")
        if context_parts:
            parts.append('<div class="context-block">')
            parts.append("\n".join(context_parts))
            parts.append("</div>")
        else:
            parts.append('<div class="context-block">—</div>')
        parts.append("<h3>Обоснование</h3>")
        parts.append(f"<pre>{_pre(reasoning) if reasoning else '—'}</pre>")
        llm_corr = r.get("llm_correlation_reasoning")
        dfu = r.get("llm_decision_fused")
        kf = r.get("llm_key_factors")
        if llm_corr or dfu or kf:
            parts.append("<h3>С учётом корреляции (LLM)</h3>")
            if llm_corr:
                parts.append(f"<pre>{_pre(llm_corr)}</pre>")
            if kf:
                parts.append(f"<p class=\"meta\">Ключевые факторы: {', '.join(_pre(str(x)) for x in kf[:10])}</p>")
            if dfu:
                diff = r.get("llm_ab_fusion_differs")
                diff_s = "да" if diff else "нет"
                parts.append(
                    f"<p class=\"meta\"><strong>LLM decision_fused</strong> (явный учёт fused_bias): "
                    f"{_pre(str(dfu))} · отличается от legacy: {diff_s}</p>"
                )
                rf = r.get("llm_reasoning_fused")
                if rf:
                    parts.append(f"<pre class=\"meta\">{_pre(str(rf))}</pre>")
        parts.append("</div>")

    parts.append("</body></html>")
    return "\n".join(parts)


def _human_trade_explanation_from_exit_ctx(exit_ctx: Any) -> str:
    from report_generator import human_trade_explanation_from_exit_context

    return human_trade_explanation_from_exit_context(exit_ctx)


def _build_closed_html(closed: List[Any], total_pnl: float = 0, impulse_pct: bool = False) -> str:
    """Собирает простой HTML для отчёта закрытых позиций (для сохранения в кэш). impulse_pct: добавить колонку Impulse %."""
    rows_html = []
    ncol = 14 if impulse_pct else 13
    for t in closed:
        direction = "Long" if getattr(t, "side", "") == "SELL" else "Short"
        pts = t.exit_price - t.entry_price
        pips = round(pts * 10000) if ("=X" in t.ticker or "USD" in t.ticker or "EUR" in t.ticker) else round(pts, 2)
        impulse_cell = ""
        if impulse_pct:
            imp = getattr(t, "entry_impulse_pct", None)
            if imp is not None:
                impulse_cell = f"<td>{imp:+.2f}%</td>"
            elif t.entry_price and t.entry_price > 0:
                imp = (t.exit_price - t.entry_price) / t.entry_price * 100.0
                impulse_cell = f"<td>{imp:+.2f}%</td>"
            else:
                impulse_cell = "<td>—</td>"
        entry_s = html.escape(str(getattr(t, "entry_strategy", None) or "—"))
        exit_s = html.escape(str(getattr(t, "exit_strategy", None) or "—"))
        profit_cls = "positive" if t.net_pnl >= 0 else "negative"
        qty_val = getattr(t, "quantity", None)
        qty_str = f"{int(qty_val)}" if qty_val is not None and float(qty_val) == int(float(qty_val)) else f"{float(qty_val):.2f}" if qty_val is not None else "—"
        pnl_pct = (t.exit_price - t.entry_price) / t.entry_price * 100.0 if t.entry_price and t.entry_price > 0 else 0.0
        pnl_pct_str = f"{pnl_pct:+.2f}%"
        row_cells = (
            f"<tr><td>{html.escape(t.ticker)}</td><td>{direction}</td>"
            f"<td>{t.entry_price:.2f}</td><td>{t.exit_price:.2f}</td>"
        )
        if impulse_pct:
            row_cells += impulse_cell
        reason = html.escape(str(getattr(t, "signal_type", None) or "—"))
        row_cells += (
            f"<td>{pips}</td><td>{qty_str}</td>"
            f'<td class="{profit_cls}">{t.net_pnl:+.2f}</td>'
            f'<td class="{profit_cls}">{pnl_pct_str}</td>'
            f"<td>{entry_s}</td><td>{exit_s}</td><td>{reason}</td>"
            f"<td>{_ts_msk(t.entry_ts)}</td><td>{_ts_msk(t.ts)}</td></tr>"
        )
        rows_html.append(row_cells)
        expl = _human_trade_explanation_from_exit_ctx(getattr(t, "exit_context_json", None))
        if expl:
            safe = html.escape(expl[:2000] + ("…" if len(expl) > 2000 else ""))
            rows_html.append(
                f'<tr class="human_note"><td colspan="{ncol}"><strong>Пояснение (вход→выход):</strong> {safe}</td></tr>'
            )
    body = "\n".join(rows_html)
    summary = f'<p class="summary"><strong>Итого:</strong> {len(closed)} позиций, суммарный P/L: ${total_pnl:+,.2f}</p>'
    th_impulse = "<th>Импульс % (при входе)</th>" if impulse_pct else ""
    thead = f"<thead><tr><th>Instrument</th><th>Dir</th><th>Open</th><th>Close</th>{th_impulse}<th>Pips</th><th>Qty</th><th>Profit</th><th>P/L %</th><th>Entry</th><th>Exit</th><th>Причина выхода</th><th>Open (MSK)</th><th>Close (MSK)</th></tr></thead>"
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Закрытые позиции</title>
<style>table{{border-collapse:collapse;width:100%}} th,td{{padding:6px;text-align:left;border:1px solid #ddd}} th{{background:#f5f5f5}} .positive{{color:green}} .negative{{color:red}} .summary{{margin-top:1em}} .human_note td{{font-size:0.9em;color:#333;background:#f9f9f9;vertical-align:top}}</style>
</head><body><h1>Закрытые позиции</h1><p>Даты в MSK. Entry/Exit — стратегия. Причина выхода: TAKE_PROFIT (достигнут тейк), TIME_EXIT (конец сессии или макс. дней), SELL (сигнал), STOP_LOSS. Цель тейка при входе задаётся от импульса 2ч (напр. 6%%), но выход может быть по другой причине — тогда P/L %% меньше цели. Ниже по сделкам — пояснение вход→выход, если оно сохранено в БД при закрытии.</p>
<table>{thead}<tbody>{body}</tbody></table>{summary}</body></html>"""


def _build_replay_closed_html(payload: Dict[str, Any]) -> str:
    """HTML отчёт по результатам replay_non_take_closures_daily.py."""
    stats = payload.get("stats") or {}
    closed = payload.get("closed") or []
    opened = payload.get("open") or []
    comps = payload.get("comparisons") or []
    blocked = payload.get("blocked_entries") or []

    comp_rows: List[str] = []
    for r in comps:
        try:
            ticker = html.escape(str(r.get("ticker") or "—"))
            buy_id = html.escape(str(r.get("buy_id") or "—"))
            entry = float(r.get("buy_price") or 0.0)
            qty = r.get("qty")
            qty_s = html.escape(
                str(int(qty)) if qty is not None and float(qty) == int(float(qty))
                else f"{float(qty):.2f}" if qty is not None
                else "—"
            )
            orig_reason = html.escape(str(r.get("orig_exit_reason") or "—"))
            orig_pnl = r.get("orig_pnl_pct")
            orig_pnl_s = "—" if orig_pnl is None else f"{float(orig_pnl):+.2f}%"
            alt_status = html.escape(str(r.get("alt_status") or "—"))
            alt_reason = html.escape(str(r.get("alt_exit_reason") or "—"))
            alt_pnl = r.get("alt_pnl_pct")
            alt_pnl_s = "—" if alt_pnl is None else f"{float(alt_pnl):+.2f}%"
            cls_o = "positive" if (orig_pnl is not None and float(orig_pnl) >= 0) else "negative" if orig_pnl is not None else ""
            cls_a = "positive" if (alt_pnl is not None and float(alt_pnl) >= 0) else "negative" if alt_pnl is not None else ""
            ignored_ids = r.get("ignored_non_take_sell_ids") or []
            ignored_s = html.escape(",".join(str(x) for x in ignored_ids[:10]) + ("…" if len(ignored_ids) > 10 else "")) if ignored_ids else "—"
            comp_rows.append(
                "<tr>"
                f"<td>{ticker}</td><td>{buy_id}</td><td>{entry:.2f}</td><td>{qty_s}</td>"
                f"<td>{orig_reason}</td><td class=\"{cls_o}\">{orig_pnl_s}</td>"
                f"<td>{alt_status}</td><td>{alt_reason}</td><td class=\"{cls_a}\">{alt_pnl_s}</td>"
                f"<td>{ignored_s}</td>"
                f"<td>{html.escape(str(r.get('buy_ts') or '—'))[:16]}</td>"
                f"<td>{html.escape(str(r.get('orig_exit_ts') or '—'))[:16]}</td>"
                f"<td>{html.escape(str(r.get('alt_exit_ts') or '—'))[:16]}</td>"
                "</tr>"
            )
        except Exception:
            continue

    closed_rows: List[str] = []
    for r in closed:
        try:
            entry = float(r.get("entry_price") or 0.0)
            exitp = float(r.get("exit_price") or 0.0)
            pnl_pct = ((exitp - entry) / entry * 100.0) if entry > 0 else 0.0
            cls = "positive" if pnl_pct >= 0 else "negative"
            closed_rows.append(
                "<tr>"
                f"<td>{html.escape(str(r.get('ticker') or '—'))}</td>"
                f"<td>{entry:.2f}</td>"
                f"<td>{exitp:.2f}</td>"
                f"<td class=\"{cls}\">{pnl_pct:+.2f}%</td>"
                f"<td>{html.escape(str(r.get('exit_reason') or '—'))}</td>"
                f"<td>{html.escape(str(r.get('entry_ts') or '—'))[:16]}</td>"
                f"<td>{html.escape(str(r.get('exit_ts') or '—'))[:16]}</td>"
                "</tr>"
            )
        except Exception:
            continue
    open_rows: List[str] = []
    for r in opened:
        ignored = r.get("ignored_sells") or []
        open_rows.append(
            "<tr>"
            f"<td>{html.escape(str(r.get('ticker') or '—'))}</td>"
            f"<td>{float(r.get('entry_price') or 0.0):.2f}</td>"
            f"<td>{float(r.get('take_level') or 0.0):.2f}</td>"
            f"<td>{float(r.get('take_pct') or 0.0):.2f}%</td>"
            f"<td>{html.escape(str(r.get('entry_ts') or '—'))[:16]}</td>"
            f"<td>{len(ignored)}</td>"
            "</tr>"
        )
    closed_body = "\n".join(closed_rows) if closed_rows else "<tr><td colspan='7'>Нет альтернативно закрытых позиций</td></tr>"
    open_body = "\n".join(open_rows) if open_rows else "<tr><td colspan='6'>Нет альтернативно открытых позиций</td></tr>"
    comp_body = "\n".join(comp_rows) if comp_rows else "<tr><td colspan='13'>Нет строк сравнения</td></tr>"

    blocked_body = ""
    if blocked:
        trs = []
        for b in blocked[:200]:
            trs.append(
                "<tr>"
                f"<td>{html.escape(str(b.get('ticker') or '—'))}</td>"
                f"<td>{html.escape(str(b.get('buy_id') or '—'))}</td>"
                f"<td>{html.escape(str(b.get('buy_ts') or '—'))[:16]}</td>"
                f"<td>{float(b.get('buy_price') or 0.0):.2f}</td>"
                f"<td>{html.escape(str(b.get('blocked_by_open_buy_id') or '—'))}</td>"
                f"<td>{html.escape(str(b.get('blocked_by_entry_ts') or '—'))[:16]}</td>"
                "</tr>"
            )
        blocked_body = (
            "<h2>Входы, которые стали невозможны (позиция ещё открыта)</h2>"
            "<table><thead><tr><th>Ticker</th><th>BUY id</th><th>BUY ts</th><th>BUY price</th><th>Blocked by BUY id</th><th>Open since</th></tr></thead>"
            f"<tbody>{''.join(trs)}</tbody></table>"
        )
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Replay закрытий (TAKE-only)</title>
<style>table{{border-collapse:collapse;width:100%;margin-bottom:14px}} th,td{{padding:6px;text-align:left;border:1px solid #ddd}} th{{background:#f5f5f5}} .positive{{color:green}} .negative{{color:red}} .summary{{margin:8px 0}}</style>
</head><body>
<h1>Replay закрытий (только TAKE_PROFIT)</h1>
<p class="summary">BUY: {int(stats.get('buy_total', 0))}, SELL: {int(stats.get('sell_total', 0))} (take={int(stats.get('sell_take_total', 0))}, non-take={int(stats.get('sell_non_take_total', 0))}), ignored non-take={int(stats.get('ignored_non_take_sells', 0))}, ignored BUY while open={int(stats.get('ignored_buys_while_open', 0))}, skipped BUY w/o take={int(stats.get('skipped_buys_without_take_at_entry', 0))}.</p>
<h2>Сравнение: как было → как стало</h2>
<table><thead><tr>
<th>Ticker</th><th>BUY id</th><th>Entry</th><th>Qty</th>
<th>Orig reason</th><th>Orig P/L %</th>
<th>Alt status</th><th>Alt reason</th><th>Alt P/L %</th>
<th>Ignored non-take SELL ids</th>
<th>BUY ts</th><th>Orig exit ts</th><th>Alt exit ts</th>
</tr></thead><tbody>{comp_body}</tbody></table>
<h2>Альтернативно закрытые</h2>
<table><thead><tr><th>Ticker</th><th>Open</th><th>Close</th><th>P/L %</th><th>Reason</th><th>Entry ts</th><th>Exit ts</th></tr></thead><tbody>{closed_body}</tbody></table>
<h2>Остались открытыми</h2>
<table><thead><tr><th>Ticker</th><th>Entry</th><th>Take level</th><th>Take %</th><th>Entry ts</th><th>Ignored non-take sells</th></tr></thead><tbody>{open_body}</tbody></table>
{blocked_body}
</body></html>"""


def _build_pending_html(
    pending: List[Any],
    latest_prices: Dict[str, float],
    tickers_in_game_5m: Set[str],
    total_entry: float = 0,
    total_now: float = 0,
    pnl_total: float = 0,
    ret_pct: float = 0,
) -> str:
    """Собирает простой HTML для отчёта открытых позиций (для сохранения в кэш)."""
    rows_html = []
    for p in pending:
        strat = (p.strategy_name or "—").strip() or "—"
        if strat == "GAME_5M" and p.ticker not in tickers_in_game_5m:
            strat = "5m вне"
        now_price = latest_prices.get(p.ticker)
        if now_price is not None and p.entry_price and p.entry_price > 0:
            pct = (now_price - p.entry_price) / p.entry_price * 100.0
            usd = (now_price - p.entry_price) * p.quantity
            pl_str = f"{pct:+.1f}% {usd:+.0f}$"
            pl_cls = "positive" if pct >= 0 else "negative"
        else:
            pl_str = "—"
            pl_cls = ""
        now_str = f"{now_price:.2f}" if now_price is not None else "—"
        tp_str = f"{p.take_profit}%" if getattr(p, "take_profit", None) is not None else "—"
        sl_str = f"{p.stop_loss}%" if getattr(p, "stop_loss", None) is not None else "—"
        n_buys = int(getattr(p, "buy_leg_count", 1) or 1)
        rows_html.append(
            f"<tr><td>{html.escape(p.ticker)}</td><td>Long</td>"
            f"<td>{p.entry_price:.2f}</td><td>{now_str}</td><td>{int(p.quantity)}</td>"
            f'<td class="{pl_cls}">{html.escape(pl_str)}</td><td>{html.escape(strat)}</td>'
            f"<td>{tp_str}</td><td>{sl_str}</td>"
            f"<td>{n_buys}</td>"
            f"<td>{_ts_msk(p.entry_ts)}</td></tr>"
        )
    body = "\n".join(rows_html)
    summary = ""
    if total_entry and total_entry > 0:
        summary = f'<p class="summary"><strong>Итого по позициям:</strong> вход ${total_entry:,.0f} → сейчас ${total_now:,.0f} | P/L: ${pnl_total:+,.0f} ({ret_pct:+.2f}%)</p>'
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Открытые позиции</title>
<style>table{{border-collapse:collapse;width:100%}} th,td{{padding:6px;text-align:left;border:1px solid #ddd}} th{{background:#f5f5f5}} .positive{{color:green}} .negative{{color:red}} .summary{{margin-top:1em}}</style>
</head><body><h1>Открытые позиции</h1><p>Даты в MSK. «5m вне» — тикер убран из игры 5m.</p>
<table><thead><tr><th>Instrument</th><th>Dir</th><th>Open</th><th>Now</th><th>Units</th><th>P/L</th><th>Strategy</th><th>TP</th><th>SL</th><th>BUYs</th><th>Open (MSK)</th></tr></thead>
<tbody>{body}</tbody></table>{summary}</body></html>"""


def _build_corr_html(corr, n_days: int, title: str = "Корреляции лог-доходностей") -> str:
    """HTML-таблица матрицы корреляций (pandas DataFrame)."""
    cols = list(corr.columns)
    thead = "<tr><th></th>" + "".join(f"<th>{html.escape(str(c))}</th>" for c in cols) + "</tr>"
    tbody_rows = []
    for r in cols:
        tds = [f"<td><strong>{html.escape(str(r))}</strong></td>"]
        for c in cols:
            v = corr.loc[r, c]
            if v is None or (isinstance(v, float) and (v != v or v == float("nan"))):
                tds.append("<td>—</td>")
            else:
                cls = "positive" if v >= 0.3 else "negative" if v <= -0.3 else ""
                tds.append(f'<td class="{cls}">{float(v):.3f}</td>')
        tbody_rows.append("<tr>" + "".join(tds) + "</tr>")
    tbody = "\n".join(tbody_rows)
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>table{{border-collapse:collapse;width:100%}} th,td{{padding:6px;text-align:right;border:1px solid #ddd}} th{{background:#f5f5f5}} td:first-child{{text-align:left}} .positive{{color:green}} .negative{{color:red}}</style>
</head><body><h1>{html.escape(title)}</h1><p>Лог-доходности close, {n_days} дн. Откройте в браузере.</p>
<table><thead>{thead}</thead><tbody>{tbody}</tbody></table></body></html>"""


def _build_premarket_html(rows: List[Dict[str, Any]]) -> str:
    """HTML-отчёт премаркета: тикер, prev_close, premarket_last, gap%, min_to_open, last_time_et."""
    trs = []
    for r in rows:
        ticker = html.escape(str(r.get("ticker", "—")))
        prev = r.get("prev_close")
        prev_s = f"{prev:.2f}" if prev is not None else "—"
        last = r.get("premarket_last")
        last_s = f"{last:.2f}" if last is not None else "—"
        gap = r.get("premarket_gap_pct")
        gap_s = f"{gap:+.2f}%" if gap is not None else "—"
        gap_cls = "positive" if (gap is not None and gap >= 0) else "negative" if gap is not None else ""
        mins = r.get("minutes_until_open")
        mins_s = f"{mins} мин" if mins is not None else "—"
        time_et = (r.get("premarket_last_time_et") or "—")[:16] if r.get("premarket_last_time_et") else "—"
        trs.append(
            f"<tr><td>{ticker}</td><td>{prev_s}</td><td>{last_s}</td>"
            f'<td class="{gap_cls}">{gap_s}</td><td>{mins_s}</td><td>{html.escape(str(time_et))}</td></tr>'
        )
    body = "\n".join(trs)
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Премаркет</title>
<style>table{{border-collapse:collapse;width:100%}} th,td{{padding:6px;text-align:left;border:1px solid #ddd}} th{{background:#f5f5f5}} .positive{{color:green}} .negative{{color:red}}</style>
</head><body><h1>Премаркет (до открытия US 9:30 ET)</h1><p>Цена — последняя минута Yahoo (prepost). Даты/время — ET.</p>
<table><thead><tr><th>Ticker</th><th>Prev Close</th><th>Premarket</th><th>Gap %</th><th>Min to open</th><th>Last time (ET)</th></tr></thead>
<tbody>{body}</tbody></table></body></html>"""


class LSETelegramBot:
    """
    Telegram Bot для LSE Trading System
    
    Фокус на независимых инструментах:
    - Золото (GC=F), нефть (CL=F)
    - Валютные пары (GBPUSD=X, EURUSD=X и т.д.)
    - Отдельные акции (MSFT, SNDK и т.д.)
    """
    
    def __init__(self, token: str, allowed_users: Optional[list] = None):
        """
        Инициализация бота
        
        Args:
            token: Telegram Bot Token
            allowed_users: Список разрешенных user_id (если None - доступ для всех)
        """
        self.token = token
        self.allowed_users = allowed_users
        
        # Инициализация компонентов. USE_LLM в config.env (или в БД strategy_parameters GLOBAL) — если false, LLM не применяем.
        use_llm = get_use_llm_for_analyst()
        self.analyst = AnalystAgent(use_llm=use_llm, use_strategy_factory=True)
        self.vector_kb = VectorKB()
        
        # Инициализация LLM только для обработки вопросов в /ask
        try:
            from services.llm_service import get_llm_service
            self.llm_service = get_llm_service()
            logger.info("✅ LLM сервис инициализирован для обработки вопросов (/ask)")
        except Exception as e:
            logger.warning(f"⚠️ LLM сервис недоступен для вопросов: {e}")
            self.llm_service = None
        
        # Создаем приложение (увеличенные таймауты — при медленной сети отправка графика/фото иначе даёт TimedOut)
        builder = (
            Application.builder()
            .token(token)
            .read_timeout(30.0)
            .write_timeout(30.0)
            .connect_timeout(15.0)
        )
        try:
            builder.media_write_timeout(300.0)  # отправка фото (chart5m и т.д.) — 5 мин при медленной сети
        except AttributeError:
            pass  # старые версии PTB без media_write_timeout
        self.application = builder.build()
        # Логируем каждый входящий апдейт (чтобы понять, доходят ли команды до бота)
        _orig_process = self.application.process_update
        async def _logged_process_update(update: Update) -> None:
            msg = getattr(update, "message", None) or getattr(update, "edited_message", None)
            text = getattr(msg, "text", None) if msg else None
            logger.info("Входящий апдейт: update_id=%s, chat_id=%s, text=%r", update.update_id, getattr(msg, "chat_id", None) if msg else None, text)
            await _orig_process(update)
        self.application.process_update = _logged_process_update

        # Получаем информацию о боте для логирования
        async def get_bot_info():
            bot_info = await self.application.bot.get_me()
            logger.info(f"Bot info: username={bot_info.username}, id={bot_info.id}, first_name={bot_info.first_name}")
            return bot_info
        
        # Регистрируем handlers
        self._register_handlers()
        
        logger.info("✅ LSE Telegram Bot инициализирован")
        
        # Логируем информацию о боте после инициализации
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Если loop уже запущен, создаём задачу
                loop.create_task(get_bot_info())
            else:
                # Если loop не запущен, запускаем
                loop.run_until_complete(get_bot_info())
        except Exception as e:
            logger.warning(f"Не удалось получить информацию о боте: {e}")
    
    def _register_handlers(self):
        """Регистрация обработчиков команд и сообщений"""
        # Команды
        self.application.add_handler(CommandHandler("start", self._handle_start))
        self.application.add_handler(CommandHandler("help", self._handle_help))
        self.application.add_handler(CommandHandler("signal", self._handle_signal))
        self.application.add_handler(CommandHandler("news", self._handle_news))
        self.application.add_handler(CommandHandler("newssources", self._handle_newssources))
        self.application.add_handler(CommandHandler("price", self._handle_price))
        self.application.add_handler(CommandHandler("chart", self._handle_chart))
        self.application.add_handler(CommandHandler("chart5m", self._handle_chart5m))
        self.application.add_handler(CommandHandler("table5m", self._handle_table5m))
        self.application.add_handler(CommandHandler("tickers", self._handle_tickers))
        self.application.add_handler(CommandHandler("ask", self._handle_ask))
        self.application.add_handler(CommandHandler("portfolio", self._handle_portfolio))
        self.application.add_handler(CommandHandler("buy", self._handle_buy))
        self.application.add_handler(CommandHandler("sell", self._handle_sell))
        self.application.add_handler(CommandHandler("history", self._handle_history))
        self.application.add_handler(CommandHandler("closed", self._handle_closed))
        self.application.add_handler(CommandHandler("closed_impulse", self._handle_closed_impulse))
        self.application.add_handler(CommandHandler("replay_closed", self._handle_replay_closed))
        self.application.add_handler(CommandHandler("pending", self._handle_pending))
        self.application.add_handler(CommandHandler("set_strategy", self._handle_set_strategy))
        self.application.add_handler(CommandHandler("prompt_entry", self._handle_prompt_entry))
        self.application.add_handler(CommandHandler("pe_5m", self._handle_pe_5m))
        self.application.add_handler(CommandHandler("strategies", self._handle_strategies))
        self.application.add_handler(CommandHandler("recommend", self._handle_recommend))
        self.application.add_handler(CommandHandler("recommend5m", self._handle_recommend5m))
        self.application.add_handler(CommandHandler("signal5m", self._handle_signal5m))
        self.application.add_handler(CommandHandler("game5m", self._handle_game5m))
        self.application.add_handler(CommandHandler("gameparams", self._handle_gameparams))
        self.application.add_handler(CommandHandler("dashboard", self._handle_dashboard))
        self.application.add_handler(CommandHandler("analyser", self._handle_analyser))
        self.application.add_handler(CommandHandler("premarket", self._handle_premarket))
        self.application.add_handler(CommandHandler("corr", self._handle_corr))
        self.application.add_handler(CommandHandler("corr5m", self._handle_corr5m))
        
        # Обработка текстовых сообщений (для произвольных запросов)
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        
        # Обработка callback queries (для inline кнопок)
        self.application.add_handler(CallbackQueryHandler(self._handle_callback))
        # Логирование любых ошибок в обработчиках (чтобы не молчать при падении команды)
        self.application.add_error_handler(self._handle_error)

    async def _handle_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Логируем ошибку при обработке апдейта (иначе команды падают без вывода)."""
        logger.exception("Ошибка при обработке команды/сообщения: %s", context.error)
        if update and isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "❌ Произошла ошибка. Проверьте логи бота."
                )
            except Exception:
                pass

    def _check_access(self, user_id: int) -> bool:
        """Проверка доступа пользователя"""
        if self.allowed_users is None:
            return True
        return user_id in self.allowed_users

    async def _reply_to_update(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
        parse_mode: str | None = "Markdown",
    ) -> None:
        """Отправляет ответ в чат: через message.reply_text или bot.send_message, если message отсутствует."""
        if update.message is not None:
            await update.message.reply_text(text, parse_mode=parse_mode)
            return
        if update.effective_chat is not None and context.bot is not None:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                parse_mode=parse_mode,
            )
            return
        logger.warning("Не удалось отправить ответ: нет update.message и effective_chat")

    async def _get_recent_news_async(self, ticker: str, timeout: int = 30):
        """
        Получает новости для тикера в executor с таймаутом.
        Не блокирует event loop. При таймауте выбрасывает asyncio.TimeoutError.
        """
        loop = asyncio.get_event_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, self.analyst.get_recent_news, ticker),
            timeout=timeout,
        )

    async def _send_kb_news_report(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        ticker: str,
        news_df,
        top_n: int,
    ) -> None:
        """
        HTML в чат + полный HTML-файл (как nyse /news): draft_bias, news.bias, gate SKIP/LITE/FULL.
        """
        from services.kb_news_report import (
            kb_news_lookback_hours,
            compute_kb_news_bias_metrics,
            build_kb_news_short_html,
            build_kb_news_full_html,
        )

        h = kb_news_lookback_hours()
        metrics = compute_kb_news_bias_metrics(
            news_df,
            ticker,
            self.analyst,
            lookback_hours=h,
            engine=getattr(self.analyst, "engine", None),
        )
        short_html = build_kb_news_short_html(ticker, news_df, metrics, top_n=top_n)
        full_html = build_kb_news_full_html(ticker, news_df, metrics, top_n=top_n)

        if len(short_html) > TELEGRAM_MAX_MESSAGE_LENGTH - 80:
            parts = self._split_long_message(short_html, max_length=TELEGRAM_MAX_MESSAGE_LENGTH - 80)
            for part in parts:
                await self._reply_to_update(update, context, part, parse_mode="HTML")
        else:
            await self._reply_to_update(update, context, short_html, parse_mode="HTML")

        try:
            if update.message is not None:
                fn = f"news_{ticker}_{datetime.utcnow().strftime('%Y-%m-%d_%H-%M')}.html"
                await update.message.reply_document(
                    document=BytesIO(full_html.encode("utf-8")),
                    filename=fn,
                    caption="📎 Полный отчёт (формулы + таблица) — откройте в браузере",
                )
        except Exception as e:
            logger.warning("Не удалось отправить HTML-документ /news: %s", e)
    
    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        chat_type = update.effective_chat.type if update.effective_chat else "?"
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        logger.info(f"/start: chat_type={chat_type} chat_id={chat_id} user_id={user_id} allowed_users={'*' if self.allowed_users else 'all'}")
        if user_id is None or update.message is None:
            logger.warning("/start: нет user или message в update")
            return
        if not self._check_access(user_id):
            await update.message.reply_text(
                f"❌ Доступ запрещен. Ваш user_id: {user_id}. Добавьте его в TELEGRAM_ALLOWED_USERS в config.env для доступа в личке."
            )
            return
        
        welcome_text = """
🤖 **LSE Trading Bot**

Анализ и виртуальная торговля (песочница):
• Золото (GC=F), нефть (CL=F), валюты (GBPUSD=X), акции (MSFT, SNDK)

**Команды:**
/news <ticker> [N] — KB (HTML + файл): draft_bias, news.bias, gate; /newssources — каналы за 14 дн.
/price <ticker> — цена
/chart <ticker> [days] — график дневной; /chart game_5m [days] — все тикеры игры 5m (горизонтально по сессиям, тикеры друг под другом)
/chart5m <ticker> [days] — график 5 мин (по требованию)
/table5m <ticker> [days] — таблица 5m свечей
/signal <ticker> — технический сигнал по тикеру: для игры 5m — 5m; иначе портфель (решение, RSI, sentiment)
/recommend [ticker] — рекомендация по портфелю; без тикера — по кластеру
/recommend5m [ticker] [days] — компактный прогноз 5m (технический + LLM при включении); без тикера — кластер
/signal5m [ticker] [days] — только технический сигнал 5m (тот же источник, что крон)
/game5m [ticker|platform] — мониторинг 5m по тикеру; mode platform/sync/all: отправить массив GAME_5M в Kerim /game и вернуть 3 HTML (notOpened/opened/closed)
/gameparams — все существенные параметры игр (5m и портфель): тикеры, тейк/стоп, cooldown
/dashboard [5m|daily|all] — дашборд по тикерам: решения, 5m, новости (проактивный мониторинг)
/analyser [days] [GAME_5M|ALL|Portfolio] [llm] — анализ эффективности закрытых сделок (единый код с web /analyzer)
/ask <вопрос> — вопрос (работает в группах!)
/tickers — список инструментов

**Песочница (вход/выход, P&L):**
/portfolio — портфель и P&L
/buy <ticker> <кол-во> — купить
/sell <ticker> [кол-во] — продать (без кол-ва — вся позиция)
/history [тикер] [N] — последние сделки (с тикером — фильтр по тикеру)
/closed [тикер] [N] — закрытые позиции; без аргументов — последние N (по умолч. из config, см. TELEGRAM_CLOSED_REPORT_DEFAULT); N не больше TELEGRAM_CLOSED_REPORT_MAX (по умолч. 200). Примеры: /closed 100, /closed MU 80
/closed_impulse [N] [pct|all] — закрытые 5m без стоп-лоссов; N — лимит строк (как /closed, TELEGRAM_CLOSED_REPORT_*); pct=порог импульса при входе %% (по умолч. 5), all=все сделки
/pending [тикер] [N] — открытые позиции; с тикером — только по нему (напр. /pending SNDK)
/premarket [тикер] — премаркет: таблица + HTML; с тикером — ещё график 1m (как /chart5m)
/corr [ticker1] [ticker2] — корреляции по кластеру портфеля (60 дн.). Без аргументов — матрица; один/два тикера — строка или пара.
/corr5m [ticker1] [ticker2] — то же по кластеру игры 5m.
/set_strategy <ticker> <стратегия> — переназначить стратегию у открытой позиции (напр. «5m вне» → Manual)
/strategies — описание стратегий (GAME_5M, Portfolio, Manual, Momentum и др.)
/prompt_entry [portfolio|game5m|5m|тикер] — отчёт: как получено решение (контекст + ответ); 5m = game5m; для портфеля — ещё промпт для LLM. Коротко: /pe_5m = /prompt_entry 5m.

/help — полная справка
        """
        # Без parse_mode: в тексте много подчёркиваний (game_5m, closed_impulse, config.env),
        # Telegram Markdown воспринимает _ как курсив и падает с "can't find end of entity"
        await update.message.reply_text(welcome_text.strip(), parse_mode=None)
    
    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /help"""
        logger.info("Команда /help вызвана, user_id=%s", update.effective_user.id if update.effective_user else None)
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is not None:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass
        user_id = update.effective_user.id
        
        if not self._check_access(user_id):
            logger.warning("Доступ запрещён для user_id=%s (нет в TELEGRAM_ALLOWED_USERS)", user_id)
            await update.message.reply_text("❌ Доступ запрещен")
            return

        help_text = """
📖 **Справка по командам**

**Прогноз (решение BUY/HOLD/STRONG\_BUY):** один путь для всех команд; при USE\_LLM=true (config\.env или глобальные параметры БД) — с LLM; при USE\_LLM=false — только стратегия и тех\. сигнал без модели\. Дашборд по крону — без LLM\.

**Рекомендации (итог по игре) и отчёт (как получено решение):**
Одна цепочка решений; разница — форма вывода. При USE_LLM=false итог = только тех. рекомендация; при USE_LLM=true LLM учитывает тех. сигнал и может скорректировать.
`/signal <ticker>` — технический сигнал: если тикер в игре 5m — 5m (как signal5m); иначе портфель (цена, RSI, решение, sentiment).
`/recommend [ticker]` — рекомендация по игре портфель: когда входить, стоп/тейк; без тикера — по кластеру. Итоговая рекомендация.
`/recommend5m [ticker] [days]` — компактный прогноз 5m (таблица: технический + LLM). `/signal5m [ticker]` — только технический 5m.
`/prompt_entry [portfolio|game5m|5m|тикер]` — не рекомендация, а отчёт: контекст и ответ по каждому тикеру. Коротко: `5m` = game5m, `/pe_5m` = то же. Для портфеля в выгрузке также промпт для LLM. Без аргумента — пустой шаблон.

**Анализ (справка по signal):**
`/signal` — справка и список доступных тикеров
  Пример: `/signal MSFT` или `/signal GC=F`

**Новости:**
`/news <ticker> [N]` - Новости из PostgreSQL knowledge_base (окно KB_NEWS_LOOKBACK_HOURS, по умолч. ~14 дней).
  В чате: HTML как nyse — draft_bias (грубое среднее), news.bias (взвеш. как AnalystAgent), режим Gate FULL/LITE/SKIP (пороги как nyse GAME_5M).
  Файл: полный отчёт с формулами и таблицей. Пример: `/news MSFT` или `/news MSFT 15`
  Показывает: последние новости с источником и sentiment
`/newssources` — все каналы новостей и кол-во записей за последние 14 дней

**Цена:**
`/price <ticker>` - Текущая цена инструмента
  Пример: `/price MSFT`

**График:**
`/chart <ticker> [days]` - График цены за период (по умолч. 1 день, макс. 30). Пример: `/chart GC=F 7`
`/chart game_5m [days]` - Все тикеры игры 5m: для каждого тикер — горизонтальный график как /chart5m, тикеры друг под другом (макс. 7 сессий).
`/chart5m <ticker> [days]` - Внутридневной график 5 мин (по требованию, макс. 7 дней)
`/table5m <ticker> [days]` - Таблица последних 5-минутных свечей (макс. 7 дней)

**Список инструментов:**
`/tickers` - Показать все отслеживаемые инструменты

**Произвольные вопросы:**
`/ask <вопрос>` - Задать вопрос боту (работает в группах!)

**Примеры вопросов:**
• `/ask какая цена золота`
• `/ask какие новости по MSFT`
• `/ask анализ GBPUSD`
• `/ask сколько стоит золото`
• `/ask что с фунтом`

**Песочница (виртуальная торговля):**
`/portfolio` — кэш, позиции и P&L по последним ценам
`/buy <ticker> <кол-во>` — купить по последней цене из БД
`/sell <ticker>` — закрыть всю позицию; `/sell <ticker> <кол-во>` — частичная продажа
`/history [тикер] [N]` — последние сделки (по умолч. 15); с тикером — только по нему. В ответе — стратегия [GAME\_5M / Portfolio / Manual]
`/closed [тикер] [N]` — закрытые позиции; с тикером — только по нему (напр. `/closed MU 80`). Лимит: `TELEGRAM_CLOSED_REPORT_DEFAULT` / `TELEGRAM_CLOSED_REPORT_MAX` в config.env.
`/closed_impulse [N] [pct|all]` — закрытые 5m без стоп-лоссов. Лимит N — те же `TELEGRAM_CLOSED_REPORT_*`, что у `/closed`. По умолч. импульс при входе >5%%; pct=другой порог; all=все сделки. Внизу — открытые 5m.
`/pending [тикер] [N]` — открытые позиции; с тикером — фильтр (напр. `/pending SNDK`). «5m вне» — тикер убран из игры 5m.
`/premarket` — таблица премаркета + HTML. `/premarket <тикер>` — дополнительно график 1m по тикеру (как /chart5m).
`/corr` — матрица корреляций по кластеру портфеля (60 дн.). `/corr5m` — по кластеру 5m. С аргументами: строка по тикеру или пара T1 T2.
`/set\_strategy <ticker> <стратегия>` — переназначить стратегию у открытой позиции (Manual, Portfolio)
`/strategies` — описание стратегий (GAME\_5M, Portfolio, Manual, Momentum и др.)
`/game5m [ticker|platform]` — мониторинг игры 5m по тикеру (по умолч. SNDK). Режим `platform`/`sync`/`all`: отправить массив GAME_5M в Kerim `/game` и получить 3 HTML-отчёта (`notOpened`, `opened`, `closed`).
`/gameparams` — все параметры игр (5m и портфель): тикеры, тейк/стоп, cooldown _(config.env)_
`/dashboard [5m|daily|all]` — дашборд: все тикеры, сигналы, 5m (SNDK), новости за 7 дн. Для смены курса и решений.
`/analyser [days] [GAME_5M|ALL|Portfolio] [llm]` — анализ эффективности закрытых сделок и зоны улучшений (тот же код, что web /analyzer).
  В /ask можно спросить: когда можно открыть позицию по SNDK и какие параметры советуешь.
  Пример: `/recommend SNDK`, `/buy GC=F 5`, `/sell MSFT`

        **Стратегии** (колонка в /history, /pending, /closed):
  • **GAME\_5M** — игра 5m (крон, интрадей). «5m вне» — тикер убран из списка, крон не управляет.
  • **Portfolio** — портфельный цикл (trading\_cycle), дефолт при отсутствии имени стратегии. SELL по стоп-лоссу выполняется.
  • **Manual** — ручные команды `/buy`, `/sell`.
  • **Momentum, Mean Reversion, Neutral** и др. — стратегии из StrategyManager при портфельном цикле.
  Подробнее: `/strategies`
        """
        # Сначала пробуем отправить HTML-файлом; при ошибке — текст без разметки (как /start)
        raw_help = help_text.strip().replace("\\_", "_").replace("\\.", ".")
        try:
            html_content = _build_help_html(help_text.strip())
            filename = _unique_report_filename("Справка")
            await update.message.reply_document(
                document=BytesIO(html_content.encode("utf-8")),
                filename=filename,
                caption="📖 Справка по командам. Откройте файл в браузере.",
            )
        except Exception as doc_e:
            logger.warning("Не удалось отправить HTML-файл help: %s", doc_e)
            # Fallback: справка текстом без parse_mode (лимит Telegram 4096)
            chunk_size = 4000
            for i in range(0, len(raw_help), chunk_size):
                chunk = raw_help[i : i + chunk_size]
                await update.message.reply_text(chunk, parse_mode=None)
    
    def _get_available_tickers(self) -> list:
        """Список тикеров для справки /signal и др.: quotes + конфиг (TICKERS_FAST/MEDIUM/LONG), чтобы тикеры вроде CL=F были видны сразу после добавления в конфиг."""
        try:
            from sqlalchemy import create_engine, text
            from config_loader import get_database_url
            from services.ticker_groups import get_all_ticker_groups
            engine = create_engine(get_database_url())
            from_quotes = []
            with engine.connect() as conn:
                result = conn.execute(
                    text("SELECT DISTINCT ticker FROM quotes ORDER BY ticker")
                )
                from_quotes = [row[0] for row in result]
            from_config = get_all_ticker_groups()
            seen = set(from_quotes)
            for t in from_config:
                if t and t not in seen:
                    seen.add(t)
                    from_quotes.append(t)
            return sorted(from_quotes)
        except Exception as e:
            logger.warning(f"Не удалось загрузить тикеры: {e}")
            return []

    async def _handle_signal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /signal [ticker]. Без аргумента — справка и список тикеров."""
        user_id = update.effective_user.id
        
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        
        # Без аргумента — показываем справку и доступные тикеры
        if not context.args or len(context.args) == 0:
            tickers = self._get_available_tickers()
            help_msg = (
                "📌 **Как пользоваться /signal**\n\n"
                "Команда даёт анализ по инструменту: решение (BUY/HOLD/SELL), цену, RSI, "
                "технический сигнал, sentiment новостей и выбранную стратегию.\n\n"
                "**Формат:**\n"
                "`/signal` — эта справка и список тикеров\n"
                "`/signal <тикер>` — анализ по выбранному инструменту\n\n"
                "**Примеры:**\n"
                "`/signal MSFT`\n"
                "`/signal GC=F`\n"
                "`/signal GBPUSD=X`\n\n"
                "**Как выбирается стратегия:**\n"
                "По волатильности и sentiment: Momentum (тренд), Mean Reversion (откат), Volatile Gap (гэпы). "
                "Если ни одна не подошла — **Neutral** (режим не определён, рекомендация удержание).\n\n"
            )
            if tickers:
                commodities = [t for t in tickers if "=" in t or str(t).startswith("GC")]
                currencies = [t for t in tickers if "USD" in str(t) or "EUR" in str(t) or "GBP" in str(t)]
                stocks = [t for t in tickers if t not in commodities and t not in currencies]
                help_msg += "**Доступные тикеры:**\n"
                if stocks:
                    help_msg += "Акции: " + ", ".join(f"`{t}`" for t in stocks[:20]) + "\n"
                if currencies:
                    help_msg += "Валюты: " + ", ".join(f"`{t}`" for t in currencies[:15]) + "\n"
                if commodities:
                    help_msg += "Товары: " + ", ".join(f"`{t}`" for t in commodities[:10]) + "\n"
                if len(tickers) > 45:
                    help_msg += f"\n_Всего {len(tickers)} инструментов. Полный список: /tickers_"
            else:
                help_msg += "_Список тикеров пуст (нет данных в БД)._"
            await update.message.reply_text(help_msg, parse_mode="Markdown")
            return
        
        # Извлекаем тикер: если первый аргумент не похож на тикер (служебные слова), ищем дальше
        ticker = None
        if context.args:
            first_arg = context.args[0].upper()
            # Служебные слова, которые не тикеры
            skip_words = {'ДЛЯ', 'ПО', 'АНАЛИЗ', 'АНАЛИЗА', 'ПОКАЖИ', 'ДАЙ', 'THE', 'FOR', 'SHOW', 'GET'}
            if first_arg not in skip_words and len(first_arg) >= 2:
                ticker = first_arg
            else:
                # Пробуем найти тикер в остальных аргументах или извлекаем из всего текста
                if len(context.args) > 1:
                    ticker = context.args[1].upper()
                else:
                    # Извлекаем тикер из полного текста сообщения
                    full_text = update.message.text or ""
                    ticker = self._extract_ticker_from_text(full_text)
                    if not ticker:
                        ticker = first_arg  # Fallback на первый аргумент
        
        if not ticker:
            await update.message.reply_text(
                "❌ Не указан тикер\n"
                "Пример: `/signal GBPUSD=X` или `/signal GC=F`",
                parse_mode='Markdown'
            )
            return
        
        # Нормализуем тикер (GC-F -> GC=F и т.д.)
        ticker = _normalize_ticker(ticker)
        
        logger.info(f"📊 Запрос /signal для {ticker} от пользователя {update.effective_user.id} (исходные args: {context.args})")
        
        try:
            # Если тикер в игре 5m — технический сигнал 5m (тот же источник, что signal5m и cron)
            try:
                from services.ticker_groups import get_tickers_game_5m
                from services.recommend_5m import get_5m_technical_signal
                game5m_set = set(get_tickers_game_5m() or [])
                if ticker in game5m_set:
                    await update.message.reply_text(f"🔍 Сигнал 5m для {ticker}...")
                    tech = get_5m_technical_signal(ticker, days=5, use_llm_news=False)
                    if tech:
                        response = self._format_5m_technical_signal(ticker, tech)
                        await update.message.reply_text(response, parse_mode=None)
                        return
            except Exception as e:
                logger.debug("5m technical signal fallback: %s", e)
            
            # Показываем, что анализ начат
            await update.message.reply_text(f"🔍 Анализ {ticker}...")
            
            # Получаем решение от AnalystAgent (портфель и прочие игры)
            logger.info(f"Вызов analyst.get_decision_with_llm({ticker})")
            decision_result = self.analyst.get_decision_with_llm(ticker)
            logger.info(f"Получен результат для {ticker}: decision={decision_result.get('decision')}")
            
            # Форматируем ответ
            logger.info(f"Форматирование ответа для {ticker}")
            response = self._format_signal_response(ticker, decision_result)
            logger.info(f"Ответ сформирован для {ticker}, длина: {len(response)} символов")
            
            # Пытаемся отправить с Markdown, при ошибке парсинга — без форматирования
            try:
                logger.info(f"Отправка ответа для {ticker} с Markdown")
                await update.message.reply_text(response, parse_mode='Markdown')
                logger.info(f"✅ Ответ для {ticker} успешно отправлен")
            except Exception as parse_err:
                if 'parse' in str(parse_err).lower() or 'entit' in str(parse_err).lower():
                    logger.warning(f"Ошибка парсинга Markdown для {ticker}, отправляем без форматирования: {parse_err}")
                    await update.message.reply_text(response)
                    logger.info(f"✅ Ответ для {ticker} отправлен без форматирования")
                else:
                    logger.error(f"Ошибка отправки для {ticker}: {parse_err}", exc_info=True)
                    raise
            
        except Exception as e:
            logger.error(f"Ошибка анализа сигнала для {ticker}: {e}", exc_info=True)
            await update.message.reply_text(
                f"❌ Ошибка анализа {ticker}: {str(e)}"
            )
    
    async def _handle_news(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /news <ticker>"""
        user_id = update.effective_user.id if update.effective_user else None
        if user_id is None or not self._check_access(user_id):
            await self._reply_to_update(update, context, "❌ Доступ запрещен")
            return

        # Извлекаем ticker и опциональный лимит: /news MSFT  или  /news MSFT 15
        if not context.args or len(context.args) == 0:
            await self._reply_to_update(
                update, context,
                "❌ Укажите тикер\n"
                "Пример: `/news GC=F` или `/news MSFT 15` (число — сколько новостей показать, по умолчанию 10)",
                parse_mode='Markdown',
            )
            return

        ticker_raw = context.args[0].upper()
        ticker = _normalize_ticker(ticker_raw)
        limit = 10
        if len(context.args) >= 2:
            try:
                n = int(context.args[1])
                limit = max(1, min(50, n))
            except ValueError:
                pass

        try:
            await self._reply_to_update(update, context, f"📰 Поиск новостей для {ticker}...")

            news_timeout = 30
            try:
                news_df = await self._get_recent_news_async(ticker, timeout=news_timeout)
            except asyncio.TimeoutError:
                logger.error(f"Таймаут получения новостей для {ticker} ({news_timeout} с)")
                await self._reply_to_update(
                    update, context,
                    f"❌ Получение новостей для {ticker} заняло больше {news_timeout} с. "
                    "Попробуйте позже или проверьте доступность БД.",
                )
                return

            if news_df.empty:
                await self._reply_to_update(
                    update, context,
                    f"ℹ️ Новостей для {ticker} не найдено в knowledge_base за окно "
                    f"(см. KB_NEWS_LOOKBACK_HOURS в config.env; по умолчанию ~14 дней).",
                )
                return

            await self._send_kb_news_report(update, context, ticker, news_df, top_n=limit)

        except Exception as e:
            err_type = type(e).__name__
            err_msg = str(e)
            logger.error(
                f"Ошибка получения новостей для {ticker}: [{err_type}] {err_msg}",
                exc_info=True,
            )
            if "timed out" in err_msg.lower() or "timeout" in err_msg.lower():
                reply = (
                    f"❌ Запрос новостей для {ticker} завершился по таймауту. "
                    "Возможны перегрузка БД или медленный запрос к knowledge_base. Попробуйте позже."
                )
            else:
                reply = f"❌ Ошибка получения новостей для {ticker}: {err_msg}"
            await self._reply_to_update(update, context, reply)

    async def _handle_newssources(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /newssources — список каналов новостей и кол-во записей за последние 2 недели."""
        user_id = update.effective_user.id if update.effective_user else None
        if user_id is None or not self._check_access(user_id):
            await self._reply_to_update(update, context, "❌ Доступ запрещен")
            return
        try:
            from sqlalchemy import create_engine
            from config_loader import get_database_url
            from news_importer import get_news_sources_stats
            engine = create_engine(get_database_url())
            stats = get_news_sources_stats(engine, days=14)
            engine.dispose()
        except Exception as e:
            logger.exception("Ошибка получения статистики каналов новостей")
            await self._reply_to_update(update, context, f"❌ Ошибка: {e}")
            return
        if not stats:
            await self._reply_to_update(update, context, "📰 За последние 14 дней записей в базе новостей нет.")
            return
        total = sum(s["count"] for s in stats)
        lines = ["📰 **Каналы новостей** (за 14 дней)\n"]
        for s in stats:
            lines.append(f"• {_escape_markdown(s['source'])} — {s['count']}")
        lines.append(f"\nВсего записей: **{total}**")
        await self._reply_to_update(update, context, "\n".join(lines), parse_mode="Markdown")
    
    async def _handle_price_by_ticker(self, update: Update, ticker: str, ticker_raw: str = None):
        """Вспомогательная функция для получения цены по тикеру"""
        if ticker_raw is None:
            ticker_raw = ticker
        try:
            # Получаем последнюю цену из БД
            from sqlalchemy import create_engine, text
            from config_loader import get_database_url
            
            engine = create_engine(get_database_url())
            with engine.connect() as conn:
                result = conn.execute(
                    text("""
                        SELECT date, close, sma_5, volatility_5, rsi
                        FROM quotes
                        WHERE ticker = :ticker
                        ORDER BY date DESC
                        LIMIT 1
                    """),
                    {"ticker": ticker}
                )
                row = result.fetchone()
            
            if not row:
                # Пробуем найти похожий тикер в БД
                # Ищем по базовому символу (GC, GBPUSD и т.д.)
                base_symbol = ticker.replace('=', '').replace('-', '').replace('X', '').replace('F', '')
                with engine.connect() as conn:
                    similar = conn.execute(
                        text("""
                            SELECT DISTINCT ticker FROM quotes
                            WHERE ticker LIKE :pattern1 OR ticker LIKE :pattern2
                            ORDER BY ticker
                            LIMIT 5
                        """),
                        {
                            "pattern1": f"{base_symbol}%",
                            "pattern2": f"%{base_symbol}%"
                        }
                    ).fetchall()
                if similar:
                    suggestions = ", ".join([f"`{s[0]}`" for s in similar])
                    await update.message.reply_text(
                        f"❌ Нет данных для `{ticker_raw}`\n\n"
                        f"Возможно, вы имели в виду: {suggestions}",
                        parse_mode='Markdown'
                    )
                else:
                    await update.message.reply_text(
                        f"❌ Нет данных для `{ticker_raw}`\n"
                        f"Проверьте тикер или запустите `update_prices.py {ticker}`",
                        parse_mode='Markdown'
                    )
                return
            
            date, close, sma_5, vol_5, rsi = row
            
            # Форматируем значения с проверкой на None
            date_str = date.strftime('%Y-%m-%d') if date else 'N/A'
            close_str = f"${close:.2f}" if close is not None else "N/A"
            sma_str = f"${sma_5:.2f}" if sma_5 is not None else "N/A"
            vol_str = f"{vol_5:.2f}%" if vol_5 is not None else "N/A"
            
            # Форматируем RSI
            rsi_text = ""
            if rsi is not None:
                if rsi >= 70:
                    rsi_emoji = "🔴"
                    rsi_status = "перекупленность"
                elif rsi <= 30:
                    rsi_emoji = "🟢"
                    rsi_status = "перепроданность"
                elif rsi >= 60:
                    rsi_emoji = "🟡"
                    rsi_status = "близко к перекупленности"
                elif rsi <= 40:
                    rsi_emoji = "🟡"
                    rsi_status = "близко к перепроданности"
                else:
                    rsi_emoji = "⚪"
                    rsi_status = "нейтральная зона"
                rsi_text = f"\n{rsi_emoji} RSI: {rsi:.1f} ({rsi_status})"
            
            # Экранируем ticker для Markdown
            ticker_escaped = _escape_markdown(ticker)
            
            response = f"""
💰 **{ticker_escaped}**

📅 Дата: {date_str}
💵 Цена: {close_str}
📈 SMA(5): {sma_str}
📊 Волатильность(5): {vol_str}{rsi_text}
            """
            
            # Пытаемся отправить с Markdown, при ошибке — без форматирования
            try:
                await update.message.reply_text(response.strip(), parse_mode='Markdown')
            except Exception as parse_err:
                if 'parse' in str(parse_err).lower() or 'entit' in str(parse_err).lower():
                    logger.warning(f"Ошибка парсинга Markdown для /price {ticker}, отправляем без форматирования: {parse_err}")
                    await update.message.reply_text(response.strip())
                else:
                    raise
            
        except Exception as e:
            logger.error(f"Ошибка получения цены для {ticker}: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def _handle_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /price <ticker>"""
        user_id = update.effective_user.id
        
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        
        if not context.args or len(context.args) == 0:
            await update.message.reply_text(
                "❌ Укажите тикер\n"
                "Пример: `/price GC=F`",
                parse_mode='Markdown'
            )
            return
        
        ticker_raw = context.args[0].upper()
        ticker = _normalize_ticker(ticker_raw)
        await self._handle_price_by_ticker(update, ticker, ticker_raw)
    
    async def _handle_chart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /chart <ticker> [days] или /chart game_5m [days]"""
        user_id = update.effective_user.id

        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return

        if not context.args or len(context.args) == 0:
            await update.message.reply_text(
                "❌ Укажите тикер или game_5m\n"
                "Примеры: `/chart GC=F 7` или `/chart game_5m 5` (все тикеры игры 5m, 5 сессий)",
                parse_mode='Markdown'
            )
            return

        first_arg = context.args[0].strip().lower()
        if first_arg in ("game_5m", "game5m"):
            days = 5
            if len(context.args) >= 2:
                try:
                    days = max(1, min(7, int(context.args[1].strip())))
                except (ValueError, TypeError):
                    pass
            await self._handle_chart_game_5m(update, context, days)
            return

        ticker_raw = context.args[0].strip().upper()
        ticker = _normalize_ticker(ticker_raw)
        days = 1  # По умолчанию текущий день
        for i in range(1, len(context.args)):
            try:
                d = int(context.args[i].strip())
                days = max(1, min(30, d))
                break
            except (ValueError, IndexError):
                continue

        try:
            await update.message.reply_text(f"📈 Построение графика для {ticker}...")

            # Получаем данные из БД
            from sqlalchemy import create_engine, text
            from config_loader import get_database_url
            from datetime import datetime, timedelta
            import pandas as pd

            engine = create_engine(get_database_url())
            # Начало дня (00:00), чтобы не отсечь дневные свечи с date в полночь
            cutoff_date = (datetime.now() - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
            
            logger.info(f"Запрос данных для {ticker} с {cutoff_date} (последние {days} дней)")
            
            with engine.connect() as conn:
                df = pd.read_sql(
                    text("""
                        SELECT date, open, high, low, close, sma_5, volatility_5, rsi
                        FROM quotes
                        WHERE ticker = :ticker AND date >= :cutoff_date
                        ORDER BY date ASC
                    """),
                    conn,
                    params={"ticker": ticker, "cutoff_date": cutoff_date}
                )
                # Сделки за период графика — для отметок входа/выхода (фиксация прибыли/убытков)
                end_date = (pd.Timestamp(df["date"].max()) + pd.Timedelta(days=1)) if not df.empty else datetime.now()
                trades_rows = conn.execute(
                    text("""
                        SELECT ts, price, side, signal_type
                        FROM trade_history
                        WHERE ticker = :ticker AND ts >= :cutoff_date AND ts < :end_date
                        ORDER BY ts ASC
                    """),
                    {"ticker": ticker, "cutoff_date": cutoff_date, "end_date": end_date},
                ).fetchall()
            
            logger.info(f"Получено {len(df)} записей для {ticker}")
            
            if df.empty:
                logger.warning(f"Нет данных для {ticker} за последние {days} дней")
                await update.message.reply_text(
                    f"❌ Нет данных для {ticker} за последние {days} дней\n"
                    f"Попробуйте увеличить период: `/chart {ticker} 7`",
                    parse_mode='Markdown'
                )
                return
            
            # Объясняем пользователю формат данных
            if days == 1 and len(df) == 1:
                await update.message.reply_text(
                    f"ℹ️ **Формат данных:**\n\n"
                    f"В базе хранятся **дневные данные** (цена закрытия за день), "
                    f"а не внутридневные.\n\n"
                    f"За один день = одна запись (цена закрытия).\n\n"
                    f"Для графика с несколькими точками используйте:\n"
                    f"`/chart {ticker} 7` (7 дней = 7 точек)\n"
                    f"`/chart {ticker} 30` (30 дней = 30 точек)",
                    parse_mode='Markdown'
                )
            
            # Строим график
            try:
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as plt
                import matplotlib.dates as mdates
                from matplotlib.patches import Rectangle
                from matplotlib.lines import Line2D
                from io import BytesIO

                logger.info("Инициализация matplotlib...")
                try:
                    plt.style.use('seaborn-v0_8-whitegrid')
                except Exception:
                    pass
                plt.rcParams['font.size'] = 9

                df['date'] = pd.to_datetime(df['date'])
                n_points = len(df)
                has_ohlc = all(c in df.columns and df[c].notna().any() for c in ('open', 'high', 'low'))

                # Разбор сделок: маркер выхода по фактическому PnL (выход >= вход → тейк, иначе стоп)
                trades_buy_ts, trades_buy_p = [], []
                trades_take_ts, trades_take_p = [], []
                trades_stop_ts, trades_stop_p = [], []
                trades_other_ts, trades_other_p = [], []
                last_buy_price = None
                for row in trades_rows:
                    ts, price, side, signal_type = row[0], float(row[1]), row[2], (row[3] or "")
                    if ts is None:
                        continue
                    ts = pd.Timestamp(ts)
                    if getattr(ts, "tzinfo", None) is not None:
                        try:
                            ts = ts.tz_localize(None)
                        except Exception:
                            ts = ts.tz_convert(None) if ts.tzinfo else ts
                    if side == "BUY":
                        trades_buy_ts.append(ts)
                        trades_buy_p.append(price)
                        last_buy_price = price
                    elif side == "SELL":
                        if last_buy_price is not None:
                            if price >= last_buy_price:
                                trades_take_ts.append(ts)
                                trades_take_p.append(price)
                            else:
                                trades_stop_ts.append(ts)
                                trades_stop_p.append(price)
                        else:
                            trades_other_ts.append(ts)
                            trades_other_p.append(price)

                # Интервал подписей дат: все точки рисуем, подписи реже
                if n_points <= 7:
                    day_interval = 1
                elif n_points <= 14:
                    day_interval = 2
                else:
                    day_interval = max(1, n_points // 10)

                def draw_price_axes(ax1, use_ohlc):
                    ax1.set_facecolor('#ffffff')
                    if use_ohlc:
                        width = 0.7
                        half = width / 2
                        hr = df['high'].max() - df['low'].min()
                        hr = hr if hr and hr > 0 else float(df['close'].max() - df['close'].min() or 1)
                        min_body = max(0.005 * hr, 0.01)
                        for _, row in df.iterrows():
                            x = mdates.date2num(row['date'])
                            o = row.get('open') if pd.notna(row.get('open')) else row['close']
                            h = row.get('high') if pd.notna(row.get('high')) else max(o, row['close'])
                            l = row.get('low') if pd.notna(row.get('low')) else min(o, row['close'])
                            c = float(row['close'])
                            o, h, l = float(o), float(h), float(l)
                            # Тени (тонкие)
                            ax1.vlines(x, l, h, color='#444', linewidth=0.6, alpha=0.9)
                            top, bot = max(o, c), min(o, c)
                            body_h = (top - bot) if top > bot else min_body
                            if top == bot:
                                bot -= min_body / 2
                                body_h = min_body
                            color = '#26a69a' if c >= o else '#ef5350'  # зелёный / красный
                            rect = Rectangle((x - half, bot), width, body_h, facecolor=color, edgecolor=color, linewidth=0.5)
                            ax1.add_patch(rect)
                        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
                        ax1.xaxis.set_major_locator(mdates.DayLocator(interval=day_interval))
                        leg_up = Line2D([0], [0], color='#26a69a', linewidth=6, label='Рост')
                        leg_dn = Line2D([0], [0], color='#ef5350', linewidth=6, label='Падение')
                        legend_handles = [leg_up, leg_dn]
                    else:
                        ax1.plot(df['date'], df['close'], color='#1565c0', linewidth=2, label='Close')
                        legend_handles = []
                    if 'sma_5' in df.columns and df['sma_5'].notna().any():
                        ax1.plot(df['date'], df['sma_5'], color='#7e57c2', linewidth=1.2, linestyle='--', label='SMA(5)')
                    ax1.set_ylabel('Цена', fontsize=10)
                    h = list(legend_handles) + [l for l in ax1.get_lines() if (l.get_label() or '').startswith('SMA')]
                    ax1.legend(handles=h if h else None, loc='upper left', framealpha=0.9)
                    ax1.grid(True, linestyle='--', alpha=0.4)
                    ax1.tick_params(axis='both', labelsize=9)

                def draw_trade_markers(ax):
                    """Отметки сделок: вход (BUY) — *, тейк/стоп — треугольники; серый — выход без P/L."""
                    if trades_buy_ts:
                        ax.scatter(
                            trades_buy_ts,
                            trades_buy_p,
                            color='#0284c7',
                            marker='*',
                            s=200,
                            zorder=5,
                            label='Вход * (BUY)',
                            edgecolors='#0c4a6e',
                            linewidths=0.9,
                        )
                    if trades_take_ts:
                        ax.scatter(trades_take_ts, trades_take_p, color='#0277bd', marker='v', s=80, zorder=5, label='Тейк (прибыль)', edgecolors='#01579b', linewidths=1)
                    if trades_stop_ts:
                        ax.scatter(trades_stop_ts, trades_stop_p, color='#c62828', marker='v', s=80, zorder=5, label='Стоп (убыток)', edgecolors='#b71c1c', linewidths=1)
                    if trades_other_ts:
                        ax.scatter(trades_other_ts, trades_other_p, color='#757575', marker='v', s=60, zorder=4, label='Выход (без P/L)', edgecolors='#616161', linewidths=0.8)

                has_rsi = 'rsi' in df.columns and df['rsi'].notna().any()
                if n_points <= 2 or not has_rsi:
                    fig, ax1 = plt.subplots(1, 1, figsize=(11, 5), facecolor='white')
                    draw_price_axes(ax1, has_ohlc)
                    draw_trade_markers(ax1)
                    ax1.legend(loc='upper left', framealpha=0.9)
                    ax1.set_xlabel('Дата', fontsize=10)
                    ax1.set_title(f'{ticker}  —  {n_points} дн.', fontsize=11, fontweight='bold', pad=6)
                    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
                    ax1.xaxis.set_major_locator(mdates.DayLocator(interval=day_interval))
                    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30, ha='right')
                else:
                    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), facecolor='white', sharex=True,
                                                    gridspec_kw={'height_ratios': [1.4, 0.8], 'hspace': 0.08})
                    draw_price_axes(ax1, has_ohlc)
                    draw_trade_markers(ax1)
                    ax1.legend(loc='upper left', framealpha=0.9)
                    ax1.set_title(f'{ticker}  —  {n_points} дн.', fontsize=11, fontweight='bold', pad=6)
                    ax2.set_facecolor('#ffffff')
                    ax2.plot(df['date'], df['rsi'], color='#ff9800', linewidth=1.8, label='RSI')
                    ax2.axhline(y=70, color='#c62828', linestyle='--', alpha=0.6, linewidth=0.8)
                    ax2.axhline(y=30, color='#2e7d32', linestyle='--', alpha=0.6, linewidth=0.8)
                    ax2.set_ylabel('RSI', fontsize=10)
                    ax2.set_ylim(0, 100)
                    ax2.legend(loc='upper left', framealpha=0.9)
                    ax2.grid(True, linestyle='--', alpha=0.4)
                    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
                    ax2.xaxis.set_major_locator(mdates.DayLocator(interval=day_interval))
                    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30, ha='right')
                    ax2.tick_params(axis='both', labelsize=9)

                plt.tight_layout(pad=1.2)
                img_buffer = BytesIO()
                plt.savefig(img_buffer, format='png', dpi=120, bbox_inches='tight', facecolor='white')
                img_buffer.seek(0)
                plt.close()
                
                logger.info(f"Отправка графика для {ticker} ({len(df)} точек данных)")
                
                # Формируем подпись
                n_trades = len(trades_buy_ts) + len(trades_take_ts) + len(trades_stop_ts) + len(trades_other_ts)
                caption = f"📈 {ticker} - {days} дней ({len(df)} точек)"
                if n_trades > 0:
                    parts = []
                    if trades_buy_ts:
                        parts.append("* вход (син.)")
                    if trades_take_ts:
                        parts.append("▼ тейк (голуб.)")
                    if trades_stop_ts:
                        parts.append("▼ стоп (красн.)")
                    if trades_other_ts:
                        parts.append("▼ выход без P/L (сер.)")
                    caption += f"\n📌 Сделки: {', '.join(parts)} — {n_trades} шт."
                if has_ohlc:
                    caption += "\n\nℹ️ Свечи: open, high, low, close (дневные)"
                elif days == 1:
                    caption += "\n\nℹ️ Данные: дневные (цена закрытия за день)"
                elif len(df) < 5:
                    caption += "\n\nℹ️ Данные: дневные (цена закрытия). Для свечей загрузите OHLC: python update_prices.py --backfill 30"
                
                # Отправляем изображение
                await update.message.reply_photo(photo=img_buffer, caption=caption)
                
            except ImportError as e:
                logger.error(f"Ошибка импорта matplotlib: {e}")
                await update.message.reply_text(
                    "❌ Библиотека matplotlib не установлена.\n"
                    "Установите: `pip install matplotlib`"
                )
            except Exception as e:
                logger.error(f"Ошибка построения графика: {e}", exc_info=True)
                await update.message.reply_text(f"❌ Ошибка построения графика: {str(e)}")
            
        except Exception as e:
            logger.error(f"Ошибка построения графика для {ticker}: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка построения графика: {str(e)}")

    def _fetch_5m_data_sync(self, ticker: str, days: int = 5):
        """Синхронная загрузка 5-минутных данных через yfinance (вызывать из executor).

        Запрашивает явный диапазон дат [сегодня − days .. сегодня], чтобы получать
        самые свежие данные. Yahoo при period='1d' отдаёт «последний торговый день»
        с задержкой, поэтому без start/end данные могут быть за прошлые дни.
        """
        import yfinance as yf
        import pandas as pd
        t = yf.Ticker(ticker)
        days = min(max(1, days), 7)
        end_date = datetime.utcnow() + timedelta(days=1)  # end exclusive
        start_date = datetime.utcnow() - timedelta(days=days)
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")
        df = t.history(start=start_str, end=end_str, interval="5m", auto_adjust=False)
        if df is None or df.empty:
            return None
        df = df.rename_axis("datetime").reset_index()
        for c in ("Open", "High", "Low", "Close"):
            if c not in df.columns:
                return None
        return df

    async def _handle_chart5m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """График 5-минутных данных по требованию."""
        async def _reply(text: str, **kwargs):
            try:
                await update.message.reply_text(text, **kwargs)
            except Exception as e:
                logger.warning("chart5m: не удалось отправить ответ: %s", e)
        if not self._check_access(update.effective_user.id):
            await _reply("❌ Доступ запрещен")
            return
        if not context.args:
            await _reply(
                "❌ Укажите тикер. Пример: /chart5m SNDK или /chart5m GBPUSD=X 3",
                parse_mode=None,
            )
            return
        ticker_raw = context.args[0].strip().upper()
        ticker = _normalize_ticker(ticker_raw)
        days = 5
        if len(context.args) >= 2:
            try:
                days = max(1, min(7, int(context.args[1].strip())))
            except (ValueError, TypeError):
                pass
        logger.info("chart5m: тикер=%s, days=%s (args=%s)", ticker, days, context.args)
        await _reply(
            f"📥 Загрузка 5m для {ticker}: последние {days} амер. сессий (9:30–16:00 ET)…"
        )
        loop = asyncio.get_event_loop()
        try:
            from services.recommend_5m import fetch_5m_ohlc, filter_to_last_n_us_sessions
            fetch_days = min(max(days + 2, 5), 7)
            df = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: fetch_5m_ohlc(ticker, days=fetch_days)
                ),
                timeout=90.0,
            )
            if df is not None and not df.empty:
                df = filter_to_last_n_us_sessions(df, n=days)
        except asyncio.TimeoutError:
            logger.warning("chart5m: таймаут загрузки 5m данных для %s", ticker)
            await _reply(
                f"❌ Таймаут загрузки данных для {ticker} (90 с). Yahoo может быть перегружен. Попробуйте позже или /chart5m {ticker} 3"
            )
            return
        except Exception as e:
            logger.exception("Ошибка загрузки 5m")
            await _reply(f"❌ Ошибка загрузки: {e}")
            return
        if df is None or df.empty:
            msg = (
                f"❌ Нет 5m данных для {ticker} за последние {days} сессий (9:30–16:00 ET). "
                "Попробуйте /chart5m SNDK 1 или 3. В выходные биржа закрыта."
            )
            try:
                from datetime import datetime, timedelta
                from services.game_5m import get_trades_for_chart, trade_ts_to_et, TRADE_HISTORY_TZ
                now = datetime.utcnow()
                dt_start = now - timedelta(days=min(days + 2, 14))
                trades = get_trades_for_chart(ticker, dt_start, now)
                if trades:
                    lines = ["📋 Сделки GAME_5M по %s (без свечей):" % ticker]
                    for t in trades[-10:]:
                        ts = t.get("ts")
                        tz = t.get("ts_timezone") or TRADE_HISTORY_TZ
                        try:
                            ts_et = trade_ts_to_et(ts, source_tz=tz)
                            ts_str = ts_et.strftime("%d.%m %H:%M") if hasattr(ts_et, "strftime") else str(ts)
                        except Exception:
                            ts_str = str(ts)
                        lines.append("  %s @ %.2f — %s" % (t.get("side", ""), float(t.get("price", 0)), ts_str))
                    msg = msg + "\n\n" + "\n".join(lines)
            except Exception:
                pass
            await _reply(msg, parse_mode=None)
            return
        # Открытая позиция только из игры 5m (GAME_5M); портфель ExecutionAgent на график 5m не тянем
        entry_price = None
        try:
            from services.game_5m import get_open_position as get_game_position
            pos = get_game_position(ticker)
            if pos and isinstance(pos.get("entry_price"), (int, float)):
                entry_price = float(pos["entry_price"])
        except Exception:
            pass
        # Прогноз для графика: хай сессии, оценка подъёма по кривизне, тейк при открытой позиции (таймаут 45 с)
        d5_chart = None
        try:
            from services.recommend_5m import get_decision_5m
            d5_chart = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: get_decision_5m(ticker, days=days, use_llm_news=False)),
                timeout=45.0,
            )
        except asyncio.TimeoutError:
            logger.debug("chart5m: таймаут get_decision_5m для %s, график без прогноза", ticker)
        except Exception:
            pass
        try:
            import pandas as pd
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from io import BytesIO
            df["datetime"] = pd.to_datetime(df["datetime"])
            # Шкала в времени американской биржи (Eastern): маркеры сделок (ET) совпадают с свечами
            if hasattr(df["datetime"].dtype, "tz") and df["datetime"].dtype.tz is not None:
                dt_plot = df["datetime"].dt.tz_convert("America/New_York").dt.tz_localize(None)
            else:
                d = df["datetime"]
                try:
                    d = d.dt.tz_localize("America/New_York", ambiguous=True)
                except Exception:
                    d = d.dt.tz_localize("UTC", ambiguous=True).dt.tz_convert("America/New_York")
                dt_plot = d.dt.tz_localize(None)
            df["_dt_plot"] = dt_plot
            # Ключ сессии всегда из dt_plot (ET), чтобы не зависеть от типа _session из фильтра
            df["_session_key"] = dt_plot.dt.strftime("%Y-%m-%d")
            dt_min = dt_plot.min()
            dt_max = dt_plot.max()
            # Сессии: слева направо от более раннего к более позднему дню (каждая — своё окно 09:30–16:00 ET).
            MIN_BARS_PER_SESSION = 3
            unique_keys = sorted(df["_session_key"].unique())
            session_dates = [sk for sk in unique_keys if (df["_session_key"] == sk).sum() >= MIN_BARS_PER_SESSION]
            if not session_dates:
                session_dates = unique_keys
            if not session_dates:
                await _reply(
                    f"❌ Нет данных по сессиям 9:30–16:00 ET для {ticker}. Попробуйте позже или другой тикер."
                )
                return
            # Для каждой даты — явное окно торговли ET (09:30–16:00), чтобы ось и данные всегда по своей дате
            def session_window(session_key: str):
                t = pd.Timestamp(session_key)
                start = t.replace(hour=9, minute=30, second=0, microsecond=0)
                end = t.replace(hour=16, minute=0, second=0, microsecond=0)
                return start, end
            n_sessions = len(session_dates)
            if n_sessions == 1:
                fig, axes = plt.subplots(1, 1, figsize=(11, 5), facecolor="white")
                axes = [axes]
            else:
                # Горизонтально впритык: каждая сессия — свой столбец (умеренный размер — меньше таймаут при отправке)
                w_per_day = 3.6
                fig, axes = plt.subplots(
                    1, n_sessions, figsize=(w_per_day * n_sessions, 4.5), sharex=False, sharey=True, facecolor="white"
                )
                axes = list(axes)
            # Сделки за период (один раз); маркер выхода — по фактическому PnL (цена выхода vs входа), а не по signal_type
            buy_ts, buy_p = [], []
            take_ts, take_p = [], []
            stop_ts, stop_p = [], []
            other_ts, other_p = [], []
            try:
                from services.game_5m import (
                    get_trades_for_chart,
                    partition_trades_for_chart_pnl,
                    trade_ts_to_et,
                    TRADE_HISTORY_TZ,
                )

                def _trade_ts_et(t):
                    ts = t["ts"]
                    try:
                        stored_tz = t.get("ts_timezone") or TRADE_HISTORY_TZ
                        ts_et = trade_ts_to_et(ts, source_tz=stored_tz)
                        if ts_et is not None:
                            dt = ts_et.to_pydatetime() if hasattr(ts_et, "to_pydatetime") else ts_et
                            ts = dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt
                    except Exception:
                        pass
                    return ts

                raw_trades = get_trades_for_chart(ticker, dt_min, dt_max)
                buys, sell_win, sell_loss, sell_neutral = partition_trades_for_chart_pnl(raw_trades)
                for t in buys:
                    buy_ts.append(_trade_ts_et(t))
                    buy_p.append(float(t["price"]))
                for t in sell_win:
                    take_ts.append(_trade_ts_et(t))
                    take_p.append(float(t["price"]))
                for t in sell_loss:
                    stop_ts.append(_trade_ts_et(t))
                    stop_p.append(float(t["price"]))
                for t in sell_neutral:
                    other_ts.append(_trade_ts_et(t))
                    other_p.append(float(t["price"]))
            except Exception:
                pass
            for idx, sd in enumerate(session_dates):
                ax = axes[idx]
                ax.set_facecolor("#ffffff")
                # Окно торговли для этой даты (09:30–16:00 ET) — только сессия, без зоны после 16:00
                window_start, window_end = session_window(sd)
                df_i = df[(df["_dt_plot"] >= window_start) & (df["_dt_plot"] <= window_end)].copy()
                ax.set_xlim(window_start, window_end)
                ax.autoscale(enable=False, axis="x")
                if df_i.empty:
                    ax.text(0.5, 0.5, f"Нет данных за {sd}", ha="center", va="center", transform=ax.transAxes)
                    try:
                        sd_str = pd.Timestamp(sd).strftime("%d.%m.%Y")
                    except Exception:
                        sd_str = str(sd)
                    ax.set_title(f"{ticker} — 5m · {sd_str} (9:30–16:00 ET)", fontsize=10, fontweight="bold")
                    ax.set_ylabel("Цена", fontsize=10)
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
                    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=10))
                    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")
                    ax.grid(True, linestyle="--", alpha=0.4)
                    continue
                dt_i = df_i["_dt_plot"]
                dt_i_min = window_start
                dt_i_max = window_end
                ax.plot(dt_i, df_i["Close"], color="#1565c0", linewidth=1.2, label="Close")
                if "Open" in df_i.columns:
                    ax.fill_between(dt_i, df_i["Low"], df_i["High"], alpha=0.15, color="#1565c0")
                if entry_price is not None:
                    ax.axhline(
                        entry_price,
                        color="#0284c7",
                        linestyle="--",
                        linewidth=1.2,
                        alpha=0.9,
                        label=f"Вход * @ {entry_price:.2f}",
                    )
                is_last_session = idx == len(session_dates) - 1
                if is_last_session and d5_chart:
                    price_cur = d5_chart.get("price")
                    session_high = d5_chart.get("session_high")
                    est_bounce = d5_chart.get("estimated_bounce_pct")
                    if session_high is not None and session_high > 0:
                        ax.axhline(
                            session_high,
                            color="#f57c00",
                            linestyle=":",
                            linewidth=1.0,
                            alpha=0.85,
                            label=f"Хай сессии {session_high:.2f}",
                        )
                    if price_cur is not None and price_cur > 0 and est_bounce is not None and est_bounce > 0:
                        forecast_price = price_cur * (1 + est_bounce / 100.0)
                        ax.axhline(
                            forecast_price,
                            color="#00897b",
                            linestyle="-.",
                            linewidth=1.0,
                            alpha=0.85,
                            label=f"Прогноз подъёма ~{forecast_price:.2f}",
                        )
                    if entry_price is not None and entry_price > 0:
                        try:
                            from services.game_5m import _effective_take_profit_pct
                            mom = d5_chart.get("momentum_2h_pct")
                            take_pct = _effective_take_profit_pct(mom, ticker=ticker)
                            take_level = entry_price * (1 + take_pct / 100.0)
                            ax.axhline(
                                take_level,
                                color="#15803d",
                                linestyle=":",
                                linewidth=1.0,
                                alpha=0.85,
                                label=f"Тейк @ {take_level:.2f} (+{take_pct:.1f}%)",
                            )
                        except Exception:
                            pass
                # Маркеры сделок: показываем все сделки, попадающие в окно сессии [dt_i_min, dt_i_max]
                def _in_range(ts, lo, hi):
                    try:
                        t = pd.Timestamp(ts)
                        if t.tzinfo is not None:
                            t = t.tz_convert("America/New_York").tz_localize(None)
                        return lo <= t <= hi
                    except Exception:
                        return False
                # Диапазон цен сессии: маркеры тейка/стопа не рисуем выше/ниже реальных цен (если в БД ошибочно записана 600 при хае 571)
                price_lo = float(df_i["Low"].min()) if "Low" in df_i.columns else float(df_i["Close"].min())
                price_hi = float(df_i["High"].max()) if "High" in df_i.columns else float(df_i["Close"].max())
                def _clip_p(pr: float) -> float:
                    return max(price_lo, min(price_hi, pr))
                buy_i = [(t, p) for t, p in zip(buy_ts, buy_p) if _in_range(t, dt_i_min, dt_i_max)]
                take_i = [(t, _clip_p(p)) for t, p in zip(take_ts, take_p) if _in_range(t, dt_i_min, dt_i_max)]
                stop_i = [(t, _clip_p(p)) for t, p in zip(stop_ts, stop_p) if _in_range(t, dt_i_min, dt_i_max)]
                other_i = [(t, _clip_p(p)) for t, p in zip(other_ts, other_p) if _in_range(t, dt_i_min, dt_i_max)]
                if buy_i:
                    ax.scatter(
                        [x[0] for x in buy_i],
                        [x[1] for x in buy_i],
                        color="#0284c7",
                        marker="*",
                        s=200,
                        zorder=5,
                        label="Вход * (BUY)",
                        edgecolors="#0c4a6e",
                        linewidths=0.9,
                    )
                if take_i:
                    ax.scatter([x[0] for x in take_i], [x[1] for x in take_i], color="#22c55e", marker="^", s=70, zorder=5, label="Закрытие + (прибыль)", edgecolors="#14532d", linewidths=1)
                if stop_i:
                    ax.scatter([x[0] for x in stop_i], [x[1] for x in stop_i], color="#ef4444", marker="v", s=70, zorder=5, label="Закрытие − (убыток)", edgecolors="#991b1b", linewidths=1)
                if other_i:
                    ax.scatter([x[0] for x in other_i], [x[1] for x in other_i], color="#94a3b8", marker="^", s=60, zorder=4, label="Выход (нет P/L к позиции)", edgecolors="#475569", linewidths=0.8)
                ax.set_ylabel("Цена", fontsize=10)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
                ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=10))
                plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")
                # Легенда справа от графика, чтобы не закрывать цену
                ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1), fontsize=7, framealpha=0.95)
                ax.grid(True, linestyle="--", alpha=0.4)
                try:
                    sd_str = pd.Timestamp(sd).strftime("%d.%m.%Y")
                except Exception:
                    sd_str = str(sd)
                ax.set_title(f"{ticker} — 5m · {sd_str} (9:30–16:00 ET)", fontsize=10, fontweight="bold")
            axes[-1].set_xlabel("Дата, время", fontsize=10)
            plt.tight_layout()
            buf = BytesIO()
            plt.savefig(buf, format="png", dpi=60, bbox_inches="tight", facecolor="white")
            buf.seek(0)
            plt.close()
            n_markers = len(buy_ts) + len(take_ts) + len(stop_ts) + len(other_ts)
            range_str = f"{dt_min.strftime('%d.%m %H:%M')} – {dt_max.strftime('%d.%m %H:%M')}"
            if n_sessions > 1:
                caption = f"📈 {ticker} — 5 мин, {n_sessions} сессий (9:30–16:00 ET), {len(df)} свечей."
            else:
                caption = f"📈 {ticker} — 5 мин, {len(df)} свечей. {range_str}"
            if n_markers > 0:
                parts = []
                if buy_ts:
                    parts.append("* вход")
                if take_ts:
                    parts.append("▲ закрытие +")
                if stop_ts:
                    parts.append("▼ закрытие −")
                if other_ts:
                    parts.append("▲ выход без P/L (сер.)")
                caption += f"\n📌 Сделки: {', '.join(parts)} — {n_markers} шт. Время ET. Зелёный ▲ — прибыльное закрытие; серый — SELL без расчёта P/L к позиции; * — покупка."
            if entry_price is not None:
                caption += f"\n📌 Позиция открыта @ ${entry_price:.2f}"
            buf.seek(0)
            try:
                await update.message.reply_photo(
                    photo=buf,
                    caption=caption,
                )
            except Exception as send_err:
                if "timeout" in str(send_err).lower() or "timed" in str(send_err).lower():
                    buf.seek(0)
                    try:
                        await update.message.reply_document(
                            document=buf,
                            filename=f"chart5m_{ticker}_{n_sessions}d.png",
                            caption=caption,
                        )
                    except Exception:
                        await update.message.reply_text(
                            "⏱ График построен, но отправка по таймауту. Попробуйте /chart5m с 1 сессией или повторите позже."
                        )
                else:
                    raise
        except Exception as e:
            logger.exception("Ошибка графика 5m")
            err_msg = str(e)[:400] if str(e) else repr(e)[:400]
            try:
                await update.message.reply_text(f"❌ Ошибка графика 5m: {err_msg}")
            except Exception:
                pass

    async def _handle_chart_game_5m(self, update: Update, context: ContextTypes.DEFAULT_TYPE, days: int):
        """График по всей игре 5m: для каждого тикера — горизонтальный график как /chart5m, тикеры друг под другом."""
        if not self._check_access(update.effective_user.id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        from services.ticker_groups import get_tickers_game_5m
        tickers = get_tickers_game_5m()
        if not tickers:
            await update.message.reply_text("❌ Нет тикеров в игре 5m (GAME_5M_TICKERS / TICKERS_FAST).")
            return

        async def _reply(text: str):
            try:
                msg = update.message or (update.effective_message if update else None)
                if msg is not None:
                    await msg.reply_text(text)
            except Exception as e:
                logger.warning("chart game_5m: %s", e)

        await _reply(f"📥 Загрузка 5m по {len(tickers)} тикерам, {days} сессий…")
        loop = asyncio.get_event_loop()

        def fetch_one(ticker: str) -> dict:
            from services.recommend_5m import fetch_5m_ohlc, filter_to_last_n_us_sessions
            from services.game_5m import get_trades_for_chart, get_open_position, trade_ts_to_et, TRADE_HISTORY_TZ
            from services.recommend_5m import get_decision_5m
            import pandas as pd
            empty = {"ticker": ticker, "df": None, "session_dates": [], "trades": [], "entry_price": None, "d5_chart": None, "dt_min": None, "dt_max": None}
            try:
                fetch_days = min(max(days + 2, 5), 7)
                df = fetch_5m_ohlc(ticker, days=fetch_days)
            except Exception as e:
                logger.warning("chart game_5m: загрузка %s: %s", ticker, e)
                return empty
            if df is None or df.empty:
                return empty
            df = filter_to_last_n_us_sessions(df, n=days)
            if df is None or df.empty:
                return empty
            df = df.copy()
            df["datetime"] = pd.to_datetime(df["datetime"])
            if hasattr(df["datetime"].dtype, "tz") and df["datetime"].dtype.tz is not None:
                dt_plot = df["datetime"].dt.tz_convert("America/New_York").dt.tz_localize(None)
            else:
                d = df["datetime"]
                try:
                    d = d.dt.tz_localize("America/New_York", ambiguous=True)
                except Exception:
                    d = d.dt.tz_localize("UTC", ambiguous=True).dt.tz_convert("America/New_York")
                dt_plot = d.dt.tz_localize(None)
            df["_dt_plot"] = dt_plot
            df["_session_key"] = dt_plot.dt.strftime("%Y-%m-%d")
            unique_keys = sorted(df["_session_key"].unique())
            MIN_BARS = 3
            session_dates = [sk for sk in unique_keys if (df["_session_key"] == sk).sum() >= MIN_BARS]
            if not session_dates:
                session_dates = unique_keys
            dt_min = df["_dt_plot"].min()
            dt_max = df["_dt_plot"].max()
            trades = []
            try:
                trades = get_trades_for_chart(ticker, dt_min, dt_max)
            except Exception:
                pass
            entry_price = None
            try:
                pos = get_open_position(ticker)
                if pos and isinstance(pos.get("entry_price"), (int, float)):
                    entry_price = float(pos["entry_price"])
            except Exception:
                pass
            d5_chart = None
            try:
                d5_chart = get_decision_5m(ticker, days=days, use_llm_news=False)
            except Exception:
                pass
            return {"ticker": ticker, "df": df, "session_dates": session_dates, "dt_min": dt_min, "dt_max": dt_max, "trades": trades, "entry_price": entry_price, "d5_chart": d5_chart}

        results = await asyncio.gather(*[loop.run_in_executor(None, lambda t=t: fetch_one(t)) for t in tickers])
        failed = [r["ticker"] for r in results if r["df"] is None and not r["session_dates"]]
        if failed and len(failed) == len(tickers):
            await _reply(f"❌ Нет 5m данных ни по одному тикеру (Yahoo пустой ответ). Попробуйте позже или /chart5m SNDK 1")
            return
        if failed:
            await _reply(f"⚠️ Нет данных по: {', '.join(failed)}. Остальные тикеры на графике.")

        max_cols = max((len(r["session_dates"]) for r in results), default=1)
        if max_cols == 0:
            max_cols = 1
        n_rows = len(tickers)
        w_per_day = 3.5
        h_per_row = 3.2
        import pandas as pd
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from io import BytesIO
        import numpy as np
        try:
            plt.style.use("seaborn-v0_8-whitegrid")
        except Exception:
            pass
        plt.rcParams["font.size"] = 8
        fig, axes = plt.subplots(n_rows, max_cols, figsize=(w_per_day * max_cols, h_per_row * n_rows), sharex=False, sharey=False, facecolor="white")
        if n_rows == 1 and max_cols == 1:
            axes = np.array([[axes]])
        elif n_rows == 1:
            axes = axes.reshape(1, -1)
        elif max_cols == 1:
            axes = axes.reshape(-1, 1)

        def session_window(session_key: str):
            t = pd.Timestamp(session_key)
            start = t.replace(hour=9, minute=30, second=0, microsecond=0)
            end = t.replace(hour=16, minute=0, second=0, microsecond=0)
            return start, end

        for i, res in enumerate(results):
            ticker = res["ticker"]
            df = res["df"]
            session_dates = res["session_dates"]
            trades = res["trades"]
            entry_price = res["entry_price"]
            d5_chart = res["d5_chart"]
            buy_ts, buy_p = [], []
            take_ts, take_p = [], []
            stop_ts, stop_p = [], []
            other_ts, other_p = [], []
            if df is not None and not df.empty and trades:
                from services.game_5m import partition_trades_for_chart_pnl, trade_ts_to_et, TRADE_HISTORY_TZ

                def _trade_ts_et_row(t):
                    ts = t["ts"]
                    try:
                        stored_tz = t.get("ts_timezone") or TRADE_HISTORY_TZ
                        ts_et = trade_ts_to_et(ts, source_tz=stored_tz)
                        if ts_et is not None:
                            dt = ts_et.to_pydatetime() if hasattr(ts_et, "to_pydatetime") else ts_et
                            ts = dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt
                    except Exception:
                        pass
                    return ts

                buys, sell_win, sell_loss, sell_neutral = partition_trades_for_chart_pnl(trades)
                for t in buys:
                    buy_ts.append(_trade_ts_et_row(t))
                    buy_p.append(float(t["price"]))
                for t in sell_win:
                    take_ts.append(_trade_ts_et_row(t))
                    take_p.append(float(t["price"]))
                for t in sell_loss:
                    stop_ts.append(_trade_ts_et_row(t))
                    stop_p.append(float(t["price"]))
                for t in sell_neutral:
                    other_ts.append(_trade_ts_et_row(t))
                    other_p.append(float(t["price"]))

            for j in range(max_cols):
                ax = axes[i, j]
                ax.set_facecolor("#ffffff")
                if j >= len(session_dates):
                    ax.set_visible(False)
                    continue
                sd = session_dates[j]
                window_start, window_end = session_window(sd)
                if df is None or df.empty:
                    ax.text(0.5, 0.5, f"{ticker}\nнет данных", ha="center", va="center", transform=ax.transAxes, fontsize=9)
                    try:
                        sd_str = pd.Timestamp(sd).strftime("%d.%m")
                    except Exception:
                        sd_str = str(sd)
                    ax.set_title(f"{ticker} · {sd_str}", fontsize=9)
                    ax.set_xlim(window_start, window_end)
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
                    ax.grid(True, linestyle="--", alpha=0.4)
                    continue
                df_i = df[(df["_dt_plot"] >= window_start) & (df["_dt_plot"] <= window_end)].copy()
                ax.set_xlim(window_start, window_end)
                ax.autoscale(enable=False, axis="x")
                if df_i.empty:
                    ax.text(0.5, 0.5, f"Нет данных за {sd}", ha="center", va="center", transform=ax.transAxes, fontsize=8)
                else:
                    dt_i = df_i["_dt_plot"]
                    ax.plot(dt_i, df_i["Close"], color="#1565c0", linewidth=1.0, label="Close")
                    if "Open" in df_i.columns:
                        ax.fill_between(dt_i, df_i["Low"], df_i["High"], alpha=0.12, color="#1565c0")
                    if entry_price is not None:
                        ax.axhline(entry_price, color="#0284c7", linestyle="--", linewidth=1.0, alpha=0.85, label=f"Вход * {entry_price:.1f}")
                    is_last_session = j == len(session_dates) - 1
                    if is_last_session and d5_chart:
                        session_high = d5_chart.get("session_high")
                        if session_high is not None and session_high > 0:
                            ax.axhline(session_high, color="#f57c00", linestyle=":", linewidth=0.9, alpha=0.8, label=f"Хай {session_high:.1f}")
                        if entry_price is not None and entry_price > 0:
                            try:
                                from services.game_5m import _effective_take_profit_pct
                                mom = d5_chart.get("momentum_2h_pct")
                                take_pct = _effective_take_profit_pct(mom, ticker=ticker)
                                take_level = entry_price * (1 + take_pct / 100.0)
                                ax.axhline(
                                    take_level,
                                    color="#15803d",
                                    linestyle=":",
                                    linewidth=0.9,
                                    alpha=0.85,
                                    label=f"Тейк @ {take_level:.2f} (+{take_pct:.1f}%)",
                                )
                            except Exception:
                                pass
                    def _in_range(ts, lo, hi):
                        try:
                            t = pd.Timestamp(ts)
                            if t.tzinfo is not None:
                                t = t.tz_convert("America/New_York").tz_localize(None)
                            return lo <= t <= hi
                        except Exception:
                            return False
                    price_lo = float(df_i["Low"].min()) if "Low" in df_i.columns else float(df_i["Close"].min())
                    price_hi = float(df_i["High"].max()) if "High" in df_i.columns else float(df_i["Close"].max())
                    def _clip(p):
                        return max(price_lo, min(price_hi, p))
                    buy_i = [(t, p) for t, p in zip(buy_ts, buy_p) if _in_range(t, window_start, window_end)]
                    take_i = [(t, _clip(p)) for t, p in zip(take_ts, take_p) if _in_range(t, window_start, window_end)]
                    stop_i = [(t, _clip(p)) for t, p in zip(stop_ts, stop_p) if _in_range(t, window_start, window_end)]
                    other_i = [(t, _clip(p)) for t, p in zip(other_ts, other_p) if _in_range(t, window_start, window_end)]
                    if buy_i:
                        ax.scatter(
                            [x[0] for x in buy_i],
                            [x[1] for x in buy_i],
                            color="#0284c7",
                            marker="*",
                            s=130,
                            zorder=5,
                            edgecolors="#0c4a6e",
                            linewidths=0.8,
                        )
                    if take_i:
                        ax.scatter([x[0] for x in take_i], [x[1] for x in take_i], color="#22c55e", marker="^", s=40, zorder=5, edgecolors="#14532d", linewidths=0.8)
                    if stop_i:
                        ax.scatter([x[0] for x in stop_i], [x[1] for x in stop_i], color="#ef4444", marker="v", s=40, zorder=5, edgecolors="#991b1b", linewidths=0.8)
                    if other_i:
                        ax.scatter([x[0] for x in other_i], [x[1] for x in other_i], color="#94a3b8", marker="^", s=32, zorder=4, edgecolors="#475569", linewidths=0.6)
                try:
                    sd_str = pd.Timestamp(sd).strftime("%d.%m")
                except Exception:
                    sd_str = str(sd)
                ax.set_title(f"{ticker} · {sd_str} (9:30–16:00 ET)", fontsize=9, fontweight="bold")
                ax.set_ylabel("Цена", fontsize=8)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
                ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=8))
                plt.setp(ax.xaxis.get_majorticklabels(), rotation=25, ha="right")
                ax.grid(True, linestyle="--", alpha=0.4)
            axes[i, 0].set_ylabel(ticker, fontsize=10, fontweight="bold")
        plt.tight_layout()
        buf = BytesIO()
        plt.savefig(buf, format="png", dpi=72, bbox_inches="tight", facecolor="white")
        buf.seek(0)
        plt.close()
        caption = f"📈 Игра 5m: все тикеры, {days} сессий (9:30–16:00 ET). Каждый ряд — один тикер, как /chart5m."
        try:
            await update.message.reply_photo(photo=buf, caption=caption)
        except Exception as send_err:
            if "timeout" in str(send_err).lower() or "timed" in str(send_err).lower():
                await _reply("⏱ График построен, отправка по таймауту. Попробуйте /chart game_5m 3 или повторите позже.")
            else:
                raise

    async def _handle_table5m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Таблица последних 5-минутных свечей."""
        if not self._check_access(update.effective_user.id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        if not context.args:
            await update.message.reply_text(
                "❌ Укажите тикер. Пример: `/table5m SNDK` или `/table5m GC=F 2`",
                parse_mode="Markdown"
            )
            return
        ticker_raw = context.args[0].strip().upper()
        ticker = _normalize_ticker(ticker_raw)
        days = 3
        for i in range(1, len(context.args)):
            try:
                days = max(1, min(7, int(context.args[i].strip())))
                break
            except (ValueError, IndexError):
                continue
        await update.message.reply_text(f"📥 Загрузка 5m для {ticker}...")
        loop = asyncio.get_event_loop()
        try:
            df = await loop.run_in_executor(None, self._fetch_5m_data_sync, ticker, days)
        except Exception as e:
            logger.exception("Ошибка загрузки 5m")
            await update.message.reply_text(f"❌ Ошибка загрузки: {e}")
            return
        if df is None or df.empty:
            await update.message.reply_text(f"❌ Нет 5m данных для {ticker}.")
            return
        import pandas as pd
        df["datetime"] = pd.to_datetime(df["datetime"])
        total = len(df)
        df_sorted = df.sort_values("datetime", ascending=False)
        range_str = ""
        if not df_sorted.empty:
            dt_min = df_sorted["datetime"].min()
            dt_max = df_sorted["datetime"].max()
            range_str = f"\n_Период в данных: {dt_min.strftime('%d.%m %H:%M')} – {dt_max.strftime('%d.%m %H:%M')}_"
        df_head = df_sorted.head(25)
        lines = [f"`{'Дата':<16} {'O':>10} {'H':>10} {'L':>10} {'C':>10}`"]
        for _, row in df_head.iterrows():
            ts = row["datetime"].strftime("%d.%m %H:%M")
            def _cell(v, width=10):
                if pd.isna(v) or (isinstance(v, float) and v != v):
                    return "—".rjust(width)
                return f"{float(v):>10.4f}"
            o_s = _cell(row.get("Open"))
            h_s = _cell(row.get("High"))
            lo_s = _cell(row.get("Low"))
            c_s = _cell(row.get("Close"))
            lines.append(f"`{ts:<16} {o_s} {h_s} {lo_s} {c_s}`")
        msg = f"📋 **{ticker}** — 5m свечи (последние {len(df_head)} из {total}){range_str}\n\n" + "\n".join(lines)
        if len(msg) > 4000:
            msg = msg[:3970] + "\n…"
        await update.message.reply_text(msg, parse_mode="Markdown")

    def _build_dashboard_sync(self, mode: str = "all") -> str:
        """Строит сводку дашборда (делегирует в services.dashboard_builder)."""
        from services.dashboard_builder import build_dashboard_text
        return build_dashboard_text(mode)

    async def _handle_dashboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Дашборд по отслеживаемым тикерам для проактивного мониторинга (решения, 5m, новости)."""
        if not self._check_access(update.effective_user.id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        mode = "all"
        if context.args:
            a = context.args[0].strip().lower()
            if a in ("5m", "daily", "all"):
                mode = a
        await update.message.reply_text("📥 Сбор дашборда...")
        loop = asyncio.get_event_loop()
        try:
            text = await loop.run_in_executor(None, self._build_dashboard_sync, mode)
        except Exception as e:
            logger.exception("Ошибка дашборда")
            await update.message.reply_text(f"❌ Ошибка: {e}")
            return
        # Без parse_mode: в тексте дашборда могут быть _ * из тикеров, VIX, period_str и т.д. — парсер падает
        if len(text) > 4000:
            parts = [text[i : i + 4000] for i in range(0, len(text), 4000)]
            for p in parts:
                await update.message.reply_text(p)
        else:
            await update.message.reply_text(text)

    async def _handle_analyser(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Анализатор эффективности закрытых сделок (единый код с web /analyzer)."""
        if not self._check_access(update.effective_user.id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        days = 7
        strategy = "GAME_5M"
        use_llm = False
        for a in (context.args or []):
            aa = (a or "").strip()
            if not aa:
                continue
            low = aa.lower()
            if low.isdigit():
                days = max(1, min(30, int(low)))
                continue
            if low in ("llm", "--llm"):
                use_llm = True
                continue
            if low.upper() in ("GAME_5M", "ALL", "PORTFOLIO"):
                strategy = low.upper()
        await update.message.reply_text(f"📊 Запускаю анализатор сделок: {days} дн., стратегия={strategy}, LLM={'on' if use_llm else 'off'}...")
        try:
            from services.trade_effectiveness_analyzer import analyze_trade_effectiveness, format_trade_effectiveness_text
            loop = asyncio.get_event_loop()
            report = await loop.run_in_executor(
                None,
                lambda: analyze_trade_effectiveness(days=days, strategy=strategy, use_llm=use_llm),
            )
            text = format_trade_effectiveness_text(report)
            if len(text) > 3900:
                text = text[:3900] + "\n…"
            await update.message.reply_text(text, parse_mode=None)
            try:
                payload_json = json.dumps(report, ensure_ascii=False, indent=2)
                fn = f"analyser_{strategy}_{days}d_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.json"
                await update.message.reply_document(document=BytesIO(payload_json.encode("utf-8")), filename=fn)
            except Exception as e:
                logger.debug("Не удалось отправить JSON анализатора: %s", e)
        except Exception as e:
            logger.exception("Ошибка /analyser")
            await update.message.reply_text(f"❌ Ошибка анализатора: {e}")
    
    async def _handle_tickers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /tickers"""
        user_id = update.effective_user.id if update.effective_user else None
        if user_id is None or not self._check_access(user_id):
            await self._reply_to_update(update, context, "❌ Доступ запрещен")
            return

        async def _send(text: str, parse_mode: str = "Markdown") -> None:
            await self._reply_to_update(update, context, text, parse_mode=parse_mode)

        try:
            # Список тикеров: из quotes (есть котировки) + из конфига (TICKERS_FAST/MEDIUM/LONG), чтобы тикеры вроде CL=F показывались сразу после добавления в TICKERS_LONG
            from sqlalchemy import create_engine, text
            from config_loader import get_database_url
            from services.ticker_groups import (
                get_tickers_fast,
                get_tickers_for_portfolio_game,
                get_all_ticker_groups,
            )

            engine = create_engine(get_database_url())
            from_quotes = []
            with engine.connect() as conn:
                result = conn.execute(
                    text("SELECT DISTINCT ticker FROM quotes ORDER BY ticker")
                )
                from_quotes = [row[0] for row in result]
            from_config = get_all_ticker_groups()
            seen = set(from_quotes)
            for t in from_config:
                if t and t not in seen:
                    seen.add(t)
                    from_quotes.append(t)
            tickers = sorted(from_quotes)

            if not tickers:
                await _send("ℹ️ Нет отслеживаемых инструментов")
                return

            # Роли: индикаторы (только контекст), 5m, портфель (открываем позиции)
            from services.ticker_groups import get_tickers_game_5m, get_tickers_for_portfolio_game, get_tickers_indicator_only
            indicator_set = set(get_tickers_indicator_only())
            game5m_set = set(get_tickers_game_5m())
            portfolio_set = set(get_tickers_for_portfolio_game())
            # Портфель без индикаторов = тикеры, по которым реально открываем позиции
            portfolio_trade_set = portfolio_set - indicator_set

            def _line(t: str, suffix: str = "") -> str:
                return f"  • {_escape_markdown(t)}{suffix}"

            response = "📊 **Отслеживаемые инструменты:**\n\n"

            # 1. Технические индексы (индикаторы) — только для контекста/корреляции
            indicators = [t for t in tickers if t in indicator_set]
            if indicators:
                response += "📐 **Технические индексы (индикаторы):**\n"
                response += "  _только контекст и корреляция, позиции не открываем_\n"
                response += "\n".join([_line(t) for t in sorted(indicators)])
                response += "\n\n"

            # 2. 5m — быстрая игра
            in_5m = [t for t in tickers if t in game5m_set]
            if in_5m:
                response += "⚡ **5m (быстрая игра):**\n"
                response += "\n".join([_line(t) for t in sorted(in_5m)])
                response += "\n\n"

            # 3. Портфель (trading_cycle) — по ним открываем позиции
            in_portfolio = [t for t in tickers if t in portfolio_trade_set]
            if in_portfolio:
                response += "📈 **Портфель (trading_cycle):**\n"
                response += "  _открываем позиции (MEDIUM/LONG)_\n"
                response += "\n".join([_line(t) for t in sorted(in_portfolio)])

            # Тикеры не ни в одной группе (только в quotes/конфиге)
            rest = [t for t in tickers if t not in indicator_set and t not in game5m_set and t not in portfolio_trade_set]
            if rest:
                response += "\n\n📋 **Прочие (в конфиге/quotes):**\n"
                response += "\n".join([_line(t) for t in sorted(rest)[:15]])
                if len(rest) > 15:
                    response += f"\n  ... и ещё {len(rest) - 15}"

            if len(tickers) > 30 and not rest:
                response += "\n\n... и еще " + _escape_markdown(str(len(tickers) - 30)) + " инструментов"

            legend = "Индикаторы — только для контекста; 5m — быстрая игра; Портфель — trading_cycle (открываем позиции)."
            response += "\n\n" + _escape_markdown(legend)

            await _send(response)

        except Exception as e:
            logger.error(f"Ошибка получения списка тикеров: {e}", exc_info=True)
            await _send(f"❌ Ошибка: {str(e)}")
    
    def _get_recommendation_data(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Собирает данные для рекомендации: сигнал, цена, риск-параметры, позиция по тикеру."""
        try:
            result = self.analyst.get_decision_with_llm(ticker)
            decision = result.get("decision", "HOLD")
            strategy = result.get("selected_strategy") or "—"
            technical = result.get("technical_data") or {}
            sentiment = result.get("sentiment_normalized") or result.get("sentiment") or 0.0
            if isinstance(sentiment, (int, float)) and 0 <= sentiment <= 1:
                sentiment = (sentiment - 0.5) * 2.0
            from sqlalchemy import create_engine, text
            from config_loader import get_database_url
            engine = create_engine(get_database_url())
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT close, rsi FROM quotes WHERE ticker = :ticker ORDER BY date DESC LIMIT 1"),
                    {"ticker": ticker},
                ).fetchone()
            price = float(row[0]) if row and row[0] is not None else None
            rsi = float(row[1]) if row and row[1] is not None else technical.get("rsi")
            try:
                from utils.risk_manager import get_risk_manager
                rm = get_risk_manager()
                stop_loss_pct = rm.get_stop_loss_percent()
                take_profit_pct = rm.get_take_profit_percent()
                max_pos_usd = rm.get_max_position_size(ticker)
                max_ticker_pct = rm.get_max_single_ticker_exposure()
            except Exception:
                stop_loss_pct = 5.0
                take_profit_pct = 10.0
                max_pos_usd = 10000.0
                max_ticker_pct = 20.0
            has_position = False
            position_info = None
            ex = self._get_execution_agent()
            if ex:
                summary = ex.get_portfolio_summary()
                for p in summary.get("positions") or []:
                    if p["ticker"] == ticker:
                        has_position = True
                        position_info = p
                        break
            return {
                "ticker": ticker,
                "decision": decision,
                "strategy": strategy,
                "price": price,
                "rsi": rsi,
                "sentiment": sentiment,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "max_position_usd": max_pos_usd,
                "max_ticker_pct": max_ticker_pct,
                "has_position": has_position,
                "position": position_info,
                "reasoning": result.get("reasoning", ""),
            }
        except Exception as e:
            logger.warning(f"Ошибка сбора рекомендации для {ticker}: {e}")
            return None

    def _format_recommendation(self, data: Dict[str, Any]) -> str:
        """Форматирует текст рекомендации по данным из _get_recommendation_data."""
        t = _escape_markdown(data["ticker"])
        decision = data["decision"]
        strategy = data["strategy"]
        price = data["price"]
        price_str = f"${price:.2f}" if price is not None else "—"
        rsi = data["rsi"]
        rsi_str = f"{rsi:.1f}" if rsi is not None else "—"
        sl = data["stop_loss_pct"]
        tp = data["take_profit_pct"]
        max_usd = data["max_position_usd"]
        max_pct = data["max_ticker_pct"]
        has_pos = data["has_position"]
        pos = data.get("position")
        if decision in ("BUY", "STRONG_BUY"):
            action = "можно открывать длинную позицию" if not has_pos else "позиция уже открыта — можно держать или докупать по своей тактике"
            emoji = "🟢"
        elif decision == "SELL":
            action = "рекомендуется закрыть или не открывать длинную позицию" if has_pos else "вход не рекомендую; можно рассмотреть короткую или ждать разворота"
            emoji = "🔴"
        else:
            action = "сигнал нейтральный — лучше подождать более чёткого сигнала перед входом"
            emoji = "⚪"
        lines = [
            f"{emoji} **Рекомендация по {t}**",
            "",
            f"**Сигнал:** {decision} (стратегия: {strategy})",
            f"**Цена:** {price_str}  ·  **RSI:** {rsi_str}",
            "",
            f"**Действие:** {action}",
            "",
            "**Параметры управления (песочница):**",
            f"• Стоп-лосс: −{sl:.0f}% от цены входа",
            f"• Тейк-профит (ориентир): +{tp:.0f}%",
            f"• Размер позиции: до ${max_usd:,.0f} или до {max_pct:.0f}% портфеля",
        ]
        if has_pos and pos:
            pnl = pos.get("pnl") or 0
            pnl_pct = pos.get("pnl_pct") or 0
            lines.append(f"\n_Текущая позиция: P&L ${pnl:,.2f} ({pnl_pct:+.2f}%)_")
        if data.get("reasoning"):
            lines.append(f"\n💭 _{_escape_markdown(str(data['reasoning'])[:180])}..._")
        return "\n".join(lines)

    def _build_recommendation_data_5m_from_d5(self, ticker: str, data_5m: Dict[str, Any]) -> Dict[str, Any]:
        """Собирает структуру для _format_recommendation_5m из готового выхода get_decision_5m (при кластерном запуске)."""
        has_position = False
        position_info = None
        ex = self._get_execution_agent()
        if ex:
            summary = ex.get_portfolio_summary()
            for p in summary.get("positions") or []:
                if p["ticker"] == ticker:
                    has_position = True
                    position_info = p
                    break
        alex_rule = None
        if ticker.upper() == "SNDK":
            try:
                from services.alex_rule import get_alex_rule_status
                alex_rule = get_alex_rule_status(ticker, data_5m.get("price"))
            except Exception:
                pass
        return {
            "ticker": ticker,
            "decision": data_5m.get("decision", "HOLD"),
            "strategy": "5m (интрадей + 5–7д статистика)",
            "price": data_5m.get("price"),
            "rsi": data_5m.get("rsi_5m"),
            "reasoning": data_5m.get("reasoning", ""),
            "period_str": data_5m.get("period_str", ""),
            "momentum_2h_pct": data_5m.get("momentum_2h_pct"),
            "volatility_5m_pct": data_5m.get("volatility_5m_pct"),
            "stop_loss_enabled": data_5m.get("stop_loss_enabled", True),
            "stop_loss_pct": data_5m.get("stop_loss_pct"),
            "take_profit_pct": data_5m.get("take_profit_pct", 5.0),
            "bars_count": data_5m.get("bars_count"),
            "has_position": has_position,
            "position": position_info,
            "alex_rule": alex_rule,
            "llm_insight": data_5m.get("llm_insight"),
            "llm_news_content": data_5m.get("llm_news_content"),
            "curvature_5m_pct": data_5m.get("curvature_5m_pct"),
            "possible_bounce_to_high_pct": data_5m.get("possible_bounce_to_high_pct"),
            "estimated_bounce_pct": data_5m.get("estimated_bounce_pct"),
            "session_high": data_5m.get("session_high"),
            "entry_advice": data_5m.get("entry_advice"),
            "entry_advice_reason": data_5m.get("entry_advice_reason"),
            "estimated_upside_pct_day": data_5m.get("estimated_upside_pct_day"),
            "suggested_take_profit_price": data_5m.get("suggested_take_profit_price"),
            "premarket_entry_recommendation": data_5m.get("premarket_entry_recommendation"),
            "premarket_suggested_limit_price": data_5m.get("premarket_suggested_limit_price"),
            "premarket_last": data_5m.get("premarket_last"),
            "premarket_gap_pct": data_5m.get("premarket_gap_pct"),
            "minutes_until_open": data_5m.get("minutes_until_open"),
            "max_position_usd": 0,
            "max_ticker_pct": 0,
        }

    def _get_recommendation_data_5m(self, ticker: str, days: int = 5) -> Optional[Dict[str, Any]]:
        """Собирает данные для рекомендации по 5m (свечи за 5–7 дн. + опционально LLM перед решением)."""
        try:
            from services.recommend_5m import get_decision_5m
            data_5m = get_decision_5m(ticker, days=days, use_llm_news=True)
            if not data_5m:
                return None
            return self._build_recommendation_data_5m_from_d5(ticker, data_5m)
        except Exception as e:
            logger.warning(f"Ошибка рекомендации 5m для {ticker}: {e}")
            return None

    def _format_recommendation_5m(self, data: Dict[str, Any]) -> str:
        """Форматирует текст рекомендации по 5m данным."""
        t = _escape_markdown(data["ticker"])
        decision = data["decision"]
        price = data["price"]
        price_str = f"${price:.2f}" if price is not None else "—"
        rsi = data.get("rsi")
        rsi_str = f"{rsi:.1f}" if rsi is not None else "—"
        stop_enabled = data.get("stop_loss_enabled", True)
        sl = data.get("stop_loss_pct") if stop_enabled else None
        tp = data.get("take_profit_pct", 5.0)
        period_str = data.get("period_str") or ""
        mom = data.get("momentum_2h_pct")
        mom_str = f"{mom:+.2f}%" if mom is not None else "—"
        vol = data.get("volatility_5m_pct")
        vol_str = f"{vol:.2f}%" if vol is not None else "—"
        has_pos = data.get("has_position", False)
        pos = data.get("position")
        if decision in ("BUY", "STRONG_BUY"):
            action = "можно открывать длинную позицию (по 5m)" if not has_pos else "позиция открыта — держать или докупать по тактике"
            emoji = "🟢"
        elif decision == "SELL":
            action = "рекомендуется закрыть или не входить" if has_pos else "вход не рекомендую по 5m"
            emoji = "🔴"
        else:
            action = "сигнал нейтральный — ждать более чёткого сигнала по 5m"
            emoji = "⚪"
        lines = [
            f"{emoji} **Рекомендация 5m по {t}**",
            "",
            f"**Сигнал:** {decision} (стратегия: 5m + 5д статистика)",
            f"**Цена:** {price_str}  ·  **RSI(5m):** {rsi_str}  ·  **Импульс 2ч:** {mom_str}  ·  **Волатильность 5m:** {vol_str}",
            "",
            f"**Период данных:** {period_str}" if period_str else "",
            "",
            f"**Действие:** {action}",
            "",
            "**Параметры (интрадей):**",
            (f"• Стоп-лосс: −{sl:.1f}%  ·  Тейк-профит: +{tp:.1f}%" if sl is not None else f"• Стоп: выкл.  ·  Тейк-профит: +{tp:.1f}%"),
        ]
        upside = data.get("estimated_upside_pct_day")
        take_price = data.get("suggested_take_profit_price")
        if upside is not None or take_price is not None:
            parts = []
            if upside is not None:
                parts.append(f"Оценка апсайда на день: +{upside:.1f}%")
            if take_price is not None:
                parts.append(f"Цель (close-ордер): ${take_price:.2f}")
            lines.append("• " + "  ·  ".join(parts))
        advice = data.get("entry_advice")
        advice_reason = data.get("entry_advice_reason")
        entry_rec = data.get("entry_price_recommended")
        entry_lo = data.get("entry_price_range_low")
        entry_hi = data.get("entry_price_range_high")
        exp_take = data.get("expected_profit_pct_if_take")
        if advice == "ALLOW":
            parts = []
            if entry_rec is not None:
                parts.append(f"реком. вход: ${float(entry_rec):.2f}")
            if entry_lo is not None and entry_hi is not None:
                parts.append(f"диапазон: ${float(entry_lo):.2f}–${float(entry_hi):.2f}")
            if exp_take is not None:
                parts.append(f"ожидаемая прибыль до тейка: +{float(exp_take):.2f}%")
            if parts:
                lines.append("")
                lines.append("✅ **План входа:** " + "  ·  ".join(parts))
        if advice in ("CAUTION", "AVOID") and advice_reason:
            lines.append("")
            lines.append(f"⚠️ **Вход:** {advice} — _{_escape_markdown(advice_reason)}_")
        pm_rec = data.get("premarket_entry_recommendation")
        if pm_rec:
            lines.append("")
            lines.append(f"📋 **Премаркет:** _{_escape_markdown(pm_rec[:200])}_")
        curv = data.get("curvature_5m_pct")
        bounce_to_high = data.get("possible_bounce_to_high_pct")
        est_bounce = data.get("estimated_bounce_pct")
        if curv is not None or bounce_to_high is not None:
            parts = []
            if curv is not None:
                parts.append(f"Кривизна 5m: {curv:+.3f}%" + (" (разворот вверх)" if curv > 0 else ""))
            if bounce_to_high is not None:
                parts.append(f"До хая сессии: +{bounce_to_high:.2f}%")
            if est_bounce is not None:
                parts.append(f"Оценка подъёма (по кривизне): ~+{est_bounce:.2f}%")
            lines.append("")
            lines.append("**График / возможный подъём:** " + "  ·  ".join(parts))
        if has_pos and pos:
            pnl = pos.get("pnl") or 0
            pnl_pct = pos.get("pnl_pct") or 0
            lines.append(f"\n_Позиция: P&L ${pnl:,.2f} ({pnl_pct:+.2f}%)_")
        if data.get("reasoning"):
            lines.append(f"\n💭 _{_escape_markdown(str(data['reasoning'])[:220])}_")
        llm_insight = data.get("llm_insight")
        llm_content = (data.get("llm_news_content") or "").strip()[:350]
        if llm_insight:
            lines.append("")
            lines.append(f"📰 **LLM (по обучению, не в реальном времени):** _{_escape_markdown(llm_insight)}_")
        elif llm_content:
            lines.append("")
            lines.append(f"📰 **LLM (по обучению):** _{_escape_markdown(llm_content)}…_")
        alex = data.get("alex_rule")
        if alex and alex.get("message"):
            lines.append("")
            lines.append(f"📋 _{_escape_markdown(alex['message'])}_")
        return "\n".join([s for s in lines if s])

    def _get_execution_agent(self):
        """Ленивая инициализация ExecutionAgent для песочницы."""
        if getattr(self, "_execution_agent", None) is None:
            try:
                from execution_agent import ExecutionAgent
                self._execution_agent = ExecutionAgent()
            except Exception as e:
                logger.warning(f"ExecutionAgent недоступен: {e}")
                self._execution_agent = False
        return self._execution_agent if self._execution_agent else None

    async def _handle_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Портфель: cash, позиции, текущая оценка и P&L."""
        if update.message is None:
            logger.warning("/portfolio: update.message is None")
            return
        try:
            user_id = (update.effective_user or update.message.from_user).id if (update.effective_user or update.message.from_user) else None
            if user_id is None:
                await update.message.reply_text("❌ Не удалось определить пользователя.")
                return
            if not self._check_access(user_id):
                await update.message.reply_text("❌ Доступ запрещен")
                return
            agent = self._get_execution_agent()
            if not agent:
                await update.message.reply_text("❌ Песочница недоступна (не инициализирован ExecutionAgent).")
                return
            summary = agent.get_portfolio_summary()
            cash = summary.get("cash", 0)
            total = summary.get("total_equity", cash)
            lines = [f"💵 **Кэш:** ${cash:,.2f}", f"📊 **Итого (оценка):** ${total:,.2f}"]
            ret = summary.get("total_return_pct")
            if ret is not None:
                initial = summary.get("initial_cash") or 0
                lines.append(f"📈 **Суммарная доходность:** {ret:+.2f}% (от нач. кэша ${initial:,.0f})")
            for p in summary.get("positions") or []:
                pnl_emoji = "🟢" if p.get("pnl", 0) >= 0 else "🔴"
                ticker = _escape_markdown(str(p.get("ticker", "?")))
                qty = p.get("quantity", 0)
                entry = p.get("entry_price", 0)
                curr = p.get("current_price", entry)
                pnl = p.get("pnl", 0)
                pnl_pct = p.get("pnl_pct", 0)
                lines.append(
                    f"\n{pnl_emoji} **{ticker}** — {qty:.0f} шт.\n"
                    f"  Вход: ${entry:.2f} → Сейчас: ${curr:.2f}\n"
                    f"  P&L: ${pnl:,.2f} ({pnl_pct:+.2f}%)"
                )
            if not summary.get("positions"):
                lines.append("\n_Позиций нет. /buy <ticker> <кол-во>_")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка портфеля: {e}", exc_info=True)
            try:
                await update.message.reply_text(f"❌ Ошибка портфеля: {str(e)[:400]}")
            except Exception:
                pass

    async def _handle_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Виртуальная покупка: /buy <ticker> <кол-во>."""
        user_id = update.effective_user.id
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        agent = self._get_execution_agent()
        if not agent:
            await update.message.reply_text("❌ Песочница недоступна.")
            return
        if not context.args or len(context.args) < 2:
            await update.message.reply_text(
                "❌ Формат: `/buy <ticker> <кол-во>`\nПример: `/buy GC=F 5` или `/buy MSFT 10`",
                parse_mode='Markdown',
            )
            return
        ticker = _normalize_ticker(context.args[0])
        try:
            qty = float(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ Укажите число в качестве количества.")
            return
        ok, msg = agent.execute_manual_buy(ticker, qty)
        await update.message.reply_text(msg if ok else f"❌ {msg}")

    async def _handle_sell(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Виртуальная продажа: /sell <ticker> [кол-во]. Без кол-ва — закрыть всю позицию."""
        user_id = update.effective_user.id
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        agent = self._get_execution_agent()
        if not agent:
            await update.message.reply_text("❌ Песочница недоступна.")
            return
        if not context.args or len(context.args) < 1:
            await update.message.reply_text(
                "❌ Формат: `/sell <ticker>` или `/sell <ticker> <кол-во>`\nПример: `/sell GC=F` или `/sell MSFT 5`",
                parse_mode='Markdown',
            )
            return
        ticker = _normalize_ticker(context.args[0])
        qty = None
        if len(context.args) >= 2:
            try:
                qty = float(context.args[1])
            except ValueError:
                await update.message.reply_text("❌ Укажите число в качестве количества.")
                return
        ok, msg = agent.execute_manual_sell(ticker, qty)
        await update.message.reply_text(msg if ok else f"❌ {msg}")

    async def _handle_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Последние сделки: /history [тикер] [N] — без аргументов все сделки; с тикером только по этому тикеру."""
        user_id = update.effective_user.id
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        agent = self._get_execution_agent()
        if not agent:
            await update.message.reply_text("❌ Песочница недоступна.")
            return
        limit = 15
        ticker = None
        args = (context.args or [])[:2]
        if args:
            first = args[0].strip().upper()
            try:
                n = int(first)
                limit = min(n, 50)
            except ValueError:
                ticker = _normalize_ticker(first)
                if len(args) >= 2:
                    try:
                        limit = min(int(args[1].strip()), 50)
                    except ValueError:
                        pass
        try:
            rows = agent.get_trade_history(limit=limit, ticker=ticker)
            if not rows:
                msg = "История сделок пуста." if not ticker else f"По тикеру {ticker} сделок нет."
                await update.message.reply_text(msg)
                return
            from services.game_5m import trade_ts_to_et
            # По фактическому PnL: выход в плюс → 🔵, в минус → 🔴 (не по signal_type)
            rows_asc = sorted(rows, key=lambda x: (x["ts"], x.get("ticker", "")))
            last_buy_price = {}
            for r in rows_asc:
                tkr = r.get("ticker", "")
                if r["side"] == "BUY":
                    last_buy_price[tkr] = float(r.get("price") or 0)
                elif r["side"] == "SELL":
                    entry = last_buy_price.get(tkr)
                    r["_is_profit"] = (entry is not None and float(r.get("price") or 0) >= entry)
            title = f"📜 **Последние сделки**" + (f" ({ticker})" if ticker else "") + ":"
            lines = [title]
            for r in rows:
                ts_raw = r["ts"]
                stored_tz = r.get("ts_timezone")
                ts_et = trade_ts_to_et(ts_raw, source_tz=stored_tz)
                if ts_et is not None and hasattr(ts_et, "strftime"):
                    ts = ts_et.strftime("%Y-%m-%d %H:%M") + " ET"
                elif hasattr(ts_raw, "strftime"):
                    ts = ts_raw.strftime("%Y-%m-%d %H:%M")
                else:
                    ts = str(ts_raw)
                if r["side"] == "BUY":
                    side = "🟢"
                else:
                    side = "🔵" if r.get("_is_profit") else "🔴"  # тейк / стоп по факту
                strat = r.get("strategy_name", "—")
                lines.append(f"{side} {ts} — {r['side']} {r['ticker']} x{r['quantity']:.0f} @ ${r['price']:.2f} ({r['signal_type']}) [{strat}]")
            if rows:
                lines.append("")
                lines.append("_🟢 Вход · 🔵 Выход в плюс · 🔴 Выход в минус_")
            if rows and ticker:
                lines.append(f"📈 _График:_ `/chart5m {ticker} 7` или `/chart {ticker} 7`")
            await update.message.reply_text("\n".join(lines), parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Ошибка history: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")

    async def _handle_premarket(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Таблица премаркета по тикерам (TICKERS_FAST + портфельная игра): Prev Close, Premarket, Gap %, Min to open, Last time ET. + HTML-файл."""
        if update.message is None:
            return
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is not None:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass
        user_id = (update.effective_user or (update.message.from_user if update.message else None))
        user_id = user_id.id if user_id else None
        if user_id is None:
            await update.message.reply_text("❌ Не удалось определить пользователя.")
            return
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        chart_ticker = None
        if context.args and len(context.args) >= 1:
            first = context.args[0].strip()
            try:
                int(first)
            except ValueError:
                chart_ticker = _normalize_ticker(first)
        try:
            from services.market_session import get_market_session_context
            from services.premarket import get_premarket_context, get_premarket_ohlc
            from services.ticker_groups import get_tickers_fast, get_tickers_for_portfolio_game

            ctx = get_market_session_context()
            phase = (ctx.get("session_phase") or "").strip()
            seen = set()
            tickers = []
            for t in get_tickers_fast() + (get_tickers_for_portfolio_game() or []):
                if t not in seen:
                    seen.add(t)
                    tickers.append(t)
            if chart_ticker and chart_ticker not in seen:
                tickers.append(chart_ticker)
            if not tickers:
                await update.message.reply_text("📊 Нет тикеров для премаркета (TICKERS_FAST / портфельная игра).")
                return
            rows_data: List[Dict[str, Any]] = []
            for ticker in tickers:
                pm = get_premarket_context(ticker)
                if pm.get("error"):
                    continue
                rows_data.append({
                    "ticker": ticker,
                    "prev_close": pm.get("prev_close"),
                    "premarket_last": pm.get("premarket_last"),
                    "premarket_gap_pct": pm.get("premarket_gap_pct"),
                    "minutes_until_open": pm.get("minutes_until_open"),
                    "premarket_last_time_et": pm.get("premarket_last_time_et"),
                })
            if not rows_data:
                await update.message.reply_text("📊 По выбранным тикерам нет данных премаркета (возможно, сейчас не премаркет или Yahoo не вернул данные).")
                return
            sep = "  "
            w_t = 10
            w_pc = 10
            w_pm = 10
            w_gap = 10
            w_min = 10
            w_time = 18

            def _cell(s: str, w: int) -> str:
                return str(s)[:w].ljust(w)

            header = (
                _cell("Ticker", w_t) + sep + _cell("PrevClose", w_pc) + sep + _cell("Premarket", w_pm) + sep
                + _cell("Gap %", w_gap) + sep + _cell("MinToOpen", w_min) + sep + "Last time (ET)"
            )
            lines_table = [header]
            for r in rows_data:
                prev = r.get("prev_close")
                prev_s = f"{prev:.2f}" if prev is not None else "—"
                last = r.get("premarket_last")
                last_s = f"{last:.2f}" if last is not None else "—"
                gap = r.get("premarket_gap_pct")
                gap_s = f"{gap:+.2f}%" if gap is not None else "—"
                mins = r.get("minutes_until_open")
                mins_s = f"{mins}" if mins is not None else "—"
                time_et = (r.get("premarket_last_time_et") or "—")[:18]
                lines_table.append(
                    _cell(str(r.get("ticker", "—")), w_t) + sep + _cell(prev_s, w_pc) + sep + _cell(last_s, w_pm) + sep
                    + _cell(gap_s, w_gap) + sep + _cell(mins_s, w_min) + sep + time_et
                )
            table = "\n".join(lines_table)
            phase_display = (phase or "").replace("_", " ")
            phase_note = f" (сейчас: {phase_display})" if phase_display else ""
            text_msg = (
                f"📊 **Премаркет**{phase_note}\n"
                "Цена — последняя минута Yahoo (prepost). Открытие US 9:30 ET.\n\n"
                f"```\n{table}\n```\n\n"
                "📎 _HTML‑отчёт — в документе ниже (откройте в браузере)._"
            )
            await update.message.reply_text(text_msg, parse_mode="Markdown")
            html_content = _build_premarket_html(rows_data)
            filename = _unique_report_filename("Премаркет")
            try:
                await update.message.reply_document(
                    document=BytesIO(html_content.encode("utf-8")),
                    filename=filename,
                    caption="📊 Премаркет (таблица выше). Откройте этот файл в браузере для удобного просмотра.",
                )
            except Exception as doc_e:
                logger.warning(f"Не удалось отправить HTML-файл premarket: {doc_e}")
            if chart_ticker:
                await update.message.reply_text(f"📥 Загрузка графика премаркета 1m для {chart_ticker}…")
                loop = asyncio.get_event_loop()
                df_prem = await loop.run_in_executor(None, lambda: get_premarket_ohlc(chart_ticker))
                if df_prem is not None and not df_prem.empty and "Close" in df_prem.columns:
                    pm_ctx = await loop.run_in_executor(None, lambda: get_premarket_context(chart_ticker))
                    prev_close = pm_ctx.get("prev_close") if pm_ctx else None
                    try:
                        import pandas as pd
                        import matplotlib
                        matplotlib.use("Agg")
                        import matplotlib.pyplot as plt
                        import matplotlib.dates as mdates
                        dt_col = "Datetime" if "Datetime" in df_prem.columns else "Date"
                        df_prem = df_prem.copy()
                        df_prem["datetime"] = pd.to_datetime(df_prem[dt_col])
                        if hasattr(df_prem["datetime"].dtype, "tz") and df_prem["datetime"].dtype.tz is not None:
                            dt_plot = df_prem["datetime"].dt.tz_convert("America/New_York").dt.tz_localize(None)
                        else:
                            d = df_prem["datetime"]
                            try:
                                d = d.dt.tz_localize("America/New_York", ambiguous=True)
                            except Exception:
                                d = d.dt.tz_localize("UTC", ambiguous=True).dt.tz_convert("America/New_York")
                            dt_plot = d.dt.tz_localize(None)
                        fig, ax = plt.subplots(figsize=(10, 5), facecolor="white")
                        ax.set_facecolor("#ffffff")
                        ax.plot(dt_plot, df_prem["Close"], color="#1565c0", linewidth=1.2, label="Premarket 1m")
                        if "High" in df_prem.columns and "Low" in df_prem.columns:
                            ax.fill_between(dt_plot, df_prem["Low"], df_prem["High"], alpha=0.15, color="#1565c0")
                        if prev_close is not None:
                            ax.axhline(prev_close, color="#757575", linestyle="--", linewidth=1, alpha=0.8, label=f"Вчера close {prev_close:.2f}")
                        ax.set_ylabel("Цена", fontsize=10)
                        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
                        ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=12))
                        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")
                        ax.set_title(f"{chart_ticker} — премаркет 1m (сегодня, Yahoo prepost)", fontsize=10, fontweight="bold")
                        ax.legend(loc="upper left", fontsize=8)
                        ax.grid(True, linestyle="--", alpha=0.4)
                        plt.tight_layout()
                        buf = BytesIO()
                        plt.savefig(buf, format="png", dpi=72, bbox_inches="tight", facecolor="white")
                        buf.seek(0)
                        plt.close()
                        await update.message.reply_photo(photo=buf, caption=f"📈 {chart_ticker} — премаркет 1m, {len(df_prem)} точек.")
                    except Exception as chart_e:
                        logger.exception("Ошибка графика премаркета")
                        await update.message.reply_text(f"❌ Ошибка графика: {str(chart_e)[:200]}")
                else:
                    await update.message.reply_text(f"❌ Нет данных 1m премаркета для {chart_ticker}. Попробуйте в часы премаркета US.")
        except Exception as e:
            logger.error(f"Ошибка premarket: {e}", exc_info=True)
            try:
                await update.message.reply_text(f"❌ Ошибка: {str(e)[:400]}")
            except Exception:
                pass

    async def _handle_corr(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Корреляции по кластеру портфеля (как в промпте портфельной игры)."""
        if update.message is None:
            return
        from services.ticker_groups import get_tickers_for_portfolio_game
        tickers = get_tickers_for_portfolio_game()
        if not tickers:
            await update.message.reply_text(
                "❌ Тикеры не заданы (TRADING_CYCLE_TICKERS или TICKERS_MEDIUM/TICKERS_LONG в config.env)."
            )
            return
        await self._run_corr_reply(update, context, tickers, days=60, cluster_label="портфель (медленные/средние игры)")

    async def _handle_corr5m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Корреляции по кластеру игры 5m (как в промпте 5m)."""
        if update.message is None:
            return
        from services.ticker_groups import get_tickers_game_5m
        tickers = get_tickers_game_5m()
        if not tickers:
            await update.message.reply_text(
                "❌ Тикеры не заданы (GAME_5M_TICKERS или TICKERS_FAST в config.env)."
            )
            return
        await self._run_corr_reply(update, context, tickers, days=60, cluster_label="5m")

    async def _run_corr_reply(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        base_tickers: list,
        days: int = 60,
        cluster_label: str = "",
    ):
        """Общая логика: корреляции лог-доходностей по списку тикеров. Без аргументов — матрица; /corr T1 [T2] — строка или пара."""
        if update.message is None:
            return
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is not None:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass
        user_id = (update.effective_user or (update.message.from_user if update.message else None))
        user_id = user_id.id if user_id else None
        if user_id is None:
            await update.message.reply_text("❌ Не удалось определить пользователя.")
            return
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        raw_args = (context.args or [])[:3]
        args = [str(a).strip() for a in raw_args if a is not None and str(a).strip()]
        ticker1 = _normalize_ticker(args[0]) if len(args) >= 1 else None
        ticker2 = _normalize_ticker(args[1]) if len(args) >= 2 else None
        all_tickers = list(base_tickers)
        if ticker1 and ticker1 not in all_tickers:
            all_tickers.append(ticker1)
        if ticker2 and ticker2 not in all_tickers:
            all_tickers.append(ticker2)
        if len(all_tickers) < 2:
            await update.message.reply_text(
                f"❌ Для расчёта корреляции нужно минимум 2 тикера. "
                f"В кластере «{cluster_label}» задан один тикер — добавьте тикеры в config или укажите: /corr T1 T2."
            )
            return
        try:
            import numpy as np
            import pandas as pd
            from report_generator import get_engine
            from services.cluster_manager import ClusterManager

            engine = get_engine()
            cm = ClusterManager(engine)
            # Портфель (много тикеров): запрашиваем больше истории (до 252 дн.), чтобы после thresh осталось достаточно строк
            max_days_load = max(days + 30, 252) if len(all_tickers) > 4 else days + 30
            prices = cm.get_price_data_with_fallback(
                all_tickers, max_days=max_days_load, for_correlation=True
            )
            source_note = ""
            if prices is None or prices.shape[0] < 5:
                await update.message.reply_text(
                    "❌ Недостаточно общих дней по тикерам (в БД и yfinance). "
                    "Проверьте тикеры в config и наличие котировок."
                )
                return
            if prices.shape[1] < 2:
                await update.message.reply_text("❌ После выравнивания дат осталось меньше 2 тикеров — корреляцию не посчитать.")
                return
            prices = prices.tail(min(days * 2, prices.shape[0]))  # окно до 2*days, чтобы после лог-доходностей хватило
            if prices.shape[0] < 5:
                await update.message.reply_text("❌ Меньше 5 дней в окне — увеличьте объём данных или проверьте тикеры.")
                return
            # Не dropna(how="any"): при портфеле в prices есть NaN (thresh), иначе теряем все строки; corr() считает по парам
            log_returns = np.log(prices / prices.shift(1)).replace([np.inf, -np.inf], np.nan)
            log_returns = log_returns.dropna(how="all").dropna(axis=1, how="all")  # убрать только полностью пустые строки/столбцы
            if log_returns.shape[0] < 5 or log_returns.shape[1] < 2:
                await update.message.reply_text("❌ Недостаточно данных для лог-доходностей после выравнивания.")
                return
            corr = log_returns.corr()
            if not hasattr(corr, "columns") or not hasattr(corr, "loc"):
                await update.message.reply_text("❌ Ошибка расчёта матрицы корреляций.")
                return
            n_days = len(log_returns)
            cluster_note = f" Кластер: {cluster_label}." if cluster_label else ""

            def _cell(s: str, w: int) -> str:
                return str(s)[:w].ljust(w)

            def _corr_fmt(v):
                if v is None or (isinstance(v, float) and (v != v or v == float("nan"))):
                    return "—"
                try:
                    return f"{float(v):.3f}"
                except (TypeError, ValueError):
                    return "—"

            if ticker1 and ticker2:
                t1, t2 = ticker1, ticker2
                if t1 not in corr.columns or t2 not in corr.columns:
                    await update.message.reply_text(f"❌ Тикер не найден в данных: {t1} или {t2}.")
                    return
                val = corr.loc[t1, t2]
                if np.isnan(val) if hasattr(np, "isnan") else (val != val):
                    await update.message.reply_text(
                        f"❌ По паре ({t1}, {t2}) недостаточно общих дней для корреляции."
                    )
                    return
                await update.message.reply_text(
                    f"📊 **Корреляция лог-доходностей** ({n_days} дн.){source_note}{cluster_note}\n\n"
                    f"Corr({_escape_markdown(t1)}, {_escape_markdown(t2)}) = **{float(val):.3f}**",
                    parse_mode="Markdown",
                )
                return
            if ticker1:
                if ticker1 not in corr.columns:
                    await update.message.reply_text(f"❌ Тикер {ticker1} не найден в данных.")
                    return
                row = corr.loc[ticker1].sort_values(ascending=False, na_position="last")
                sep = "  "
                w_t = 12
                w_c = 8
                header = _cell("Ticker", w_t) + sep + _cell("Corr", w_c)
                lines = [header]
                for t, v in row.items():
                    if t != ticker1:
                        lines.append(_cell(t, w_t) + sep + _corr_fmt(v))
                table = "\n".join(lines)
                msg = (
                    f"📊 **Корреляции с {_escape_markdown(ticker1)}** ({n_days} дн.){source_note}{cluster_note}\n\n"
                    f"```\n{table}\n```"
                )
                await update.message.reply_text(msg, parse_mode="Markdown")
                return
            cols = list(corr.columns)
            sep = "  "
            w = 7
            header = _cell("", w) + sep + sep.join(_cell(c, w) for c in cols)
            lines = [header]
            for r in cols:
                cells = [_cell(r, w)] + [(_corr_fmt(corr.loc[r, c])[:w]).ljust(w) for c in cols]
                lines.append(sep.join(cells))
            table = "\n".join(lines)
            if len(table) > 3800:
                table = "\n".join(lines[:15]) + "\n... (обрезано, используйте /corr T1 или /corr T1 T2)"
            title = f"Корреляции лог-доходностей ({cluster_label})" if cluster_label else "Корреляции лог-доходностей"
            html_content = _build_corr_html(corr, n_days, title)
            filename = _unique_report_filename("Корреляции")
            caption = f"📊 {title} ({n_days} дн., {len(cols)} тикеров). Откройте файл в браузере."
            try:
                await update.message.reply_document(
                    document=BytesIO(html_content.encode("utf-8")),
                    filename=filename,
                    caption=caption,
                )
            except Exception as doc_e:
                logger.warning("Не удалось отправить HTML-файл corr: %s", doc_e)
                msg = (
                    f"📊 **Корреляция лог-доходностей** ({n_days} дн., {len(cols)} тикеров){source_note}{cluster_note}\n\n"
                    f"```\n{table}\n```"
                )
                await update.message.reply_text(msg, parse_mode="Markdown")
        except Exception as e:
            logger.error("Ошибка corr: %s", e, exc_info=True)
            try:
                await update.message.reply_text(f"❌ Ошибка: {str(e)[:300]}")
            except Exception:
                pass

    async def _handle_closed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Таблица закрытых позиций. /closed [тикер] [N] — фильтр по тикеру, затем лимит (см. TELEGRAM_CLOSED_REPORT_*)."""
        if update.message is None:
            return
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is not None:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass
        user_id = (update.effective_user or (update.message.from_user if update.message else None))
        user_id = user_id.id if user_id else None
        if user_id is None:
            await update.message.reply_text("❌ Не удалось определить пользователя.")
            return
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        default_lim, max_lim = _telegram_closed_report_limits()
        limit = default_lim
        ticker_filter = None
        if context.args:
            a0 = str(context.args[0]).strip()
            a1 = str(context.args[1]).strip() if len(context.args) > 1 else None
            if a0.isdigit():
                limit = min(int(a0), max_lim)
                if a1 and not a1.isdigit():
                    ticker_filter = _normalize_ticker(a1)
            else:
                ticker_filter = _normalize_ticker(a0)
                if a1 and a1.isdigit():
                    limit = min(int(a1), max_lim)
        try:
            import pandas as pd
            from report_generator import get_engine, load_trade_history, compute_closed_trade_pnls

            engine = get_engine()
            trades = load_trade_history(engine)
            closed = compute_closed_trade_pnls(trades)
            if ticker_filter:
                closed = [t for t in closed if t.ticker == ticker_filter]
            if not closed:
                await update.message.reply_text(
                    f"📋 Закрытых позиций по тикеру {ticker_filter} нет." if ticker_filter else "📋 Закрытых позиций пока нет."
                )
                return
            closed = sorted(closed, key=lambda t: t.ts, reverse=True)[:limit]

            def _fmt_ts_msk(ts) -> str:
                if ts is None:
                    return "—"
                try:
                    t = pd.Timestamp(ts)
                    if t.tzinfo is not None:
                        t = t.tz_convert("Europe/Moscow")
                    # наивное время считаем уже MSK (как в БД)
                    return t.strftime("%d.%m.%Y %H:%M")
                except Exception:
                    return str(ts)[:16] if ts else "—"

            # Колонки с выравниванием; стратегия: открытие (Entry) / закрытие (Exit), чтобы не путать
            sep = "  "
            w_inst = 10
            w_dir = 6
            w_open = 8
            w_close = 8
            w_pips = 8
            w_profit = 10
            w_strat = 8
            w_date = 16

            def _cell(s: str, w: int) -> str:
                return str(s)[:w].ljust(w)

            w_qty = 5
            w_pct = 8
            w_reason = 12
            header = (
                _cell("Instrument", w_inst) + sep + _cell("Dir", w_dir) + sep
                + _cell("Open", w_open) + sep + _cell("Close", w_close) + sep
                + _cell("Pips", w_pips) + sep + _cell("Qty", w_qty) + sep + _cell("Profit", w_profit) + sep
                + _cell("P/L %", w_pct) + sep
                + _cell("Entry", w_strat) + sep + _cell("Exit", w_strat) + sep + _cell("Причина", w_reason) + sep
                + _cell("Open (MSK)", w_date) + sep + "Close (MSK)"
            )
            rows = [header]
            for t in closed:
                direction = "Long" if t.side == "SELL" else "Short"
                pts = t.exit_price - t.entry_price
                if "=X" in t.ticker or "USD" in t.ticker or "EUR" in t.ticker:
                    try:
                        pips_val = round(pts * 10000) if abs(pts) < 1 else round(pts, 2)
                    except Exception:
                        pips_val = round(pts, 2)
                else:
                    pips_val = round(pts, 2)
                qty_val = getattr(t, "quantity", None)
                qty_str = f"{int(qty_val)}" if qty_val is not None and float(qty_val) == int(float(qty_val)) else f"{float(qty_val):.2f}" if qty_val is not None else "—"
                pnl_pct = (t.exit_price - t.entry_price) / t.entry_price * 100.0 if t.entry_price and t.entry_price > 0 else 0.0
                pct_str = f"{pnl_pct:+.2f}%"
                entry_s = getattr(t, "entry_strategy", None) or "—"
                exit_s = getattr(t, "exit_strategy", None) or "—"
                reason_s = getattr(t, "signal_type", None) or "—"
                row = (
                    _cell(str(t.ticker), w_inst) + sep + _cell(direction, w_dir) + sep
                    + _cell(f"{t.entry_price:.2f}", w_open) + sep + _cell(f"{t.exit_price:.2f}", w_close) + sep
                    + _cell(str(pips_val), w_pips) + sep + _cell(qty_str, w_qty) + sep + _cell(f"{t.net_pnl:+.2f}", w_profit) + sep
                    + _cell(pct_str, w_pct) + sep
                    + _cell(entry_s, w_strat) + sep + _cell(exit_s, w_strat) + sep + _cell(reason_s, w_reason) + sep
                    + _cell(_fmt_ts_msk(t.entry_ts), w_date) + sep + _fmt_ts_msk(t.ts)
                )
                rows.append(row)
            total_pnl = sum(t.net_pnl for t in closed)
            ticker_note = f" по {ticker_filter}" if ticker_filter else ""
            footer_closed = f"\nИтого: {len(closed)} позиций, суммарный P/L: ${total_pnl:+,.2f}"
            table = "\n".join(rows) + footer_closed
            html_content = _build_closed_html(closed, total_pnl=total_pnl)
            filename = _unique_report_filename("Закрытые позиции")
            caption = (
                f"📋 Закрытые позиции{ticker_note} (последние {len(closed)}). "
                f"Итого: {len(closed)} позиций, суммарный P/L: ${total_pnl:+,.2f}. Откройте файл в браузере."
            )
            try:
                await update.message.reply_document(
                    document=BytesIO(html_content.encode("utf-8")),
                    filename=filename,
                    caption=caption,
                )
            except Exception as doc_e:
                logger.warning(f"Не удалось отправить HTML-файл closed: {doc_e}")
                await update.message.reply_text(
                    f"📋 Закрытые позиции{ticker_note} (последние {len(closed)})\nEntry/Exit — стратегия. Даты в MSK.\n\n```\n{table}\n```",
                    parse_mode="Markdown",
                )
        except Exception as e:
            logger.error(f"Ошибка closed: {e}", exc_info=True)
            try:
                await update.message.reply_text(f"❌ Ошибка: {str(e)[:400]}")
            except Exception:
                pass

    async def _handle_replay_closed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """HTML отчёт replay (TAKE-only) из local/replay_non_take_take_only.json.
        Использование: /replay_closed [путь_к_json]
        """
        if update.message is None:
            return
        user_id = (update.effective_user or update.message.from_user).id if (update.effective_user or getattr(update.message, "from_user", None)) else None
        if user_id is None or not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        rel = str(context.args[0]).strip() if context.args else "local/replay_non_take_take_only.json"
        p = Path(rel)
        if not p.is_absolute():
            p = project_root / p
        if not p.exists():
            await update.message.reply_text(
                f"❌ Файл не найден: {p}\nСначала выполните replay-скрипт в контейнере."
            )
            return
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            html_content = _build_replay_closed_html(payload)
            filename = _unique_report_filename("Replay_closed_take_only")
            stats = payload.get("stats") or {}
            caption = (
                "📋 Replay closed (TAKE-only): "
                f"alt closed={len(payload.get('closed') or [])}, "
                f"alt open={len(payload.get('open') or [])}, "
                f"ignored non-take={int(stats.get('ignored_non_take_sells', 0))}."
            )
            await update.message.reply_document(
                document=BytesIO(html_content.encode("utf-8")),
                filename=filename,
                caption=caption,
            )
        except Exception as e:
            logger.error("Ошибка replay_closed: %s", e, exc_info=True)
            await update.message.reply_text(f"❌ Ошибка replay_closed: {str(e)[:300]}")

    async def _handle_closed_impulse(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Закрытые 5m: по умолчанию импульс при входе >5%, без стоп-лоссов. Лимит N — TELEGRAM_CLOSED_REPORT_DEFAULT / MAX."""
        if update.message is None:
            return
        user_id = (update.effective_user or update.message.from_user).id if (update.effective_user or getattr(update.message, "from_user", None)) else None
        if user_id is None or not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        default_lim, max_lim = _telegram_closed_report_limits()
        limit = default_lim
        show_all = False  # all = показывать все закрытые 5m без фильтра по импульсу
        impulse_min = 5.0
        args = [str(a).strip() for a in (context.args or [])]
        if args and args[0].isdigit():
            limit = min(int(args[0]), max_lim)
            if len(args) > 1:
                if args[1].lower() in ("all", "*", "все", "всё"):
                    show_all = True
                else:
                    try:
                        impulse_min = float(args[1])
                    except ValueError:
                        pass
        elif args and args[0].lower() in ("all", "*", "все", "всё"):
            show_all = True
            if len(args) > 1 and args[1].isdigit():
                limit = min(int(args[1]), max_lim)
        elif args and not args[0].isdigit():
            try:
                impulse_min = float(args[0])
                if len(args) > 1 and args[1].isdigit():
                    limit = min(int(args[1]), max_lim)
            except ValueError:
                if args[0].lower() in ("all", "*", "все", "всё"):
                    show_all = True

        try:
            import pandas as pd
            from report_generator import get_engine, load_trade_history, compute_closed_trade_pnls, compute_open_positions, get_latest_prices

            engine = get_engine()
            trades_5m = load_trade_history(engine, strategy_name="GAME_5M")
            closed_all = compute_closed_trade_pnls(trades_5m)
            # Без стоп-лоссов в любом режиме
            closed_all = [t for t in closed_all if (getattr(t, "signal_type", "") or "").strip().upper() != "STOP_LOSS"]
            if show_all:
                closed = closed_all
            else:
                # Импульс при входе (entry_impulse_pct) >= порог; при отсутствии импульса не показываем (только при show_all)
                closed = [
                    t for t in closed_all
                    if getattr(t, "entry_impulse_pct", None) is not None and float(t.entry_impulse_pct) > impulse_min
                ]
            closed = sorted(closed, key=lambda x: x.ts, reverse=True)[:limit]
            open_5m = compute_open_positions(trades_5m)

            def _fmt_ts_msk(ts) -> str:
                if ts is None:
                    return "—"
                try:
                    t = pd.Timestamp(ts)
                    if t.tzinfo:
                        t = t.tz_convert("Europe/Moscow")
                    return t.strftime("%d.%m.%Y %H:%M")
                except Exception:
                    return str(ts)[:16] if ts else "—"

            sep = "  "
            w_inst, w_open, w_close, w_imp, w_qty, w_profit, w_pct, w_strat, w_date = 10, 8, 8, 9, 5, 10, 8, 8, 16
            def _cell(s: str, w: int) -> str:
                return str(s)[:w].ljust(w)
            w_reason = 12
            header = (
                _cell("Instrument", w_inst) + sep + _cell("Open", w_open) + sep + _cell("Close", w_close) + sep
                + _cell("Импульс%вх", w_imp) + sep + _cell("Qty", w_qty) + sep + _cell("Profit", w_profit) + sep + _cell("P/L %", w_pct) + sep
                + _cell("Причина", w_reason) + sep
                + _cell("Open (MSK)", w_date) + sep + "Close (MSK)"
            )
            rows = [header]
            for t in closed:
                impulse_at_entry = getattr(t, "entry_impulse_pct", None)
                impulse_str = f"{impulse_at_entry:+.2f}%" if impulse_at_entry is not None else "—"
                qty_val = getattr(t, "quantity", None)
                qty_str = f"{int(qty_val)}" if qty_val is not None and float(qty_val) == int(float(qty_val)) else f"{float(qty_val):.2f}" if qty_val is not None else "—"
                pnl_pct = (t.exit_price - t.entry_price) / t.entry_price * 100.0 if t.entry_price and t.entry_price > 0 else 0.0
                pct_str = f"{pnl_pct:+.2f}%"
                reason_s = getattr(t, "signal_type", "") or "—"
                row = (
                    _cell(t.ticker, w_inst) + sep + _cell(f"{t.entry_price:.2f}", w_open) + sep + _cell(f"{t.exit_price:.2f}", w_close) + sep
                    + _cell(impulse_str, w_imp) + sep + _cell(qty_str, w_qty) + sep + _cell(f"{t.net_pnl:+.2f}", w_profit) + sep + _cell(pct_str, w_pct) + sep
                    + _cell(reason_s, w_reason) + sep
                    + _cell(_fmt_ts_msk(t.entry_ts), w_date) + sep + _fmt_ts_msk(t.ts)
                )
                rows.append(row)
            total_pnl = sum(t.net_pnl for t in closed)
            if closed:
                rows.append("")
                rows.append(f"Итого: {len(closed)} позиций, суммарный P/L: ${total_pnl:+,.2f}")
            if open_5m:
                rows.append("")
                rows.append("--- Открытые 5m (ещё не закрыты) ---")
                latest = get_latest_prices(engine, [p.ticker for p in open_5m])
                for p in open_5m[:20]:
                    now_price = latest.get(p.ticker) or p.entry_price
                    pnl_pct = (now_price - p.entry_price) / p.entry_price * 100.0 if p.entry_price and p.entry_price > 0 else 0.0
                    pq = getattr(p, "quantity", None)
                    pqty = f"{int(pq)}" if pq is not None and float(pq) == int(float(pq)) else f"{float(pq):.2f}" if pq is not None else "—"
                    rows.append(
                        _cell(p.ticker, w_inst) + sep + _cell(f"{p.entry_price:.2f}", w_open) + sep + _cell(f"{now_price:.2f}", w_close) + sep
                        + _cell(f"{pnl_pct:+.2f}%", w_imp) + sep + _cell(pqty, w_qty) + sep + _cell("—", w_profit) + sep + _cell(f"{pnl_pct:+.2f}%", w_pct) + sep
                        + _cell("—", w_reason) + sep
                        + _cell(_fmt_ts_msk(p.entry_ts), w_date) + sep + "не закрыта"
                    )
            table = "\n".join(rows)
            html_content = _build_closed_html(closed, total_pnl=total_pnl, impulse_pct=True)
            if open_5m:
                html_content = html_content.replace("</body>", "<h2>Открытые 5m (ещё не закрыты)</h2><p>Ниже — позиции, по которым ещё не было выхода.</p></body>")
            filename = _unique_report_filename("Закрытые_импульс_5")
            if show_all:
                caption = f"📋 closed_impulse: все закрытые 5m без стоп-лоссов (последние {len(closed)}). Импульс при входе: если есть — в колонке, иначе —."
            else:
                caption = f"📋 closed_impulse: закрытые 5m с импульсом при входе >{impulse_min}%, без стоп-лоссов (последние {len(closed)})."
            if open_5m:
                caption += f" Открытые 5m: {len(open_5m)}."
            try:
                await update.message.reply_document(
                    document=BytesIO(html_content.encode("utf-8")),
                    filename=filename,
                    caption=caption,
                )
            except Exception as doc_e:
                logger.warning("closed_impulse: не удалось отправить HTML: %s", doc_e)
                mode_str = "все 5m" if show_all else f"импульс при входе >{impulse_min}%"
                await update.message.reply_text(
                    f"📋 closed_impulse ({mode_str}, без стоп-лоссов)\n\n```\n{table}\n```",
                    parse_mode="Markdown",
                )
        except Exception as e:
            logger.error("Ошибка closed_impulse: %s", e, exc_info=True)
            try:
                await update.message.reply_text(f"❌ Ошибка: {str(e)[:400]}")
            except Exception:
                pass

    async def _handle_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Таблица открытых позиций: Instrument, Open, Units, Strategy, Open (MSK). /pending [ticker] [N] — фильтр по тикеру."""
        user_id = update.effective_user.id
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        limit = 25
        ticker_filter = None
        if context.args:
            a0 = str(context.args[0]).strip()
            a1 = str(context.args[1]).strip() if len(context.args) > 1 else None
            if a0.isdigit():
                limit = min(int(a0), 50)
                if a1 and not a1.isdigit():
                    ticker_filter = _normalize_ticker(a1)
            else:
                ticker_filter = _normalize_ticker(a0)
                if a1 and a1.isdigit():
                    limit = min(int(a1), 50)
        try:
            import pandas as pd
            from report_generator import get_engine, load_trade_history, compute_open_positions, get_latest_prices
            from services.ticker_groups import get_tickers_game_5m, get_tickers_fast
            from services.recommend_5m import get_decision_5m

            engine = get_engine()
            trades = load_trade_history(engine)
            pending = compute_open_positions(trades)
            if ticker_filter:
                pending = [p for p in pending if _normalize_ticker(p.ticker) == ticker_filter]
            if not pending:
                await update.message.reply_text(
                    f"📋 Открытых позиций по тикеру {ticker_filter} нет." if ticker_filter else "📋 Открытых позиций нет."
                )
                return
            pending = pending[:limit]
            tickers_in_game_5m = set(get_tickers_game_5m())
            latest_prices = get_latest_prices(engine, [p.ticker for p in pending])
            # Для быстрых тикеров (SNDK, MU и т.д.) подставляем последний close 5m — актуальнее, чем дневной quotes
            fast_set = set(get_tickers_fast())
            for p in pending:
                if p.ticker in fast_set:
                    try:
                        d5 = get_decision_5m(p.ticker, use_llm_news=False)
                        if d5 and d5.get("price") is not None and d5["price"] > 0:
                            latest_prices[p.ticker] = float(d5["price"])
                    except Exception:
                        pass

            def _fmt_ts_msk(ts) -> str:
                if ts is None:
                    return "—"
                try:
                    t = pd.Timestamp(ts)
                    if t.tzinfo is not None:
                        t = t.tz_convert("Europe/Moscow")
                    return t.strftime("%d.%m.%Y %H:%M")
                except Exception:
                    return str(ts)[:16] if ts else "—"

            sep = "  "
            w_inst = 10
            w_dir = 6
            w_open = 8
            w_now = 8
            w_units = 6
            w_pl = 14
            w_strat = 10
            w_buys = 5
            w_date = 16

            def _cell(s: str, w: int) -> str:
                return str(s)[:w].ljust(w)

            header = (
                _cell("Instrument", w_inst) + sep + _cell("Dir", w_dir) + sep
                + _cell("Open", w_open) + sep + _cell("Now", w_now) + sep + _cell("Units", w_units) + sep
                + _cell("P/L", w_pl) + sep + _cell("Strategy", w_strat) + sep + _cell("BUYs", w_buys) + sep + "Open (MSK)"
            )
            rows = [header]
            for p in pending:
                strat = p.strategy_name or "—"
                if strat == "GAME_5M" and p.ticker not in tickers_in_game_5m:
                    strat = "5m вне"
                now_price = latest_prices.get(p.ticker)
                if now_price is not None and p.entry_price and p.entry_price > 0:
                    pct = (now_price - p.entry_price) / p.entry_price * 100.0
                    usd = (now_price - p.entry_price) * p.quantity
                    pl_str = f"{pct:+.1f}% {usd:+.0f}$"
                else:
                    pl_str = "—"
                    now_price = None
                now_str = f"{now_price:.2f}" if now_price is not None else "—"
                n_buys = int(getattr(p, "buy_leg_count", 1) or 1)
                row = (
                    _cell(str(p.ticker), w_inst) + sep + _cell("Long", w_dir) + sep
                    + _cell(f"{p.entry_price:.2f}", w_open) + sep + _cell(now_str, w_now) + sep
                    + _cell(str(int(p.quantity)), w_units) + sep + _cell(pl_str, w_pl) + sep
                    + _cell(strat, w_strat) + sep + _cell(str(n_buys), w_buys) + sep + _fmt_ts_msk(p.entry_ts)
                )
                rows.append(row)
            total_entry = sum(p.entry_price * p.quantity for p in pending if p.entry_price)
            total_now = sum(
                (latest_prices.get(p.ticker) or p.entry_price or 0) * p.quantity
                for p in pending
            )
            if total_entry and total_entry > 0:
                pnl_total = total_now - total_entry
                ret_pct = (total_now - total_entry) / total_entry * 100.0
                footer = f"\nИтого по позициям: вход ${total_entry:,.0f} → сейчас ${total_now:,.0f} | P/L: ${pnl_total:+,.0f} ({ret_pct:+.2f}%)"
            else:
                footer = ""
                pnl_total = ret_pct = 0.0
            table = "\n".join(rows) + footer
            html_content = _build_pending_html(
                pending, latest_prices, tickers_in_game_5m,
                total_entry=total_entry, total_now=total_now, pnl_total=pnl_total, ret_pct=ret_pct,
            )
            filename = _unique_report_filename("Открытые позиции")
            ticker_note_p = f" по {ticker_filter}" if ticker_filter else ""
            if total_entry and total_entry > 0:
                caption = (
                    f"📋 Открытые позиции{ticker_note_p} (показано {len(pending)}). "
                    f"Итого: вход ${total_entry:,.0f} → сейчас ${total_now:,.0f} | P/L: ${pnl_total:+,.0f} ({ret_pct:+.2f}%). Откройте файл в браузере."
                )
            else:
                caption = f"📋 Открытые позиции{ticker_note_p} (показано {len(pending)}). Откройте файл в браузере."
            try:
                await update.message.reply_document(
                    document=BytesIO(html_content.encode("utf-8")),
                    filename=filename,
                    caption=caption,
                )
            except Exception as doc_e:
                logger.warning(f"Не удалось отправить HTML-файл pending: {doc_e}")
                await update.message.reply_text(
                    "📋 **Открытые позиции{}** (показано {})\nNow и P/L — по 5m или quotes. Даты в MSK.\n\n```\n{}\n```".format(ticker_note_p, len(pending), table),
                    parse_mode="Markdown",
                )
        except Exception as e:
            logger.error(f"Ошибка pending: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")

    async def _handle_set_strategy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Переназначить стратегию у открытой позиции (для тикеров «вне игры»): /set_strategy TICKER STRATEGY."""
        user_id = update.effective_user.id
        if not self._check_access(user_id):
            await self._reply_to_update(update, context, "❌ Доступ запрещен")
            return
        if not context.args or len(context.args) < 2:
            await self._reply_to_update(
                update, context,
                "Укажите тикер и стратегию.\n"
                "Пример: `/set_strategy GC=F Manual` или `/set_strategy GC=F Geopolitical Bounce`\n\n"
                "Нужно для позиций «5m вне»: после переназначения в /pending будет новая стратегия.",
                parse_mode="Markdown",
            )
            return
        ticker = _normalize_ticker(context.args[0])
        strategy = (" ".join(context.args[1:]) or "Manual").strip().strip('"\'') or "Manual"
        agent = self._get_execution_agent()
        if not agent:
            await self._reply_to_update(update, context, "❌ Песочница недоступна.")
            return
        try:
            ok = agent.set_open_position_strategy(ticker, strategy)
            if ok:
                await self._reply_to_update(
                    update, context,
                    f"✅ Стратегия последнего BUY по **{ticker}** изменена на «{strategy}». "
                    "В `/pending` будет отображаться новая стратегия.",
                    parse_mode="Markdown",
                )
            else:
                await self._reply_to_update(
                    update, context,
                    f"По {ticker} не найден BUY в истории (нет открытой позиции по этому тикеру)."
                )
        except Exception as e:
            logger.exception("Ошибка set_strategy")
            await self._reply_to_update(update, context, f"❌ Ошибка: {str(e)}")

    async def _handle_pe_5m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Алиас: /pe_5m = /prompt_entry 5m — отчёт по кластеру игры 5m."""
        context.args = ["5m"]
        await self._handle_prompt_entry(update, context)

    async def _handle_prompt_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Шаблон принятия решения по игре. Аргумент: игра (portfolio, game5m) или тикер — контекст по параметрам и тикерам этой игры. Выгрузка: отчёт для человека; для портфеля в отчёте также промпт к LLM (system/user и ответ). Последний аргумент json — выгрузка в JSON."""
        user_id = update.effective_user.id
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        raw_args = list(context.args or [])
        output_json = any((a or "").strip().upper() == "JSON" for a in raw_args)
        if output_json:
            context.args = [a for a in raw_args if (a or "").strip().upper() != "JSON"]
        ticker_arg = (context.args or [])[0].strip() if context.args else ""
        game_arg = ticker_arg.strip().upper() if ticker_arg else ""
        is_portfolio_cluster = game_arg in ("PORTFOLIO", "ALL")
        is_game5m_cluster = game_arg in ("GAME5M", "GAME_5M", "5M")
        ticker = None if (is_portfolio_cluster or is_game5m_cluster) else (_normalize_ticker(ticker_arg) if ticker_arg else None)

        try:
            from services.llm_service import LLMService
            t = LLMService.get_entry_decision_prompt_template()

            if is_game5m_cluster:
                # Игра 5m: отчёт по кластеру (контекст + решение по правилам), не промпт к LLM
                await update.message.reply_text("📋 Формирую отчёт по кластеру 5m…")
                from services.ticker_groups import get_tickers_game_5m
                from services.cluster_recommend import (
                    load_game5m_llm_correlation,
                    GAME5M_LLM_CORRELATION_NOTE,
                    get_avg_volatility_20_pct_from_quotes,
                )
                from services.recommend_5m import get_decision_5m
                cluster_5m = list(get_tickers_game_5m() or [])
                if not cluster_5m:
                    await update.message.reply_text("Нет тикеров в игре 5m (GAME_5M_TICKERS / TICKERS_FAST).")
                    return
                corr_matrix, corr_tickers_used, _ = load_game5m_llm_correlation(days=30)
                correlation_note = GAME5M_LLM_CORRELATION_NOTE if corr_matrix else None
                # Тот же период, что и в /recommend5m по умолчанию (5 дн.), чтобы выводы совпадали
                days_5m = 5
                per_ticker_results: List[Dict[str, Any]] = []
                for tkr in cluster_5m:
                    d5 = get_decision_5m(tkr, days=days_5m, use_llm_news=True)
                    if not d5:
                        per_ticker_results.append({"ticker": tkr, "decision": "NO_DATA", "reasoning": "Нет 5m данных."})
                        continue
                    kb_news = d5.get("kb_news") or []
                    kb_summary = ""
                    if kb_news:
                        titles = [n.get("title") or n.get("content", "")[:80] for n in kb_news[:5]]
                        kb_summary = "; ".join(t[:100] + ("…" if len(t) > 100 else "") for t in titles if t)
                    per_ticker_results.append({
                        "ticker": tkr,
                        "decision": d5.get("decision"),
                        "reasoning": d5.get("reasoning"),
                        "price": d5.get("price"),
                        "rsi_5m": d5.get("rsi_5m"),
                        "momentum_2h_pct": d5.get("momentum_2h_pct"),
                        "price_forecast_5m": d5.get("price_forecast_5m"),
                        "price_forecast_5m_summary": d5.get("price_forecast_5m_summary"),
                        "volatility_5m_pct": d5.get("volatility_5m_pct"),
                        "stop_loss_pct": d5.get("stop_loss_pct"),
                        "take_profit_pct": d5.get("take_profit_pct"),
                        "stop_loss_enabled": d5.get("stop_loss_enabled", True),
                        "period_str": d5.get("period_str"),
                        "kb_news_impact": d5.get("kb_news_impact"),
                        "kb_news_summary": kb_summary,
                        "kb_news": kb_news,
                        "llm_news_content": d5.get("llm_news_content"),
                        "llm_sentiment": d5.get("llm_sentiment"),
                        "entry_advice": d5.get("entry_advice"),
                        "entry_advice_reason": d5.get("entry_advice_reason"),
                        "estimated_upside_pct_day": d5.get("estimated_upside_pct_day"),
                    })
                extra_tech: Dict[str, Dict[str, Any]] = {}
                if corr_tickers_used:
                    cluster_set = set(cluster_5m)
                    for t in corr_tickers_used:
                        if t in cluster_set:
                            continue
                        d5 = get_decision_5m(t, days=days_5m, use_llm_news=False)
                        if d5 and (d5.get("price") is not None or d5.get("rsi_5m") is not None):
                            extra_tech[t] = {"price": d5.get("price"), "rsi": d5.get("rsi_5m")}
                # Метрики tech+KB fusion — всегда в отчёте (HTML), не только при вызове LLM.
                from services.llm_service import build_entry_fusion_metrics

                for r in per_ticker_results:
                    if r.get("decision") == "NO_DATA":
                        continue
                    td_min: Dict[str, Any] = {
                        "technical_signal": r.get("decision"),
                        "momentum_2h_pct": r.get("momentum_2h_pct"),
                        "kb_news_days": days_5m,
                    }
                    kb_rows = list(r.get("kb_news") or [])
                    if kb_rows:
                        r["entry_fusion_metrics"] = build_entry_fusion_metrics(
                            r["ticker"], td_min, kb_rows, 0.5,
                        )
                    else:
                        imp = str(r.get("kb_news_impact") or "")
                        sent = 0.5
                        if "негатив" in imp:
                            sent = 0.35
                        elif "позитив" in imp:
                            sent = 0.65
                        r["entry_fusion_metrics"] = build_entry_fusion_metrics(
                            r["ticker"], td_min, [], sent,
                        )
                if get_use_llm_for_analyst() and corr_matrix and (corr_tickers_used or cluster_5m):
                    tech_by_ticker_5m = {r.get("ticker"): {"price": r.get("price"), "rsi": r.get("rsi_5m")} for r in per_ticker_results if r.get("ticker")}
                    tech_by_ticker_5m.update(extra_tech)
                    for r in per_ticker_results:
                        if r.get("decision") == "NO_DATA" or not cluster_5m:
                            continue
                        cluster_note = _build_cluster_note_for_5m_llm(
                            r["ticker"], cluster_5m, corr_matrix, tech_by_ticker_5m,
                        )
                        if not cluster_note:
                            continue
                        try:
                            from services.llm_service import get_llm_service
                            llm = get_llm_service()
                            if not getattr(llm, "client", None):
                                continue
                            technical_data = {
                                "close": r.get("price"),
                                "rsi": r.get("rsi_5m"),
                                "volatility_5": r.get("volatility_5m_pct"),
                                "avg_volatility_20": get_avg_volatility_20_pct_from_quotes(r["ticker"]),
                                "technical_signal": r.get("decision"),
                                "cluster_note": cluster_note,
                                "momentum_2h_pct": r.get("momentum_2h_pct"),
                                "take_profit_pct": r.get("take_profit_pct"),
                                "stop_loss_pct": r.get("stop_loss_pct"),
                                "estimated_upside_pct_day": r.get("estimated_upside_pct_day"),
                                "price_forecast_5m": r.get("price_forecast_5m"),
                                "price_forecast_5m_summary": r.get("price_forecast_5m_summary"),
                                "kb_news_days": days_5m,
                            }
                            kb_rows = list(r.get("kb_news") or [])
                            news_list = []
                            for n in kb_rows[:8]:
                                news_list.append({
                                    "source": (n.get("source") or "KB")[:80],
                                    "content": (n.get("content") or "")[:500],
                                    "sentiment_score": n.get("sentiment_score"),
                                })
                            fm = r.get("entry_fusion_metrics") or {}
                            sentiment = 0.5
                            if kb_rows:
                                sentiment = max(
                                    0.0,
                                    min(1.0, 0.5 + float(fm.get("news_bias_kb") or 0.0) / 2.0),
                                )
                            else:
                                imp = str(r.get("kb_news_impact") or "")
                                if "негатив" in imp:
                                    sentiment = 0.35
                                elif "позитив" in imp:
                                    sentiment = 0.65
                            technical_data["tech_bias_neg1"] = fm.get("tech_bias_neg1")
                            technical_data["rough_bias_kb"] = fm.get("rough_bias_kb")
                            technical_data["row_mean_bias_kb"] = fm.get("row_mean_bias_kb")
                            technical_data["news_bias_kb"] = fm.get("news_bias_kb")
                            technical_data["fused_bias_neg1"] = fm.get("fused_bias_neg1")
                            technical_data["regime_stress_kb"] = fm.get("regime_stress_kb")
                            technical_data["gate_mode_kb"] = fm.get("gate_mode_kb")
                            technical_data["gate_reason_kb"] = fm.get("gate_reason_kb")
                            technical_data["n_kb_rows"] = fm.get("n_kb_rows")
                            technical_data["draft_impulse_kb"] = fm.get("draft_impulse_kb")
                            technical_data["kb_reg_context"] = fm.get("kb_reg_context")
                            result = llm.analyze_trading_situation(
                                r["ticker"], technical_data, news_list, sentiment,
                                strategy_name="GAME_5M", strategy_signal=r.get("decision"),
                            )
                            if result and result.get("llm_analysis"):
                                ana = result["llm_analysis"]
                                r["llm_correlation_reasoning"] = ana.get("reasoning") or ""
                                r["llm_key_factors"] = ana.get("key_factors") or []
                                r["llm_decision_fused"] = ana.get("decision_fused")
                                r["llm_ab_fusion_differs"] = ana.get("ab_fusion_differs")
                                r["llm_reasoning_fused"] = ana.get("reasoning_fused") or ""
                        except Exception as e:
                            logger.debug("LLM с корреляцией для 5m %s: %s", r.get("ticker"), e)
                if output_json:
                    import json
                    def _json_serial(obj):
                        if hasattr(obj, "isoformat"):
                            return obj.isoformat()
                        if hasattr(obj, "item"):  # numpy scalar
                            return obj.item()
                        raise TypeError(type(obj).__name__)
                    tickers_payload = []
                    for r in per_ticker_results:
                        row = {k: v for k, v in r.items() if v is not None and k not in ("kb_news",)}
                        tickers_payload.append(row)
                    payload = {
                        "game": "5m",
                        "cluster": cluster_5m,
                        "correlation_note": correlation_note,
                        "days": days_5m,
                        "tickers": tickers_payload,
                    }
                    if corr_matrix:
                        payload["correlation_matrix"] = {k: {k2: float(v2) for k2, v2 in v.items()} for k, v in corr_matrix.items()}
                    json_bytes = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_serial).encode("utf-8")
                    filename = f"prompt_entry_game5m_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.json"
                    await update.message.reply_document(document=BytesIO(json_bytes), filename=filename)
                else:
                    html_content = _build_prompt_entry_game5m_html(
                        cluster_5m, correlation_note, per_ticker_results,
                        correlation_matrix=corr_matrix, correlation_tickers=corr_tickers_used if corr_matrix else None,
                        extra_tech_by_ticker=extra_tech if extra_tech else None,
                    )
                    filename = f"prompt_entry_game5m_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.html"
                    await update.message.reply_document(document=BytesIO(html_content.encode("utf-8")), filename=filename)
                decisions_str = ", ".join(f"{r['ticker']}={r.get('decision', '?')}" for r in per_ticker_results)
                await update.message.reply_text(f"✅ Прогноз для вступления (5m) выгружен. Решения: {decisions_str}")
                return

            if is_portfolio_cluster:
                # Портфель: общий кластерный контекст (MEDIUM+LONG), предикт по каждому тикеру (AnalystAgent), вход/выход — портфельные тейк/стоп
                await update.message.reply_text("📋 Формирую промпт по кластеру портфеля…")
                from services.ticker_groups import get_tickers_for_portfolio_game, get_tickers_indicator_only
                from services.cluster_recommend import get_correlation_matrix
                full_list = list(get_tickers_for_portfolio_game() or [])
                indicator_only = set(get_tickers_indicator_only())
                tickers_to_run = [x for x in full_list if x not in indicator_only]
                if not tickers_to_run:
                    await update.message.reply_text("Нет тикеров для анализа (портфель пуст или только индикаторы).")
                    return
                cluster_ctx = None
                if len(full_list) >= 2:
                    corr = get_correlation_matrix(full_list, days=30)
                    if corr:
                        cluster_ctx = {"tickers": full_list, "correlation": corr, "other_signals": {}}
                correlation_note = "Корреляция по кластеру за 30 дн. (контекст общий). Вход/выход — портфельная игра (тейк/стоп из стратегии или PORTFOLIO_*)."
                per_ticker_payloads: List[Dict[str, Any]] = []
                other_signals: Dict[str, str] = {}
                for tkr in tickers_to_run:
                    ctx = None
                    if cluster_ctx:
                        ctx = {**cluster_ctx, "other_signals": dict(other_signals)}
                    result = self.analyst.get_decision_with_llm(tkr, cluster_context=ctx)
                    dec = result.get("decision", "HOLD")
                    other_signals[tkr] = dec
                    per_ticker_payloads.append({
                        "ticker": tkr,
                        "user_prompt": result.get("prompt_user"),
                        "llm_response": result.get("llm_response_raw"),
                        "decision": dec,
                        "note": None,
                    })
                if output_json:
                    import json
                    payload = {
                        "game": "portfolio",
                        "cluster": full_list,
                        "correlation_note": correlation_note,
                        "tickers": per_ticker_payloads,
                    }
                    json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                    filename = f"prompt_entry_{'all' if game_arg == 'ALL' else 'portfolio'}_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.json"
                    await update.message.reply_document(document=BytesIO(json_bytes), filename=filename)
                else:
                    html_content = _build_prompt_entry_all_html(full_list, correlation_note, per_ticker_payloads)
                    filename = f"prompt_entry_{'all' if game_arg == 'ALL' else 'portfolio'}_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.html"
                    await update.message.reply_document(document=BytesIO(html_content.encode("utf-8")), filename=filename)
                decisions_str = ", ".join(f"{p['ticker']}={p.get('decision', '?')}" for p in per_ticker_payloads)
                await update.message.reply_text(f"✅ Промпт по кластеру портфеля выгружен. Предикт: {decisions_str}")
                return
            if not ticker:
                payload = {
                    "ticker": None,
                    "system_prompt": t["system"].strip(),
                    "user_template": t["user_template"].strip(),
                    "note": "Шаблон без подстановки. Укажите игру: portfolio, game5m — контекст по параметрам и тикерам этой игры; или тикер — контекст портфельной игры по этому тикеру. В выгрузке: отчёт для человека; для портфеля — также промпт к LLM.",
                }
            else:
                await update.message.reply_text(f"📋 Формирую промпт и запрос к LLM для {ticker}…")
                cluster_ctx = None
                try:
                    from services.ticker_groups import get_tickers_for_portfolio_game
                    from services.cluster_recommend import get_correlation_matrix
                    cluster_tickers = list(get_tickers_for_portfolio_game() or [])
                    if ticker not in cluster_tickers:
                        cluster_tickers = [ticker] + cluster_tickers
                    if len(cluster_tickers) >= 2:
                        corr = get_correlation_matrix(cluster_tickers, days=30)
                        if corr:
                            cluster_ctx = {"tickers": cluster_tickers, "correlation": corr, "other_signals": {}}
                except Exception:
                    cluster_ctx = None
                decision_result = self.analyst.get_decision_with_llm(ticker, cluster_context=cluster_ctx)
                payload = {
                    "ticker": ticker,
                    "system_prompt": decision_result.get("prompt_system") or t["system"].strip(),
                    "user_prompt": decision_result.get("prompt_user"),
                    "llm_response": decision_result.get("llm_response_raw"),
                    "llm_analysis": decision_result.get("llm_analysis"),
                    "decision": decision_result.get("decision"),
                    "technical_signal": decision_result.get("technical_signal"),
                }
                if decision_result.get("decision") == "NO_DATA":
                    payload["note"] = "Недостаточно данных по тикеру."
                elif not decision_result.get("prompt_user"):
                    payload["note"] = "Промпт не заполнен (нет данных для сборки)."
                elif not decision_result.get("llm_response_raw"):
                    payload["note"] = "Промпт заполнен по данным тикера; ответ LLM пуст (use_llm выключен или ошибка API)."

            html_content = _build_prompt_entry_html(payload)
            filename = f"prompt_entry_{ticker or 'template'}_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.html"
            await update.message.reply_document(
                document=BytesIO(html_content.encode("utf-8")),
                filename=filename,
            )
            if ticker:
                dec = payload.get("decision", "—")
                await update.message.reply_text(f"✅ Промпт и ответ LLM для **{ticker}** выгружены. Решение: {dec}", parse_mode="Markdown")
        except Exception as e:
            logger.exception("Ошибка prompt_entry")
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")

    async def _handle_strategies(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Описание стратегий (отображаются в /history, /pending, /closed)."""
        user_id = update.effective_user.id
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        text = """
📋 **Стратегии**

**Источники сделок (кто открыл/закрыл):**

• **GAME\_5M** — игра 5m: крон по тикерам из GAME\_5M\_TICKERS, интрадей (вход/выход по 5m, тейк/стоп). В /pending для тикеров, убранных из списка, показывается «5m вне» — крон по ним больше не управляет.

• **Portfolio** — портфельный цикл (trading\_cycle\_cron, ExecutionAgent). Сделки по сигналу AnalystAgent по списку MEDIUM/LONG тикеров. Если StrategyManager не вернул имя стратегии, в БД пишется «Portfolio». Стоп-лосс по таким позициям проверяется при каждом запуске крона — SELL выполняется автоматически при срабатывании.

• **Manual** — ручные команды `/buy` и `/sell` в боте.

**Стратегии из StrategyManager** (при портфельном цикле выбирается одна по режиму рынка):

• **Momentum** — низкая волатильность + положительный sentiment.
• **Mean Reversion** — высокая волатильность + нейтральный sentiment.
• **Volatile Gap** — очень высокая волатильность + гэп или экстремальный sentiment.
• **Geopolitical Bounce** — резкое падение предыдущей сессии (≥2%), отскок long.
• **Neutral** — fallback, когда ни одна стратегия не подошла; консервативный HOLD (режим не определён).

Переназначить стратегию у открытой позиции: `/set\_strategy <ticker> <стратегия>` (например для «5m вне» → Manual или Portfolio).
        """
        await update.message.reply_text(text.strip(), parse_mode="Markdown")

    async def _handle_recommend(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Рекомендация по портфелю: выгрузка в HTML (тот же формат, что prompt_entry portfolio). Без тикера — по кластеру."""
        user_id = update.effective_user.id
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        ticker = None
        if context.args and len(context.args) >= 1:
            ticker = _normalize_ticker(context.args[0])
        try:
            from services.llm_service import LLMService
            t = LLMService.get_entry_decision_prompt_template()
            if not ticker:
                await update.message.reply_text("🔍 Готовлю рекомендации по кластеру портфеля (HTML)...")
                from services.ticker_groups import get_tickers_for_portfolio_game, get_tickers_indicator_only
                from services.cluster_recommend import get_correlation_matrix
                full_list = list(get_tickers_for_portfolio_game() or [])
                indicator_only = set(get_tickers_indicator_only())
                tickers_to_run = [x for x in full_list if x not in indicator_only]
                if not tickers_to_run:
                    await update.message.reply_text("❌ Тикеры не заданы (TRADING_CYCLE_TICKERS или TICKERS_MEDIUM/TICKERS_LONG).")
                    return
                cluster_ctx = None
                if len(full_list) >= 2:
                    corr = get_correlation_matrix(full_list, days=30)
                    if corr:
                        cluster_ctx = {"tickers": full_list, "correlation": corr, "other_signals": {}}
                correlation_note = "Корреляция по кластеру за 30 дн. (контекст общий). Вход/выход — портфельная игра."
                per_ticker_payloads: List[Dict[str, Any]] = []
                other_signals: Dict[str, str] = {}
                for tkr in tickers_to_run:
                    ctx = {**cluster_ctx, "other_signals": dict(other_signals)} if cluster_ctx else None
                    result = self.analyst.get_decision_with_llm(tkr, cluster_context=ctx)
                    dec = result.get("decision", "HOLD")
                    other_signals[tkr] = dec
                    per_ticker_payloads.append({
                        "ticker": tkr,
                        "user_prompt": result.get("prompt_user"),
                        "llm_response": result.get("llm_response_raw"),
                        "decision": dec,
                        "note": None,
                    })
                html_content = _build_prompt_entry_all_html(full_list, correlation_note, per_ticker_payloads)
                filename = f"recommend_portfolio_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.html"
                await update.message.reply_document(document=BytesIO(html_content.encode("utf-8")), filename=filename)
                decisions_str = ", ".join(f"{p['ticker']}={p.get('decision', '?')}" for p in per_ticker_payloads)
                await update.message.reply_text(f"✅ Рекомендации по портфелю выгружены (HTML). Решения: {decisions_str}")
            else:
                await update.message.reply_text(f"🔍 Готовлю рекомендацию по {ticker} (HTML)...")
                from services.ticker_groups import get_tickers_for_portfolio_game
                from services.cluster_recommend import get_correlation_matrix
                cluster_tickers = list(get_tickers_for_portfolio_game() or [])
                if ticker not in cluster_tickers:
                    cluster_tickers = [ticker] + cluster_tickers
                cluster_ctx = None
                if len(cluster_tickers) >= 2:
                    corr = get_correlation_matrix(cluster_tickers, days=30)
                    if corr:
                        cluster_ctx = {"tickers": cluster_tickers, "correlation": corr, "other_signals": {}}
                decision_result = self.analyst.get_decision_with_llm(ticker, cluster_context=cluster_ctx)
                payload = {
                    "ticker": ticker,
                    "system_prompt": decision_result.get("prompt_system") or t["system"].strip(),
                    "user_prompt": decision_result.get("prompt_user"),
                    "llm_response": decision_result.get("llm_response_raw"),
                    "llm_analysis": decision_result.get("llm_analysis"),
                    "decision": decision_result.get("decision"),
                    "technical_signal": decision_result.get("technical_signal"),
                }
                if decision_result.get("decision") == "NO_DATA":
                    payload["note"] = "Недостаточно данных по тикеру."
                html_content = _build_prompt_entry_html(payload)
                filename = f"recommend_portfolio_{ticker}_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.html"
                await update.message.reply_document(document=BytesIO(html_content.encode("utf-8")), filename=filename)
                await update.message.reply_text(f"✅ Рекомендация по **{ticker}** выгружена (HTML). Решение: {payload.get('decision', '—')}", parse_mode="Markdown")
        except Exception as e:
            logger.exception("Ошибка рекомендации портфеля")
            await update.message.reply_text(f"❌ Ошибка: {e}")

    async def _handle_recommend5m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Прогноз для вступления в игру 5m: выгрузка в HTML (решение и параметры входа). Без тикера — по кластеру."""
        if not self._check_access(update.effective_user.id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        ticker = None
        days = 5
        if context.args and len(context.args) >= 1:
            ticker = _normalize_ticker(context.args[0])
        if len(context.args) >= 2:
            try:
                days = max(1, min(7, int(context.args[1].strip())))
            except (ValueError, IndexError):
                pass
        try:
            from services.ticker_groups import get_tickers_game_5m
            from services.cluster_recommend import (
                load_game5m_llm_correlation,
                GAME5M_LLM_CORRELATION_NOTE,
                get_avg_volatility_20_pct_from_quotes,
            )
            from services.recommend_5m import get_decision_5m, get_5m_card_payload
            cluster_5m = list(get_tickers_game_5m() or []) if not ticker else [ticker]
            if not cluster_5m:
                await update.message.reply_text("❌ Тикеры не заданы (GAME_5M_TICKERS или TICKERS_FAST).")
                return
            await update.message.reply_text("🔍 Готовлю рекомендации 5m (HTML)...")
            corr_matrix, corr_tickers_used, _ = load_game5m_llm_correlation(days=30)
            correlation_note = GAME5M_LLM_CORRELATION_NOTE if corr_matrix else None
            per_ticker_results: List[Dict[str, Any]] = []
            for tkr in cluster_5m:
                d5 = get_decision_5m(tkr, days=days, use_llm_news=True)
                item = get_5m_card_payload(d5, tkr)
                if d5:
                    kb_news = d5.get("kb_news") or []
                    kb_summary = ""
                    if kb_news:
                        titles = [n.get("title") or (n.get("content", "")[:80] if n.get("content") else "") for n in kb_news[:5]]
                        kb_summary = "; ".join((t[:100] + ("…" if len(t) > 100 else "")) for t in titles if t)
                    item["kb_news_summary"] = kb_summary
                    item["llm_news_content"] = d5.get("llm_news_content")
                    item["llm_sentiment"] = d5.get("llm_sentiment")
                per_ticker_results.append(item)
            extra_tech = {}
            if corr_tickers_used:
                cluster_set = set(cluster_5m)
                for t in corr_tickers_used:
                    if t in cluster_set:
                        continue
                    d5 = get_decision_5m(t, days=days, use_llm_news=False)
                    if d5 and (d5.get("price") is not None or d5.get("rsi_5m") is not None):
                        extra_tech[t] = {"price": d5.get("price"), "rsi": d5.get("rsi_5m")}
            if get_use_llm_for_analyst() and corr_matrix and (corr_tickers_used or cluster_5m):
                tech_by_ticker_5m = {r.get("ticker"): {"price": r.get("price"), "rsi": r.get("rsi_5m")} for r in per_ticker_results if r.get("ticker")}
                tech_by_ticker_5m.update(extra_tech)
                for r in per_ticker_results:
                    if r.get("decision") == "NO_DATA" or not cluster_5m:
                        continue
                    cluster_note = _build_cluster_note_for_5m_llm(
                        r["ticker"], cluster_5m, corr_matrix, tech_by_ticker_5m,
                    )
                    if not cluster_note:
                        continue
                    try:
                        from services.llm_service import get_llm_service
                        llm = get_llm_service()
                        if not getattr(llm, "client", None):
                            continue
                        technical_data = {
                            "close": r.get("price"),
                            "rsi": r.get("rsi_5m"),
                            "volatility_5": r.get("volatility_5m_pct"),
                            "avg_volatility_20": get_avg_volatility_20_pct_from_quotes(r["ticker"]),
                            "technical_signal": r.get("decision"),
                            "cluster_note": cluster_note,
                            "momentum_2h_pct": r.get("momentum_2h_pct"),
                            "take_profit_pct": r.get("take_profit_pct"),
                            "stop_loss_pct": r.get("stop_loss_pct"),
                            "estimated_upside_pct_day": r.get("estimated_upside_pct_day"),
                            "price_forecast_5m": r.get("price_forecast_5m"),
                            "price_forecast_5m_summary": r.get("price_forecast_5m_summary"),
                        }
                        news_list = []
                        if r.get("kb_news_summary") or r.get("kb_news_impact"):
                            news_list = [{"source": "KB", "content": (r.get("kb_news_summary") or r.get("kb_news_impact") or "")[:500], "sentiment_score": 0.5}]
                        sentiment = 0.5
                        if r.get("kb_news_impact") == "негативно":
                            sentiment = 0.35
                        elif r.get("kb_news_impact") == "позитивно":
                            sentiment = 0.65
                        result = llm.analyze_trading_situation(
                            r["ticker"], technical_data, news_list, sentiment,
                            strategy_name="GAME_5M", strategy_signal=r.get("decision"),
                        )
                        if result and result.get("llm_analysis"):
                            ana = result["llm_analysis"]
                            r["llm_correlation_reasoning"] = ana.get("reasoning") or ""
                            r["llm_key_factors"] = ana.get("key_factors") or []
                    except Exception as e:
                        logger.debug("LLM с корреляцией для 5m %s: %s", r.get("ticker"), e)
            html_content = _build_recommend5m_compact_html(per_ticker_results, days)
            filename = f"recommend_5m_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.html"
            await update.message.reply_document(document=BytesIO(html_content.encode("utf-8")), filename=filename)
            decisions_str = ", ".join(f"{r['ticker']}={r.get('decision', '?')}" for r in per_ticker_results)
            await update.message.reply_text(f"✅ Прогноз для вступления в игру 5m (HTML). Решения: {decisions_str}")
        except Exception as e:
            logger.exception("Ошибка рекомендации 5m")
            await update.message.reply_text(f"❌ Ошибка: {e}")

    async def _handle_signal5m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Технический сигнал 5m: тот же источник, что recommend5m и cron. Без LLM-нарратива."""
        if not self._check_access(update.effective_user.id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        from services.ticker_groups import get_tickers_game_5m
        from services.recommend_5m import get_5m_technical_signal

        ticker = None
        days = 5
        if context.args and len(context.args) >= 1:
            ticker = _normalize_ticker(context.args[0])
        if len(context.args) >= 2:
            try:
                days = max(1, min(7, int(context.args[1].strip())))
            except (ValueError, IndexError):
                pass
        cluster_5m = list(get_tickers_game_5m() or [])
        if not cluster_5m:
            await update.message.reply_text("❌ Тикеры игры 5m не заданы (GAME_5M_TICKERS / TICKERS_FAST).")
            return
        if ticker and ticker not in cluster_5m:
            await update.message.reply_text(f"❌ {ticker} не в игре 5m. Кластер: {', '.join(cluster_5m)}")
            return
        tickers_to_show = [ticker] if ticker else cluster_5m
        try:
            results = []
            for tkr in tickers_to_show:
                tech = get_5m_technical_signal(tkr, days=days, use_llm_news=False)
                if tech:
                    tech["ticker"] = tkr
                    results.append(tech)
                else:
                    results.append({"ticker": tkr, "decision": "NO_DATA"})
            if not results:
                await update.message.reply_text("❌ Нет 5m данных по выбранным тикерам.")
                return
            if len(results) == 1:
                text = self._format_5m_technical_signal(results[0]["ticker"], results[0])
                await update.message.reply_text(text, parse_mode=None)
            else:
                # Единый payload: все поля из get_5m_technical_signal (TECHNICAL_SIGNAL_KEYS)
                html_content = _build_recommend5m_compact_html(
                    [{"ticker": r.get("ticker"), **r} for r in results],
                    days,
                )
                filename = f"signal5m_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.html"
                await update.message.reply_document(document=BytesIO(html_content.encode("utf-8")), filename=filename)
                await update.message.reply_text("✅ Сигнал 5m (технический) выгружен.")
        except Exception as e:
            logger.exception("Ошибка signal5m")
            await update.message.reply_text(f"❌ Ошибка: {e}")

    async def _handle_game5m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Мониторинг игры 5m: открытая позиция, закрытые сделки, win rate и PnL (только просмотр, сделками управляет send_sndk_signal_cron)."""
        if not self._check_access(update.effective_user.id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        mode = (context.args[0].strip().lower() if context.args else "")
        # Новый режим: по /game5m [platform|sync|all] отправляем массив GAME_5M в Kerim /game,
        # затем возвращаем 3 HTML-отчёта: notOpened/opened/closed.
        if mode in ("platform", "sync", "all"):
            try:
                from services.platform_game_api import is_platform_game_enabled, post_game_positions
                from services.ticker_groups import get_tickers_game_5m
                from report_generator import get_engine
                from sqlalchemy import text
                import pandas as pd
            except Exception as e:
                await update.message.reply_text(f"❌ game5m/platform: import error: {e}")
                return

            if not is_platform_game_enabled():
                await update.message.reply_text(
                    "❌ PLATFORM_GAME_API_ENABLED=false. Включите интеграцию в config.env и перезапустите lse."
                )
                return

            tickers = list(get_tickers_game_5m() or [])
            if not tickers:
                await update.message.reply_text("❌ GAME_5M тикеры не заданы.")
                return

            positions: List[Dict[str, Any]] = []
            skipped: List[str] = []
            try:
                engine = get_engine()
                with engine.connect() as conn:
                    df_buy = pd.read_sql(
                        text(
                            """
                            SELECT id, ts, ticker, quantity, price, signal_type, strategy_name,
                                   take_profit, stop_loss, context_json
                            FROM trade_history
                            WHERE side='BUY' AND strategy_name='GAME_5M'
                            ORDER BY ts ASC, id ASC
                            """
                        ),
                        conn,
                    )
            except Exception as e:
                await update.message.reply_text(f"❌ Не удалось загрузить BUY-историю GAME_5M: {e}")
                return

            if df_buy is None or df_buy.empty:
                await update.message.reply_text("❌ В истории нет BUY-позиций GAME_5M.")
                return

            allowed = set(tickers)
            for _, row in df_buy.iterrows():
                tkr = str(row.get("ticker") or "").upper().strip()
                if not tkr or tkr not in allowed:
                    continue
                # quantity желателен, но при мусоре не пропускаем запись: fallback -> 1.
                try:
                    units = max(1, int(float(row.get("quantity") or 0)))
                except Exception:
                    units = 1
                # entry_price для Kerim не обязателен; нужен только если считаем тейк из pct.
                try:
                    entry_price = float(row.get("price"))
                    if entry_price <= 0:
                        entry_price = None
                except Exception:
                    entry_price = None

                # Исторический момент входа (как просили: параметры входа исторические).
                ts = row.get("ts")
                try:
                    t = pd.Timestamp(ts)
                    if t.tzinfo is None:
                        t = t.tz_localize("UTC")
                    else:
                        t = t.tz_convert("UTC")
                    created_at = t.strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    created_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

                ctx = row.get("context_json")
                if isinstance(ctx, str):
                    try:
                        ctx = json.loads(ctx)
                    except Exception:
                        ctx = {}
                if not isinstance(ctx, dict):
                    ctx = {}

                # В GAME_5M входы BUY — LONG; SHORT только если явно сохранено в контексте.
                direction = str(ctx.get("direction") or "LONG").upper()
                if direction not in ("LONG", "SHORT"):
                    direction = "LONG"

                # Исторический take price: сперва явная цена, затем pct от цены входа.
                take_price = ctx.get("suggested_take_profit_price")
                if take_price is None:
                    take_pct = None
                    for k in ("take_profit_pct", "estimated_upside_pct_day", "effective_take_profit_pct"):
                        v = ctx.get(k)
                        if isinstance(v, (int, float)):
                            take_pct = float(v)
                            break
                    if take_pct is None and row.get("take_profit") is not None:
                        try:
                            take_pct = float(row.get("take_profit"))
                        except Exception:
                            take_pct = None
                    if take_pct is not None and entry_price is not None:
                        take_price = entry_price * (1.0 + take_pct / 100.0) if direction == "LONG" else entry_price * (1.0 - take_pct / 100.0)
                try:
                    take_price_f = float(take_price)
                except Exception:
                    # Если тейк не восстановился из истории, используем безопасный широкий уровень.
                    take_price_f = 1_000_000.0 if direction == "LONG" else 0.01

                order_type = str(ctx.get("orderType") or ctx.get("order_type") or "MARKET").upper()
                if order_type not in ("MARKET", "LIMIT"):
                    order_type = "MARKET"
                if order_type == "LIMIT":
                    limit_in = ctx.get("limitIn")
                    try:
                        limit_in_f = float(limit_in)
                    except Exception:
                        # Нет исторического limitIn — деградация до MARKET, чтобы не ломать API.
                        order_type = "MARKET"
                        limit_in_f = None

                if order_type == "LIMIT":
                    positions.append(
                        {
                            "orderType": "LIMIT",
                            "limit": {
                                "instrument": tkr,
                                "direction": direction,
                                "createdAt": created_at,
                                "takeProfit": float(round(take_price_f, 4)),
                                "units": int(units),
                                "limitIn": float(round(limit_in_f, 4)),
                            },
                        }
                    )
                else:
                    positions.append(
                        {
                            "orderType": "MARKET",
                            "market": {
                                "instrument": tkr,
                                "direction": direction,
                                "createdAt": created_at,
                                "takeProfit": float(round(take_price_f, 4)),
                                "units": int(units),
                            },
                        }
                    )

            if not positions:
                await update.message.reply_text("❌ Нет валидных позиций для отправки в Platform /game.")
                return

            not_opened: List[Dict[str, Any]] = []
            opened: List[Dict[str, Any]] = []
            closed: List[Dict[str, Any]] = []
            errors: List[Dict[str, Any]] = []
            for pos in positions:
                tkr = ((pos.get("market") or {}).get("instrument") or "?")
                try:
                    one = post_game_positions([pos])
                    not_opened.extend(one.get("notOpened") or [])
                    opened.extend(one.get("opened") or [])
                    closed.extend(one.get("closed") or [])
                except Exception as e:
                    errors.append({
                        "instrument": tkr,
                        "error": str(e),
                        "payload": pos,
                    })

            if not_opened == [] and opened == [] and closed == [] and errors:
                first_err = errors[0]["error"]
                await update.message.reply_text(f"❌ Platform /game: {first_err}", parse_mode=None)
                payload_text = json.dumps({"positions": positions}, ensure_ascii=False, indent=2)
                payload_preview = payload_text[:3500] + ("\n... (truncated)" if len(payload_text) > 3500 else "")
                await update.message.reply_text("Request payload:\n" + payload_preview, parse_mode=None)
                return
            summary = (
                "📊 /game5m platform\n"
                f"Отправлено позиций: {len(positions)} (из тикеров {len(tickers)}), пропущено: {len(skipped)}\n"
                f"Ответ: pending={len(not_opened)}, opened={len(opened)}, closed={len(closed)}, errors={len(errors)}"
            )
            await update.message.reply_text(summary, parse_mode=None)

            sent_files = 0
            for key, title, items, filename in (
                ("notOpened", "Game API — pending", not_opened, "pending.html"),
                ("opened", "Game API — opened", opened, "opened.html"),
                ("closed", "Game API — closed", closed, "closed.html"),
            ):
                if not items:
                    continue
                html_content = _build_platform_game_status_html(key, title, items)
                await update.message.reply_document(
                    document=BytesIO(html_content.encode("utf-8")),
                    filename=filename,
                )
                sent_files += 1
            if sent_files == 0:
                await update.message.reply_text("ℹ️ Все статусы пустые — HTML-файлы не отправлены.", parse_mode=None)
            if errors:
                err_lines = ["Ошибки Platform /game по тикерам:"]
                for e in errors[:20]:
                    payload_json = json.dumps(e.get("payload") or {}, ensure_ascii=False)
                    err_lines.append(
                        "• {inst}: {err}\n"
                        "  json={payload}".format(
                            inst=e.get("instrument", "?"),
                            err=e.get("error", "unknown"),
                            payload=payload_json,
                        )
                    )
                msg = "\n".join(err_lines)
                if len(msg) <= 3900:
                    await update.message.reply_text(msg, parse_mode=None)
                else:
                    # Если ошибок/JSON много — отправим файлом.
                    filename = f"game5m_platform_errors_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.txt"
                    await update.message.reply_document(
                        document=BytesIO(msg.encode("utf-8")),
                        filename=filename,
                    )
            return

        ticker = "SNDK"
        if context.args and len(context.args) >= 1:
            ticker = _normalize_ticker(context.args[0])
        limit = 15
        if len(context.args) >= 2:
            try:
                limit = max(5, min(30, int(context.args[1].strip())))
            except (ValueError, IndexError):
                pass

        def _fetch_game5m():
            from services.game_5m import get_open_position, get_recent_results, get_strategy_params
            pos = get_open_position(ticker)
            results = get_recent_results(ticker, limit=limit)
            params = get_strategy_params()
            return pos, results, params

        loop = asyncio.get_event_loop()
        try:
            pos, results, params = await loop.run_in_executor(None, _fetch_game5m)
        except Exception as e:
            logger.exception("Ошибка загрузки игры 5m")
            await update.message.reply_text(f"❌ Ошибка: {e}")
            return

        lines = [f"📊 **Игра 5m — {_escape_markdown(ticker)}** (мониторинг)", ""]
        if params.get("stop_loss_enabled", True):
            lines.append(f"Параметры: стоп −{params['stop_loss_pct']}%, тейк +{params['take_profit_pct']}%, макс. {params['max_position_days']} дн. _(config.env)_")
        else:
            lines.append(f"Параметры: тейк +{params['take_profit_pct']}% (стоп выкл.), макс. {params['max_position_days']} дн. _(config.env)_")
        lines.append("")
        if pos:
            entry_ts = pos.get("entry_ts")
            ts_str = str(entry_ts)[:16] if entry_ts else "—"
            lines.append(f"🟢 **Открытая позиция**")
            lines.append(f"Вход: {ts_str} @ ${pos['entry_price']:.2f} · {pos['quantity']:.0f} шт. · сигнал {pos.get('entry_signal_type', '—')}")
            lines.append("")
        else:
            lines.append("_Нет открытой позиции_")
            lines.append("")

        if not results:
            lines.append("_Закрытых сделок пока нет._")
        else:
            pnls = [r["pnl_pct"] for r in results if r.get("pnl_pct") is not None]
            pnls_usd = [r["pnl_usd"] for r in results if r.get("pnl_usd") is not None]
            wins = sum(1 for p in pnls if p > 0)
            total = len(pnls)
            win_rate = (100.0 * wins / total) if total else 0
            avg_pnl = (sum(pnls) / total) if total else 0
            sum_usd = sum(pnls_usd) if pnls_usd else 0
            lines.append(f"**Закрытые сделки (последние {len(results)}):**")
            lines.append(f"Win rate: {wins}/{total} ({win_rate:.1f}%) · Средний PnL: {avg_pnl:+.2f}% · Сумма: ${sum_usd:+.2f}")
            lines.append("")
            for r in results[:8]:
                exit_ts = r.get("exit_ts") or "—"
                exit_str = str(exit_ts)[:16] if exit_ts != "—" else "—"
                pct = r.get("pnl_pct")
                pct_str = f"{pct:+.2f}%" if pct is not None else "—"
                usd = r.get("pnl_usd")
                usd_str = f" ${usd:+.2f}" if usd is not None else ""
                lines.append(f"• {exit_str} {r.get('exit_signal_type', '—')} PnL {pct_str}{usd_str}")
            if len(results) > 8:
                lines.append(f"_… и ещё {len(results) - 8} сделок_")
        text = "\n".join(lines)
        await update.message.reply_text(text, parse_mode="Markdown")

    async def _handle_gameparams(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать все существенные параметры для игры 5m и портфельной игры (config.env)."""
        if not self._check_access(update.effective_user.id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        try:
            import re
            from config_loader import get_database_url
            from services.game_5m import get_strategy_params, _take_profit_cap_pct, _max_position_days
            from services.ticker_groups import get_tickers_game_5m, get_tickers_for_portfolio_game
            params_5m = get_strategy_params()
            tickers_5m = get_tickers_game_5m()
            tickers_portfolio = get_tickers_for_portfolio_game() or []
            cooldown = get_config_value("GAME_5M_COOLDOWN_MINUTES", "120").strip()
            momentum_factor = get_config_value("GAME_5M_TAKE_MOMENTUM_FACTOR", "1.0").strip()
            max_atr_pct = get_config_value("GAME_5M_MAX_ATR_5M_PCT", "").strip()
            min_vol_pct = get_config_value("GAME_5M_MIN_VOLUME_VS_AVG_PCT", "").strip()
            portfolio_take = get_config_value("PORTFOLIO_TAKE_PROFIT_PCT", "0").strip() or "0"
            from config_loader import get_dynamic_config_value
            from report_generator import get_engine
            _engine = get_engine()
            stop_level = (get_dynamic_config_value("STOP_LOSS_LEVEL", "0.95", engine=_engine) or "0.95").strip()
            _sl_raw = (get_dynamic_config_value("PORTFOLIO_STOP_LOSS_ENABLED", "true", engine=_engine) or "true").strip().lower()
            stop_enabled_raw = _sl_raw in ("1", "true", "yes")
            stop_enabled_str = "вкл." if stop_enabled_raw else "выкл. (только тейк)"
        except Exception as e:
            logger.exception("Ошибка загрузки параметров игр")
            await update.message.reply_text(f"❌ Ошибка: {e}")
            return
        db_info = ""
        try:
            url = get_database_url()
            m = re.match(r"postgresql://[^@]+@([^:/]+)(?::(\d+))?/([^?]+)", url)
            if m:
                db_info = f"• БД (для сверки с крон-логом): host={m.group(1)} port={m.group(2) or '5432'} database={m.group(3)}"
        except Exception:
            pass
        lines = [
            "⚙️ Параметры игр (config.env)",
            "",
            "Игра 5m (send_sndk_signal_cron):",
            f"• Тикеры: {', '.join(tickers_5m) or '—'}",
            f"• Cooldown рассылки: {cooldown} мин",
            f"• Стоп 5m: {'−' + str(params_5m['stop_loss_pct']) + '% (мин. −' + str(params_5m['stop_loss_min_pct']) + '%)' if params_5m.get('stop_loss_enabled', True) else 'выкл. (только тейк)'}",
            f"• Тейк: +{params_5m['take_profit_pct']}% (базовый); мин. от импульса 2ч: +{params_5m['take_profit_min_pct']}%",
            (f"• Стоп/тейк ratio: {params_5m['stop_to_take_ratio']}" if params_5m.get('stop_loss_enabled', True) else "• Стоп 5m выкл. — в ответах не рекомендуй стоп по 5m"),
            f"• Фактор тейка от импульса: {momentum_factor}",
            f"• Макс. дней в позиции: {params_5m['max_position_days']}",
        ]
        if max_atr_pct or min_vol_pct:
            lines.append("• Пороги входа (расширенный тех. анализ):")
            if max_atr_pct:
                lines.append(f"  — ATR 5m макс.: {max_atr_pct}% (GAME_5M_MAX_ATR_5M_PCT; выше — не входить)")
            if min_vol_pct:
                lines.append(f"  — Объём мин. от среднего: {min_vol_pct}% (GAME_5M_MIN_VOLUME_VS_AVG_PCT; ниже — не входить)")
        base_take = params_5m["take_profit_pct"]
        base_days = params_5m["max_position_days"]
        lines.append("• По тикерам (тейк потолок, макс. дней):")
        for t in (tickers_5m or []):
            eff_take = _take_profit_cap_pct(t)
            eff_days = _max_position_days(t)
            note = ""
            if eff_take != base_take or eff_days != base_days:
                note = " (переопределён)"
            lines.append(f"  — {t}: тейк +{eff_take}%, макс. {eff_days} дн.{note}")
        lines += [
            "",
            "Портфельная игра (trading_cycle_cron):",
            f"• Тикеры: {', '.join(tickers_portfolio) or '—'}",
            f"• Тейк по умолчанию: +{portfolio_take}% (PORTFOLIO_TAKE_PROFIT_PCT; 0 = не закрывать по тейку)",
            f"• Стоп: {stop_enabled_str}, порог={stop_level} (PORTFOLIO_STOP_LOSS_ENABLED / STOP_LOSS_LEVEL)",
        ]
        if not stop_enabled_raw:
            lines.append("")
            lines.append("⚠️ Стоп-лосс портфеля выключен (PORTFOLIO_STOP_LOSS_ENABLED=false, config или БД). Закрытие по стопу не выполняется, только по тейку.")
        if db_info:
            lines.append("")
            lines.append(db_info)
        lines.append("")
        lines.append("Подробнее: docs/CRONS_AND_TAKE_STOP.md")
        # Без Markdown: в тексте есть PORTFOLIO_STOP_LOSS_ENABLED и др. — подчёркивания ломают парсер (Can't parse entities)
        await update.message.reply_text("\n".join(lines), parse_mode=None)

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик произвольных текстовых сообщений"""
        # В группах игнорируем текстовые сообщения без упоминания
        # Используйте команду /ask для вопросов в группах
        if update.message.chat.type in ('group', 'supergroup'):
            return
        
        user_id = update.effective_user.id
        
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        
        text = update.message.text.strip()
        await self._process_query(update, text)
        
    async def _handle_ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /ask <вопрос>"""
        user_id = update.effective_user.id
        
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        
        if not context.args or len(context.args) == 0:
            await update.message.reply_text(
                "❌ Задайте вопрос после команды\n"
                "Примеры:\n"
                "`/ask какая цена золота`\n"
                "`/ask какие новости по MSFT`\n"
                "`/ask анализ GBPUSD`",
                parse_mode='Markdown'
            )
            return
        
        # Объединяем все аргументы в один текст
        text = ' '.join(context.args).strip()
        logger.info(f"Обработка команды /ask: '{text}'")
        
        # Используем общую логику обработки запросов
        await self._process_query(update, text)
    
    async def _process_query(self, update: Update, text: str):
        """Общая логика обработки запросов (используется в /ask и текстовых сообщениях)"""
        logger.info(f"Обработка запроса: '{text}'")
        
        try:
            # Определяем тип запроса по ключевым словам
            text_lower = text.lower()
            is_news_query = any(word in text_lower for word in ['новости', 'новость', 'news', 'новостей', 'что пишут', 'что пишут про'])
            is_price_query = any(word in text_lower for word in ['цена', 'price', 'стоимость', 'стоит', 'сколько', 'какая цена', 'какая стоимость'])
            # Расширяем ключевые слова для анализа: "что с", "как дела", "ситуация" и т.д.
            is_analysis_query = any(word in text_lower for word in [
                'анализ', 'analysis', 'сигнал', 'signal', 'прогноз', 'forecast',
                'что с', 'как дела', 'ситуация', 'тренд', 'trend', 'рекомендация'
            ])
            is_recommendation_query = any(phrase in text_lower for phrase in [
                'когда можно открыть', 'когда открыть позицию', 'когда купить', 'когда войти',
                'какие параметры', 'параметры управления', 'что советуешь', 'какой стоп',
                'стоп-лосс', 'стейк-лосс', 'рекомендуй вход', 'можно ли открыть позицию'
            ])
            
            logger.info(f"Тип запроса: news={is_news_query}, price={is_price_query}, analysis={is_analysis_query}, recommend={is_recommendation_query}")
            
            # Пытаемся извлечь все тикеры из текста (может быть несколько)
            tickers = self._extract_all_tickers_from_text(text)
            logger.info(f"Извлечённые тикеры из текста '{text}': {tickers}")
            
            # Вопрос про вход в позицию и параметры управления — даём рекомендацию по тикеру
            if is_recommendation_query:
                rec_ticker = _normalize_ticker(tickers[0]) if tickers else None
                if not rec_ticker:
                    await update.message.reply_text(
                        "Укажите инструмент в вопросе, например:\n"
                        "• _когда можно открыть позицию по SNDK и какие параметры советуешь?_\n"
                        "• _рекомендуй параметры управления для GC=F_",
                        parse_mode="Markdown",
                    )
                    return
                await update.message.reply_text(f"🔍 Готовлю рекомендацию по {rec_ticker}...")
                data = self._get_recommendation_data(rec_ticker)
                if not data:
                    await update.message.reply_text(f"❌ Не удалось получить данные для {rec_ticker}.")
                    return
                recommendation_text = self._format_recommendation(data)
                if self.llm_service and recommendation_text:
                    try:
                        from services.game_5m import _game_5m_stop_loss_enabled
                        game5m_stop_off = not _game_5m_stop_loss_enabled()
                        system_prompt = (
                            "Ты помощник по виртуальной торговле. Пользователь задаёт вопрос о том, когда открыть позицию и какие параметры управления использовать. "
                            "Ответь кратко и по делу на русском, опираясь ТОЛЬКО на приведённые данные. "
                            + ("Стоп-лосс игры 5m отключён — не рекомендуй и не упоминай стоп по 5m позициям. " if game5m_stop_off else "Упомяни: стоит ли открывать позицию сейчас, стоп-лосс (из данных), размер позиции. ")
                            + "Не придумывай цифры — используй только данные из контекста."
                        )
                        ctx = (
                            f"Данные для ответа:\n{recommendation_text}\n\n"
                            f"Вопрос пользователя: {text}"
                        )
                        result = self.llm_service.generate_response(
                            messages=[{"role": "user", "content": ctx}],
                            system_prompt=system_prompt,
                            temperature=0.3,
                            max_tokens=400,
                        )
                        answer = (result.get("response") or "").strip()
                        if answer:
                            await update.message.reply_text(answer, parse_mode="Markdown")
                            return
                    except Exception as e:
                        logger.warning(f"LLM для рекомендации не сработал: {e}")
                # Без parse_mode: рекомендация может содержать _ * из reasoning/LLM — парсер падает
                await update.message.reply_text(recommendation_text, parse_mode=None)
                return
            
            if tickers:
                # Если найдено несколько тикеров и это запрос новостей - собираем все новости и выбираем топ N
                if is_news_query and len(tickers) > 1:
                    # Извлекаем количество новостей из запроса (если указано)
                    import re
                    count_match = re.search(r'(\d+)\s*(самые|топ|top|последние|важные)', text_lower)
                    top_n = int(count_match.group(1)) if count_match else 10
                    
                    await update.message.reply_text(f"📰 Поиск {top_n} самых важных новостей для {len(tickers)} инструментов...")
                    
                    # Собираем все новости по всем тикерам
                    import pandas as pd
                    all_news = []
                    ticker_names = []
                    
                    news_timeout_per_ticker = max(20, 60 // max(1, len(tickers)))
                    for ticker in tickers:
                        ticker = _normalize_ticker(ticker)
                        ticker_names.append(ticker)
                        try:
                            news_df = await self._get_recent_news_async(ticker, timeout=news_timeout_per_ticker)
                        except asyncio.TimeoutError:
                            logger.warning(f"Таймаут новостей для {ticker}, пропускаем")
                            continue
                        if not news_df.empty:
                            # Добавляем колонку с тикером для идентификации
                            news_df = news_df.copy()
                            news_df['ticker'] = ticker
                            all_news.append(news_df)
                    
                    if all_news:
                        # Объединяем все новости
                        combined_news = pd.concat(all_news, ignore_index=True)
                        
                        # Сортируем по важности:
                        # 1. Приоритет NEWS и EARNINGS над ECONOMIC_INDICATOR
                        # 2. По sentiment (более сильный sentiment = важнее)
                        # 3. По дате (более свежие = важнее)
                        def importance_score(row):
                            score = 0
                            # Приоритет типов событий
                            event_type = str(row.get('event_type', '')).upper()
                            if event_type == 'NEWS':
                                score += 1000
                            elif event_type == 'EARNINGS':
                                score += 800
                            elif event_type == 'ECONOMIC_INDICATOR':
                                score += 100
                            
                            # Sentiment (чем дальше от 0.5, тем важнее)
                            sentiment = row.get('sentiment_score', 0.5)
                            if sentiment is not None and not pd.isna(sentiment):
                                score += abs(sentiment - 0.5) * 500
                            
                            return score
                        
                        combined_news['importance'] = combined_news.apply(importance_score, axis=1)
                        combined_news = combined_news.sort_values('importance', ascending=False)
                        
                        # Берем топ N
                        top_news = combined_news.head(top_n)
                        
                        # Форматируем ответ
                        response = f"📰 **Топ {top_n} самых важных новостей** ({', '.join(ticker_names)}):\n\n"
                        
                        for idx, row in top_news.iterrows():
                            ticker = row.get('ticker', 'N/A')
                            ts = row.get('ts', '')
                            source = _escape_markdown(row.get('source') or '—')
                            event_type = _escape_markdown(row.get('event_type') or '')
                            content = row.get('content') or row.get('insight') or ''
                            if content:
                                preview = _escape_markdown(str(content)[:200])
                            else:
                                preview = "(без текста)"
                            
                            sentiment = row.get('sentiment_score')
                            sentiment_str = ""
                            if sentiment is not None and not pd.isna(sentiment):
                                if sentiment > 0.6:
                                    sentiment_str = " 📈"
                                elif sentiment < 0.4:
                                    sentiment_str = " 📉"
                            
                            date_str = ts.strftime('%Y-%m-%d %H:%M') if hasattr(ts, 'strftime') else str(ts)
                            prefix = "Ожидается отчёт:" if event_type == "EARNINGS" else ""
                            type_str = f" [{event_type}]" if event_type else ""
                            response += f"**{ticker}** - {prefix}{date_str}{sentiment_str}\n🔹 {source}{type_str}\n{preview}\n\n"
                        
                        try:
                            await update.message.reply_text(response, parse_mode='Markdown')
                        except Exception:
                            await update.message.reply_text(response)
                    else:
                        await update.message.reply_text(f"ℹ️ Не найдено новостей для {', '.join(ticker_names)}")
                elif len(tickers) == 1:
                    # Один тикер - обрабатываем как обычно
                    ticker = _normalize_ticker(tickers[0])
                    
                    if is_news_query:
                        # Извлекаем количество новостей из запроса (если указано)
                        import re
                        count_match = re.search(r'(\d+)\s*(самые|топ|top|последние)', text_lower)
                        top_n = int(count_match.group(1)) if count_match else 10
                        
                        # Запрос новостей
                        await update.message.reply_text(f"📰 Поиск новостей для {ticker}...")
                        try:
                            news_df = await self._get_recent_news_async(ticker, timeout=30)
                        except asyncio.TimeoutError:
                            await update.message.reply_text(
                                f"❌ Таймаут при получении новостей для {ticker}. Попробуйте позже."
                            )
                            return
                        if news_df.empty:
                            await update.message.reply_text(
                                f"ℹ️ Новостей для {ticker} в knowledge_base за окно KB не найдено."
                            )
                        else:
                            await self._send_kb_news_report(
                                update, context, ticker, news_df, top_n=top_n
                            )
                    elif is_price_query:
                        # Запрос цены
                        await self._handle_price_by_ticker(update, ticker)
                    else:
                        # Полный анализ: если тикер в игре 5m — технический сигнал 5m, иначе портфель
                        logger.info(f"Выполняем полный анализ для {ticker}")
                        await update.message.reply_text(f"🔍 Анализ {ticker}...")
                        try:
                            from services.ticker_groups import get_tickers_game_5m
                            from services.recommend_5m import get_5m_technical_signal
                            game5m_set = set(get_tickers_game_5m() or [])
                            if ticker in game5m_set:
                                tech = get_5m_technical_signal(ticker, days=5, use_llm_news=False)
                                if tech:
                                    response = self._format_5m_technical_signal(ticker, tech)
                                    await update.message.reply_text(response, parse_mode=None)
                                    return
                            decision_result = self.analyst.get_decision_with_llm(ticker)
                            logger.info(f"Получен результат анализа для {ticker}: {decision_result.get('decision')}")
                            response = self._format_signal_response(ticker, decision_result)
                            try:
                                await update.message.reply_text(response, parse_mode='Markdown')
                            except Exception as e:
                                logger.warning(f"Ошибка отправки Markdown, отправляем без форматирования: {e}")
                                await update.message.reply_text(response)
                        except Exception as e:
                            logger.error(f"Ошибка при анализе {ticker}: {e}", exc_info=True)
                            await update.message.reply_text(f"❌ Ошибка при анализе {ticker}: {str(e)}")
                else:
                    # Несколько тикеров, но не новости — по каждому: 5m технический если в игре 5m, иначе портфель
                    await update.message.reply_text(f"🔍 Анализ {len(tickers)} инструментов...")
                    try:
                        from services.ticker_groups import get_tickers_game_5m
                        from services.recommend_5m import get_5m_technical_signal
                        game5m_set = set(get_tickers_game_5m() or [])
                    except Exception:
                        game5m_set = set()
                    all_responses = []
                    for ticker in tickers:
                        ticker = _normalize_ticker(ticker)
                        try:
                            if ticker in game5m_set:
                                tech = get_5m_technical_signal(ticker, days=5, use_llm_news=False)
                                response = self._format_5m_technical_signal(ticker, tech) if tech else f"❌ Нет 5m данных: {ticker}"
                            else:
                                decision_result = self.analyst.get_decision_with_llm(ticker)
                                response = self._format_signal_response(ticker, decision_result)
                            all_responses.append(response)
                        except Exception as e:
                            logger.error(f"Ошибка при анализе {ticker}: {e}")
                            all_responses.append(f"❌ Ошибка при анализе {ticker}: {str(e)}")
                    
                    combined_response = "\n\n" + "="*40 + "\n\n".join(all_responses)
                    try:
                        await update.message.reply_text(combined_response, parse_mode='Markdown')
                    except Exception:
                        await update.message.reply_text(combined_response)
            else:
                # Тикер не найден - пробуем использовать LLM для понимания вопроса
                if self.llm_service:
                    logger.info("Тикер не найден, используем LLM для понимания вопроса")
                    await update.message.reply_text("🤖 Анализирую вопрос...")
                    
                    try:
                        # Пытаемся понять вопрос через LLM и найти тикер
                        llm_response = await self._ask_llm_about_ticker(update, text)
                        if llm_response:
                            try:
                                await update.message.reply_text(llm_response, parse_mode='Markdown')
                            except Exception:
                                await update.message.reply_text(llm_response)
                            return
                    except Exception as e:
                        logger.error(f"Ошибка при обращении к LLM: {e}", exc_info=True)
                
                # Fallback: ищем в Vector KB похожие события
                await update.message.reply_text("🔍 Поиск в базе знаний...")
                
                similar = self.vector_kb.search_similar(
                    query=text,
                    limit=3,
                    min_similarity=0.4
                )
                
                if similar.empty:
                    await update.message.reply_text(
                        "ℹ️ Не найдено релевантной информации.\n"
                        "Попробуйте указать тикер, например: GC=F или GBPUSD=X"
                    )
                else:
                    response = f"📚 **Найдено похожих событий:**\n\n"
                    for idx, row in similar.iterrows():
                        response += f"• {row.get('ticker', 'N/A')}: {row.get('content', '')[:100]}...\n"
                        response += f"  Similarity: {row.get('similarity', 0):.2f}\n\n"
                    
                    try:
                        await update.message.reply_text(response, parse_mode='Markdown')
                    except Exception:
                        await update.message.reply_text(response)
        
        except Exception as e:
            logger.error(f"Ошибка обработки запроса '{text}': {e}", exc_info=True)
            try:
                await update.message.reply_text(
                    f"❌ Ошибка обработки запроса: {str(e)}\n\n"
                    "Попробуйте использовать команды:\n"
                    "/ask <вопрос>\n"
                    "/signal <ticker>\n"
                    "/news <ticker>"
                )
            except Exception as send_err:
                logger.error(f"Ошибка отправки сообщения об ошибке: {send_err}")
    
    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик callback queries (для inline кнопок)"""
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        if not self._check_access(user_id):
            await query.edit_message_text("❌ Доступ запрещен")
            return
        
        # Обработка callback data
        data = query.data
        # Можно добавить логику для кнопок позже

    def _format_5m_technical_signal(self, ticker: str, tech: Dict[str, Any]) -> str:
        """Технический сигнал 5m — единый формат из services.signal_message_5m."""
        from services.signal_message_5m import build_5m_technical_short_text
        return build_5m_technical_short_text(tech, ticker)

    def _format_signal_response(self, ticker: str, decision_result: Dict[str, Any]) -> str:
        """Форматирует ответ с анализом сигнала"""
        decision = decision_result.get('decision', 'HOLD')
        technical_signal = decision_result.get('technical_signal', 'N/A')
        # Получаем sentiment (может быть в разных форматах)
        sentiment = decision_result.get('sentiment_normalized') or decision_result.get('sentiment', 0.0)
        if isinstance(sentiment, (int, float)):
            if 0.0 <= sentiment <= 1.0:
                # Конвертируем из 0.0-1.0 в -1.0-1.0
                sentiment = (sentiment - 0.5) * 2.0
        else:
            sentiment = 0.0
        strategy = decision_result.get('selected_strategy') or 'N/A'
        news_count = decision_result.get('news_count', 0)
        
        # Получаем текущую цену и RSI; при отсутствии RSI — считаем локально по close
        from sqlalchemy import create_engine, text
        from config_loader import get_database_url
        from services.rsi_calculator import get_or_compute_rsi
        
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT close, rsi FROM quotes WHERE ticker = :ticker ORDER BY date DESC LIMIT 1"),
                {"ticker": ticker}
            )
            row = result.fetchone()
            if not row:
                logger.warning(f"Нет данных в quotes для {ticker}")
                price = "N/A"
                rsi = None
            else:
                price = f"${row[0]:.2f}" if row[0] is not None else "N/A"
                rsi = row[1] if row[1] is not None else None
        if rsi is None:
            rsi = get_or_compute_rsi(engine, ticker)
        
        # Эмодзи для решения
        decision_emoji = {
            'STRONG_BUY': '🟢',
            'BUY': '🟡',
            'HOLD': '⚪',
            'SELL': '🔴'
        }.get(decision, '⚪')
        
        # Эмодзи для sentiment
        if sentiment > 0.3:
            sentiment_emoji = '📈'
            sentiment_label = 'положительный'
        elif sentiment < -0.3:
            sentiment_emoji = '📉'
            sentiment_label = 'отрицательный'
        else:
            sentiment_emoji = '➡️'
            sentiment_label = 'нейтральный'
        
        # RSI: берём из ответа аналитика, если есть, иначе из БД уже подтянули выше
        rsi_to_show = rsi
        if rsi_to_show is None:
            rsi_to_show = (decision_result.get("technical_data") or {}).get("rsi")
        # Форматируем RSI — строка всегда есть (либо значение, либо "нет данных")
        if rsi_to_show is not None:
            if rsi_to_show >= 70:
                rsi_emoji = "🔴"
                rsi_status = "перекупленность"
            elif rsi_to_show <= 30:
                rsi_emoji = "🟢"
                rsi_status = "перепроданность"
            elif rsi_to_show >= 60:
                rsi_emoji = "🟡"
                rsi_status = "близко к перекупленности"
            elif rsi_to_show <= 40:
                rsi_emoji = "🟡"
                rsi_status = "близко к перепроданности"
            else:
                rsi_emoji = "⚪"
                rsi_status = "нейтральная зона"
            rsi_text = f"\n{rsi_emoji} **RSI:** {rsi_to_show:.1f} ({rsi_status})"
        else:
            # Локальный расчёт уже пробовали (get_or_compute_rsi); нет данных = мало истории close
            rsi_hint = "недостаточно данных (нужно 15 дней close) или запустите update_prices.py"
            rsi_text = f"\n⚪ **RSI:** нет данных ({rsi_hint})"
        
        # Экранируем ticker для Markdown (GBPUSD=X содержит =)
        ticker_escaped = _escape_markdown(ticker)
        
        response = f"""
{decision_emoji} **{ticker_escaped}** - {decision}

💰 **Цена:** {price}{rsi_text}
📊 **Технический сигнал:** {technical_signal}
{sentiment_emoji} **Sentiment:** {sentiment:.2f} ({sentiment_label})
📋 **Стратегия:** {strategy}
📰 **Новостей:** {news_count}
        """
        
        # Добавляем reasoning если есть (экранируем)
        if decision_result.get('reasoning'):
            reasoning_escaped = _escape_markdown(str(decision_result.get('reasoning')[:200]))
            response += f"\n💭 **Обоснование:**\n{reasoning_escaped}..."
        
        return response.strip()
    
    def _format_news_response(self, ticker: str, news_df, top_n: int = 10) -> str:
        """Форматирует ответ с новостями. top_n — сколько записей показать. Шум (календарные числа) скрыт."""
        from services.kb_news_report import filter_kb_display_rows, order_kb_display_rows_for_ticker

        display_df = order_kb_display_rows_for_ticker(filter_kb_display_rows(news_df), ticker)
        total_display = len(display_df)
        if total_display == 0:
            return (
                f"📰 **Новости для {_escape_markdown(ticker)}** (последние 7 дней)\n\n"
                "Нет новостей с текстом. В выборке только записи календаря без описания."
            )
        response = (
            f"📰 **Новости для {_escape_markdown(ticker)}** (последние 7 дней, топ {top_n})\n"
            "_sentiment: 0–1 (0=негатив, 0.5=нейтр., 1=позитив)_\n\n"
        )

        def _content_preview(row) -> str:
            raw = (row.get('content') or row.get('insight') or '')
            if raw is None or (isinstance(raw, float) and str(raw) == 'nan'):
                raw = ''
            text = str(raw).strip()
            event = row.get('event_type')
            if len(text) <= 30 and text and ' ' not in text:
                prefix = f"[{event}] " if event else ""
                return f"{prefix}{text}"
            return text[:250] if len(text) > 250 else text

        shown = 0
        for idx, row in display_df.iterrows():
            if shown >= top_n:
                break
            ts = row.get('ts', '')
            source = _escape_markdown(row.get('source') or '—')
            event_type = _escape_markdown(row.get('event_type') or '')
            preview = _escape_markdown(_content_preview(row))
            if not preview:
                preview = "(без текста)"
            sentiment = row.get('sentiment_score')
            sentiment_str = ""
            if sentiment is not None and not (isinstance(sentiment, float) and math.isnan(sentiment)):
                if sentiment > 0.6:
                    sentiment_str = " 📈"
                elif sentiment < 0.4:
                    sentiment_str = " 📉"
                # Числовое значение для проверки (сетка 0.0–1.0: 0=негатив, 0.5=нейтр., 1=позитив)
                sentiment_str += f" ({float(sentiment):.2f})"
            date_str = ts.strftime('%Y-%m-%d %H:%M') if hasattr(ts, 'strftime') else str(ts)
            # EARNINGS: ts = дата отчёта (ожидаемая), не дата публикации
            prefix = "Ожидается отчёт:" if event_type == "EARNINGS" else "📅"
            type_str = f" [{event_type}]" if event_type else ""
            response += f"{prefix} {date_str}{sentiment_str}\n🔹 **{source}**{type_str}\n{preview}\n"
            # Insight от LLM (начало) — для проверки в боте
            insight_val = row.get('insight')
            if insight_val and isinstance(insight_val, str) and insight_val.strip():
                insight_esc = _escape_markdown(insight_val.strip()[:100])
                if insight_esc:
                    response += f"💭 _{insight_esc}_\n"
            response += "\n"
            shown += 1

        if total_display > shown:
            response += f"\n... и еще {total_display - shown} записей"
        if len(display_df) < len(news_df):
            response += f"\n_{_escape_markdown(f'скрыто записей календаря без текста: {len(news_df) - len(display_df)}')}_"
        return response
    
    def _extract_ticker_from_text(self, text: str) -> Optional[str]:
        """Пытается извлечь ticker из текста, включая естественные названия"""
        text_upper = text.upper()
        text_lower = text.lower()
        
        # Маппинг естественных названий на тикеры
        natural_names = {
            # Товары
            'золото': 'GC=F',
            'gold': 'GC=F',
            'золота': 'GC=F',
            'золотом': 'GC=F',
            'золоте': 'GC=F',
            'золоту': 'GC=F',  # дательный падеж
            'золот': 'GC=F',   # родительный падеж множественного числа
            'нефть': 'CL=F',
            'нефти': 'CL=F',
            'oil': 'CL=F',
            'crude': 'CL=F',
            'wti': 'CL=F',

            # Валютные пары
            'gbpusd': 'GBPUSD=X',
            'gbp/usd': 'GBPUSD=X',
            'gbp-usd': 'GBPUSD=X',
            'gbp usd': 'GBPUSD=X',
            'фунт': 'GBPUSD=X',
            'фунта': 'GBPUSD=X',
            'фунтом': 'GBPUSD=X',
            'фунте': 'GBPUSD=X',
            'фунту': 'GBPUSD=X',  # дательный падеж
            'фунт-доллар': 'GBPUSD=X',
            'фунт доллар': 'GBPUSD=X',
            'gbp': 'GBPUSD=X',  # короткое название
            
            'eurusd': 'EURUSD=X',
            'eur/usd': 'EURUSD=X',
            'eur-usd': 'EURUSD=X',
            'eur usd': 'EURUSD=X',
            'евро': 'EURUSD=X',
            'евро-доллар': 'EURUSD=X',
            'евро доллар': 'EURUSD=X',
            
            'usdjpy': 'USDJPY=X',
            'usd/jpy': 'USDJPY=X',
            'usd-jpy': 'USDJPY=X',
            'usd jpy': 'USDJPY=X',
            'йена': 'USDJPY=X',
            'йены': 'USDJPY=X',
            
            # Акции
            'microsoft': 'MSFT',
            'микрософт': 'MSFT',
            'sandisk': 'SNDK',
            'сандиск': 'SNDK',
        }
        
        # Проверяем естественные названия (сначала более длинные совпадения)
        # Сортируем по длине в обратном порядке, чтобы сначала проверять более длинные фразы
        sorted_names = sorted(natural_names.items(), key=lambda x: len(x[0]), reverse=True)
        for name, ticker in sorted_names:
            if name in text_lower:
                logger.debug(f"Найдено совпадение '{name}' -> {ticker} в тексте '{text_lower}'")
                return ticker
        
        # Известные тикеры
        known_tickers = [
            'GC=F', 'CL=F', 'GBPUSD=X', 'EURUSD=X', 'USDJPY=X',
            'MSFT', 'SNDK', 'MU', 'LITE', 'ALAB', 'TER'
        ]

        for ticker in known_tickers:
            if ticker in text_upper:
                return ticker
        
        # Пытаемся найти паттерн тикера (3-5 заглавных букв)
        import re
        match = re.search(r'\b([A-Z]{2,5}(?:=X|=F)?)\b', text_upper)
        if match:
            return match.group(1)
        
        return None
    
    async def _ask_llm_about_ticker(self, update: Update, question: str) -> Optional[str]:
        """Использует LLM для понимания вопроса и поиска тикера"""
        if not self.llm_service:
            return None
        
        system_prompt = """Ты помощник для торгового бота. Твоя задача - понять вопрос пользователя о финансовых инструментах и определить, о каком инструменте идёт речь.

Доступные инструменты:
- Золото: GC=F (также "золото", "gold")
- Нефть: CL=F (WTI, также "нефть", "oil", "crude")
- Валютные пары: GBPUSD=X (фунт, GBP), EURUSD=X (евро, EUR), USDJPY=X (йена, JPY)
- Акции: MSFT (Microsoft), SNDK (Sandisk) и другие

Если пользователь спрашивает про инструмент, определи тикер и ответь в формате:
ТИКЕР: <тикер>
ОПИСАНИЕ: <краткое описание что это>

Если не можешь определить тикер, ответь:
НЕИЗВЕСТНО

Примеры:
- "что с фунтом" -> ТИКЕР: GBPUSD=X
- "какая цена нефти" -> ТИКЕР: CL=F
- "какая цена золота" -> ТИКЕР: GC=F
- "новости по Microsoft" -> ТИКЕР: MSFT"""

        try:
            result = self.llm_service.generate_response(
                messages=[{"role": "user", "content": question}],
                system_prompt=system_prompt,
                temperature=0.1,
                max_tokens=200
            )
            
            response = result.get("response", "").strip()
            logger.info(f"LLM ответ на вопрос '{question}': {response}")
            
            # Пытаемся извлечь тикер из ответа LLM
            ticker_match = re.search(r'ТИКЕР:\s*([A-Z0-9=]+)', response, re.IGNORECASE)
            if ticker_match:
                ticker = ticker_match.group(1).upper()
                logger.info(f"LLM определил тикер: {ticker}")
                
                # Нормализуем тикер
                ticker = _normalize_ticker(ticker)
                
                # Выполняем анализ для найденного тикера
                decision_result = self.analyst.get_decision_with_llm(ticker)
                response = self._format_signal_response(ticker, decision_result)
                
                return response
            else:
                # LLM не смог определить тикер
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при обращении к LLM: {e}", exc_info=True)
            return None
    
    def _extract_all_tickers_from_text(self, text: str) -> list:
        """Извлекает все тикеры из текста (может быть несколько)"""
        text_upper = text.upper()
        text_lower = text.lower()
        
        found_tickers = []
        found_names = set()  # Чтобы не дублировать
        
        # Маппинг естественных названий на тикеры
        natural_names = {
            # Товары
            'золото': 'GC=F',
            'gold': 'GC=F',
            'золота': 'GC=F',
            'золотом': 'GC=F',
            'золоте': 'GC=F',
            'золоту': 'GC=F',
            'золот': 'GC=F',
            'нефть': 'CL=F',
            'нефти': 'CL=F',
            'oil': 'CL=F',
            'crude': 'CL=F',
            'wti': 'CL=F',

            # Валютные пары
            'gbpusd': 'GBPUSD=X',
            'gbp/usd': 'GBPUSD=X',
            'gbp-usd': 'GBPUSD=X',
            'gbp usd': 'GBPUSD=X',
            'фунт': 'GBPUSD=X',
            'фунта': 'GBPUSD=X',
            'фунтом': 'GBPUSD=X',
            'фунте': 'GBPUSD=X',
            'фунту': 'GBPUSD=X',
            'фунт-доллар': 'GBPUSD=X',
            'фунт доллар': 'GBPUSD=X',
            'gbp': 'GBPUSD=X',

            'eurusd': 'EURUSD=X',
            'eur/usd': 'EURUSD=X',
            'eur-usd': 'EURUSD=X',
            'eur usd': 'EURUSD=X',
            'евро': 'EURUSD=X',
            'евро-доллар': 'EURUSD=X',
            'евро доллар': 'EURUSD=X',
            
            'usdjpy': 'USDJPY=X',
            'usd/jpy': 'USDJPY=X',
            'usd-jpy': 'USDJPY=X',
            'usd jpy': 'USDJPY=X',
            'йена': 'USDJPY=X',
            'йены': 'USDJPY=X',
            
            # Акции
            'microsoft': 'MSFT',
            'микрософт': 'MSFT',
            'sandisk': 'SNDK',
            'сандиск': 'SNDK',
        }
        
        # Проверяем естественные названия (сначала более длинные фразы)
        sorted_names = sorted(natural_names.items(), key=lambda x: len(x[0]), reverse=True)
        for name, ticker in sorted_names:
            if name in text_lower and name not in found_names:
                found_tickers.append(ticker)
                found_names.add(name)
                logger.debug(f"Найдено совпадение '{name}' -> {ticker} в тексте '{text_lower}'")
        
        # Известные тикеры
        known_tickers = [
            'GC=F', 'CL=F', 'GBPUSD=X', 'EURUSD=X', 'USDJPY=X',
            'MSFT', 'SNDK', 'MU', 'LITE', 'ALAB', 'TER'
        ]
        
        for ticker in known_tickers:
            if ticker in text_upper and ticker not in found_tickers:
                found_tickers.append(ticker)
        
        # Пытаемся найти паттерн тикера (3-5 заглавных букв)
        import re
        matches = re.findall(r'\b([A-Z]{2,5}(?:=X|=F)?)\b', text_upper)
        for match in matches:
            if match not in found_tickers:
                found_tickers.append(match)
        
        return found_tickers
    
    def _split_long_message(self, text: str, max_length: int = 4000) -> list:
        """Разбивает длинное сообщение на части"""
        parts = []
        current_part = ""
        
        for line in text.split('\n'):
            if len(current_part) + len(line) + 1 > max_length:
                if current_part:
                    parts.append(current_part)
                    current_part = line + '\n'
                else:
                    # Строка слишком длинная, разбиваем по словам
                    words = line.split()
                    for word in words:
                        if len(current_part) + len(word) + 1 > max_length:
                            if current_part:
                                parts.append(current_part)
                            current_part = word + ' '
                        else:
                            current_part += word + ' '
            else:
                current_part += line + '\n'
        
        if current_part:
            parts.append(current_part)
        
        return parts
    
    def run_polling(self):
        """Запуск бота в режиме polling (для разработки)"""
        logger.info("🚀 Запуск Telegram бота в режиме polling...")
        logger.info(
            "Если команды не срабатывают: 1) Пишите боту в личку (Private), не в группе. "
            "2) Убедитесь, что не запущен второй процесс с тем же токеном (ps aux | grep run_telegram_bot). "
            "3) В логах при вашем сообщении должна появиться строка «Входящий апдейт»."
        )
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)
    
    def get_webhook_handler(self):
        """Возвращает функцию-обработчик для webhook (для использования в FastAPI)"""
        async def webhook_handler(update: Update):
            await self.application.process_update(update)
        
        return webhook_handler


if __name__ == "__main__":
    """Единая точка запуска: scripts/run_telegram_bot.py (без дублирования логики)."""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    script = root / "scripts" / "run_telegram_bot.py"
    raise SystemExit(subprocess.run([sys.executable, str(script)], cwd=str(root)).returncode)
