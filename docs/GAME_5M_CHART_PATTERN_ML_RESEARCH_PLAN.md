# План (research): Chart-pattern ML — CNN + LSTM для GAME_5M entry

**Статус:** research-only roadmap (2026-06). **Не prod** до явного beat tabular baseline.  
**Связь с Эллиоттом:** не ручная разметка «волн 1–5», а **автоматическое** обучение на паттернах графика с **теми же y**, что у bar v2 (triple barrier) — иначе сравнение бессмысленно.

**Связанные документы:**

| Тема | Документ |
|------|----------|
| Метки y (triple barrier vs net_pnl) | §2 ниже; [GAME_5M_PREDICTOR_DATASET_PLAN.md](GAME_5M_PREDICTOR_DATASET_PLAN.md) |
| Bar v2 baseline (tabular) | [ML_GAME5M_CATBOOST.md](ML_GAME5M_CATBOOST.md), `services/game5m_triple_barrier.py` |
| Prod roadmap (без LSTM end-to-end) | [GAME_5M_PREDICTOR_DATASET_PLAN.md](GAME_5M_PREDICTOR_DATASET_PLAN.md) §2 |
| Triple barrier код | `services/game5m_triple_barrier.py` |
| Builder строк | `scripts/build_game5m_entry_bar_dataset.py` |
| **Отчёт Phase 1–2 (2026-06-21–22)** | [GAME_5M_CHART_ML_RESEARCH_REPORT.md](GAME_5M_CHART_ML_RESEARCH_REPORT.md) |
| **Exit/hold bake-off (план)** | [GAME_5M_EXIT_HOLD_ML_BAKEOFF_PLAN.md](GAME_5M_EXIT_HOLD_ML_BAKEOFF_PLAN.md) |

---

## 1. Зачем отдельный план

Идея: **2D CNN** (форма свечного окна) + **LSTM** (динамика последовательности окон или баров) для классификации **хороший / плохой** кандидат входа — аналог «читаю график как Эллиотт/теханalyst», но метки **объективные**, не субъективный подсчёт волн.

| | Tabular CatBoost v2 (prod shadow) | Chart CNN+LSTM (этот план) |
|---|-----------------------------------|---------------------------|
| Вход | RSI, momentum, vol, … | Tensor / PNG окна OHLC |
| y | `y_entry_good` (barrier) | **тот же y** (обязательно) |
| Prod | shadow telemetry | **только offline до go/no-go** |
| Риск | selection bias (снят bar sampling) | переобучение, leak, мало N |

---

## 2. Как у нас формализованы «хороший / плохой вход» (пояснение)

Два связанных, но **разных** определения — chart-ML должен использовать **barrier y**, не смешивать с prod v1 без явного ablation.

### 2.1 Triple barrier — «хороший вход» для bar v2 (рекомендуемый y для chart-ML)

**Юнит:** `(ticker, bar_ts)` — кандидат на вход (BUY-сигнал или subsampled HOLD), **не** только закрытая сделка.

**Механика** (`triple_barrier_forward` в `services/game5m_triple_barrier.py`):

1. Якорь — **Close** бара решения.  
2. Вперёд по 5m OHLC (до `max_bars` / `max_minutes`, default ~2h).  
3. **Upper** = +(`GAME_5M_TB_UPPER_PCT` + cost bps), **lower** = −(`GAME_5M_TB_LOWER_PCT` + cost bps).  
4. **First touch** High/Low: что коснулись первым — `upper` | `lower` | иначе `time`.  
5. **`y_entry_good = 1` iff `tb_label == upper`** — path-dependent «цена пошла в наш take-barrier раньше stop-barrier».

```text
        upper -------+  (+1% + costs)
              ╱
anchor Close ──●
              ╲
        lower -------+  (−1% + costs)

y=1  →  upper touched first
y=0  →  lower first, или time без upper
```

