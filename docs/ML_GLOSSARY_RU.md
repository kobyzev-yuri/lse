# ML и decision stack: словарь терминов (RU)

**Назначение:** единая таблица расшифровок английских терминов, сокращений и идентификаторов из кода/JSON, которые встречаются в ML-документации LSE. В тексте других документов допускается краткая форма **«термин (расшифровка)»** со ссылкой сюда.

**См. также:** [TRADING_GLOSSARY.md](TRADING_GLOSSARY.md) (ATR, RSI, backtesting…), [earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md](earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md) §10 (earnings/product-лексика).

---

## Как читать документы с английскими терминами

1. **В скобках** после первого упоминания — русская расшифровка: `readiness (готовность к обучению/продукту)`.
2. **Жирный английский идентификатор** — имя в коде, БД или `config.env`; его **не переводят** при правках конфигов.
3. **Сложные случаи** (якоря BMO/AMH, open-path vs event 5d) — §4 и §5 ниже + примеры в [EVENT_REACTION_PIPELINE.md](EVENT_REACTION_PIPELINE.md), [TRADE_ML_DATASETS_AND_TARGETS_RU.md](TRADE_ML_DATASETS_AND_TARGETS_RU.md) §0.

---

## 1. Архитектура ML: слои и поверхности

| Термин | Расшифровка | Пример / контекст |
|--------|-------------|-------------------|
| **L1 — Retrain** | слой переобучения: «пора ли пересобрать данные и `.cbm`?» | `run_ml_refresh_dispatcher.py`, триггер по Δ строк |
| **L2 — Quality gates** | слой качества: «достаточно ли данных и метрик?» | `ml_train_readiness.jsonl`, пороги AUC/RMSE |
| **L3 — Trading product** | слой продукта: «влияет ли сигнал на сделку?» | `GAME_5M_CATBOOST_ENABLED`, `gate_mode=apply` |
| **readiness** | готовность контура (L2): JSON с `ready=true/false` и причинами | `last_*_readiness.json` |
| **promotion** | перевод контура из shadow/telemetry в исполнение на legacy | portfolio CatBoost ✅; entry CatBoost ❌ |
| **contour** | ML-контур (отдельная задача + cron + артефакт) | `game5m_entry`, `earnings_grid`, `open_path` |
| **dispatcher** | единый планировщик L1 refresh по 8 контурам | `run_ml_refresh_dispatcher.py --slot nightly` |
| **dual-track** | два параллельных пути решения: legacy исполняет, stack пишет snapshot | см. §3 |
| **legacy hot path** | текущий исполнитель сделок (rules + флаги `*_ENABLED`) | `technical_decision_effective` |
| **decision_stack** | параллельный сбор вкладов всех контуров в `decision_snapshot` | `services/decision_stack/` |
| **RESOLVE** | флаг `DECISION_STACK_RESOLVE_ENABLED`: stack становится исполнителем | prod: `false` (только shadow) |
| **shadow** | режим «считаем и логируем, но не блокируем сделку» | earnings grid, open-path classifier |
| **advisory** | подсказка в Brief/UI без автоблока BUY | event regression, spillover ML |
| **gate_mode** | режим влияния: `log_only` / `caution` / `apply` | multiday entry `apply`, hold `log_only` |
| **telemetry** | только метрики и логи, без влияния на сделку | recovery D4a |

---

## 2. ML-метрики и обучение

| Термин | Расшифровка | Когда смотреть |
|--------|-------------|----------------|
| **AUC** (Area Under ROC Curve) | площадь под ROC-кривой; качество бинарного классификатора, 0.5 = случайность | entry CatBoost, recovery |
| **RMSE** (Root Mean Squared Error) | корень из средней квадратичной ошибки регрессии | portfolio, event 5d, spillover |
| **MAE** (Mean Absolute Error) | средняя абсолютная ошибка | gap forecast (pp = процентные пункты) |
| **accuracy** | доля верных классов | scenario classifier, open-path |
| **sign accuracy** | доля совпадения знака (+/−) прогноза и факта | peer spillover regressor |
| **OOS** (Out Of Sample) | вне выборки / walk-forward: метрика на «будущих» днях, не на train | multiday ridge |
| **valid / holdout** | отложенная выборка для метрик (не train) | CatBoost `eval_set` |
| **`.cbm`** | файл модели CatBoost (classifier/regressor) | `game5m_entry_catboost.cbm` |
| **dry-run** | прогон без записи в БД / без train | backfill `--dry-run` |
| **backfill** | дозаполнение данных или признаков задним числом | `backfill_event_reaction_labeling.py` |
| **vol-scaled threshold** | порог метки UP/DOWN масштабируется по волатильности тикера | §4.3 |
| **log-return** | логарифмическая доходность: `ln(P1/P0)` | все forward-исходы в ERD |

