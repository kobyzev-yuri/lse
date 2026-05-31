"""Standalone HTML reports for per-card LLM (game5m / portfolio)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from services.game_report_html import esc, render_game_report_document, section_h2_pre


def _fmt_num(v: Any, decimals: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return esc(v)


def build_game5m_card_llm_html(payload: dict[str, Any]) -> str:
    ticker = str(payload.get("ticker") or "?").upper()
    decision = payload.get("technical_signal") or payload.get("decision") or "—"
    meta = []
    if payload.get("price") is not None:
        meta.append(f"Цена: {_fmt_num(payload.get('price'), 2)}")
    if payload.get("rsi_5m") is not None:
        meta.append(f"RSI(5m): {_fmt_num(payload.get('rsi_5m'), 1)}")
    if payload.get("momentum_2h_pct") is not None:
        meta.append(f"Импульс 2ч: {float(payload['momentum_2h_pct']):+.2f}%")
    if payload.get("volatility_5m_pct") is not None:
        meta.append(f"Vol 5m: {_fmt_num(payload.get('volatility_5m_pct'), 2)}%")
    if payload.get("take_profit_pct") is not None:
        meta.append(f"Тейк: +{_fmt_num(payload.get('take_profit_pct'), 2)}%")

    parts = [
        f'<div class="ticker-section"><h2>{esc(ticker)} — {esc(decision)}</h2>',
    ]
    if meta:
        parts.append(f'<p class="meta">{" · ".join(meta)}</p>')

    kb = payload.get("kb_news_impact")
    if kb:
        parts.append(section_h2_pre("KB (влияние на решение)", str(kb)))

    earn = (payload.get("earnings_entry_context_block") or "").strip()
    if earn:
        parts.append(section_h2_pre("Earnings (контекст в промпте)", earn))

    cn = payload.get("cluster_note")
    if cn:
        parts.append(section_h2_pre("Кластер / корреляция", str(cn)))

    ana = payload.get("llm_analysis") if isinstance(payload.get("llm_analysis"), dict) else {}
    llm_lines = []
    if ana.get("decision") or ana.get("decision_fused"):
        llm_lines.append(
            f"Решение LLM: legacy={esc(ana.get('decision') or '—')} · fused={esc(ana.get('decision_fused') or '—')}"
        )
    if ana.get("confidence") is not None:
        llm_lines.append(f"Уверенность: {_fmt_num(ana.get('confidence'), 2)}")
    reasoning = (payload.get("llm_reasoning") or ana.get("reasoning") or "").strip()
    if reasoning:
        llm_lines.append(reasoning)
    factors = payload.get("llm_key_factors") or ana.get("key_factors")
    if isinstance(factors, list) and factors:
        llm_lines.append("Ключевое: " + " · ".join(str(x) for x in factors if x))
    if not llm_lines:
        llm_lines.append(payload.get("llm_note") or "LLM не вызывался (нет cluster_note или WEB LLM off).")
    parts.append(section_h2_pre("Ответ LLM", "\n\n".join(llm_lines)))

    pu = (payload.get("prompt_user") or "").strip()
    if pu:
        parts.append(
            '<details><summary>Промпт user (контекст модели)</summary>'
            f"<pre>{esc(pu)}</pre></details>"
        )
    parts.append("</div>")

    ts = payload.get("generated_at_utc") or datetime.now(timezone.utc).isoformat()
    return render_game_report_document(
        title=f"GAME_5M · LLM отчёт · {ticker}",
        subtitle=f"Сгенерировано {ts} · открыт в отдельной вкладке (не сбрасывается автообновлением карточек)",
        body_html="\n".join(parts),
    )


def build_portfolio_card_llm_html(payload: dict[str, Any]) -> str:
    ticker = str(payload.get("ticker") or "?").upper()
    decision = payload.get("decision_effective") or payload.get("decision") or "—"
    meta = []
    if payload.get("close") is not None:
        meta.append(f"Close: {_fmt_num(payload.get('close'), 2)}")
    if payload.get("rsi") is not None:
        meta.append(f"RSI: {_fmt_num(payload.get('rsi'), 1)}")
    if payload.get("technical_signal"):
        meta.append(f"Техника: {esc(payload.get('technical_signal'))}")
    if payload.get("selected_strategy"):
        meta.append(f"Стратегия: {esc(payload.get('selected_strategy'))}")

    parts = [
        f'<div class="ticker-section"><h2>{esc(ticker)} — {esc(decision)}</h2>',
    ]
    if meta:
        parts.append(f'<p class="meta">{" · ".join(meta)}</p>')

    td = payload.get("technical_data") if isinstance(payload.get("technical_data"), dict) else {}
    earn = (td.get("earnings_entry_context_block") or payload.get("earnings_entry_context_block") or "").strip()
    if earn:
        parts.append(section_h2_pre("Earnings (контекст в промпте)", earn))

    kb = td.get("kb_news_signal_plain") or payload.get("kb_news_signal_plain")
    if kb:
        parts.append(section_h2_pre("KB", str(kb)))

    er_note = payload.get("event_reaction_ml_effect") or payload.get("event_reaction_ml_note")
    if er_note:
        parts.append(section_h2_pre("Event reaction ML (earnings advisory)", str(er_note)))

    ana = payload.get("llm_analysis") if isinstance(payload.get("llm_analysis"), dict) else {}
    llm_lines = []
    if ana.get("decision") or ana.get("decision_fused"):
        llm_lines.append(
            f"Решение LLM: legacy={esc(ana.get('decision') or '—')} · fused={esc(ana.get('decision_fused') or '—')}"
        )
    if ana.get("confidence") is not None:
        llm_lines.append(f"Уверенность: {_fmt_num(ana.get('confidence'), 2)}")
    if ana.get("reasoning"):
        llm_lines.append(str(ana["reasoning"]))
    if isinstance(ana.get("key_factors"), list) and ana["key_factors"]:
        llm_lines.append("Ключевое: " + " · ".join(str(x) for x in ana["key_factors"] if x))
    if not llm_lines:
        llm_lines.append("Нет ответа LLM.")
    parts.append(section_h2_pre("Ответ LLM", "\n\n".join(llm_lines)))

    pu = (payload.get("prompt_user") or "").strip()
    if pu:
        parts.append(
            '<details open><summary>Промпт user</summary>'
            f"<pre>{esc(pu)}</pre></details>"
        )
    ps = (payload.get("prompt_system") or "").strip()
    if ps:
        parts.append(
            '<details><summary>Промпт system</summary>'
            f"<pre>{esc(ps)}</pre></details>"
        )
    parts.append("</div>")

    ts = payload.get("generated_at_utc") or datetime.now(timezone.utc).isoformat()
    return render_game_report_document(
        title=f"Portfolio · LLM отчёт · {ticker}",
        subtitle=f"Сгенерировано {ts} · отдельная вкладка",
        body_html="\n".join(parts),
    )
