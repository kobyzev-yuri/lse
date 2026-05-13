# План: ранние выходы (TIME_EXIT_EARLY) — параметры по данным, затем ML recovery

**Статус-сводка (все подсистемы):** [PROJECT_STATUS_AND_ROADMAP.md](../PROJECT_STATUS_AND_ROADMAP.md). Ниже — детальный чеклист recovery (A–D).

Цель: уменьшить **преждевременные** выходы в убытке, сохранив защиту капитала. Реализация идёт **через анализатор** (`services/trade_effectiveness_analyzer.py` и API отчёта), а не разрозненными ручными правками вне этого контура.

Принципы:

- **Сначала простое:** эвристики и предлагаемые уровни `GAME_5M_*` из фактической истории и 5m OHLC.
- **Потом ML:** отдельная модель «отскок / траектория на горизонте H»; до прод-исполнения в `game_5m` — только отчётность, калибровка и офлайн-решения в анализаторе.
- **Один вход для оператора:** прогон `analyze_trade_effectiveness` / focused + JSON (и при необходимости расширение `/api/analyzer`), без параллельных «теневых» скриптов как источника правды.

---

## Фаза A (текущая база — уже в коде)

- [x] **Контрфакт после exit:** `time_exit_early_review` — post-exit MFE/MAE (1h, EOD), recovery к **входу**, минуты после RTH open, флаг whipsaw.
- [x] **Предложения порогов:** `config_candidates.proposals` для `STALE_REVERSAL_*`, `EARLY_DERISK_*`, при необходимости `EXIT_GUARD_*`; дублирование в `practical_parameter_suggestions` и `game_5m_config_hints` для `auto_config_override`.
- [x] **Пример конфига:** раскомментированные ключи в `config.env.example` для редактора и автоприменения.

**Действия по эксплуатации (не код):** регулярный прогон на 2–4 неделях сделок → малые шаги порогов → фиксация в `config.env` на VM после спокойного окна.

---

## Фаза B — упростить цикл «данные → решение» внутри анализатора

Всё ниже — изменения **только** в анализаторе (и при необходимости общих хелперах, вызываемых из него).

| # | Задача | Смысл |
|---|--------|--------|
| [x] B1 | **Сводка «готовность к правке»** в корне отчёта: один блок `time_exit_early_action_summary` (число сделок по `exit_detail`, доля whipsaw, список top-N тикеров, есть ли предложения с `sample_confidence >= medium`). | Один взгляд без копания в `by_exit_detail`. |
| [x] B2 | **Стабилизация эвристик:** при малой выборке не дублировать противоречивые предложения; явный флаг `insufficient_data_for_ml=true` при n &lt; порога. | Меньше шума в `auto_config_override`. |
| [x] B3 | **Согласованность STALE vs EARLY_DERISK:** если в окне доминирует один `exit_detail`, помечать «крутить в первую очередь ключи ветки X» в том же summary. | Понятная очередность правок. |
| [x] B4 | **Focused-режим:** при `analyze_trade_effectiveness_focused(..., trade_ids=...)` усилить вес кейса в summary (уже есть фильтр; добавить явный подблок «single-trade review» с теми же метриками). | Разбор инцидентов вроде одной сделки SNDK. |
| [x] B5 | **Текстовый вывод / LLM:** в системном промпте для `_build_llm_recommendations` (если используется) явно сослаться на `time_exit_early_review` и запретить выдумывать числа, если их нет в `config_candidates`. | Согласованность с числовым блоком. |

Критерий готовности фазы B: оператор за один отчёт видит **сто ли крутить параметры** и **какие ключи приоритетны**, без внешних таблиц.

---

## Фаза C — подготовка данных для ML recovery (всё ещё через анализатор)

Цель датасета: на момент бара **t** (в удержании long) предсказать исход на горизонте **H** (2h / остаток сессии / ~1 торговый день в RTH). Пока **не** внедрять в `should_close_position` — только строить выборку и отчёты.

