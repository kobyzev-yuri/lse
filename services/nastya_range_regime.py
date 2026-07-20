"""
Отчёт «коридоры / боковик / bias» для анализа Насти (~46 стоков).

Excel — ручные якоря; OHLCV/RVOL/NDX/VIX — авто из quotes (fallback yfinance).
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
from sqlalchemy import bindparam, text

from config_loader import get_config_value

logger = logging.getLogger(__name__)

DEFAULT_TICKERS = ("META", "AMKR", "ARM")
# Bump when comment/UI payload shape changes so stale JSON cache is ignored.
REPORT_SCHEMA_VERSION = 3
METHOD_RU = """
Цель (запрос Насти)
• Широкие границы движения: где ориентировочно «пол» и «потолок» (не точка входа).
• Боковик: насколько широкая зона / как долго roughly держится (Age≈, width60d).
• Bias: куда вероятнее выход из текущей зоны (up/down/neutral) — эвристика, не прогноз цены.
• RVOL: проверка гипотезы, что на РАЗВОРОТАХ тренда и на ВЫХОДАХ из боковика объём
  аномально растёт или падает. Текущий столник показывает RVOL «сегодня» как индикатор;
  сама гипотеза проверяется на истории дат разворота, а не одним спокойным снимком.

Что на экране
• Коридор floor→ceil = якоря Excel + локальный 20d channel.
• Regime: uptrend / downtrend / range / transition (локальный канал Насти).
• ML trend: режим тренда 20d нашей portfolio-сетки (melt_up / trend_up / neutral / breakdown)
  — отдельный слой, не замена bias_exit.
• NDX + VIX: глобальный фон (не сигнал купить/продать тикер).
• Кнопка LLM по тикеру: сначала краткий «Итог» (что ждать по движению в ближайшее время),
  затем разбор по пунктам Насти; может глянуть карточку портфеля и дневной график ~6м
  с маркерами earnings (X) и сильных новостей (▲/▼, топ‑5/мес).

Якоря Excel
• Min low (июн.2022–дек.2023) ×10 — зона ×10 к минимуму 2022 (rerating / замедление).
• UPSIDE = (Min low ×10) / Max high 52w — ×10-зона относительно годового максимума (≈1.0 → у зоны).
• Потенциал от close = (Min low ×10) / Close — та же ×10-зона относительно текущей цены (не дубль UPSIDE).
• %%margin 17%/25% = цель (+17%/+25%) / (Drop 30% от max high).
• Drop 20/25/30%, цели +17/+25%, июльский гребень — ориентиры пола/потолка.

По умолчанию: META (гиперскейлер), AMKR, ARM.
Excel — вручную при новой дате close; котировки/RVOL/NDX/VIX — авто.
""".strip()

LLM_SYSTEM_RU = """
Ты помогаешь аналитику Насте разобрать один тикер по отчёту «коридоры / боковик».
Отвечай по-русски, коротко, живым языком. Без торговых приказов BUY/SELL и без точной цены входа.

Структура ответа строго так:

Итог: 1–2 предложения — что в ближайшие дни/недели скорее ждать по движению
(продолжение боковика / отскок от пола / пробой вниз / вялый дрейф вверх и т.п.).
Это рабочая гипотеза по коридору+графику+ML trend, не гарантия.

Затем подробный разбор по пунктам:
1) Пол и потолок — где ориентировочные границы и где цена внутри коридора.
2) Боковик — есть ли (range vs transition/тренд), насколько зона широкая, сколько уже держится (Age).
   Не выдумывай дату конца боковика.
3) Куда вероятнее выход — bias up/down/neutral и почему (позиция в коридоре / режим).
4) Объём (RVOL) — только как сегодняшний снимок; не пиши, что гипотеза разворота
   «подтверждена» или «опровергнута» одним днём.
5) Тренд сетки ML portfolio (20d) — как соотносится с коридором и bias; без порогов конфигов,
   без jargon вроде thr/late_chase_min.
6) История новостей/earnings на графике — 1–2 аналогии: что было после похожих маркеров
   раньше и что это может значить сейчас (с оговоркой, что аналогия не гарантия).

Можно упомянуть 1–2 бытовых факта с карточки портфеля (решение стратегии, день ±%),
если они помогают. Не перечисляй RSI/SMA/волатильность списком.

Если приложена картинка дневного графика (~6 месяцев) — обязательно взгляни на форму
цены (полка, пила, V, импульс вниз/вверх) и свяжи это с боковиком/Age/bias/ML trend и с блоком «Итог».
На графике могут быть маркеры: зелёный/красный X = earnings (beat/miss по EPS),
зелёный ▲ / красный ▼ = сильные новости (не нейтральные; до 5/мес).

Важно про историю новостей/earnings:
• посмотри, как цена вела себя ПОСЛЕ прошлых маркеров на этом же графике (и в JSON
  маркеров — поля ret_5d/ret_10d, если есть);
