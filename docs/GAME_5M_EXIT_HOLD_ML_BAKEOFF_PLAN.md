# План: единая оценка выхода/удержания + bake-off подходов (GAME_5M)

**Статус:** research roadmap (2026-06). **Цель:** одна **каноническая метка** «держать на этом баре — хорошо / плохо», несколько **подходов** (tabular / chart / legacy recovery), **сравнение метрик** и выбор победителя перед prod-shadow.

**Не prod** до bake-off sign-off. Не замена `should_close_position` одним классификатором.

**Связанные документы:**

| Тема | Документ |
|------|----------|
| Entry chart bake-off (аналог) | [GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md](GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md), [GAME_5M_CHART_ML_RESEARCH_REPORT.md](GAME_5M_CHART_ML_RESEARCH_REPORT.md) |
| Recovery (существующий) | [GAME_5M_TIME_EXIT_RECOVERY_PLAN.md](GAME_5M_TIME_EXIT_RECOVERY_PLAN.md) |
| Continuation (post-TP, другой вопрос) | [GAME_5M_PREDICTOR_DATASET_PLAN.md](GAME_5M_PREDICTOR_DATASET_PLAN.md) §2 |
| Датасеты / targets | [TRADE_ML_DATASETS_AND_TARGETS_RU.md](TRADE_ML_DATASETS_AND_TARGETS_RU.md) |
| Triple barrier код | `services/game5m_triple_barrier.py` (`recovery_y_label`) |

---

## 1. Проблема: сейчас «выход» размазан по контурам

| Контур | Юнит | Вопрос | y | Статус |
|--------|------|--------|---|--------|
| **recovery_ml** | бар удержания | «не выходить по TIME_EXIT_EARLY?» | `h120_y_recovery` | CatBoost, D4a log_only |
| **continuation_ml** | **закрытая** TAKE | «тейк был ранним?» | `label_missed_upside` | shadow |
| **stuck dataset** | целая сделка | тип траектории | multi-label | offline |
| **rules** `should_close_position` | бар | исполнение | — | prod |
| **chart entry** | бар входа | хороший вход? | `y_entry_good` | research |

**Нет одного ответа:** «на **этом** баре удержания long — держать или закрывать?» с **одной меткой** для всех моделей.

---

## 2. Единая постановка (канон v1)

### 2.1 Юнит наблюдения

**`(trade_id, bar_ts_et)`** — один 5m бар **внутри открытого** GAME_5M long, RTH, пока позиция открыта.

- Якорь решения: **Close** бара `bar_ts_et` (как entry: predict at close).
- **State** (обязателен для exit, нет на entry): `entry_price`, `unrealized_pnl_pct`, `hold_minutes`, `minutes_after_rth_open`.

### 2.2 Каноническая метка `y_hold_good` (primary для bake-off)

**Смысл:** «держать дальше с этого бара — **хорошо**» (не закрывать сейчас).

**Определение (совпадает с recovery C4 + `recovery_y_label` в `game5m_triple_barrier.py`):**

На горизонте **H = 120 min** (настраиваемо, default как `GAME_5M_RECOVERY_ML_HORIZONS_MINUTES`):

- Вперёд по 5m OHLC от `bar_ts` (не включая leak в features).
- `mfe_fwd_pct`, `mae_fwd_pct` от **ref_close** = Close бара решения.
- **`y_hold_good = 1`** iff `mfe_fwd ≥ ε₁` **и** `mae_fwd ≥ ε₂`  
  (default ε₁ = +0.5%, ε₂ = −3.0% — как recovery).

```text
bar t (в позиции) ──● ref_close
         forward H min
              ╱  MFE ≥ ε₁
             ╱
            ╲  MAE не хуже ε₂
             ╲
y_hold_good=1  →  «держать было выгодно»
y_hold_good=0  →  «лучше было закрыть около t»
```

**Связь с действием:**

| P(y_hold_good) высокая | Интерпретация для gate |
|------------------------|-------------------------|
| → | **не закрывать** / отложить exit |
| низкая | закрытие на t **оправдано** |

Это **симметрично entry** по духу (forward path), но метка **другая** (recovery rule, не upper-first barrier).

### 2.3 Альтернативные y (ablation, не primary)

