"""
Рекомендация по 5-минутным данным для решения агента (игра 5m, сигналы).

Для решения используются все доступные 5m-отметки от текущего момента назад на 7 дней
(или сколько отдаёт Yahoo). Эти данные вместе с контекстом из KB и запросом о свежих
новостях (LLM) передаются агенту для принятия решения BUY/HOLD/SELL.

- свечи 5m: полное окно до MAX_DAYS_5M=7 дней (yfinance);
- по данным: RSI, волатильность, импульс 2ч, high/low за окно (где хватает баров);
- решение и параметры стоп/тейк под интрадей;
- опционально: запрос к LLM о свежих новостях/настроениях перед решением (USE_LLM_NEWS).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Макс. длина content в контексте KB (чтобы не раздувать ответ)
KB_NEWS_CONTENT_MAX_LEN = 500

# Максимум дней 5m по ограничениям Yahoo
MAX_DAYS_5M = 7
# Период RSI по 5m свечам (14 свечей ≈ 70 мин)
RSI_PERIOD_5M = 14
# Баров в «2 часа» для импульса
BARS_2H = 24


def fetch_5m_ohlc(ticker: str, days: int = None) -> Optional[pd.DataFrame]:
    """
    Загружает все доступные 5-минутные OHLC от текущего момента назад на days дней
    (по умолчанию MAX_DAYS_5M=7 — максимум, что даёт Yahoo).

    Returns:
        DataFrame с колонками datetime, Open, High, Low, Close или None.
    """
    if days is None:
        days = MAX_DAYS_5M
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance не установлен")
        return None
    days = min(max(1, days), MAX_DAYS_5M)
    end_date = datetime.utcnow() + timedelta(days=1)
    start_date = datetime.utcnow() - timedelta(days=days)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    t = yf.Ticker(ticker)
    df = t.history(start=start_str, end=end_str, interval="5m", auto_adjust=False)
    if df is None or df.empty:
        return None
    df = df.rename_axis("datetime").reset_index()
    for c in ("Open", "High", "Low", "Close"):
        if c not in df.columns:
            return None
    return df


def has_5m_data(ticker: str, days: int = None, min_bars: int = 1) -> bool:
    """
    Есть ли 5m данные по тикеру: все доступные отметки от «сейчас» назад на days дней
    (по умолчанию 7 — полное окно Yahoo). Достаточно любого непустого набора баров.
    """
    if days is None:
        days = MAX_DAYS_5M
    df = fetch_5m_ohlc(ticker, days=days)
    return df is not None and not df.empty and len(df) >= min_bars


def fetch_kb_news_for_period(ticker: str, days: int) -> List[Dict[str, Any]]:
    """
    Новости/события из KB за те же days дней, что и окно 5m. Позволяет агенту
    сопоставить динамику цены и новости за один период и учесть влияние при решении.
    """
    try:
        from sqlalchemy import create_engine, text
        from config_loader import get_database_url
        cutoff = datetime.utcnow() - timedelta(days=days)
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT ts, ticker, source, content, sentiment_score, insight
                    FROM knowledge_base
                    WHERE (ticker = :ticker OR ticker IN ('MACRO', 'US_MACRO'))
                      AND ts >= :cutoff
                      AND content IS NOT NULL
                      AND LENGTH(TRIM(content)) > 0
                    ORDER BY ts DESC
                """),
                {"ticker": ticker, "cutoff": cutoff},
            )
            rows = result.fetchall()
        out = []
        for row in rows:
            ts, kb_ticker, source, content, sentiment_score, insight = row
            content_str = (content or "")[:KB_NEWS_CONTENT_MAX_LEN]
            if len(content or "") > KB_NEWS_CONTENT_MAX_LEN:
                content_str += "..."
            out.append({
                "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "ticker": kb_ticker,
                "source": source or "",
                "content": content_str,
                "sentiment_score": float(sentiment_score) if sentiment_score is not None else None,
                "insight": (insight or "")[:300] if insight else None,
            })
        return out
    except Exception as e:
        logger.debug("KB новости за период %s %d дн.: %s", ticker, days, e)
        return []


def _log_returns(series: pd.Series) -> pd.Series:
    """Лог-доходности по правилам проекта."""
    return np.log(series / series.shift(1)).dropna()


def compute_rsi_5m(closes: pd.Series, period: int = RSI_PERIOD_5M) -> Optional[float]:
    """RSI по ряду 5m закрытий (последнее значение = текущее)."""
    from services.rsi_calculator import compute_rsi_from_closes
    vals = closes.dropna().tolist()
    if len(vals) < period + 1:
        return None
    return compute_rsi_from_closes(vals, period=period)


