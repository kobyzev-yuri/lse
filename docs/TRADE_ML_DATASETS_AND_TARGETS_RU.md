# CatBoost-сетки, ridge multiday и earnings grid: датасеты, метки, метрики и путь к единой точке решения

В репозитории **`lse`** несколько обучаемых **CatBoost**-моделей (градиентный бустинг, артефакт `.cbm`) с разными юнитами наблюдения, признаками и типом предикта; отдельно — **ridge** (линейная регрессия с L2) по дневным log-доходностям на 1–3 торговых дня (не CatBoost) и **OLS gap forecast** (ordinary least squares — линейная регрессия, не CatBoost). Ниже — структура датасетов, метки, метрики L2, роль в **legacy hot path** (текущий исполнитель) и в **decision_stack** (путь к единому `decision_effective`).

**Словарь терминов:** [ML_GLOSSARY_RU.md](ML_GLOSSARY_RU.md) — L1/L2/L3, AUC, RMSE, BMO/AMH, open-path vs event 5d, shadow, spillover.

**Связанные документы:**

| Тема | Документ |
|------|----------|
| Фазы калибровки A–E | [ML_CALIBRATION_PHASES.md](ML_CALIBRATION_PHASES.md) |
| Dual-track legacy + stack, prod-статус | [ML_STATUS_REPORT.md](ML_STATUS_REPORT.md) |
| Канон L1–L3, cron | [ML_AND_DECISION_ARCHITECTURE.md](ML_AND_DECISION_ARCHITECTURE.md) |
| Алгоритм GAME_5M, contributions | [GAME_5M_DECISION_ARCHITECTURE.md](GAME_5M_DECISION_ARCHITECTURE.md) |
| Earnings UI, Fusion | [earnings-event-agent-lse/EARNINGS_UI_GUIDE.md](earnings-event-agent-lse/EARNINGS_UI_GUIDE.md) |

**L1 retrain:** [ML_UNIFIED_RETRAIN_FRAMEWORK.md](ML_UNIFIED_RETRAIN_FRAMEWORK.md). Runtime-реестр: `services/ml_product_runtime.py`.

---

## 0. Event 5d vs Open-path — два разных контура (не смешивать)

Полная таблица и примеры: [ML_GLOSSARY_RU.md](ML_GLOSSARY_RU.md) §5. Якоря BMO/AMH: [EVENT_REACTION_PIPELINE.md](EVENT_REACTION_PIPELINE.md).

| | **Event 5d** (реакция на earnings, горизонт ~5 дней) | **Open-path** (сценарий первого RTH-часа) |
|---|-------------|---------------|
| **Вопрос** | Куда пойдёт цена через ~5 торговых дней после отчёта? | Gap fade, continuation или chop в первый час? |
| **Таблица / юнит** | `event_reaction_dataset` — 1 строка на событие | `game5m_open_path_labels` — `(symbol, trade_date)` |
| **Target (y)** | `forward_log_ret_5d`, LLM `final_label` | `target_scenario` (rule после close) |
| **Premarket в X** | **Нет** — leak-safe close-якорь (T−1 для BMO) | **Да** — по задумке |
| **Prod-режим** | advisory (Brief) | shadow |
| **Скрипты** | `backfill_event_reaction_labeling.py`, `train_event_reaction_catboost.py` | `label_open_path_scenarios.py`, `train_open_path_scenario_classifier.py` |

**Пример — NVDA earnings BMO во вторник (один календарный день, два ML-вопроса):**

```text
Event 5d:
  X на Mon close (без Tue premarket)
  y = log-ret NVDA Mon close → +5 торговых дней
  → «через неделю после отчёта UP/DOWN/FLAT?»

Open-path (если есть GAME_5M в тот же trade_date):
  X на Tue pre-open: gap, macro, multiday h1
  y = rule по OHLC первого RTH-часа (fade / continuation / …)
  → «как торговать открытие во вторник?»
```

> Критика «premarket leakage в event ML» относится к **неправильному якорю** event 5d, **не** к open-path.

---

## Сводная матрица: датасет → метка → метрика → использование