**Почему это «хороший вход»:** не oracle max/min, а **реалистичный** take/stop горизонт с комиссией; совпадает с философией GAME_5M (короткий RTH swing).

**Negative sampling:** HOLD-бары / rejected BUY — в `build_game5m_entry_bar_dataset.py` (`GAME_5M_ENTRY_BAR_NEG_RATIO`).

### 2.2 net_pnl — «хороший вход» для CatBoost v1 (trade-based)

**Юнит:** только **фактически открытые** BUY → закрытие.

**y:** `1` если `net_pnl > 0` (или `log_return > 0` с `--label log_return_pos`) — см. [ML_GAME5M_CATBOOST.md](ML_GAME5M_CATBOOST.md).

| | Barrier y | net_pnl y |
|---|-----------|-----------|
| Selection | bar + negatives | только сделки |
| Зависит от exit cron | нет (фикс. окно forward) | да (take/stop/time rules) |
| AUC v2 prod train | **0.5495** valid | v1 ~0.52 log_only |
| Для chart-ML | **primary** | optional ablation |

**Вывод для chart-плана:** обучаем CNN+LSTM на **`y_entry_good` / `tb_label`** с тем же builder pipeline; отдельный эксперимент «y = profitable trade» только для сравнения bias.

### 2.3 Эллиотт vs наши метки

| Эллиотт (ручной) | Наш barrier y |
|------------------|---------------|
| «Волна 3», «ABC» — субъективно | Алгоритм first-touch на OHLC |
| Прогноз направления | Бинарный: upper раньше lower |
| Не привязан к costs | `cost_bps` в barrier |

Chart-ML **не обязан** кодировать Elliott rules; он может **выучить** похожие формы (флаги, импульсы), если они коррелируют с `y_entry_good`. Опционально **Фаза 5** — rule-based Elliott features как tabular baseline (не CNN).

---

## 3. Цель research-плана

**Go:** valid AUC (time-split) **≥ bar v2 CatBoost + 2 pp** на том же hold-out, **стабильно на 2+ retrain**, без leak audit failures.

**No-go:** не интегрировать в cron / fusion; зафиксировать rationale в [GAME_5M_AGENT_TUNING_LOG.md](GAME_5M_AGENT_TUNING_LOG.md).

**Не цель:** замена `rules_5m`, end-to-end «предсказатель цены», prod без sign-off.

---

## 4. Архитектуры (кандидаты)

### A. 2D CNN на «картинке» свечей

- Вход: `(C, H, W)` — каналы: OHLC normalized, volume, optional RSI heatmap.  
- H × W = последние N баров × features per bar **или** rendered candlestick RGB (64×128).  
- Backbone: small ResNet / 3–4 conv blocks.  
- Head: binary `P(y_entry_good)`.

### B. LSTM на последовательности баров

- Вход: `(T, F)` — T баров, F = OHLCV + derived (returns, range).  
- 1–2 layer LSTM/GRU → dense.  
- Baseline проще CNN; меньше hyperparams.

### C. CNN + LSTM (комбо)

- CNN encoder per window **или** per-bar → sequence of embeddings → LSTM → head.  
- Либо: multi-scale windows (T1=12, T2=24, T3=48 bars) — fusion layer.

### D. Fusion с tabular (если A/B/C не проходят alone)

- Concat CNN embedding + CatBoost features → small MLP / CatBoost on concat.  
- Prod-путь только если fusion beat v2 на valid.

---

## 5. Dataset (общий контракт)

### 5.1 Full context X (T + news + calendar) — Phase 1.5

Бинарный классификатор на баре решения должен видеть **все влияния, доступные в prod** на этом баре, не только OHLC/RSI.

