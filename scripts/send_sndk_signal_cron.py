#!/usr/bin/env python3
"""
Поллинг 5m по быстрым бумагам (SNDK, NDK, LITE, NBIS и т.д.) и проактивная отправка сигнала о входе.

Список тикеров берётся из TICKERS_FAST (config.env). По каждому тикеру:
- при BUY/STRONG_BUY — отправка в Telegram (с cooldown по тикеру) и запись входа в игру (trade_history, GAME_5M);
- при открытой позиции и (SELL или >2 дней) — закрытие позиции в игре.

Настройка config.env:
  TELEGRAM_BOT_TOKEN=..., TELEGRAM_SIGNAL_CHAT_IDS, TICKERS_FAST, GAME_5M_COOLDOWN_MINUTES (и др. GAME_5M_*).

Аргументы: [тикеры] — если заданы, используются вместо TICKERS_FAST (через запятую).

Cron: */5 * * * 1-5  cd /path/to/lse && python scripts/send_sndk_signal_cron.py
  или с тикерами: ... send_sndk_signal_cron.py SNDK,NDK
"""

import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
import urllib.parse
import urllib.request

from config_loader import get_config_value
from services.ticker_groups import get_tickers_fast

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


def get_cooldown_minutes() -> int:
    """Cooldown между рассылками по одному тикеру (минуты). config.env: GAME_5M_COOLDOWN_MINUTES."""
    try:
        return int(get_config_value("GAME_5M_COOLDOWN_MINUTES", "120").strip())
    except (ValueError, TypeError):
        return 120


def cooldown_file(ticker: str) -> Path:
    return project_root / f".last_signal_sent_{ticker}"


def last_signal_sent_at(ticker: str) -> datetime | None:
    try:
        f = cooldown_file(ticker)
        if f.exists():
            t = float(f.read_text().strip())
            return datetime.fromtimestamp(t)
    except Exception:
        pass
    return None


def mark_signal_sent(ticker: str) -> None:
    try:
        cooldown_file(ticker).write_text(str(datetime.now().timestamp()))
    except Exception as e:
        logger.warning("Не удалось записать cooldown для %s: %s", ticker, e)


def get_signal_chat_ids() -> list[str]:
    """Список chat_id для рассылки сигналов. Без дубликатов — один чат получает сообщение один раз."""
    ids_raw = get_config_value("TELEGRAM_SIGNAL_CHAT_IDS", "").strip()
    if ids_raw:
        raw_list = [x.strip() for x in ids_raw.split(",") if x.strip()]
        # убираем дубликаты (один и тот же чат не должен получать сообщение несколько раз)
        seen = set()
        return [x for x in raw_list if x not in seen and not seen.add(x)]
    single = get_config_value("TELEGRAM_SIGNAL_CHAT_ID", "").strip()
    if single:
        return [single]
    dashboard = get_config_value("TELEGRAM_DASHBOARD_CHAT_ID", "").strip()
    if dashboard:
        return [dashboard]
    allowed = get_config_value("TELEGRAM_ALLOWED_USERS", "")
    if allowed:
        return [allowed.split(",")[0].strip()]
    return []


def get_signal_mentions() -> str:
    raw = get_config_value("TELEGRAM_SIGNAL_MENTIONS", "").strip()
    if not raw:
        return ""
    seen = set()
    parts = []
    for x in raw.split(","):
        u = x.strip()
        if u and u not in seen:
            seen.add(u)
            parts.append(u)
    return " ".join(parts) if parts else ""