| Контур | Юнит наблюдения | Источник / таблица | Target (y) | L2-метрика | Legacy (исполняет) | Stack (`decision_snapshot`) | Prod (2026-06) |
|--------|-----------------|-------------------|------------|------------|-------------------|----------------------------|----------------|
| **game5m_entry** | 1 закрытая GAME_5M сделка | `trade_history.context_json` | `net_pnl_pos` / `log_return_pos` | AUC valid ≥0.52 | `GAME_5M_CATBOOST_ENABLED` + fusion | `catboost_entry_5m` | ❌ off (AUC≈0.50) |
| **portfolio** | (ticker, trade_date) | `quotes` + `portfolio_ml_features` | `target_log_return` H=5d | RMSE, top-decile edge | `PORTFOLIO_CATBOOST_ENABLED` | `portfolio_catboost` | ✅ promoted |
| **recovery** | 1 бар удержания (5m) | JSONL анализатора | `h{H}_y_recovery` бинарный | AUC valid | D4a telemetry (`RECOVERY_ML_ENABLED`) | `recovery_ml` log_only | telemetry |
| **multiday_lr** | (ticker, day i) | daily `quotes` + opt. premarket | log-ret 1/2/3d forward | walk-forward OOS | `MULTIDAY_ENTRY/HOLD_GATE_MODE` | `multiday_lr` | entry apply |
| **gap_forecast** | (symbol, trade_date) | `game5m_gap_forecast_daily` | `open_gap_pct` (факт) | MAE pp, sign agreement | `premarket_gap_baseline` (observable) | `gap_forecast` caution | ML < naive |
| **event_reaction** | 1 earnings event | `event_reaction_dataset` (ERD) | `forward_log_ret_5d` | RMSE valid ≤0.12 | advisory brief | `event_reaction` | advisory |
| **earnings_grid** | 1 earnings event | ERD + LLM labels | `final_label` (класс сценария) | valid accuracy | shadow `/earnings` | — (Fusion) | shadow |
| **peer_spillover** | (source_event, peer) | ERD + `peer_graph_edge` | `peer_forward_log_ret_5d` | sign accuracy | brief context | — | advisory |
| **open_path** | (symbol, trade_date) pre-open | `game5m_open_path_labels` | `target_scenario` (rule) | accuracy, prerequisites | shadow | — | shadow |

Скрипты и артефакты:

| Сетка | Скрипт | Артефакт | Тип задачи |
|-------|--------|----------|------------|
| Вход GAME_5M (5m) | `train_game5m_catboost.py` | `game5m_entry_catboost.cbm` | классификация |
| Portfolio (дневка) | `train_portfolio_catboost.py` | `portfolio_return_catboost.cbm` | регрессия |
| Удержание / recovery | `train_game5m_recovery_catboost.py` | `game5m_recovery_catboost.cbm` | классификация |
| Event regression | `train_event_reaction_catboost.py` | `event_reaction_forward5d_catboost.cbm` | регрессия |
| Scenario classifier | `train_event_reaction_scenario_classifier.py` | `event_reaction_scenario_catboost.cbm` | классификация |
| Peer spillover | `train_peer_spillover_regressor.py` | `peer_spillover_forward5d_catboost.cbm` | регрессия |
| Open-path | `train_open_path_scenario_classifier.py` | `open_path_scenario_catboost.cbm` | классификация |
| Multiday ridge | `train_game5m_multiday_lr.py` | `multiday_lr/{TICKER}.json` | ridge регрессия |
| Gap forecast | `analyze_game5m_gap_forecast.py` | OLS coefs + metrics JSON | OLS (не .cbm) |

> **Про «макро-календарь» Investing в KB:** гейт новостей (`kb_news_report`, `calendar_ctx`) — **отдельно** от CatBoost; здесь «календарь событий» = строки **`event_reaction_dataset`** (earnings из KB + forward-исход по `quotes`).

> **Два слоя event ML не смешивать:** product-регрессия (`quotes_regime_v1`) ≠ earnings grid classifier (`quotes_regime_earnings_v1` + LLM labels). Вкладка **Spillover** на `/earnings` — **факты** forward log-ret peers из `quotes`, не ML-прогноз.

Контроль готовности: `scripts/run_ml_train_readiness_cron.py`, earnings grid: `scripts/run_earnings_ml_refresh.py` (флаги `ML_READINESS_SKIP_*`). **Переобучение по контурам (data-driven):** [ML_UNIFIED_RETRAIN_FRAMEWORK.md](ML_UNIFIED_RETRAIN_FRAMEWORK.md).

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

### Метрики L2 / train

| Метрика | Порог readiness | Prod (2026-06) |
|---------|-----------------|----------------|
| `auc_valid` | ≥ `ML_READINESS_GAME5M_AUC_MIN` (0.52) | ≈0.50 ❌ |
| `n_valid` | ≥ min rows | 45 |
| Артефакт | `last_game5m_train_metrics.json` | dry-run/full в dispatcher |

### Результат в рантайме

`GAME_5M_CATBOOST_ENABLED` и путь к `.cbm`; поля `catboost_entry_proba_good` в 5m-рекомендациях. Fusion: `GAME_5M_CATBOOST_FUSION=hold_if_buy_below_p`. **Legacy** path: `apply_game5m_policy_gates()` или блок в `get_decision_5m` при `OWN_FINALIZE=false`. **Stack:** `catboost_entry_5m` contribution.

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

### Метрики L2 / train

| Метрика | Порог | Prod |
|---------|-------|------|
| `rmse_valid` | readiness gate | ≈0.078 ✅ |
| top-decile edge vs bps | analyzer | monitoring |
| Артефакт | `last_portfolio_train_metrics.json` | |