| Слой | Поля (tabular / broadcast в chart) | Источник |
|------|-------------------------------------|----------|
| **T** | `BAR_TRAIN_NUMERIC_KEYS` | `compute_5m_features` |
| **N** | `kb_news_impact_enc`, `kb_news_sentiment_mean`, `kb_news_count` | KB `as_of` bar close |
| **C** | `session_phase_enc`, `dow_et`, `hour_et`, `ndx_gap_pct`, `spy_gap_pct`, `premarket_gap_pct`, `macro_risk_enc` | bar time + `premarket_daily_features` |
| **+** | `prob_up`, `prob_down`, `llm_sentiment`, `cb_corr_*` | context / entry snapshot (0 если нет) |

**Контракт:** `services/game5m_ml_context_features.py` → `ENTRY_CONTEXT_NUMERIC_KEYS`, `BAR_TRAIN_FULL_NUMERIC_KEYS`.

**Chart:** скаляры **broadcast** на каждый timestep окна `(T, 5+C)` — один NPZ, те же LSTM/CNN trainers.

**Bake-off tracks:**

| Track | X | Скрипт |
|-------|---|--------|
| T | tech only (legacy) | `--no-enrich` |
| T+N+C | full tabular | default builder |
| Chart | OHLCV window | chart NPZ без ctx |
| Chart+ctx | OHLCV + broadcast ctx | chart NPZ с `context_feature_names` |
| Fusion | tab embedding + chart (Phase 2) | отдельный trainer |

**Leak:** KB/calendar только с `ts ≤ bar_ts`; forward bars — только в y.

---

**Источник строк:** тот же CSV что `build_game5m_entry_bar_dataset.py` (ticker, bar_ts, features, `y_entry_good`, `tb_label`).

**Доп. артефакты chart builder** (новый скрипт):

| Поле | Описание |
|------|----------|
| `sample_id` | hash(ticker, bar_ts) |
| `tensor_path` или `npz` | сохранённый tensor |
| `y_entry_good` | из barrier |
| `tb_label` | upper/lower/time |
| `split` | train/valid by **time** (не random ticker) |

**Правила без leak:**

- Окно **заканчивается** на bar_ts decision (включительно или exclusive — зафиксировать v1: **включительно**, predict at close).  
- Normalization: **per-window** z-score или % from window first close — fit только on train stats.  
- RTH filter — как в bar builder.  
- Не использовать future bars в картинке.

**Объём:** target ≥ **8k** labeled windows (у bar builder ~9625 prod); при нехватке — расширить tickers/days.

---

## 6. Фазы работ

### Фаза 0 — Setup (3–5 дней)

| # | Задача | Артефакт |
|---|--------|----------|
| 0.1 | `CHART_ENTRY_ML_SCHEMA` dict + version | `services/game5m_chart_entry_dataset.py` |
| 0.2 | `scripts/build_game5m_chart_entry_dataset.py` | NPZ/PT + stats JSON |
| 0.3 | Time-split protocol doc (match bar v2 train) | в этом файле §7 |
| 0.4 | Leak tests (synthetic shift) | unit tests |

### Фаза 1 — Baselines (1–2 нед)

| # | Модель | Критерий |
|---|--------|----------|
| 1.1 | **Majority / tabular-only** на тех же splits | floor AUC |
| 1.2 | **CatBoost v2** на CSV (уже есть) | **reference AUC ≈ 0.5495** |
| 1.3 | **LSTM-only** (B) | valid AUC, calibration |
| 1.4 | **CNN-only** (A) | valid AUC |

### Фаза 2 — CNN + LSTM (1–2 нед)

| # | Задача | Критерий |
|---|--------|----------|
| 2.1 | Combo (C), hyperparam grid small | best valid AUC |
| 2.2 | Ablation: window 24 vs 48 vs 96 bars | report |
| 2.3 | Ablation: PNG render vs raw OHLC tensor | report |

### Фаза 3 — Evaluation & go/no-go

| # | Задача | Критерий |
|---|--------|----------|
| 3.1 | Analyzer block `game5m_chart_entry_model_status` (offline) | meta.json path |
| 3.2 | Compare vs bar v2 on **same valid fold** | table AUC, PR, calibration buckets |
| 3.3 | Error analysis: tickers, vol regimes, tb_label=time | markdown appendix |
| 3.4 | **Go/no-go memo** | §8 |

