# План: развитие предикторов GAME_5M через улучшение датасетов

**Статус:** активный roadmap (2026-06).  
**Не новая игра** — эволюция существующих контуров `catboost_entry_5m`, `recovery_ml`, `multiday_lr` и офлайн `continuation`.

**Связанные документы:**

| Тема | Документ |
|------|----------|
| Контуры, targets, prod-статус | [TRADE_ML_DATASETS_AND_TARGETS_RU.md](TRADE_ML_DATASETS_AND_TARGETS_RU.md) |
| Алгоритм решений, gate modes | [GAME_5M_DECISION_ARCHITECTURE.md](GAME_5M_DECISION_ARCHITECTURE.md) |
| CatBoost входа (текущий y) | [ML_GAME5M_CATBOOST.md](ML_GAME5M_CATBOOST.md) |
| Recovery hold-labels | [GAME_5M_TIME_EXIT_RECOVERY_PLAN.md](GAME_5M_TIME_EXIT_RECOVERY_PLAN.md) |
| Multiday ridge + enrich X | [GAME_5M_MULTIDAY_LR_RIDGE.md](GAME_5M_MULTIDAY_LR_RIDGE.md), [GAME_5M_MULTIDAY_LR_FEATURE_ENRICHMENT_PLAN.md](GAME_5M_MULTIDAY_LR_FEATURE_ENRICHMENT_PLAN.md) |
| L2 gates, retrain | [ML_UNIFIED_RETRAIN_FRAMEWORK.md](ML_UNIFIED_RETRAIN_FRAMEWORK.md), [DECISION_TRUST_ARBITER.md](DECISION_TRUST_ARBITER.md) |
| Сводка ops | [ML_STATUS_REPORT.md](ML_STATUS_REPORT.md) |

---

## 1. Цель и принципы

**Цель:** поднять качество предикторов **без** нового «предсказателя цены» — за счёт честных **юнитов наблюдения**, **path-dependent меток** и снятия **selection bias**.

| Принцип | Содержание |
|---------|------------|
| Log-returns + costs | Метки и контрфакты в % / log-ret; round-trip commission в barrier-порогах |
| Dual-track | Сначала dataset + shadow/log_only + analyzer; `apply` только после L2/trust порогов |
| Один контракт | Общая схема колонок (`*_ML_SCHEMA`, version) — как у recovery |
| Не oracle min/max | Не обучать на «купи на дне / продай на хаях»; допустим **offline ceiling** для метрик |
| Переиспользовать OHLC | `fetch_5m_ohlc` / `load_bars_5m_for_replay` / кэш анализатора |

**Три кита (из обсуждения):**

1. **Bar-level sampling** — строка = кандидат входа (bar + rule signal), не только закрытая сделка.  
2. **Triple barrier** — метка y по первому касанию upper / lower / time (с ε и H).  
3. **Continuation** — метка «продали рано» + модель на exit-ветке (dataset уже частично есть).

---

## 2. Карта: что улучшаем, что новое

| Компонент | Тип | Контур | Сейчас | После плана |
|-----------|-----|--------|--------|-------------|
| Bar-level builder | **новый pipeline** | питает entry | нет | `game5m_entry_bar_dataset` |
| Triple barrier labels | **новая метка y** | `catboost_entry_5m` | y = net_pnl сделки | y = barrier outcome на bar |
| `train_game5m_catboost.py` | улучшение | entry | closed trades only | v2: bar dataset + старый режим fallback |
| `build_game5m_continuation_dataset.py` | улучшение | exit | CSV, офлайн | + train + analyzer + telemetry |
| Continuation CatBoost | **новая модель** | exit (`continuation_ml`?) | нет | shadow → caution → apply |
| Recovery labels | выравнивание | hold | MFE/MAE (своя схема) | общий `triple_barrier.py` где возможно |
| Oracle ceiling report | **новый offline** | analyzer | нет | «% captured of RTH oracle exit» |
| Multiday enrich X | параллельный трек | `multiday_lr` | daily lags | calendar/news flags (отдельный план) |

**Явно не делаем в этом плане:** регрессия \( \hat P_{t+1} \), LSTM/Transformer end-to-end, замена `rules_5m` одним классификатором.

---

## 3. Фаза 0 — Общая инфраструктура меток (1–2 нед.)

Единый модуль, чтобы entry / recovery / continuation не дублировали forward OHLC.

