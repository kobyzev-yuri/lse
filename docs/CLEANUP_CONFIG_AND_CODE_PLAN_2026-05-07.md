# План: аудит конфига и зачистка кода (старт 2026-05-06, завершение 2026-05-07)

> **Живой срез «что открыто по инфраструктуре»:** [PROJECT_STATUS_AND_ROADMAP.md](PROJECT_STATUS_AND_ROADMAP.md) — дублировать сюда длинные статусы не требуется. Этот файл сохраняется как **история аудита** и чеклист P0–P3.

## Контекст и ограничения

- **2026-05-06 (сегодня):** идёт торговля на сервере — **не деплоить** удаление кода/веток и не трогать `config.env` радикально без проверки.
- **2026-05-07 (завтра):** спокойное окно — закоммитить накопленное, задеплоить, прогнать смоук‑тесты в `lse-bot`, при необходимости точечно править прод‑конфиг.

## Цель

1. Убрать **заброшенные** env‑ключи и ветки кода (или задокументировать их в `config.env.example`, если ветка живая).
2. Не плодить новые параметры: приоритет — **свести поверхность конфига** к одному источнику правды (`config.env.example` + whitelist редактирования).
3. Инструмент: `scripts/audit_config_unused_keys.py` в режимах:
   - `config_to_code` — ключи в конфиге без упоминаний в репо (кандидаты на удаление из конфига).
   - `code_to_example` — ключи, которые **читает код**, но **нет в `config.env.example`** (кандидаты: документировать или удалить код).

## Сегодня (начато, без прод‑риска)

- [x] Расширен аудит‑скрипт: два режима выше.
- [x] Зафиксировано: `GAME_5M_MAX_POSITION_DAYS*` **используются** (`services/game_5m.py`) — **не удалять** как «мёртвые».
- [x] Сохранить в репозиторий полный вывод аудита (артефакты в `docs/`):
  - `docs/audit_code_to_example_2026-05-06.txt`
  - `docs/audit_config_to_code_example_2026-05-06.txt`
  ```bash
  python3 scripts/audit_config_unused_keys.py --mode code_to_example --root . > docs/audit_code_to_example_2026-05-06.txt
  python3 scripts/audit_config_unused_keys.py --mode config_to_code --config config.env.example --root . > docs/audit_config_to_code_example_2026-05-06.txt
  ```
- [x] Закоммитить **только безопасные** изменения: анализатор (`time_exit_early_review`, правка кеша OHLC), архив документов, `audit_config_unused_keys.py`, правки без изменения торговой логики — **после** локального `py_compile` (`python3 -m compileall -q services scripts`).

## Завтра на сервере (спокойное окно)

### 1. Синхронизация

- [ ] `git pull` / стандартный деплой (`./scripts/deploy_from_github.sh` на VM).
- [ ] Убедиться, что в контейнере есть актуальный `scripts/audit_config_unused_keys.py`.

### 2. Аудит против **боевого** `config.env`

На VM (внутри контейнера или с копией `/app/config.env`):

```bash
docker exec -i lse-bot python3 /app/scripts/audit_config_unused_keys.py \
  --mode config_to_code --config /app/config.env --root /app > /tmp/prod_config_no_hits.txt
```

- Строки в `prod_config_no_hits.txt` — **кандидаты на удаление из прод‑конфига** (перед удалением: проверить docker‑compose, cron, внешние сервисы).

### 3. Смоук после деплоя

- [ ] Один прогон анализатора: `analyze_trade_effectiveness(days=14, strategy="GAME_5M", use_llm=False)` — проверить `time_exit_early_review`, отсутствие ошибок.
- [ ] При необходимости — короткий прогон крона в безопасном режиме (без сделок), если добавлялись изменения в `send_sndk_signal_cron` / `game_5m` (на этот релиз — **не планировалось**).

### 4. Волна зачистки кода (после аудита, маленькими PR)

Порядок (от меньшего риска к большему):

