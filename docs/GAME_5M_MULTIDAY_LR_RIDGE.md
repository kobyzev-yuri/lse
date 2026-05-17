# Мультидневный ridge по лог-доходности (GAME_5M): данные, обучение, рантайм и калибровка

Наивная **ridge-регрессия** по **дневным** закрытиям (и опционально признакам **premarket_daily_features** из PostgreSQL) для оценки **суммарной лог-доходности** на горизонтах **1, 2 и 3 торговых дня** вперёд от «текущего» дня ряда. В рантайме 5m к вектору признаков могут добавляться **volatility_5m_pct** и **momentum_2h_pct** (в долях); при офлайн-обучении артефакта эти два столбца в истории заполняются **нулями** (модель учит в основном дневную часть).

Модель **не заменяет** правила входа/выхода и по умолчанию **выключена** (`GAME_5M_MULTIDAY_LR_REG_ENABLED=false`). Доходности в смысле таргета — **log-returns**; при сравнении с торговыми решениями учитывайте **transaction costs** (правила проекта).

См. также: [ML_CALIBRATION_PHASES.md](ML_CALIBRATION_PHASES.md) (единые фазы калибровки по всем ML-сеткам), [GAME_5M_WEB_CARDS.md](GAME_5M_WEB_CARDS.md) (поля карточек и логи), `scripts/ingest_premarket_daily_features.py`. **План обогащения X (новости, календарь):** [GAME_5M_MULTIDAY_LR_FEATURE_ENRICHMENT_PLAN.md](GAME_5M_MULTIDAY_LR_FEATURE_ENRICHMENT_PLAN.md).

---

## 1. Цель и ограничения

| Аспект | Содержание |
|--------|------------|
| **Зачем** | Дополнительный **справочный** сигнал «куда смещена ожидаемая дневная доходность» на несколько торговых дней, в одном ряду с интрадей-прогнозом цены (30/60/120 мин) — **другой горизонт**, не путать с минутами. |
| **Чего не делает** | Не выбирает размер позиции, не подменяет стоп/тейк, не калибрует вероятности в смысле `P(win)` (это регрессия в log-пространстве, не классификатор). |
| **Риски** | Мало дней в ряду, смена режима рынка, утечка если бы подмешивали будущее (в текущей схеме таргеты строятся только вперёд от индекса `i`); **in-sample RMSE** в логах обучения — **не** замена walk-forward. |

---

## 2. Что предсказываем

- Для каждого горизонта \(h \in \{1,2,3\}\) (торговых дня): цель — \(\log(C_{i+h}/C_i)\) по ряду **close** без выходных.
- На выходе в API/карточках: в т.ч. **`predicted_pct_vs_spot`** (перевод из log), **`train_rmse_log`** на обучающей выборке онлайн-ridge, **`bias_summary`** (грубое согласование знаков между горизонтами).

---

## 3. Данные и признаки

| Источник | Назначение |
|----------|------------|
| **Дневные close** | `public.quotes` (при `--source auto|quotes` и достаточной глубине) или Yahoo 1d через `yfinance` (`--source yahoo` / fallback в `auto`). |
| **Премаркет** | Таблица `public.premarket_daily_features` (ingest: `scripts/ingest_premarket_daily_features.py`). Признаки в долях (/100): gap, return, range, gap_vs_daily_volatility. |
| **5m хвост (live)** | Только в `get_decision_5m` при включённом флаге: текущие `volatility_5m_pct`, `momentum_2h_pct` / 100. |

Код: `services/log_return_multiday_forecast.py`, `services/multiday_lr_pipeline.py`.

---

## 4. Версии артефакта (офлайн)

| `artifact_version` | Признаки (порядок весов) |
|---------------------|---------------------------|
| **v1** | 7 дневных + 2 intraday (нули в train) |
| **v2** | 7 дневных + 4 премаркет + 2 intraday |

