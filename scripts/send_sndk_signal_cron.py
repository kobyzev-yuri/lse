#!/usr/bin/env python3
"""
Поллинг 5m по быстрым бумагам (игра 5m) и проактивная отправка прогноза для вступления в игру.

Приоритет: прогнозы для вступления в игру — трейдер играет сам, ему нужна информация, по возможности раньше.
Система даёт сигнал (BUY/STRONG_BUY) и параметры входа; решение о сделке принимает трейдер.

Список тикеров: GAME_5M_TICKERS или TICKERS_FAST (config.env). По каждому тикеру:
- при BUY/STRONG_BUY — отправка в Telegram (с cooldown по тикеру) и запись входа в игру (trade_history, GAME_5M);
- при открытой позиции и (SELL или >2 дней) — закрытие позиции в игре.

Настройка config.env:
  TELEGRAM_BOT_TOKEN=..., TELEGRAM_SIGNAL_CHAT_IDS, TICKERS_FAST, GAME_5M_COOLDOWN_MINUTES (и др. GAME_5M_*).

Аргументы: [тикеры] — если заданы, используются вместо GAME_5M_TICKERS/TICKERS_FAST (через запятую).

Cron: */5 * * * 1-5  cd /path/to/lse && python scripts/send_sndk_signal_cron.py
  (каждые 5 мин — чтобы прогноз для вступления приходил по возможности раньше)
"""

import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging

from config_loader import get_config_value, get_dynamic_config_value
from services.ticker_groups import get_tickers_fast, get_tickers_game_5m, get_tickers_for_5m_correlation
from services.telegram_signal import get_signal_chat_ids, send_telegram_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


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


def clear_cooldown(ticker: str) -> None:
    """Сбрасывает cooldown по тикеру (после закрытия позиции — следующий запуск может отправить новый вход)."""
    try:
        f = cooldown_file(ticker)
        if f.exists():
            f.unlink()
            logger.info("%s: cooldown сброшен после закрытия позиции → в следующем запуске при BUY сигнал будет отправлен", ticker)
    except Exception as e:
        logger.warning("Не удалось сбросить cooldown для %s: %s", ticker, e)


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