### Фаза 4 — Optional prod path (только после go)

| # | Задача |
|---|--------|
| 4.1 | Shadow: `chart_entry_proba_good` in BUY context_json |
| 4.2 | Fusion gate (D) log_only |
| 4.3 | Trust arbiter contour `chart_entry_cnn` |
| 4.4 | **Не раньше** bar v2 apply sign-off (predictor plan §14) |

### Фаза 5 — Elliott explicit (optional research)

| # | Задача |
|---|--------|
| 5.1 | Offline lib или эвристики: swing high/low count, impulse/corrective ratio |
| 5.2 | Tabular features → CatBoost (без CNN) vs Elliott-agnostic |
| 5.3 | Если tabular Elliott features **не** beat v2 — CNN «Elliott» маловероятен; закрыть трек |

---

## 7. Train / valid protocol

Согласовано с bar v2:

- **Split:** time-based последние 20% bar_ts → valid (или фикс. cutoff date из `game5m_entry_bar_v2_train.json`).  
- **Metric:** ROC-AUC на `y_entry_good`; secondary: balanced accuracy, Brier, calibration @ 0.5.  
- **Class weight:** как CatBoost `scale_pos_weight` / neg_ratio.  
- **Seed:** фиксированный; 3 seeds для stability check на go candidate.  
- **Hardware:** CPU ok for small net; GPU on VM optional.

---

## 8. Go / no-go criteria

| | Go (прод shadow) | No-go |
|---|------------------|-------|
| AUC valid | ≥ **bar v2 + 0.02** (≥ ~0.57 при v2=0.5495) | < +0.01 vs v2 |
| Stability | 2 retrain same order | flip flop |
| Leak audit | pass | any future in window |
| n_valid | ≥ 500 | < 200 |
| Ops | memo + tuning log | defer |

**No-go default:** chart-ML остаётся **research artifact**; prod остаётся tabular + rules.

---

## 9. Риски

| Риск | Mitigation |
|------|------------|
| Мало данных | shared builder, augment (time shift ±1 bar только train), не invent labels |
| Overfit tickers | group split by ticker on secondary eval |
| Дублирует tabular | fusion only if additive; else stop |
| «Elliott» hype | objective y = barrier, not wave count |
| Prod complexity | no cron until phase 4 go |

---

## 10. Календарь (ориентир)

| Веха | Срок |
|------|------|
| R0 — этот план | 2026-06 |
| R1 — dataset + LSTM/CNN baseline | +2–3 нед |
| R2 — CNN+LSTM + ablations | +2 нед |
| R3 — go/no-go memo | **2026-08-15** (после bar v2 promotion review 07-14) |
| R4 — shadow (if go) | Q4 2026 |

Chart research **параллелен** MM shadow и ML telemetry; **не блокирует** их.

---

## 11. Чеклист (живой)

### Фаза 0
- [x] 0.1 план (этот файл)
- [x] 0.2 `services/game5m_chart_entry_dataset.py`
- [x] 0.3 `scripts/build_game5m_chart_entry_dataset.py` + stats JSON
- [x] 0.4 leak unit tests (`tests/test_game5m_chart_entry_dataset.py`)
- [x] 0.5 `scripts/train_game5m_chart_entry_lstm.py` (local GPU)

### Фаза 1.5 — Full context enrichment
- [x] 1.5.0 spec §5.1 + `game5m_ml_context_features.py`
- [x] 1.5.1 bar CSV enrich columns (default on)
- [x] 1.5.2 chart NPZ broadcast ctx channels
- [x] 1.5.3 AUC compare T vs T+N+C vs Chart+ctx → `local/datasets/game5m_entry_bakeoff_phase15.json`, `run_game5m_tabular_ablation.py`