### Результат в рантайме

`PORTFOLIO_CATBOOST_ENABLED`, `PORTFOLIO_CATBOOST_MODEL_PATH`; `predict_portfolio_expected_returns` для карточек. **Единственный контур с L2✅ + L3✅ на legacy** (без RESOLVE).

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

## 4. Event regression — product advisory (`event_reaction_forward5d_catboost`)

### Цель

Предсказать **форвардную 5-дневную log-доходность source-тикера** после **конкретного earnings** (якорь = дата/время события в KB), зная только признаки **до отчёта**. Отдельный контур от GAME_5M; см. [EVENT_REACTION_PIPELINE.md](EVENT_REACTION_PIPELINE.md), `/earnings` → Brief / Fusion.

**Не путать с multiday ridge (§6):** ridge — на **любой** торговый день и горизонты 1–3d; event regression — только **даты earnings**, горизонт **5d после отчёта**, pooled CatBoost по событиям.

### Юнит наблюдения

Одна строка **`event_reaction_dataset`**: symbol, `event_time_et`, JSON **`features_before`**, **`outcomes_after`**.

Product dataset: **`v0_expanded_baseline`**, **`feature_builder_version=quotes_regime_v1`** (quotes + market regime + RSI и т.д.). Тексты call / LLM extract в **этой** регрессии пока **не** входят в X (идут в earnings grid и Brief).

### Накопление фактов

- Скелет из KB: `scripts/build_event_reaction_dataset.py`
- Разметка: `scripts/backfill_event_reaction_labeling.py`, `services/event_reaction_labeling.py`
- Исходы из daily **`quotes`**: `forward_log_ret_1d`, `_5d`, `_20d` в `outcomes_after`

### Предикт

**CatBoostRegressor**, цель **`outcomes_after.forward_log_ret_5d`**. Признаки — плоские числа из `features_before` + категориальный **`symbol`**.

Inference: `/api/ml/event-reaction/{ticker}?event_date=YYYY-MM-DD` (строка dataset на дату события; при нескольких версиях features приоритет у `quotes_regime_v1`).

### Результат в рантайме

Advisory в карточках и `/earnings` (Brief regression block). **`execution_blocked`** в Fusion — сделки не исполняются автоматически. Readiness: `event_reaction.gate` в `run_ml_train_readiness_cron.py`.

### Что даёт сверх ridge для GAME / earnings

| Аспект | Multiday ridge | Event regression |
|--------|----------------|------------------|
| Якорь | Конец **произвольного** дня | **Дата earnings** |
| Горизонт | 1 / 2 / 3 торг. дня | **5d после отчёта** |
| Частота | Каждый день | ~раз в квартал на тикер |
| Смысл | «Обычный краткий drift» | «Реакция **после этого отчёта**» |
| Peers / call | Нет (план: флаги календаря в X) | Regime; graph/tone — в **classifier** (§5) |

Ridge **не заменяется** — это фоновый контур GAME_5M; event regression — **event-conditioned** слой на редких высокоинформативных датах.

---

## 5. Scenario classifier — earnings grid (`event_reaction_scenario_catboost`)

### Цель

Ответить не «сколько % за 5 дней», а **«какая история разворачивается»** после конкретного earnings. Это критично, когда **source и peers двигаются в разные стороны**: META −10% при MU +28% — регрессия по META даёт одну цифру, а сценарий `capex_positive_for_infra_peers` перенаправляет внимание на **infra-пиров**.

**Глубокий разбор LLM → label → train:** [earnings-event-agent-lse/EARNINGS_LLM_ML_LABELS_AND_TRAINING.md](earnings-event-agent-lse/EARNINGS_LLM_ML_LABELS_AND_TRAINING.md).  
**Решение source vs peer (примеры META, NVDA):** тот же документ **§8**.  
**UI / Fusion:** [earnings-event-agent-lse/EARNINGS_UI_GUIDE.md](earnings-event-agent-lse/EARNINGS_UI_GUIDE.md).

Оркестратор: `scripts/run_earnings_ml_refresh.py`. Inference: `services/earnings_scenario_signal.py`. Fusion: `services/earnings_intelligence_fusion.py`.

### Юнит наблюдения и разметка

| Поле | Значение |
|------|----------|
| Таблица | `event_reaction_dataset` (одна строка = **одно** earnings-событие **одного** source-тикера) |
| **X** | `features_before` с `feature_builder_version=quotes_regime_earnings_v1`: quotes, regime, tone/timing (скаляры из LLM), `peer_graph_*`, peer momentum |
| **y** | `final_label` = ID сценария, `label_source=llm_scenario_v0` |
| Исключения из train | `UP` / `DOWN` / `FLAT` (`label_source=auto_quotes_v1`) — это rule-метки, не scenario ML |