| # | Задача | Артефакт | Критерий готовности |
|---|--------|----------|---------------------|
| 0.1 | **`services/game5m_triple_barrier.py`**: forward scan 5m OHLC, first-touch upper/lower/time, net of round-trip cost bps | unit tests | Согласованность с ручным разбором 3–5 synthetic paths |
| 0.2 | Константы в `config.env.example`: `GAME_5M_TB_UPPER_PCT`, `GAME_5M_TB_LOWER_PCT`, `GAME_5M_TB_MAX_BARS`, `GAME_5M_TB_MAX_MINUTES`, `GAME_5M_TB_COST_BPS` | config | Документированы в этом файле §6 |
| 0.3 | **`GAME_5M_ENTRY_BAR_ML_SCHEMA`** + version в `services/game5m_entry_bar_dataset.py` (или analyzer) | schema dict | Перечень в `ANALYZER_METRIC_DEFINITIONS` / ML glossary |
| 0.4 | Offline **oracle ceiling** (опционально): `% captured` vs best RTH exit — блок analyzer `game5m_oracle_exit_ceiling` | report block | Одна цифра + by exit_signal на 30d |

---

## 4. Фаза 1 — Bar-level + triple barrier → CatBoost entry v2 (приоритет)

**Проблема:** AUC ~0.52, log_only — модель учится только на **открытых** сделках ([ML_GAME5M_CATBOOST.md](ML_GAME5M_CATBOOST.md) § selection bias).

**Юнит наблюдения:** `(ticker, bar_ts)` где `technical_decision ∈ {BUY, STRONG_BUY}` **или** (опция) каждый RTH bar с полным feature vector для negative sampling.

| # | Задача | Артефакт | Критерий готовности |
|---|--------|----------|---------------------|
| 1.1 | **`scripts/build_game5m_entry_bar_dataset.py`**: universe tickers × период; features = `compute_5m_features` + snapshot поля; фильтр RTH | CSV/JSONL | ≥5k rows / 90d на 15+ тикерах |
| 1.2 | Метка **`tb_label`** ∈ {upper, lower, time, insufficient_data}; **`y_entry_good`** = 1 если upper первым (или upper до lower при настраиваемой политике) | columns | Баланс классов в отчёте builder |
| 1.3 | Negative rows: HOLD bars (subsample) или rejected BUY (session/macro) с тем же TB | config `GAME_5M_ENTRY_BAR_NEG_RATIO` | Нет только «успешных» входов |
| 1.4 | **`train_game5m_catboost.py --dataset bar`**: time-split; метрики AUC, calibration bucket; артефакт `game5m_entry_catboost_v2.cbm` | .cbm + meta | **AUC valid ≥ 0.545** (promotion gate; было 0.55), n_valid ≥ 80 |
| 1.5 | Analyzer: **`game5m_entry_bar_dataset_stats`**, **`game5m_entry_model_v2_status`** | API /analyzer | Объём, balance, AUC рядом с v1 |
| 1.6 | Runtime: **`catboost_5m_signal.py`** — флаг `GAME_5M_CATBOOST_DATASET_VERSION=bar|trade`; snapshot `catboost_entry_proba_good_v2` | log_only | 2 нед telemetry без apply |
| 1.7 | Trust arbiter: contour `catboost_entry_5m` обновляет n_min / T_hit из bar-valid | digest | Пороги из [DECISION_TRUST_ARBITER.md](DECISION_TRUST_ARBITER.md) §3 |
| 1.8 | Promotion: `GAME_5M_CATBOOST_ENABLED` + fusion только после sign-off | apply | Δ log-ret OOS на 20+ новых сделках vs v1/off |

**Cron (целевой):** weekly build dataset (вс 05:30 UTC после WF) + nightly retrain через `run_ml_refresh_dispatcher` slot `weekly_full` / отдельный hook.

---

## 5. Фаза 2 — Continuation ML (exit, быстрый win)

**База:** [scripts/build_game5m_continuation_dataset.py](../scripts/build_game5m_continuation_dataset.py) — TAKE_PROFIT / TAKE_PROFIT_SUSPEND, label missed upside.

| # | Задача | Артефакт | Критерий готовности |
|---|--------|----------|---------------------|
| 2.1 | Зафиксировать schema **`CONTINUATION_ML_SCHEMA`**, порог `min_extra_upside_pct` | service module | Version bump + tests ✅ |
| 2.2 | **`scripts/train_game5m_continuation_catboost.py`**: y = `label_missed_upside` (или graded) | .cbm | AUC valid ≥ 0.55, n ≥ 50 TAKE rows |
| 2.3 | Analyzer: **`game5m_continuation_model_status`**, backtest «delay TAKE на K bars if P > τ» | report | Контрфакт Δ log-ret без комиссий + note ✅ |
| 2.4 | Telemetry в `context_json` при TAKE: `continuation_ml` (log_only) | cron | Аналог recovery D4a ✅ |
| 2.5 | Согласование с **bullish multiday hold** — не defer TAKE если multiday gate bullish + hold apply | `game_5m.py` | Integration test ✅ |
| 2.6 | Promotion: gate `GAME_5M_CONTINUATION_ML_GATE_MODE` | apply | Ops sign-off после 2 нед log ✅ (infra) |