### Фаза 1
- [x] 1.1 smoke baselines (smoke CSV, yfinance — sanity only)
- [x] 1.2 CatBoost tech-only (`auc_valid≈0.577` E0) / **E3 full TNC `0.610`**
- [x] 1.3 LSTM OHLCV (`auc_valid≈0.616` seed42; stability mean **0.612**)
- [x] 1.4 CNN OHLCV (`auc_valid≈0.599` seed42; stability mean **0.614**)
- [x] 1.5 stability 3 seeds LSTM/CNN → `local/datasets/game5m_chart_entry_stability.json`

### Фаза 2
- [x] 2.1 **Fusion** LSTM(OHLCV) + tabular sidecar (E2/E3), residual + concat → `game5m_chart_entry_fusion_bakeoff.json`
- [ ] 2.1b end-to-end CNN+LSTM combo (§4.C) — **не делали**
- [ ] 2.2 window ablation 24/48/96 bars
- [ ] 2.3 PNG render vs raw tensor

### Фаза 3
- [x] 3.4 interim report → [GAME_5M_CHART_ML_RESEARCH_REPORT.md](GAME_5M_CHART_ML_RESEARCH_REPORT.md) (v2 ниже)
- [ ] 3.1 analyzer block `game5m_chart_entry_model_status`
- [ ] 3.2 calibration / Brier / ticker-group valid
- [ ] 3.4 **final go/no-go memo** (deadline ~2026-08-15)

### Фаза 4–5
- [ ] chart prod shadow — **no-go interim** (§14)
- [x] **tabular E3 entry shadow** в prod (`catboost_entry_proba_good_e3`) — см. predictor plan §13
- [x] **tabular H3 hold shadow** в prod (`hold_quality_ml`) — exit bake-off track B3
- [ ] Elliott tabular features (Ф5)

---

## 12. Local-first workflow (GPU + SSH tunnel)

**Терминал 1 — tunnel к prod Postgres:**

```bash
ssh -N -L 5433:127.0.0.1:5432 ai8049520@104.154.205.58
```

**Терминал 2 — локальный venv, `config.env` с tunnel URL:**

```bash
# DATABASE_URL=postgresql://postgres:<pass>@127.0.0.1:5433/lse_trading
pip install torch  # CUDA: см. https://pytorch.org
pip install -r requirements.txt -r requirements-catboost.txt
```

**Шаг 1 — bar CSV + enrich (канон E3):**

```bash
python scripts/build_game5m_entry_bar_dataset.py \
  --source db --days 90 \
  --out local/datasets/game5m_entry_bar_dataset.csv \
  --summary-json local/datasets/game5m_entry_bar_stats.json

python scripts/enrich_game5m_entry_bar_csv.py \
  --in local/datasets/game5m_entry_bar_dataset.csv \
  --out local/datasets/game5m_entry_bar_full.csv
```

**Шаг 1b — hold CSV (канон H3):**

```bash
python scripts/build_game5m_hold_bar_dataset.py \
  --source db --days 90 \
  --out local/datasets/game5m_hold_bar_dataset.csv
```

**Шаг 2 — chart NPZ (OHLCV-only recommended):**

```bash
python scripts/build_game5m_chart_entry_dataset.py \
  --bar-csv local/datasets/game5m_entry_bar_full.csv \
  --source db --days 90 \
  --out local/datasets/game5m_chart_entry_v1.npz \
  --summary-json local/datasets/game5m_chart_entry_v1_stats.json
```

**Шаг 3 — LSTM / CNN baseline:**

```bash
python scripts/train_game5m_chart_entry_lstm.py \
  --npz local/datasets/game5m_chart_entry_v1.npz \
  --json-metrics-out local/datasets/game5m_chart_entry_lstm_metrics.json
```

**Шаг 4 — tabular ablation + prod shadow train:**

