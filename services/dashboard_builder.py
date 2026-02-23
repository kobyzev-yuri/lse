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


def build_dashboard_text(mode: str = "all") -> str:
    """
    Строит сводку по отслеживаемым тикерам.

    mode: "all" | "5m" | "daily"
    - all: цена, RSI, решение, новости, плюс блок 5m по SNDK
    - 5m: акцент на 5m (SNDK и быстрые тикеры)
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
    now_str = datetime.now().strftime("%d.%m %H:%M")

    # Режим рынка один на всех — по индексу VIX (не по тикерам). Пороги: <15 LOW_FEAR, 15–25 NEUTRAL, >25 HIGH_PANIC
    regime_hint = ""
    if vix_val is not None and vix_regime != "N/A":
        if vix_regime == "NEUTRAL":
            regime_hint = " (VIX 15–25)"
        elif vix_regime == "LOW_FEAR":
            regime_hint = " (VIX <15)"
        elif vix_regime == "HIGH_PANIC":
            regime_hint = " (VIX >25)"
    lines = [
        "📊 **Дашборд** (мониторинг)",
        f"🕐 {now_str}  ·  VIX: {vix_val:.1f}" if vix_val is not None else f"🕐 {now_str}  ·  VIX: —",
        f"Режим рынка (по VIX, один для всех тикеров): {vix_regime}{regime_hint}",
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
                lines.append(f"• **{ticker}** — нет данных")
                continue
            price = float(row[0])
            rsi = float(row[1]) if row[1] is not None else None
            rsi_str = f"RSI {rsi:.0f}" if rsi is not None else "RSI —"

            news_count = 0
            try:
                cutoff = datetime.now() - timedelta(days=7)
                with engine.connect() as conn2:
                    rn = conn2.execute(
                        text(
                            """
                            SELECT COUNT(*) FROM knowledge_base
                            WHERE ticker = :ticker AND ts >= :cutoff
                              AND content IS NOT NULL AND LENGTH(content) > 10
                            """
                        ),
                        {"ticker": ticker, "cutoff": cutoff},
                    ).fetchone()
                news_count = int(rn[0]) if rn and rn[0] else 0
            except Exception:
                pass

            decision = "—"
            try:
                decision = analyst.get_decision(ticker)
            except Exception as e:
                logger.debug("Dashboard get_decision %s: %s", ticker, e)
            emoji = "🟢" if decision in ("BUY", "STRONG_BUY") else "🔴" if decision == "SELL" else "⚪"
            line = f"{emoji} **{ticker}** ${price:.2f}  {rsi_str}  → {decision}  ·  Новостей 7д: {news_count}"
            lines.append(line)
        except Exception as e:
            logger.warning("Dashboard ticker %s: %s", ticker, e)
            lines.append(f"• **{ticker}** — ошибка")

    if mode in ("5m", "all"):
        lines.append("")
        lines.append("⏱ **5m (интрадей):**")
        try:
            from services.recommend_5m import get_decision_5m
            d5 = get_decision_5m("SNDK", days=2)
            if d5:
                lines.append(
                    f"  SNDK: ${d5['price']:.2f}  RSI(5m) {d5.get('rsi_5m') or '—'}  "
                    f"импульс 2ч {d5.get('momentum_2h_pct', 0):+.2f}%  → **{d5['decision']}**"
                )
                lines.append(f"  _Период данных: {d5.get('period_str', '')}_")
            else:
                lines.append("  SNDK: нет 5m данных")
        except Exception as e:
            logger.debug("Dashboard 5m: %s", e)
            lines.append("  SNDK: 5m недоступен")

    if mode in ("daily", "all"):
        lines.append("")
        lines.append("📰 **Новости (фокус дня):** по тикерам выше. Для деталей: /news <ticker>")

    lines.append("")
    lines.append("_Детали: /recommend <ticker>  ·  5m: /recommend5m SNDK  ·  График 5m: /chart5m SNDK_")
    return "\n".join(lines)