• сделай короткую разумную аналогию: похожий тип события раньше → похожий/иной отклик сейчас;
• не утверждай причинность наверняка; 1–2 аналогии достаточно, без пересказа каждой новости.
Не пересказывай каждую свечу.
""".strip()


NEWS_SENTIMENT_STRONG_LO = 0.35
NEWS_SENTIMENT_STRONG_HI = 0.65
NEWS_TOP_PER_MONTH = 5


def _as_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return pd.Timestamp(v).date()
    except Exception:
        return None


def classify_earnings_tone(eps_actual: Any, eps_estimate: Any) -> Optional[str]:
    """good/bad/neutral by EPS beat/miss; None if cannot classify."""
    a = _f_num(eps_actual)
    e = _f_num(eps_estimate)
    if a is None or e is None:
        return None
    if a > e:
        return "good"
    if a < e:
        return "bad"
    return "neutral"


def select_top_news_per_month(
    items: Sequence[Dict[str, Any]],
    *,
    top_n: int = NEWS_TOP_PER_MONTH,
) -> List[Dict[str, Any]]:
    """Keep strongest non-neutral news, at most top_n per calendar month."""
    by_month: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    for it in items:
        d = _as_date(it.get("date"))
        sc = _f_num(it.get("sentiment_score"))
        if d is None or sc is None:
            continue
        if NEWS_SENTIMENT_STRONG_LO < sc < NEWS_SENTIMENT_STRONG_HI:
            continue
        key = (d.year, d.month)
        by_month.setdefault(key, []).append(dict(it))
    out: List[Dict[str, Any]] = []
    for key in sorted(by_month.keys()):
        bucket = by_month[key]
        bucket.sort(key=lambda x: abs(float(x.get("sentiment_score") or 0.5) - 0.5), reverse=True)
        out.extend(bucket[: max(0, int(top_n))])
    out.sort(key=lambda x: _as_date(x.get("date")) or date.min)
    return out


def load_chart_event_markers(
    ticker: str,
    *,
    start: date,
    end: date,
    engine=None,
    news_top_per_month: int = NEWS_TOP_PER_MONTH,
) -> Dict[str, List[Dict[str, Any]]]:
    """Earnings (EPS beat/miss) + strongest non-neutral KB news for chart overlays."""
    t = (ticker or "").strip().upper()
    empty: Dict[str, List[Dict[str, Any]]] = {"earnings": [], "news": []}
    if not t or start > end:
        return empty
    eng = engine
    if eng is None:
        try:
            from report_generator import get_engine

            eng = get_engine()
        except Exception:
            eng = None
    if eng is None:
        return empty

    earnings: List[Dict[str, Any]] = []
    news_raw: List[Dict[str, Any]] = []
    try:
        with eng.connect() as conn:
            erows = conn.execute(
                text(
                    """
                    SELECT kb.ts::date AS d,
                           ed.eps_actual, ed.eps_estimate,
                           ed.revenue_actual, ed.revenue_estimate
                    FROM earnings_event_detail ed
                    JOIN knowledge_base kb ON kb.id = ed.knowledge_base_id
                    WHERE upper(coalesce(kb.symbol, kb.ticker)) = :t
                      AND kb.ts::date >= :d0 AND kb.ts::date <= :d1
                    ORDER BY kb.ts ASC
                    """
                ),
                {"t": t, "d0": start, "d1": end},
            ).mappings().all()
            for r in erows:
                tone = classify_earnings_tone(r.get("eps_actual"), r.get("eps_estimate"))
                if tone is None:
                    continue
                earnings.append(
                    {
                        "date": _as_date(r.get("d")),
                        "kind": "earnings",
                        "tone": tone,
                        "eps_actual": _f_num(r.get("eps_actual")),
                        "eps_estimate": _f_num(r.get("eps_estimate")),
                        "label_ru": (
                            "earnings beat"
                            if tone == "good"
                            else ("earnings miss" if tone == "bad" else "earnings inline")
                        ),
                    }
                )
            nrows = conn.execute(
                text(
                    """
                    SELECT ts::date AS d, sentiment_score,
                           left(coalesce(insight, content), 100) AS title
                    FROM knowledge_base
                    WHERE upper(coalesce(ticker, symbol)) = :t
                      AND sentiment_score IS NOT NULL
                      AND ts::date >= :d0 AND ts::date <= :d1
                      AND (sentiment_score <= :lo OR sentiment_score >= :hi)
                    ORDER BY ts ASC
                    """
                ),
                {
                    "t": t,
                    "d0": start,
                    "d1": end,
                    "lo": NEWS_SENTIMENT_STRONG_LO,
                    "hi": NEWS_SENTIMENT_STRONG_HI,
                },
            ).mappings().all()
            for r in nrows:
                sc = _f_num(r.get("sentiment_score"))
                if sc is None:
                    continue
                tone = "good" if sc >= NEWS_SENTIMENT_STRONG_HI else "bad"
                news_raw.append(
                    {
                        "date": _as_date(r.get("d")),
                        "kind": "news",
                        "tone": tone,
                        "sentiment_score": sc,
                        "title": (str(r.get("title") or "").strip() or None),
                        "label_ru": "news+" if tone == "good" else "news−",
                    }
                )
    except Exception as e:
        logger.debug("load_chart_event_markers %s: %s", t, e)
        return empty

    news = select_top_news_per_month(news_raw, top_n=news_top_per_month)
    return {"earnings": earnings, "news": news}


def _price_on_or_before(close: pd.Series, d: date) -> Optional[float]:
    if close is None or close.empty:
        return None
    idx = pd.to_datetime(close.index)
    target = pd.Timestamp(d)
    mask = idx <= target
    try:
        import numpy as np

        positions = np.flatnonzero(np.asarray(mask))
    except Exception:
        positions = [i for i, ok in enumerate(mask) if bool(ok)]
    if len(positions):
        return float(close.iloc[int(positions[-1])])
    return float(close.iloc[0])


def _forward_return_pct(close: pd.Series, d: date, *, horizon_bars: int) -> Optional[float]:
    """Close-to-close % move over ~horizon trading bars after event date."""
    if close is None or close.empty or horizon_bars <= 0:
        return None
    idx = pd.to_datetime(close.index)
    target = pd.Timestamp(d)
    try:
        import numpy as np

        positions = np.flatnonzero(np.asarray(idx >= target))
    except Exception:
        positions = [i for i, ts in enumerate(idx) if ts >= target]
    if not len(positions):
        return None
    i0 = int(positions[0])
    i1 = i0 + int(horizon_bars)
    if i1 >= len(close):
        return None
    p0 = float(close.iloc[i0])
    p1 = float(close.iloc[i1])
    if p0 <= 0:
        return None
    return round((p1 / p0 - 1.0) * 100.0, 2)


def enrich_markers_with_forward_returns(
    close: pd.Series,
    markers: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Attach post-event 5d/10d % moves so LLM can analogize historically."""
    out: Dict[str, List[Dict[str, Any]]] = {"earnings": [], "news": []}
    for key in ("earnings", "news"):
        for raw in markers.get(key) or []:
            m = dict(raw)
            dd = _as_date(m.get("date"))
            if dd is not None:
                m["ret_5d_pct"] = _forward_return_pct(close, dd, horizon_bars=5)
                m["ret_10d_pct"] = _forward_return_pct(close, dd, horizon_bars=10)
            out[key].append(m)
    return out