| ID | Метка | Когда сравниваем |
|----|-------|------------------|
| **Y0** | `y_hold_good` (recovery) | **primary bake-off** |
| **Y1** | Symmetric TB: `y=1` iff forward **upper** (ещё +X%) before lower | как entry barrier |
| **Y2** | `y_exit_good = 1 - y_hold_good` | явный «хороший выход сейчас» |
| **Y3** | Counterfactual PnL: hold до t+H vs close at t | регрессия / ranking |

Все подходы в §4 сначала учатся на **Y0**; Y1–Y3 — только если Y0 bake-off неясен.

### 2.4 Что **не** входит в единый hold-bake-off

| Контур | Почему отдельно |
|--------|-----------------|
| **continuation_ml** | юнит = **после** TAKE, не бар удержания |
| **entry chart/bar** | другой вопрос (вход) |
| **oracle ceiling** | post-hoc отчёт, не классификатор |

Continuation остаётся **параллельным** треком «качество тейка»; в bake-off — **secondary metric** (см. §6).

---

## 3. Единый датасет (контракт)

### 3.1 Full context X на баре удержания (entry snapshot + exit influences)

На баре `(trade_id, bar_ts)` классификатор `y_hold_good` должен видеть:

| Слой | Поля | Источник |
|------|------|----------|
| **State** | `pnl_pct`, `hold_minutes`, `minutes_after_rth_open`, `dow`, `hour_et` | OHLC + entry_ts |
| **Entry memory** | `entry_rsi_5m`, `entry_vol_5m_pct`, `entry_momentum_2h_pct`, `entry_kb_news_impact_enc`, `entry_prob_up`, … | `context_json` BUY |
| **Exit tech (t)** | `rsi_5m`, `momentum_2h_pct`, `volatility_5m_pct`, `pullback_from_high_pct` | `compute_5m_features` до t |
| **Exit N+C (t)** | `kb_news_*`, `session_phase_enc`, gaps, `macro_risk_enc` | KB/DB `as_of` t |

**Модуль:** `services/game5m_ml_context_features.py` — `HOLD_BAR_TRAIN_NUMERIC_KEYS` (recovery legacy = subset B1).

**Tracks bake-off:** B1 legacy (recovery only) → **B2 full** (state + entry snapshot + exit tech + N+C).

---

**Builder (новый):** `scripts/build_game5m_hold_bar_dataset.py`  
**Schema:** `services/game5m_hold_bar_dataset.py`

| Поле | Описание |
|------|----------|
| `trade_id`, `ticker`, `bar_ts_et` | ключ строки |
| `entry_ts`, `entry_price` | state |
| `ref_close`, `pnl_pct`, `hold_minutes` | state |
| `exit_signal_at_trade_end` | только для stratified eval, **не в train** |
| `y_hold_good`, `mfe_fwd_pct`, `mae_fwd_pct` | метки |
| `h_minutes` | 120 default |
| Tabular features | как recovery CB + exit-time RSI/momentum/vol |
| `sample_kind` | `hold_bar` / subsampled `near_exit` (опционально) |

**Источники:** `trade_history` (GAME_5M BUY→SELL) + `market_bars_5m` (db).

**Chart NPZ (новый):** `scripts/build_game5m_hold_chart_dataset.py` — окно OHLCV **+ state channels** (pnl_pct, hold_min normalized) → тот же `y_hold_good`, тот же split.

**Объём target:** ≥ **5k** hold-bars (ожидаемо из ~400–800 сделок × несколько баров).

---

## 4. Подходы для bake-off (как у entry)

Все на **одних и тех же строках** и **одном time-split** (по `bar_ts_et`, последние 20% → valid).

| ID | Подход | Модель | Вход | Скрипт (план) |
|----|--------|--------|------|----------------|
| **B0** | Rules only | `should_close_position` | rules + state | replay counterfactual |
| **B1** | Recovery tabular (legacy) | CatBoost | `RECOVERY_CB_FEATURE_NAMES` | существующий `train_game5m_recovery_catboost.py` |
| **B2** | Hold-bar tabular v2 | CatBoost | расширенные exit features | `train_game5m_hold_bar_catboost.py` |
| **B3** | Chart LSTM | LSTM | (T, F) window + state | `train_game5m_hold_chart_lstm.py` |
| **B4** | Chart 2D CNN | CNN | (1, T, F) map | `train_game5m_hold_chart_cnn.py` |
| **B5** | Fusion | CatBoost / MLP | concat(B2 embedding, B4 embedding) | после B2/B4 |
| **B6** | Symmetric TB label (Y1) | B2 или B4 | тот же X, другой y | ablation only |

