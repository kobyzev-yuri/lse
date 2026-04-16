"""
Отчёт /news для LSE-бота: данные из knowledge_base + метрики как в nyse news pipeline.

- **rough_bias**: скаляр ``single_scalar_draft_bias(draft_impulse)`` после TF-IDF кластеризации REG
  и ``scored_from_news_articles`` / ``draft_impulse`` (см. ``services/nyse_news_pipeline.py``).
- **row_mean_bias**: среднее (sentiment_score−0.5)×2 по строкам окна — для справки, не для гейта.
- **news_bias**: взвешенное среднее как в AnalystAgent.calculate_weighted_sentiment.
- **regime_stress**: ``draft_impulse.regime_stress`` (REG с exp-затуханием), не среднее по MACRO-строкам.
- **Режим гейта**: как ``nyse`` ``decide_llm_mode`` + оверлей CALENDAR (KB); отдельной ветки GEO нет.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from services.nyse_news_pipeline import (
    DraftImpulse,
    NewsImpactChannel,
    classify_channel,
    run_kb_nyse_draft_bundle,
)


# Согласовано с nyse PROFILE_GAME5M (docs/calibration.md)
_T1 = 0.12
_T2 = 0.5
_MAX_ARTICLES_FULL = 8
_REGIME_STRESS_MIN = 0.05

# Uppercase substrings: MACRO rows that mention the company name (not the ticker) still rank above generic MACRO.
_DISPLAY_TEXT_NEEDLES: Dict[str, Tuple[str, ...]] = {
    "SNDK": ("SNDK", "SANDISK", "SAN DISK"),
}
_FULL_BIAS_MULT = 2.0


def _macro_body_matches_ticker(row: pd.Series, tk: str) -> bool:
    if str(row.get("ticker") or "").upper() not in ("MACRO", "US_MACRO"):
        return False
    body = f"{row.get('content') or ''} {row.get('insight') or ''}".upper()
    needles = (tk,) + _DISPLAY_TEXT_NEEDLES.get(tk, ())
    return any(n in body for n in needles)


def kb_row_relevant_to_ticker(row: pd.Series, ticker: str) -> bool:
    """Ticker column matches, or MACRO/US_MACRO row whose text mentions the symbol / display needles."""
    tk = (ticker or "").upper()
    t = str(row.get("ticker") or "").upper()
    if t == tk:
        return True
    return _macro_body_matches_ticker(row, tk)


def filter_relevant_kb_rows_for_ticker(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if df.empty:
        return df
    mask = df.apply(lambda r: kb_row_relevant_to_ticker(r, ticker), axis=1)
    return df[mask].reset_index(drop=True)


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
    """
    REG-контекст как в nyse: TF-IDF кластер REG → ``n_geo`` = число REG-тем после merge,
    ``geo_stress`` = ``draft_impulse.regime_stress`` (не словарь geopolitical_prompt).
    """
    from services.nyse_news_pipeline import summarize_kb_regime_geopolitical

    return summarize_kb_regime_geopolitical(news_df)


def _h(s: Any) -> str:
    return html.escape(str(s) if s is not None else "")


def _telegram_chat_html_sanitize(s: str) -> str:
    """
    Short /news messages use parse_mode=HTML. Some Telegram builds reject ``<br>`` / ``<br/>``;
    plain newlines are preserved and avoid entity-parser errors. Also strips any ``<br>`` from KB text.
    """
    return re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)


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


def order_kb_display_rows_for_ticker(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """
    Order rows for /news tables and Markdown news list: exact ticker first, then MACRO/US_MACRO
    whose text mentions the ticker, then generic MACRO; within each tier prefer rows with
    numeric sentiment_score, then newest COALESCE(ingested_at, ts). Does not affect bias metrics.
    """
    if df.empty:
        return df
    tk = (ticker or "").upper()

    def _tier(row: pd.Series) -> int:
        t = str(row.get("ticker") or "").upper()
        if t == tk:
            return 0
        if t in ("MACRO", "US_MACRO"):
            return 1 if _macro_body_matches_ticker(row, tk) else 2
        return 1

    def _has_score(row: pd.Series) -> int:
        ss = row.get("sentiment_score")
        if ss is None:
            return 0
        if isinstance(ss, float) and math.isnan(ss):
            return 0
        try:
            float(ss)
            return 1
        except (TypeError, ValueError):
            return 0

    out = df.copy()
    out["_tier"] = out.apply(_tier, axis=1)
    out["_has_score"] = out.apply(_has_score, axis=1)
    if "ingested_at" in out.columns:
        ia = pd.to_datetime(out["ingested_at"], errors="coerce")
        ts0 = pd.to_datetime(out["ts"], errors="coerce")
        out["_ts"] = ia.fillna(ts0)
    else:
        out["_ts"] = pd.to_datetime(out["ts"], errors="coerce")
    min_ts = pd.Timestamp("1970-01-01", tz="UTC")
    out["_ts"] = out["_ts"].fillna(min_ts)
    out = out.sort_values(by=["_tier", "_has_score", "_ts"], ascending=[True, False, False])
    return out.drop(columns=["_tier", "_has_score", "_ts"], errors="ignore").reset_index(drop=True)


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
    """Канал как ``nyse.pipeline.news.channels.classify_channel`` (REG / POL / INC)."""
    _ = ticker
    content = str(row.get("content") or row.get("title") or "").strip()
    lines = content.split("\n", 1)
    title = (lines[0][:200] if lines else "")[:200]
    summary = lines[1][:1200] if len(lines) > 1 else ""
    ch, _ = classify_channel(title, summary)
    if ch == NewsImpactChannel.REGIME:
        return "REG", "гео·энерго·режим"
    if ch == NewsImpactChannel.POLICY_RATES:
        return "POL", "ЦБ·ставки"
    return "INC", "эмитент·отрасль"


def decide_kb_gate(
    rough_bias: float,
    regime_stress: float,
    article_count: int,
    *,
    calendar_ctx: Optional[Dict[str, Any]] = None,
    geo_ctx: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """
    Как ``nyse.pipeline.news.gates.decide_llm_mode`` (PROFILE_GAME5M): draft_scalar, REGIME,
    плюс оверлей CALENDAR (KB). Параметр ``geo_ctx`` не влияет на решение (только для отчёта).
    """
    _ = geo_ctx
    cal = dict(calendar_ctx or {})
    regime_present = regime_stress > _REGIME_STRESS_MIN
    regime_rule_confidence = 0.85 if regime_present else 0.0
    ab = abs(rough_bias)
    mega = bool(cal.get("mega_72h"))
    high48 = int(cal.get("high_48h") or 0)
    cal_sm = str(cal.get("summary_short") or "")[:160]

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
            f"SKIP: |draft_bias|={ab:.3f} < t1={_T1:.3f}, нет REGIME/CALENDAR-триггеров для FULL "
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
        empty_di = asdict(DraftImpulse())
        empty_geo = summarize_geopolitical_context(pd.DataFrame())
        return {
            "rough_bias": 0.0,
            "row_mean_bias": 0.0,
            "news_bias": 0.0,
            "regime_stress": 0.0,
            "gate_mode": "SKIP",
            "gate_reason": "Нет статей KB.",
            "n_rows": 0,
            "n_macro": 0,
            "lookback_hours": lookback_hours,
            "draft_impulse": empty_di,
            "geopolitical": empty_geo,
            "relevant_n_rows": 0,
            "relevant_n_macro": 0,
            "relevant_rough_bias": 0.0,
            "relevant_row_mean_bias": 0.0,
            "relevant_news_bias": 0.0,
            "relevant_regime_stress": 0.0,
            "relevant_draft_impulse": empty_di,
            "relevant_geopolitical": empty_geo,
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
        empty_di = asdict(DraftImpulse())
        empty_geo = summarize_geopolitical_context(disp)
        return {
            "rough_bias": 0.0,
            "row_mean_bias": 0.0,
            "news_bias": 0.0,
            "regime_stress": 0.0,
            "gate_mode": "SKIP",
            "gate_reason": "Нет строк после фильтра шума.",
            "n_rows": 0,
            "n_macro": 0,
            "lookback_hours": lookback_hours,
            "draft_impulse": empty_di,
            "geopolitical": empty_geo,
            "relevant_n_rows": 0,
            "relevant_n_macro": 0,
            "relevant_rough_bias": 0.0,
            "relevant_row_mean_bias": 0.0,
            "relevant_news_bias": 0.0,
            "relevant_regime_stress": 0.0,
            "relevant_draft_impulse": empty_di,
            "relevant_geopolitical": empty_geo,
        }
    di, _rmeta, _scored, row_mean_bias, draft_scalar, _merged = run_kb_nyse_draft_bundle(disp)
    macro_df = disp[disp["ticker"].isin(["MACRO", "US_MACRO"])]
    regime_stress = float(di.regime_stress)
    rough_bias = float(draft_scalar)
    news_bias = weighted_news_bias_neg1_from_kb_df(disp, ticker)
    geo_ctx = summarize_geopolitical_context(disp)
    mode, reason = decide_kb_gate(rough_bias, regime_stress, n, calendar_ctx=None, geo_ctx=geo_ctx)

    disp_rel = filter_relevant_kb_rows_for_ticker(disp, ticker)
    n_rel = len(disp_rel)
    n_rel_macro = (
        int(disp_rel["ticker"].isin(["MACRO", "US_MACRO"]).sum()) if n_rel and "ticker" in disp_rel.columns else 0
    )
    rel_di_d: Dict[str, Any] = asdict(DraftImpulse())
    rel_geo: Dict[str, Any] = summarize_geopolitical_context(pd.DataFrame())
    rel_rough = 0.0
    rel_row_mean = 0.0
    rel_news = 0.0
    rel_rs = 0.0
    if n_rel > 0:
        di_r, _, _, rel_row_mean, rel_draft, _ = run_kb_nyse_draft_bundle(disp_rel)
        rel_di_d = asdict(di_r)
        rel_rough = float(rel_draft)
        rel_rs = float(di_r.regime_stress)
        rel_news = float(weighted_news_bias_neg1_from_kb_df(disp_rel, ticker))
        rel_geo = summarize_geopolitical_context(disp_rel)

    return {
        "rough_bias": round(float(rough_bias), 4),
        "row_mean_bias": round(float(row_mean_bias), 4),
        "news_bias": round(float(news_bias), 4),
        "regime_stress": round(float(regime_stress), 4),
        "gate_mode": mode,
        "gate_reason": reason,
        "n_rows": n,
        "n_macro": len(macro_df),
        "lookback_hours": lookback_hours,
        "draft_impulse": asdict(di),
        "geopolitical": geo_ctx,
        "relevant_n_rows": n_rel,
        "relevant_n_macro": n_rel_macro,
        "relevant_rough_bias": round(rel_rough, 4),
        "relevant_row_mean_bias": round(rel_row_mean, 4),
        "relevant_news_bias": round(rel_news, 4),
        "relevant_regime_stress": round(rel_rs, 4),
        "relevant_draft_impulse": rel_di_d,
        "relevant_geopolitical": rel_geo,
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
    Метрики как nyse news pipeline (REG TF-IDF кластер, draft_impulse, single_scalar_draft_bias),
    news.bias по AnalystAgent, календарь KB, гейт как decide_llm_mode + оверлей календаря.
    """
    ndf = news_df if news_df is not None else pd.DataFrame()
    cal_df = fetch_kb_macro_calendar_upcoming(engine, calendar_ahead_hours) if engine is not None else pd.DataFrame()
    cal_ctx = summarize_calendar_context(cal_df, ahead_hours=calendar_ahead_hours)

    disp = filter_kb_display_rows(ndf)
    n = len(disp)
    geo_ctx = summarize_geopolitical_context(disp)

    if n == 0:
        mode, reason = decide_kb_gate(0.0, 0.0, 0, calendar_ctx=cal_ctx, geo_ctx=geo_ctx)
        empty_di = asdict(DraftImpulse())
        empty_geo = summarize_geopolitical_context(pd.DataFrame())
        return {
            "rough_bias": 0.0,
            "row_mean_bias": 0.0,
            "news_bias": 0.0,
            "regime_stress": 0.0,
            "gate_mode": mode,
            "gate_reason": reason,
            "n_rows": 0,
            "n_macro": 0,
            "lookback_hours": lookback_hours,
            "calendar": cal_ctx,
            "geopolitical": geo_ctx,
            "draft_impulse": empty_di,
            "relevant_n_rows": 0,
            "relevant_n_macro": 0,
            "relevant_rough_bias": 0.0,
            "relevant_row_mean_bias": 0.0,
            "relevant_news_bias": 0.0,
            "relevant_regime_stress": 0.0,
            "relevant_draft_impulse": empty_di,
            "relevant_geopolitical": empty_geo,
        }

    di, _rmeta, _scored, row_mean_bias, draft_scalar, _merged = run_kb_nyse_draft_bundle(disp)
    macro_df = disp[disp["ticker"].isin(["MACRO", "US_MACRO"])]
    regime_stress = float(di.regime_stress)
    rough_bias = float(draft_scalar)

    news_bias = float(analyst.calculate_weighted_sentiment(disp, ticker))

    mode, reason = decide_kb_gate(rough_bias, regime_stress, n, calendar_ctx=cal_ctx, geo_ctx=geo_ctx)

    disp_rel = filter_relevant_kb_rows_for_ticker(disp, ticker)
    n_rel = len(disp_rel)
    n_rel_macro = (
        int(disp_rel["ticker"].isin(["MACRO", "US_MACRO"]).sum()) if n_rel and "ticker" in disp_rel.columns else 0
    )
    rel_di_d: Dict[str, Any] = asdict(DraftImpulse())
    rel_geo: Dict[str, Any] = summarize_geopolitical_context(pd.DataFrame())
    rel_rough = 0.0
    rel_row_mean = 0.0
    rel_news = 0.0
    rel_rs = 0.0
    if n_rel > 0:
        di_r, _, _, rel_row_mean, rel_draft, _ = run_kb_nyse_draft_bundle(disp_rel)
        rel_di_d = asdict(di_r)
        rel_rough = float(rel_draft)
        rel_rs = float(di_r.regime_stress)
        rel_news = float(analyst.calculate_weighted_sentiment(disp_rel, ticker))
        rel_geo = summarize_geopolitical_context(disp_rel)

    return {
        "rough_bias": round(rough_bias, 4),
        "row_mean_bias": round(row_mean_bias, 4),
        "news_bias": round(news_bias, 4),
        "regime_stress": round(regime_stress, 4),
        "gate_mode": mode,
        "gate_reason": reason,
        "n_rows": n,
        "n_macro": len(macro_df),
        "lookback_hours": lookback_hours,
        "calendar": cal_ctx,
        "geopolitical": geo_ctx,
        "draft_impulse": asdict(di),
        "relevant_n_rows": n_rel,
        "relevant_n_macro": n_rel_macro,
        "relevant_rough_bias": round(rel_rough, 4),
        "relevant_row_mean_bias": round(rel_row_mean, 4),
        "relevant_news_bias": round(rel_news, 4),
        "relevant_regime_stress": round(rel_rs, 4),
        "relevant_draft_impulse": rel_di_d,
        "relevant_geopolitical": rel_geo,
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
    disp_all = order_kb_display_rows_for_ticker(filter_kb_display_rows(news_df), tv)
    disp_rel = order_kb_display_rows_for_ticker(filter_relevant_kb_rows_for_ticker(disp_all, tv), tv)
    rough = metrics["rough_bias"]
    row_mean = float(metrics.get("row_mean_bias") or 0.0)
    nb = metrics["news_bias"]
    mode = metrics["gate_mode"]
    rs = metrics["regime_stress"]
    nrel = int(metrics.get("relevant_n_rows") or 0)
    dipr = metrics.get("relevant_draft_impulse") if isinstance(metrics.get("relevant_draft_impulse"), dict) else {}

    lines = [
        f"📰 <b>{_h(tv)}</b> — KB, последние <b>{metrics['lookback_hours']}</b> ч",
    ]
    if nrel > 0:
        rrb = float(metrics.get("relevant_rough_bias") or 0.0)
        rnb = float(metrics.get("relevant_news_bias") or 0.0)
        rrm = float(metrics.get("relevant_row_mean_bias") or 0.0)
        rrs = float(metrics.get("relevant_regime_stress") or 0.0)
        nm = int(metrics.get("relevant_n_macro") or 0)
        lines.append(
            f"<b>Сигнал по релевантным</b> (строка с <code>{_h(tv)}</code> или MACRO с упом. в тексте): "
            f"n=<code>{nrel}</code> (MACRO из них <code>{nm}</code>). "
            f"<code>draft_bias</code>=<code>{rrb:+.4f}</code> {_bias_arrow(rrb)}, "
            f"<code>news.bias</code>=<code>{rnb:+.4f}</code> {_bias_arrow(rnb)}, "
            f"row_mean=<code>{rrm:+.4f}</code>, <code>regime_stress</code>=<code>{rrs:.3f}</code>. "
            f"REG/INC/POL: <code>{int(dipr.get('articles_regime', 0))}</code>/"
            f"<code>{int(dipr.get('articles_incremental', 0))}</code>/<code>{int(dipr.get('articles_policy', 0))}</code>."
        )
    else:
        lines.append(
            f"<b>Сигнал по релевантным</b>: нет строк (ни <code>{_h(tv)}</code>, ни MACRO с упоминанием символа в тексте)."
        )
    lines.extend(
        [
            f"<b>Полное окно</b> (все KB+MACRO — <b>гейт</b> считается здесь): "
            f"<code>draft_bias</code>=<code>{rough:+.4f}</code> {_bias_arrow(rough)}, "
            f"<code>news.bias</code>=<code>{nb:+.4f}</code> {_bias_arrow(nb)}, "
            f"row_mean=<code>{row_mean:+.4f}</code>, <code>regime_stress</code>=<code>{rs:.3f}</code>.",
            f"<b>Gate</b>: <code>{_h(mode)}</code> · {_h(metrics['gate_reason'])}",
        ]
    )
    cal = metrics.get("calendar") or {}
    geo = metrics.get("geopolitical") or {}
    ah = int(cal.get("ahead_hours") or 72)
    if cal.get("n_rows"):
        cal_lines = "\n".join(_h(x) for x in (cal.get("lines") or [])[:6])
        lines.append(
            f"<b>Календарь KB</b> (вперёд до {ah}ч): записей <code>{int(cal.get('n_rows', 0))}</code>, "
            f"HIGH≤48ч: <code>{int(cal.get('high_48h', 0))}</code>, mega(CPI/NFP/FOMC/…): <code>{cal.get('mega_72h')}</code>\n{cal_lines}"
        )
    else:
        lines.append(
            f"<b>Календарь KB</b> (вперёд до {ah}ч): нет строк в БД (cron <code>fetch_and_save_investing_calendar</code>, 429 или пусто)."
        )
    geo_rel = metrics.get("relevant_geopolitical") if isinstance(metrics.get("relevant_geopolitical"), dict) else {}
    rnote_r = str(geo_rel.get("regime_cluster_note") or "")
    if nrel > 0:
        lines.append(
            f"<b>REG · релевантные строки</b>: тем <code>{int(geo_rel.get('n_geo', 0))}</code>, "
            f"stress=<code>{float(geo_rel.get('geo_stress') or 0):.3f}</code> — {_h(geo_rel.get('summary_short') or '—')}"
            + (f"\n<i>{_h(rnote_r)}</i>" if rnote_r else "")
        )
    rnote = str(geo.get("regime_cluster_note") or "")
    lines.append(
        f"<b>REG · полное окно</b>: тем <code>{int(geo.get('n_geo', 0))}</code>, "
        f"<code>draft_impulse.regime_stress</code>=<code>{float(geo.get('geo_stress') or 0):.3f}</code> — "
        f"{_h(geo.get('summary_short') or 'нет выдержек')}"
        + (f"\n<i>{_h(rnote)}</i>" if rnote else "")
    )
    lines.append("")
    lines.append(f"<b>Топ строк (только релевантные к {_h(tv)})</b>")
    shown = 0
    for _, row in disp_rel.iterrows():
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
    return _telegram_chat_html_sanitize("\n".join(lines))


def _kb_news_article_table_rows(df: pd.DataFrame, tv: str, top_n: int) -> str:
    """HTML <tr>… rows for full /news document table."""
    parts: List[str] = []
    shown = 0
    for _, row in df.iterrows():
        if shown >= top_n:
            break
        ch, story = _channel_for_row(row, tv)
        rb = _row_bias_neg1(row.get("sentiment_score"))
        try:
            raw = float(row.get("sentiment_score"))
            if math.isnan(raw):
                raw_s = "—"
            else:
                raw_s = f"{raw:.4f}"
        except (TypeError, ValueError):
            raw_s = "—"
        rcls = "pos" if rb > 0.05 else ("neg" if rb < -0.05 else "neu")
        ts_row = row.get("ts")
        ts_str = ts_row.strftime("%Y-%m-%d %H:%M") if hasattr(ts_row, "strftime") else str(ts_row)
        title = str(row.get("content") or "")[:200]
        summ = str(row.get("insight") or "")[:120]
        parts.append(
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
    return "".join(parts)


def build_kb_news_full_html(
    ticker: str,
    news_df: pd.DataFrame,
    metrics: dict,
    top_n: int,
) -> str:
    """Полный HTML-отчёт с формулами и таблицей."""
    tv = ticker.upper()
    disp_all = order_kb_display_rows_for_ticker(filter_kb_display_rows(news_df), tv)
    disp_rel = order_kb_display_rows_for_ticker(filter_relevant_kb_rows_for_ticker(disp_all, tv), tv)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rough = metrics["rough_bias"]
    nb = metrics["news_bias"]
    mode = metrics["gate_mode"]
    nrel = int(metrics.get("relevant_n_rows") or 0)
    dipr = metrics.get("relevant_draft_impulse") if isinstance(metrics.get("relevant_draft_impulse"), dict) else {}

    di_html = ""
    dip = metrics.get("draft_impulse")
    if isinstance(dip, dict) and dip:
        di_html = (
            f"<br><b>DraftImpulse</b> (полное окно): inc_mean=<code>{dip.get('draft_bias_incremental', 0):.4f}</code>, "
            f"regime_stress=<code>{dip.get('regime_stress', 0):.4f}</code>, policy_stress=<code>{dip.get('policy_stress', 0):.4f}</code>, "
            f"articles REG/INC/POL = <code>{dip.get('articles_regime', 0)}</code>/"
            f"<code>{dip.get('articles_incremental', 0)}</code>/<code>{dip.get('articles_policy', 0)}</code>."
        )
    di_html_rel = ""
    if nrel > 0 and dipr:
        di_html_rel = (
            f"<b>DraftImpulse</b> (только релевантные строки): inc_mean=<code>{dipr.get('draft_bias_incremental', 0):.4f}</code>, "
            f"regime_stress=<code>{dipr.get('regime_stress', 0):.4f}</code>, policy_stress=<code>{dipr.get('policy_stress', 0):.4f}</code>, "
            f"REG/INC/POL = <code>{dipr.get('articles_regime', 0)}</code>/"
            f"<code>{dipr.get('articles_incremental', 0)}</code>/<code>{dipr.get('articles_policy', 0)}</code>.<br><br>"
        )
    explain = f"""
<div class="box">
<p class="meta"><b>Две шкалы:</b> «релевантные» = строка с тикером <code>{_h(tv)}</code> или MACRO/US_MACRO, где в тексте есть символ / алиас (см. <code>_DISPLAY_TEXT_NEEDLES</code> в коде). На них считается отдельный <code>draft_bias</code> и <code>news.bias</code> — ближе к сигналу по самому имени. <b>Гейт</b> (SKIP/LITE/FULL) по-прежнему от <b>полного окна</b> KB+MACRO и календаря — чтобы не игнорировать общий режим и CPI/FOMC.</p>
<b>Как в nyse: каналы → TF-IDF кластер REG → scored → draft_impulse → single_scalar_draft_bias</b><br>
Строки KB после фильтра шума преобразуются в «статьи» с <code>cheap_sentiment = (sentiment_score−0.5)×2</code>.
Канал <b>REG / POL / INC</b> — правила из <code>nyse/pipeline/news/channels.py</code> (портаж в
<code>services/nyse_news_pipeline.py</code>). Статьи REG кластеризуются по косинусу TF-IDF
(<code>NYSE_REGIME_CLUSTER_THRESHOLD</code>, по умолчанию 0.88), один представитель на кластер —
как <code>apply_regime_cluster_for_draft</code>. Затем <code>draft_impulse</code> с half-life 12ч
и <b>draft_bias</b> для гейта = <code>single_scalar_draft_bias</code> (incremental − 0.15×regime_stress − 0.1×policy_stress).<br>
{di_html_rel}<b>row_mean_bias</b> (полное окно) — среднее <code>cheap_sentiment</code> по всем строкам окна (справочно, не для гейта).{di_html}<br><br>
<b>Как считается news.bias</b><br>
<code>AnalystAgent.calculate_weighted_sentiment</code>: вес 2.0 если тикер строки = тикеру или тикер в тексте, иначе 1.0.<br><br>
<b>Почему режим {mode}</b><br>
{_h(metrics["gate_reason"])}<br>
Пороги как <code>decide_llm_mode</code> / <code>PROFILE_GAME5M</code>: t1={_T1}, t1×2={_T1*_FULL_BIAS_MULT},
t2={_T2}, max_articles_full={_MAX_ARTICLES_FULL}, regime_stress_min={_REGIME_STRESS_MIN}.<br><br>
<b>Календарь KB</b> (оверлей LSE): события Investing economic calendar в горизонте. Отдельной ветки GEO в гейте нет —
REG уже учтён в <code>regime_stress</code> и <code>draft_impulse</code>, как в nyse.
</div>
"""

    cal = metrics.get("calendar") or {}
    geo = metrics.get("geopolitical") or {}
    geo_rel = metrics.get("relevant_geopolitical") if isinstance(metrics.get("relevant_geopolitical"), dict) else {}
    cal_body_rows = "".join(
        f"<tr><td class=\"mono\">{i + 1}</td><td>{_h(ln)}</td></tr>"
        for i, ln in enumerate(cal.get("lines") or [])
    )
    if not cal_body_rows:
        cal_body_rows = "<tr><td colspan=\"2\">Нет строк в горизонте (или календарь не пишется в KB).</td></tr>"
    geo_li_rel = "".join(f"<li>{_h(x)}</li>" for x in (geo_rel.get("lines") or [])[:12]) or "<li>Нет REG-выдержек среди релевантных.</li>"
    rnote_rel = str(geo_rel.get("regime_cluster_note") or "")
    geo_li = "".join(f"<li>{_h(x)}</li>" for x in (geo.get("lines") or [])[:12]) or "<li>Нет REG-выдержек после кластера.</li>"
    rnote = str(geo.get("regime_cluster_note") or "")
    cal_only_html = f"""
<div class="box">
<h2 style="margin-top:0;border:none;padding:0">Календарь (KB, вперёд)</h2>
<p class="meta">Горизонт до {int(cal.get('ahead_hours') or 72)}ч · записей: {int(cal.get('n_rows') or 0)} ·
HIGH≤48ч: {int(cal.get('high_48h') or 0)} · mega: {cal.get('mega_72h')}</p>
<table><thead><tr><th>#</th><th>Событие (UTC/как в KB)</th></tr></thead><tbody>{cal_body_rows}</tbody></table>
</div>
"""
    reg_rel_html = ""
    if nrel > 0:
        reg_rel_html = f"""
<div class="box">
<h2 style="margin-top:0;border:none;padding:0">REG / режим — только релевантные строки</h2>
<p class="meta">REG-тем после merge: {int(geo_rel.get('n_geo') or 0)} · regime_stress = {float(geo_rel.get('geo_stress') or 0):.4f}</p>
<p class="meta">{_h(rnote_rel) if rnote_rel else "—"}</p>
<ul>{geo_li_rel}</ul>
</div>
"""
    reg_full_html = f"""
<div class="box">
<h2 style="margin-top:0;border:none;padding:0">REG / режим — полное окно (все KB+MACRO)</h2>
<p class="meta">REG-тем после merge: {int(geo.get('n_geo') or 0)} · regime_stress (draft_impulse REG) = {float(geo.get('geo_stress') or 0):.4f}</p>
<p class="meta">{_h(rnote) if rnote else "Кластеризация не применялась (меньше 2 REG-строк или выключено)."}</p>
<ul>{geo_li}</ul>
</div>
"""

    rows_html_rel = _kb_news_article_table_rows(disp_rel, tv, top_n)
    other_n = max(1, min(8, top_n))
    mask_other = ~disp_all.apply(lambda r: kb_row_relevant_to_ticker(r, tv), axis=1)
    disp_other = disp_all[mask_other].reset_index(drop=True)
    rows_html_other = _kb_news_article_table_rows(disp_other, tv, other_n) if not disp_other.empty else ""

    rel_rrb = float(metrics.get("relevant_rough_bias") or 0.0)
    rel_nb = float(metrics.get("relevant_news_bias") or 0.0)
    rel_rm = float(metrics.get("relevant_row_mean_bias") or 0.0)
    rel_rs = float(metrics.get("relevant_regime_stress") or 0.0)
    rel_macro = int(metrics.get("relevant_n_macro") or 0)
    summary_rel_rows = ""
    if nrel > 0:
        summary_rel_rows = f"""
<tr><td colspan="2"><b>Только релевантные к {_h(tv)}</b> (n={nrel}, из них MACRO {rel_macro})</td></tr>
<tr><td>draft_bias</td><td class="mono">{rel_rrb:+.4f}</td></tr>
<tr><td>news.bias</td><td class="mono">{rel_nb:+.4f}</td></tr>
<tr><td>row_mean_bias</td><td class="mono">{rel_rm:+.4f}</td></tr>
<tr><td>regime_stress</td><td class="mono">{rel_rs:.4f}</td></tr>
<tr><td colspan="2"><b>Полное окно (гейт)</b></td></tr>
"""
    else:
        summary_rel_rows = "<tr><td colspan=\"2\"><b>Релевантных строк нет</b> — см. только полное окно ниже.</td></tr>"

    summary_tbl = f"""
<table>
<thead><tr><th>Метрика</th><th>Значение</th></tr></thead>
<tbody>
{summary_rel_rows}
<tr><td>draft_bias</td><td class="mono">{rough:+.4f}</td></tr>
<tr><td>news.bias</td><td class="mono">{nb:+.4f}</td></tr>
<tr><td>regime_stress (<code>draft_impulse</code>)</td><td class="mono">{metrics['regime_stress']:.4f}</td></tr>
<tr><td>Gate (аналог nyse)</td><td><b>{_h(mode)}</b></td></tr>
<tr><td>Строк в отчёте</td><td>{metrics['n_rows']} (макро: {metrics['n_macro']})</td></tr>
</tbody></table>
"""

    articles_rel_block = (
        f"<h2>Статьи — релевантные к {_h(tv)} (топ {top_n})</h2>"
        + "<table><thead><tr>"
        "<th>#</th><th>Ch</th><th>Тип</th><th>Source</th><th>event_type</th><th>ticker</th>"
        "<th>row_bias</th><th>score 0–1</th><th>Время</th><th>Текст / insight</th>"
        "</tr></thead><tbody>"
        + (rows_html_rel or "<tr><td colspan=\"10\">Нет релевантных строк.</td></tr>")
        + "</tbody></table>"
    )
    articles_other_block = ""
    if rows_html_other:
        articles_other_block = (
            f"<h2>Прочий MACRO в окне (справочно, топ {other_n})</h2>"
            + "<table><thead><tr>"
            "<th>#</th><th>Ch</th><th>Тип</th><th>Source</th><th>event_type</th><th>ticker</th>"
            "<th>row_bias</th><th>score 0–1</th><th>Время</th><th>Текст / insight</th>"
            "</tr></thead><tbody>"
            + rows_html_other
            + "</tbody></table>"
        )

    body = (
        f"<h1>📰 {_h(tv)} — новости knowledge_base</h1>"
        f'<p class="meta">{_h(ts)} · окно {metrics["lookback_hours"]} ч · релевантные до {top_n} строк + прочий MACRO до {other_n}</p>'
        + summary_tbl
        + explain
        + cal_only_html
        + reg_rel_html
        + reg_full_html
        + articles_rel_block
        + articles_other_block
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
