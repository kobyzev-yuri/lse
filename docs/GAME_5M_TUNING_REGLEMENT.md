# GAME_5M Tuning Reglement

Цель регламента — менять параметры GAME_5M как **измеримые** live-эксперименты, а не как ручной перебор. Один эксперимент отвечает на один вопрос: улучшает ли **конкретное** изменение результат без роста нежелательного риска.

## 1. Три контура (не путать)

| Контур | Что делает | Когда смотреть |
|--------|------------|----------------|
| **`/api/analyzer`** (+ опц. LLM) | Закрытые сделки, метрики окна 5m, hanger v2, continuation gate, CatBoost backtest на сохранённом `context_json` | После сессии / раз в несколько дней; перед сменой порогов «по ощущениям». |
| **`run_daily_game5m_ml_pipeline.py`** | Датасеты stuck/continuation + обучение **entry** CatBoost + строка в `game5m_daily_ml_report.jsonl` | По cron после закрытия US; **не** строит replay proposals. |
| **Replay proposals** (`services/game5m_replay_proposals.py` + `game5m_tuning_controller.py propose`) | Офлайн: сетка кандидатов по **выходным/тейковым** `GAME_5M_*`, для каждого — временный `os.environ` + `replay_game5m_on_bars` по последним сделкам; ранжирование по сумме/штрафам **delta log-return** | **Вручную** или отдельный cron (например 1× в неделю вне RTH); когда накопилось достаточно закрытых GAME_5M и нужен **числовой** приоритет правок до live-теста. |

Сегодня replay proposals **не запускались автоматически**: в репозитории нет включения `propose` в дневной ML-скрипт. Это осознанно: прогон тяжёлый (БД + реплей по многим сделкам и кандидатам), результат — гипотеза, а не деплой.

## 2. Replay proposals: назначение и ограничения

Реализация: `build_game5m_replay_proposals()` в `services/game5m_replay_proposals.py`.

- Берёт последние закрытые сделки `GAME_5M` (по умолчанию до **120** за **30** дней), **исключая** «ложные тейки» от старого `session_high` (`is_false_take_profit_by_session_high`), если не передан флаг `--include-false-takes`.
- Строит ограниченную сетку значений для ключей: **`GAME_5M_TAKE_PROFIT_PCT`**, **`GAME_5M_TAKE_MOMENTUM_FACTOR`**, **`GAME_5M_TAKE_PROFIT_MIN_PCT`**, **`GAME_5M_MAX_POSITION_DAYS`**, и пер-тикерные **`GAME_5M_TAKE_PROFIT_PCT_<TICKER>`** для тикеров в выборке.
- Для каждой пары (ключ, кандидат) реплеит выход по 5m-барам из БД (`load_bars_5m_for_replay`), сравнивает **log-return** с фактом.
- **Не меняет** `config.env`: только расчёт в памяти с временными env overrides (`env_overrides`).
- **Не моделирует** новые входы, новости, смену ветки STRONG_BUY — только переигрыш **выходной** логики при том же входе/цене.

Интерпретация: высокий `score` и положительный `total_delta_log_ret` означают «на этом срезе сделок альтернативный порог мог бы улучшить суммарный log-return», с оговорками по **режиму рынка** и **переобучению на хвост**.

## 3. Когда запускать `propose`

Минимум:

- есть доступ к **той же БД**, что и прод (закрытые сделки + 5m бары для реплея);
- после серии закрытий (например конец недели), когда анализатор уже показал проблемную зону (тейк, max days, factor).

Команда (read-only, пишет только ledger):

```bash
# из корня репо, venv с зависимостями проекта
python scripts/game5m_tuning_controller.py propose --days 30 --max-trades 120 --top-n 12 --horizon-tail-days 1
```

Опции по смыслу:

- `--include-false-takes` — только если осознанно хотите включить спорные тейки в расчёт.
- `--ledger /path/to/game5m_tuning_ledger.json` — если не дефолт `local/game5m_tuning_ledger.json`.