```bash
python scripts/run_game5m_tabular_ablation.py \
  --entry-csv local/datasets/game5m_entry_bar_full.csv \
  --hold-csv local/datasets/game5m_hold_bar_dataset.csv

python scripts/train_game5m_prod_shadow_models.py

python scripts/run_game5m_ml_stability.py
```

**Шаг 5 — fusion bake-off (optional):**

```bash
python scripts/build_game5m_chart_entry_dataset.py \
  --bar-csv local/datasets/game5m_entry_bar_full.csv \
  --fusion-tab e3 --source db --days 90 \
  --out local/datasets/game5m_chart_entry_fusion_e3.npz

python scripts/train_game5m_chart_entry_fusion.py \
  --npz local/datasets/game5m_chart_entry_fusion_e3.npz \
  --fusion-tab e3 --fusion-mode residual --freeze-lstm
```

Smoke без tunnel (yfinance): `--source yfinance --days 30`. Артефакты в `local/datasets/` — **не в git**.

---

## 14. Выводы и статус (2026-06-22)

### 14.1 Сводка bake-off entry (один CSV, time-split 80/20)

Канон: `local/datasets/game5m_entry_bar_full.csv` — **11 336** строк, valid **2 267**, `y_entry_good` **34%**, 6 tickers, 90d db.

| Track | Модель | AUC valid | Δ vs E3 (0.610) | Stability (3 seeds) |
|-------|--------|-----------|-----------------|---------------------|
| E0 | CatBoost T only | 0.577 | −3.3 pp | — |
| E2 | CatBoost T+time+KB | 0.589 | −2.1 pp | — |
| **E3** | **CatBoost full T+N+C** | **0.610** | — | mean **0.609**, σ **0.004** ✅ |
| Chart | LSTM OHLCV (48 bars) | **0.616** | +0.6 pp | mean **0.612** |
| Chart | CNN OHLCV | 0.599–0.631 | +0.4 pp peak | mean **0.614** |
| Chart | LSTM + broadcast ctx | 0.523 | **−8.7 pp** | ❌ avoid |
| Fusion | E3 residual + frozen LSTM | **0.620** peak | +1.0 pp peak | mean **0.607**, σ **0.009** |
| Fusion | E2 / concat scratch | ≤0.603 | ≤−0.7 pp | хуже LSTM-only |

**Ключевые выводы:**

1. **Контекст N+C на tabular даёт +3.3 pp** (E0→E3); KB alone слаб (+0.03 pp E1→E2), NC layer — основной вклад.
2. **Chart OHLCV ≈ tabular E3** offline; LSTM чуть лучше peak, CatBoost E3 **стабильнее** (σ 0.004 vs 0.009 fusion).
3. **Broadcast ctx на timesteps вредит** chart-моделям (overfit / scale) — контекст только через **fusion sidecar** или tabular CatBoost.
4. **Fusion E3 residual** — единственный chart-путь с additive gain (+1 pp peak), но **не beat E3 стабильно** на mean.
5. **Interim prod path:** **tabular E3/H3 CatBoost shadow** (не chart cron) — уже в `decision_stack` + карточки.

### 14.2 Hold (exit bake-off, tabular)

Канон: `local/datasets/game5m_hold_bar_dataset.csv` — **4 278** баров удержания, valid **855**, `y_hold_good` **67%**.

| Track | AUC valid | Δ cumulative |
|-------|-----------|--------------|
| H0 state | 0.596 | — |
| H2 + exit tech | 0.616 | +2.0 pp |
| **H3 full T+N+C** | **0.623** | +2.7 pp |
| H recovery B1 | 0.611 | legacy baseline |

Stability H3: mean **0.593**, σ **0.022** — **нестабильнее entry**; prod train **0.596**.

Chart hold (LSTM на окне удержания) — **не делали**; приоритет tabular H3 shadow.

### 14.3 Go / no-go chart-ML (interim, 2026-06-22)