def split_nastya_llm_explanation(text: str) -> Dict[str, str]:
    """
    Выделяет краткий «Итог» и остальной подробный разбор.
    Ожидает блок, начинающийся с «Итог: …».
    """
    raw = (text or "").strip()
    if not raw:
        return {"summary_ru": "", "details_ru": "", "explanation_ru": ""}

    lines = raw.splitlines()
    summary_parts: List[str] = []
    details_parts: List[str] = []
    mode = "pre"

    def _strip_итог_prefix(s: str) -> str:
        low = s.lower()
        for prefix in ("итог:", "итог —", "итог -", "итог–"):
            if low.startswith(prefix):
                return s[len(prefix) :].strip()
        return s

    for line in lines:
        s = line.strip()
        low = s.lower()
        if mode == "pre":
            if low == "итог" or low.startswith("итог:") or low.startswith("итог —") or low.startswith("итог -") or low.startswith("итог–"):
                rest = _strip_итог_prefix(s) if low != "итог" else ""
                if rest:
                    summary_parts.append(rest)
                mode = "summary"
            continue
        if mode == "summary":
            if not s:
                mode = "details"
                continue
            if low.startswith("1)") or low.startswith("1.") or (len(s) >= 2 and s[0] == "1" and s[1] in ").] "):
                details_parts.append(line)
                mode = "details"
                continue
            if low.startswith("затем") or low.startswith("подробн"):
                mode = "details"
                continue
            summary_parts.append(s)
            continue
        details_parts.append(line)

    summary = " ".join(x.strip() for x in summary_parts if x.strip()).strip()
    details = "\n".join(details_parts).strip()
    if not summary:
        paras = [p.strip() for p in raw.split("\n\n") if p.strip()]
        if paras:
            summary = paras[0]
            details = "\n\n".join(paras[1:]).strip() if len(paras) > 1 else ""
    return {
        "summary_ru": summary,
        "details_ru": details,
        "explanation_ru": raw,
    }


def _load_ohlcv_for_ticker(ticker: str, *, engine=None, bars: int = 160) -> Optional[pd.DataFrame]:
    t = (ticker or "").strip().upper()
    if not t:
        return None
    eng = engine
    if eng is None:
        try:
            from report_generator import get_engine

            eng = get_engine()
        except Exception:
            eng = None
    frames: Dict[str, pd.DataFrame] = {}
    if eng is not None:
        frames = fetch_ohlcv_from_db(eng, [t], bars=bars)
    if t not in frames or frames[t].empty or len(frames[t]) < 30:
        yf = fetch_ohlcv_yfinance([t], period="1y")
        frames.update(yf)
    d = frames.get(t)
    if d is None or d.empty or "Close" not in d.columns:
        return None
    return d.tail(bars)


