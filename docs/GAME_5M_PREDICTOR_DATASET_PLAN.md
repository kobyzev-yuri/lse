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
| Money management (sizing) | [GAME_5M_MONEY_MANAGEMENT_PLAN.md](GAME_5M_MONEY_MANAGEMENT_PLAN.md) |
| Chart CNN+LSTM (research) | [GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md](GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md) |
| Chart ML test report | [GAME_5M_CHART_ML_RESEARCH_REPORT.md](GAME_5M_CHART_ML_RESEARCH_REPORT.md) |
| Exit/hold unified bake-off | [GAME_5M_EXIT_HOLD_ML_BAKEOFF_PLAN.md](GAME_5M_EXIT_HOLD_ML_BAKEOFF_PLAN.md) |
| Runtime slim / no docs in image | [RUNTIME_SLIM_DEPLOY_PLAN.md](RUNTIME_SLIM_DEPLOY_PLAN.md) |

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

**Явно не делаем в prod roadmap (основной план):** регрессия \( \hat P_{t+1} \), LSTM/Transformer end-to-end в cron, замена `rules_5m` одним классификатором. **Offline research** chart CNN+LSTM — отдельно: [GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md](GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md).

---

## 3. Фаза 0 — Общая инфраструктура меток (1–2 нед.)

Единый модуль, чтобы entry / recovery / continuation не дублировали forward OHLC.

| # | Задача | Артефакт | Критерий готовности |
|---|--------|----------|---------------------|
| 0.1 | **`services/game5m_triple_barrier.py`**: forward scan 5m OHLC, first-touch upper/lower/time, net of round-trip cost bps | unit tests | Согласованность с ручным разбором 3–5 synthetic paths |
| 0.2 | Константы в `config.env.example`: `GAME_5M_TB_UPPER_PCT`, `GAME_5M_TB_LOWER_PCT`, `GAME_5M_TB_MAX_BARS`, `GAME_5M_TB_MAX_MINUTES`, `GAME_5M_TB_COST_BPS` | config | Документированы в этом файле §6 |
| 0.3 | **`GAME_5M_ENTRY_BAR_ML_SCHEMA`** + version в `services/game5m_entry_bar_dataset.py` (или analyzer) | schema dict | Перечень в `ANALYZER_METRIC_DEFINITIONS` / ML glossary |
| 0.4 | Offline **oracle ceiling** (опционально): `% captured` vs best RTH exit — блок analyzer `game5m_oracle_exit_ceiling` | report block | Одна цифра + by exit_signal на 30d ✅ |

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

### Мониторинг (prod)

| Канал | Что смотреть |
|-------|----------------|
| **Web `/sql`** | Типовые SELECT с описанием: разделы *Continuation ML*, *Recovery ML*, *Entry bar v2* — `services/sql_console_presets.py` |
| **Analyzer** | `continuation_ml_live_review` (окно trade_effects), `game5m_continuation_model_status`, `continuation_take_delay_backtest` |
| **Readiness** | `logs/ml_train_readiness.jsonl` → блок `continuation` / `continuation_ml` |
| **Trust digest** | строка `continuation_ml` в unified trust |

Go/no-go для **apply** (`GAME_5M_CONTINUATION_ML_GATE_MODE=apply`): ≥8–15 TAKE с `continuation_ml` в SELL `context_json`, нет массовых `predict_failed`, ops sign-off. **Единый review всех гейтов:** §14 (целевая дата **2026-07-14**).

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
| TAKE сработал, momentum сильный | **continuation_ml** | Продали рано? (defer TAKE if P>τ; **не** если multiday hold apply+bullish) |
| Overnight / multiday | **multiday_lr** | Знак 1–3d forward |
| Open gap | **premarket_gap_baseline** (observable) | Не gap_forecast ML до beat PM |

**3.3 — когда continuation vs recovery:** `recovery_ml` — удержание near flat, выход `TIME_EXIT_EARLY` («будет отскок?»). `continuation_ml` — уже сработал `TAKE_PROFIT` («зря закрыли, дальше росло?»). Не смешивать пороги τ и не defer оба одновременно на одной сделке.

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
- [x] 0.4 oracle ceiling — analyzer `game5m_oracle_exit_ceiling`

