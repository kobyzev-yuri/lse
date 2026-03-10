#!/usr/bin/env python3
"""
Опциональный крон: премаркет-поллинг за 30–60 мин до открытия US.
Собирает контекст премаркета по TICKERS_FAST (и опционально тикерам портфельной игры),
логирует в logs/premarket_cron.log. При PREMARKET_ALERT_TELEGRAM=true отправляет алерт в Telegram.

Прогноз для вступления: при PREMARKET_ENTRY_PREVIEW_5M=true в алерт добавляется блок
«Прогноз на вступление (5m)» по тикерам игры 5m (по последним 5m данным) — чтобы трейдер видел
картину до открытия и мог подготовиться.

При сильном гэпе вниз по «индикаторам стресса» (Forex и др., PREMARKET_STRESS_TICKERS) отправляется
доп. алерт: рекомендуется закрыть позиции GAME_5m и рассмотреть закрытие прочих до открытия.

Запуск вручную: python scripts/premarket_cron.py
Cron (пример: 8:30 ET = 13:30 UTC зимой): 30 13 * * 1-5  cd /path/to/lse && python scripts/premarket_cron.py
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
from datetime import datetime

log_dir = project_root / "logs"
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_dir / "premarket_cron.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def main() -> None:
    try:
        from services.market_session import get_market_session_context
        from services.premarket import get_premarket_context
        from services.ticker_groups import get_tickers_fast, get_tickers_for_portfolio_game
        from config_loader import get_config_value
    except ImportError as e:
        logger.error("Импорт: %s", e)
        sys.exit(1)

    ctx = get_market_session_context()
    phase = (ctx.get("session_phase") or "").strip()
    if phase != "PRE_MARKET":
        logger.info("Сейчас не премаркет (phase=%s), выход", phase)
        sys.exit(0)

    # Тикеры: FAST + опционально портфельные (без дубликатов)
    fast = get_tickers_fast()
    try:
        portfolio = get_tickers_for_portfolio_game()
    except Exception:
        portfolio = []
    seen = set()
    tickers = []
    for t in fast + portfolio:
        if t not in seen:
            seen.add(t)
            tickers.append(t)

    if not tickers:
        logger.warning("Нет тикеров для премаркета")
        sys.exit(0)

    results = []
    for ticker in tickers:
        try:
            pm = get_premarket_context(ticker)
            if pm.get("error"):
                logger.warning("%s: %s", ticker, pm.get("error"))
                continue
            prev = pm.get("prev_close")
            last = pm.get("premarket_last")
            gap = pm.get("premarket_gap_pct")
            mins = pm.get("minutes_until_open")
            last_time = pm.get("premarket_last_time_et")  # время последней минуты (Yahoo), чтобы сравнивать с «там»
            results.append((ticker, prev, last, gap, mins, last_time))
            logger.info(
                "%s: prev_close=%s premarket_last=%s (на %s ET) gap_pct=%s min_to_open=%s",
                ticker, prev, last, last_time, gap, mins,
            )
        except Exception as e:
            logger.warning("%s: %s", ticker, e)

    # Подготовка Telegram (для обычного алерта и для стресс-алерта)
    import urllib.parse
    import urllib.request
    token = get_config_value("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids_raw = get_config_value("TELEGRAM_SIGNAL_CHAT_IDS", "") or get_config_value("TELEGRAM_SIGNAL_CHAT_ID", "")
    chat_ids = [x.strip() for x in chat_ids_raw.split(",") if x.strip()]
    telegram_url = f"https://api.telegram.org/bot{token}/sendMessage" if token else None

    # Опционально: алерт в Telegram (премаркет по тикерам)
    alert = get_config_value("PREMARKET_ALERT_TELEGRAM", "").strip().lower() in ("1", "true", "yes")
    entry_preview_5m = get_config_value("PREMARKET_ENTRY_PREVIEW_5M", "").strip().lower() in ("1", "true", "yes")
    if alert and results and token and chat_ids:
        lines = ["📊 **Премаркет** (до открытия US). Цена — последняя минута Yahoo (prepost)."]
        for item in results:
            ticker, prev, last, gap, mins = item[0], item[1], item[2], item[3], item[4]
            last_time = item[5] if len(item) > 5 else None
            gap_str = f"{gap:+.2f}%" if gap is not None else "—"
            mins_str = f"{mins} мин" if mins is not None else "—"
            prev_str = f", вчера close {prev}" if prev is not None else ""
            time_str = f" ({last_time})" if last_time is not None else ""
            lines.append(f"• {ticker}: премаркет {last}{time_str} гэп {gap_str}{prev_str} до открытия {mins_str}")
        # Прогноз на вступление (5m): по тикерам игры 5m — решение до открытия (трейдеру информация по возможности раньше)
        if entry_preview_5m:
            try:
                from services.ticker_groups import get_tickers_game_5m
                from services.recommend_5m import get_decision_5m
                game5m = get_tickers_game_5m()
                if game5m:
                    preview_parts = []
                    for t in game5m:
                        try:
                            d5 = get_decision_5m(t, days=2, use_llm_news=False)
                            if d5 and d5.get("price") is not None:
                                dec = d5.get("decision", "—")
                                pr = d5.get("price")
                                rsi = d5.get("rsi_5m")
                                rsi_str = f", RSI {rsi:.0f}" if rsi is not None else ""
                                preview_parts.append(f"• {t}: {dec} (${pr:.2f}{rsi_str})")
                        except Exception:
                            continue
                    if preview_parts:
                        lines.append("")
                        lines.append("🎯 **Прогноз на вступление (5m)** — по последним данным до открытия:")
                        lines.extend(preview_parts)
            except Exception as e:
                logger.debug("Прогноз на вступление 5m: %s", e)
        text = "\n".join(lines)
        data = urllib.parse.urlencode({"chat_id": chat_ids[0], "text": text, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(telegram_url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    logger.info("Премаркет-алерт отправлен в Telegram")
                else:
                    logger.warning("Telegram: %s %s", resp.status, resp.read())
        except Exception as e:
            logger.warning("Отправка алерта: %s", e)
    elif alert and (not token or not chat_ids):
        logger.debug("PREMARKET_ALERT_TELEGRAM=true, но нет TELEGRAM_BOT_TOKEN или chat id")

    # Опционально: алерт «геополитический/премаркет-стресс» — сильный гэп вниз по Forex/индикаторам
    # Рекомендация: закрыть GAME_5m и рассмотреть закрытие прочих позиций (в пятницу — до выходных)
    stress_tickers_raw = get_config_value("PREMARKET_STRESS_TICKERS", "").strip()
    stress_gap_threshold = -1.5
    try:
        stress_gap_threshold = float(get_config_value("PREMARKET_STRESS_GAP_PCT", "-1.5").strip())
    except (ValueError, TypeError):
        pass
    friday_only = get_config_value("PREMARKET_STRESS_ALERT_FRIDAY_ONLY", "true").strip().lower() in ("1", "true", "yes")
    stress_tickers = [t.strip() for t in stress_tickers_raw.split(",") if t.strip()]
    stress_detected = False
    stress_details = []
    if stress_tickers:
        try:
            from datetime import timezone
            try:
                from zoneinfo import ZoneInfo
                et_now = datetime.now(ZoneInfo("America/New_York"))
            except ImportError:
                et_now = datetime.now(timezone.utc)
            is_friday = et_now.weekday() == 4
            for ticker in stress_tickers:
                pm = get_premarket_context(ticker)
                if pm.get("error"):
                    continue
                gap = pm.get("premarket_gap_pct")
                if gap is not None and gap <= stress_gap_threshold:
                    stress_detected = True
                    stress_details.append((ticker, gap))
                    logger.info("Стресс-индикатор: %s гэп %.2f%% <= %.2f%%", ticker, gap, stress_gap_threshold)
            if stress_detected and (not friday_only or is_friday) and token and chat_ids:
                day_note = " В пятницу — по возможности закрыть позиции до выходных." if is_friday else ""
                lines_stress = [
                    "⚠️ **Премаркет-стресс (геополитика/риск)**",
                    "Сильный гэп вниз по индикаторам:",
                ]
                for t, g in stress_details:
                    lines_stress.append(f"• {t}: гэп {g:+.2f}%")
                lines_stress.append("")
                lines_stress.append("Рекомендуется закрыть позиции **GAME_5m** и рассмотреть закрытие прочих до открытия сессии." + day_note)
                text_stress = "\n".join(lines_stress)
                data_stress = urllib.parse.urlencode({"chat_id": chat_ids[0], "text": text_stress, "parse_mode": "Markdown"}).encode()
                req_stress = urllib.request.Request(telegram_url, data=data_stress, method="POST")
                req_stress.add_header("Content-Type", "application/x-www-form-urlencoded")
                try:
                    with urllib.request.urlopen(req_stress, timeout=15) as resp2:
                        if resp2.status == 200:
                            logger.info("Алерт премаркет-стресс (закрыть позиции) отправлен в Telegram")
                        else:
                            logger.warning("Telegram стресс-алерт: %s %s", resp2.status, resp2.read())
                except Exception as e:
                    logger.warning("Отправка алерта стресс: %s", e)
        except Exception as e:
            logger.warning("Проверка премаркет-стресс: %s", e)

    sys.exit(0)


if __name__ == "__main__":
    main()
