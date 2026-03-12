#!/usr/bin/env python3
"""
Ежедневный пересчёт рекомендуемых параметров 5m: потолок тейка (PCT) и макс. дней (DAYS) по тикерам.

Запускает оба анализа (suggest_take_profit_caps + suggest_max_position_days), сохраняет результат
в local/suggested_5m_params.json. При включённом USE_SUGGESTED_5M_PARAMS игра 5m подхватывает
эти значения (если файл обновлён не более 25 ч назад), чтобы параметры не выбивались из оптимальных.

Рекомендуемый cron: раз в день после закрытия US (например 00:30 MSK или 17:30 ET):
  30 0 * * 1-5  cd /path/to/lse && python scripts/daily_5m_params.py
  или с отправкой в Telegram: ... python scripts/daily_5m_params.py --telegram

Аргументы:
  --no-write   не писать JSON (только вывод в stdout)
  --telegram   отправить сводку в TELEGRAM_SIGNAL_CHAT_IDS
  --sessions-take  N   число сессий для расчёта потолка тейка (по умолч. 7)
  --sessions-days  N   число сессий для расчёта макс. дней (по умолч. 25)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def main() -> None:
    from config_loader import get_config_value
    from services.ticker_groups import get_tickers_game_5m
    from services.suggest_5m_params import compute_take_profit_suggestions, compute_max_days_suggestions

    parser = argparse.ArgumentParser(description="Ежедневный пересчёт PCT/DAYS для игры 5m")
    parser.add_argument("--no-write", action="store_true", help="Не писать local/suggested_5m_params.json")
    parser.add_argument("--telegram", action="store_true", help="Отправить сводку в Telegram")
    parser.add_argument("--sessions-take", type=int, default=7, metavar="N", help="Сессий для потолка тейка")
    parser.add_argument("--sessions-days", type=int, default=25, metavar="N", help="Сессий для макс. дней")
    args = parser.parse_args()

    tickers = get_tickers_game_5m()
    if not tickers:
        tickers = ["SNDK", "NBIS", "ASML", "MU", "LITE", "CIEN"]

    # 1) Потолки тейка
    take_result = compute_take_profit_suggestions(
        tickers, n_sessions=args.sessions_take, fetch_days=max(10, args.sessions_take + 3)
    )
    take_pct_by_ticker = {}
    for t, v in take_result.items():
        if "suggested_pct" in v:
            take_pct_by_ticker[t] = float(v["suggested_pct"])

    # 2) Макс. дней (используем только что посчитанные потолки тейка)
    days_result = compute_max_days_suggestions(
        tickers,
        n_sessions=args.sessions_days,
        take_pct_by_ticker=take_pct_by_ticker if take_pct_by_ticker else None,
        fetch_days=max(35, args.sessions_days + 10),
    )

    # Собираем итог для записи и вывода
    suggested_pct = {t: v["suggested_pct"] for t, v in take_result.items() if "suggested_pct" in v}
    suggested_days = {}
    for t, v in days_result.items():
        if v.get("suggested_days") is not None:
            suggested_days[t] = v["suggested_days"]

    payload = {
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "take_pct": suggested_pct,
        "max_days": suggested_days,
    }

    # Вывод в stdout
    print("=== Ежедневный пересчёт 5m PCT / DAYS ===")
    print("Ticker   | Take% (предлаг.) | Сейчас PCT | Days (предлаг.) | Сейчас DAYS")
    print("-" * 65)
    for t in tickers:
        tp = take_result.get(t, {})
        dy = days_result.get(t, {})
        take_sug = tp.get("suggested_pct", "—")
        take_cur = tp.get("current_config", "—")
        days_sug = dy.get("suggested_days", "—")
        days_cur = dy.get("current_config", "—")
        print(f"{t:<8} | {take_sug!s:<16} | {take_cur!s:<11} | {days_sug!s:<15} | {days_cur!s}")

    if not args.no_write:
        out_path = project_root / "local" / "suggested_5m_params.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"\nЗаписано: {out_path}")

    if args.telegram:
        token = get_config_value("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            print("TELEGRAM_BOT_TOKEN не задан — пропуск отправки.")
        else:
            from services.telegram_signal import get_signal_chat_ids, send_telegram_message
            lines = ["📊 Ежедневный пересчёт 5m: PCT / DAYS"]
            for t in tickers:
                tp = take_result.get(t, {})
                dy = days_result.get(t, {})
                if "suggested_pct" in tp and dy.get("suggested_days") is not None:
                    lines.append(
                        f"{t}: тейк {tp['suggested_pct']}% (было {tp.get('current_config', '—')}), "
                        f"дней {dy['suggested_days']} (было {dy.get('current_config', '—')})"
                    )
            if suggested_pct or suggested_days:
                lines.append("Файл: local/suggested_5m_params.json (при USE_SUGGESTED_5M_PARAMS=true подхватится игрой 5m).")
            msg = "\n".join(lines) if lines else "Нет данных по тикерам."
            for cid in get_signal_chat_ids():
                if send_telegram_message(token, cid, msg, parse_mode=None):
                    print("Отправлено в Telegram.")
                    break


if __name__ == "__main__":
    main()