| # | Задача | Смысл |
|---|--------|--------|
| [x] C1 | **Новый блок отчёта** `game5m_hold_recovery_dataset_stats`: из эффектов сделок + OHLC строить **псевдо-строки** только для окон удержания GAME_5M; опционально фильтр «ранний / около нуля» через `GAME_5M_RECOVERY_DATASET_NEAR_EARLY_PCT`. | Переиспользование кэша OHLC анализатора. |
| [x] C2 | **Схема колонок** в `services/trade_effectiveness_analyzer.py`: `RECOVERY_ML_SCHEMA`, `RECOVERY_ML_SCHEMA_VERSION`, описание в `ANALYZER_METRIC_DEFINITIONS`. | Единый контракт для будущего `train_*.py`. |
| [x] C3 | **Опциональный экспорт JSONL:** параметр `export_recovery_ml` в `analyze_trade_effectiveness` / `focused` и query `export_recovery_ml` у `/api/analyzer` и `/api/analyzer/focused` (путь к файлу не с клиента — `LOG_DIR` + timestamp или `ANALYZER_RECOVERY_ML_EXPORT_PATH`). | Удобство обучения без дублирования логики OHLC. |
| [x] C4 | **Зафиксировано ниже (C4):** H по умолчанию, ε₁/ε₂, окно меток по полному 5m после `t`, ограничения по утечкам. | Один источник правды для меток. |

### C4 — метки recovery (зафиксировано)

- **Горизонты H (минуты):** по умолчанию `(120, 390)` — настраивается `GAME_5M_RECOVERY_ML_HORIZONS_MINUTES` (CSV, каждый ≥ 15).
- **ε₁ (upside для положительного класса):** `GAME_5M_RECOVERY_ML_EPS_UP_PCT`, дефолт **0.5** (% от **Close** бара `t` до **High** вперёд на `(t, t+H]`).
- **ε₂ (нижняя граница допустимой просадки):** `GAME_5M_RECOVERY_ML_MAX_ADVERSE_PCT`, дефолт **−3.0** (% от **Close** бара `t` до **Low** вперёд на том же окне). Бинарная метка: `y_recovery = 1` если MFE_fwd ≥ ε₁ и MAE_fwd ≥ ε₂ (MAE в процентах отрицательный или нулевой).
- **Окно вперёд:** бары 5m с `datetime` строго после `t` и до `t+H` включительно; используется **полный** ряд OHLC по тикеру (время после фактического выхода из сделки допустимо для контрфакта «если бы держали дальше»). В фичах не подмешиваются будущие цены после `t`, кроме явных колонок `h{H}_*`.
- **Утечки при обучении:** в строках есть `exit_signal` и агрегаты по сделке — при обучении recovery их нужно исключать или использовать только в hold-only срезе; см. `note` в `game5m_hold_recovery_dataset_stats`.

Критерий готовности фазы C: из одного прогона анализатора можно получить **воспроизводимый** снимок данных для обучения и понятные **числа по объёму/балансу классов**.

---

## Фаза D — ML (CatBoost или аналог), связка с анализатором

| # | Задача | Смысл |
|---|--------|--------|
| [x] D1 | Скрипт **`scripts/train_game5m_recovery_catboost.py`**: читает JSONL из фазы C (`--jsonl` или `GAME_5M_RECOVERY_TRAIN_JSONL`), метка `h{H}_y_recovery` (по умолчанию H=120), пишет **`GAME_5M_RECOVERY_CATBOOST_MODEL_PATH`** (`.cbm` + `.meta.json`). Признаки: `services/game5m_recovery_catboost.py`. | Не в торговом hot path. |
| [x] D2 | Блок отчёта **`game5m_recovery_model_status`**: файлы модели/meta, `trained_at`, AUC/n_valid, **`recovery_trust_level`**, флаг конфига **`GAME_5M_RECOVERY_ML_ENABLED`** (прод ещё не обязан быть включён). Калибровка «на сделках» — в **`recovery_scenario_backtest`**. | Прозрачность перед кроном/продом. |
| [x] D3 | Блок **`recovery_scenario_backtest`**: сделки `TIME_EXIT_EARLY` + `exit_strategy=GAME_5M` — скор на **последнем** 5m баре удержания; если `P < GAME_5M_RECOVERY_SCENARIO_TAU`, контрфакт выхода через **`GAME_5M_RECOVERY_SCENARIO_DELAY_BARS`** баров после `exit_ts` (Close, **без комиссий**). | Подбор τ до live. |
| [x] D4a | **Прод телеметрия (log-only):** при SELL `TIME_EXIT_EARLY` в `context_json` пишется объект `recovery_ml_time_exit_early` (скор, τ, гварды, `would_defer_exit`, `log_only=true`). Поведение выходов **не** меняется. См. `scripts/send_sndk_signal_cron.py`, ключи `GAME_5M_RECOVERY_*` в `config.env.example`. | Прозрачность до apply. |
| [ ] D4b | **Прод apply:** отложить `TIME_EXIT_EARLY` на K баров при `P >= TAU_HOLD` и прохождении гвардов — только после ревью по данным D4a + офлайн D3. | Целевое включение или откат. |