| Критерий §8 | Chart LSTM | Fusion E3 | Tabular E3 |
|-------------|------------|-----------|------------|
| AUC ≥ ref+2 pp (≥0.57) | ✅ | ✅ peak | ✅ |
| Стабильность 3 seeds | ✅ | ⚠️ mean < E3 | ✅ |
| Beat E3 **устойчиво** | ❌ (+0.6 pp mean) | ❌ | — |
| Prod shadow | ❌ research | ❌ | ✅ **deployed** |
| **Verdict** | research continue | research continue | **prod shadow** |

**Формальный go chart → prod shadow:** **no-go** до стабильного beat E3 ≥2 pp **и** calibration audit. **Tabular E3/H3** — shadow telemetry (T0 **2026-06-23**).

### 14.4 Единая точка решения — насколько продвинулись

```text
                    ┌─────────────────────────────────────┐
  ENTRY (bar)       │  decision_stack / game5m_policy      │
                    │  rules → core decision               │
                    │  ├─ catboost v1 (APPLY fusion)       │
                    │  ├─ bar v2 shadow (log_only)         │
                    │  └─ entry E3 shadow (log_only) ✅ NEW │
                    └─────────────────────────────────────┘
                                      │
                    ┌─────────────────▼───────────────────┐
  HOLD (exit bar)   │  send_sndk_signal_cron / should_close│
                    │  rules → exit signal                 │
                    │  ├─ recovery_ml (log_only)           │
                    │  ├─ continuation_ml (log_only)       │
                    │  ├─ multiday hold gate (log_only)    │
                    │  └─ hold H3 shadow (log_only) ✅ NEW │
                    └─────────────────────────────────────┘
```

| Аспект | Было (06-20) | Сейчас (06-22) | Ещё не сделано |
|--------|--------------|----------------|----------------|
| Общая метка entry | `y_entry_good` barrier | ✅ + enrich T+N+C | — |
| Общая метка hold | разрозненно | ✅ `y_hold_good` H0–H3 | chart hold |
| Один feature layer | частично | ✅ `game5m_ml_context_features.py` entry+hold | gap predictor в X |
| Offline bake-off | bar v2 only | ✅ entry ablation + chart + fusion + hold | chart hold |
| Prod shadow entry | bar v2 | ✅ **E3 full** + UI cards | chart, apply |
| Prod shadow hold | recovery | ✅ **H3 full** + UI при open pos | apply |
| Trust arbiter | v2 spec | spec H3; **E3/H3 не в game_contours[]** | promotion G4? |
| Apply / effective | v1 CatBoost | без изменений | E3/H3 apply после review ~07-14 |

**Итог:** к **единой точке** продвинулись **архитектурно** (общие y, общий context module, shadow в stack/cron, карточки). **Исполнение** по-прежнему rules + v1 CatBoost; ML entry/hold — **telemetry**, не арбитры. Chart — **offline**, не в runtime.

---

## 15. Канонический датасет и retrain (2026-06-22)

### 15.1 Entry pipeline

| Шаг | Скрипт | Выход | Строки (Jun-22) |
|-----|--------|-------|-----------------|
| 1 | `build_game5m_entry_bar_dataset.py --source db --days 90` | `game5m_entry_bar_dataset.csv` | ~11.3k |
| 2 | `enrich_game5m_entry_bar_csv.py` (KB, gaps, macro, corr) | **`game5m_entry_bar_full.csv`** | 11 336 |
| 3a | `train_game5m_catboost.py --dataset bar --feature-mode full` | E3 `.cbm` | AUC **0.610** |
| 3b | `run_game5m_tabular_ablation.py` | E0–E3 JSON | ablation |
| 4 | `build_game5m_chart_entry_dataset.py --bar-csv …/full.csv` | NPZ OHLCV 48×5 | 11 303 |
| 4b | same + `--fusion-tab e3` | fusion NPZ + tab sidecar | 11 303 |
| 5 | `train_game5m_chart_entry_{lstm,cnn,fusion}.py` | `.pt` checkpoints | offline |
| 6 | `run_game5m_ml_stability.py` | stability JSON | 3 seeds |