Цепочка метки: materials → LLM `scenario_hints` → `apply_earnings_scenario_labels.py` (top hint по confidence: high > medium > low) → CatBoost учится предсказывать класс **без повторного LLM** на inference.

### Каталог классов v0 (все 7)

Зашиты в промпт `services/earnings_material_extractor.py`. Модель предсказывает **ровно один** из них (+ proba по всем классам в `scenario_class_probs`).

| ID класса | Что означает (нарратив) | Типичный source (5d) | Куда смотреть по peers |
|-----------|-------------------------|----------------------|-------------------------|
| `gap_up_follow_through` | Позитивный гэп **удержался**, рост продолжается | Bullish | Peers по кластеру с умеренным позитивным spillover |
| `gap_up_fade` | Гэп вверх **не удержался**, «продали новость» | Bearish / mixed | Осторожность по source; peers часто слабее source |
| `beat_selloff_pullback` | Beat по цифрам, но акция **падает** (фиксация) | Short-term bearish | Не путать с полным развалом тезиса |
| `beat_revaluation_down` | Цифры ок, но рынок **снижает мультипликатор** (capex, margin, risk) | Bearish | Долгий re-rating; peers из того же сектора под вопросом |
| `miss_or_guide_breakdown` | Miss / плохой guidance — **ломается тезис** | Bearish | Избегать long source; contagion на слабых peers |
| `cross_earnings_contagion` | Отчёт **тянет весь кластер** (одно направление) | Зависит от знака шока | Смотреть **всех** peers из `affected_tickers` / graph |
| `capex_positive_for_infra_peers` | Source под давлением (capex/invest), но **infra-пиры** могут выиграть | Source часто **−** | **Главный peer-кейс:** MU, SNDK, AVGO и т.д. из peer graph |

**Важно:** класс описывает **событие source**, не «тикер навсегда». На новом отчёте тот же тикер может получить другой класс.

### Эвристики знака: source vs peer (код)

Модель выдаёт класс; для Fusion/Shadow знак 5d **source** и **peers** задаётся эвристикой (не отдельным target):

**Source** — `SCENARIO_SOURCE_SIGN` в `services/earnings_scenario_signal.py`:

| Класс | `predicted_scenario_sign` | Интерпретация |
|-------|---------------------------|---------------|
| `gap_up_follow_through` | **+1.0** | Ожидаем bullish 5d по source |
| `beat_selloff_pullback` | −0.5 | Слабый/отрицательный bias |
| `beat_revaluation_down` | **−1.0** | Bearish re-rating |
| `miss_or_guide_breakdown` | **−1.0** | Bearish, сильный негатив |
| `gap_up_fade` | −0.3 | Слабый негатив |
| `cross_earnings_contagion` | **0.0** | Знак не у source — смотри кластер |
| `capex_positive_for_infra_peers` | **−0.5** | Source под давлением; **peers отдельно** |

**Peers** — `SCENARIO_PEER_SIGN` в `services/earnings_scenario_shadow.py` (для shadow / spillover tab):

| Класс | Peer sign | Смысл |
|-------|-----------|--------|
| `capex_positive_for_infra_peers` | **+1.0** | Смотреть long/careful на infra peers |
| `cross_earnings_contagion` | +0.5 | Peers в том же направлении, что шок |
| `gap_up_follow_through` | +0.3 | Умеренный позитив spillover |
| `miss_or_guide_breakdown` | −0.3 | Осторожность по связанным именам |

### Как это помогает принять решение (source vs peer)

```text
Новый earnings у SOURCE (например META)
    │
    ├─ Event regression (§4)     → pred forward_log_ret_5d для META (число)
    ├─ Scenario classifier (§5)  → predicted_scenario + proba + scenario_class_probs
    ├─ LLM Brief                 → management_tone, affected_tickers, evidence_quotes
    │
    ├─ Решение по SOURCE-тикеру:
    │     • regression: pred > +0.4% log → bullish_5d / иначе bearish/neutral
    │     • scenario_sign: +1 / −1 / 0 из таблицы выше
    │     • Fusion alignment: если regression и scenario конфликтуют → conviction=low
    │
    └─ Решение по PEER-тикерам (MU, SNDK, …):
          • НЕ из scenario classifier напрямую (он обучен на source symbol)
          • peer_spillover_outcomes — факты 1d/5d после прошлых отчётов (валидация)
          • peer_spillover_ml — pred 5d log-ret для пары (source_event, peer)
          • SCENARIO_PEER_SIGN + affected_tickers + peer_graph → кого смотреть в первую очередь
```

**Пример META capex (prod smoke):** regression pred source ≈ +0.19% (слабо bullish), scenario `capex_positive_for_infra_peers` proba ~94%, source_sign −0.5, peer_sign +1.0 → оператор **не** усиливает long META, а открывает **Spillover tab** для MU/SNDK и сравнивает с `peer_spillover_ml`.

