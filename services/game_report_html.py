# -*- coding: utf-8 -*-
"""
Общие HTML-шаблоны для отчётов по играм (портфель, 5m): один CSS и одна обёртка документа.
Использование: соберите разметку «тела» отчёта (таблица, секции тикеров), передайте в render_game_report_document.
"""
from __future__ import annotations

import html
from typing import Any, Optional

# Единый стиль: читаемый светлый отчёт, таблицы + секции + pre (промпты / KB)
GAME_REPORT_SHARED_CSS = """
:root { --game-fg: #1a1a1a; --game-muted: #555; --game-border: #ddd; --game-bg-pre: #f6f8fa; --game-accent: #e8f4f8; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: var(--game-fg); max-width: 900px; margin: 1em auto; padding: 0 1em 2em; line-height: 1.45; }
.game-report-header { border-bottom: 2px solid var(--game-border); padding-bottom: 0.75em; margin-bottom: 1em; }
.game-report-header h1 { font-size: 1.25em; margin: 0 0 0.35em 0; color: #111; }
.game-report-header .subtitle { margin: 0; color: var(--game-muted); font-size: 0.92em; }
.game-report-main { min-height: 4em; }
.game-report-footer { margin-top: 2em; padding-top: 0.75em; border-top: 1px solid var(--game-border); font-size: 0.8em; color: #888; }
h2 { font-size: 1.05em; margin-top: 1.15em; color: #333; border-bottom: 1px solid var(--game-border); padding-bottom: 0.25em; }
h3 { font-size: 0.98em; margin-top: 0.85em; color: #444; }
pre { white-space: pre-wrap; word-break: break-word; background: var(--game-bg-pre); padding: 0.65em 0.75em; border-radius: 6px; font-size: 0.88em; border: 1px solid #e1e4e8; }
table { border-collapse: collapse; width: 100%; font-size: 0.88em; margin: 0.5em 0; }
th, td { border: 1px solid var(--game-border); padding: 0.4em 0.55em; text-align: left; vertical-align: top; }
th { background: var(--game-accent); font-weight: 600; }
tr:nth-child(even) { background: #fafafa; }
.llm-cell { font-size: 0.84em; color: #555; max-width: 36em; }
.decision-buy { color: #0a6; font-weight: 600; }
.decision-hold { color: #555; }
.decision-sell { color: #c33; font-weight: 600; }
.cluster { background: var(--game-accent); padding: 0.65em 0.85em; border-radius: 6px; margin: 0.5em 0; border: 1px solid #cfe8f0; }
.ticker-section { margin-top: 1.35em; padding-top: 0.5em; border-top: 1px solid #eee; }
.meta { color: var(--game-muted); font-size: 0.88em; margin: 0.35em 0; }
.corr { font-size: 0.84em; color: #555; margin: 0.25em 0; }
.context-block { background: #fafafa; border-left: 4px solid #bbb; padding: 0.5em 0.75em; margin: 0.45em 0; font-size: 0.88em; white-space: pre-wrap; word-break: break-word; }
.intro { color: #555; font-size: 0.9em; margin-bottom: 1em; line-height: 1.5; }
.note { background: #fff8e6; padding: 0.55em 0.75em; border-radius: 6px; margin: 0.5em 0; border: 1px solid #f0e0b0; font-size: 0.9em; }
ul { margin: 0.35em 0; padding-left: 1.2em; }
"""


def esc(s: Any) -> str:
    """Безопасный вывод текста в HTML."""
    if s is None or s == "":
        return "—"
    return html.escape(str(s), quote=True)


def section_h2_pre(title: str, body: str) -> str:
    """Секция: заголовок + pre (KB, промпты)."""
    return f"<h2>{esc(title)}</h2><pre>{esc(body)}</pre>"


def section_h2_raw(title: str, inner_html: str) -> str:
    """Секция с уже экранированным inner_html."""
    return f"<h2>{esc(title)}</h2>{inner_html}"


def render_game_report_document(
    *,
    title: str,
    subtitle: Optional[str] = None,
    body_html: str,
    footer: str = "LSE · отчёт по игре (единый шаблон game_report_html)",
) -> str:
    """Полный HTML5-документ с общим CSS и предсказуемой шапкой/подвалом."""
    sub = f'<p class="subtitle">{esc(subtitle)}</p>' if subtitle else ""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ru"><head><meta charset="utf-8">\n'
        f"<title>{esc(title)}</title>\n"
        "<style>\n"
        f"{GAME_REPORT_SHARED_CSS}\n"
        "</style></head><body>\n"
        '<header class="game-report-header">\n'
        f"<h1>{esc(title)}</h1>\n"
        f"{sub}\n"
        "</header>\n"
        '<main class="game-report-main">\n'
        f"{body_html}\n"
        "</main>\n"
        f'<footer class="game-report-footer">{esc(footer)}</footer>\n'
        "</body></html>"
    )
