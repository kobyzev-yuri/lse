"""
Отчёт /news для LSE-бота: данные из knowledge_base + метрики bias в духе nyse.

- **rough_bias** («черновой» / draft): среднее по строкам (sentiment_score−0.5)×2
  с равными весами — аналог дешёвого скоринга до весов тикера/макро.
- **news_bias** («итоговый» news.bias): взвешенное среднее как в AnalystAgent.calculate_weighted_sentiment
  (тикер в строке или в тексте → вес 2.0, иначе 1.0; NaN → 0.5; затем normalize в [−1, 1]).
- **Режим гейта** (SKIP / LITE / FULL): та же логика веток, что `nyse/pipeline/gates.py::decide_llm_mode`
  для профиля GAME_5M (t1=0.12, t2=0.5, max_articles=8, regime_stress_min=0.05), но вход **draft_bias**
  заменён на **rough_bias**, а «REGIME» — на **regime_stress** по макро-строкам (MACRO/US_MACRO).
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

import pandas as pd


# Согласовано с nyse PROFILE_GAME5M (docs/calibration.md)
_T1 = 0.12
_T2 = 0.5
_MAX_ARTICLES_FULL = 8
_REGIME_STRESS_MIN = 0.05
_FULL_BIAS_MULT = 2.0


def _h(s: Any) -> str:
    return html.escape(str(s) if s is not None else "")


def filter_kb_display_rows(news_df: pd.DataFrame) -> pd.DataFrame:
    """Тот же фильтр шума, что в telegram_bot._format_news_response."""

    def _is_noise(row) -> bool:
        if row.get("event_type") != "ECONOMIC_INDICATOR":
            return False
        raw = row.get("content") or row.get("insight") or ""
        if raw is None or (isinstance(raw, float) and str(raw) == "nan"):
            return True
        text = str(raw).strip()
        if len(text) > 50 or " " in text:
            return False
        return True

    if news_df.empty:
        return news_df
    return news_df[~news_df.apply(_is_noise, axis=1)].reset_index(drop=True)


def _row_bias_neg1(sentiment_score: Any) -> float:
    """Шкала KB 0..1 → −1..+1 (нейтраль 0.5 → 0). Пустое → нейтраль."""
    if sentiment_score is None or (isinstance(sentiment_score, float) and math.isnan(sentiment_score)):
        return 0.0
    try:
        s = float(sentiment_score)
    except (TypeError, ValueError):
        return 0.0
    return (s - 0.5) * 2.0


def _channel_for_row(row: pd.Series, ticker: str) -> Tuple[str, str]:
    """
    Псевдо-канал в стиле nyse (INC / REG / POL) для отображения.
    """
    t = str(row.get("ticker") or "").upper()
    ev = str(row.get("event_type") or "").upper()
    if t in ("MACRO", "US_MACRO"):
        return "REG", "Макро / режим"
    if ev == "ECONOMIC_INDICATOR":
        return "POL", "Ставки / индикаторы"
    if "MACRO" in ev or ev == "MACRO_NEWS":
        return "REG", "Макро / режим"
    return "INC", "Корп. / тикер"


def _regime_stress(macro_df: pd.DataFrame) -> float:
    """Среднее |row_bias| по макро-строкам (0 если пусто)."""
    if macro_df is None or macro_df.empty:
        return 0.0
    vals = []
    for _, r in macro_df.iterrows():
        vals.append(abs(_row_bias_neg1(r.get("sentiment_score"))))
    return sum(vals) / len(vals) if vals else 0.0


def decide_kb_gate(
    rough_bias: float,
    regime_stress: float,
    article_count: int,
) -> Tuple[str, str]:
    """
    Аналог decide_llm_mode без календаря; draft_bias = rough_bias.
    Возвращает (SKIP|LITE|FULL, пояснение на русском).
    """
    regime_present = regime_stress > _REGIME_STRESS_MIN
    regime_rule_confidence = 0.85 if regime_present else 0.0
    ab = abs(rough_bias)

    if regime_present and regime_rule_confidence >= _T2:
        return (
            "FULL",
            f"FULL: REGIME — stress={regime_stress:.3f} > {_REGIME_STRESS_MIN}, "
            f"rule_conf={regime_rule_confidence:.2f} ≥ t2={_T2} (как nyse: макро-фон тянет полный LLM; "
            f"у LSE это только пояснение к приоритету анализа KB).",
        )
    if ab >= _T1 * _FULL_BIAS_MULT:
        return (
            "FULL",
            f"FULL: |draft_bias|={ab:.3f} ≥ t1×2={_T1 * _FULL_BIAS_MULT:.3f} "
            f"(draft = среднее row-bias по KB, пороги как PROFILE_GAME5M).",
        )
    if ab < _T1 and not regime_present:
        return (
            "SKIP",
            f"SKIP: |draft_bias|={ab:.3f} < t1={_T1:.3f}, нет REGIME (как nyse: дорогой LLM не нужен).",
        )
    if article_count > _MAX_ARTICLES_FULL:
        return (
            "LITE",
            f"LITE: статей {article_count} > max_full={_MAX_ARTICLES_FULL} "
            f"(как nyse: сужение батча при умеренном сигнале).",
        )
    return (
        "LITE",
        f"LITE: умеренный сигнал |draft_bias|={ab:.3f} (t1 ≤ |x| < t1×2 или REGIME слабее порога).",
    )


def weighted_news_bias_neg1_from_kb_df(news_df: pd.DataFrame, ticker: str) -> float:
    """
    Взвешенный news.bias в шкале −1..+1 без AnalystAgent (та же логика весов 2.0/1.0
    и normalize_sentiment, что calculate_weighted_sentiment).
    """
    from utils.sentiment_utils import normalize_sentiment

    if news_df is None or news_df.empty:
        return 0.0
    tk = (ticker or "").upper()

    def _weight(row: pd.Series) -> float:
        content = str(row.get("content") or row.get("title") or row.get("insight") or "")
        t = str(row.get("ticker") or "")
        if t.upper() == tk or tk in content.upper():
            return 2.0
        return 1.0

    df = news_df.copy()
    if "content" not in df.columns:
        df["content"] = ""
    df["weight"] = df.apply(_weight, axis=1)
    sentiment_series = pd.to_numeric(df["sentiment_score"], errors="coerce").fillna(0.5).astype(float)
    tw = float((sentiment_series * df["weight"]).sum())
    wsum = float(df["weight"].sum())
    weighted_0_1 = tw / wsum if wsum > 0 else 0.5
    return float(normalize_sentiment(weighted_0_1))


def compute_kb_bias_from_article_dicts(
    rows: List[dict],
    ticker: str,
    lookback_hours: Optional[int] = None,
) -> dict:
    """
    Метрики bias по списку статей KB (как get_decision_5m → kb_news), без AnalystAgent.
    Совместимо по смыслу с compute_kb_news_bias_metrics (rough_bias, news_bias, gate).
    """
    if not rows:
        return {
            "rough_bias": 0.0,
            "news_bias": 0.0,
            "regime_stress": 0.0,
            "gate_mode": "SKIP",
            "gate_reason": "Нет статей KB.",
            "n_rows": 0,
            "n_macro": 0,
            "lookback_hours": lookback_hours,
        }
    df = pd.DataFrame(rows)
    if "content" not in df.columns:
        df["content"] = ""
    if "ticker" not in df.columns:
        df["ticker"] = ""
    if "sentiment_score" not in df.columns:
        df["sentiment_score"] = None
    if "event_type" not in df.columns:
        df["event_type"] = ""
    disp = filter_kb_display_rows(df)
    n = len(disp)
    if n == 0:
        return {
            "rough_bias": 0.0,
            "news_bias": 0.0,
            "regime_stress": 0.0,
            "gate_mode": "SKIP",
            "gate_reason": "Нет строк после фильтра шума.",
            "n_rows": 0,
            "n_macro": 0,
            "lookback_hours": lookback_hours,
        }
    biases = [_row_bias_neg1(r.get("sentiment_score")) for _, r in disp.iterrows()]
    rough_bias = sum(biases) / len(biases)
    macro_df = disp[disp["ticker"].isin(["MACRO", "US_MACRO"])]
    regime_stress = _regime_stress(macro_df)
    news_bias = weighted_news_bias_neg1_from_kb_df(disp, ticker)
    mode, reason = decide_kb_gate(rough_bias, regime_stress, n)
    return {
        "rough_bias": round(float(rough_bias), 4),
        "news_bias": round(float(news_bias), 4),
        "regime_stress": round(float(regime_stress), 4),
        "gate_mode": mode,
        "gate_reason": reason,
        "n_rows": n,
        "n_macro": len(macro_df),
        "lookback_hours": lookback_hours,
    }


def compute_kb_news_bias_metrics(
    news_df: pd.DataFrame,
    ticker: str,
    analyst: Any,
    lookback_hours: int,
) -> dict:
    """
    Считает rough_bias, news_bias (weighted), regime_stress, gate_mode, gate_reason.
    """
    disp = filter_kb_display_rows(news_df)
    n = len(disp)

    if n == 0:
        return {
            "rough_bias": 0.0,
            "news_bias": 0.0,
            "regime_stress": 0.0,
            "gate_mode": "SKIP",
            "gate_reason": "Нет строк для отображения после фильтра шума.",
            "n_rows": 0,
            "n_macro": 0,
            "lookback_hours": lookback_hours,
        }

    biases = [_row_bias_neg1(r.get("sentiment_score")) for _, r in disp.iterrows()]
    rough_bias = sum(biases) / len(biases)

    macro_df = disp[disp["ticker"].isin(["MACRO", "US_MACRO"])]
    regime_stress = _regime_stress(macro_df)

    news_bias = float(analyst.calculate_weighted_sentiment(disp, ticker))

    mode, reason = decide_kb_gate(rough_bias, regime_stress, n)

    return {
        "rough_bias": round(rough_bias, 4),
        "news_bias": round(news_bias, 4),
        "regime_stress": round(regime_stress, 4),
        "gate_mode": mode,
        "gate_reason": reason,
        "n_rows": n,
        "n_macro": len(macro_df),
        "lookback_hours": lookback_hours,
    }


def _bias_arrow(b: float) -> str:
    return "▲" if b > 0.05 else ("▼" if b < -0.05 else "■")


_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: #0d1117; color: #e6edf3; margin: 0; padding: 16px; }
h1 { font-size: 1.25em; margin: 0 0 8px; }
h2 { font-size: 1em; color: #8b949e; margin: 20px 0 8px; border-bottom: 1px solid #30363d; padding-bottom: 4px; }
.meta { color: #8b949e; font-size: 0.85em; margin-bottom: 12px; }
table { border-collapse: collapse; width: 100%; margin-bottom: 12px; }
th { background: #161b22; color: #8b949e; font-size: 0.78em; text-align: left; padding: 6px 8px; }
td { padding: 6px 8px; border-bottom: 1px solid #21262d; font-size: 0.88em; vertical-align: top; }
.tag { background: #21262d; border-radius: 4px; padding: 1px 6px; font-size: 0.8em; }
.pos { color: #3fb950; } .neg { color: #f85149; } .neu { color: #8b949e; }
.mono { font-family: ui-monospace, monospace; }
.box { background: #161b22; border-radius: 6px; padding: 12px; margin: 12px 0; font-size: 0.9em; line-height: 1.5; }
"""


