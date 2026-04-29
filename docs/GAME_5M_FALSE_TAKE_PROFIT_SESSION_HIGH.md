# GAME_5M: ложные TAKE_PROFIT из-за session_high

Дата разбора: 2026-04-29.

## Что произошло

В `build_5m_close_context()` для проверки выхода по тейку поле `bar_high`
могло подменяться на `session_high`, если максимум сессии был выше последних
5m high. Это давало ложное срабатывание `TAKE_PROFIT`: решение принималось по
старому/далекому максимуму сессии, а в `trade_history` записывался текущий
`exit_bar_close`.

Типичный симптом:

```text
signal_type=TAKE_PROFIT
actual PnL ниже цели или даже отрицательный
exit_context_json.bar_high_session_lifted=true
exit_context_json.bar_high=session_high
exit_context_json.bar_high_recent_max сильно ниже целевого уровня
```

Историю сделок не переписываем. Для обучения такие строки исключаются.

## Затронутые закрытия

В истории `GAME_5M` найдено 5 ошибочных закрытий, где recent 5m high не достигал
целевого тейка, но `TAKE_PROFIT` сработал из-за `session_high`.

| trade_id | ticker | вход ET | ошибочный выход ET | вход | выход | факт PnL | цель | recent high на выходе | session_high | что было бы по 5m replay |
|---:|---|---|---|---:|---:|---:|---:|---:|---:|---|
| 455 | MU | 2026-04-14 15:29 | 2026-04-20 15:19 | 459.8111 | 447.7900 | -2.61% | 0.50% | 449.3500 | 464.5600 | Цена достигла корректного порога 2026-04-22 09:30 ET: high 469.0000, close 466.0000, close PnL около +1.35%. |
| 490 | LITE | 2026-04-28 14:16 | 2026-04-28 14:17 | 792.8350 | 792.2800 | -0.07% | 4.50% | 793.7999 | 842.0000 | Вчера тейк не достигался; первый корректный тейк 2026-04-29 09:35 ET: high 837.8500, close 831.0000, close PnL около +4.81%. |
| 491 | MU | 2026-04-28 13:33 | 2026-04-28 14:48 | 502.4500 | 508.8000 | +1.26% | 3.31% | 509.6400 | 518.8333 | Первый корректный тейк 2026-04-29 09:30 ET: high 530.9899, close 525.2100, close PnL около +4.53%. |
| 495 | NBIS | 2026-04-28 13:33 | 2026-04-29 09:30 | 134.9708 | 140.2090 | +3.88% | 5.50% | 140.2090 | 165.2500 | По доступным 5m-барам корректный порог пока не достигался; максимум после входа high 138.2200 в replay. |
| 496 | CIEN | 2026-04-28 13:42 | 2026-04-29 09:30 | 471.7275 | 478.0950 | +1.35% | 5.50% | 478.0950 | 527.5000 | По доступным 5m-барам корректный порог пока не достигался; максимум после входа high 482.5400 в replay. |

Примечание: replay выше проверяет именно достижение ценового тейка по 5m high.
Он не переписывает историю и не моделирует все возможные альтернативные ветки
управления позицией после того, как ошибочный выход не произошел бы.

## Что исправлено

Кодовый фикс:

- `services/recommend_5m.py`: `build_5m_close_context()` больше не подмешивает
  `session_high` в `bar_high` для проверки тейка. Для выхода используются только
  `recent_bars_high_max` / `last_bar_high`; `session_high` остается в JSON только
  для диагностики.

Фильтр обучения:

- `scripts/train_game5m_catboost.py`: при сборке train dataset исключаются строки
  `false_take_profit_by_session_high`, где:
  - `exit_signal` равен `TAKE_PROFIT` или `TAKE_PROFIT_SUSPEND`;
  - `bar_high_session_lifted=true`;
  - recent 5m high не достигал целевого тейка с учетом допуска 0.05 п.п.
- Количество исключенных строк сохраняется в meta модели:
  `excluded_false_take_profit_by_session_high`.

Runtime:

- Hotfix скопирован в живой контейнер `lse-bot`.
- CatBoost установлен в контейнер.
- `Dockerfile` обновлен, чтобы CatBoost ставился при следующей сборке образа.

## Проверки после исправления

Проверено на `gcp-lse` без рестарта контейнера:

```text
NBIS replay: bar_high=140.209, session_high=165.25, lifted=false, should_close=false
CIEN replay: bar_high=478.095, session_high=527.5, lifted=false, should_close=false
```

Текущие открытые позиции после фикса:

```text
LITE: take=4.50%, pnl_for_take=2.13%, should_close=false
MU:   take=4.50%, pnl_for_take=1.56%, should_close=false
```

CatBoost dry-run после очистки:

```text
исключено false_take_profit_by_session_high=5
train rows=132
wins/losses=97/35
AUC(valid)≈0.692
```
