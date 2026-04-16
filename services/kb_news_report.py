"""
Отчёт /news для LSE-бота: данные из knowledge_base + метрики bias в духе nyse.

- **rough_bias** («черновой» / draft): среднее по строкам (sentiment_score−0.5)×2
  с равными весами — аналог дешёвого скоринга до весов тикера/макро.
- **news_bias** («итоговый» news.bias): взвешенное среднее как в AnalystAgent.calculate_weighted_sentiment
  (тикер в строке или в тексте → вес 2.0, иначе 1.0; NaN → 0.5; затем normalize в [−1, 1]).
- **Режим гейта** (SKIP / LITE / FULL): база как `nyse` PROFILE_GAME_5M (t1, t2, max_articles, regime_stress),
  плюс **оверлеи CALENDAR / GEO**: ближайшие события Investing.com economic calendar в KB (ts вперёд)
  и геополитические совпадения в окне новостей — могут поднять режим до LITE/FULL (см. `decide_kb_gate`).
"""

from __future__ import annotations

import html
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


# Согласовано с nyse PROFILE_GAME5M (docs/calibration.md)
_T1 = 0.12
_T2 = 0.5
_MAX_ARTICLES_FULL = 8
_REGIME_STRESS_MIN = 0.05
_FULL_BIAS_MULT = 2.0


def fetch_kb_macro_calendar_upcoming(engine: Any, ahead_hours: int = 72) -> pd.DataFrame:
    """События Investing.com economic calendar в KB с ts в будущем (до ahead_hours от сейчас)."""
    if engine is None:
        return pd.DataFrame()
    ah = max(6, min(int(ahead_hours), 24 * 14))
    try:
        from sqlalchemy import text

        now = datetime.utcnow()
        until = now + timedelta(hours=ah)
        with engine.connect() as conn:
            df = pd.read_sql(
                text(
                    """
                    SELECT ts, ticker, source, content, event_type, importance, region
                    FROM knowledge_base
                    WHERE ticker IN ('MACRO', 'US_MACRO')
                      AND source ILIKE '%Investing.com%Economic%Calendar%'
                      AND ts >= :now_ts
                      AND ts <= :until_ts
                    ORDER BY ts ASC
                    LIMIT 48
                    """
                ),
                conn,
                params={"now_ts": now, "until_ts": until},
            )
        return df if df is not None else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _is_mega_macro_calendar_row(row: pd.Series) -> bool:
    """CPI / NFP / ставки / FOMC — «крупные» окна как в nyse."""
    et = str(row.get("event_type") or "").upper()
    if et in ("CPI", "NFP", "RATE_DECISION", "GDP", "PMI", "UNEMPLOYMENT", "RETAIL_SALES", "PPI"):
        return True
    c = str(row.get("content") or "").lower()
    keys = (
        "fomc", "fed rate", "interest rate decision", "non-farm", "payrolls",
        "consumer price", "cpi ", " cpi", "core pce", "powell",
    )
    return any(k in c for k in keys)


def summarize_calendar_context(cal_df: pd.DataFrame, *, ahead_hours: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ahead_hours": int(ahead_hours),
        "n_rows": 0,
        "high_48h": 0,
        "mega_72h": False,
        "summary_short": "",
        "lines": [],
    }
    if cal_df is None or cal_df.empty:
        return out
    dfc = cal_df.copy()
    out["n_rows"] = int(len(dfc))
    now = pd.Timestamp.now(tz="UTC")
    t48 = now + pd.Timedelta(hours=48)
    t72 = now + pd.Timedelta(hours=72)
    dfc["_ts"] = pd.to_datetime(dfc["ts"], errors="coerce", utc=True)
    dfc = dfc[dfc["_ts"].notna()]
    if dfc.empty:
        return out
    impu = dfc["importance"].astype(str).str.strip().str.upper()
    high48 = int(((impu == "HIGH") & (dfc["_ts"] <= t48) & (dfc["_ts"] >= now)).sum())
    m72 = dfc[(dfc["_ts"] <= t72) & (dfc["_ts"] >= now)]
    mega = bool(m72.apply(_is_mega_macro_calendar_row, axis=1).any()) if not m72.empty else False
    out["high_48h"] = high48
    out["mega_72h"] = bool(mega)
    lines: List[str] = []
    for _, row in dfc.head(14).iterrows():
        tsr = row["_ts"]
        tss = tsr.strftime("%Y-%m-%d %H:%M")
        im = str(row.get("importance") or "")
        ev = str(row.get("content") or "").replace("\n", " ")[:70]
        lines.append(f"{tss} [{im}] {ev}")
    out["lines"] = lines
    if lines:
        out["summary_short"] = lines[0][:130] + ("…" if len(lines[0]) > 130 else "")
    return out