Docker (пример):

```bash
docker compose exec -T lse python3 scripts/game5m_tuning_controller.py propose --days 30 --max-trades 120 --top-n 12
```

После `propose` в ledger появляется `latest_proposals` с полями `proposals[]` (`proposal_id`, `env_key`, `proposed`, `current`, `score`, `metrics`, `evidence`).

Просмотр:

```bash
python scripts/game5m_tuning_controller.py status --top-n 8
```

## 4. Отбор одного кандидата и apply

Правила:

- не применять **несколько** сильных изменений сразу;
- приоритет — выход и удержание (тейк, factor, min take, max days), согласованно с планом hanger/stale (`docs/GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md`);
- replay **score — не приказ**, а гипотеза: перед продом сверить с анализатором и здравым смыслом.

Изменение в live — **только** через controller (валидация ключа и шага — `services/game5m_tuning_policy.py`):

```bash
python scripts/game5m_tuning_controller.py apply --proposal-id <proposal_id> --observe-days 2
```

Смягчённый шаг вручную (то же правило валидации):

```bash
python scripts/game5m_tuning_controller.py apply --key GAME_5M_TAKE_PROFIT_MIN_PCT --value 2.5 --observe-days 2
```

`--dry-run` — проверить без записи в `config.env`. При активном эксперименте `pending_effect` повторный `apply` без `--force` будет отклонён.

## 5. Окно наблюдения

- минимум **1** полный торговый день;
- лучше **2–3** дня;
- либо до накопления **8–20** новых закрытых GAME_5M (как в `observe --min-new-trades`).

Пока эксперимент активен, не менять другие параметры, сильно влияющие на вход/выход.

Фиксация наблюдения:

```bash
python scripts/game5m_tuning_controller.py observe --days 2 --min-new-trades 8
python scripts/game5m_tuning_controller.py status
```

Смотреть: число новых закрытий, total/avg log-return, win rate, распределение по `exit_signal`, затронутые тикеры; сравнить с `baseline_summary` в ledger.

## 6. Решение: оставить / продлить / откатить

Оставить изменение, если:

- достаточно новых сделок;
- avg/total log-return **лучше** baseline;
- нет всплеска плохих выходов (stale, преждевременный тейк там, где раньше был плюс);
- нет деградации по ключевым тикерам.

Продлить наблюдение, если сделок мало или эффект нейтральный.

Откатить (вернуть старое значение в `config.env` вручную или через политику отката веба), если live расходится с replay, растут зависания или ухудшился PnL.

## 7. Связь с вебом

На странице анализатора блок **GAME_5M tuning** использует тот же дух «один параметр, observe»: API ` /api/analyzer/tuning/*` и файл `local/game5m_tuning_ledger.json`. Controller по умолчанию пишет в **тот же** `game5m_tuning_ledger.json` — не плодите второй ledger без необходимости.

## 8. Пример эксперимента (2026-04-29)

Replay top direction: снизить минимальный take-profit.

Live-тест на 2026-04-29 → 2026-04-30:

```env
GAME_5M_TAKE_PROFIT_MIN_PCT=2.5
```

Мягкий шаг между `3.0` и replay-кандидатом `2.0`: проверить, уменьшит ли более ранняя фиксация просадки и пропуски выхода, не убивая сильные движения.

## 9. Рекомендуемый график (операционно)

| Частота | Действие |
|---------|----------|
| **Ежедневно** (cron) | `run_daily_game5m_ml_pipeline` — датасеты + entry CatBoost + JSONL. |
| **2–5× в неделю** | `/analyzer` или снимок JSON — качество сделок и блоки hanger/continuation. |
| **0–2× в неделю** | `game5m_tuning_controller.py propose` — когда нужен ранжированный список **выходных** порогов по реплею; затем один `apply` + observe. |

Если неделя прошла без `propose` — это нормально: инструмент вспомогательный, а не обязательный ежедневный шаг.