def build_kb_news_short_html(
    ticker: str,
    news_df: pd.DataFrame,
    metrics: dict,
    top_n: int,
) -> str:
    """Краткое HTML для чата (nyse-стиль): bias + топ строк."""
    tv = ticker.upper()
    disp = filter_kb_display_rows(news_df)
    rough = metrics["rough_bias"]
    nb = metrics["news_bias"]
    mode = metrics["gate_mode"]
    rs = metrics["regime_stress"]

    lines = [
        f"📰 <b>{_h(tv)}</b> — KB, последние <b>{metrics['lookback_hours']}</b> ч",
        f"<b>draft_bias</b> (грубое среднее): <code>{rough:+.4f}</code> {_bias_arrow(rough)}",
        f"<b>news.bias</b> (взвеш. KB): <code>{nb:+.4f}</code> {_bias_arrow(nb)}",
        f"<b>Gate</b> (как nyse, по draft): <code>{_h(mode)}</code> · regime_stress=<code>{rs:.3f}</code>",
        f"<i>{_h(metrics['gate_reason'])}</i>",
        "",
    ]
    shown = 0
    for _, row in disp.iterrows():
        if shown >= top_n:
            break
        ch, _story = _channel_for_row(row, tv)
        bar = _bias_arrow(_row_bias_neg1(row.get("sentiment_score")))
        rb = _row_bias_neg1(row.get("sentiment_score"))
        title = str(row.get("content") or "")[:72]
        src = str(row.get("source") or "?")[:20]
        lines.append(
            f"{bar} <code>{_h(ch)}</code> <code>{_h(src)}</code> "
            f"<code>{rb:+.2f}</code> {_h(title)}"
        )
        shown += 1
    lines.append("")
    lines.append("📎 <i>Полный HTML — в документе ниже</i>")
    return "\n".join(lines)