### Фаза 1 — entry bar v2
- [x] 1.1 build dataset script
- [x] 1.2–1.3 labels + negatives
- [x] 1.4 train v2
- [x] 1.5 analyzer blocks
- [x] 1.6 log_only telemetry
- [x] 1.7 trust arbiter
- [ ] 1.8 apply sign-off

### Фаза 2 — continuation
- [x] 2.1 schema
- [x] 2.2 train script
- [x] 2.3 analyzer backtest
- [x] 2.4 telemetry (+ prod `CONTINUATION_ML_ENABLED=true` 2026-06-20)
- [x] 2.5 multiday interaction
- [x] 2.6 apply gate (infra; apply после sign-off)
- [x] 2.7 SQL presets + analyzer `continuation_ml_live_review` (`/sql`, `sql_console_presets.py`)

### Фаза 3 — recovery unify
- [x] 3.1 refactor labels (`forward_mfe_mae_pct_window` + export)
- [ ] 3.2 D4b decision (defer: ждём D4a + continuation telemetry)
- [x] 3.3 doc split continuation vs recovery (§10)

### Фаза 4 — multiday enrich
- [ ] см. GAME_5M_MULTIDAY_LR_FEATURE_ENRICHMENT_PLAN.md — **deferred**, не блокирует 1–2

### Фаза 5 — ops
- [x] readiness jsonl (`entry_bar_v2`, `continuation_ml`)
- [x] trust digest lines (bar v2 + continuation_ml)
- [x] ML_STATUS_REPORT таблица (2026-06-20)
- [x] retrain hooks (dispatcher weekly_full)

---

## 13. Где мы сейчас (зафиксировано 2026-06-20)

**Фаза roadmap:** shadow / log_only — **код и infra готовы**, prod apply **не включён**.

| Контур | Код | Prod config | Telemetry в БД | Блокер apply |
|--------|-----|-------------|----------------|--------------|
| Entry bar v2 (1.6–1.7) | ✅ train AUC **0.5495** | shadow log (default `BAR_V2_LOG=true`) | **0** BUY с `catboost_entry_proba_good_v2` | ≥2 нед RTH BUY после деплоя |
| Continuation ML (2.4–2.6) | ✅ train AUC **≈0.735** | `CONTINUATION_ML_ENABLED=true`, `GATE_MODE=log_only` | **0** TAKE с `continuation_ml` | ≥8–15 TAKE + нет mass `predict_failed` |
| Recovery D4a | ✅ | log_only D4a | **23** `recovery_ml_time_exit_early` | D4b — отдельный vote на promotion review |
| Multiday enrich (Ф4) | — | без изменений | — | **deferred**, не входит в promotion review |

**Факты prod БД (2026-06-20):**

| Метрика | Значение |
|---------|----------|
| Последняя сделка (любая) | **2026-06-18** |
| Последний TAKE | **2026-06-12** (до включения telemetry) |
| Последний BUY | **2026-06-18** (до деплоя shadow 20.06) |
| Deploy shadow telemetry | **2026-06-20** (`d319b60`) |
| Следующий RTH (ожидание событий) | **2026-06-23** (пн) |

**Почему SQL «последние TAKE с continuation_ml» пустой:** запрос корректен; фильтр `context_json ? 'continuation_ml'` не находит строк — telemetry пишется **только при новом TAKE после 20.06**. Диагностика: `/sql` → *Сводка ожидания telemetry* или *Последние TAKE (с флагом has_ml)*.

**Чего ждём (P0):** не код, а **рыночные события** — cron на RTH закрывает позиции → первые строки в `context_json`. Без TAKE continuation_ml не появится; без BUY — bar v2 shadow.

---

## 14. Единая точка решения — prod apply всех гейтов

Все контуры выходят из shadow в **одном ops-review**, не по отдельности ad hoc.

### Календарь (ориентир)