def render_nastya_range_chart_bundle(
    ticker: str,
    *,
    row: Optional[Dict[str, Any]] = None,
    engine=None,
    days: int = 130,
) -> Dict[str, Any]:
    """
    Дневной close-график ~6м + маркеры earnings/news.
    Returns {"png": bytes|None, "markers": {"earnings": [...], "news": [...]}}.
    """
    t = (ticker or "").strip().upper()
    empty_markers: Dict[str, List[Dict[str, Any]]] = {"earnings": [], "news": []}
    d = _load_ohlcv_for_ticker(t, engine=engine, bars=max(60, int(days)))
    if d is None or d.empty:
        return {"png": None, "markers": empty_markers}
    close = d["Close"].astype(float)
    idx = pd.to_datetime(close.index)
    start_d = _as_date(idx[0]) or date.today()
    end_d = _as_date(idx[-1]) or date.today()
    markers = load_chart_event_markers(t, start=start_d, end=end_d, engine=engine)
    markers = enrich_markers_with_forward_returns(close, markers)
    try:
        import io

        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except Exception as e:
        logger.warning("matplotlib unavailable for nastya chart: %s", e)
        return {"png": None, "markers": markers}

    fig, ax = plt.subplots(figsize=(8.4, 4.4), dpi=110)
    ax.plot(idx, close.values, color="#2563eb", linewidth=1.6, label="Close")
    if len(close) >= 20:
        sma20 = close.rolling(20).mean()
        ax.plot(idx, sma20.values, color="#94a3b8", linewidth=1.0, alpha=0.9, label="SMA20")
    floor = ceil = None
    if isinstance(row, dict):
        floor = _f_num(row.get("band_floor"))
        ceil = _f_num(row.get("band_ceiling"))
    if floor is not None:
        ax.axhline(floor, color="#16a34a", linestyle="--", linewidth=1.0, alpha=0.85, label=f"floor {floor}")
    if ceil is not None:
        ax.axhline(ceil, color="#dc2626", linestyle="--", linewidth=1.0, alpha=0.85, label=f"ceil {ceil}")

    def _scatter(points: List[Dict[str, Any]], *, marker: str, color: str, label: str, z: int = 5) -> None:
        xs, ys = [], []
        for p in points:
            dd = _as_date(p.get("date"))
            if dd is None:
                continue
            y = _price_on_or_before(close, dd)
            if y is None:
                continue
            xs.append(pd.Timestamp(dd))
            ys.append(y)
        if xs:
            ax.scatter(xs, ys, marker=marker, c=color, s=70, linewidths=1.6, zorder=z, label=label)

    earn_good = [p for p in markers.get("earnings") or [] if p.get("tone") == "good"]
    earn_bad = [p for p in markers.get("earnings") or [] if p.get("tone") == "bad"]
    news_good = [p for p in markers.get("news") or [] if p.get("tone") == "good"]
    news_bad = [p for p in markers.get("news") or [] if p.get("tone") == "bad"]
    _scatter(earn_good, marker="x", color="#16a34a", label="earnings beat", z=6)
    _scatter(earn_bad, marker="x", color="#dc2626", label="earnings miss", z=6)
    _scatter(news_good, marker="^", color="#16a34a", label="news+", z=5)
    _scatter(news_bad, marker="v", color="#dc2626", label="news−", z=5)

    regime = (row or {}).get("portfolio_trend_regime") if isinstance(row, dict) else None
    bias = (row or {}).get("bias_exit") if isinstance(row, dict) else None
    title = f"{t} · daily ~{len(close)}d"
    if bias:
        title += f" · bias {bias}"
    if regime:
        title += f" · ML {regime}"
    n_e = len(markers.get("earnings") or [])
    n_n = len(markers.get("news") or [])
    if n_e or n_n:
        title += f" · E{n_e}/N{n_n}"
    ax.set_title(title, fontsize=11)
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best", fontsize=7, ncol=2)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    fig.autofmt_xdate(rotation=0, ha="center")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return {"png": buf.getvalue(), "markers": markers}


def render_nastya_range_chart_png(
    ticker: str,
    *,
    row: Optional[Dict[str, Any]] = None,
    engine=None,
    days: int = 130,
) -> Optional[bytes]:
    """Дневной close-график ~6м с полом/потолком и маркерами earnings/news."""
    return render_nastya_range_chart_bundle(ticker, row=row, engine=engine, days=days).get("png")


def build_nastya_llm_user_content(
    user_prompt: str,
    *,
    chart_png: Optional[bytes] = None,
) -> Any:
    """OpenAI-compatible user content: text (+ optional chart image)."""
    import base64

    if not chart_png:
        return user_prompt
    b64 = base64.b64encode(chart_png).decode("ascii")
    return [
        {"type": "text", "text": user_prompt + "\n\nПриложена картинка дневного графика (~6м). Учти форму тренда."},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        },
    ]


def _attach_portfolio_trend(ticker: str, *, engine=None) -> Dict[str, Any]:
    """Слой portfolio trend 20d — отдельно от локального regime коридоров."""
    try:
        from services.portfolio_trend_regime import portfolio_trend_regime_snapshot

        snap = portfolio_trend_regime_snapshot(ticker, engine=engine)
        if not isinstance(snap, dict):
            return {
                "portfolio_trend_regime": "n/a",
                "portfolio_trend_ret_20d_pct": None,
                "portfolio_trend_near_20d_high": None,
                "portfolio_trend_status": "n/a",
            }
        return {
            "portfolio_trend_regime": snap.get("portfolio_trend_regime"),
            "portfolio_trend_ret_20d_pct": snap.get("portfolio_trend_ret_20d_pct"),
            "portfolio_trend_near_20d_high": snap.get("portfolio_trend_near_20d_high"),
            "portfolio_trend_drawdown_from_20d_high_pct": snap.get(
                "portfolio_trend_drawdown_from_20d_high_pct"
            ),
            "portfolio_trend_note_ru": snap.get("portfolio_trend_note_ru"),
            "portfolio_trend_status": snap.get("portfolio_trend_status") or "ok",
        }
    except Exception as e:
        logger.debug("portfolio_trend attach %s: %s", ticker, e)
        return {
            "portfolio_trend_regime": "error",
            "portfolio_trend_ret_20d_pct": None,
            "portfolio_trend_near_20d_high": None,
            "portfolio_trend_status": "error",
            "portfolio_trend_note_ru": str(e),
        }


