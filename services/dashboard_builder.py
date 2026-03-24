"""
Сборка текста дашборда по отслеживаемым тикерам для проактивного мониторинга.
Используется командой /dashboard в боте и скриптом send_dashboard_cron.py для рассылки по расписанию.
"""

from datetime import datetime, timedelta
import logging

from sqlalchemy import create_engine, text

from config_loader import get_database_url, get_config_value
from analyst_agent import AnalystAgent

logger = logging.getLogger(__name__)


def _escape_md(s: str) -> str:
    """Экранирует символы Markdown для Telegram (parse_mode=Markdown), чтобы не ломать парсер."""
    if not s:
        return s
    return str(s).replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")


try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except ImportError:
    _ET = None


def build_dashboard_text(mode: str = "all") -> str:
    """
    Строит сводку по отслеживаемым тикерам.

    mode: "all" | "5m" | "daily"
    - all: цена, RSI, решение, новости, открытые 5m/сделки 24ч, блок 5m по всем игровым тикерам
    - 5m: акцент на 5m (все тикеры игры 5m)
    - daily: акцент на новостях (мониторинг через новости при дневных ценах)
    """
    watchlist_str = get_config_value("DASHBOARD_WATCHLIST", "SNDK,MU,LITE,ALAB,TER,MSFT")
    watchlist = [t.strip() for t in watchlist_str.split(",") if t.strip()]
    if not watchlist:
        watchlist = ["SNDK", "MU", "LITE", "ALAB", "TER", "MSFT"]

    engine = create_engine(get_database_url())
    analyst = AnalystAgent(use_llm=False, use_strategy_factory=True)
    vix_info = analyst.get_vix_regime()
    vix_val = vix_info.get("vix_value")
    vix_regime = vix_info.get("regime") or "N/A"
    # В интерфейсе время всегда показываем в ET (Eastern Time)
    now_dt = datetime.now(_ET) if _ET else datetime.now()
    now_str = now_dt.strftime("%d.%m %H:%M")

    # Режим рынка один на всех — по индексу VIX (не по тикерам). Пороги: <15 LOW_FEAR, 15–25 NEUTRAL, >25 HIGH_PANIC
    regime_hint = ""
    if vix_val is not None and vix_regime != "N/A":
        if vix_regime == "NEUTRAL":
            regime_hint = " (VIX 15–25)"
        elif vix_regime == "LOW_FEAR":
            regime_hint = " (VIX <15)"
        elif vix_regime == "HIGH_PANIC":
            regime_hint = " (VIX >25)"
    # Всего новостей в KB за 7 дн.; по тикерам (не макро) — для пояснения "почему по 0"
    total_news_7d = None
    news_with_ticker_7d = None
    try:
        with engine.connect() as conn:
            r = conn.execute(
                text(
                    "SELECT COUNT(*) FROM knowledge_base WHERE (COALESCE(ingested_at, ts))::date >= current_date - 7 "
                    "AND content IS NOT NULL AND LENGTH(TRIM(content)) > 5"
                ),
            ).fetchone()
            total_news_7d = int(r[0]) if r and r[0] is not None else 0
            r2 = conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM knowledge_base
                    WHERE (COALESCE(ingested_at, ts))::date >= current_date - 7 AND content IS NOT NULL AND LENGTH(TRIM(content)) > 5
                      AND ticker IS NOT NULL AND ticker NOT IN ('MACRO', 'US_MACRO')
                    """
                ),
            ).fetchone()
            news_with_ticker_7d = int(r2[0]) if r2 and r2[0] is not None else 0
    except Exception:
        try:
            cutoff = datetime.now() - timedelta(days=7)
            with engine.connect() as conn:
                r = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM knowledge_base WHERE COALESCE(ingested_at, ts) >= :cutoff "
                        "AND content IS NOT NULL AND LENGTH(content) > 5"
                    ),
                    {"cutoff": cutoff},
                ).fetchone()
                total_news_7d = int(r[0]) if r and r[0] is not None else 0
                r2 = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM knowledge_base
                        WHERE COALESCE(ingested_at, ts) >= :cutoff AND content IS NOT NULL AND LENGTH(content) > 5
                          AND ticker IS NOT NULL AND ticker NOT IN ('MACRO', 'US_MACRO')
                        """
                    ),
                    {"cutoff": cutoff},
                ).fetchone()
                news_with_ticker_7d = int(r2[0]) if r2 and r2[0] is not None else 0
        except Exception:
            pass

    news_line = f"Новостей в KB за 7 дн.: {total_news_7d}"
    if total_news_7d is not None and news_with_ticker_7d is not None and total_news_7d > 0:
        news_line += f" (с тикером в KB: {news_with_ticker_7d})"

    lines = [
        "📊 **Дашборд** (мониторинг)",
        f"🕐 {now_str} ET  ·  VIX: {vix_val:.1f}" if vix_val is not None else f"🕐 {now_str} ET  ·  VIX: —",
        f"Режим рынка (по VIX, один для всех тикеров): {_escape_md(vix_regime)}{regime_hint}",
        news_line if total_news_7d is not None else "",
        "",
    ]
    for ticker in watchlist:
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT close, rsi FROM quotes WHERE ticker = :ticker ORDER BY date DESC LIMIT 1"
                    ),
                    {"ticker": ticker},
                ).fetchone()
            if not row or row[0] is None:
                lines.append(f"• **{_escape_md(ticker)}** — нет данных")
                continue
            price = float(row[0])
            rsi = float(row[1]) if row[1] is not None else None
            rsi_str = f"RSI {rsi:.0f}" if rsi is not None else "RSI —"

            news_count = 0
            try:
                # Окно 7 дней: по дате в БД (PostgreSQL) или по cutoff в Python (fallback)
                with engine.connect() as conn2:
                    try:
                        rn = conn2.execute(
                            text(
                                """
                                SELECT COUNT(*) FROM knowledge_base
                                WHERE ticker = :ticker AND (COALESCE(ingested_at, ts))::date >= current_date - 7
                                  AND content IS NOT NULL AND LENGTH(TRIM(content)) > 5
                                """
                            ),
                            {"ticker": ticker},
                        ).fetchone()
                    except Exception:
                        cutoff = datetime.now() - timedelta(days=7)
                        rn = conn2.execute(
                            text(
                                """
                                SELECT COUNT(*) FROM knowledge_base
                                WHERE ticker = :ticker AND COALESCE(ingested_at, ts) >= :cutoff
                                  AND content IS NOT NULL AND LENGTH(content) > 5
                                """
                            ),
                            {"ticker": ticker, "cutoff": cutoff},
                        ).fetchone()
                news_count = int(rn[0]) if rn and rn[0] else 0
            except Exception as e:
                logger.debug("Dashboard news_count %s: %s", ticker, e)

            decision = "—"
            try:
                decision = analyst.get_decision(ticker)
            except Exception as e:
                logger.debug("Dashboard get_decision %s: %s", ticker, e)
            emoji = "🟢" if decision in ("BUY", "STRONG_BUY") else "🔴" if decision == "SELL" else "⚪"
            line = f"{emoji} **{_escape_md(ticker)}** ${price:.2f}  {rsi_str}  → {decision}  ·  Новостей 7д: {news_count}"
            lines.append(line)
        except Exception as e:
            logger.warning("Dashboard ticker %s: %s", ticker, e)
            lines.append(f"• **{_escape_md(ticker)}** — ошибка")

    # Открытые позиции 5m и сделки за 24ч — чтобы дашборд не был "ни покупок ни продаж"
    if mode in ("5m", "all"):
        try:
            from report_generator import get_engine as get_report_engine, load_trade_history, compute_open_positions
            report_engine = get_report_engine()
            trades_5m = load_trade_history(report_engine, strategy_name="GAME_5M")
            open_5m = compute_open_positions(trades_5m)
            if open_5m:
                parts = [f"**{_escape_md(p.ticker)}** @ {p.entry_price:.2f}" for p in open_5m[:6]]
                lines.append("")
                lines.append("📌 **Открытые 5m:** " + ", ".join(parts))
            else:
                # Сделки за 24ч: сколько входов и выходов (по БД)
                cutoff_24h = datetime.now() - timedelta(hours=24)
                try:
                    with engine.connect() as conn:
                        buy_24 = conn.execute(
                            text(
                                "SELECT COUNT(*) FROM trade_history WHERE strategy_name = 'GAME_5M' AND side = 'BUY' AND ts >= :c"
                            ),
                            {"c": cutoff_24h},
                        ).fetchone()
                        sell_24 = conn.execute(
                            text(
                                "SELECT COUNT(*) FROM trade_history WHERE strategy_name = 'GAME_5M' AND side = 'SELL' AND ts >= :c"
                            ),
                            {"c": cutoff_24h},
                        ).fetchone()
                    n_buy = int(buy_24[0]) if buy_24 else 0
                    n_sell = int(sell_24[0]) if sell_24 else 0
                    if n_buy or n_sell:
                        lines.append("")
                        lines.append(f"📌 Открытых 5m: нет. За 24ч: входов {n_buy}, выходов {n_sell}")
                except Exception:
                    pass
        except Exception as e:
            logger.debug("Dashboard open/trades: %s", e)

    if mode in ("5m", "all"):
        lines.append("")
        lines.append("⏱ **5m (интрадей):**")
        try:
            from services.recommend_5m import get_decision_5m
            from services.ticker_groups import get_tickers_game_5m
            game_tickers = get_tickers_game_5m()
            if not game_tickers:
                game_tickers = watchlist[:6]
            period_str = None
            for ticker in game_tickers[:8]:
                d5 = get_decision_5m(ticker)
                if d5:
                    rsi_val = d5.get("rsi_5m")
                    rsi_str = f"{rsi_val:.2f}" if rsi_val is not None else "—"
                    lines.append(
                        f"  **{_escape_md(ticker)}**: ${d5['price']:.2f}  RSI(5m) {rsi_str}  "
                        f"импульс 2ч {d5.get('momentum_2h_pct', 0):+.2f}%  → **{d5['decision']}**"
                    )
                    if period_str is None:
                        period_str = d5.get("period_str")
                else:
                    lines.append(f"  **{_escape_md(ticker)}**: нет 5m данных")
            if period_str:
                lines.append(f"  _Период данных: {_escape_md(period_str)}_")
        except Exception as e:
            logger.debug("Dashboard 5m: %s", e)
            lines.append("  5m недоступен")

    if mode in ("daily", "all"):
        lines.append("")
        lines.append("📰 **Новости (фокус дня):** по тикерам выше. Для деталей: /news по тикеру")

    lines.append("")
    try:
        from services.ticker_groups import get_tickers_game_5m
        gt = get_tickers_game_5m()
        first_ticker = gt[0] if gt else (watchlist[0] if watchlist else "SNDK")
    except Exception:
        first_ticker = watchlist[0] if watchlist else "SNDK"
    lines.append(f"_Детали: /recommend  ·  5m: /recommend5m по тикеру  ·  График: /chart5m {_escape_md(first_ticker)}_")
    return "\n".join(lines)
