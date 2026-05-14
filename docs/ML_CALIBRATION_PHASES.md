# Фазы калибровки ML-сеток: где что происходит и какая цель

Документ задаёт **единую терминологию** фаз для всех обучаемых контуров в репозитории: четыре **CatBoost**-сетки, **recovery** по JSONL, **multiday ridge** (регрессия по дневным close), а также точки интеграции с **`run_ml_train_readiness_cron`**, **`run_ml_data_quality_report`** и **анализатором** (`/analyzer`, `trade_effectiveness_analyzer`).

**Принципы проекта:** где речь о доходностях в моделях и отчётах — **log-returns**; в торговых симуляциях и порогах «стоит ли входить» — **transaction costs**.

---

## 1. Общая схема фаз

| Фаза | Название | Цель |
|------|----------|------|
| **A** | Целостность данных и признаков | Достаточно строк, нет дыр в `quotes`, корректный `context_json` / датасет / JSONL; для event — заполненность `features_before` / `outcomes_after`. |
| **B** | Стабильность модели (гиперпараметры) | Подбор depth/iterations/λ/learning_rate или сетки ridge; валидация **по времени**, не случайный shuffle там, где это уже реализовано в скрипте. |
| **C** | Честная оценка качества **прогноза** | Hold-out / walk-forward / бэктест анализатора: метрики вне обучающей выборки (AUC, RMSE, калибровка бакетов вероятностей, RMSE по forward log-ret). |
| **D** | Калибровка **политики** (исполнение) | Пороги вероятностей, fusion (`GAME_5M_CATBOOST_FUSION`), τ по горизонтам multiday, правила «не входить если…» — **не** подмена весов модели случайными правками без фазы C. |
| **E** | Прод и мониторинг | Включение флагов в `config.env`, регрессии метрик на новых закрытиях / новых датах; при деградации — откат или переобучение. |

Фазы **A→B** обычно закрываются вручную или кроном подготовки данных + dry-run train. **C** — анализатор, офлайн-ноутбуки, JSON из `--json-metrics-out` и хвосты `*_report.jsonl`. **D** — продуктовые решения и конфиг. **E** — наблюдение в бою.

---

## 2. Где что происходит по инструментам

| Инструмент | Фазы | Комментарий |
|------------|------|-------------|
| `scripts/run_ml_train_readiness_cron.py` | A (косвенно), **B** (dry-run / full train), частично пороги **C** через гейты | Пишет `ml_train_readiness.jsonl`: `auc_valid`, `n_train`, `gate` для game5m / portfolio / event_reaction. **Multiday ridge в крон не входит** (пока отдельный скрипт). |
| `scripts/run_ml_data_quality_report.py` | A, снимок мета `.cbm` | Единый JSON качества данных и ML; опция `--game5m-train-dry-run` и т.д. — см. [ML_DATA_QUALITY_PIPELINE.md](ML_DATA_QUALITY_PIPELINE.md). |
| `scripts/train_*` + **`--json-metrics-out`** | **B**, снимок для **C** | Машинно читаемые метрики после прогона (сравнение между деплоями). |
| **Анализатор** (`/analyzer`, `trade_effectiveness_analyzer`) | **C** (и визуальная правдоподобность для людей) | CatBoost entry backtest, recovery-сценарии, портфельные поля — см. [TRADE_EFFECTIVENESS_ANALYZER.md](TRADE_EFFECTIVENESS_ANALYZER.md). |
| **`config.env`** флаги `*_ENABLED`, fusion, multiday | **D**, **E** | Включение влияния на карточки/решения только после приемлемой фазы C. |

---

## 3. Сводка по сеткам: цель и фокус калибровки

