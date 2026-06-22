# Отчёт: Chart-pattern ML + tabular bake-off (2026-06-21 — 2026-06-22)

**Статус:** research / pre-go-no-go для **chart prod**; **tabular E3/H3 shadow** — в prod.  
**План:** [GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md](GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md) §14–16

---

## 1. Цель

Проверить, может ли **chart ML** (окно OHLCV 5m) и **fusion** (LSTM + tabular sidecar) предсказывать **`y_entry_good`** лучше **tabular E3 full T+N+C** на **том же time-valid fold**.

**Go-критерий chart prod (план §8):** AUC valid ≥ tabular ref + **2 pp**, стабильно на 3 seeds, без leak.

---

## 2. Канонический датасет (Jun-22)

### Entry

| | |
|---|---|
| CSV | `game5m_entry_bar_full.csv` — **11 336** rows |
| NPZ chart | **11 303** × (48 bars, 5 OHLCV features) |
| valid | **2 267** (20% time tail) |
| tickers | ASML, CIEN, LITE, MU, NBIS, SNDK |
| `y_entry_good` | 34.0% |
| enrich | KB as_of + gaps/macro/corr via `game5m_ml_context_features.py` |

### Hold (exit bake-off)

| | |
|---|---|
| CSV | `game5m_hold_bar_dataset.csv` — **4 278** hold bars |
| valid | **855** |
| `y_hold_good` | 67.4% |
| y rule | forward MFE≥0.5% & MAE≥−3% @ 120min |

---

## 3. Entry bake-off — AUC valid

### 3.1 Tabular ablation (CatBoost, seed 42)

| Track | Features | AUC | Δ vs E0 |
|-------|----------|-----|---------|
| E0 T | 8 tech | 0.577 | — |
| E1 T+time | 11 | 0.588 | +1.2 pp |
| E2 T+time+KB | 14 | 0.589 | +0.03 pp |
| **E3 full T+N+C** | **28** | **0.610** | **+3.3 pp** |
| E T+NC only | 22 | 0.597 | +2.0 pp |

**Вывод:** NC layer (gaps, macro, corr, prob) — главный вклад; KB marginal после time.

### 3.2 Chart vs tabular (Phase 1 + 1.5)

| Model | AUC valid | Δ vs E3 |
|-------|-----------|---------|
| CatBoost E3 | **0.610** | — |
| LSTM OHLCV | **0.616** | +0.6 pp |
| CNN OHLCV (mean 3 seeds) | **0.614** | +0.4 pp |
| LSTM + broadcast ctx | 0.523 | **−8.7 pp** ❌ |
| CatBoost full (early run) | 0.604 | −0.6 pp |

### 3.3 Fusion Phase 2 (LSTM frozen + tab MLP)

| Mode | Tab sidecar | AUC valid |
|------|-------------|-----------|
| E2 concat scratch | time+KB | 0.576 |
| E2 concat frozen LSTM | time+KB | 0.603 |
| E3 concat scratch | full TNC | 0.565 |
| **E3 residual frozen** | full TNC | **0.620** (best single) |
| E3 residual (3-seed mean) | full TNC | **0.607** |

**Вывод:** fusion помогает **только** с pretrained LSTM + **E3** sidecar + **residual** mode. Mean fusion **ниже** E3 tabular.

### 3.4 Stability (3 seeds)

| Model | mean AUC | min–max | σ |
|-------|----------|---------|---|
| CatBoost E3 | **0.609** | 0.603–0.613 | **0.004** |
| Fusion E3 residual | 0.607 | 0.595–0.615 | 0.009 |
| LSTM OHLCV (Phase 1) | 0.612 | 0.604–0.616 | ~0.006 |
| CNN OHLCV (Phase 1) | 0.614 | 0.599–0.631 | ~0.014 |

---

## 4. Hold bake-off — AUC valid (tabular)

| Track | AUC | Δ cumulative |
|-------|-----|--------------|
| H0 state | 0.596 | — |
| H2 + exit tech | 0.616 | +2.0 pp |
| **H3 full** | **0.623** | +2.7 pp |
| H recovery B1 | 0.611 | −1.1 vs H3 |

Stability H3 (3 seeds): mean **0.593**, σ **0.022** — нестабильно vs entry.

Chart hold — **не запускали**.

---

## 5. Interim verdict

| Вопрос | Ответ |
|--------|--------|
| Chart beat tabular **offline**? | **Marginally** (+0.4–0.6 pp mean); peak fusion +1 pp |
| Chart beat tabular **стабильно**? | **Нет** — E3 tabular σ=0.004, fusion mean < E3 |
| Broadcast ctx on chart? | **No-go** — сильный минус |
| Chart → prod shadow? | **No-go interim** |
| Tabular E3/H3 → prod shadow? | **Yes** — deployed 22.06, log_only |
| Единая точка решения? | **Частично** — shadow entry+hold в stack/cron; apply только v1 CatBoost |

---

## 6. Prod shadow (не chart)

| Контур | Модель | Runtime | UI |
|--------|--------|---------|-----|
| Entry E3 | `game5m_entry_catboost_e3.cbm` | `game5m_policy.py` | `/game5m/cards` ML shadow |
| Hold H3 | `game5m_hold_bar_catboost_h3.cbm` | `send_sndk_signal_cron.py` | cards при open position |

Policy offline (bar labels): H3 @ τ=0.55 defer 34%, precision ~70%; E3 poorly calibrated at high τ.

---

## 7. Артефакты (local, не в git)

| JSON | Содержание |
|------|------------|
| `game5m_tabular_ablation.json` | E0–E3, H0–H3 |
| `game5m_ml_stability.json` | E3, H3, fusion 3 seeds |
| `game5m_chart_entry_fusion_bakeoff.json` | Phase 2 fusion |
| `game5m_entry_bakeoff_phase15.json` | T vs TNC vs chart+ctx |
| `game5m_ml_policy_backtest.json` | τ-grid |

---

## 8. Следующие шаги

1. **P0:** 2 нед RTH shadow telemetry → promotion review ~**2026-07-14**
2. **P1:** H3 train alignment; policy backtest on **trades** (ΔPnL)
3. **P2:** Chart window ablation; ticker-group valid; calibration
4. **P3:** Final chart go/no-go memo ~**2026-08-15** — prod chart только если stable beat E3 ≥2 pp

---

*Отчёт v2 — 2026-06-22. v1 (2026-06-21) — Phase 1 LSTM/CNN only; superseded by §3–5.*