def process_ticker(
    token: str,
    chat_ids: list[str],
    mentions: str,
    ticker: str,
    d5_precomputed: dict | None = None,
    cluster_context: dict | None = None,
) -> bool:
    """Обрабатывает один тикер: игра (закрытие/вход) и при BUY/STRONG_BUY — рассылка. Возвращает True если хотя бы одно сообщение отправлено.
    d5_precomputed: при кластерном запуске — готовое решение из get_cluster_decisions_5m; иначе вызывается get_decision_5m(ticker).
    cluster_context: при GAME_5M_ENTRY_STRATEGY=llm — {decisions, correlation, tickers} для вызова LLM с контекстом корреляций."""
    from services.recommend_5m import get_decision_5m, get_5m_card_payload, build_5m_close_context
    from services.game_5m import get_open_position, get_open_position_any, close_position, should_close_position, record_entry, _effective_take_profit_pct, _effective_stop_loss_pct, _game_5m_stop_loss_enabled

    d5 = d5_precomputed
    if d5 is None:
        try:
            d5 = get_decision_5m(ticker, use_llm_news=True)
        except Exception as e:
            logger.warning("get_decision_5m(%s): %s", ticker, e)
            return False
    if not d5:
        logger.debug("Нет 5m данных по %s, пропуск", ticker)
        return False

    card = get_5m_card_payload(d5, ticker)
    close_ctx = build_5m_close_context(d5)
    # Выход (тейк/стоп/SELL) — по чистым правилам; вход и LLM — по technical_decision_effective (CatBoost fusion).
    decision_exit = d5.get("technical_decision_core") or card.get("decision", "HOLD")
    decision_entry = d5.get("technical_decision_effective") or card.get("decision", "HOLD")
    decision = decision_entry
    price = card.get("price")
    rsi_5m = card.get("rsi_5m")
    momentum_2h_pct = card.get("momentum_2h_pct")
    bar_high = close_ctx.get("bar_high")
    bar_low = close_ctx.get("bar_low")

    outcome_lines = []  # итоговая причина для лога
    closed_this_run = False  # закрыли позицию в этом запуске — отправим уведомление и сбросим cooldown
    close_price = close_exit_type = close_entry_price = None

    try:
        # Сначала «любая» позиция по тикеру (как в /pending), иначе только GAME_5M — чтобы видеть MU и др. при другой стратегии
        open_pos = get_open_position_any(ticker) or get_open_position(ticker)
        has_pos = open_pos is not None
        price_ok = price is not None and price > 0
        strategy_label = (" [%s]" % open_pos.get("strategy_name")) if (has_pos and open_pos.get("strategy_name")) else ""
        _dec_log = decision_exit
        if decision_entry != decision_exit:
            _dec_log = "%s (вход=%s)" % (decision_exit, decision_entry)
        logger.info(
            "[5m] %s: открытая_позиция=%s%s, цена_5m=%s, решение=%s",
            ticker, "да" if has_pos else "нет", strategy_label, "%.2f" % price if price_ok else "нет", _dec_log,
        )

        if has_pos and price_ok:
            try:
                from report_generator import get_engine, get_latest_prices
                engine = get_engine()
                quotes_prices = get_latest_prices(engine, [ticker])
                price_quotes = quotes_prices.get(ticker)
            except Exception:
                price_quotes = None
            price_for_check = max(price, price_quotes) if (price_quotes is not None and price_quotes > 0) else price
            should_close, exit_type = should_close_position(
                open_pos, decision_exit, price_for_check, momentum_2h_pct=momentum_2h_pct,
                bar_high=bar_high, bar_low=bar_low,
                rsi_5m=rsi_5m,
            )
            entry = open_pos.get("entry_price")
            entry_f = float(entry) if entry is not None and entry > 0 else None

            if should_close and exit_type:
                base_exit = close_ctx.get("exit_bar_close") if isinstance(close_ctx.get("exit_bar_close"), (int, float)) and close_ctx.get("exit_bar_close") > 0 else price_for_check
                if exit_type == "TAKE_PROFIT":
                    # В БД пишем цену закрытия бара (exit_bar_close), чтобы на графике маркер «Тейк» был на линии Close, а не выше (bar_high).
                    # Решение о тейке принято по bar_high; для отображения и PnL используем close бара.
                    if base_exit and base_exit > 0:
                        exit_price = base_exit
                    elif bar_high is not None and bar_high > 0:
                        exit_price = bar_high
                    else:
                        exit_price = base_exit
                elif exit_type == "STOP_LOSS" and bar_low is not None and bar_low > 0:
                    exit_price = max(base_exit, bar_low)
                else:
                    exit_price = base_exit
                logger.info(
                    "[5m] %s закрытие: тип=%s, exit_bar_close=%s, price_5m=%.2f, bar_high=%s, bar_low=%s → exit_price=%.2f",
                    ticker, exit_type, close_ctx.get("exit_bar_close"), price, bar_high, bar_low, exit_price,
                )
                close_position(
                    ticker, exit_price, exit_type, position=open_pos,
                    bar_high=bar_high if exit_type == "TAKE_PROFIT" else None,
                    bar_low=bar_low if exit_type == "STOP_LOSS" else None,
                    context_json=close_ctx,
                )
                outcome_lines.append("позиция закрыта по %s @ %.2f" % (exit_type, exit_price))
                closed_this_run = True
                close_exit_type = exit_type
                # Цена входа/выхода из БД (как в /closed), чтобы уведомление совпадало с отчётом
                try:
                    from report_generator import get_engine, get_last_closed_for_ticker
                    last_closed = get_last_closed_for_ticker(get_engine(), ticker, "GAME_5M")
                    if last_closed:
                        close_entry_price = last_closed.entry_price
                        close_price = last_closed.exit_price
                    else:
                        close_entry_price = entry_f
                        close_price = exit_price
                except Exception as e:
                    logger.debug("get_last_closed_for_ticker: %s", e)
                    close_entry_price = entry_f
                    close_price = exit_price
            else:
                if entry_f:
                    pnl_pct = (price_for_check - entry_f) / entry_f * 100.0
                    take_pct = _effective_take_profit_pct(momentum_2h_pct, ticker=ticker)
                    if _game_5m_stop_loss_enabled():
                        stop_pct = _effective_stop_loss_pct(momentum_2h_pct, ticker=ticker)
                        outcome_lines.append(
                            "позиция открыта: вход=%.2f, текущая=%.2f, pnl=%.2f%%, тейк=%.1f%%, стоп=%.1f%% — тейк/стоп не сработали"
                            % (entry_f, price_for_check, pnl_pct, take_pct, stop_pct)
                        )
                    else:
                        outcome_lines.append(
                            "позиция открыта: вход=%.2f, текущая=%.2f, pnl=%.2f%%, тейк=%.1f%% (стоп выкл.) — тейк не сработал"
                            % (entry_f, price_for_check, pnl_pct, take_pct)
                        )
                else:
                    outcome_lines.append("позиция есть, но entry_price неизвестен — проверка пропущена")
        elif has_pos and not price_ok:
            outcome_lines.append("позиция есть, цена 5m отсутствует — проверка тейка/стопа пропущена")
        else:
            outcome_lines.append("позиции нет")

        if decision_entry not in ("BUY", "STRONG_BUY"):
            outcome_lines.append(
                "сигнал на вход %s → нет рассылки (правила: %s)" % (decision_entry, decision_exit)
            )
    except Exception as e:
        logger.warning("game_5m: проверка/закрытие %s: %s", ticker, e)
        outcome_lines.append("ошибка при проверке: %s" % e)

    logger.info("[5m] %s: итог — %s", ticker, "; ".join(outcome_lines))

    # Сразу после закрытия: уведомление в Telegram и установка cooldown (не открывать снова сразу — ждать GAME_5M_COOLDOWN_MINUTES)
    if closed_this_run and close_exit_type:
        mark_signal_sent(ticker)
        logger.info("%s: после закрытия установлен cooldown → следующий вход не раньше чем через %d мин", ticker, get_cooldown_minutes())
        if chat_ids:
            pnl_str = ""
            if close_entry_price is not None and close_price is not None and close_entry_price > 0:
                pnl_pct = (close_price - close_entry_price) / close_entry_price * 100.0
                pnl_str = ", PnL %+.2f%%" % pnl_pct
            close_msg = "🔒 **5m:** %s закрыта по %s @ %.2f%s. Открытые позиции: /pending" % (
                ticker, close_exit_type, close_price or 0, pnl_str,
            )
            ok = 0
            for cid in chat_ids:
                if send_telegram_message(token, cid, close_msg, parse_mode=None):
                    ok += 1
            if ok > 0:
                logger.info("[5m] %s: отправлено уведомление о закрытии по %s @ %.2f (в этом запуске новый вход не слать)", ticker, close_exit_type, close_price or 0)
                return True
        return True  # закрыли в этом запуске — не слать новый вход в том же запуске

    if decision_entry not in ("BUY", "STRONG_BUY"):
        return False

    # Вход только в регулярную сессию NYSE (9:30–16:00 ET). В премаркете/после закрытия торговля «плоская», ликвидность низкая.
    session_phase = (d5.get("market_session") or {}).get("session_phase") or ""
    if session_phase in ("PRE_MARKET", "AFTER_HOURS", "WEEKEND", "HOLIDAY"):
        logger.info("%s: решение BUY, позиции нет, но сессия=%s — пропуск рассылки (вход только в регулярную сессию 9:30–16:00 ET)", ticker, session_phase)
        return False

    cooldown_min = get_cooldown_minutes()
    last_sent = last_signal_sent_at(ticker)
    if last_sent:
        elapsed_sec = (datetime.now() - last_sent).total_seconds()
        if elapsed_sec < cooldown_min * 60:
            mins_ago = int(elapsed_sec / 60)
            logger.info(
                "%s: решение BUY, позиции нет, но пропуск рассылки — cooldown (последняя рассылка %d мин назад, лимит %d мин). Следующий вход: после истечения cooldown или после закрытия позиции.",
                ticker, mins_ago, cooldown_min,
            )
            return False

    # Не слать «Сигнал на вход», если уже в позиции — это не новый вход, ждём закрытия
    try:
        if get_open_position_any(ticker) is not None:
            logger.info("%s: решение BUY, но уже в позиции — пропуск рассылки (ожидаем закрытия по тейку/стопу)", ticker)
            return False
    except Exception as e:
        logger.warning("game_5m: проверка открытой позиции %s: %s", ticker, e)

    # Стратегия входа: technical (по умолчанию) или llm — с учётом корреляций (для тестирования)
    entry_strategy = (get_config_value("GAME_5M_ENTRY_STRATEGY", "technical") or "technical").strip().lower()
    if entry_strategy == "llm" and cluster_context and cluster_context.get("correlation"):
        try:
            from services.cluster_recommend import build_cluster_note_for_5m_llm, get_avg_volatility_20_pct_from_quotes
            from services.llm_service import get_llm_service
            decisions_map = cluster_context.get("decisions") or {}
            full_list = cluster_context.get("tickers") or list(decisions_map.keys())
            tech_by_ticker = {
                t: {"price": d.get("price"), "rsi": d.get("rsi_5m")}
                for t, d in decisions_map.items() if d
            }
            cluster_note = build_cluster_note_for_5m_llm(ticker, full_list, cluster_context.get("correlation"), tech_by_ticker)
            if cluster_note:
                llm = get_llm_service()
                if getattr(llm, "client", None):
                    technical_data = {
                        "close": d5.get("price"),
                        "rsi": d5.get("rsi_5m"),
                        "volatility_5": d5.get("volatility_5m_pct"),
                        "avg_volatility_20": get_avg_volatility_20_pct_from_quotes(ticker),
                        "technical_signal": decision_entry,
                        "technical_signal_core": d5.get("technical_decision_core") or d5.get("decision"),
                        "catboost_entry_proba_good": d5.get("catboost_entry_proba_good"),
                        "catboost_signal_status": d5.get("catboost_signal_status"),
                        "catboost_fusion_note": d5.get("catboost_fusion_note"),
                        "cluster_note": cluster_note,
                        "momentum_2h_pct": d5.get("momentum_2h_pct"),
                        "take_profit_pct": d5.get("take_profit_pct"),
                        "stop_loss_pct": d5.get("stop_loss_pct"),
                        "estimated_upside_pct_day": d5.get("estimated_upside_pct_day"),
                        "price_forecast_5m": d5.get("price_forecast_5m"),
                        "price_forecast_5m_summary": d5.get("price_forecast_5m_summary"),
                    }
                    kb_impact = d5.get("kb_news_impact") or ""
                    kb_news = d5.get("kb_news") or []
                    news_list = [
                        {"source": "KB", "content": (n.get("content") or n.get("title") or "")[:200], "sentiment_score": float(n.get("sentiment_score", 0.5))}
                        for n in kb_news[:5]
                    ] if kb_news else []
                    if not news_list and kb_impact:
                        news_list = [{"source": "KB", "content": kb_impact[:500], "sentiment_score": 0.5}]
                    sentiment = 0.5
                    if "негатив" in (kb_impact or "").lower():
                        sentiment = 0.35
                    elif "позитив" in (kb_impact or "").lower():
                        sentiment = 0.65
                    result = llm.analyze_trading_situation(
                        ticker, technical_data, news_list, sentiment,
                        strategy_name="GAME_5M", strategy_signal=decision_entry,
                    )
                    if result and result.get("llm_analysis"):
                        llm_decision = (result["llm_analysis"].get("decision") or "").strip().upper()
                        if llm_decision not in ("BUY", "STRONG_BUY"):
                            logger.info(
                                "%s: стратегия входа=llm, LLM дал %s — вход не выполняем (тех.итог был %s, правила %s)",
                                ticker, llm_decision or "—", decision_entry, decision_exit,
                            )
                            return False
                        decision = llm_decision
                        ana = result["llm_analysis"]
                        reasoning = (ana.get("reasoning") or d5.get("reasoning") or "")[:500]
                        llm_key_factors = ana.get("key_factors")
                        if not isinstance(llm_key_factors, list):
                            llm_key_factors = [llm_key_factors] if llm_key_factors else None
                        d5 = dict(d5, decision=decision, reasoning=reasoning, entry_strategy="llm", llm_key_factors=llm_key_factors)
                        logger.info("%s: стратегия входа=llm, решение LLM: %s", ticker, decision)
                else:
                    logger.debug("%s: стратегия llm, но LLM недоступен — используем технический вход", ticker)
            else:
                logger.debug("%s: стратегия llm, cluster_note пустой — используем технический вход", ticker)
        except Exception as e:
            logger.warning("game_5m: LLM для входа %s: %s — используем технический вход", ticker, e)

    logger.info("[5m] %s: отправка сигнала на вход (BUY, позиции нет, cooldown пройден)", ticker)
    from services.signal_message_5m import build_5m_entry_signal_text
    text = build_5m_entry_signal_text(d5, ticker, mentions=mentions)

    # Игра: сначала записать вход в public.trade_history — без записи алерт не слать
    if price is None:
        logger.warning("game_5m: нет цены для %s, рассылка отменена", ticker)
        return False
    reasoning = (d5.get("reasoning") or "")[:500]
    try:
        # Полный дамп параметров (prompt_entry-уровень) для новых сделок; старые — упрощённый формат
        from services.deal_params_5m import build_full_entry_context
        from services.cluster_recommend import extract_correlation_features_for_5m_entry

        corr_feats = None
        if cluster_context and cluster_context.get("correlation"):
            try:
                corr_feats = extract_correlation_features_for_5m_entry(
                    ticker,
                    cluster_context["correlation"],
                    get_tickers_game_5m(),
                )
            except Exception as e:
                logger.debug("correlation features для context_json %s: %s", ticker, e)
        entry_context = (
            build_full_entry_context(d5, correlation_entry_features=corr_feats) if d5 else None
        )
        if entry_context is not None and not entry_context:
            entry_context = None
        entry_id = record_entry(ticker, price, decision, reasoning, entry_context=entry_context)
        if entry_id is None:
            logger.error("game_5m: запись входа %s не создана (record_entry вернул None), рассылка отменена", ticker)
            return False
    except Exception as e:
        logger.exception("game_5m: ошибка записи входа %s в trade_history: %s — рассылка отменена", ticker, e)
        return False

    # Лог для проверки: крон и бот должны подключаться к одной БД (см. docs/CRONS_AND_TAKE_STOP.md §6)
    try:
        from config_loader import get_database_url
        import re
        url = get_database_url()
        m = re.match(r"postgresql://[^@]+@([^:/]+)(?::(\d+))?/([^?]+)", url)
        if m:
            logger.info("[5m] БД (для проверки /pending): host=%s port=%s database=%s", m.group(1), m.group(2) or "5432", m.group(3))
    except Exception:
        pass

    # Напоминание: позиция уже в trade_history; смотреть в /pending (бот и крон должны использовать один DATABASE_URL в config.env)
    text = text.strip()
    if "/pending" not in text:
        text += "\n\n📋 Позиция записана в игру 5m. Открытые позиции: /pending"

    ok = 0
    # Без parse_mode: в тексте бывают reasoning/новости с _ * ` — ломают Markdown и дают 400
    for cid in chat_ids:
        if send_telegram_message(token, cid, text, parse_mode=None):
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

    # При закрытой бирже: в AFTER_HOURS всё равно проверяем открытые позиции (тейк/стоп по последним барам),
    # чтобы не пропустить фиксацию, если цена ушла выше тейка в конце сессии (16:00 ET). Остальные фазы — пропуск.
    try:
        from services.market_session import get_market_session_context
        from services.game_5m import get_open_position, get_open_position_any, close_position, should_close_position
        from services.recommend_5m import get_decision_5m, has_5m_data, build_5m_close_context
        ctx = get_market_session_context()
        phase = (ctx.get("session_phase") or "").strip()
        if phase in ("PRE_MARKET", "WEEKEND", "HOLIDAY"):
            logger.info("Биржа закрыта (сессия=%s), пропуск поллинга 5m до 9:30 ET", phase)
            sys.exit(0)
        if phase == "AFTER_HOURS":
            # Тикеры для проверки: из аргумента или config (get_tickers_game_5m импортирован вверху)
            tickers_ah = [t.strip() for t in (sys.argv[1].strip().split(",") if len(sys.argv) > 1 and sys.argv[1].strip() else get_tickers_game_5m()) if t.strip()]
            tickers_ah = [t for t in tickers_ah if has_5m_data(t)]
            for ticker in tickers_ah:
                try:
                    open_pos = get_open_position_any(ticker) or get_open_position(ticker)
                    if not open_pos:
                        continue
                    d5 = get_decision_5m(ticker, use_llm_news=False)
                    if not d5 or d5.get("price") is None:
                        continue
                    price = d5.get("price")
                    try:
                        from report_generator import get_engine, get_latest_prices
                        quotes_prices = get_latest_prices(get_engine(), [ticker])
                        pq = quotes_prices.get(ticker)
                        price_for_check = max(price, pq) if (pq is not None and pq > 0) else price
                    except Exception:
                        price_for_check = price
                    close_ctx_ah = build_5m_close_context(d5)
                    bar_high = close_ctx_ah.get("bar_high")
                    bar_low = close_ctx_ah.get("bar_low")
                    should_close, exit_type = should_close_position(
                        open_pos,
                        d5.get("technical_decision_core") or d5.get("decision", "HOLD"),
                        price_for_check,
                        momentum_2h_pct=close_ctx_ah.get("momentum_2h_pct"),
                        bar_high=bar_high, bar_low=bar_low,
                        rsi_5m=close_ctx_ah.get("rsi_5m"),
                    )
                    if should_close and exit_type:
                        base_exit = close_ctx_ah.get("exit_bar_close") if isinstance(close_ctx_ah.get("exit_bar_close"), (int, float)) and close_ctx_ah.get("exit_bar_close") > 0 else price_for_check
                        exit_price = base_exit
                        if exit_type == "TAKE_PROFIT" and bar_high is not None and bar_high > 0:
                            exit_price = min(exit_price, bar_high)
                        elif exit_type == "STOP_LOSS" and bar_low is not None and bar_low > 0:
                            exit_price = max(exit_price, bar_low)
                        close_position(
                            ticker, exit_price, exit_type, position=open_pos,
                            bar_high=bar_high if exit_type == "TAKE_PROFIT" else None,
                            bar_low=bar_low if exit_type == "STOP_LOSS" else None,
                            context_json=close_ctx_ah,
                        )
                        logger.info("AFTER_HOURS: закрыта позиция %s @ %.2f (%s)", ticker, exit_price, exit_type)
                except Exception as e:
                    logger.warning("AFTER_HOURS проверка %s: %s", ticker, e)
            logger.info("Биржа закрыта (AFTER_HOURS), проверка открытых позиций выполнена, выход")
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
        tickers_all = get_tickers_game_5m()
    if not tickers_all:
        logger.warning("Тикеры не заданы (GAME_5M_TICKERS или TICKERS_FAST в config.env, или аргумент скрипта)")
        sys.exit(0)

    # Только тикеры с доступными 5m данными (/chart5m, игра 5m)
    tickers = [t for t in tickers_all if has_5m_data(t)]
    skipped = [t for t in tickers_all if t not in tickers]
    logger.info("Игра 5m: тикеры из конфига %s → обрабатываем %s", tickers_all, tickers)
    for t in skipped:
        logger.warning("%s: нет 5m данных (Yahoo), пропуск в этом запуске. Вернуть в игру: запуск в часы торгов или проверка Yahoo.", t)

    if not tickers:
        logger.warning("Нет быстрых тикеров с 5m данными, выход")
        sys.exit(0)

    # Кластерный анализ: корреляция для LLM — load_game5m_llm_correlation() внутри get_cluster_decisions_5m (как веб и Telegram GAME5M)
    cluster_decisions = None
    try:
        from services.cluster_recommend import get_cluster_decisions_5m
        cluster_decisions = get_cluster_decisions_5m(tickers, days=5, use_llm_news=True)
        if cluster_decisions and cluster_decisions.get("correlation"):
            logger.info(
                "[5m] Кластер: корреляция для универсa %s",
                cluster_decisions.get("correlation_tickers") or get_tickers_for_5m_correlation(),
            )
    except Exception as e:
        logger.debug("Кластер 5m (fallback на потикерный вызов): %s", e)

    any_sent = False
    for ticker in tickers:
        d5_pre = (cluster_decisions.get("decisions") or {}).get(ticker) if cluster_decisions else None
        if process_ticker(token, chat_ids, mentions, ticker, d5_precomputed=d5_pre, cluster_context=cluster_decisions):
            any_sent = True

    if not any_sent:
        logger.info(
            "[5m] Рассылка: ни одного сообщения не отправлено. Причины по каждому тикеру — см. строки выше: "
            "cooldown (минуты назад/лимит), уже в позиции, решение HOLD, сессия не торговая."
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