**Feature contract entry:** `BAR_TRAIN_NUMERIC_KEYS` (8) + `ENTRY_CONTEXT_NUMERIC_KEYS` (20) = **28** cols (E3).  
**y:** `y_entry_good` = triple barrier upper-first (`game5m_triple_barrier.py`).

### 15.2 Hold pipeline

| Шаг | Скрипт | Выход |
|-----|--------|-------|
| 1 | `build_game5m_hold_bar_dataset.py --source db` | `game5m_hold_bar_dataset.csv` (4 278) |
| 2 | `train_game5m_hold_bar_catboost.py --feature-mode full` | H3 `.cbm` |
| 3 | `run_game5m_tabular_ablation.py --hold-only` | H0–H3 JSON |

**Feature contract hold:** `HOLD_STATE_KEYS` + entry snapshot + exit tech + NC = **34** cols (H3).  
**y:** `y_hold_good` = forward MFE/MAE rule (`recovery_y_label`).

### 15.3 Prod shadow models (не chart)

```bash
python scripts/train_game5m_prod_shadow_models.py
# → local/models/game5m_entry_catboost_e3.cbm  (aligned hyperparams: iter=400, scale_pos_weight)
# → local/models/game5m_hold_bar_catboost_h3.cbm
# scp → VM /app/logs/ml/models/  (one-off; code via git deploy)
```

Runtime: `attach_entry_e3_signal()` в `game5m_policy.py`; `build_hold_quality_shadow()` в `send_sndk_signal_cron.py`.

### 15.4 Train protocol (все модели)

- Split: time-ordered, последние **20%** по `bar_ts_et`
- CatBoost: `iterations=400`, `scale_pos_weight`, `use_best_model=True`, seed 42
- Chart: 30 epochs, BCE + pos_weight, best AUC checkpoint; fusion — **freeze pretrained LSTM**
- Stability: seeds **42, 43, 44** → `run_game5m_ml_stability.py`
- Policy τ-grid (offline): `eval_game5m_ml_policy_backtest.py`

---

## 16. Следующие шаги (приоритет)

| P | Задача | Срок |
|---|--------|------|
| P0 | **2 нед shadow telemetry** E3 BUY + H3 SELL в БД; `/sql` presets | T0 23.06 → 07.07 |
| P0 | Promotion review #1: G1 v2, G2 continuation, **+ E3/H3 sign-off?** | ~14.07 |
| P1 | Align H3 prod train с ablation (σ hold); retrain | до review |
| P1 | Policy backtest **на trades** (ΔPnL), не только bar labels | до review |
| P2 | Chart: window 24/48/96 ablation; ticker-group valid | август |
| P2 | Chart hold LSTM vs H3 tabular | Q3 |
| P3 | Fusion prod path — **только если** stable beat E3 | после chart go |
| P3 | Trust arbiter contours `entry_e3` / `hold_h3` в digest | с telemetry |
| defer | Broadcast ctx on chart timesteps | closed (negative result) |
| defer | Chart prod cron / torch on VM | no-go interim |

---

## 17. FAQ

**Это замена Эллиотта?**  
Нет — замена **субъективного** wave count на **обучение с barrier y**. Эллиотт — интуиция; метрика — barrier.

**Почему не net_pnl?**  
Trade y смещён (только входы, которые rules уже пропустили). Barrier — честнее для «стоило входить на этом баре».

**CNN на PNG из Telegram?**  
Нет — tensor из того же OHLC что CatBoost, иначе train/serve drift.

**Локально с GPU и tunnel?**  
Да — рекомендуемый research path (§12). Prod cron не нужен до go/no-go.

---

*Обновлять при закрытии фаз; go/no-go — строка в [GAME_5M_AGENT_TUNING_LOG.md](GAME_5M_AGENT_TUNING_LOG.md).*
