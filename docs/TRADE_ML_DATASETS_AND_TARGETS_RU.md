# Четыре CatBoost-сетки: 5m вход, portfolio, удержание, «календарь»

В репозитории **`lse`** четыре обучаемых **CatBoost**-модели с разными юнитами наблюдения, признаками и типом предикта. Ниже — цель каждой сетки, откуда накапливаются факты и как задаётся **положительный / отрицательный** (или знак) итог для обучения.

Сводка по скриптам и артефактам:

| Сетка | Скрипт | Артефакт `.cbm` | Тип задачи |
|-------|--------|-----------------|------------|
| Вход GAME_5M (5m) | `scripts/train_game5m_catboost.py` | `game5m_entry_catboost.cbm` | классификация |
| Portfolio (дневка) | `scripts/train_portfolio_catboost.py` | `portfolio_return_catboost.cbm` | регрессия |
| Удержание / recovery | `scripts/train_game5m_recovery_catboost.py` | `game5m_recovery_catboost.cbm` | классификация |
| Календарь событий (earnings) | `scripts/train_event_reaction_catboost.py` | `event_reaction_forward5d_catboost.cbm` | регрессия |

> **Про «макро-календарь» Investing в KB:** гейт новостей (`kb_news_report`, `calendar_ctx`) — **отдельно** от этой CatBoost; здесь «календарь» = строки **`event_reaction_dataset`** (события из KB + forward-исход по `quotes`).

Контроль готовности кроном: `scripts/run_ml_train_readiness_cron.py` (флаги `ML_READINESS_SKIP_*`).

---

## 1. CatBoost входа в игру 5m (`game5m_entry_catboost`)

### Цель

Оценить **вероятность благоприятного исхода сделки** по признакам **только на момент входа** (тот же `context_json`, что пишет бот на BUY). Не заменяет правила; см. `docs/ML_GAME5M_CATBOOST.md`, `docs/GAME_5M_CATBOOST_FUSION.md`.

### Юнит наблюдения

Одна **закрытая** сделка `GAME_5M` с непустым нормализованным `context_json` на входе.

### Накопление фактов

- Источник: `trade_history` → `compute_closed_trade_pnls`.
- Признаки: `services/catboost_5m_signal.py` (`row_from_entry_context_dict`) — числовые поля + `ticker`; корреляции **только из сохранённого JSON**, без пересчёта «текущей» матрицы.
- Исключаются из обучения известные артефакты (ложный тейк по session high, выход на границе 09:25–09:30 ET) — см. `train_game5m_catboost.py`.

### Предикт (что учим)

**CatBoostClassifier**, `Logloss`, на выходе `P(y=1|X)`.

| `--label` | y = 1 (положительный класс) | y = 0 |
|-----------|-----------------------------|--------|
| `net_pnl_pos` (по умолчанию) | `net_pnl > 0` (после комиссий в модели PnL) | иначе |
| `log_return_pos` | `log_return > 0` | иначе |

### Результат в рантайме

`GAME_5M_CATBOOST_ENABLED` и путь к `.cbm`; поля вроде `catboost_entry_proba_good` в ответе 5m-рекомендаций (`services/recommend_5m.py`).

---

## 2. CatBoost portfolio (`portfolio_return_catboost`)

### Цель

**Справочная** дневная оценка **ожидаемой форвард лог-доходности** по тикеру в объединённом universe (портфель + GAME_5M + корреляционный список + leader/core). Не исполняет сделки автоматически; см. `services/portfolio_catboost_signal.py`.

### Юнит наблюдения

Одна строка **(ticker, торговая дата `date`)** — срез признаков на закрытии дня `t`, таргет — движение **после** `t`.

### Накопление фактов

- Модуль: `services/portfolio_ml_features.py`, функция `build_portfolio_ml_dataset`.
- Daily `quotes` по universe из `get_portfolio_ml_universe()` (портфель, `get_tickers_game_5m`, `get_tickers_for_5m_correlation`, `PORTFOLIO_LEADER_CLUSTER` / `PORTFOLIO_CORE_CLUSTER`).
- На дату `t` признаки строятся **только из данных до close[t]** (лог-реты за 1/3/5/10/20д, волатильности, RSI, корреляции с корзинами за `corr_window_days`, относительные реты и т.д.).
- Цель обучения: колонка **`target_log_return`** =  
  `log(close[t+H] / close[t])` в **торговых** днях, где `H` = `--horizon-days` (по умолчанию **5**). Дополнительно в датафрейме есть `target_good_entry` для метрик «лучше порога издержек» — см. `portfolio_ml_threshold_log()` (bps из config).

### Предикт

**CatBoostRegressor**, `RMSE`: модель выдаёт **число в лог-пространстве** (ожидаемая форвард лог-доходность на горизонте). Положительное предсказание ≠ гарантия прибыли; в инференсе скор может маппиться в 0–100 (`_score_from_expected_log_return` в `portfolio_catboost_signal.py`).

### Результат в рантайме

`PORTFOLIO_CATBOOST_ENABLED`, `PORTFOLIO_CATBOOST_MODEL_PATH`; батч-предикт `predict_portfolio_expected_returns` для карточек портфеля.

---

## 3. CatBoost удержания (recovery, `game5m_recovery_catboost`)

### Цель