CatBoost остаётся разумным дефолтом (табличка + ticker); смена на LightGBM — вопрос одного скрипта, контракт фич из фазы C не меняется.

### Контроль эффекта и прозрачность (D4a → решение D4b или отложить)

**Цель:** накопить **доказательную базу** в БД и логах, затем **осознанно** либо включить отложение выхода (D4b), либо **отложить** внедрение (оставить log-only, выключить флаг, или пересобрать модель/τ).

**Принцип:** автоматического «включи торговлю, когда метрика ≥ X» в коде нет; решение — **операционное** (go / no-go / defer). Прозрачность = всё нужное для этого решения уже в `trade_history.context_json` и в офлайн-отчётах.

#### Что уже пишется при D4a (фактическая схема)

В SELL с `signal_type='TIME_EXIT_EARLY'` в `context_json` добавляется объект **`recovery_ml_time_exit_early`** (имя ключа в JSON), минимум:

| Поле (внутри объекта) | Смысл |
|------------------------|--------|
| `enabled`, `log_only` | Включён ли контур и режим только логирования |
| `status` | `ok` / `skipped` / ошибка предикта |
| `recovery_proba`, `tau_hold`, `defer_bars` | Скор и пороги на момент выхода |
| `would_defer_exit`, `would_defer_by_model` | Хотели бы отложить с учётом гвардов / только по модели |
| `deny_reasons` | Напр. `hard_stop_loss`, `too_old`, `near_close` |
| `guards` | Снимок PnL, momentum, возраста, минут до закрытия |
| `model_path`, `meta_summary` | Воспроизводимость версии модели |

Плюс в логе крона строка **`RECOVERY_ML_GATE`** (grep по `cron_sndk_signal.log`). До первого `TIME_EXIT_EARLY` после деплоя записей не будет — это нормально.

**После D4b** в тот же объект (или рядом) имеет смысл добавить явные флаги `recovery_ml_applied` / `defer_count` — в D4a их ещё нет, т.к. выход не откладывается.

#### Окно наблюдения (рекомендация)

| Этап | Минимум | Комментарий |
|------|---------|-------------|
| Сбор D4a | **≥ 2 торговые недели** или **≥ 8–15** событий `TIME_EXIT_EARLY` с непустым `recovery_ml_time_exit_early` | Меньше — только качественный разбор |
| Параллельно | Еженедельный `recovery_scenario_backtest` + `game5m_recovery_model_status` на том же окне дней | Сверка: офлайн «что было бы» vs live «что модель сказала в момент выхода» |

#### SQL-контроль (примеры)

- События с телеметрией после даты деплоя D4a:

```sql
SELECT id, ts, ticker,
       (context_json ? 'recovery_ml_time_exit_early') AS has_gate,
       context_json->'recovery_ml_time_exit_early'->>'status' AS st,
       context_json->'recovery_ml_time_exit_early'->>'recovery_proba' AS p,
       context_json->'recovery_ml_time_exit_early'->>'would_defer_exit' AS would_defer
FROM trade_history
WHERE strategy_name='GAME_5M' AND side='SELL' AND signal_type='TIME_EXIT_EARLY'
ORDER BY ts DESC LIMIT 30;
```