**Не сравнивать честно:** continuation на другом юните — только **корреляция** с `y_hold_good` на барах перед TAKE.

---

## 5. Протокол сравнения метрик

### 5.1 Offline (обязательно)

| Метрика | Назначение |
|---------|------------|
| **ROC-AUC** на `y_hold_good` (valid) | primary ranking |
| **PR-AUC** | дисбаланс классов |
| **Brier / calibration @0.5** | для порога gate |
| **Balanced accuracy @τ** | ops |
| **Stability** | 3 seeds, min/mean/max AUC |

**Split:** time-ordered 80/20 по `bar_ts_et`; **запрет** random split по ticker без secondary eval.

### 5.2 Policy value (обязательно для выбора «эффективного»)

Классификатор с высоким AUC может не улучшить PnL. Добавить **одинаковый** offline backtest:

На valid-сделках, где фактический exit = `TIME_EXIT_EARLY` (и опционально все exit types):

```text
policy: if P(y_hold_good) ≥ τ → defer exit K bars (counterfactual close)
metric: Δ net_pnl vs factual exit (log-return, costs)
```

Сравнивать **B0–B5** на фиксированной сетке τ ∈ {0.45, 0.5, 0.55, 0.6}.

**Победитель bake-off:** лучший **AUC mean** среди стабильных **и** не хуже rules по policy ΔPnL на valid (или +X bps при том же max DD).

### 5.3 Secondary

| Метрика | |
|---------|--|
| AUC по `exit_signal` strata | TIME_EXIT vs TAKE vs STALE |
| Group valid by ticker | overfit check |
| Correlation continuation `label_missed_upside` vs last hold-bar score | sanity |

---

## 6. Go / no-go (выбор подхода)

| Критерий | Выбрать подход |
|----------|----------------|
| AUC valid mean ≥ **best tabular + 1.5 pp** | chart (B3/B4) в shadow |
| Chart ≈ tabular, policy лучше у tabular | **B2** (проще ops) |
| Legacy B1 ≈ B2 | не строить v2; улучшить features |
| Ни один не beat B0 policy | остаёмся на rules + recovery D4a как есть |
| Fusion B5 beat single best +0.5 pp | fusion candidate |

**Prod path:** только **shadow** `hold_quality_proba` в `context_json` на SELL path → trust arbiter → apply не раньше entry chart go (если оба в stack).

---

## 7. Фазы работ

### Фаза 0 — Schema + unified builder (1 нед)

| # | Задача | Артефакт |
|---|--------|----------|
| 0.1 | `HOLD_BAR_ML_SCHEMA`, `y_hold_good` | `services/game5m_hold_bar_dataset.py` |
| 0.2 | `build_game5m_hold_bar_dataset.py` | CSV + stats JSON |
| 0.3 | Align export с recovery JSONL (subset check) | diff report |
| 0.4 | Leak tests (no forward in X) | unit tests |

### Фаза 1 — Tabular bake-off (1 нед)

| # | Задача |
|---|--------|
| 1.1 | Train **B1** recovery на unified CSV rows |
| 1.2 | Train **B2** hold-bar CatBoost |
| 1.3 | Metrics JSON + stability 3 seeds |
| 1.4 | **B0** rules counterfactual baseline |

### Фаза 2 — Chart bake-off (1–2 нед)

| # | Задача |
|---|--------|
| 2.1 | `build_game5m_hold_chart_dataset.py` (reuse chart entry tensor + state) |
| 2.2 | **B3** LSTM, **B4** CNN (копия entry trainers, hold schema) |
| 2.3 | `run_game5m_hold_chart_stability.py` |
| 2.4 | Сравнение с B2 на **идентичном** valid fold |

### Фаза 3 — Policy + отчёт (1 нед)