---

## 3. Торговые и decision-термины

| Термин | Расшифровка |
|--------|-------------|
| **RTH** (Regular Trading Hours) | основная сессия US (≈ 09:30–16:00 ET) |
| **premarket** | торги до открытия RTH; признаки gap, импульс |
| **BMO** (Before Market Open) | отчёт до открытия рынка в день T |
| **AMH / DURING** | отчёт в ходе сессии (during market hours) |
| **AFTER_CLOSE** | отчёт после закрытия RTH (after hours) |
| **gap / open gap** | разрыв между вчерашним close и **сегодняшним** RTH open (9:30 ET) |
| **gap forecast (ML)** | predict open gap **того же дня** в PRE_MARKET по текущему PM + macro; обучение: PM→open внутри одного `trade_date`; не T+1 |
| **baseline→open** | наивный прогноз open: open gap ≈ текущий `premarket_gap_pct` |
| **effective→open** | рабочий прогноз open по policy `auto` (baseline, пока ML MAE ≥ naive; иначе ML); **не** гейт входа — вход через `premarket_gap_baseline` |
| **fade** | «схлопывание» движения после гэпа (gap up fade) |
| **follow-through** | продолжение движения после события |
| **veto** | жёсткий запрет действия (downgrade BUY → HOLD) |
| **fusion** | объединение rules + ML (напр. CatBoost fusion) |
| **context_json** | JSON сделки с полным audit trail решения |
| **execution_blocked** | флаг «не исполнять автоматически» (earnings Brief) |

---

## 4. Event reaction (`event_reaction_dataset`)

### 4.1 Таблица полей и ролей

| Термин | Расшифровка |
|--------|-------------|
| **ERD** | `event_reaction_dataset` — одна строка = одно earnings-событие |
| **features_before** | JSON признаков **до** реакции рынка (leak-safe якорь) |
| **outcomes_after** | JSON исходов **после** якоря (forward log-ret 1/5/20d) |
| **final_label** | итоговая метка сценария: UP / DOWN / FLAT |
| **anchor / якорь** | торговый день и close, от которого считаются returns |
| **outcome_anchor_date** | дата close, **после** которого начинается forward-окно |
| **peer spillover** | распространение реакции на **аналоги** (peers), не на source |
| **feature_builder_version** | версия формулы признаков (`quotes_regime_v1`, `quotes_regime_earnings_v1`) |
| **dataset_version** | версия состава строк датасета (`v0_expanded_baseline`) |

### 4.2 Якоря по фазе отчёта (source symbol)

Код: `resolve_event_anchors()` в `services/event_reaction_labeling.py`.

| Фаза | Признаки (`features_before`) | Исход (`outcomes_after` от) | Зачем |
|------|------------------------------|----------------------------|-------|
| **BMO / DURING** | close **T−1** | close **T−1** | в daily-баре T ещё нет реакции на отчёт |
| **AFTER_CLOSE (AMH)** | close **T** | close **T** | daily close T — **до** after-hours релиза |

**Пример 1 — NVDA, вторник BMO (до открытия):**

```text
T = Tue 2026-02-25
features_as_of_date = Mon 2026-02-24 close   ← последний бар без реакции на отчёт
forward_log_ret_5d считается от Mon close → +5 торговых дней
```

**Пример 2 — NVDA, вторник AMH (после close):**

```text
T = Tue 2026-02-25 (релиз после 16:00 ET)
features_as_of_date = Tue 2026-02-25 close   ← бар дня T закрылся до AH-релиза
forward_log_ret_5d от Tue close
```

### 4.3 Якорь peer spillover (календарь аналога)

Код: `resolve_peer_outcome_anchor_date()`. Peer реагирует на **следующую** сессию после того, как рынок «увидел» shock source.

**Пример 3 — NVDA BMO во вторник → AMD (peer):**

```text
NVDA отчёт: Tue BMO
AMD торгует реакцию: Tue open (в тот же день)
peer_outcome_anchor_date = Mon close   ← последний close AMD до Tue open
peer_forward_log_ret_5d: от Mon close AMD
```

**Пример 4 — NVDA AMH во вторник → AMD:**

```text
NVDA отчёт: Tue after close
AMD торгует реакцию: Wed open
peer_outcome_anchor_date = Tue close AMD
```

> **Частая ошибка:** считать peer-исход от того же календарного T, что и source BMO — завышает `same_sign_rate` и смешивает pre-event бар peer с post-shock движением.

### 4.4 Vol-scaled порог меток UP/DOWN/FLAT

Конфиг: `EVENT_REACTION_LABEL_THRESHOLD_MODE=vol_scaled` (default), `EVENT_REACTION_LABEL_VOL_K=1.5`.

