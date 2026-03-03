#!/usr/bin/env python3
"""
Анализ: были ли в KB существенные геополитические намёки перед понедельником,
и мог ли бэктест/правило «закрыть до выходных при стрессе» избежать потерь по SNDK.

Использование:
  python scripts/analyze_geopolitical_sndk_backtest.py [--days 14] [--ticker SNDK]

Что делает:
  1. Ищет в knowledge_base новости за последние days дней с ключевыми словами
     (Israel, Iran, war, escalation, strike, attack, Middle East, геополит и т.д.).
  2. Выводит список таких новостей (ts, тикер, sentiment, отрывок content) —
     чтобы проверить, были ли «существенные геополитические намеки».
  3. По котировкам SNDK (quotes) находит пары «пятница закрытие / понедельник закрытие»
     за тот же период и считает просадку (%). Это даёт основание: «если бы закрыли
     в пятницу, избежали бы X% просадки в понедельник».

Потерянные из‑за переезда сделки в БД уже нет — скрипт не восстанавливает их,
а даёт аргумент: по новостям стресс был; по динамике цены — закрытие в пятницу
могло бы уменьшить потери.
"""

import argparse
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

# Ключевые слова геополитики (англ + рус)
GEOPOLITICAL_KEYWORDS = [
    "israel", "iran", "war", "escalat", "strike", "attack", "middle east",
    "geopolit", "military", "retaliat", "tension", "conflict", "invasion",
    "израил", "иран", "война", "эскалац", "удар", "ближний восток",
    "геополит", "военн", "напряж", "конфликт",
]


def fetch_geopolitical_news(engine, days: int):
    """Новости из KB за последние days дней, в content которых есть геополитические ключевые слова."""
    from sqlalchemy import text
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    conditions = " OR ".join(
        f"content ILIKE :k{i}" for i in range(len(GEOPOLITICAL_KEYWORDS))
    )
    params = {"cutoff": cutoff}
    for i, kw in enumerate(GEOPOLITICAL_KEYWORDS):
        params[f"k{i}"] = f"%{kw}%"
    with engine.connect() as conn:
        r = conn.execute(
            text(f"""
                SELECT id, ts, ticker, source, content, sentiment_score, insight
                FROM knowledge_base
                WHERE ts >= :cutoff
                  AND content IS NOT NULL
                  AND LENGTH(TRIM(content)) > 0
                  AND ({conditions})
                ORDER BY ts DESC
            """),
            params,
        )
        return r.fetchall()


def fetch_sndk_friday_monday_pairs(engine, ticker: str, days: int):
    """
    По quotes за последние days дней находит торговые дни и строит пары
    (последняя пятница, следующий понедельник) и считает % изменения close.
    """
    from sqlalchemy import text
    from datetime import datetime, timedelta
    import pandas as pd
    cutoff = datetime.utcnow() - timedelta(days=days)
    with engine.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT date, close
                FROM quotes
                WHERE ticker = :ticker AND date >= :cutoff
                ORDER BY date ASC
            """),
            conn,
            params={"ticker": ticker, "cutoff": cutoff},
        )
    if df.empty or len(df) < 2:
        return []
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df["weekday"] = df["date"].dt.weekday  # 4 = Friday, 0 = Monday
    fridays = df[df["weekday"] == 4].drop_duplicates(subset=["date"]).sort_values("date")
    mondays = df[df["weekday"] == 0].drop_duplicates(subset=["date"]).sort_values("date")
    pairs = []
    seen = set()
    for _, fr in fridays.iterrows():
        d_fr = fr["date"].date()
        next_mondays = mondays[mondays["date"].dt.date > d_fr]
        if next_mondays.empty:
            continue
        mo = next_mondays.iloc[0]
        d_mo = mo["date"].date()
        if (d_fr, d_mo) in seen:
            continue
        seen.add((d_fr, d_mo))
        close_fr = float(fr["close"])
        close_mo = float(mo["close"])
        if close_fr <= 0:
            continue
        pct = (close_mo / close_fr - 1.0) * 100.0
        pairs.append((d_fr, d_mo, close_fr, close_mo, pct))
    return pairs


def main():
    parser = argparse.ArgumentParser(description="Геополитические новости в KB и просадка SNDK пятница→понедельник")
    parser.add_argument("--days", type=int, default=14, help="Окно в днях для новостей и котировок")
    parser.add_argument("--ticker", type=str, default="SNDK", help="Тикер для анализа пятница/понедельник")
    args = parser.parse_args()

    from config_loader import get_database_url
    from sqlalchemy import create_engine

    engine = create_engine(get_database_url())

    # 1) Геополитические новости
    print("=" * 60)
    print("1. Новости в KB с геополитическими ключевыми словами")
    print("   (Israel, Iran, war, escalation, strike, Middle East, геополит и др.)")
    print("=" * 60)
    rows = fetch_geopolitical_news(engine, args.days)
    if not rows:
        print("   За указанный период записей не найдено.")
        print("   Проверьте, что в KB есть новости с такими темами (например скрипт add_manual_news_iran_israel_us.py).")
    else:
        print(f"   Найдено записей: {len(rows)}\n")
        for row in rows:
            id_, ts, ticker, source, content, sentiment, insight = row
            ts_str = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)
            sent_str = f"{float(sentiment):.2f}" if sentiment is not None else "—"
            snippet = (content or "")[:280] + ("..." if len(content or "") > 280 else "")
            print(f"   [{ts_str}] {ticker}  sentiment={sent_str}  source={source or '—'}")
            print(f"   {snippet}")
            if insight:
                print(f"   insight: {insight[:200]}")
            print()
    print()

    # 2) Пары пятница → понедельник по SNDK
    print("=" * 60)
    print(f"2. Динамика {args.ticker}: закрытие в пятницу → закрытие в понедельник")
    print("   (основание для «если бы закрыли в пятницу, избежали бы просадки X%»)")
    print("=" * 60)
    pairs = fetch_sndk_friday_monday_pairs(engine, args.ticker, args.days)
    if not pairs:
        print(f"   Недостаточно данных по {args.ticker} за последние {args.days} дней или нет пар пт/пн.")
    else:
        for d_fr, d_mo, close_fr, close_mo, pct in pairs:
            sign = "▼" if pct < 0 else "▲"
            print(f"   Пт {d_fr} close={close_fr:.2f}  →  Пн {d_mo} close={close_mo:.2f}   {sign} {pct:+.2f}%")
        worst = min(pairs, key=lambda x: x[4])
        print()
        print(f"   Худшая неделя (по close→close): Пт {worst[0]} → Пн {worst[1]}, изменение {worst[4]:+.2f}%.")
        print("   Вывод: при правиле «закрыть позиции в пятницу при геополитическом стрессе в новостях»")
        print("   можно было бы избежать просадки (если стресс в KB был до пятницы).")
        print("   Важно: если закрывали в начале торгов в понедельник, просадка могла быть больше,")
        print("   чем пт close → пн close (открытие часто ниже; в quotes только дневные close).")
    print()
    print("=" * 60)
    print("Бэктест в полном виде (прогон стратегии с правилом «нет позиции через выходные при стрессе»)")
    print("требует доработки backtest_engine (учёт новостей и условное закрытие в пт).")
    print("Данный скрипт лишь проверяет: 1) есть ли в KB намёки; 2) была ли просадка пт→пн.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