def send_telegram_message(token: str, chat_id: str, text: str, parse_mode: str = "Markdown") -> bool:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": parse_mode}).encode()
    req = urllib.request.Request(TELEGRAM_SEND_URL.format(token=token), data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                logger.error("Telegram API error: %s %s", resp.status, resp.read())
                return False
            return True
    except Exception as e:
        logger.exception("Send failed: %s", e)
        return False


def process_ticker(
    token: str,
    chat_ids: list[str],
    mentions: str,
    ticker: str,
) -> bool:
    """Обрабатывает один тикер: игра (закрытие/вход) и при BUY/STRONG_BUY — рассылка. Возвращает True если хотя бы одно сообщение отправлено."""
    from services.recommend_5m import get_decision_5m
    from services.game_5m import get_open_position, close_position, should_close_position, record_entry, _effective_take_profit_pct, _effective_stop_loss_pct

    # Свечи за текущий и 5–7 предыдущих дней для анализа входа/выхода; опционально LLM перед решением
    d5 = get_decision_5m(ticker, use_llm_news=True)  # полное окно 7 дн. + KB + LLM новости
    if not d5:
        logger.debug("Нет 5m данных по %s, пропуск", ticker)
        return False

    decision = d5.get("decision", "HOLD")
    price = d5.get("price")

    # Игра: закрыть позицию по тейку/стопу. Учитываем макс. High и мин. Low за последние ~30 мин (6 свечей),
    # чтобы при кроне каждые 5 мин не проскочить фазу подъёма и фиксации прибыли (как при отскоке в начале сессии).
    momentum_2h_pct = d5.get("momentum_2h_pct")
    bar_high = d5.get("recent_bars_high_max") or d5.get("last_bar_high")
    bar_low = d5.get("recent_bars_low_min") or d5.get("last_bar_low")
    try:
        open_pos = get_open_position(ticker)
        if open_pos and price is not None:
            should_close, exit_type = should_close_position(
                open_pos, decision, price, momentum_2h_pct=momentum_2h_pct,
                bar_high=bar_high, bar_low=bar_low,
            )
            if should_close and exit_type:
                close_position(ticker, price, exit_type)
    except Exception as e:
        logger.warning("game_5m: проверка/закрытие %s: %s", ticker, e)

    if decision not in ("BUY", "STRONG_BUY"):
        return False

    # Вход только в регулярную сессию NYSE (9:30–16:00 ET). В премаркете/после закрытия торговля «плоская», ликвидность низкая.
    session_phase = (d5.get("market_session") or {}).get("session_phase") or ""
    if session_phase in ("PRE_MARKET", "AFTER_HOURS", "WEEKEND", "HOLIDAY"):
        logger.info("%s: решение BUY, но сессия=%s — вход отложен до открытия биржи", ticker, session_phase)
        return False

    cooldown_min = get_cooldown_minutes()
    if last_signal_sent_at(ticker) and (datetime.now() - last_signal_sent_at(ticker)).total_seconds() < cooldown_min * 60:
        logger.info("%s: cooldown, пропуск рассылки", ticker)
        return False

    # Не слать «Сигнал на вход», если уже в позиции — это не новый вход, ждём закрытия
    try:
        if get_open_position(ticker) is not None:
            logger.info("%s: уже в позиции, пропуск рассылки (ожидаем закрытия)", ticker)
            return False
    except Exception as e:
        logger.warning("game_5m: проверка открытой позиции %s: %s", ticker, e)

    rsi = d5.get("rsi_5m")
    mom = d5.get("momentum_2h_pct")
    vol = d5.get("volatility_5m_pct")
    period = d5.get("period_str", "")
    reasoning = (d5.get("reasoning") or "")[:200]

    lines = [
        f"🟢 **Сигнал на вход {ticker} (5m)**",
        "",
        f"**Решение:** {decision}",
        f"Цена: ${price:.2f}" if price is not None else "",
        f"RSI(5m): {rsi:.1f}" if rsi is not None else "",
        f"Импульс 2ч: {mom:+.2f}%" if mom is not None else "",
        f"Волатильность 5m: {vol:.2f}%" if vol is not None else "",
        f"_Период данных: {period}_" if period else "",
        "",
        "Параметры (интрадей): стоп −%.1f%%, тейк +%.1f%% (стоп < тейк, оба от импульса 2ч)." % (_effective_stop_loss_pct(momentum_2h_pct), _effective_take_profit_pct(momentum_2h_pct)),
        "",
        f"Подробнее: /recommend5m {ticker}",
    ]
    if reasoning:
        lines.insert(-2, f"💭 {reasoning}")

    # Влияние новостей на решение (явно учитывается в короткой игре 5m)
    news_impact = d5.get("kb_news_impact") or "нейтрально"
    lines.append("")
    lines.append(f"📰 **Учёт новостей:** {news_impact}")

    # Новости из базы за период 5m (показываем в алерте)
    kb_news = d5.get("kb_news") or []
    if kb_news:
        recent = [n for n in kb_news[:3]]  # последние 3
        parts = []
        for n in recent:
            sent = n.get("sentiment_score")
            sent_str = f" (тон {sent:.2f})" if sent is not None else ""
            content = (n.get("content") or "").strip()[:80]
            if content:
                parts.append(f"• {content}{sent_str}")
        if parts:
            lines.append("")
            lines.append("📰 **Новости из базы (за период 5m):**")
            lines.extend(parts)

    # Свежие новости/настроения от LLM (запрос непосредственно перед решением)
    llm_insight = d5.get("llm_insight")
    llm_content = (d5.get("llm_news_content") or "").strip()[:400]
    if llm_insight:
        lines.append("")
        lines.append(f"📰 **LLM (свежие новости):** {llm_insight}")
    elif llm_content:
        lines.append("")
        lines.append(f"📰 **LLM:** {llm_content}…")

    # Правило Алекса только для SNDK (дневной контекст)
    if ticker.upper() == "SNDK":
        try:
            from services.alex_rule import get_alex_rule_status
            alex = get_alex_rule_status(ticker, price)
            if alex and alex.get("message"):
                lines.append("")
                lines.append(f"📋 {alex['message']}")
        except Exception:
            pass

    text = "\n".join([s for s in lines if s])
    if mentions:
        text = mentions + "\n\n" + text

    # Игра: сначала записать вход в public.trade_history — без записи алерт не слать
    if price is None:
        logger.warning("game_5m: нет цены для %s, рассылка отменена", ticker)
        return False
    try:
        entry_id = record_entry(ticker, price, decision, reasoning)
        if entry_id is None:
            logger.error("game_5m: запись входа %s не создана (record_entry вернул None), рассылка отменена", ticker)
            return False
    except Exception as e:
        logger.exception("game_5m: ошибка записи входа %s в trade_history: %s — рассылка отменена", ticker, e)
        return False

    ok = 0
    for cid in chat_ids:
        if send_telegram_message(token, cid, text):
            ok += 1
            logger.info("Сигнал %s отправлен в chat_id=%s", ticker, cid)
        else:
            logger.error("Не удалось отправить %s в chat_id=%s", ticker, cid)
    if ok > 0:
        mark_signal_sent(ticker)
    return ok > 0


def main():
    token = get_config_value("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN не задан в config.env")
        sys.exit(1)

    chat_ids = get_signal_chat_ids()
    if not chat_ids:
        logger.warning(
            "TELEGRAM_SIGNAL_CHAT_IDS / TELEGRAM_SIGNAL_CHAT_ID не заданы — рассылка в Telegram отключена, "
            "игра (вход/выход в trade_history) продолжает работать."
        )
    mentions = get_signal_mentions()

    # При закрытой бирже не дергаем 5m (Yahoo пустой, вход/выход невозможны). Новости — отдельный крон.
    try:
        from services.market_session import get_market_session_context
        ctx = get_market_session_context()
        phase = (ctx.get("session_phase") or "").strip()
        if phase in ("PRE_MARKET", "AFTER_HOURS", "WEEKEND", "HOLIDAY"):
            logger.info("Биржа закрыта (сессия=%s), пропуск поллинга 5m до 9:30 ET", phase)
            sys.exit(0)
    except Exception as e:
        logger.debug("Проверка сессии биржи: %s", e)

    try:
        from services.recommend_5m import get_decision_5m, has_5m_data
    except ImportError as e:
        logger.error("Модуль recommend_5m недоступен: %s", e)
        sys.exit(1)

    # Тикеры: из аргумента (через запятую) или из config.env TICKERS_FAST
    if len(sys.argv) > 1 and sys.argv[1].strip():
        tickers_all = [t.strip() for t in sys.argv[1].strip().split(",") if t.strip()]
    else:
        tickers_all = get_tickers_fast()
    if not tickers_all:
        logger.warning("Тикеры не заданы (TICKERS_FAST в config.env или аргумент скрипта)")
        sys.exit(0)

    # Только тикеры с доступными 5m данными (/chart5m, игра 5m)
    tickers = [t for t in tickers_all if has_5m_data(t)]
    for t in tickers_all:
        if t not in tickers:
            logger.warning("%s: нет 5m данных (Yahoo), пропуск в этом запуске. Уберите из TICKERS_FAST или добавьте сбор.", t)

    if not tickers:
        logger.warning("Нет быстрых тикеров с 5m данными, выход")
        sys.exit(0)

    any_sent = False
    for ticker in tickers:
        if process_ticker(token, chat_ids, mentions, ticker):
            any_sent = True

    if not any_sent:
        logger.info("Нет сигналов BUY/STRONG_BUY по быстрым тикерам или cooldown")
    sys.exit(0)


if __name__ == "__main__":
    main()