| Веха | Дата | Смысл |
|------|------|--------|
| **T0 — старт shadow-окна** | **2026-06-23** | Первый RTH после деплоя; с этого дня считаем telemetry |
| **T+14 — минимум shadow** | **2026-07-07** | 2 календарные недели log_only (checklist §8 п.3) |
| **T+21 — целевой review** | **2026-07-14** | **Promotion review #1** — go/no-go по всем гейтам ниже |
| **T+28 — запасной review** | **2026-07-21** | Если к 14.07 мало TAKE (<8) или мало BUY |

**Целевая дата prod apply (если review пройден):** **2026-07-15 – 2026-07-22** (config flip на VM + deploy, один change window).

### Agenda promotion review #1 (одна встреча / один чеклист)

| # | Gate | Env / действие | Go если |
|---|------|----------------|---------|
| G1 | **Entry bar v2 fusion** (1.8) | `GAME_5M_CATBOOST_DATASET_VERSION=bar` + fusion sign-off | ≥10–15 BUY с v2 telemetry; trust medium+; offline AUC ≥0.545 |
| G2 | **Continuation ML apply** (2.6) | `GAME_5M_CONTINUATION_ML_GATE_MODE=apply` | ≥8–15 TAKE с `continuation_ml`; `status=ok` ≥80%; analyzer backtest не против |
| G3 | **Recovery D4b** (3.2) | recovery live apply (см. recovery plan) | D4a ≥15 строк; `tau_sweep` / whipsaw review; **может быть defer** отдельно от G1–G2 |
| — | Multiday enrich (Ф4) | — | **вне scope** review #1 |

**No-go (любой пункт):** остаёмся log_only, перенос review на **2026-07-21** или +1 нед RTH.

**После apply:** snapshot в `decision_effective`, строка в [GAME_5M_AGENT_TUNING_LOG.md](GAME_5M_AGENT_TUNING_LOG.md), обновить [ML_STATUS_REPORT.md](ML_STATUS_REPORT.md).

### Мониторинг до review

| Когда | Действие |
|-------|----------|
| Ежедневно (RTH) | `/sql` → *Сводка ожидания telemetry*, *Сводка shadow BUY* |
| Еженедельно | analyzer (continuation_ml_live_review, bar v2 status, recovery D4a); readiness jsonl |
| **2026-07-07** | Промежуточный sanity: есть ли ≥5 TAKE + ≥10 BUY с telemetry; иначе сразу сдвиг на 21.07 |
| **2026-07-14** | **Promotion review #1** — решение по G1–G3 |

---

## 15. Handoff технич. (2026-06-20)

| Артефакт | Статус |
|----------|--------|
| Entry bar v2 model | `/app/logs/ml/models/game5m_entry_catboost_v2.cbm`, AUC valid **0.5495** |
| Continuation model | `/app/logs/ml/models/game5m_continuation_catboost.cbm`, AUC valid **≈0.735** |
| SQL мониторинг | `/sql` — presets + диагностика wait dashboard |
| Analyzer | `continuation_ml_live_review`, `game5m_oracle_exit_ceiling` |
| Deploy | `d319b60` на VM |

---

## 16. Backlog (после review #1)

- **3.2** recovery D4b — если не go на 14.07, отдельный mini-review после +2 нед TIME_EXIT_EARLY.
- **Ф4** multiday enrich — параллельный трек, не блокирует G1–G2.
- **Money management (sizing)** — [GAME_5M_MONEY_MANAGEMENT_PLAN.md](GAME_5M_MONEY_MANAGEMENT_PLAN.md): vol + sentiment, shadow → apply.
- **Chart pattern ML (CNN+LSTM)** — research-only [GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md](GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md); prod только если beat bar v2 + 2pp AUC.
- **Exit/hold bake-off** — единая `y_hold_good` + сравнение tabular/chart/rules: [GAME_5M_EXIT_HOLD_ML_BAKEOFF_PLAN.md](GAME_5M_EXIT_HOLD_ML_BAKEOFF_PLAN.md).
- Пересмотр TB-порогов только если weekly retrain снова AUC < 0.545.

**Явно не делать до promotion review:** подмена prod CatBoost v1, отключение rules_5m, apply без sign-off.

---

*Обновлять этот файл при закрытии пунктов; крупные решения (barrier %, promotion) — строка в [GAME_5M_AGENT_TUNING_LOG.md](GAME_5M_AGENT_TUNING_LOG.md).*