| Сетка / контур | Скрипт (основной) | Что калибруем в фазе B | Что проверяем в фазе C | Политика (фаза D) |
|----------------|-------------------|-------------------------|-------------------------|-------------------|
| **GAME_5M entry** CatBoost | `train_game5m_catboost.py` | `valid_ratio`, min rows, опц. `scale_pos_weight` | **AUC** hold-out, бэктест анализатора по бакетам `P` vs факт | `GAME_5M_CATBOOST_FUSION`, пороги `HOLD_BELOW_P` — см. [ML_GAME5M_CATBOOST.md](ML_GAME5M_CATBOOST.md), [GAME_5M_CATBOOST_FUSION.md](GAME_5M_CATBOOST_FUSION.md) |
| **Portfolio** CatBoost | `train_portfolio_catboost.py` | `horizon-days`, RMSE-гейт в readiness | RMSE/MAE, top-decile edge vs **bps** — см. [ML_PORTFOLIO_CATBOOST.md](ML_PORTFOLIO_CATBOOST.md) | `PORTFOLIO_CATBOOST_ENABLED`, пороги edge/score в карточках |
| **Recovery** удержание | `train_game5m_recovery_catboost.py` | горизонт метки `h*`, JSONL экспорт | AUC / сценарии в анализаторе — см. [GAME_5M_TIME_EXIT_RECOVERY_PLAN.md](GAME_5M_TIME_EXIT_RECOVERY_PLAN.md) | `GAME_5M_RECOVERY_ML_ENABLED` (исполнение в `game_5m` — отдельное решение) |
| **Event / earnings** forward 5d | `train_event_reaction_catboost.py` | горизонт, версия датасета | RMSE, покрытие `event_reaction_dataset` — см. [EVENT_REACTION_PIPELINE.md](EVENT_REACTION_PIPELINE.md) | Включение в продукт по фичам/отчётам, не обязательно в hot path |
| **Multiday ridge** (дневка, 1–3 дня) | `train_game5m_multiday_lr.py` + онлайн `compute_log_return_multiday_forecast` | λ (сетка в артефакте; один λ в env для онлайн), `period_days`, премаркет on/off | **Walk-forward** по дневным close; не путать с in-sample RMSE в ответе API | Пороги по `multiday_lr_horizon_*`, кворум горизонтов, слияние с 5m-риском — см. [GAME_5M_MULTIDAY_LR_RIDGE.md](GAME_5M_MULTIDAY_LR_RIDGE.md) |

Четыре CatBoost-сетки свёрнуты по таргетам и данным в [TRADE_ML_DATASETS_AND_TARGETS_RU.md](TRADE_ML_DATASETS_AND_TARGETS_RU.md).

---

## 4. Гейты readiness vs фаза C

`run_ml_train_readiness_cron.py` проверяет **последний хвост** обучения (dry-run или full) и пороги вроде `ML_READINESS_GAME5M_AUC_MIN`. Это **быстрый индикатор фазы B**, а не полная замена walk-forward по всем тикерам и режимам рынка.

**Зелёный gate** → имеет смысл планировать **C** (анализатор, ручной бэктест) и только потом **D** (включение влияния на решение).

---

## 5. Multiday ridge и пробел в автоматизации

- В **`ml_train_readiness.jsonl`** multiday **пока не пишется**; метрики собираются через **`--json-metrics-out`** и логи `train_game5m_multiday_lr.py`.
- Рекомендуемый следующий шаг при стабилизации контура: одна строка в том же JSONL или включение снимка в `run_ml_data_quality_report.py` (по аналогии с `--json-metrics-out` других `train_*`).

---

## 6. Переиспользование multiday для портфеля

Математика ridge по дневным рядам **не привязана** к 5m. Для портфеля логично вызывать тот же расчёт с **`use_intraday_features=false`** и завести отдельные env-имена (`PORTFOLIO_*` или общий префикс), чтобы фазы D/E не смешивались с GAME_5M. Калибровка **фаз C/D** при этом та же по смыслу, но выборка фактов — **портфельные** входы и forward-исходы на вашем горизонте.

---

## 7. Ссылки

| Документ | Тема |
|----------|------|
| [ML_DATA_QUALITY_PIPELINE.md](ML_DATA_QUALITY_PIPELINE.md) | Единый отчёт, readiness-крон, включение CatBoost в прод |
| [GAME_5M_MULTIDAY_LR_RIDGE.md](GAME_5M_MULTIDAY_LR_RIDGE.md) | Multiday: данные, скрипт, фазы A–E детально |
| [ML_GAME5M_CATBOOST.md](ML_GAME5M_CATBOOST.md) | Entry 5m CatBoost |
| [ML_PORTFOLIO_CATBOOST.md](ML_PORTFOLIO_CATBOOST.md) | Портфельная регрессия |
| [GAME_5M_WEB_CARDS.md](GAME_5M_WEB_CARDS.md) | Поля карточек, multiday в UI |