Офлайн-обучение и сохранение JSON: `scripts/train_game5m_multiday_lr.py` → каталог **`GAME_5M_MULTIDAY_LR_MODEL_DIR`**. На прод-боте по умолчанию (если переменная не задана и существует `/app/logs/ml/models/`) — **`/app/logs/ml/models/multiday_lr/`** (рядом с `.cbm`); локально без этого пути — `local/multiday_lr_models/` от корня репо. Явно задайте `GAME_5M_MULTIDAY_LR_MODEL_DIR` в `config.env`, если нужен другой том.

---

## 5. Рантайм (GAME_5M)

| Переменная | Смысл |
|------------|--------|
| `GAME_5M_MULTIDAY_LR_REG_ENABLED` | Вкл/выкл расчёт в `get_decision_5m` (по умолчанию выкл.). |
| `GAME_5M_MULTIDAY_LR_REG_PERIOD_DAYS` | Глубина запроса к **Yahoo** (дневки), когда ряд берётся не из `quotes`. |
| `GAME_5M_MULTIDAY_LR_REG_RIDGE_LAMBDA` | Фиксированный λ онлайн-ridge (не сетка, в отличие от `fit_artifact_for_ticker`). |
| `GAME_5M_MULTIDAY_LR_REG_MIN_SAMPLES` | Минимум строк обучения. |
| `GAME_5M_MULTIDAY_LR_REG_USE_5M_TAIL` | Подмешивать ли vol/mom в вектор предсказания. |
| `GAME_5M_MULTIDAY_LR_USE_PREMARKET_DB` | Подгрузка премаркета из БД при наличии `db_engine` в вызове. |
| `GAME_5M_MULTIDAY_LR_DAILY_CLOSE_SOURCE` | `auto` \| `quotes` \| `yahoo`: источник дневных close в онлайн-ridge (`compute_log_return_multiday_forecast`). По умолчанию **auto** — при `db_engine` сначала `public.quotes` (как walk-forward анализатора), при нехватке длины ряда для обучения — Yahoo. |
| `GAME_5M_MULTIDAY_LR_REG_APPEND_REASONING` | Добавлять однострочник в `reasoning`. |

Плоские поля для UI/контекста сделки: `multiday_lr_horizon_{1,2,3}d_pct_vs_spot`, `multiday_lr_bias`, и др. (см. `TECHNICAL_SIGNAL_KEYS` в `services/recommend_5m.py`, дамп входа в `services/deal_params_5m.py`).

---

## 6. Обучение (скрипт)

```bash
# Явный список
python scripts/train_game5m_multiday_lr.py SNDK MU --source auto --json-metrics-out /tmp/mlr.json

# Как список игры 5m (GAME_5M_TICKERS или TICKERS_FAST)
python scripts/train_game5m_multiday_lr.py --tickers-source game5m --dry-run

# Все тикеры FAST+MEDIUM+LONG из config.env
python scripts/train_game5m_multiday_lr.py --tickers-source config --json-metrics-out /tmp/mlr.json

# Как команда /tickers в Telegram: DISTINCT quotes ∪ конфиг, затем sort
python scripts/train_game5m_multiday_lr.py --tickers-source merged --source auto
```

В JSON метрик: `training.lambda_grid_cv`, `training.ridge_lambda` (выбранный при подборе λ на holdout), `horizons_metrics`, поле **`tickers_source`**.

---

## 7. Фазы калибровки (multiday ridge)

Ниже — **конкретизация** общей схемы из [ML_CALIBRATION_PHASES.md](ML_CALIBRATION_PHASES.md) для этой компоненты.

### Фаза A — целостность данных

- Достаточно дневных **close** в `quotes` (или осознанный Yahoo).
- При цели v2: строки **`premarket_daily_features`** по тем же тикерам/датам (или осознанный режим без премаркета).

**Провал:** короткий ряд → нет прогноза; премаркет пуст → v1 или нули в признаках.

