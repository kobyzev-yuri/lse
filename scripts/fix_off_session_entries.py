#!/usr/bin/env python3
"""
Исправление «невозможных» сделок GAME_5M: вход/выход вне сессии NYSE (премаркет, после закрытия, выходные).
Такие записи нарушают правила биржи. Варианты:
  --move-to-open   перенести ts в начало ближайшей сессии (9:30 ET) [по умолчанию]
  --move-to-close  перенести ts в конец предыдущей сессии (16:00 ET)
  --delete         удалить записи (пару BUY+SELL удаляем вместе, иначе остаётся «висячий» выход)
Запуск: python scripts/fix_off_session_entries.py [--dry-run] [--move-to-open|--move-to-close|--delete]
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from datetime import timezone
from sqlalchemy import create_engine, text

from config_loader import get_database_url
from services.market_session import session_phase_for_dt, clamp_ts_to_session
from services.game_5m import GAME_5M_STRATEGY


def _naive_utc(dt):
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None):
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def main():
    dry_run = "--dry-run" in sys.argv
    if "--delete" in sys.argv:
        action = "delete"
    elif "--move-to-close" in sys.argv:
        action = "move_to_close"
    else:
        action = "move_to_open"

    engine = create_engine(get_database_url())
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, ts, side, ticker
                FROM public.trade_history
                WHERE strategy_name = :strategy
                ORDER BY ticker, ts, id
            """),
            {"strategy": GAME_5M_STRATEGY},
        ).fetchall()

    off_session = []
    for r in rows:
        row_id, ts, side, ticker = r
        ts_utc = _naive_utc(ts)
        phase = session_phase_for_dt(ts_utc)
        if phase in ("PRE_MARKET", "AFTER_HOURS", "WEEKEND", "HOLIDAY"):
            off_session.append({"id": row_id, "ts": ts_utc, "side": side, "ticker": ticker, "phase": phase})

    if not off_session:
        print("Нет сделок GAME_5M вне сессии. Всё в порядке.")
        return

    print(f"Найдено записей вне сессии NYSE: {len(off_session)}")
    for x in off_session:
        print(f"  id={x['id']} {x['side']} {x['ticker']} ts={x['ts']} phase={x['phase']}")

    if dry_run:
        print("\n[--dry-run] Изменения не применены.")
        return

    if action == "delete":
        # Удаляем пары BUY+SELL: для каждого off-session BUY находим следующий SELL по тому же тикеру и удаляем оба
        ids_to_delete = set()
        for x in off_session:
            ids_to_delete.add(x["id"])
        # Для каждого off-session SELL — тоже в список
        # Парность: если удаляем BUY, нужно удалить и его SELL (следующий SELL по тикеру после этого BUY)
        buy_ids_off = {x["id"] for x in off_session if x["side"] == "BUY"}
        with engine.connect() as conn:
            for x in off_session:
                if x["side"] != "BUY":
                    continue
                # Найти SELL по этому тикеру с ts > buy_ts
                sell_row = conn.execute(
                    text("""
                        SELECT id FROM public.trade_history
                        WHERE strategy_name = :strategy AND ticker = :ticker AND side = 'SELL'
                          AND ts > :after_ts
                        ORDER BY ts, id LIMIT 1
                    """),
                    {"strategy": GAME_5M_STRATEGY, "ticker": x["ticker"], "after_ts": x["ts"]},
                ).fetchone()
                if sell_row:
                    ids_to_delete.add(sell_row[0])
            # Удалить и off-session SELL, которые не являются парой к off-session BUY (одиночные SELL вне сессии)
            for x in off_session:
                if x["side"] == "SELL":
                    ids_to_delete.add(x["id"])
        with engine.begin() as conn:
            for rid in sorted(ids_to_delete):
                conn.execute(text("DELETE FROM public.trade_history WHERE id = :id"), {"id": rid})
        print(f"Удалено записей: {len(ids_to_delete)}")
        return

    to_start = action == "move_to_open"
    with engine.begin() as conn:
        for x in off_session:
            new_ts = clamp_ts_to_session(x["ts"], to_start=to_start)
            conn.execute(
                text("UPDATE public.trade_history SET ts = :ts WHERE id = :id"),
                {"ts": new_ts, "id": x["id"]},
            )
    print(f"Обновлено записей: {len(off_session)} (ts перенесён на {'начало' if to_start else 'конец'} сессии).")


if __name__ == "__main__":
    main()
