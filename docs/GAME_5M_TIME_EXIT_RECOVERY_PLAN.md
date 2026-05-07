# План: ранние выходы (TIME_EXIT_EARLY) — параметры по данным, затем ML recovery

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
| [ ] D4 | **Прод в `game_5m`:** recovery-gate для `TIME_EXIT_EARLY` (отложить закрытие на K баров) — **только после D3** и под явным флагом. Делать в 2 шага: **log-only** → **apply**. **В той же поставке** — телеметрия в `exit_context_json`, иначе ревью «улучшил ли именно ML» невозможно. Отдельный PR. | Стратегическое включение. |

CatBoost остаётся разумным дефолтом (табличка + ticker); смена на LightGBM — вопрос одного скрипта, контракт фич из фазы C не меняется.

### Контроль эффекта после включения (ревью по эксплуатации)

**Цель:** через некоторое время после прод-включения иметь **воспроизводимую** оценку: дал ли слой recovery измеримое улучшение и по каким разрезам (ветка выхода, τ, тикер, окно времени, режим рынка).

**Принцип:** автоматического «включи торговлю, когда метрика ≥ X» нет; решение остаётся операционным. Зато данные для **ручного и полуавтоматического** контроля должны попадать в историю сделок.

1. **Телеметрия в момент решения (обязательно в рамках D4)** — в `exit_context_json` закрывающей сделки (или эквивалентном снимке выхода), минимум:
   - `recovery_ml_enabled` (bool) — флаг конфига на момент решения;
   - `recovery_ml_applied` (bool) — реально ли ML изменил исход относительно «чистого» rule-based early (отложили / не закрыли);
   - `recovery_ml_proba` (float | null), `recovery_ml_tau` (float), `recovery_ml_delay_bars` (int | null) — чтобы срезы по порогам были честными задним числом;
   - `recovery_ml_schema_version` или путь к модели / хеш meta (короткая строка) — какая версия скора использовалась;
   - `exit_detail` / ветка (stale_reversal vs early_derisk и т.д.) — уже есть или дополняется для согласованности.

2. **Ревью через анализатор (после накопления истории)** — регулярный прогон `analyze_trade_effectiveness` / focused на окне «после включения»:
   - сравнение когорт: сделки с `recovery_ml_applied=true` vs остальные `TIME_EXIT_EARLY` (и при необходимости vs окно «до включения» по дате, если флаг в контексте есть всегда);
   - те же блоки, что и для здоровья правил: `time_exit_early_review`, `time_exit_early_action_summary`, `recovery_scenario_backtest`, `game5m_recovery_model_status`;
   - **Параметры улучшения** (по ним же резать отчёт или SQL): `realized_pct` / `net_pnl`, доля и тяжесть `TIME_EXIT_EARLY`, post-exit MFE/MAE и whipsaw из `time_exit_early_review`, при желании — упрощённый counterfactual из D3 на новых закрытиях.

3. **Дальнейшая аккуратная реализация (по мере необходимости)** — не обязательно в первом PR D4:
   - явный подблок отчёта `recovery_ml_post_deploy_review` (агрегаты по полям телеметрии), чтобы не копать `trade_effects` вручную;
   - экспорт выборки для таблицы (тот же контур, что JSONL в фазе C).

**Критерий успеха процесса:** по закрытым сделкам можно ответить: «сколько раз сработал ML», «как изменились PnL и ранние выходы в этой подвыборке», «не ухудшили ли мы защиту (stale/риск)» — с опорой на сохранённые поля, а не на ощущение.

### D4 — план PR (реализация recovery-gate в live)

Цель D4: **не заменить** Hanger V2/правила, а добавить “тонкую пролонгацию” **только в момент `TIME_EXIT_EARLY`**, когда правила уже решили закрывать.

1. **D4a (log-only, безопасно)**:
   - Добавить параметры (пример):  
     `GAME_5M_RECOVERY_ML_ENABLED` (общий), `GAME_5M_RECOVERY_ML_LOG_ONLY=true`,  
     `GAME_5M_RECOVERY_LIVE_TAU_HOLD` (например 0.55–0.65), `GAME_5M_RECOVERY_LIVE_DEFER_BARS` (например 6),  
     hard-guards: `GAME_5M_RECOVERY_HARD_STOP_LOSS_PCT`, `GAME_5M_RECOVERY_HARD_STOP_MOMENTUM_2H_PCT`, `GAME_5M_RECOVERY_DEFER_MAX_AGE_MINUTES`.
   - В ветке `TIME_EXIT_EARLY` считать `recovery_proba` по текущим доступным фичам (аналогично D3) и логировать “что было бы”, **не меняя** закрытие.
   - Записать телеметрию в `exit_context_json`: `recovery_ml_enabled`, `recovery_ml_log_only`, `recovery_ml_proba`, `recovery_ml_tau_hold`, `recovery_ml_defer_bars`, `recovery_ml_decision=defer|deny|skip`, `recovery_ml_deny_reason`.

2. **D4b (apply, ограниченно)**:
   - При `TIME_EXIT_EARLY`: если `recovery_proba >= TAU_HOLD` и hard-guards не сработали — **не закрывать**, а отложить `TIME_EXIT_EARLY` на K баров (defer budget).
   - Стоп/конец сессии/прочие hard exits продолжают действовать.
   - Защититься от бесконечного defer: бюджет + “один defer на сделку” или cooldown.

3. **Пост-ревью**:
   - После 1–2 недель: анализатором/SQL сравнить `recovery_ml_applied=true` vs нет по PnL и по метрикам `time_exit_early_review` (whipsaw/MFE/MAE), принять решение о TAU/K и оставлять ли apply.

---

## Порядок выполнения (для чеклиста в чате)

1. Закрыть **фазу B** в `trade_effectiveness_analyzer.py` (+ минимальные тесты/py_compile).
2. Закрыть **фазу C** (статистика + опциональный экспорт из анализатора).
3. По готовности данных — **фаза D1–D3** (обучение + отчёты; без изменения live-выходов).
4. **D4** — отдельное ревью и деплой (включая телеметрию в `exit_context_json` для пост-ревью по анализатору; см. «Контроль эффекта после включения»).

---

## Ссылки

- Текущая логика выходов: `services/game_5m.py` (`TIME_EXIT_EARLY`, stale, early_derisk).
- Анализатор: `services/trade_effectiveness_analyzer.py` (`time_exit_early_review`, `auto_config_override`).
- CatBoost entry (для контраста): `docs/ML_GAME5M_CATBOOST.md`, `scripts/train_game5m_catboost.py`.
- Recovery CatBoost: `services/game5m_recovery_catboost.py`, `scripts/train_game5m_recovery_catboost.py`.
- План hanger / stale: `docs/GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md`.
