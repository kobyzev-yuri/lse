# GAME_5M — план ML-стратегии (актуализация 2026-07-12)

Зафиксировано после сессии 12.07.2026: BUY-only bar v2 калибровка, fusion sweep no-go, разделение контуров **A (стоп)** / **B (разработка)**.

**Связанные документы:** [GAME_5M_AGENT_TUNING_LOG.md](GAME_5M_AGENT_TUNING_LOG.md), [GAME_5M_SESSION_ANALYSIS_PLAYBOOK.md](GAME_5M_SESSION_ANALYSIS_PLAYBOOK.md), [GAME_5M_PREDICTOR_DATASET_PLAN.md](GAME_5M_PREDICTOR_DATASET_PLAN.md).

---

## 1. Что сделано сегодня (12.07.2026)

| Шаг | Результат | Артефакт |
|-----|-----------|----------|
| BUY-only retrain + Platt calibration | AUC BUY 0.557, gates valid зелёные | `game5m_entry_catboost_v2.cbm` |
| Fusion sweep 90d (224 BUY) | **no-go**: Spearman P↔PnL ≈ 0.03, τ=0.50 режет 68/69 | `bar_v2_fusion_sweep.json` |
| Калибровка отчёт | `fusion_calibration_ready=true`, но $-gate красный | `bar_v2_calibration_report.json` |
| Стратегическое решение | **A закрыть**, **B развивать** | bundles + dispatcher + cron |
| Prod bundles | `ml_restore_b_development_v1` + `ml_freeze_a_contours_v1` | `config.env` на VM |

**Git:** `42a92fc` (calibration) → `515b522` (исправление scope freeze A/B).

---

## 2. Список A — остановлено (freeze)

Контуры без PnL-edge / застой. **Не в hot path**, train/cron заморожены.

| # | Контур | Причина стопа | Prod / cron |
|---|--------|---------------|-------------|
| A1 | **game5m_entry v1** (trade CatBoost) | AUC 0.46↓, заменён | dispatcher skip, `ML_READINESS_SKIP_GAME5M=1` |
| A2 | **bar v2 fusion** | sweep no-go, P≈const на BUY | `GAME_5M_CATBOOST_FUSION=none` |
| A3 | **bar v2 train** (BUY-only иначе) | fusion закрыт; модель только архив | `DAILY_ML_RUN_ENTRY_BAR_V2_APPLY=0`, dispatcher skip `game5m_entry_bar_v2` |
| A4 | **recovery_ml** | AUC ~0.46 flat | `RECOVERY_ML_ENABLED=false`, dispatcher skip, recovery D4a cron off |
| A5 | **gap_forecast ML** | PM baseline лучше ML | dispatcher skip `gap_forecast` (PM baseline в rules) |
| A6 | **event_reaction_5d** | RMSE flat, advisory only | dispatcher skip, `ML_READINESS_SKIP_EVENT_REACTION=1` |
| A7 | **open_path** | holdout ~50% | dispatcher skip, open_path weekly train cron off |
| A8 | **peer_spillover** | нет PnL-эффекта | в составе earnings/event pipeline, train frozen |
| A9 | **chart ML** (LSTM/CNN/fusion) | offline no-go | не в cron, закрыт |

**Bundle:** `ml_freeze_a_contours_v1` — `FUSION=none`, `RECOVERY_ML=false`.

**Dispatcher `ML_FROZEN_REFRESH_CONTOURS`:** `game5m_entry`, `game5m_entry_bar_v2`, `recovery`, `gap_forecast`, `open_path`, `event_reaction_regression`.

---

## 3. Список B — в разработке (active)

Единственный ML-фокус. Сначала **exit/hold**, потом entry PnL-label.

