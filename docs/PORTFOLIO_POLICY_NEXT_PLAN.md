# Portfolio policy — следующий план (без return-CatBoost)

**Дата:** 2026-07-17  
**Статус:** активный план внедрения  
**Связано:** [PORTFOLIO_TREND_OVERLAY_PLAN.md](PORTFOLIO_TREND_OVERLAY_PLAN.md), [ML_PORTFOLIO_CATBOOST.md](ML_PORTFOLIO_CATBOOST.md), [OPTIONS_SENTIMENT_INTEGRATION_PLAN.md](OPTIONS_SENTIMENT_INTEGRATION_PLAN.md)

---

## Вердикт по CatBoost 5d / D1–D4

| Вопрос | Ответ |
|--------|--------|
| Ждать D1–D4 (drop price, IC gate, FX out, rescale)? | **Нет.** Это косметика readiness, не новая гипотеза edge. |
| Опираться на 5d block / ML-take от expected_5d? | **Нет.** IC≈0, за 30д блок thr=42 = 0 срабатываний; калибровка score↔win инвертирована. |
| 20d return CatBoost? | Оставить **telemetry / soft prospect**; не инвестировать в дообучение того же target. |
| Что делать с nightly 5d/20d train? | Можно не трогать cron (дёшево); **не** планировать promotion/тюнинг порогов ради «оживления» модели. |

**Правило:** новые силы — в **policy** (exit / late-chase / peer-rank / options), не в forecast `log(close[t+h]/close[t])`.

---

## Очередь по ожидаемой эффективности

| # | Идея | Зачем (боль) | Режим | DoD |
|---|------|--------------|-------|-----|
| **P1** | **Live regime на выходе** + counterfactual trailing | ALAB/MU/TER: ранний TRAILING при продолжении melt-up; NBIS: узкий выход в breakdown | apply (live regime) | Exit использует **текущий** 20d regime, не только snapshot на BUY; скрипт CF → JSON |
| **P2** | **Late-chase bake-off** (rule, без новой модели) | INTC: BUY у high после ралли | shadow CF → optional thr | CF thr 18/20/25 на истории; apply только если CF бьёт текущий 25 |
| **P3** | **Peer rank в shape-cluster** | Не абсолютный return, а «хуже peers в группе» | log_only → context | Поля `portfolio_peer_*` в snapshot; analyzer counts |
| **P4** | **Options sentiment → portfolio** | Режимный фильтр aggressive BUY | log_only (фаза 6 options plan) | Contribution + `would_block` в guards, без apply |

ML-классификатор exit/late-chase — **фаза P1b / P2b** только если rule+CF дают стабильный сигнал на ≥15–20 закрытых сделках.

---

## P1 — Live exit regime (сейчас)

**Баг/ограничение MVP:** `evaluate_portfolio_exit` брал `regime_from_context` (режим **на входе**). Позиция, вошедшая в `neutral` и разогнанная в `melt_up`, продолжала узкий trailing.

**Фикс:** `PORTFOLIO_TREND_EXIT_USE_LIVE_REGIME=true` (default) → на каждом exit-poll снимок `portfolio_trend_regime_snapshot(ticker)`.

**Артефакт CF:** `scripts/run_portfolio_exit_policy_counterfactual.py` → `last_portfolio_exit_policy_cf.json`  
Метрики: для TRAILING_TAKE — peak после выхода, giveback, «сработал бы melt_up trailing?».

---

## P2 — Late-chase

Уже в prod: `PORTFOLIO_TREND_LATE_CHASE_*` (ret_20d≥25% + near high).

Шаги:
1. CF на закрытых BUY: метка «late_chase_bad» = near_high & ret_20d≥X & realized&lt;0 (или max adverse ≥N%).
2. Сравнить X∈{18,20,25} и near_high_pct∈{2.5,4}.
3. Менять thr в config **только** после CF go.

---

## P3 — Peer relative rank

1. Взять cluster membership из shape-clusters cache.
2. Rank по `ret_20d` (потом опционально path capture).
3. Shadow: `portfolio_peer_rank`, `portfolio_peer_n`, `portfolio_peer_ret_vs_medoid_pct`.
4. Apply позже: block/deprioritize если rank худший в кластере из ≥3 и regime≠melt_up.

---

## P4 — Options → portfolio

См. [OPTIONS_SENTIMENT_INTEGRATION_PLAN.md](OPTIONS_SENTIMENT_INTEGRATION_PLAN.md) фаза 6:  
`options_sentiment_blocks_buy` log_only + contribution в `decision_stack/portfolio.py`.  
Apply (фаза 7) — отдельный go/no-go.

---

## Что не делать в этом треке

- Новые horizon CatBoost / Ridge на portfolio returns.
- Поднимать `PORTFOLIO_CATBOOST_HOLD_BELOW_SCORE` «наугад».
- Одновременно apply P2 thr + P4 options + P3 peer.

---

## Статус внедрения (2026-07-17)

| # | Статус | Что в коде |
|---|--------|------------|
| **P1** | ✅ | `resolve_exit_regime` + `PORTFOLIO_TREND_EXIT_USE_LIVE_REGIME` (default true); CF `scripts/run_portfolio_exit_policy_counterfactual.py` |
| **P2** | ✅ CF | Тот же скрипт: late_chase thr 18/20/25; **thr в config не меняли** — ждать go |
| **P3** | ✅ shadow | `services/portfolio_peer_rank.py` → `portfolio_peer_*` в snapshot |
| **P4** | ✅ log_only | `portfolio_options_*` + contribution `options_sentiment` в portfolio stack; apply off |

**5d D1–D4:** frozen — не ждать.
