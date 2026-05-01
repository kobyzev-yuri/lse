#!/usr/bin/env python3
"""
Поллинг 5m по быстрым бумагам (игра 5m) и проактивная отправка прогноза для вступления в игру.

Приоритет: прогнозы для вступления в игру — трейдер играет сам, ему нужна информация, по возможности раньше.
Система даёт сигнал (BUY/STRONG_BUY) и параметры входа; решение о сделке принимает трейдер.

Список тикеров: GAME_5M_TICKERS или TICKERS_FAST (config.env). По каждому тикеру:
- при BUY/STRONG_BUY — отправка в Telegram (с cooldown по тикеру) и запись входа в игру (trade_history, GAME_5M);
- при открытой позиции и (SELL или >2 дней) — закрытие позиции в игре.
- в фазе AFTER_HOURS (после 16:00 ET) закрытия тейка/стопа идут отдельным циклом — уведомление в Telegram то же, что при дневном кроне (send_game5m_close_notification).

Настройка config.env:
  TELEGRAM_BOT_TOKEN=..., TELEGRAM_SIGNAL_CHAT_IDS, TICKERS_FAST, GAME_5M_COOLDOWN_MINUTES (и др. GAME_5M_*).
  Висяки (JSON + dual): см. GAME_5M_HANGER_* в config.env; в логе маркер «HANGER_TACTIC» — grep по logs/cron_sndk_signal.log.
  Опционально: PLATFORM_GAME_API_ENABLED, PLATFORM_GAME_API_URL — внешний POST /game (kerimsrv), см. kerimsrv/platform doc.md.

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


def send_game5m_close_notification(
    token: str,
    chat_ids: list[str],
    ticker: str,
    close_exit_type: str,
    close_price: float | None,
    close_entry_price: float | None,
    close_narrative_ctx: dict | None,
) -> int:
    """Уведомление в Telegram о закрытии GAME_5M (тейк/стоп/SELL). Возвращает число успешных отправок."""
    if not chat_ids:
        logger.debug("[5m] %s: закрытие — нет TELEGRAM_SIGNAL_CHAT_IDS, уведомление пропущено", ticker)
        return 0
    pnl_str = ""
    if close_entry_price is not None and close_price is not None and close_entry_price > 0:
        pnl_pct = (close_price - close_entry_price) / close_entry_price * 100.0
        pnl_str = ", PnL %+.2f%%" % pnl_pct
    close_msg = "🔒 **5m:** %s закрыта по %s @ %.2f%s. Открытые позиции: /pending" % (
        ticker, close_exit_type, close_price or 0, pnl_str,
    )
    if close_narrative_ctx:
        er = (close_narrative_ctx.get("entry_recap_for_human") or "").strip()
        ex_c = (close_narrative_ctx.get("exit_condition") or "").strip()
        ex_i = (close_narrative_ctx.get("exit_intuition") or "").strip()
        if er:
            close_msg += "\n\n📥 " + er[:380] + ("…" if len(er) > 380 else "")
        if ex_c:
            close_msg += "\n📌 Выход: " + ex_c[:420] + ("…" if len(ex_c) > 420 else "")
        if ex_i:
            close_msg += "\n💡 " + ex_i[:380] + ("…" if len(ex_i) > 380 else "")
    ok = 0
    for cid in chat_ids:
        if send_telegram_message(token, cid, close_msg, parse_mode=None):
            ok += 1
        else:
            logger.error("[5m] не удалось отправить уведомление о закрытии %s в chat_id=%s", ticker, cid)
    if ok > 0:
        logger.info(
            "[5m] %s: отправлено уведомление о закрытии по %s @ %.2f (в этом запуске новый вход не слать)",
            ticker, close_exit_type, close_price or 0,
        )
    return ok


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
    from services.recommend_5m import (
        get_decision_5m,
        get_5m_card_payload,
        build_5m_close_context,
        merge_close_context_with_trade_narrative,
    )
    from services.game_5m import (
        resolve_open_position_for_game5m_close,
        close_position,
        should_close_position,
        record_entry,
        get_latest_buy_context_json,
        _effective_take_profit_pct,
        _effective_stop_loss_pct,
        _take_profit_cap_pct,
        _game_5m_stop_loss_enabled,
        classify_game5m_position_state_v2,
        evaluate_game5m_continuation_gate,
    )

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
    close_narrative_ctx = None  # merge_close_context_with_trade_narrative — для текста в Telegram
    entry_hanger_diag_checked = False
    entry_live_hanger_kind = None
    position_state_v2 = None
    continuation_gate = None

    try:
        # Нетто GAME_5M (VWAP по всем лотам) — согласовано с hanger_tune; иначе any / последний BUY.
        open_pos = resolve_open_position_for_game5m_close(ticker)
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
            engine = None
            apply_hanger_json = None
            hdiag = None  # результат live_aggregate_hanger_diagnosis (если dual)
            try:
                from report_generator import get_engine, get_latest_prices
                engine = get_engine()
                quotes_prices = get_latest_prices(engine, [ticker])
                price_quotes = quotes_prices.get(ticker)
            except Exception:
                price_quotes = None
            price_for_check = max(price, price_quotes) if (price_quotes is not None and price_quotes > 0) else price
            ms = d5.get("market_session") if isinstance(d5.get("market_session"), dict) else {}
            if engine is not None:
                dual = (get_config_value("GAME_5M_HANGER_DUAL_MODE", "false") or "false").strip().lower() in (
                    "1",
                    "true",
                    "yes",
                )
                if dual and (open_pos.get("strategy_name") or "GAME_5M").strip().upper() == "GAME_5M":
                    try:
                        from services.game5m_param_hypothesis_backtest import live_aggregate_hanger_diagnosis

                        live_days = int((get_config_value("GAME_5M_HANGER_LIVE_CALENDAR_DAYS", "6") or "6").strip())
                        sag_eps = float((get_config_value("GAME_5M_HANGER_LIVE_SAG_EPSILON_LOG", "0") or "0").strip())
                        skip_sag = (get_config_value("GAME_5M_HANGER_LIVE_SKIP_SAG", "false") or "false").strip().lower() in (
                            "1",
                            "true",
                            "yes",
                        )
                        bar_hz = int((get_config_value("GAME_5M_BAR_HORIZON_DAYS", "10") or "10").strip())
                        hdiag = live_aggregate_hanger_diagnosis(
                            engine=engine,
                            ticker=ticker,
                            open_position=open_pos,
                            exchange="US",
                            hanger_calendar_days=live_days,
                            sag_epsilon_log=sag_eps,
                            skip_sag_check=skip_sag,
                            bar_horizon_days_after_entry=bar_hz,
                        )
                        entry_hanger_diag_checked = True
                        entry_live_hanger_kind = hdiag.get("kind") if isinstance(hdiag, dict) else None
                        apply_hanger_json = hdiag is not None
                    except Exception as e:
                        logger.warning("%s: GAME_5M_HANGER_DUAL_MODE live hanger: %s", ticker, e)
            should_close, exit_type, exit_detail = should_close_position(
                open_pos,
                decision_exit,
                price_for_check,
                momentum_2h_pct=momentum_2h_pct,
                bar_high=bar_high,
                bar_low=bar_low,
                rsi_5m=rsi_5m,
                pullback_from_high_pct=d5.get("pullback_from_high_pct"),
                session_phase=ms.get("session_phase"),
                apply_hanger_json=apply_hanger_json,
            )
            entry = open_pos.get("entry_price")
            entry_f = float(entry) if entry is not None and entry > 0 else None

            if (open_pos.get("strategy_name") or "GAME_5M").strip().upper() == "GAME_5M" and entry_f and entry_f > 0:
                cap_e = _take_profit_cap_pct(ticker, apply_hanger_json=apply_hanger_json)
                take_e = _effective_take_profit_pct(
                    momentum_2h_pct, ticker=ticker, apply_hanger_json=apply_hanger_json
                )
                position_state_v2 = classify_game5m_position_state_v2(
                    open_pos,
                    current_price=price_for_check,
                    current_decision=decision_exit,
                    momentum_2h_pct=momentum_2h_pct,
                    take_pct=take_e,
                )
                thr = float(take_e) - 0.05
                px_hi = bar_high if (bar_high is not None and float(bar_high) > 0) else price_for_check
                px_take = max(float(price_for_check), float(px_hi))
                pnl_take_line = (px_take - entry_f) / entry_f * 100.0
                hk = hdiag.get("kind") if isinstance(hdiag, dict) else None
                logger.info(
                    "[5m] HANGER_TACTIC %s: apply_hanger_json=%s live_hanger_kind=%s cap_pct=%.4f eff_take_pct=%.4f "
                    "thr_take_pct=%.4f pnl_pct_for_take=%.4f should_close=%s exit_type=%s",
                    ticker,
                    apply_hanger_json,
                    hk,
                    cap_e,
                    take_e,
                    thr,
                    pnl_take_line,
                    bool(should_close),
                    exit_type or "",
                )
                if position_state_v2 and position_state_v2.get("enabled"):
                    logger.info(
                        "[5m] HANGER_V2 %s: state=%s score=%s age_min=%s pnl=%.4f%% mom=%s distance_to_take=%s",
                        ticker,
                        position_state_v2.get("state"),
                        position_state_v2.get("score"),
                        position_state_v2.get("age_minutes"),
                        float(position_state_v2.get("pnl_pct") or 0.0),
                        position_state_v2.get("momentum_2h_pct"),
                        position_state_v2.get("distance_to_take_pct"),
                    )

            if should_close and exit_type:
                base_exit = close_ctx.get("exit_bar_close") if isinstance(close_ctx.get("exit_bar_close"), (int, float)) and close_ctx.get("exit_bar_close") > 0 else price_for_check
                if exit_type in ("TAKE_PROFIT", "TAKE_PROFIT_SUSPEND"):
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
                strat_nm = (open_pos.get("strategy_name") or "GAME_5M").strip() or "GAME_5M"
                entry_ctx_db = get_latest_buy_context_json(ticker, strat_nm)
                take_pct_e = _effective_take_profit_pct(
                    momentum_2h_pct, ticker=ticker, apply_hanger_json=apply_hanger_json
                )
                stop_pct_e = (
                    _effective_stop_loss_pct(momentum_2h_pct, ticker=ticker, apply_hanger_json=apply_hanger_json)
                    if _game_5m_stop_loss_enabled()
                    else 0.0
                )
                take_level = (
                    entry_f * (1.0 + take_pct_e / 100.0)
                    if (entry_f is not None and entry_f > 0 and take_pct_e is not None)
                    else None
                )
                pnl_to_take_pct = (
                    ((bar_high / entry_f) - 1.0) * 100.0
                    if (exit_type in ("TAKE_PROFIT", "TAKE_PROFIT_SUSPEND") and entry_f and entry_f > 0 and bar_high and bar_high > 0)
                    else None
                )
                if exit_type in ("TAKE_PROFIT", "TAKE_PROFIT_SUSPEND"):
                    current_pnl_pct = ((exit_price / entry_f) - 1.0) * 100.0 if entry_f and entry_f > 0 else None
                    continuation_gate = evaluate_game5m_continuation_gate(
                        ticker=ticker,
                        pnl_pct=current_pnl_pct,
                        momentum_2h_pct=momentum_2h_pct,
                        rsi_5m=rsi_5m,
                        volume_vs_avg_pct=d5.get("volume_vs_avg_pct"),
                    )
                    if continuation_gate.get("enabled"):
                        logger.info(
                            "[5m] CONTINUATION_GATE %s: decision=%s would_extend=%s log_only=%s pnl=%s mom=%s rsi=%s",
                            ticker,
                            continuation_gate.get("decision"),
                            continuation_gate.get("would_extend_take"),
                            continuation_gate.get("log_only"),
                            continuation_gate.get("pnl_pct"),
                            continuation_gate.get("momentum_2h_pct"),
                            continuation_gate.get("rsi_5m"),
                        )
                logger.info(
                    "[5m] %s закрытие: тип=%s, exit_bar_close=%s exit_bar_close_ts=%s exit_bar_et=[%s..%s), "
                    "price_5m=%.2f, bar_high=%s bar_high_recent_max=%s bar_high_session_lifted=%s, "
                    "recent_bars_high_ts=%s session_high=%s session_high_ts=%s recent_bars_low_ts=%s, "
                    "bar_low=%s, take_pct=%.4f take_level=%s pnl_vs_take_from_bar_high_pct=%s → exit_price=%.2f",
                    ticker,
                    exit_type,
                    close_ctx.get("exit_bar_close"),
                    close_ctx.get("exit_bar_close_ts"),
                    close_ctx.get("exit_bar_start_et"),
                    close_ctx.get("exit_bar_end_et"),
                    price,
                    bar_high,
                    close_ctx.get("bar_high_recent_max"),
                    close_ctx.get("bar_high_session_lifted"),
                    close_ctx.get("recent_bars_high_ts"),
                    close_ctx.get("session_high"),
                    close_ctx.get("session_high_ts"),
                    close_ctx.get("recent_bars_low_ts"),
                    bar_low,
                    take_pct_e if take_pct_e is not None else -1.0,
                    ("%.4f" % take_level) if take_level is not None else "n/a",
                    ("%.4f" % pnl_to_take_pct) if pnl_to_take_pct is not None else "n/a",
                    exit_price,
                )
                close_ctx_enriched = merge_close_context_with_trade_narrative(
                    close_ctx,
                    d5=d5,
                    exit_type=exit_type,
                    exit_detail=exit_detail or "",
                    open_position=open_pos,
                    entry_ctx=entry_ctx_db,
                    exit_price=exit_price,
                    take_pct=take_pct_e,
                    stop_pct=stop_pct_e,
                )
                if position_state_v2 is not None:
                    close_ctx_enriched["position_state_v2"] = position_state_v2
                if continuation_gate is not None:
                    close_ctx_enriched["continuation_gate"] = continuation_gate
                close_narrative_ctx = close_ctx_enriched
                close_position(
                    ticker, exit_price, exit_type, position=open_pos,
                    bar_high=bar_high if exit_type in ("TAKE_PROFIT", "TAKE_PROFIT_SUSPEND") else None,
                    bar_low=bar_low if exit_type == "STOP_LOSS" else None,
                    context_json=close_ctx_enriched,
                    trade_ts=(
                        close_ctx.get("exit_5m_bar_open_et")
                        or close_ctx.get("decision_5m_bar_open_et")
                        or close_ctx.get("exit_bar_close_ts")
                    ),
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
                    take_pct = _effective_take_profit_pct(
                        momentum_2h_pct, ticker=ticker, apply_hanger_json=apply_hanger_json
                    )
                    if _game_5m_stop_loss_enabled():
                        stop_pct = _effective_stop_loss_pct(
                            momentum_2h_pct, ticker=ticker, apply_hanger_json=apply_hanger_json
                        )
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
        send_game5m_close_notification(
            token, chat_ids, ticker, close_exit_type, close_price, close_entry_price, close_narrative_ctx
        )
        return True  # закрыли в этом запуске — не слать новый вход в том же запуске

    # Докуп вторым BUY запрещён по умолчанию: иначе при открытой прибыльной позиции крон снова пишет BUY в БД.
    allow_pyramid = (get_config_value("GAME_5M_ALLOW_PYRAMID_BUY", "false") or "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    allow_pyramid_if_not_hanger = (
        get_config_value("GAME_5M_ALLOW_PYRAMID_IF_NOT_HANGER", "false") or "false"
    ).strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not closed_this_run and decision_entry in ("BUY", "STRONG_BUY") and not allow_pyramid:
        pos_for_entry = resolve_open_position_for_game5m_close(ticker)
        if pos_for_entry:
            if (
                allow_pyramid_if_not_hanger
                and entry_hanger_diag_checked
                and entry_live_hanger_kind is None
                and (pos_for_entry.get("strategy_name") or "GAME_5M").strip().upper() == "GAME_5M"
            ):
                logger.info(
                    "[5m] %s: докуп разрешён — позиция открыта, но live hanger-диагностика не классифицировала её как висяк "
                    "(GAME_5M_ALLOW_PYRAMID_IF_NOT_HANGER=true)",
                    ticker,
                )
            else:
                reason = (
                    "позиция classified as hanger=%s" % entry_live_hanger_kind
                    if entry_hanger_diag_checked
                    else "live hanger-диагностика не выполнена"
                )
                logger.info(
                    "[5m] %s: пропуск входа — уже есть открытая позиция; докуп выключен "
                    "(GAME_5M_ALLOW_PYRAMID_BUY=false, GAME_5M_ALLOW_PYRAMID_IF_NOT_HANGER=%s; %s)",
                    ticker,
                    allow_pyramid_if_not_hanger,
                    reason,
                )
                return False

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

    logger.info("[5m] %s: отправка сигнала на вход (BUY, cooldown пройден)", ticker)
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
        entry_id = record_entry(
            ticker,
            price,
            decision,
            reasoning,
            entry_context=entry_context,
            trade_ts=(
                (d5.get("entry_5m_bar_open_et") or d5.get("decision_5m_bar_open_et") or d5.get("exit_bar_close_ts"))
                if d5
                else None
            ),
        )
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
    # yfinance пишет «possibly delisted; no price data» на ERROR при пустом ответе Yahoo.
    # Важно: setLevel(WARNING) НЕ отсекает ERROR (ERROR ≥ WARNING). Нужен CRITICAL, чтобы не засорять watchdog.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    token = get_config_value("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.warning(
            "TELEGRAM_BOT_TOKEN не задан в config.env — рассылка в Telegram невозможна, выход (код 1). "
            "Для игры 5m без уведомлений задайте токен в secrets или отключите этот cron."
        )
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
        from services.game_5m import (
            resolve_open_position_for_game5m_close,
            close_position,
            should_close_position,
            get_latest_buy_context_json,
            _effective_take_profit_pct,
            _effective_stop_loss_pct,
            _take_profit_cap_pct,
            _game_5m_stop_loss_enabled,
            classify_game5m_position_state_v2,
            evaluate_game5m_continuation_gate,
        )
        from services.recommend_5m import get_decision_5m, has_5m_data, build_5m_close_context, merge_close_context_with_trade_narrative
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
                    open_pos = resolve_open_position_for_game5m_close(ticker)
                    if not open_pos:
                        continue
                    d5 = get_decision_5m(ticker, use_llm_news=False)
                    if not d5 or d5.get("price") is None:
                        continue
                    price = d5.get("price")
                    engine_ah = None
                    apply_hanger_json_ah = None
                    hdiag_ah = None
                    try:
                        from report_generator import get_engine, get_latest_prices
                        engine_ah = get_engine()
                        quotes_prices = get_latest_prices(engine_ah, [ticker])
                        pq = quotes_prices.get(ticker)
                        price_for_check = max(price, pq) if (pq is not None and pq > 0) else price
                    except Exception:
                        price_for_check = price
                    if engine_ah is not None:
                        dual_ah = (get_config_value("GAME_5M_HANGER_DUAL_MODE", "false") or "false").strip().lower() in (
                            "1",
                            "true",
                            "yes",
                        )
                        if dual_ah and (open_pos.get("strategy_name") or "GAME_5M").strip().upper() == "GAME_5M":
                            try:
                                from services.game5m_param_hypothesis_backtest import live_aggregate_hanger_diagnosis

                                live_days = int((get_config_value("GAME_5M_HANGER_LIVE_CALENDAR_DAYS", "6") or "6").strip())
                                sag_eps = float((get_config_value("GAME_5M_HANGER_LIVE_SAG_EPSILON_LOG", "0") or "0").strip())
                                skip_sag = (
                                    get_config_value("GAME_5M_HANGER_LIVE_SKIP_SAG", "false") or "false"
                                ).strip().lower() in ("1", "true", "yes")
                                bar_hz = int((get_config_value("GAME_5M_BAR_HORIZON_DAYS", "10") or "10").strip())
                                hdiag_ah = live_aggregate_hanger_diagnosis(
                                    engine=engine_ah,
                                    ticker=ticker,
                                    open_position=open_pos,
                                    exchange="US",
                                    hanger_calendar_days=live_days,
                                    sag_epsilon_log=sag_eps,
                                    skip_sag_check=skip_sag,
                                    bar_horizon_days_after_entry=bar_hz,
                                )
                                apply_hanger_json_ah = hdiag_ah is not None
                            except Exception as e:
                                logger.warning("%s: AFTER_HOURS HANGER_DUAL_MODE: %s", ticker, e)
                    close_ctx_ah = build_5m_close_context(d5)
                    bar_high = close_ctx_ah.get("bar_high")
                    bar_low = close_ctx_ah.get("bar_low")
                    ms_ah = d5.get("market_session") if isinstance(d5.get("market_session"), dict) else {}
                    should_close, exit_type, exit_detail = should_close_position(
                        open_pos,
                        d5.get("technical_decision_core") or d5.get("decision", "HOLD"),
                        price_for_check,
                        momentum_2h_pct=close_ctx_ah.get("momentum_2h_pct"),
                        bar_high=bar_high,
                        bar_low=bar_low,
                        rsi_5m=close_ctx_ah.get("rsi_5m"),
                        pullback_from_high_pct=d5.get("pullback_from_high_pct"),
                        session_phase=ms_ah.get("session_phase"),
                        apply_hanger_json=apply_hanger_json_ah,
                    )
                    try:
                        entry_ah0 = open_pos.get("entry_price")
                        entry_f0 = float(entry_ah0) if entry_ah0 is not None and float(entry_ah0) > 0 else None
                    except (TypeError, ValueError):
                        entry_f0 = None
                    if (open_pos.get("strategy_name") or "GAME_5M").strip().upper() == "GAME_5M" and entry_f0:
                        mom0 = close_ctx_ah.get("momentum_2h_pct")
                        cap0 = _take_profit_cap_pct(ticker, apply_hanger_json=apply_hanger_json_ah)
                        take0 = _effective_take_profit_pct(mom0, ticker=ticker, apply_hanger_json=apply_hanger_json_ah)
                        position_state_ah = classify_game5m_position_state_v2(
                            open_pos,
                            current_price=price_for_check,
                            current_decision=d5.get("technical_decision_core") or d5.get("decision", "HOLD"),
                            momentum_2h_pct=mom0,
                            take_pct=take0,
                        )
                        thr0 = float(take0) - 0.05
                        px_hi0 = bar_high if (bar_high is not None and float(bar_high) > 0) else price_for_check
                        px_take0 = max(float(price_for_check), float(px_hi0))
                        pnl_line0 = (px_take0 - entry_f0) / entry_f0 * 100.0
                        hk0 = hdiag_ah.get("kind") if isinstance(hdiag_ah, dict) else None
                        logger.info(
                            "AFTER_HOURS HANGER_TACTIC %s: apply_hanger_json=%s live_hanger_kind=%s cap_pct=%.4f "
                            "eff_take_pct=%.4f thr_take_pct=%.4f pnl_pct_for_take=%.4f should_close=%s exit_type=%s",
                            ticker,
                            apply_hanger_json_ah,
                            hk0,
                            cap0,
                            take0,
                            thr0,
                            pnl_line0,
                            bool(should_close),
                            exit_type or "",
                        )
                    else:
                        position_state_ah = None
                    if should_close and exit_type:
                        base_exit = close_ctx_ah.get("exit_bar_close") if isinstance(close_ctx_ah.get("exit_bar_close"), (int, float)) and close_ctx_ah.get("exit_bar_close") > 0 else price_for_check
                        exit_price = base_exit
                        if exit_type in ("TAKE_PROFIT", "TAKE_PROFIT_SUSPEND") and bar_high is not None and bar_high > 0:
                            exit_price = min(exit_price, bar_high)
                        elif exit_type == "STOP_LOSS" and bar_low is not None and bar_low > 0:
                            exit_price = max(exit_price, bar_low)
                        strat_ah = (open_pos.get("strategy_name") or "GAME_5M").strip() or "GAME_5M"
                        entry_ctx_ah = get_latest_buy_context_json(ticker, strat_ah)
                        mom_ah = close_ctx_ah.get("momentum_2h_pct")
                        take_ah = _effective_take_profit_pct(
                            mom_ah, ticker=ticker, apply_hanger_json=apply_hanger_json_ah
                        )
                        stop_ah = (
                            _effective_stop_loss_pct(mom_ah, ticker=ticker, apply_hanger_json=apply_hanger_json_ah)
                            if _game_5m_stop_loss_enabled()
                            else 0.0
                        )
                        continuation_gate_ah = None
                        if exit_type in ("TAKE_PROFIT", "TAKE_PROFIT_SUSPEND"):
                            entry_price_ah = open_pos.get("entry_price")
                            try:
                                pnl_ah = (float(exit_price) / float(entry_price_ah) - 1.0) * 100.0 if entry_price_ah else None
                            except (TypeError, ValueError):
                                pnl_ah = None
                            continuation_gate_ah = evaluate_game5m_continuation_gate(
                                ticker=ticker,
                                pnl_pct=pnl_ah,
                                momentum_2h_pct=mom_ah,
                                rsi_5m=close_ctx_ah.get("rsi_5m"),
                                volume_vs_avg_pct=d5.get("volume_vs_avg_pct"),
                            )
                        close_ctx_ah_merged = merge_close_context_with_trade_narrative(
                            close_ctx_ah,
                            d5=d5,
                            exit_type=exit_type,
                            exit_detail=exit_detail or "",
                            open_position=open_pos,
                            entry_ctx=entry_ctx_ah,
                            exit_price=exit_price,
                            take_pct=take_ah,
                            stop_pct=stop_ah,
                        )
                        if position_state_ah is not None:
                            close_ctx_ah_merged["position_state_v2"] = position_state_ah
                        if continuation_gate_ah is not None:
                            close_ctx_ah_merged["continuation_gate"] = continuation_gate_ah
                        close_position(
                            ticker, exit_price, exit_type, position=open_pos,
                            bar_high=bar_high if exit_type in ("TAKE_PROFIT", "TAKE_PROFIT_SUSPEND") else None,
                            bar_low=bar_low if exit_type == "STOP_LOSS" else None,
                            context_json=close_ctx_ah_merged,
                            trade_ts=(
                                close_ctx_ah.get("exit_5m_bar_open_et")
                                or close_ctx_ah.get("decision_5m_bar_open_et")
                                or close_ctx_ah.get("exit_bar_close_ts")
                            ),
                        )
                        entry_ah = open_pos.get("entry_price")
                        try:
                            entry_f_ah = float(entry_ah) if entry_ah is not None and float(entry_ah) > 0 else None
                        except (TypeError, ValueError):
                            entry_f_ah = None
                        take_lvl_ah = (
                            entry_f_ah * (1.0 + take_ah / 100.0)
                            if (entry_f_ah is not None and take_ah is not None)
                            else None
                        )
                        pnl_take_ah = (
                            ((bar_high / entry_f_ah) - 1.0) * 100.0
                            if (
                                exit_type in ("TAKE_PROFIT", "TAKE_PROFIT_SUSPEND")
                                and entry_f_ah
                                and bar_high
                                and bar_high > 0
                            )
                            else None
                        )
                        logger.info(
                            "AFTER_HOURS: закрыта позиция %s @ %.2f (%s) exit_bar_close=%s exit_bar_close_ts=%s "
                            "exit_bar_et=[%s..%s) bar_high=%s bar_high_recent_max=%s bar_high_session_lifted=%s "
                            "recent_bars_high_ts=%s session_high=%s session_high_ts=%s take_pct=%.4f take_level=%s "
                            "pnl_vs_take_from_bar_high_pct=%s",
                            ticker,
                            exit_price,
                            exit_type,
                            close_ctx_ah.get("exit_bar_close"),
                            close_ctx_ah.get("exit_bar_close_ts"),
                            close_ctx_ah.get("exit_bar_start_et"),
                            close_ctx_ah.get("exit_bar_end_et"),
                            bar_high,
                            close_ctx_ah.get("bar_high_recent_max"),
                            close_ctx_ah.get("bar_high_session_lifted"),
                            close_ctx_ah.get("recent_bars_high_ts"),
                            close_ctx_ah.get("session_high"),
                            close_ctx_ah.get("session_high_ts"),
                            take_ah if take_ah is not None else -1.0,
                            ("%.4f" % take_lvl_ah) if take_lvl_ah is not None else "n/a",
                            ("%.4f" % pnl_take_ah) if pnl_take_ah is not None else "n/a",
                        )
                        mark_signal_sent(ticker)
                        close_price_for_tg = exit_price
                        close_entry_for_tg = entry_f_ah
                        try:
                            from report_generator import get_engine, get_last_closed_for_ticker

                            last_ah_closed = get_last_closed_for_ticker(get_engine(), ticker, "GAME_5M")
                            if last_ah_closed:
                                close_entry_for_tg = last_ah_closed.entry_price
                                close_price_for_tg = last_ah_closed.exit_price
                        except Exception as ex_ah:
                            logger.debug("AFTER_HOURS get_last_closed_for_ticker %s: %s", ticker, ex_ah)
                        send_game5m_close_notification(
                            token,
                            chat_ids,
                            ticker,
                            exit_type,
                            close_price_for_tg,
                            close_entry_for_tg,
                            close_ctx_ah_merged,
                        )
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