### Fusion advisory (`build_earnings_fusion_advisory`)

| Поле | Роль |
|------|------|
| `regression_ml.forward_log_ret_5d_pred` | Числовой bias по **source** |
| `scenario_ml.predicted_scenario` | Класс истории |
| `scenario_ml.predicted_scenario_proba` | Уверенность модели |
| `scenario_ml.scenario_class_probs` | Полное распределение по 7 классам |
| `advisory.regression_bias` | `bullish_5d` / `bearish_5d` / `neutral_5d` |
| `advisory.scenario_bias` | `scenario_bullish` / `bearish` / `mixed` |
| `advisory.alignment` | `aligned_or_weak` / `conflict` / `partial` |
| `advisory.conviction` | `low` / `medium` (medium только при согласии + сильный pred) |
| `peer_spillover` / `peer_spillover_ml` | Таблица по **каждому peer** |

**`execution_blocked: true`** — Fusion **никогда** не шлёт ордер. Это research/advisory слой до фазы D ([EARNINGS_LLM_ML_LABELS_AND_TRAINING.md](earnings-event-agent-lse/EARNINGS_LLM_ML_LABELS_AND_TRAINING.md) §8, §11).

### Метрики L2 / shadow

| Метрика | Где | Prod (ориентир) |
|---------|-----|-----------------|
| `valid_accuracy` | `last_event_reaction_scenario_train_metrics.json` | ~67% при n_train≈24 |
| Sign accuracy (source 5d) | `last_earnings_scenario_shadow.json` | ~64% на matured |
| Class accuracy vs LLM label | shadow report | зависит от редких классов |
| Peer sign accuracy | shadow | отдельно от source |
| Readiness | `overall_scenario_classifier_ready`, `overall_grid_ready` | grid ready, shadow pilot |

Редкие классы (`miss_or_guide_breakdown`, `beat_revaluation_down`) часто по 1–2 sample — модель может их не предсказывать стабильно.

### Связь с другими контурами (не дублировать)

| Контур | Вопрос | vs scenario classifier |
|--------|--------|------------------------|
| Event regression (§4) | Сколько % source за 5d? | Число без нарратива |
| Peer spillover ML (§9) | Сколько % **конкретного peer**? | Per-peer регрессия |
| Spillover tab (факты) | Как peers **ходили** на прошлых отчётах? | Backtest, не forward |
| GAME_5M decision_stack | Вход long 5m сегодня? | **Не подключено** (фаза D+) |

### Планируемое использование (roadmap)

| Фаза | Source-тикер | Peer-тикеры |
|------|--------------|-------------|
| **A (сейчас)** | Fusion + Brief advisory | Spillover tab + peer_spillover_ml в UI |
| **B** | Оператор доверяет при ≥80 LLM labels, shadow sign≥58% | Те же gates + peer sign accuracy |
| **C** | Event desk alerts, watchlist | Peer watchlist из `affected_tickers` |
| **D (долго)** | Soft veto/boost в GAME_5M при `miss_or_guide_breakdown` + high proba | `event_fusion` policy с hard caps |

Veto по `miss_or_guide_breakdown` — **только после** доказанного shadow backtest с transaction costs.

---

## 6. Multiday ridge (дневка, 1–3 торговых дня, GAME_5M)

Отдельный контур GAME_5M: **ridge-регрессия** по дневным close (и опц. premarket / календарные флаги из БД), горизонты **1 / 2 / 3** торговых дня в log-доходности от **конца произвольного дня i**. Скрипт: `scripts/train_game5m_multiday_lr.py`. Рантайм: `services/log_return_multiday_forecast.py`. Полное описание: [GAME_5M_MULTIDAY_LR_RIDGE.md](GAME_5M_MULTIDAY_LR_RIDGE.md). План обогащения X: [GAME_5M_MULTIDAY_LR_FEATURE_ENRICHMENT_PLAN.md](GAME_5M_MULTIDAY_LR_FEATURE_ENRICHMENT_PLAN.md).

| Аспект | Содержание |
|--------|------------|
| **Зачем** | Справочный сигнал «куда смещена ожидаемая дневная доходность» на 1–3 торг. дня — **не** event-study |
| **Признаки** | Лаги daily log-ret, vol, cum5d; опц. premarket; live: vol/mom 5m; **план:** флаги earnings/macro как дневные колонки (не текст call) |
| **По умолчанию** | `GAME_5M_MULTIDAY_LR_REG_ENABLED` на prod **true**; entry gate **apply** на legacy |
| **vs event regression (§4)** | Ridge — **каждый день**; event CatBoost — **только earnings**, горизонт **5d** |
| **L2 gate** | `multiday_lr` block в `ml_train_readiness.jsonl`; arbiter `multiday_lr_gates_arbiter` |
| **Метрики train** | `last_multiday_lr_train_metrics.json` (n_tickers_fitted) |

