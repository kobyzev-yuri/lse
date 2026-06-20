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
- [ ] 0.2 schema module
- [ ] 0.3 build script + stats JSON
- [ ] 0.4 leak unit tests

### Фаза 1
- [ ] 1.1–1.4 baselines trained, metrics JSON

### Фаза 2
- [ ] 2.1 CNN+LSTM best checkpoint
- [ ] 2.2–2.3 ablation report

### Фаза 3
- [ ] 3.1 analyzer status block
- [ ] 3.4 go/no-go memo

### Фаза 4–5
- [ ] only if go

---

## 12. FAQ

**Это замена Эллиотта?**  
Нет — замена **субъективного** wave count на **обучение с barrier y**. Эллиотт — интуиция; метрика — barrier.

**Почему не net_pnl?**  
Trade y смещён (только входы, которые rules уже пропустили). Barrier — честнее для «стоило входить на этом баре».

**CNN на PNG из Telegram?**  
Нет — tensor из того же OHLC что CatBoost, иначе train/serve drift.

---

*Обновлять при закрытии фаз; go/no-go — строка в [GAME_5M_AGENT_TUNING_LOG.md](GAME_5M_AGENT_TUNING_LOG.md).*