def slim_portfolio_card_context(ticker: str) -> Dict[str, Any]:
    """Лёгкий срез карточки портфеля для LLM Насти (без вызова LLM портфеля)."""
    t = (ticker or "").strip().upper()
    out: Dict[str, Any] = {"ticker": t, "in_portfolio_game": False}
    try:
        from analyst_agent import AnalystAgent
        from services.portfolio_card import (
            get_portfolio_trade_tickers,
            load_fallback_portfolio_take_pct,
            portfolio_card_payload,
        )

        trade = set(get_portfolio_trade_tickers())
        if t not in trade:
            out["note_ru"] = "Тикер не в списке портфельной игры — только коридоры."
            return out
        out["in_portfolio_game"] = True
        agent = AnalystAgent(use_llm=False)
        r = agent.get_decision_with_llm(t, cluster_context=None)
        if r.get("decision") == "NO_DATA":
            out["status"] = "no_data"
            return out
        card = portfolio_card_payload(t, r, fallback_take_pct=load_fallback_portfolio_take_pct())
        keep = (
            "decision",
            "selected_strategy",
            "close",
            "prev_day_return_pct",
            "current_day_return_pct",
            "vix_regime",
            "strategy_insight",
            "cluster_note",
            "portfolio_trend_regime",
            "portfolio_trend_ret_20d_pct",
        )
        for k in keep:
            if card.get(k) is not None:
                out[k] = card.get(k)
        out["status"] = "ok"
    except Exception as e:
        logger.debug("slim_portfolio_card_context %s: %s", t, e)
        out["status"] = "error"
        out["error"] = str(e)
    return out


def build_nastya_llm_prompts(
    row: Dict[str, Any],
    *,
    market: Optional[Dict[str, Any]] = None,
    portfolio_slim: Optional[Dict[str, Any]] = None,
    chart_markers: Optional[Dict[str, Any]] = None,
) -> tuple[str, str]:
    """System + user prompts for per-ticker Nastya explanation."""
    payload = {
        "цели_насти": [
            "краткий Итог: что ждать по движению в ближайшее время",
            "пол и потолок",
            "боковик: ширина / сколько уже длится",
            "bias выхода из зоны",
            "RVOL как снимок (не вердикт по гипотезе разворота)",
            "соотнести с ML trend portfolio 20d",
            "форма тренда по графику (~6м) и маркеры earnings/news, если есть",
            "историческая аналогия: как цена реагировала на похожие новости/earnings раньше (ret_5d/ret_10d)",
        ],
        "строка_отчёта": row,
        "рынок": market or {},
        "карточка_портфеля_кратко": portfolio_slim or {},
        "маркеры_графика": chart_markers or {"earnings": [], "news": []},
        "легенда_маркеров": {
            "earnings_X_green": "EPS beat vs estimate",
            "earnings_X_red": "EPS miss vs estimate",
            "news_triangle_up_green": "сильная позитивная новость",
            "news_triangle_down_red": "сильная негативная новость",
            "news_cap": "до 5 сильнейших не-нейтральных новостей в месяц",
            "ret_5d_pct": "изменение close за ~5 торговых дней после маркера",
            "ret_10d_pct": "изменение close за ~10 торговых дней после маркера",
        },
    }
    user = (
        "Разбери тикер по пунктам из system prompt.\n"
        f"Данные (JSON):\n{json.dumps(payload, ensure_ascii=False, default=str)}"
    )
    return LLM_SYSTEM_RU, user


