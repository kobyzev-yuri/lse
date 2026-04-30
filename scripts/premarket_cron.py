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

Cron (сервер в MSK, типичный EDT): ``session_phase == PRE_MARKET`` только когда в NY < 09:30 ET,
т.е. примерно 04:00–09:29 ET → в MSK это примерно 11:00–16:29 (сдвиг ~+7 ч к ET).
Пример двух запусков в окне премаркета: ``30 12,15 * * 1-5`` (12:30 и 15:30 MSK ≈ 5:30 и 8:30 ET).
Неверно: ``15 17`` MSK в апреле–октябре ≈ 10:15 ET — уже ``NEAR_OPEN``, Telegram не уйдёт.

Telegram: в config нужны ``PREMARKET_ALERT_TELEGRAM=true``, ``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_SIGNAL_CHAT_IDS``;
сообщение уходит только если фаза PRE_MARKET и по тикерам есть хотя бы одна строка в results.
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
        logger.info(
            "Сейчас не премаркет (phase=%s, NY=%s), выход — крон нужен только когда в NY < 09:30 ET (см. session_phase PRE_MARKET в services/market_session.py).",
            phase,
            ctx.get("et_now") or "?",
        )
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
    import urllib.error
    import html

    from services.telegram_signal import get_telegram_urllib_opener

    _tg_opener = get_telegram_urllib_opener()
    token = get_config_value("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids_raw = get_config_value("TELEGRAM_SIGNAL_CHAT_IDS", "") or get_config_value("TELEGRAM_SIGNAL_CHAT_ID", "")
    chat_ids = [x.strip() for x in chat_ids_raw.split(",") if x.strip()]
    telegram_url = f"https://api.telegram.org/bot{token}/sendMessage" if token else None

    def _send_telegram_message(text: str, *, label: str) -> None:
        if not (telegram_url and chat_ids):
            return
        data = urllib.parse.urlencode(
            {
                "chat_id": chat_ids[0],
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode()
        req = urllib.request.Request(telegram_url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with _tg_opener.open(req, timeout=15) as resp:
                if resp.status == 200:
                    logger.info("%s отправлен в Telegram", label)
                else:
                    logger.warning("Telegram (%s): %s %s", label, resp.status, resp.read())
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:
                body = b""
            logger.warning("Telegram (%s): HTTP %s %s", label, getattr(e, "code", "?"), body or str(e))
        except Exception as e:
            logger.warning("Telegram (%s): %s", label, e)

    # Опционально: алерт в Telegram (премаркет по тикерам)
    alert = get_config_value("PREMARKET_ALERT_TELEGRAM", "").strip().lower() in ("1", "true", "yes")
    entry_preview_5m = get_config_value("PREMARKET_ENTRY_PREVIEW_5M", "").strip().lower() in ("1", "true", "yes")
    if alert and results and token and chat_ids:
        lines = ["📊 <b>Премаркет</b> (до открытия US). Цена — последняя минута Yahoo (prepost)."]
        for item in results:
            ticker, prev, last, gap, mins = item[0], item[1], item[2], item[3], item[4]
            last_time = item[5] if len(item) > 5 else None
            gap_str = f"{gap:+.2f}%" if gap is not None else "—"
            mins_str = f"{mins} мин" if mins is not None else "—"
            prev_str = f", вчера close {prev}" if prev is not None else ""
            time_str = f" ({last_time})" if last_time is not None else ""
            lines.append(
                "• "
                + html.escape(str(ticker))
                + ": премаркет "
                + html.escape(str(last))
                + html.escape(str(time_str))
                + " гэп "
                + html.escape(str(gap_str))
                + html.escape(str(prev_str))
                + " до открытия "
                + html.escape(str(mins_str))
            )
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
                                preview_parts.append(
                                    "• "
                                    + html.escape(str(t))
                                    + ": "
                                    + html.escape(str(dec))
                                    + " ($"
                                    + html.escape(f"{pr:.2f}{rsi_str}")
                                    + ")"
                                )
                        except Exception:
                            continue
                    if preview_parts:
                        lines.append("")
                        lines.append("🎯 <b>Прогноз на вступление (5m)</b> — по последним данным до открытия:")
                        lines.extend(preview_parts)
            except Exception as e:
                logger.debug("Прогноз на вступление 5m: %s", e)
        text = "\n".join(lines)
        _send_telegram_message(text, label="Премаркет-алерт")
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
                    "⚠️ <b>Премаркет-стресс (геополитика/риск)</b>",
                    "Сильный гэп вниз по индикаторам:",
                ]
                for t, g in stress_details:
                    lines_stress.append(f"• {html.escape(str(t))}: гэп {g:+.2f}%")
                lines_stress.append("")
                lines_stress.append(
                    "Рекомендуется закрыть позиции <b>GAME_5m</b> и рассмотреть закрытие прочих до открытия сессии."
                    + html.escape(day_note)
                )
                text_stress = "\n".join(lines_stress)
                _send_telegram_message(text_stress, label="Премаркет-стресс алерт")
        except Exception as e:
            logger.warning("Проверка премаркет-стресс: %s", e)

    sys.exit(0)


if __name__ == "__main__":
    main()