**Контур id (предложение):** `continuation_ml` в decision_snapshot; не смешивать с `recovery_ml`.

---

## 6. Фаза 3 — Recovery ↔ общий barrier (опционально, после 1–2)

Recovery уже path-dependent ([GAME_5M_TIME_EXIT_RECOVERY_PLAN.md](GAME_5M_TIME_EXIT_RECOVERY_PLAN.md) C4). Задача — **не переписать prod**, а унифицировать код меток.

| # | Задача | Критерий |
|---|--------|----------|
| 3.1 | Refactor: recovery forward MFE/MAE через `game5m_triple_barrier` (special case: asymmetric ε) | JSONL export bit-identical ±ε |
| 3.2 | D4b recovery apply — **отдельное** ops-решение после накопления telemetry | Checklist из recovery plan |
| 3.3 | Документ: когда continuation vs recovery (TAKE vs TIME_EXIT_EARLY) | §7 этого файла |

---

## 7. Фаза 4 — Multiday ridge: enrich X (параллельно, не блокирует 1–2)

Отдельный детальный план: [GAME_5M_MULTIDAY_LR_FEATURE_ENRICHMENT_PLAN.md](GAME_5M_MULTIDAY_LR_FEATURE_ENRICHMENT_PLAN.md).

| # | Задача | Критерий |
|---|--------|----------|
| 4.1 | DDL + ingest daily flags (earnings/macro) | as-of close без leak |
| 4.2 | Переобучение WF + `last_multiday_wf_game5m.json` | sign 1d OOS ≥ 58% или verdict ready stable 2 runs |
| 4.3 | Не смешивать с bar-level entry dataset | разные юниты |

---

## 8. Фаза 5 — Наблюдаемость и promotion (сквозная)

| # | Задача |
|---|--------|
| 5.1 | `ml_train_readiness.jsonl` — строки для `entry_bar_v2`, `continuation_ml` |
| 5.2 | Unified trust digest — human lines для новых контуров (как [DECISION_TRUST_ARBITER.md](DECISION_TRUST_ARBITER.md)) |
| 5.3 | [ML_STATUS_REPORT.md](ML_STATUS_REPORT.md) — обновление таблицы контуров после каждой фазы |
| 5.4 | `ML_*_RETRAIN_MIN_NEW_UNITS` в [ML_UNIFIED_RETRAIN_FRAMEWORK.md](ML_UNIFIED_RETRAIN_FRAMEWORK.md) |

**Promotion checklist (любой контур):**

1. Dataset stats в analyzer (n, balance, leak audit).  
2. Time-split valid ≥ порога L2.  
3. ≥2 нед `log_only` telemetry в prod.  
4. Trust label medium+ и ops sign-off.  
5. `apply` на legacy + snapshot в `decision_effective`.

---

## 9. Параметры triple barrier (дефолты для согласования)

```env
# Entry bar labeling (Фаза 1)
GAME_5M_TB_UPPER_PCT=1.0          # take barrier, % от close bar t
GAME_5M_TB_LOWER_PCT=1.0          # stop barrier, % (positive number)
GAME_5M_TB_MAX_BARS=24            # ~2h on 5m
GAME_5M_TB_MAX_MINUTES=120        # alternative cap; min(bars, minutes)
GAME_5M_TB_COST_BPS=20            # round-trip drag added to barriers
GAME_5M_ENTRY_BAR_NEG_RATIO=0.25  # HOLD subsample vs BUY rows
GAME_5M_ENTRY_BAR_MIN_ROWS=5000   # builder warning threshold

# Continuation (Фаза 2) — reuse existing continuation script thresholds
# Recovery (Фаза 3) — keep GAME_5M_RECOVERY_ML_* as today
```

Первый prod-probe: **symmetric 1% / 2h** — согласовано с типичным take/min hold; калибровка через analyzer proposals (как STALE/EARLY_DERISK).

---

## 10. Разведение контуров (когда что применять)

| Ситуация | Контур | Вопрос модели |
|----------|--------|----------------|
| Bar с BUY до входа | **entry bar + TB** | Стоит ли входить сейчас? |
| Удержание, near flat, TIME_EXIT? | **recovery_ml** | Будет отскок в H? |
| TAKE сработал, momentum сильный | **continuation_ml** | Продали рано? |
| Overnight / multiday | **multiday_lr** | Знак 1–3d forward |
| Open gap | **premarket_gap_baseline** (observable) | Не gap_forecast ML до beat PM |

---

## 11. Порядок работ (спринты)