def explain_nastya_range_row_llm(
    ticker: str,
    *,
    engine=None,
    row: Optional[Dict[str, Any]] = None,
    market: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    LLM-пояснение одной строки отчёта коридоров в контексте вопросов Насти.
    При возможности передаёт дневной график (~6м) с маркерами earnings/news.
    """
    import base64

    t = (ticker or "").strip().upper()
    if not t:
        return {"ok": False, "error": "Пустой тикер"}

    if row is None or market is None:
        report = get_or_build_report(tickers=[t], refresh=False, engine=engine)
        market = market or (report.get("market") if isinstance(report.get("market"), dict) else {})
        if row is None:
            for r in report.get("tickers") or []:
                if str(r.get("ticker") or "").upper() == t:
                    row = r
                    break
    if not isinstance(row, dict) or row.get("status") not in (None, "ok"):
        return {"ok": False, "ticker": t, "error": f"Нет данных коридоров для {t}"}

    portfolio_slim = slim_portfolio_card_context(t)
    chart_bundle = render_nastya_range_chart_bundle(t, row=row, engine=engine, days=130)
    chart_png = chart_bundle.get("png")
    chart_markers = chart_bundle.get("markers") or {"earnings": [], "news": []}
    system_prompt, user_prompt = build_nastya_llm_prompts(
        row,
        market=market,
        portfolio_slim=portfolio_slim,
        chart_markers=chart_markers,
    )
    user_content = build_nastya_llm_user_content(user_prompt, chart_png=chart_png)

    try:
        from services.llm_service import LLMService

        llm = LLMService()
        if getattr(llm, "client", None) is None:
            return {"ok": False, "ticker": t, "error": "LLM недоступен (нет ключа)"}
        out = llm.generate_response(
            messages=[{"role": "user", "content": user_content}],
            system_prompt=system_prompt,
            max_tokens=1300,
        )
        text = (out or {}).get("response") or (out or {}).get("content") or (out or {}).get("text") or ""
        if not str(text).strip():
            err = (out or {}).get("error") or "Пустой ответ LLM"
            return {"ok": False, "ticker": t, "error": str(err)}
        parts = split_nastya_llm_explanation(str(text))
        result: Dict[str, Any] = {
            "ok": True,
            "ticker": t,
            "explanation_ru": parts.get("explanation_ru") or str(text).strip(),
            "summary_ru": parts.get("summary_ru") or "",
            "details_ru": parts.get("details_ru") or "",
            "model": (out or {}).get("model") or getattr(llm, "model", None),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "portfolio_context_used": bool(portfolio_slim.get("in_portfolio_game")),
            "chart_used": bool(chart_png),
            "chart_markers": chart_markers,
            "chart_markers_counts": {
                "earnings": len(chart_markers.get("earnings") or []),
                "news": len(chart_markers.get("news") or []),
            },
        }
        if chart_png:
            result["chart_png_base64"] = base64.b64encode(chart_png).decode("ascii")
            result["chart_mime"] = "image/png"
        return result
    except Exception as e:
        logger.exception("explain_nastya_range_row_llm %s: %s", t, e)
        return {"ok": False, "ticker": t, "error": str(e)}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_excel_path() -> Path:
    raw = (get_config_value("NASTYA_STOCKS_XLSX", "") or "").strip()
    if raw:
        return Path(raw)
    return _project_root() / "nastya" / "Stocks_17.07.xlsx"


def default_cache_path() -> Path:
    app = Path("/app/logs/ml/ml_data_quality/last_nastya_range_regime.json")
    if Path("/app/logs").exists():
        return app
    p = _project_root() / "local" / "logs" / "ml_data_quality" / "last_nastya_range_regime.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def method_ru() -> str:
    return METHOD_RU


def _f_num(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def load_excel_table(path: Optional[Path] = None) -> pd.DataFrame:
    p = path or default_excel_path()
    if not p.is_file():
        return pd.DataFrame()
    df = pd.read_excel(p)
    if "Ticker" not in df.columns:
        return pd.DataFrame()
    df = df.copy()
    df["Ticker"] = df["Ticker"].astype(str).str.upper().str.strip()
    return df.set_index("Ticker")


def fetch_ohlcv_from_db(engine, tickers: Sequence[str], *, bars: int = 260) -> Dict[str, pd.DataFrame]:
    wanted = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not wanted or engine is None:
        return {}
    lim = int(bars) + 5
    sql = text(
        """
        SELECT ticker, date, open, high, low, close, volume
        FROM (
            SELECT ticker, date, open, high, low, close, volume,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM quotes
            WHERE ticker IN :tickers
        ) x
        WHERE rn <= :n
        ORDER BY ticker ASC, date ASC
        """
    ).bindparams(bindparam("tickers", expanding=True))
    try:
        with engine.connect() as conn:
            df = pd.read_sql(sql, conn, params={"tickers": wanted, "n": lim})
    except Exception as e:
        logger.debug("nastya ohlcv db: %s", e)
        return {}
    if df is None or df.empty:
        return {}
    out: Dict[str, pd.DataFrame] = {}
    df["date"] = pd.to_datetime(df["date"])
    for t, g in df.groupby("ticker"):
        g = g.set_index("date").sort_index()
        for col in ("Open", "High", "Low", "Close", "Volume"):
            src = col.lower()
            if src in g.columns:
                g[col] = pd.to_numeric(g[src], errors="coerce")
        out[str(t).upper()] = g[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    return out


def fetch_ohlcv_yfinance(tickers: Sequence[str], *, period: str = "1y") -> Dict[str, pd.DataFrame]:
    try:
        import yfinance as yf
    except Exception as e:
        logger.debug("yfinance missing: %s", e)
        return {}
    out: Dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            d = yf.Ticker(t).history(period=period, auto_adjust=True)
            if d is None or d.empty:
                continue
            d = d.rename(columns=str.title)
            need = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in d.columns]
            out[str(t).upper()] = d[need].dropna(subset=["Close"])
        except Exception as e:
            logger.debug("yfinance %s: %s", t, e)
    return out


def _rvol(volume: pd.Series, win: int = 20) -> pd.Series:
    ma = volume.rolling(win).mean()
    return volume / ma


def _detect_local(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    *,
    lookback: int = 60,
    band_pct: float = 8.0,
) -> Dict[str, Any]:
    c = close.iloc[-lookback:]
    h = high.iloc[-lookback:]
    l = low.iloc[-lookback:]
    mid = float((h.max() + l.min()) / 2.0) if len(h) else None
    width = float((h.max() - l.min()) / mid * 100.0) if mid else None
    ret = float(c.iloc[-1] / c.iloc[0] - 1.0) * 100.0 if len(c) > 1 else None
    ch_hi = float(high.iloc[-20:].max())
    ch_lo = float(low.iloc[-20:].min())
    age = 0
    for i in range(len(close) - 1, -1, -1):
        if float(low.iloc[i]) < ch_lo * 0.98 or float(high.iloc[i]) > ch_hi * 1.02:
            break
        age += 1
    sma20 = close.rolling(20).mean().iloc[-1]
    sma50 = close.rolling(50).mean().iloc[-1]
    last = float(close.iloc[-1])
    if width is not None and width <= band_pct and abs(ret or 0.0) < band_pct * 0.6:
        regime = "range"
    elif pd.notna(sma20) and pd.notna(sma50) and last > float(sma20) > float(sma50) and (ret or 0.0) > 5:
        regime = "uptrend"
    elif pd.notna(sma20) and pd.notna(sma50) and last < float(sma20) < float(sma50) and (ret or 0.0) < -5:
        regime = "downtrend"
    else:
        regime = "transition"
    return {
        "regime": regime,
        "range_width_60d_pct": round(width, 2) if width is not None else None,
        "ret_60d_pct": round(ret, 2) if ret is not None else None,
        "approx_range_age_days": age,
        "channel_lo_20d": round(ch_lo, 2),
        "channel_hi_20d": round(ch_hi, 2),
        "sma20": round(float(sma20), 2) if pd.notna(sma20) else None,
        "sma50": round(float(sma50), 2) if pd.notna(sma50) else None,
    }


def _excel_anchors(ex: pd.DataFrame, ticker: str) -> Dict[str, Any]:
    if ex.empty or ticker not in ex.index:
        return {"in_excel": False}
    r = ex.loc[ticker]
    if isinstance(r, pd.DataFrame):
        r = r.iloc[0]

    def col(*names: str) -> Optional[float]:
        for n in names:
            if n in r.index:
                return _f_num(r[n])
        return None

    # Close column may be dated in header
    close_col = None
    for c in r.index:
        if str(c).startswith("Close"):
            close_col = _f_num(r[c])
            break

    return {
        "in_excel": True,
        "excel_close": close_col,
        "drop20_from_52w": col("Drop 20% от max high"),
        "drop25_from_52w": col("Drop 25% от max high"),
        "target_17pct": col("17% growth от close"),
        "target_25pct": col("25% growth от close"),
        "max_52w": col("Max high за 52 недели"),
        "july_crest": col("Max close до дропа 01.07.2026"),
        "drop_from_july_crest": col("Дроп от июльского гребня"),
        "upside": col("UPSIDE"),
        "potential_from_close": col("Потенциал от close"),
        "fall_from_52w": col("Падение от 52w high"),
    }


def _blend_band(last: float, local: Dict[str, Any], xa: Dict[str, Any]) -> Dict[str, Any]:
    supports = [
        v
        for v in [xa.get("drop25_from_52w"), xa.get("drop20_from_52w"), local.get("channel_lo_20d")]
        if v is not None
    ]
    resistances = [
        v
        for v in [
            xa.get("target_17pct"),
            xa.get("target_25pct"),
            xa.get("july_crest"),
            local.get("channel_hi_20d"),
        ]
        if v is not None
    ]
    below = [v for v in supports if v < last]
    above = [v for v in resistances if v > last]
    floor = max(below) if below else (min(supports) if supports else None)
    ceiling = min(above) if above else (max(resistances) if resistances else None)
    pos = None
    if floor is not None and ceiling is not None and ceiling > floor:
        pos = (last - floor) / (ceiling - floor)
    bias = "neutral"
    if pos is not None and pos < 0.35 and local.get("regime") != "downtrend":
        bias = "up"
    elif pos is not None and pos > 0.75:
        bias = "down"
    if local.get("regime") == "uptrend":
        bias = "up"
    if local.get("regime") == "downtrend":
        bias = "down"
    return {
        "band_floor": round(floor, 2) if floor is not None else None,
        "band_ceiling": round(ceiling, 2) if ceiling is not None else None,
        "pos_in_band": round(pos, 2) if pos is not None else None,
        "bias_exit": bias,
    }


def _macro_from_frames(ndx: Optional[pd.DataFrame], vix: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if ndx is None or ndx.empty or "Close" not in ndx.columns:
        return {"status": "missing_ndx"}
    weekly = ndx["Close"].resample("W-FRI").last().dropna()
    wret = weekly.pct_change()
    streak = maxstreak = 0
    for v in wret.iloc[-52:]:
        if pd.notna(v) and float(v) > 0:
            streak += 1
            maxstreak = max(maxstreak, streak)
        else:
            streak = 0
    ndx_6w = ndx["Close"].iloc[-30:]
    vix_close = None
    vix_regime = None
    if vix is not None and not vix.empty and "Close" in vix.columns:
        vix_close = round(float(vix["Close"].iloc[-1]), 2)
        vix_regime = "calm" if vix_close < 20 else ("elevated" if vix_close < 30 else "storm")
    return {
        "status": "ok",
        "ndx_close": round(float(ndx["Close"].iloc[-1]), 2),
        "ndx_ret_approx_6w_pct": round(float(ndx_6w.iloc[-1] / ndx_6w.iloc[0] - 1.0) * 100.0, 2),
        "ndx_max_consec_up_weeks_1y": int(maxstreak),
        "vix_close": vix_close,
        "vix_regime": vix_regime,
        "asof": str(pd.Timestamp(ndx.index[-1]).date()),
    }


def build_nastya_range_regime_report(
    *,
    tickers: Optional[Sequence[str]] = None,
    excel_path: Optional[Path] = None,
    engine=None,
    use_yfinance_fallback: bool = True,
) -> Dict[str, Any]:
    excel_p = excel_path or default_excel_path()
    ex = load_excel_table(excel_p)
    wanted = [str(t).strip().upper() for t in (tickers or DEFAULT_TICKERS) if str(t).strip()]
    if not wanted:
        wanted = list(DEFAULT_TICKERS)

    need = list(dict.fromkeys(wanted + ["^NDX", "^VIX"]))
    frames: Dict[str, pd.DataFrame] = {}
    source = "none"
    if engine is not None:
        frames = fetch_ohlcv_from_db(engine, need, bars=260)
        source = "quotes_db" if frames else "none"
    missing = [t for t in need if t not in frames or frames[t].empty or len(frames[t]) < 40]
    if missing and use_yfinance_fallback:
        yf_frames = fetch_ohlcv_yfinance(missing, period="1y")
        frames.update(yf_frames)
        if source == "quotes_db" and yf_frames:
            source = "quotes_db+yfinance"
        elif yf_frames:
            source = "yfinance"

    rows: List[Dict[str, Any]] = []
    for t in wanted:
        d = frames.get(t)
        if d is None or d.empty or "Close" not in d.columns:
            rows.append({"ticker": t, "status": "no_ohlcv", "in_excel": t in ex.index})
            continue
        close, high, low = d["Close"], d["High"], d["Low"]
        vol = d["Volume"] if "Volume" in d.columns else pd.Series(dtype=float)
        local = _detect_local(close, high, low)
        xa = _excel_anchors(ex, t)
        last = float(close.iloc[-1])
        rvol_now = None
        rvol_flag = "n/a"
        if len(vol) >= 20:
            rv = _rvol(vol)
            if pd.notna(rv.iloc[-1]):
                rvol_now = float(rv.iloc[-1])
                rvol_flag = "high" if rvol_now >= 1.5 else ("low" if rvol_now <= 0.7 else "normal")
        band = _blend_band(last, local, xa)
        upside = xa.get("upside")
        # Short labels for UI; not repeated in comment_ru (table/card already show UPSIDE).
        upside_note = None
        if upside is not None:
            if upside <= 1.05:
                upside_note = "у зоны ×10"
            elif upside <= 1.25:
                upside_note = "близко к ×10"
            else:
                upside_note = "запас до ×10"
        potential = xa.get("potential_from_close")
        if potential is not None:
            try:
                xa = dict(xa)
                xa["potential_from_close"] = round(float(potential), 2)
            except (TypeError, ValueError):
                pass
        # Per-ticker notes: only non-default / actionable; skip boilerplate
        # (UPSIDE note, transition, RVOL normal, floor/ceiling bias — already in columns).
        comments: List[str] = []
        if local.get("regime") == "range":
            comments.append(
                f"Боковик: ширина 60d ≈{local.get('range_width_60d_pct')}%, Age≈{local.get('approx_range_age_days')}d."
            )
        if rvol_flag == "high":
            comments.append("RVOL high — объём аномально высокий (выход/разворот?).")
        elif rvol_flag == "low":
            comments.append("RVOL low — объём слабый.")
        trend = _attach_portfolio_trend(t, engine=engine)
        tr = trend.get("portfolio_trend_regime")
        if tr and tr not in ("n/a", "error", "disabled", "insufficient"):
            ret20 = trend.get("portfolio_trend_ret_20d_pct")
            ret_s = f", 20d {ret20:+.1f}%" if isinstance(ret20, (int, float)) else ""
            comments.append(f"ML trend: {tr}{ret_s}.")
        rows.append(
            {
                "ticker": t,
                "status": "ok",
                "asof": str(pd.Timestamp(close.index[-1]).date()),
                "close": round(last, 2),
                **local,
                "rvol_20": round(rvol_now, 2) if rvol_now is not None else None,
                "rvol_flag": rvol_flag,
                **band,
                **xa,
                **trend,
                "upside_note_ru": upside_note,
                "comment_ru": " ".join(comments),
            }
        )

    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_excel": str(excel_p),
        "excel_exists": excel_p.is_file(),
        "ohlcv_source": source,
        "method_ru": METHOD_RU,
        "market": _macro_from_frames(frames.get("^NDX"), frames.get("^VIX")),
        "tickers": rows,
        "default_tickers": list(DEFAULT_TICKERS),
        "data_policy": {
            "excel": "manual/semi — якоря Насти; обновлять при новой дате close",
            "ohlcv_rvol_ndx_vix": "auto daily",
            "not_required_mvp": ["candlestick labels", "Investing AI news", "earnings text"],
        },
    }
    mkt = report["market"] if isinstance(report.get("market"), dict) else {}
    mnotes: List[str] = []
    if mkt.get("vix_regime") == "calm":
        mnotes.append("VIX спокойный — глобального «шторма» нет.")
    elif mkt.get("vix_regime") == "storm":
        mnotes.append("VIX storm — осторожнее с bias up.")
    try:
        ndx6 = float(mkt.get("ndx_ret_approx_6w_pct"))
        if ndx6 <= -5:
            mnotes.append(f"NDX ~6w {ndx6}% — фон рынка слабый.")
        elif ndx6 >= 5:
            mnotes.append(f"NDX ~6w +{ndx6}% — фон рынка сильный.")
    except (TypeError, ValueError):
        pass
    mnotes.append(
        "RVOL в таблице — текущий снимок; гипотеза Насти про объём проверяется на датах разворота/выхода из боковика, не этим экраном в одиночку."
    )
    report["market_comment_ru"] = " ".join(mnotes)
    return report


def save_report_cache(report: Dict[str, Any], path: Optional[Path] = None) -> Path:
    p = path or default_cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p


def load_report_cache(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    p = path or default_cache_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def get_or_build_report(
    *,
    tickers: Optional[Sequence[str]] = None,
    refresh: bool = False,
    engine=None,
) -> Dict[str, Any]:
    if not refresh:
        cached = load_report_cache()
        if (
            cached
            and cached.get("tickers")
            and int(cached.get("schema_version") or 0) == REPORT_SCHEMA_VERSION
        ):
            # reuse if same ticker set requested
            req = [str(t).strip().upper() for t in (tickers or DEFAULT_TICKERS) if str(t).strip()]
            got = [str(r.get("ticker") or "").upper() for r in (cached.get("tickers") or [])]
            if req == got:
                cached = dict(cached)
                cached["cache_hit"] = True
                return cached
    if engine is None:
        try:
            from report_generator import get_engine

            engine = get_engine()
        except Exception:
            engine = None
    report = build_nastya_range_regime_report(tickers=tickers, engine=engine)
    save_report_cache(report)
    report["cache_hit"] = False
    return report