| # | Задача |
|---|--------|
| 3.1 | `scripts/eval_game5m_hold_policy_backtest.py` — τ grid, ΔPnL |
| 3.2 | [GAME_5M_EXIT_HOLD_ML_BAKEOFF_REPORT.md](GAME_5M_EXIT_HOLD_ML_BAKEOFF_REPORT.md) |
| 3.3 | Рекомендация: B2 / B3 / B4 / B5 / defer |
| 3.4 | Строка в [GAME_5M_AGENT_TUNING_LOG.md](GAME_5M_AGENT_TUNING_LOG.md) |

### Фаза 4 — Prod shadow (только после 3.3 go)

| # | Задача |
|---|--------|
| 4.1 | `hold_quality_proba` в context при evaluate exit |
| 4.2 | Analyzer block `game5m_hold_quality_model_status` |
| 4.3 | Связка с recovery D4b / continuation gate (не дублировать) |

---

## 8. Локальный workflow (GPU + tunnel)

Как entry ([GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md](GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md) §13):

```bash
conda activate py12
ssh -N -L 5433:127.0.0.1:5432 ai8049520@104.154.205.58
# DATABASE_URL=postgresql://postgres:<pass>@127.0.0.1:5433/lse_trading

python scripts/build_game5m_hold_bar_dataset.py \
  --source db --days 180 \
  --out local/datasets/game5m_hold_bar_dataset.csv \
  --summary-json local/datasets/game5m_hold_bar_stats.json

python scripts/build_game5m_hold_chart_dataset.py \
  --bar-csv local/datasets/game5m_hold_bar_dataset.csv \
  --source db --days 180 \
  --out local/datasets/game5m_hold_chart_v1.npz

python scripts/run_game5m_hold_ml_bakeoff.py \
  --csv local/datasets/game5m_hold_bar_dataset.csv \
  --npz local/datasets/game5m_hold_chart_v1.npz \
  --json-out local/datasets/game5m_hold_bakeoff_results.json
```

---

## 9. Матрица «вход vs выход» (единая логика проекта)

| | Entry (сделано) | Exit/hold (этот план) |
|---|-----------------|------------------------|
| Юнит | `(ticker, bar_ts)` кандидат | `(trade_id, bar_ts)` в позиции |
| Primary y | `y_entry_good` (TB upper) | **`y_hold_good` (recovery forward)** |
| Tabular | bar v2 CatBoost | hold-bar / recovery CatBoost |
| Chart | LSTM/CNN v1 | hold LSTM/CNN |
| Bake-off | [GAME_5M_CHART_ML_RESEARCH_REPORT.md](GAME_5M_CHART_ML_RESEARCH_REPORT.md) | **этот план → REPORT** |
| Prod | shadow pending | shadow после bake-off |

---

## 10. Чеклист (живой)

### Фаза 0
- [x] 0.0 план (этот файл)
- [x] 0.0b Full context X spec (§3.1)
- [x] 0.1 schema module (`game5m_hold_bar_dataset.py`)
- [x] 0.2 hold bar CSV builder (`build_game5m_hold_bar_dataset.py`)
- [ ] 0.3 recovery alignment check
- [ ] 0.4 leak tests

### Фаза 1
- [ ] B1 vs B2 vs B0 metrics

### Фаза 2
- [ ] B3/B4 chart vs B2

### Фаза 3
- [ ] policy backtest + bakeoff report

### Фаза 4
- [ ] only if go

---

## 11. FAQ

**Это заменит recovery и continuation?**  
Нет. **Объединяет метку hold** для bake-off; continuation остаётся для post-TP; recovery CatBoost = кандидат **B1** в том же соревновании.

**Почему не symmetric TB как entry?**  
Можно как **Y1 ablation**; recovery y уже в прод-плане и коде (`recovery_y_label`).

**Один бинарник «выйти/держать»?**  
Да: `y_hold_good` (держать хорошо) или эквивалент `y_exit_good = 1 - y_hold_good`.

---

*Обновлять при закрытии фаз; итог bake-off — [GAME_5M_EXIT_HOLD_ML_BAKEOFF_REPORT.md](GAME_5M_EXIT_HOLD_ML_BAKEOFF_REPORT.md) (создать после Фазы 3).*