def build_kb_news_full_html(
    ticker: str,
    news_df: pd.DataFrame,
    metrics: dict,
    top_n: int,
) -> str:
    """Полный HTML-отчёт с формулами и таблицей."""
    tv = ticker.upper()
    disp = filter_kb_display_rows(news_df)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rough = metrics["rough_bias"]
    nb = metrics["news_bias"]
    mode = metrics["gate_mode"]

    explain = f"""
<div class="box">
<b>Как считается draft_bias (грубый)</b><br>
Для каждой строки после фильтра шума: <code>row_bias = (sentiment_score − 0.5) × 2</code>,
где <code>sentiment_score</code> из PostgreSQL (0=негатив, 0.5=нейтр., 1=позитив), как после
<code>add_sentiment_to_news_cron</code>. Пустое значение → нейтраль 0.<br>
<b>draft_bias</b> = среднее арифметическое всех <code>row_bias</code> (равные веса) — аналог
«дешёвого» чернового импульса до разделения тикер/макро.<br><br>
<b>Как считается news.bias</b><br>
То же, что <code>AnalystAgent.calculate_weighted_sentiment</code>: вес 2.0 если
<code>ticker</code> строки = тикеру или тикер встречается в <code>content</code>, иначе 1.0 (макро).
Пропуски <code>sentiment_score</code> заполняются 0.5 (нейтраль), затем взвешенное среднее в шкале 0..1
и нормализация <code>(x−0.5)×2</code> в [−1, +1].<br><br>
<b>Почему режим {mode}</b><br>
{_h(metrics["gate_reason"])}<br>
Пороги совпадают с nyse <code>PROFILE_GAME5M</code>: t1={_T1}, t1×2={_T1*_FULL_BIAS_MULT},
t2={_T2}, max_articles_full={_MAX_ARTICLES_FULL}, regime_stress_min={_REGIME_STRESS_MIN}.
У LSE нет вызова LLM по этой команде — режим показывает, <i>какой шаг был бы выбран</i> в nyse-конвейере
при таком черновом сигнале.
</div>
"""

    rows_html = []
    shown = 0
    for _, row in disp.iterrows():
        if shown >= top_n:
            break
        ch, story = _channel_for_row(row, tv)
        rb = _row_bias_neg1(row.get("sentiment_score"))
        try:
            raw = float(row.get("sentiment_score"))
            if math.isnan(raw):
                raw_s = "—"
            else:
                raw_s = f"{raw:.2f}"
        except (TypeError, ValueError):
            raw_s = "—"
        rcls = "pos" if rb > 0.05 else ("neg" if rb < -0.05 else "neu")
        ts_row = row.get("ts")
        ts_str = ts_row.strftime("%Y-%m-%d %H:%M") if hasattr(ts_row, "strftime") else str(ts_row)
        title = str(row.get("content") or "")[:200]
        summ = str(row.get("insight") or "")[:120]
        rows_html.append(
            "<tr>"
            f"<td>{shown + 1}</td>"
            f'<td><span class="tag">{_h(ch)}</span></td>'
            f"<td>{_h(story)}</td>"
            f"<td>{_h(row.get('source') or '')}</td>"
            f"<td>{_h(str(row.get('event_type') or ''))}</td>"
            f"<td>{_h(str(row.get('ticker') or ''))}</td>"
            f'<td class="mono {rcls}">{_bias_arrow(rb)} {rb:+.3f}</td>'
            f'<td class="mono">{_h(raw_s)}</td>'
            f"<td>{_h(ts_str)}</td>"
            f"<td>{_h(title)}"
            + (f"<br><small style='color:#8b949e'>{_h(summ)}</small>" if summ.strip() else "")
            + "</td>"
            "</tr>"
        )
        shown += 1

    summary_tbl = f"""
<table>
<thead><tr><th>Метрика</th><th>Значение</th></tr></thead>
<tbody>
<tr><td>draft_bias (грубое среднее row_bias)</td><td class="mono">{rough:+.4f}</td></tr>
<tr><td>news.bias (взвешенное KB)</td><td class="mono">{nb:+.4f}</td></tr>
<tr><td>regime_stress (макро-строки)</td><td class="mono">{metrics['regime_stress']:.4f}</td></tr>
<tr><td>Gate (аналог nyse)</td><td><b>{_h(mode)}</b></td></tr>
<tr><td>Строк в отчёте</td><td>{metrics['n_rows']} (макро: {metrics['n_macro']})</td></tr>
</tbody></table>
"""

    body = (
        f"<h1>📰 {_h(tv)} — новости knowledge_base</h1>"
        f'<p class="meta">{_h(ts)} · окно {metrics["lookback_hours"]} ч · топ {top_n} строк</p>'
        + summary_tbl
        + explain
        + "<h2>Статьи</h2>"
        + "<table><thead><tr>"
        "<th>#</th><th>Ch</th><th>Тип</th><th>Source</th><th>event_type</th><th>ticker</th>"
        "<th>row_bias</th><th>score 0–1</th><th>Время</th><th>Текст / insight</th>"
        "</tr></thead><tbody>"
        + "".join(rows_html)
        + "</tbody></table>"
    )

    return (
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        f"<title>{_h(tv)} KB news</title><style>{_CSS}</style></head><body>"
        + body
        + "</body></html>"
    )


def kb_news_lookback_hours() -> int:
    """Тот же дефолт, что у AnalystAgent.get_recent_news."""
    try:
        from config_loader import get_config_value

        raw = (get_config_value("KB_NEWS_LOOKBACK_HOURS", "336") or "336").strip()
        h = int(raw)
    except (ValueError, TypeError):
        h = 336
    return max(24, min(h, 24 * 45))