```text
threshold = max(portfolio_edge, K × vol_10d_log_ret_std)
|forward_log_ret_5d| < threshold  →  FLAT
иначе знак определяет UP / DOWN
```

**Пример 5 — тихий тикер vs волатильный:**

```text
portfolio_edge = 0.004 (≈0.4% в log)
K = 1.5

AAPL: vol_10d = 0.012  →  thr = max(0.004, 0.018) = 0.018
      forward +1.2%     →  FLAT (ниже порога)

SNDK: vol_10d = 0.045  →  thr = max(0.004, 0.0675) = 0.0675
      forward +5%       →  DOWN/UP по знаку (выше порога)
```

Режим `fixed` — только `EVENT_REACTION_LABEL_THRESHOLD_LOG` без масштабирования по vol.

---

## 5. Open-path vs Event 5d (не смешивать)

Два **разных** ML-контура с разными вопросами, горизонтами и правилами утечки данных.

| | **Event 5d** | **Open-path** |
|---|-------------|---------------|
| **Вопрос** | Куда пойдёт цена **через ~5 торговых дней** после earnings? | Как ведёт себя **первый RTH-час** (fade, continuation…)? |
| **Юнит** | 1 строка ERD на событие | `(symbol, trade_date)` на каждый торговый день |
| **Target** | `forward_log_ret_5d`, LLM `final_label` | `target_scenario` (rule после close дня) |
| **Premarket в X** | **Нет** (leak-safe close-якорь) | **Да** (по задумке: gap до open) |
| **Горизонт** | 5–20 дней | минуты–час после open |
| **Prod** | advisory Brief | shadow до `overall_open_path_classifier_ready` |

**Пример 6 — один и тот же день NVDA earnings BMO:**

```text
Event 5d:
  X = признаки на Mon close (без Tue premarket)
  y = log-ret NVDA от Mon close до +5d
  Использование: «через неделю после отчёта — UP/DOWN?»

Open-path (если есть GAME_5M сессия в тот же trade_date):
  X = premarket gap, macro, multiday h1 на Tue pre-open
  y = rule: open_gap_up_fade / continuation / … по OHLC первого часа
  Использование: «как торговать открытие во вторник?»
```

Критика «premarket leakage в event 5d» относится к **неправильному якорю** (T вместо T−1 для BMO), **не** к open-path — там premarket **должен** быть в признаках.

---

## 6. Индексы и macro в technical params

| Термин | Расшифровка |
|--------|-------------|
| **NDX** | индекс Nasdaq-100 (`^NDX`); primary для `ndx_close`, fallback QQQ |
| **SPY gap** | overnight gap индекса S&P 500 в macro/premarket контексте |
| **market_regime_daily** | дневной снимок режима: SPY/QQQ/NDX, VIX, флаги |
| **peer_graph_edge** | ребро графа «тикер → аналог» с весом влияния |

---

## 7. Earnings grid и autoprep

| Термин | Расшифровка |
|--------|-------------|
| **earnings_grid** | контур сценарного классификатора (`quotes_regime_earnings_v1` + LLM labels) |
| **autoprep** | nightly подготовка LLM labels / materials для grid |
| **overall_grid_ready** | L2 gate: grid обучается и метрики в норме |
| **overall_earnings_autoprep_ready** | достаточно LLM labels (≥40) для train |
| **overall_peer_spillover_ready** | spillover dataset + regressor готовы |
| **Fusion** | слияние earnings-сигналов в Brief/UI (не cron BUY) |

---

## 8. Быстрый алфавитный указатель

| EN | RU кратко |
|----|-----------|
| advisory | подсказка без автоблока |
| anchor | якорная дата close для returns |
| AUC | качество классификатора |
| backfill | дозаполнение |
| BMO | отчёт до open |
| AMH / AFTER_CLOSE | отчёт после close сессии |
| CatBoost | градиентный бустинг (`.cbm`) |
| contour | ML-контур |
| dispatcher | планировщик refresh |
| dual-track | legacy + stack параллельно |
| ERD | event_reaction_dataset |
| gate | порог готовности / режим влияния |
| legacy | текущий исполнитель сделок |
| L1/L2/L3 | retrain / quality / product |
| log-return | лог-доходность |
| OOS | вне выборки |
| open-path | сценарий первого часа RTH |
| peer spillover | реакция аналогов на чужой earnings |
| premarket | до открытия RTH |
| readiness | готовность контура |
| RESOLVE | stack как исполнитель |
| RMSE | ошибка регрессии |
| RTH | основная сессия |
| shadow | только лог, без блока |
| spillover | см. peer spillover |
| vol-scaled | порог от волатильности |

---

*Обновлять при добавлении контуров, якорей или gate-флагов. Канон архитектуры: [ML_AND_DECISION_ARCHITECTURE.md](ML_AND_DECISION_ARCHITECTURE.md).*