def get_decision_5m(
    ticker: str,
    days: int = None,
    use_llm_news: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Решение по 5m для короткой игры: 5m-свечи + новости из KB за тот же период.
    Влияние новостей на решение явное: сильный негатив (sentiment < 0.35) откладывает
    вход (BUY→HOLD), негатив (0.35–0.4) ослабляет (STRONG_BUY→BUY); позитив (> 0.65)
    при пограничном RSI может поддержать вход (HOLD→BUY). Результат возвращает
    kb_news_impact для отображения в алерте.

    use_llm_news: при True и USE_LLM_NEWS — запрос к LLM о свежих новостях (дополняет KB).
    LLM не ищет в интернете; breaking news должны быть в KB (cron: Investing.com News и т.д.).

    Returns:
        dict: decision, reasoning, price, rsi_5m, volatility_5m_pct, momentum_2h_pct,
        high_5d, low_5d, period_str, bars_count, stop_loss_pct, take_profit_pct;
        kb_news_days, kb_news — новости из KB за тот же период; market_session — открытие/закрытие биржи и праздники (отдельно для учёта особых процессов новостей);
        при use_llm_news — llm_news_content, llm_sentiment, llm_insight.
        None только если нет ни одного бара (Yahoo не вернул данных).
    """
    if days is None:
        days = MAX_DAYS_5M
    df = fetch_5m_ohlc(ticker, days=days)
    if df is None or df.empty:
        logger.warning("Нет 5m данных для %s за последние %d дн.", ticker, days)
        return None

    df = df.sort_values("datetime").reset_index(drop=True)
    closes = df["Close"].astype(float)
    high_5d = float(df["High"].max())
    low_5d = float(df["Low"].min())
    price = float(closes.iloc[-1])

    # Лог-доходности за весь период
    log_ret = _log_returns(closes)
    volatility_5m_pct = float(log_ret.std() * 100) if len(log_ret) > 1 else 0.0

    # RSI по 5m
    rsi_5m = compute_rsi_5m(closes, period=RSI_PERIOD_5M)

    # Импульс за последние ~2 часа (24 свечи по 5m); при малом числе баров — за доступный хвост
    n = min(BARS_2H, len(closes) - 1)
    momentum_2h_pct = 0.0
    if n >= 1 and len(closes) >= n + 1:
        price_2h_ago = float(closes.iloc[-(n + 1)])
        if price_2h_ago > 0:
            momentum_2h_pct = ((price / price_2h_ago) - 1.0) * 100.0

    dt_min = df["datetime"].min()
    dt_max = df["datetime"].max()
    if hasattr(dt_min, "strftime"):
        period_str = f"{dt_min.strftime('%d.%m %H:%M')} – {dt_max.strftime('%d.%m %H:%M')}"
    else:
        period_str = f"{dt_min} – {dt_max}"

    # Хай сессии (последний торговый день в данных) и откат от него — чтобы видеть моменты "съехал от хая, вход на откате"
    session_high = high_5d
    pullback_from_high_pct = 0.0
    try:
        dts = pd.to_datetime(df["datetime"])
        last_date = dts.max().date()
        session_mask = dts.dt.date == last_date
        session_high_val = float(df.loc[session_mask, "High"].max())
        if session_high_val > 0 and price < session_high_val:
            session_high = session_high_val
            pullback_from_high_pct = (session_high - price) / session_high * 100.0
    except Exception:
        pass

    # Правила решения (агрессивные под интрадей)
    decision = "HOLD"
    reasons = []

    if rsi_5m is not None:
        if rsi_5m <= 32 and momentum_2h_pct >= -0.3:
            decision = "STRONG_BUY"
            reasons.append(f"RSI(5m)={rsi_5m:.1f} — перепроданность, отскок")
        elif rsi_5m <= 38 and price <= low_5d * 1.005:
            decision = "BUY"
            reasons.append(f"RSI(5m)={rsi_5m:.1f}, цена у 5д минимума")
        elif rsi_5m >= 76:
            decision = "SELL"
            reasons.append(f"RSI(5m)={rsi_5m:.1f} — перекупленность")
        elif rsi_5m >= 68:
            if decision == "HOLD":
                reasons.append(f"RSI(5m)={rsi_5m:.1f} — ближе к перекупленности, ждать")
        elif momentum_2h_pct > 0.5 and (rsi_5m is None or rsi_5m < 62):
            if decision == "HOLD":
                decision = "BUY"
                reasons.append(f"импульс +{momentum_2h_pct:.2f}% за 2ч, RSI не перекуплен")

    if volatility_5m_pct > 0.4 and decision in ("BUY", "STRONG_BUY"):
        reasons.append(f"волатильность 5m высокая ({volatility_5m_pct:.2f}%) — предпочтительны лимитные ордера")
    elif volatility_5m_pct > 0.6:
        if decision == "HOLD":
            reasons.append(f"волатильность 5m {volatility_5m_pct:.2f}% — выжидать")

    # Влияние новостей из KB на короткую игру 5m: явный учёт в решении BUY/HOLD/SELL
    kb_news = fetch_kb_news_for_period(ticker, days)
    news_with_sentiment = [(n, float(n["sentiment_score"])) for n in kb_news[:10] if n.get("sentiment_score") is not None]
    recent_negative = [n for n, s in news_with_sentiment if s < 0.4]
    very_negative = [n for n, s in news_with_sentiment if s < 0.35]
    recent_positive = [n for n, s in news_with_sentiment if s > 0.65]
    kb_news_impact = "нейтрально"  # для вывода в алерт

    if very_negative:
        # Сильный негатив (шорт, скандал и т.п.) — не входим
        if decision in ("BUY", "STRONG_BUY"):
            decision = "HOLD"
            reasons.append("сильный негатив в новостях — вход отложен")
            kb_news_impact = "негатив (вход отложен)"
    elif recent_negative:
        # Негатив — смягчаем вход
        if decision == "STRONG_BUY":
            decision = "BUY"
            reasons.append("негативные новости в базе — вход без STRONG_BUY")
            kb_news_impact = "негатив (вход ослаблен)"
        elif decision == "BUY":
            reasons.append("негативные новости в базе — осторожность")
            kb_news_impact = "негатив (осторожность)"
    elif recent_positive and decision == "HOLD" and rsi_5m is not None and 38 <= rsi_5m <= 52 and momentum_2h_pct >= -0.5:
        # Позитив в новостях при пограничном RSI — разрешаем осторожный BUY
        decision = "BUY"
        reasons.append("позитив в новостях поддерживает вход при нейтральном RSI")
        kb_news_impact = "позитив (поддержка входа)"
    elif recent_positive and decision in ("BUY", "STRONG_BUY"):
        kb_news_impact = "позитив"

    if not reasons:
        rsi_str = f"{rsi_5m:.1f}" if rsi_5m is not None else "— (мало баров)"
        reasons.append(
            f"5m: цена {price:.2f}, RSI={rsi_str}, импульс 2ч={momentum_2h_pct:+.2f}%, волатильность={volatility_5m_pct:.2f}%, баров={len(df)}"
        )

    reasoning = " ".join(reasons)

    # Параметры под интрадей (уже стоп/тейк)
    stop_loss_pct = 2.5
    take_profit_pct = 5.0

    # KB уже загружены выше для учёта негатива в решении; передаём в контекст
    # Открытие/закрытие биржи и праздники — отдельно (особые процессы новостей в эти моменты)
    market_session = {}
    try:
        from services.market_session import get_market_session_context
        market_session = get_market_session_context()
    except Exception as e:
        logger.debug("Контекст сессии биржи для 5m: %s", e)

    out = {
        "decision": decision,
        "reasoning": reasoning,
        "price": price,
        "rsi_5m": rsi_5m,
        "volatility_5m_pct": volatility_5m_pct,
        "momentum_2h_pct": momentum_2h_pct,
        "high_5d": high_5d,
        "low_5d": low_5d,
        "session_high": session_high,
        "pullback_from_high_pct": pullback_from_high_pct,
        "period_str": period_str,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "bars_count": len(df),
        "kb_news_days": days,
        "kb_news": kb_news,
        "kb_news_impact": kb_news_impact,
        "market_session": market_session,
    }

    # Свежие новости/настроения от LLM непосредственно перед решением (дополнение к KB)
    if use_llm_news:
        try:
            from config_loader import get_config_value
            if get_config_value("USE_LLM_NEWS", "").strip().lower() in ("1", "true", "yes"):
                from services.llm_service import get_llm_service
                llm = get_llm_service()
                llm_data = llm.fetch_news_for_ticker(ticker) if getattr(llm, "client", None) else None
                if llm_data:
                    out["llm_news_content"] = llm_data.get("content")
                    out["llm_sentiment"] = llm_data.get("sentiment_score")
                    out["llm_insight"] = llm_data.get("insight")
                    if llm_data.get("llm_comparison"):
                        out["llm_comparison"] = llm_data["llm_comparison"]
        except Exception as e:
            logger.debug("LLM новости перед решением %s: %s", ticker, e)

    return out