### Фаза B — стабильность модели (гиперпараметры)

| Параметр | Что смотреть |
|----------|----------------|
| **λ (ridge)** | В офлайн-артефакте — сетка `lambda_grid_cv` и `selected_lambda`; на проде онлайн — один `GAME_5M_MULTIDAY_LR_REG_RIDGE_LAMBDA`: сравнить чувствительность на walk-forward (ручной прогон по датам). |
| **`period_days` / min_train_rows** | Не обрывать ряд слишком рано; не учить на десятках точек. |
| **Премаркет on/off** | Сравнить out-of-sample ошибку / знак v1 vs v2 на одних и тех же окнах. |
| **`holdout_frac`** (только в `fit_artifact_for_ticker`) | Влияет на выбор λ в артефакте, не на торговые горизонты. |

### Фаза C — честная оценка качества прогноза (не in-sample RMSE из лога)

- **Walk-forward:** на каждую дату `t` модель обучается только на прошлом, предсказание на `t`, сравнение с реализованным \(\log(C_{t+h}/C_t)\).
- Метрики: RMSE/MAE в log, **доля верного знака**, при необходимости — **экономический** критерий после **bps** издержек на вашем горизонте удержания.

**Важно:** `train_rmse_log` в ответе API относится к **текущему** fit на истории до последнего дня — это **диагностика переобучения**, не доказательство качества на будущем.

### Фаза D — калибровка политики (решение «учитывать / не учитывать»)

Здесь настраиваются **не веса ridge**, а **пороги и правила слияния** с остальной системой (после того как фаза C приемлема):

| Тип | Примеры |
|-----|--------|
| **Пороги по горизонтам** | `τ_1d`, `τ_2d`, `τ_3d` на `predicted_pct_vs_spot` или на log-score. |
| **Кворум** | Например, «2 из 3 горизонтов > 0» для ослабления/усиления входа. |
| **Совместимость** | Не усиливать long, если риск-метрики 5m / CatBoost / новости в конфликте. |

Пока **`GAME_5M_MULTIDAY_LR_REG_ENABLED=false`**, фаза D в проде **не активна** — можно только копить метрики фазы C офлайн.

### Фаза E — прод и мониторинг

- Включить флаг **только** после зелёной фазы C/D на отложенном окне.
- Логировать предсказания рядом с фактом закрытия/дневным исходом; при деградации — снова выкл. или переобучение/смена λ.

---

## 8. Портфельная игра (перспектива)

Тот же математический объект (`compute_log_return_multiday_forecast` с **`use_intraday_features=false`** и без 5m-контекста) может питать **портфельный** горизонт решений; отдельная интеграция в код портфеля и отдельные env-имена — на усмотрение (см. [ML_CALIBRATION_PHASES.md](ML_CALIBRATION_PHASES.md), раздел «Переиспользование»).

---

## 9. Связанные файлы

| Файл | Роль |
|------|------|
| `services/log_return_multiday_forecast.py` | Онлайн ridge, `compute_log_return_multiday_forecast`. |
| `services/multiday_lr_pipeline.py` | Quotes, премаркет, `build_training_stack`, `fit_artifact_for_ticker`, readiness-хелперы. |
| `scripts/train_game5m_multiday_lr.py` | Обучение JSON, `--tickers-source`, `--json-metrics-out`. |
| `tests/test_log_return_multiday_forecast.py` | Синтетические проверки без сети. |

---

## 10. Анализатор эффективности сделок

В `services/trade_effectiveness_analyzer.py` каждый запуск добавляет в JSON **`multiday_lr_reality_check`** (walk-forward OOS ridge vs дневной факт) и **`ml_production_arbiter`** (сводный вердикт по multiday, CatBoost entry, портфельному CatBoost, recovery; поле **`conclusion_ru`** — текст для финального заключения оператора). См. `docs/TRADE_EFFECTIVENESS_ANALYZER.md`, раздел про эти ключи.