В контексте **раннего time-exit** оценить: **если в момент бара `t` подождать ещё H минут реального времени**, уложится ли цена в шаблон «достаточный отскок при ограниченной просадке» от **ref_close** на `t`. Офлайн / телеметрия и план D4 — см. `docs/GAME_5M_TIME_EXIT_RECOVERY_PLAN.md`.

### Юнит наблюдения

Одна строка = **один 5m-бар внутри удержания** закрытой long GAME_5M (псевдо-датасет из анализатора / экспорт JSONL), не целая сделка.

### Накопление фактов

- Схема и логика меток: `RECOVERY_ML_SCHEMA` и `_build_game5m_hold_recovery_dataset_stats` в `services/trade_effectiveness_analyzer.py`.
- Признаки на баре: тикер, `ref_close`, `entry_price`, PnL% от входа, время удержания, минуты после открытия RTH, календарные признаки, RSI/vol/momentum из контекста входа, усечённый `entry_decision` (категория).
- По OHLC **строго после** `t` до `t+H` считаются MFE/MAE вперёд; **`h{H}_y_recovery` = 1**, если MFE ≥ `eps_up` и MAE не хуже `max_adverse` (пороги из config), иначе **0**.

### Предикт

**CatBoostClassifier**: `P(y=1)` = вероятность «благоприятного» H-минутного окна в смысле заданных порогов. **y=0** — окно не удовлетворило критерию recovery.

### Обучение и данные

`scripts/train_game5m_recovery_catboost.py` читает JSONL экспорта (`export_recovery_ml` / `GAME_5M_RECOVERY_TRAIN_JSONL`). В вектор признаков **не** попадают post-hoc поля вроде `exit_signal` — см. `row_vector_from_export_record` в `services/game5m_recovery_catboost.py`.

---

## 4. CatBoost «календаря» событий (`event_reaction_forward5d_catboost`)

### Цель

MVP-модель **реакции цены на календарное событие** (earnings и др. в KB): по признакам **до события** предсказать **форвардную 5-дневную лог-доходность** после времени события. Отдельная таблица БД, не GAME_5M; см. `docs/EVENT_REACTION_PIPELINE.md`.

### Юнит наблюдения

Одна строка таблицы **`event_reaction_dataset`**: символ, `event_time_et`, JSON **`features_before`** и **`outcomes_after`**.

### Накопление фактов

- Скелет строк из KB (`scripts/build_event_reaction_dataset.py`), разметка исходов из daily `quotes` (`scripts/backfill_event_reaction_labeling.py`, `services/event_reaction_labeling.py`).
- В обучение попадают только строки с заполненными `features_before` / `outcomes_after`, версией `dataset_version`, и `feature_builder_version` в JSON (по умолчанию **`quotes_mvp_1`**).

### Предикт

**CatBoostRegressor**: цель **`outcomes_after.forward_log_ret_5d`** (лог-доходность на 5 торговых дней вперёд от якоря в билдере исходов). Признаки — плоские числа из `features_before` (`ret_1d_log`, `ret_5d_log`, …) + категориальный **`symbol`**.

- **Положительный / отрицательный факт** для строки: знак и величина уже **зашиты в `forward_log_ret_5d`** после бэктеста по котировкам; модель учится воспроизводи́ть это число, а не бинарный «win/loss» (хотя по порогу можно считать hit-rate в метриках train-скрипта).

### Горизонты и «второй этап»

Разметка **`outcomes_after`** в `services/event_reaction_labeling.py` уже считает **несколько** форвардных лог-доходностей: по умолчанию **`forward_log_ret_1d`**, **`_5d`**, **`_20d`** (если хватает daily `quotes` вперёд). Правило **`final_label`** (UP/DOWN/FLAT) завязано на **5d**. Текущий **`train_event_reaction_catboost.py`** обучает **только на 5d** — отдельных голов под 1d/20d в скрипте пока нет.

Расширение **фич** (второй этап в смысле *качества входа X*, а не обязательно «вторая модель»): в пайплайне задуманы дополнительные таблицы и версии `feature_builder_version` (`market_regime_daily`, `peer_graph_edge` и т.д., см. `docs/EVENT_REACTION_PIPELINE.md`) — пока они часто пусты/опциональны, **`features_before`** остаётся MVP **`quotes_mvp_1`**. Следующий этап — обогащать JSON признаков и при необходимости добавить обучение на **других горизонтах** или мульти-таргет.

### Результат в рантайме

Отдельный контур от 5m-бота; артефакт по умолчанию `local/models/event_reaction_forward5d_catboost.cbm` или `/app/logs/ml/models/...`. В readiness-кроне по умолчанию часто **пропускается** (`ML_READINESS_SKIP_EVENT_REACTION`).

---

## Связанные CSV (не четыре CatBoost)

Датасеты **`game5m_stuck_dataset`** и **`game5m_continuation_dataset`** строятся для разметки риска зависания / post-take upside; **отдельных** `train_*_stuck_catboost` в репо нет — это подготовка данных, см. `docs/GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md`.

---

## Быстрая памятка «что за что отвечает»

| Сетка | Вопрос модели |
|-------|----------------|
| 5m entry | При **таком входе** чаще ли в истории получался **плюс по сделке**? |
| Portfolio | На **таком дневном срезе** какова **ожидаемая лог-доходность на H торговых дней**? |
| Recovery (удержание) | С **этого бара** за **H минут** цена пройдёт порог «upside без чрезмерной просадки»? |
| Event / календарь | После **события в календаре** каков **5d log-return**, зная только пре-дневные признаки? |
