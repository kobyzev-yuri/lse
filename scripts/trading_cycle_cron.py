#!/usr/bin/env python3
"""
Скрипт портфельной игры (торговый цикл по дневным стратегиям).
Кластер и логика входа совпадают с /prompt_entry portfolio (портфельная игра).

Тикеры по умолчанию из config.env: TRADING_CYCLE_TICKERS (если задан) или TICKERS_MEDIUM + TICKERS_LONG.
Аргумент: [тикеры] — через запятую, переопределяет config.

После исполнения сделок в Telegram отправляются уведомления по сделкам портфельной игры
(не GAME_5M — те идут через send_sndk_signal_cron). TELEGRAM_BOT_TOKEN и TELEGRAM_SIGNAL_CHAT_IDS.

Cron: 0 9,13,17 * * 1-5  cd /path/to/lse && python scripts/trading_cycle_cron.py
  или с тикерами: ... trading_cycle_cron.py "MSFT,ORCL,AMD"
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config_loader import get_config_value
from execution_agent import ExecutionAgent
from services.ticker_groups import get_tickers_for_portfolio_game, get_tickers_indicator_only
from services.telegram_signal import get_signal_chat_ids, send_telegram_message
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(project_root / 'logs' / 'trading_cycle.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def _notify_portfolio_trades(agent: ExecutionAgent) -> None:
    """Отправить в Telegram уведомления о сделках портфельной игры. Сначала — сделки этого запуска (тейк/стоп), иначе — за последние 5 мин из БД."""
    token = get_config_value("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = get_signal_chat_ids()
    if not token or not chat_ids:
        return
    trades = getattr(agent, "_trades_done_this_run", None) or []
    if not trades:
        trades = agent.get_recent_trades(minutes_ago=5, exclude_strategy_name="GAME_5M")
    # Исключаем GAME_5M — те уведомляет send_sndk_signal_cron
    trades = [r for r in trades if (r.get("strategy_name") or "").strip() != "GAME_5M"]
    for r in trades:
        ts = r["ts"].strftime("%Y-%m-%d %H:%M") if hasattr(r["ts"], "strftime") else str(r["ts"])
        side_emoji = "🟢" if r["side"] == "BUY" else "🔴"
        strat = r.get("strategy_name", "—")
        text = (
            f"{side_emoji} Портфель {r['side']} {r['ticker']} x{r['quantity']:.0f} "
            f"@ ${r['price']:.2f} ({r['signal_type']}) [{strat}]\n{ts}"
        )
        for cid in chat_ids:
            try:
                # Без parse_mode: в signal_type/strategy могут быть _ или * — ломают Markdown и дают HTTP 400
                if send_telegram_message(token, cid, text, parse_mode=None):
                    logger.info("Уведомление о сделке %s %s отправлено в chat_id=%s", r["side"], r["ticker"], cid)
            except Exception as e:
                logger.warning("Не удалось отправить уведомление в %s: %s", cid, e)


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1].strip():
            tickers = [t.strip() for t in sys.argv[1].strip().split(",") if t.strip()]
            cluster_tickers = None  # аргумент — только торгуемые
        else:
            full_list = get_tickers_for_portfolio_game()
            indicator_only = set(get_tickers_indicator_only())
            tickers = [t for t in full_list if t not in indicator_only]
            # Корреляция по полному списку (включая индикаторы ^VIX и т.д.) — LLM видит связи, позиции не открываем по индикаторам
            cluster_tickers = full_list if indicator_only else None

        if not tickers:
            logging.warning("Тикеры не заданы (TRADING_CYCLE_TICKERS или TICKERS_MEDIUM/TICKERS_LONG в config.env, либо аргумент)")
            sys.exit(0)

        agent = ExecutionAgent()
        agent.run_for_tickers(tickers, cluster_tickers=cluster_tickers)
        _notify_portfolio_trades(agent)
    except Exception as e:
        logger.error("Ошибка торгового цикла: %s", e)
        sys.exit(1)



