# Отчёт: Chart-pattern ML — результаты offline-тестирования (2026-06-21)

**Статус:** research / pre-go-no-go. **Prod:** не интегрировано.  
**План:** [GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md](GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md)

---

## 1. Цель эксперимента

Проверить, может ли **chart ML** (окно OHLCV 5m) предсказывать **`y_entry_good`** (triple barrier, как bar v2 CatBoost) лучше tabular baseline на **том же time-valid fold**.

**Go-критерий (план):** AUC valid ≥ bar v2 + **2 pp** (~0.57 при ref 0.5495), стабильно на 2+ retrain.

---

## 2. Окружение

| Параметр | Значение |
|----------|----------|
| Дата | 2026-06-21 |
| Python | conda `py12` 3.12 |
| torch | 2.12.1+cu130 (RTX 5070 Laptop, sm_120) |
| Данные | prod `market_bars_5m` через SSH tunnel `:5433` |
| Метки | `y_entry_good` из `build_game5m_entry_bar_dataset.py` |
| Split | time-ordered, последние **20%** → valid |

---

## 3. Датасет v1

| | |
|---|---|
| bar CSV | 11 336 строк |
| chart NPZ | **11 303** × (48 bars, 5 features) |
| train / valid | 9 043 / **2 260** |
| tickers | ASML, CIEN, LITE, MU, NBIS, SNDK (6) |
| `y_entry_good` rate | 34.0% |
| OHLC source | `--source db` |
| skipped (no window) | 33 |

**Features per bar:** `open/high/low/close_pct_anchor`, `log_volume` (z-scored in-window).  
**Builder:** `scripts/build_game5m_chart_entry_dataset.py`  
**Schema:** `services/game5m_chart_entry_dataset.py`

> **Примечание:** prod cron bar CSV (2026-06-21) — 9 625 строк / 8 тикеров; локальный rebuild через tunnel шире по universe. Для финального go/no-go желательно зафиксировать один канонический CSV (prod nightly).

---

## 4. Baselines и модели

| ID | Модель | Скрипт | Архитектура |
|----|--------|--------|-------------|
| B0 | CatBoost v2 | `train_game5m_catboost.py --dataset bar` | tabular, `BAR_TRAIN_NUMERIC_KEYS` |
| B1 | LSTM | `train_game5m_chart_entry_lstm.py` | 1×LSTM(64), last hidden → logit |
| B2 | 2D CNN | `train_game5m_chart_entry_cnn.py` | 3×Conv2d on (1×48×5), AdaptiveAvgPool |

Обучение: 30 epochs, Adam lr=1e-3, `BCEWithLogitsLoss` + `pos_weight`, best AUC checkpoint.

---

## 5. Результаты AUC (valid)

### 5.1 Single run (seed=42)

| Модель | AUC valid | Δ vs CatBoost |
|--------|-----------|---------------|
| CatBoost v2 | **0.5648** | — |
| LSTM | **0.5987** | +3.4 pp |
| CNN | **0.5985** | +3.4 pp |

### 5.2 Stability (seeds 42, 43, 44)

Запуск: `scripts/run_game5m_chart_entry_stability.py`

| Модель | min | mean | max | все seeds |
|--------|-----|------|-----|-----------|
| CatBoost v2 | — | **0.5648** | — | (1 run) |
| LSTM | 0.6045 | **0.6124** | 0.6164 | 0.6163, 0.6045, 0.6164 |
| **CNN** | 0.5985 | **0.6138** | **0.6312** | 0.5985, **0.6312**, 0.6117 |

**Лучший прогон:** CNN seed **43** → **0.6312** (+6.6 pp vs CatBoost).

---

## 6. Smoke (не для решений)

| | rows | AUC LSTM | AUC CatBoost |
|---|------|----------|--------------|
| smoke (yfinance, 456 rows) | 456 | ~0.88 | ~0.88 |

Завышено из-за малого N и другого OHLC source — только pipeline sanity.

---

## 7. Leak / quality checks

- [x] Unit tests: `tests/test_game5m_chart_entry_dataset.py` (6 tests — window anchor, no future bars, time split)
- [x] Окно inclusive at decision bar close
- [x] Normalization per-window only
- [ ] Group split by ticker (secondary eval — не делали)
- [ ] Prod-identical bar CSV (8 tickers) — pending

---

## 8. Вывод (interim)

| Критерий | Статус |
|----------|--------|
| AUC ≥ 0.57 (+2 pp vs ~0.55 ref) | **PASS** (все 3 seeds LSTM/CNN) |
| Stability 2+ retrain | **PASS** (3 seeds) |
| n_valid ≥ 500 | **PASS** (2 260) |
| Leak audit | **PASS** (unit) |
| Beat tabular on **prod-identical** CSV | **TBD** |
| CNN+LSTM combo / ablations | **не делали** (Фаза 2) |
| Prod shadow | **не делали** (Фаза 4) |

**Interim verdict:** chart ML (LSTM и особенно **2D CNN**) на локальном db-dataset **выглядит перспективно** vs tabular bar v2. Формальный **go/no-go memo** — после Фазы 2 (combo, window ablation) и прогона на **prod bar CSV**, не раньше bar v2 apply sign-off (predictor plan §14).

**No-go риски:** другой ticker universe vs prod; один период 90d; нет calibration/Brier в отчёте; CNN max seed может быть outlier.

---

## 9. Артефакты (локально, не в git)

| Путь | Описание |
|------|----------|
| `local/datasets/game5m_entry_bar_dataset.csv` | bar v1 CSV |
| `local/datasets/game5m_chart_entry_v1.npz` | chart tensors |
| `local/datasets/game5m_chart_entry_stability.json` | 3-seed summary |
| `local/models/game5m_chart_entry_*_seed*.pt` | checkpoints |
| `local/models/game5m_entry_catboost_v2_v1.cbm` | tabular baseline |

---

## 10. Следующие шаги

1. Rebuild NPZ на **prod bar CSV** (9 625 rows, 8 tickers) — сверка AUC.
2. Фаза 2: CNN+LSTM combo; ablation window 24/48/96.
3. Secondary: valid grouped by ticker; Brier / calibration buckets.
4. Go/no-go memo → [GAME_5M_AGENT_TUNING_LOG.md](GAME_5M_AGENT_TUNING_LOG.md) (deadline ~2026-08-15).

---

## 11. Воспроизведение

```bash
conda activate py12
ssh -N -L 5433:127.0.0.1:5432 ai8049520@104.154.205.58
# DATABASE_URL=postgresql://postgres:<pass>@127.0.0.1:5433/lse_trading

python scripts/build_game5m_chart_entry_dataset.py \
  --build-bar-csv --source db --days 90 \
  --out local/datasets/game5m_chart_entry_v1.npz \
  --summary-json local/datasets/game5m_chart_entry_v1_stats.json

python scripts/run_game5m_chart_entry_stability.py \
  --npz local/datasets/game5m_chart_entry_v1.npz \
  --json-out local/datasets/game5m_chart_entry_stability.json
```

---

*Отчёт v1 — 2026-06-21. Обновлять при новых прогонах.*