- Доля «модель хотела бы defer» при срабатывании гвардов (диагностика порогов):

```sql
SELECT
  COUNT(*) FILTER (WHERE (context_json->'recovery_ml_time_exit_early'->>'would_defer_by_model')::text = 'true') AS model_would,
  COUNT(*) FILTER (WHERE (context_json->'recovery_ml_time_exit_early'->>'would_defer_exit')::text = 'true') AS after_guards
FROM trade_history
WHERE strategy_name='GAME_5M' AND side='SELL' AND signal_type='TIME_EXIT_EARLY'
  AND context_json ? 'recovery_ml_time_exit_early';
```

#### Ревью через анализатор (офлайн)

На окне **после включения D4a**:

- `time_exit_early_review`, `time_exit_early_action_summary` — здоровье правил stale/early_derisk;
- `recovery_scenario_backtest`, `game5m_recovery_model_status` — согласованность τ и доверия к модели с live-порогом `GAME_5M_RECOVERY_LIVE_TAU_HOLD`;
- при разборе инцидентов — `analyze_trades_focused` / focused API по `trade_id`.

Сравнение: для каждой строки с `recovery_ml_time_exit_early` проверить, не противоречит ли **`would_defer_exit`** здравому смыслу (глубокий минус, обвал после выхода — см. post-exit MFE в `time_exit_early_review`).

#### Критерии go / no-go для D4b (внедрить отложение выхода)

Использовать как **ориентиры**, не как жёсткий автопилот. Все пункты обсуждаются с учётом размера выборки.

**Go (имеет смысл пробовать D4b в ограниченном виде), если:**

1. **Данные:** накоплено достаточно SELL `TIME_EXIT_EARLY` с заполненным `recovery_ml_time_exit_early`; нет массовых `status != ok` / `missing_entry_ctx`.
2. **Модель:** `recovery_trust_level` в отчёте не деградировал (AUC/meta в разумных пределах относительно истории обучения); ежедневный pipeline не даёт систематических сбоев.
3. **Согласованность:** для подвыборки, где `would_defer_exit=true`, офлайн-контрфакт или пост-фактум MFE **не показывает** систематически худший исход, чем у среза «defer отклонён гвардами» (нет ощущения «модель зовёт держать в нож»).
4. **Риск:** гварды (`HARD_STOP_*`, `DEFER_MAX_AGE`, `NEAR_CLOSE`) считаются достаточными; нет желания расширять defer на `early_derisk` до отдельного ревью.

**No-go / отложить (оставить D4a или выключить `GAME_5M_RECOVERY_ML_ENABLED`), если:**

1. Мало событий или телеметрия часто `skipped` (нет BUY `context_json`, сломан путь модели).
2. `recovery_scenario_backtest` на свежем окне показывает **плохое** соотношение improved/worse при выбранном τ или модель перестала разделять классы.
3. По live-логам видно, что `would_defer_exit=true` совпадает с **экстремальными** просадками чаще, чем ожидалось (подтвердить по `trade_effects` / ручному разбору).
4. Приоритет смещён на **ужесточение** stale/entry — тогда ML-defer откладывается до стабилизации базовых правил.

**Defer (перенести решение на 2–4 недели):** выборка пограничная, спорные кейсы по 1–2 тикерам; оставить log-only, при необходимости сменить только `TAU_HOLD` / переобучить модель, **не** включая D4b.

#### Решение после ревью (одна строка в процессе)

| Исход | Действие |
|-------|----------|
| **Внедрить** | PR D4b: `GAME_5M_RECOVERY_ML_LOG_ONLY=false`, логика defer на K баров + бюджет defer; сохранить телеметрию + флаг фактического apply |
| **Отложить** | Оставить D4a или `GAME_5M_RECOVERY_ML_ENABLED=false`; править rule-based параметры; вернуться к таблице go/no-go после нового окна |
| **Отказаться от слоя** | Выключить recovery live, оставить только анализатор + обучение для исследований |