| Приоритет | Что | Риск | Действие |
|-----------|-----|------|----------|
| P0 | Ключи только в тестах/фикстурах, отладочные | Низкий | Удалить или переименовать, не трогать прод |
| P1 | `code_to_example`: один файл, флаг `false` по умолчанию, не в hot path | Низкий | Удалить ветку или добавить строку в `config.env.example` |
| P2 | `GAME_5M_HANGER_LIVE_*`, `BAR_HORIZON_DAYS`, и т.д. | Средний | Решение по факту использования в **текущем** `config.env` на сервере |
| P3 | `services/game_5m.py` / `recommend_5m.py` | Высокий | Только с реплеем/бэктестом и отдельным PR |

### 5. Критерий «готово»

- [x] **Локально (2026-05-07):** `code_to_example` → **Missing from example: 0** (парсер `config.env.example` учитывает `KEY=` внутри поясняющих комментариев; добавлены недостающие закомментированные ключи). Артефакты: `docs/audit_code_to_example_2026-05-07.txt`, `docs/audit_config_to_code_example_2026-05-07.txt`.
- [ ] После выравнивания с **боевым** `config.env` на VM — повторить аудит и убрать мусор из прода по списку `config_to_code`.
- [ ] В прод‑`config.env` нет очевидного мусора из списка `config_to_code` (или он явно помечен как внешний/infra).
- [ ] Удалённые ветки кода не оставляют «висячих» импортов; CI/локальный `py_compile` зелёный.

### 6. Разбиение коммитов: подсистема «удержание / ранний TIME_EXIT» vs чистка конфига

Две линии работ **не смешивать в одном коммите**, чтобы откат и ревью были простыми.

| Коммит / PR | Содержимое | Риск | Когда |
|-------------|------------|------|--------|
| **A. Recovery / аналитика TIME_EXIT (офлайн)** | `services/game5m_recovery_catboost.py`, `scripts/train_game5m_recovery_catboost.py`, блоки в `services/trade_effectiveness_analyzer.py` (`game5m_hold_recovery_*`, `game5m_recovery_model_status`, `recovery_scenario_backtest`), `web_app.py` (`export_recovery_ml`), ключи recovery в `config.env.example`, `docs/GAME_5M_TIME_EXIT_RECOVERY_PLAN.md`, при необходимости краткая отсылка в `docs/ML_GAME5M_CATBOOST.md`. **Без D4** — без изменения live `should_close_position` в `game_5m.py`. | Низкий для торговли (только отчёт/API/обучение) | Можно влить до или параллельно чистке, отдельным merge |
| **B. Аудит конфига и example** | Выводы `audit_config_unused_keys.py`, правки **только** `config.env.example` и документации к ключам; артефакты `docs/audit_*.txt` при необходимости. | Низкий | После прогона аудита против репо и (завтра) против прод‑`config.env` |
| **C. Зачистка кода по приоритетам P0–P2** | Удаление/упрощение веток, не трогая `game_5m`/`recommend_5m` до P3. | Средний | Маленькими коммитами по одному префиксу/файлу |
| **D. P3 торговый hot path** | `services/game_5m.py`, `recommend_5m.py` — только с реплеем и отдельным ревью. | Высокий | Отдельный PR после смоука |

**Прод‑`config.env` на VM** править отдельно от коммитов A–C (бэкап, точечные удаления ключей по списку аудита), затем деплой по `scripts/deploy_from_github.sh`.

**Пост‑ревью эффекта recovery в проде** (после будущего D4): телеметрия в `exit_context_json` и срезы через анализатор — см. раздел «Контроль эффекта после включения» в `docs/GAME_5M_TIME_EXIT_RECOVERY_PLAN.md`.

## Откат

- Любой деплой: откат образа/коммита на VM по обычному процессу; прод‑`config.env` править только из бэкапа, если трогали ключи.

## Ссылки в репозитории

- Скрипт аудита: `scripts/audit_config_unused_keys.py`
- Торговые лимиты по дням: `services/game_5m.py` (`GAME_5M_MAX_POSITION_DAYS`, `GAME_5M_MAX_POSITION_DAYS_<TICKER>`)
- Ранний выход: `services/game_5m.py` (`TIME_EXIT_EARLY`, stale_reversal), метрики в анализаторе: `time_exit_early_review`