| # | Контур | Роль | В решении сейчас | План |
|---|--------|------|------------------|------|
| B1 | **Hold H3** (exit bar CatBoost) | defer TIME_EXIT | log_only (`hold_quality_ml`) | bake-off 15–21.07 → apply |
| B2 | **Continuation TAKE** | defer TAKE | log_only (`continuation_ml`) | go/no-go **~14.07** → apply |
| B3 | **Multiday hold** | early exit / derisk | log_only | 5+ сессий observe → apply конец июля |
| B4 | **Entry PnL-label** | новый entry-ML (`net_pnl>0`) | не реализован | старт август, после B1–B3 |
| B5 | **Earnings grid** | labels + advisory | shadow UI/brief | копим labels ≥40, cron on |
| B6 | **Light analyzer** | observability | не в сделку | cron вс 06:45 MSK |

**Bundle:** `ml_restore_b_development_v1` — hold/continuation/multiday-hold в log_only.

**Приоритет очереди:**

```text
~14.07     B2 continuation apply review
15–21.07   B1 hold bake-off + B3 multiday hold
август     B4 entry PnL-label (если B1–B3 дают сигнал)
параллельно B5 labels, B6 weekly report
```

---

## 4. Рабочий prod (не ML-сетки B, не трогаем)

| Механизм | Статус |
|----------|--------|
| **market_adapt_v1** | gap + advice guards **apply** |
| **multiday entry** | **apply** |
| **portfolio CatBoost** | promoted |
| **intraday_regime** | apply (в market_adapt_v1) |

---

## 5. Prod config (эталон после 12.07.2026)

```env
# Список A
GAME_5M_CATBOOST_FUSION=none
GAME_5M_RECOVERY_ML_ENABLED=false
DAILY_ML_RUN_ENTRY_BAR_V2_APPLY=0
ML_READINESS_SKIP_GAME5M=1
ML_READINESS_SKIP_EVENT_REACTION=1

# Список B (log_only)
GAME_5M_HOLD_QUALITY_LOG_ENABLED=true
GAME_5M_CONTINUATION_ML_ENABLED=true
GAME_5M_CONTINUATION_ML_GATE_MODE=log_only
GAME_5M_MULTIDAY_HOLD_GATE_MODE=log_only
```

---

## 6. Ключевые артефакты мониторинга

| Артефакт | Назначение |
|----------|------------|
| `bar_v2_calibration_report.json` | итог калибровки BUY-only |
| `bar_v2_fusion_sweep.json` | обоснование no-go fusion |
| `analyzer_7d_light.json` | weekly B6 (shadow/guards) |
| `last_game5m_postmortem_tactics.json` | теги A/B/C, тактики |
| `ml_contours_status.json` | dispatcher phase/trigger |

---

## 7. Правила на ближайшие недели

1. **Не поднимать** `GAME_5M_CATBOOST_FUSION` / `HOLD_BELOW_P` без нового $-backtest на PnL-label (B4).
2. **Не включать apply** на B-контурах без go/no-go чеклиста (≥8–15 сделок с telemetry, precision/recall).
3. **Один bundle за раз** — см. playbook §7; не смешивать A и B в одном пакете.
4. **Deploy:** `git push` → `deploy_from_github.sh` на VM (не scp/docker cp для feature work).
5. Список **A не реанимировать** без новой гипотезы и OOS-доказательства.

---

## 8. Следующие вехи

| Дата | Действие |
|------|----------|
| **14.07 07:15 MSK** | **B2 continuation go/no-go #1** — cron + `run_game5m_b2_continuation_gonogo_review.py` |
| **15–21.07** | B1 hold H3 + B3 multiday hold: counterfactual bake-off |
| **21.07 07:15 MSK** | B2 backup review (если 14.07 defer) |
| **конец июля** | Итог: один exit-контур в apply (B2 или B1, не оба сразу без теста) |
| **август** | B4 entry PnL-label dataset + train (если exit дал эффект) |

**Операционный календарь (команды, cron, чеклисты):** [GAME_5M_B_LIST_RUN_SCHEDULE.md](GAME_5M_B_LIST_RUN_SCHEDULE.md).

---

*Последнее обновление: 2026-07-13, prod VM `9e24131`.*