**Критерий успеха процесса прозрачности:** по `trade_history` и отчёту анализатора можно ответить без догадок: сколько было `TIME_EXIT_EARLY` с телеметрией, какова была **распределённость** `recovery_proba` и `would_defer_exit`, сработали ли гварды, и согласуется ли это с офлайн `recovery_scenario_backtest`.

### D4 — план PR (recovery-gate в live)

Цель D4: **не заменить** Hanger V2/правила, а добавить “тонкую пролонгацию” **только в момент `TIME_EXIT_EARLY`**, когда правила уже решили закрывать.

1. **D4a (log-only) — [x] сделано** (`scripts/send_sndk_signal_cron.py`):
   - Параметры: `GAME_5M_RECOVERY_ML_ENABLED`, `GAME_5M_RECOVERY_ML_LOG_ONLY=true`, `GAME_5M_RECOVERY_LIVE_TAU_HOLD`, `GAME_5M_RECOVERY_LIVE_DEFER_BARS`,  
     `GAME_5M_RECOVERY_ONLY_EXIT_DETAIL` (рекомендуется начать с `stale_reversal`),  
     гварды: `GAME_5M_RECOVERY_HARD_STOP_*`, `GAME_5M_RECOVERY_DEFER_MAX_AGE_MINUTES`, `GAME_5M_RECOVERY_DEFER_NEAR_CLOSE_MINUTES`.
   - При закрытии `TIME_EXIT_EARLY`: считается `recovery_proba`, в `context_json` пишется объект **`recovery_ml_time_exit_early`**; в лог — `RECOVERY_ML_GATE`. Закрытие **не** меняется.

1b. **D4a rollup (накопление τ×K) — [x]** (`scripts/run_recovery_d4a_stats_cron.py`, поле анализатора `recovery_ml_d4a_live_review`):
   - Ночной крон дописывает одну строку в **`GAME_5M_RECOVERY_D4A_STATS_JSONL`** (append-only): `tau_sweep_by_k`, `best_tau_by_k`, счётчики по окну `GAME_5M_RECOVERY_D4A_STATS_WINDOW_DAYS`, сетка K из `GAME_5M_RECOVERY_D4A_STATS_K_BARS` (всегда включается `GAME_5M_RECOVERY_LIVE_DEFER_BARS`).
   - В той же строке — **`shallow_gate_by_window_days`** и **`window_suggestion`**: по календарным 7/14/21/… дням только числа TE и gate в БД (без OHLC), чтобы **периодически пересматривать**, не избыточно ли длинное окно для обзора τ×K.
   - Для оперативного разбора — страница анализатора и `GET /api/analyzer` (тот же блок в JSON; опционально `GAME_5M_RECOVERY_D4A_STATS_ATTACH_TO_ANALYZER=true`).

2. **D4b (apply) — [ ] после go/no-go** (см. выше «Критерии go / no-go»):
   - При `TIME_EXIT_EARLY`: если `recovery_proba >= TAU_HOLD` и гварды не сработали — **не закрывать**, отложить решение на K баров (defer budget).
   - Стоп / конец сессии / прочие жёсткие выходы без изменений.
   - Защита от бесконечного defer: бюджет + «один defer на сделку» или cooldown; в контексте — `recovery_ml_applied=true/false`.

3. **Пост-ревью**:
   - По таблице решений в разделе «Критерии go / no-go»: либо PR D4b, либо defer, либо отключение слоя; метрики — SQL + анализатор + при необходимости будущий блок `recovery_ml_post_deploy_review` в отчёте.

---

## Этап наблюдения: `TIME_EXIT_EARLY` и Recovery ML (D4a)

**Когда начинается:** сразу после деплоя в прод кода D4a и включения на VM `GAME_5M_RECOVERY_ML_ENABLED=true` при `GAME_5M_RECOVERY_ML_LOG_ONLY=true`.

**Цель этапа:** накопить факты в `trade_history` и логах, не меняя момент закрытия по ML. Оценить, насколько live-скор и `would_defer_exit` согласуются с офлайн `recovery_scenario_backtest` и с здравым смыслом по кейсам (гварды, глубина просадки).

### Что уже влияет на выходы vs что только наблюдается