def summarize_geopolitical_context(news_df: pd.DataFrame) -> Dict[str, Any]:
    """Скан окна новостей на гео-ключи (тот же словарь, что для LLM)."""
    from services.geopolitical_prompt import is_geopolitical_item

    out: Dict[str, Any] = {
        "n_geo": 0,
        "geo_stress": 0.0,
        "summary_short": "",
        "lines": [],
    }
    if news_df is None or news_df.empty:
        return out
    biases: List[float] = []
    lines: List[str] = []
    for _, row in news_df.iterrows():
        d = row.to_dict()
        if not is_geopolitical_item(d):
            continue
        out["n_geo"] += 1
        rb = _row_bias_neg1(row.get("sentiment_score"))
        biases.append(abs(rb))
        raw = str(row.get("content") or "")[:100].replace("\n", " ")
        if raw and raw not in lines and len(lines) < 8:
            lines.append(raw[:100])
    if biases:
        out["geo_stress"] = float(sum(biases) / len(biases))
    out["lines"] = lines
    if lines:
        out["summary_short"] = lines[0][:120] + ("…" if len(lines[0]) > 120 else "")
    return out


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
    *,
    calendar_ctx: Optional[Dict[str, Any]] = None,
    geo_ctx: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """
    Аналог nyse decide_llm_mode (PROFILE_GAME5M): draft = rough_bias, REGIME = regime_stress,
    плюс оверлеи CALENDAR (HIGH / CPI/NFP/FOMC в KB) и GEO (гео-новости в окне).
    """
    cal = dict(calendar_ctx or {})
    geo = dict(geo_ctx or {})
    regime_present = regime_stress > _REGIME_STRESS_MIN
    regime_rule_confidence = 0.85 if regime_present else 0.0
    ab = abs(rough_bias)
    mega = bool(cal.get("mega_72h"))
    high48 = int(cal.get("high_48h") or 0)
    cal_sm = str(cal.get("summary_short") or "")[:160]
    geo_n = int(geo.get("n_geo") or 0)
    geo_stress = float(geo.get("geo_stress") or 0.0)
    geo_sm = str(geo.get("summary_short") or "")[:160]

    if regime_present and regime_rule_confidence >= _T2:
        return (
            "FULL",
            f"FULL: REGIME — stress={regime_stress:.3f} > {_REGIME_STRESS_MIN}, "
            f"rule_conf={regime_rule_confidence:.2f} ≥ t2={_T2} (как nyse: макро-фон тянет полный LLM; "
            f"у LSE это только пояснение к приоритету анализа KB).",
        )

    if mega or high48 >= 2:
        tag = "крупное событие (CPI/NFP/FOMC/ставки…)" if mega else f"HIGH×{high48} в ближайшие 48ч"
        return (
            "FULL",
            f"FULL: CALENDAR — {tag}. Кратко: {cal_sm} (как nyse: окно макро — полный разбор).",
        )

    if geo_n >= 3 and geo_stress >= _REGIME_STRESS_MIN:
        return (
            "FULL",
            f"FULL: GEO — {geo_n} гео-релевантных строк, среднее |row_bias|={geo_stress:.3f} ≥ {_REGIME_STRESS_MIN}. "
            f"Пример: {geo_sm} (как nyse: risk-off / полный LLM).",
        )

    if ab >= _T1 * _FULL_BIAS_MULT:
        return (
            "FULL",
            f"FULL: |draft_bias|={ab:.3f} ≥ t1×2={_T1 * _FULL_BIAS_MULT:.3f} "
            f"(draft = среднее row-bias по KB, пороги как PROFILE_GAME5M).",
        )

    if high48 >= 1 and ab < _T1 and not regime_present:
        return (
            "LITE",
            f"LITE: CALENDAR overlay — при тихом draft (|x|={ab:.3f} < t1) есть HIGH-макро в 48ч "
            f"(nyse: не опускаться в SKIP). {cal_sm}",
        )

    if ab < _T1 and not regime_present:
        return (
            "SKIP",
            f"SKIP: |draft_bias|={ab:.3f} < t1={_T1:.3f}, нет REGIME/CALENDAR/GEO-триггеров для FULL "
            f"(как nyse: дорогой LLM не нужен).",
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
    *,
    engine: Any = None,
    calendar_ahead_hours: int = 72,
) -> dict:
    """
    Считает rough_bias, news_bias (weighted), regime_stress, gate_mode, gate_reason,
    плюс контекст календаря (KB, вперёд) и геополитики по окну новостей — для отчёта /news и gate.
    """
    ndf = news_df if news_df is not None else pd.DataFrame()
    cal_df = fetch_kb_macro_calendar_upcoming(engine, calendar_ahead_hours) if engine is not None else pd.DataFrame()
    cal_ctx = summarize_calendar_context(cal_df, ahead_hours=calendar_ahead_hours)
    geo_ctx = summarize_geopolitical_context(ndf)

    disp = filter_kb_display_rows(ndf)
    n = len(disp)

    if n == 0:
        mode, reason = decide_kb_gate(0.0, 0.0, 0, calendar_ctx=cal_ctx, geo_ctx=geo_ctx)
        return {
            "rough_bias": 0.0,
            "news_bias": 0.0,
            "regime_stress": 0.0,
            "gate_mode": mode,
            "gate_reason": reason,
            "n_rows": 0,
            "n_macro": 0,
            "lookback_hours": lookback_hours,
            "calendar": cal_ctx,
            "geopolitical": geo_ctx,
        }

    biases = [_row_bias_neg1(r.get("sentiment_score")) for _, r in disp.iterrows()]
    rough_bias = sum(biases) / len(biases)

    macro_df = disp[disp["ticker"].isin(["MACRO", "US_MACRO"])]
    regime_stress = _regime_stress(macro_df)

    news_bias = float(analyst.calculate_weighted_sentiment(disp, ticker))

    mode, reason = decide_kb_gate(rough_bias, regime_stress, n, calendar_ctx=cal_ctx, geo_ctx=geo_ctx)

    return {
        "rough_bias": round(rough_bias, 4),
        "news_bias": round(news_bias, 4),
        "regime_stress": round(regime_stress, 4),
        "gate_mode": mode,
        "gate_reason": reason,
        "n_rows": n,
        "n_macro": len(macro_df),
        "lookback_hours": lookback_hours,
        "calendar": cal_ctx,
        "geopolitical": geo_ctx,
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
        f"<b>Gate</b> (как nyse, по draft + календарь + GEO): <code>{_h(mode)}</code> · regime_stress=<code>{rs:.3f}</code>",
        f"<i>{_h(metrics['gate_reason'])}</i>",
    ]
    cal = metrics.get("calendar") or {}
    geo = metrics.get("geopolitical") or {}
    ah = int(cal.get("ahead_hours") or 72)
    if cal.get("n_rows"):
        cal_lines = "<br/>".join(_h(x) for x in (cal.get("lines") or [])[:6])
        lines.append(
            f"<b>Календарь KB</b> (вперёд до {ah}ч): записей <code>{int(cal.get('n_rows', 0))}</code>, "
            f"HIGH≤48ч: <code>{int(cal.get('high_48h', 0))}</code>, mega(CPI/NFP/FOMC/…): <code>{cal.get('mega_72h')}</code><br/>{cal_lines}"
        )
    else:
        lines.append(
            f"<b>Календарь KB</b> (вперёд до {ah}ч): нет строк в БД (cron <code>fetch_and_save_investing_calendar</code>, 429 или пусто)."
        )
    lines.append(
        f"<b>Геополитика в окне</b> (словарь как у LLM): совпадений <code>{int(geo.get('n_geo', 0))}</code>, "
        f"geo_stress=<code>{float(geo.get('geo_stress') or 0):.3f}</code> — {_h(geo.get('summary_short') or 'нет выдержек')}"
    )
    lines.append("")
    shown = 0
    for _, row in disp.iterrows():
        if shown >= top_n:
            break
        ch, _story = _channel_for_row(row, tv)
        bar = _bias_arrow(_row_bias_neg1(row.get("sentiment_score")))
        rb = _row_bias_neg1(row.get("sentiment_score"))
        title = str(row.get("content") or "")[:72]
        src = str(row.get("source") or "?")[:20]
        # Не :+.2f — при score≈0.5 row_bias микроскопический; шапка уже :+.4f.
        try:
            raw_s = float(row.get("sentiment_score"))
            raw_bit = f"{raw_s:.3f}" if not math.isnan(raw_s) else "—"
        except (TypeError, ValueError):
            raw_bit = "—"
        lines.append(
            f"{bar} <code>{_h(ch)}</code> <code>{_h(src)}</code> "
            f"<code>{rb:+.4f}</code> <code>s={_h(raw_bit)}</code> {_h(title)}"
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
при таком черновом сигнале.<br><br>
<b>Календарь и геополитика (как доп. ветки nyse)</b><br>
После REGIME проверяется <b>CALENDAR</b>: события Investing.com economic calendar в KB с <code>ts</code> в будущем
(горизонт в отчёте). FULL при «mega» (CPI/NFP/FOMC/ставки по тексту) в 72ч или при ≥2 HIGH в 48ч;
LITE-overlay при 1×HIGH в 48ч и тихом draft (|rough|&lt;t1), чтобы не падать в SKIP.<br>
Затем <b>GEO</b>: строки окна новостей, совпадающие с <code>services/geopolitical_prompt</code>. FULL при n_geo≥3 и
geo_stress (среднее |row_bias| по гео-строкам) ≥ regime_stress_min={_REGIME_STRESS_MIN}.
</div>
"""

    cal = metrics.get("calendar") or {}
    geo = metrics.get("geopolitical") or {}
    cal_body_rows = "".join(
        f"<tr><td class=\"mono\">{i + 1}</td><td>{_h(ln)}</td></tr>"
        for i, ln in enumerate(cal.get("lines") or [])
    )
    if not cal_body_rows:
        cal_body_rows = "<tr><td colspan=\"2\">Нет строк в горизонте (или календарь не пишется в KB).</td></tr>"
    geo_li = "".join(f"<li>{_h(x)}</li>" for x in (geo.get("lines") or [])[:12]) or "<li>Нет совпадений с гео-словарём.</li>"
    cal_geo_html = f"""
<div class="box">
<h2 style="margin-top:0;border:none;padding:0">Календарь (KB, вперёд)</h2>
<p class="meta">Горизонт до {int(cal.get('ahead_hours') or 72)}ч · записей: {int(cal.get('n_rows') or 0)} ·
HIGH≤48ч: {int(cal.get('high_48h') or 0)} · mega: {cal.get('mega_72h')}</p>
<table><thead><tr><th>#</th><th>Событие (UTC/как в KB)</th></tr></thead><tbody>{cal_body_rows}</tbody></table>
<h2 style="border:none;padding:0">Геополитика (окно новостей)</h2>
<p class="meta">n_geo={int(geo.get('n_geo') or 0)} · geo_stress={float(geo.get('geo_stress') or 0):.4f}</p>
<ul>{geo_li}</ul>
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
                # :.2f давало «0.00» при score≈0.5 — не видно микросдвига, из-за которого draft≠0
                raw_s = f"{raw:.4f}"
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
            f'<td class="mono {rcls}">{_bias_arrow(rb)} {rb:+.4f}</td>'
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
        + cal_geo_html
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
