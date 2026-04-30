# GAME_5M Tuning Reglement

Цель регламента - менять параметры GAME_5M как измеримые live-эксперименты, а не как ручной перебор. Один эксперимент должен отвечать на один вопрос: улучшает ли конкретное изменение результат стратегии без роста риска.

## 1. Proposal

Proposal строится read-only replay-прогоном:

```bash
python scripts/game5m_tuning_controller.py propose --days 30 --max-trades 120 --top-n 12 --horizon-tail-days 1
```

Обязательные правила:

- false take-profit сделки, загрязненные старым `session_high` багом, исключаются из replay и train-набора;
- в live нельзя применять несколько сильных proposals одновременно;
- приоритет имеют параметры выхода и удержания: take-profit, stop/stale/max-hold;
- replay score - это не приказ применять, а гипотеза для live-проверки.

## 2. Apply

Изменение применяется только через controller:

```bash
python scripts/game5m_tuning_controller.py apply --proposal-id <proposal_id> --observe-days 2
```

Если берем направление из proposal, но смягчаем шаг, применяем явно:

```bash
python scripts/game5m_tuning_controller.py apply --key GAME_5M_TAKE_PROFIT_MIN_PCT --value 2.5 --observe-days 2
```

Controller должен:

- проверить, что ключ разрешен к изменению;
- проверить размер шага;
- записать старое и новое значение;
- сохранить baseline по закрытым GAME_5M сделкам;
- поставить эксперимент в статус `pending_effect`;
- заблокировать следующий эксперимент до review или forced override.

## 3. Observation Window

Минимальное окно наблюдения:

- минимум 1 полный торговый день;
- лучше 2-3 торговых дня;
- либо до накопления 8-20 новых закрытых GAME_5M сделок.

Пока эксперимент активен, не меняем другие GAME_5M параметры, влияющие на вход/выход.

## 4. Observe

Промежуточная и финальная оценка:

```bash
python scripts/game5m_tuning_controller.py observe --days 2 --min-new-trades 8
python scripts/game5m_tuning_controller.py status
```

Смотрим:

- количество новых закрытых сделок после apply;
- total/avg log-return;
- win rate;
- средний net PnL после комиссий;
- число ранних take-profit;
- stop/stale/no-exit;
- отдельно affected tickers и entry branches.

## 5. Decision

Оставляем изменение, если:

- есть достаточное число новых сделок;
- avg/total log-return лучше baseline;
- нет роста stop/stale выходов;
- нет очевидного ухудшения по ключевым тикерам.

Продлеваем наблюдение, если сделок мало или результат нейтральный.

Откатываем старое значение из ledger, если:

- результат хуже baseline;
- параметр начал слишком рано фиксировать прибыль;
- выросло число плохих выходов или зависших позиций;
- поведение live расходится с replay-гипотезой.

## 6. Эксперимент 2026-04-29

Replay top direction: снизить минимальный take-profit.

Live-тест на 2026-04-29 -> 2026-04-30:

```env
GAME_5M_TAKE_PROFIT_MIN_PCT=2.5
```

Это мягкий шаг между текущим `3.0` и replay proposal `2.0`. Цель - проверить, уменьшит ли более ранняя фиксация прибыли просадки и пропущенные выходы, не убивая сильные движения.