```text
Sprint 1 (Ф0): game5m_triple_barrier.py + tests + config
Sprint 2 (Ф1.1–1.3): build_game5m_entry_bar_dataset.py + analyzer stats
Sprint 3 (Ф1.4–1.6): train v2 + log_only telemetry
Sprint 4 (Ф2.1–2.4): continuation train + telemetry
Sprint 5 (Ф1.8 / Ф2.6): promotion review + trust gates
Параллельно: Ф4 multiday enrich по отдельному плану
```

**Definition of Done для всего плана:** `catboost_entry_5m` valid AUC ≥ 0.545 (bar v2 promotion gate) и trust medium+; `continuation_ml` в shadow с понятным backtest; recovery D4b решение задокументировано; multiday enrich в WF или явно deferred с rationale.

---

## 12. Чеклист (живой)

### Фаза 0
- [x] 0.1 `services/game5m_triple_barrier.py`
- [x] 0.2 config.env.example
- [x] 0.3 ENTRY_BAR schema
- [ ] 0.4 oracle ceiling (optional)

### Фаза 1 — entry bar v2
- [x] 1.1 build dataset script
- [x] 1.2–1.3 labels + negatives
- [x] 1.4 train v2
- [x] 1.5 analyzer blocks
- [x] 1.6 log_only telemetry
- [x] 1.7 trust arbiter
- [ ] 1.8 apply sign-off

### Фаза 2 — continuation
- [ ] 2.1 schema
- [ ] 2.2 train script
- [ ] 2.3 analyzer backtest
- [ ] 2.4 telemetry
- [ ] 2.5 multiday interaction
- [ ] 2.6 apply gate

### Фаза 3 — recovery unify
- [ ] 3.1 refactor labels
- [ ] 3.2 D4b decision
- [ ] 3.3 doc split continuation vs recovery

### Фаза 4 — multiday enrich
- [ ] см. GAME_5M_MULTIDAY_LR_FEATURE_ENRICHMENT_PLAN.md

### Фаза 5 — ops
- [ ] readiness jsonl + ML_STATUS_REPORT + retrain hooks

---

## 13. Handoff — сессия 2026-06-19 (prod)

**Сделано сегодня (фаза 1, shadow):**

| Артефакт | Статус |
|----------|--------|
| `game5m_triple_barrier.py` + tests | ✅ |
| `build_game5m_entry_bar_dataset.py` | ✅ prod: 9625 rows / 8 tickers / 90d |
| `train_game5m_catboost.py --dataset bar` | ✅ AUC(valid) **0.5495**, n_valid=1925 |
| Analyzer `game5m_entry_bar_dataset_stats`, `game5m_entry_model_v2_status` | ✅ |
| log_only `catboost_entry_proba_good_v2` в cron | ✅ prod v1 без изменений |
| Commits | `1922890`, `2a38338` |

**Prod paths:** `/app/logs/ml/datasets/game5m_entry_bar_dataset.csv`, `…_stats.json`, `…_v2_train.json`, `/app/logs/ml/models/game5m_entry_catboost_v2.cbm`.

**Блокер promotion (1.8):** AUC valid **< 0.55** + нужны **~2 нед** telemetry в `context_json` (v2 уже пишется). До этого **не** трогаем fusion / `GAME_5M_CATBOOST_ENABLED` под bar.

---

## 14. План на следующую сессию (приоритет)

### P0 — не ждём 0.55

1. ~~**1.7 trust arbiter**~~ — done: `catboost_entry_bar_v2` в LSE Trust digest + `entry_bar_v2` в ml_train_readiness.jsonl.
2. ~~**Фаза 5 (частично)**~~ — weekly hook: `run_game5m_entry_bar_v2_ml_refresh.py` в `weekly_full` dispatcher.

### P1 — параллельный трек (тоже без apply)

3. **Фаза 2.1–2.2** — `CONTINUATION_ML_SCHEMA` + `train_game5m_continuation_catboost.py` (офлайн).
4. **Фаза 2.3** — analyzer `game5m_continuation_model_status` (shadow).

### P2 — опционально

5. **0.4** oracle ceiling (`game5m_oracle_exit_ceiling`) — offline % captured vs RTH oracle.
6. Пересмотр TB-порогов / neg_ratio только если после weekly retrain AUC снова < 0.55.

### Явно не делать до sign-off

- **1.8 apply** — fusion, переключение dataset version, влияние v2 на вход.
- Отключать или подменять prod CatBoost v1 (trade-based).

---

*Обновлять этот файл при закрытии пунктов; крупные решения (barrier %, promotion) — строка в [GAME_5M_AGENT_TUNING_LOG.md](GAME_5M_AGENT_TUNING_LOG.md).*