---

## 7. Gap forecast (OLS ridge, не CatBoost)

### Сухой остаток

**Да:** гэп на **open того же дня** прогнозируется **до начала RTH (9:30 ET)**, в **PRE_MARKET**, с учётом **текущего** premarket gap (и макро).  
**Нет:** это не прогноз open следующего календарного/торгового дня; не «вчера close → завтра open» без PM.

### Цель

Предсказать **фактический open gap %** `(RTH open / prev close − 1) × 100` для **текущего** `trade_date`. Сравнивается с **observable baseline** `premarket_gap_pct` (текущий PM gap как прогноз open). ML promotion только если beat baseline на OOS — см. [GAME_5M_DECISION_ARCHITECTURE.md](GAME_5M_DECISION_ARCHITECTURE.md) §5.

### Юнит наблюдения

Строка **`game5m_gap_forecast_daily`**: `symbol`, `trade_date`.

| Фаза | Поля |
|------|------|
| PRE_MARKET (до 9:30) | `premarket_gap_pct`, `pred_sector_gap_pct`, `pred_ticker_gap_pct` — **predict** |
| После open | `open_gap_pct` — **факт y**; `error_pred_ticker_vs_open_pct` |

Обучение ridge: пары **X до open → y на open** по **одному** `trade_date` (не T→T+1).

### Накопление фактов

- Ingest premarket: `ingest_game5m_gap_forecast.py --phase premarket` / `premarket_cron` → снимок в БД
- Ingest open: `--phase open` после 9:30 ET → факт + ошибки
- Анализ: `analyze_game5m_gap_forecast.py` → `last_gap_forecast_metrics.json`
- Train ridge: `train_premarket_gap_model.py`; L1 refresh: `run_gap_forecast_refresh.py`

### Predict (runtime)

- **Вход:** `premarket_gap_pct` **сегодня** (live в веб / Yahoo в cron), `macro_predicted_sector_gap_pct` **сегодня**, symbol.
- **Выход:** прогноз `open_gap_pct` **на open сегодня**.
- **Effective:** `GAME_5M_OPEN_GAP_FORECAST_POLICY=auto` → baseline (PM), пока ML не обгоняет naive на rolling MAE.

Sector OLS + pooled ridge v2 + per-ticker OLS fallback; не `.cbm`. Поля в карточке: `ticker_open_gap_predicted_pct`, `forecast_layer`; веб: `baseline_open_gap_pct`, `ml_open_gap_pct`, `effective_open_gap_pct`.

### Метрики L2

| Метрика | Baseline (premarket naive) | ML ridge (prod) |
|---------|---------------------------|-----------------|
| MAE (pp) | ≈1.36 (90d) | ≈1.62 (90d), caution |
| Sign agreement | ≈80% | ≈70% |

### Использование

- **Legacy:** `premarket_gap_baseline` (observable) — production в decision_stack; влияет на entry_advice
- **Stack:** `gap_forecast` — caution / log_only; `forecast_layer` объединяет envelope
- **Open-path:** `gf_*` признаки в open-path classifier (§8)

---

## 8. Open-path scenario classifier

> **Не путать с event 5d:** open-path — первый RTH-час; event 5d — forward ~5 дней после earnings. См. §0 и [ML_GLOSSARY_RU.md](ML_GLOSSARY_RU.md) §5.

### Цель

Классифицировать **сценарий первого RTH-часа** (Regular Trading Hours — основная сессия US: gap **fade** — схлопывание гэпа, **continuation** — продолжение, chop…) по pre-open признакам. MVP (минимальный продукт) внутри earnings/open-play; **не** блокирует GAME_5M cron.

### Юнит наблюдения

Строка **`game5m_open_path_labels`**: `(symbol, trade_date)` с rule-label `target_scenario` после close.

### Накопление фактов

- Labels: `label_open_path_scenarios.py` (23:45 cron) из `game5m_gap_forecast_daily` + RTH OHLC
- X: `open_path_classifier_dataset.py` — premarket DB, gap preds, macro flags, multiday h1
- Train: `train_open_path_scenario_classifier.py`

### Предикт

**CatBoostClassifier**, multi-class `target_scenario`. Метрики: valid accuracy; readiness: `last_open_path_readiness.json`.

### Использование

Shadow на `/earnings` и autoprep; prerequisites (premarket rows, gap history) — gate перед product. **Не** в `decision_stack` GAME_5M hot path до sign-off.

---

## 9. Peer spillover regressor

### Цель

Предсказать **5d log-return peer-тикера** после earnings **source** (не source itself). Дополняет scenario classifier: «META −10%, MU +28%» — regression даёт число по source, spillover — по peer.

### Юнит наблюдения