| Слой | Влияние на исполнение |
|------|------------------------|
| Правила `game_5m` (stale_reversal, early_derisk, тейк, стоп, конец сессии и т.д.) и ваши правки `GAME_5M_*` в `config.env` | **Да** — напрямую задают, будет ли вообще `TIME_EXIT_EARLY` и при каком `exit_detail`. |
| Recovery ML D4a (`recovery_ml_time_exit_early` в `context_json`, лог `RECOVERY_ML_GATE`) | **Нет** — только телеметрия до включения D4b. |

Пока не было ни одного SELL `TIME_EXIT_EARLY` **после** деплоя D4a, записей телеметрии в БД не будет — это ожидаемо.

### Чеклист наблюдения (без срочных доработок кода)

1. **Спокойный режим:** не включать D4b и не отключать log-only ради «проверки эффекта» — эффект на PnL от ML на этом этапе по определению отсутствует.
2. **После появления новых `TIME_EXIT_EARLY`:** убедиться, что в `context_json` есть ключ `recovery_ml_time_exit_early` и поля `status`, `recovery_proba`, `would_defer_exit`, `deny_reasons` (SQL — см. выше в этом документе).
3. **Логи:** при необходимости искать в `cron_sndk_signal.log` строки `RECOVERY_ML_GATE` (в контейнере нет `rg`, достаточно `grep`).
4. **Периодичность:** раз в несколько дней — быстрый SQL по последним SELL `TIME_EXIT_EARLY`; **раз в 1–2 недели** — прогон анализатора на том же окне дней (`time_exit_early_*`, `recovery_scenario_backtest`, `game5m_recovery_model_status`). **Ежедневно** (после деплоя крона) смотрите хвост `recovery_d4a_rollup.jsonl` из `run_recovery_d4a_stats_cron.py` — там снимок `best_tau_by_k` по нарастающей выборке.
5. **Параллельно:** ежедневный recovery pipeline (если включён в cron) — следить, что модель и `meta.json` обновляются без ошибок.

### Когда этап наблюдения считается достаточным для решения

Ориентиры (см. также таблицу «Окно наблюдения» и «Критерии go / no-go» выше):

- **≥ 2 торговые недели** *или* **≥ 8–15** событий `TIME_EXIT_EARLY` с непустым `recovery_ml_time_exit_early`;
- нет систематических `skipped` / `missing_entry_ctx` / отсутствия модели;
- проведено сравнение live-телеметрии с офлайн-сценарием и при необходимости точечный разбор `trade_id` через focused-отчёт.

**Итог этапа:** зафиксировать в процессе одно из действий из таблицы «Решение после ревью» — **внедрить D4b**, **отложить** (оставить D4a или выключить recovery), **отказаться от слоя**.

---

## Порядок выполнения (для чеклиста в чате)

1. Закрыть **фазу B** в `trade_effectiveness_analyzer.py` (+ минимальные тесты/py_compile).
2. Закрыть **фазу C** (статистика + опциональный экспорт из анализатора).
3. По готовности данных — **фаза D1–D3** (обучение + отчёты; без изменения live-выходов).
4. **D4a** — деплой log-only телеметрии (`recovery_ml_time_exit_early` в SELL `TIME_EXIT_EARLY`); **этап наблюдения** — см. раздел «Этап наблюдения: TIME_EXIT_EARLY и Recovery ML (D4a)»; SQL + анализатор — см. «Контроль эффекта и прозрачность».
5. **Решение:** по критериям go / no-go — **D4b** (apply defer) или **отложить / выключить** слой; зафиксировать итог в этом документе или в ops-заметке.

---

## Ссылки

- Текущая логика выходов: `services/game_5m.py` (`TIME_EXIT_EARLY`, stale, early_derisk).
- Анализатор: `services/trade_effectiveness_analyzer.py` (`time_exit_early_review`, `auto_config_override`).
- CatBoost entry (для контраста): `docs/ML_GAME5M_CATBOOST.md`, `scripts/train_game5m_catboost.py`.
- Recovery CatBoost: `services/game5m_recovery_catboost.py`, `scripts/train_game5m_recovery_catboost.py`.
- План hanger / stale: `docs/GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md`.