Строка **(source_event, peer_ticker)** из `peer_spillover_dataset.py`: edge weight, relation_type, source outcomes, peer features.

### Target

`peer_forward_log_ret_5d` — регрессия, **CatBoostRegressor**. Метрика L2: sign accuracy на valid.

### Использование

Advisory в earnings Brief / Fusion context. **Spillover tab** в UI — факты из `quotes` (не ML); ML spillover — forward pred для калибровки весов `peer_graph_edge`.

---

## Связанные CSV (не CatBoost-модели)

Датасеты **`game5m_stuck_dataset`** и **`game5m_continuation_dataset`** — разметка зависания / post-take upside; отдельного train CatBoost нет — см. [GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md](GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md).

---

## 10. Как ML дополняют друг друга (ситуации)

Контуры отвечают на **разные вопросы** в **разное время**. Не заменяют друг друга — накладываются слоями.

| Ситуация | Первичный сигнал | Уточняющие ML | Что делает стек |
|----------|------------------|---------------|-----------------|
| **Обычный RTH-вход long** | rules_5m RSI/momentum | entry CatBoost P(good), multiday 1d drift | CatBoost fusion → HOLD; multiday entry gate → HOLD при bearish кворуме |
| **Премаркет, gap +1.8%** | `premarket_gap_baseline` boost | gap ML (caution), multiday 2–3d | Baseline production; ML gap только telemetry пока не beat naive |
| **NEAR_OPEN / NEAR_CLOSE** | rules даёт STRONG_BUY | session (stack only при RESOLVE) | Legacy исполняет; stack projected HOLD — divergence норма |
| **Удержание, TIME_EXIT_EARLY** | rules exit | recovery P(recovery), multiday hold gate | Recovery D4a log-only; hold gate log_only (3/5 would_defer) |
| **День earnings у тикера** | rules + macro | event 5d reg, scenario class, spillover | Advisory в Brief; **не** автоблок GAME_5M |
| **Earnings у peer (META)** | spillover facts | peer spillover reg, scenario | «Capex infra peers» → смотреть MU/SNDK в Fusion |
| **Портфельный BUY** | strategy rules | portfolio CatBoost score | `PORTFOLIO_CATBOOST_BLOCK_BUY_ON_WEAK` на legacy |
| **Open-path первый час** | rule labels (offline) | open-path classifier | Shadow до product gates |

**Правило complementarity:** более **редкий** и **event-anchored** слой (earnings) не подменяет **ежедневный** (ridge, entry); он добавляет контекст на датах отчётов. Более **быстрый** горизонт (5m entry, recovery H мин) не подменяет **дневной** (multiday 1–3d) — фильтрует вход/выход на другом масштабе.

---

## 11. Единая точка принятия решения: эволюция

### Сейчас (dual-track, prod)

```text
get_decision_5m() / cron
    │
    ├─ rules_5m → technical_decision_core
    ├─ KB, macro, entry_advice, premarket_gap_baseline  (legacy policy)
    ├─ apply_game5m_policy_gates()  [OWN_FINALIZE=true]
    │     ├─ CatBoost fusion      (если GAME_5M_CATBOOST_ENABLED)
    │     └─ multiday entry gate  (если ENTRY_GATE_MODE=apply)
    │           → technical_decision_effective  ◄── LEGACY EXECUTOR (cron BUY/HOLD)
    │
    └─ finalize_game5m_decision_stack()
          ├─ collect contributions (все контуры)
          ├─ resolve_game5m_technical() → projected_effective_if_resolve
          └─ RESOLVE=false → decision_effective = legacy (snapshot в context_json)
```

**Portfolio** — отдельная поверхность: `execution_agent` + `PORTFOLIO_CATBOOST_*`, свой decision_stack в `portfolio.py`.

**Earnings** — advisory surface: Fusion, Brief; `execution_blocked: true` по умолчанию.

### Целевое состояние (единый исполнитель)

```text
decision_snapshot.contributions[]
    rules_5m, session, entry_advice, macro, premarket_gap_baseline  [production]
    catboost_entry_5m, multiday_lr, recovery_ml, gap_forecast        [production по readiness]
    news_fusion, forecast_layer, cluster_context                     [caution / LLM]
         │
         ▼
resolve_game5m_technical()  — детерминированный veto/downgrade по gate_mode=apply
         │
         ▼
decision_effective  ◄── DECISION_STACK_RESOLVE_ENABLED=true
         │
         ▼
cron / execution (одно поле, полный audit trail)
```

### Дорожная карта без big-bang

| Этап | Что включается | Где | Не ждёт |
|------|----------------|-----|---------|
| **Сейчас** | portfolio, multiday entry | **legacy** flags | RESOLVE |
| **+entry CatBoost** | AUC gate | `GAME_5M_CATBOOST_ENABLED` на legacy | RESOLVE |
| **+recovery D4b** | defer TIME_EXIT | `game_5m.py` exit path | RESOLVE |
| **+multiday hold** | defer early exit | `send_sndk_signal_cron` hold apply | RESOLVE |
| **+gap ML** | beat naive OOS | `DECISION_STACK_FORECAST_GATE_MODE` или legacy | RESOLVE |
| **+RESOLVE** | session, news_fusion, единый resolve | `DECISION_STACK_RESOLVE_ENABLED=true` | — |

**Принцип:** каждый контур с закрытым L2 gate подключается на **legacy** через свой runtime flag **сразу**; `RESOLVE=true` — финальный шаг **унификации исполнителя**, а не первое включение ML.

Проверка: `scripts/print_ml_product_status.py`, отчёт [ML_STATUS_REPORT.md](ML_STATUS_REPORT.md).

### Вход vs выход (GAME_5M)

| Фаза сделки | Единая точка входа | Единая точка выхода (цель) |
|-------------|-------------------|---------------------------|
| Сейчас | `technical_decision_effective` | `should_close_position()` в `game_5m.py` (rules + recovery telemetry) |
| С RESOLVE | `decision_effective` | recovery_ml + multiday hold в stack contributions → exit defer (D4b) |

Вход и выход **разделены намеренно**: ML entry не должен ломать технический exit core до прохождения D4a/D4b.

---

## 12. Стек ML: вклад в решения (схема)

```mermaid
flowchart TB
  subgraph legacy [Legacy hot path — исполняет сегодня]
    Rules[rules_5m]
    Policy[premarket_gap / macro / entry_advice]
    MLgates[CatBoost fusion + multiday entry]
    Rules --> Policy --> MLgates --> Eff[technical_decision_effective]
    Eff --> Cron[cron BUY/HOLD]
  end

  subgraph stack [Decision stack — параллельно]
    Contrib[contributions all contours]
    Resolve[resolve_game5m_technical]
    Contrib --> Resolve
    Resolve --> Proj[projected_effective_if_resolve]
    Resolve --> Snap[decision_snapshot in context_json]
  end

  MLgates -.-> Contrib
  Proj -.->|RESOLVE=false mirror| Eff
  Proj -.->|RESOLVE=true| Cron

  subgraph earnings [Earnings — advisory]
    ERD[event_reaction_dataset]
    ERD --> Reg[Event regression 5d]
    ERD --> Clf[Scenario classifier]
    ERD --> Peer[Peer spillover]
    Reg --> Fusion[Fusion / Brief]
    Clf --> Fusion
    Peer --> Fusion
  end

  Fusion -.->|advisory only| Contrib
```

| Слой | Тип | Когда | Вопрос | Подключение |
|------|-----|-------|--------|-------------|
| **5m entry** | классификация | Вход long | P(сделка в плюс)? | Legacy fusion (когда ENABLED) |
| **Multiday ridge** | ridge | Любой день | Drift 1–3d? | Legacy entry/hold gates |
| **Recovery** | классификация | Бар в удержании | Ждать H минут? | D4a telemetry → D4b |
| **Gap baseline** | observable | Премаркет | Фактический gap? | Legacy production |
| **Gap ML** | OLS | Премаркет/open | Pred open gap? | Stack caution |
| **Portfolio** | регрессия | Дневной срез | H-day log-ret? | Legacy portfolio cards |
| **Event regression** | регрессия | Earnings | Source 5d? | Advisory |
| **Scenario classifier** | классификация | Earnings | Какой нарратив? | Shadow / Fusion |
| **Peer spillover** | регрессия | Earnings | Peer 5d? | Brief context |
| **Open-path** | классификация | Pre-open | Сценарий 1h? | Shadow |

**LLM:** extractor и разметка — не обучаемая модель. **Fusion:** regression + classifier + brief.

---

## Быстрая памятка «что за что отвечает»

| Сетка | Вопрос модели |
|-------|----------------|
| 5m entry | При **таком входе** чаще ли в истории получался **плюс по сделке**? |
| Portfolio | На **таком дневном срезе** какова **ожидаемая лог-доходность на H торговых дней**? |
| Recovery (удержание) | С **этого бара** за **H минут** цена пройдёт порог «upside без чрезмерной просадки»? |
| **Event regression** | После **этого earnings** каков **5d log-return source**? |
| **Scenario classifier** | После **этого earnings** какой **сценарий** (fade, contagion, capex peers…)? |
| **Multiday ridge** | На **конец обычного дня i** — log-доходность на **1 / 2 / 3** торговых дня вперёд? |
| **Gap forecast (OLS)** | Каков **open gap %** vs premarket naive? |
| **Open-path** | Какой **сценарий первого часа** по pre-open признакам? |
| **Peer spillover** | Каков **5d log-ret peer** после earnings source? |
| **Spillover UI (факты)** | На **прошлом** earnings — как **фактически** ходили peers 1d/5d? |
