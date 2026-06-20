# Отчёт по ML-плану: статус контуров и dual-track

**Дата:** 2026-06-12  
**Версия:** [VERSION.md](../VERSION.md) v2.0.0  
**Планы:** [ML_CONSOLIDATION_ROLLOUT_PLAN.md](ML_CONSOLIDATION_ROLLOUT_PLAN.md), [CONSOLIDATION_NEXT_PLAN.md](CONSOLIDATION_NEXT_PLAN.md)  
**Ops-срез:** [PROJECT_STATUS_AND_ROADMAP.md](PROJECT_STATUS_AND_ROADMAP.md)  
**Словарь терминов:** [ML_GLOSSARY_RU.md](ML_GLOSSARY_RU.md) — L1/L2/L3, AUC, RMSE, shadow, BMO/AMH, open-path vs event 5d.

---

## 1. Dual-track: как устроено сейчас

**Dual-track** (два параллельных пути) — legacy **исполняет** сделки, **decision stack** (стек решений) пишет **snapshot** (снимок вкладов) в `context_json` для аудита и будущего **RESOLVE** (единый исполнитель).

Два параллельных пути **не ждут друг друга**:

```text
rules + KB + macro + premarket_gap_baseline
        │
        ├─► LEGACY HOT PATH (текущий исполнитель — исполняет сделки сегодня)
        │     technical_decision_effective
        │     + CatBoost fusion (если ENABLED)
        │     + multiday entry gate (если gate_mode=apply)
        │     + portfolio CatBoost (отдельная поверхность)
        │     cron: send_sndk_signal_cron → BUY/HOLD
        │
        └─► DECISION STACK (параллельно, в context_json)
              decision_snapshot + contributions (вклады контуров)
              projected_effective_if_resolve (что было бы при RESOLVE=true)
              RESOLVE=false → только shadow (лог без блока) / telemetry (телеметрия)
              RESOLVE=true  → stack подменяет legacy (опционально)
```

| Параметр | Значение на prod | Смысл |
|----------|------------------|-------|
| `DECISION_STACK_ENABLED` | true | Snapshot пишется в каждую сделку |
| `DECISION_STACK_OWN_FINALIZE` | true (default) | ML-гейты (CatBoost, multiday) в единой точке до snapshot |
| `DECISION_STACK_RESOLVE_ENABLED` | **false** | **Legacy исполняет**; stack не подменяет |
| Исполняемое поле GAME_5M | `technical_decision_effective` | Cron и вход смотрят сюда |

**Пример divergence (расхождение legacy vs stack):** rules дают STRONG_BUY у open; stack при `RESOLVE=false` может записать `projected_effective_if_resolve=HOLD` из-за **session veto** (запрет агрессивного входа у NEAR_OPEN). Cron всё равно исполняет legacy — это **норма**, не баг (см. §4).

**Ключевой принцип (зафиксирован):** контур с tier `promoted` (в проде) или `legacy_apply` (на legacy через gate) **включается на legacy сразу**, через свой `*_ENABLED` / `*_GATE_MODE`, **не дожидаясь** `RESOLVE=true` и не дожидаясь «завершения фазы 3».

`RESOLVE=true` — отдельный рычаг для **единого** stack-исполнителя (session veto NEAR_OPEN/CLOSE и т.д.), не для первого включения ML.

Проверка runtime:

```bash
docker exec lse-bot python scripts/print_ml_product_status.py
docker exec lse-bot python scripts/print_ml_product_status.py --json
```

Код реестра: `services/ml_product_runtime.py`.

---

## 2. Фазы consolidation plan

| Фаза | Статус | Итог |
|------|--------|------|
| **0** Документация | ✅ | Канон, rollout, archive stubs |
| **1** L1 противоречия | ✅ | Watermark, dispatcher nightly, datasets-only |
| **2** Registry 8/8 | ✅ | multiday/recovery/gap refresh, prod full train |
| **3** L3 promotion | 🟡 упор | Только portfolio + multiday entry на legacy; остальное defer/telemetry |
| **4** Ops/UI | backlog | API refresh, config audit |

---

## 3. Матрица по каждому ML-контуру

Легенда: **L1** — retrain (переобучение); **L2** — readiness gates (пороги качества); **L3** — влияние на сделку; **AUC** — качество классификатора; **RMSE** — ошибка регрессии. Подробнее: [ML_GLOSSARY_RU.md](ML_GLOSSARY_RU.md) §1–2.

### L1 / L2 / L3 / Legacy / Stack

| Контур | L1 train | L2 gate | Product tier | **Legacy исполняет?** | Stack (RESOLVE=false) | Метрики (2026-06-12) |
|--------|----------|---------|--------------|----------------------|------------------------|----------------------|
| **portfolio** | ✅ | ✅ ready | **promoted** | ✅ `PORTFOLIO_CATBOOST_ENABLED=true` | shadow | RMSE≈0.079 |
| **multiday_lr** | ✅ | **ready** (WF 2026-06-09) | **legacy_apply** | ✅ entry `apply`, hold `log_only` | shadow | v3nm 1d sign 56%, RMSE 0.047 |
| **game5m_entry** | ✅ | ❌ n_valid | **disabled** | ❌ `CATBOOST_ENABLED=false` | shadow | AUC≈**0.583**, n_valid=**49** (<80) |
| **entry_bar_v2** | ✅ weekly | ✅ AUC≥0.545 | **shadow** | ❌ log_only `catboost_entry_proba_good_v2` | shadow | AUC≈**0.5495**, n_valid=1925 |
| **continuation_ml** | ✅ weekly | ✅ AUC≥0.55 | **telemetry** | ❌ `CONTINUATION_ML_ENABLED=true` log_only | shadow | AUC≈**0.735**, 154 TAKE rows |
| **recovery** | ✅ | D4a | **telemetry** | телеметрия only (D4a) | shadow | AUC≈0.51; D4b defer |
| **gap_forecast** | ✅ | caution | **advisory** | **PM baseline** (policy auto); ML shadow | shadow | 14d PM MAE **0.99** vs ML **1.81** pp |
| **event_reaction** | ✅ | ❌ RMSE | **advisory** | ✅ advisory (`ENABLED=true`) | shadow | RMSE≈0.13 |
| **earnings_grid** | ✅ | partial | **shadow** | UI/Telegram shadow | — | labels **34/40**; spillover sign **83%** |
| **open_path** | ✅ | ❌ | **shadow** | ❌ | — | holdout acc 50%; premarket days 28/60 |

**Event / earnings refresh (2026-06-07):** ERD backfill 471 строк; peer spillover train **188** rows, sign acc valid **≈85%**, `same_sign_rate` **0.40** (после fix peer calendar); scenario shadow **41** matured, sign acc **≈70%**. Якоря и vol-scaled labels — [EVENT_REACTION_PIPELINE.md](EVENT_REACTION_PIPELINE.md), [ML_GLOSSARY_RU.md](ML_GLOSSARY_RU.md) §4.

### CatBoost-сети на диске (prod full train 2026-06-05)

| Модель | Обучена | L3 legacy | Примечание |
|--------|---------|-----------|------------|
| `portfolio_return_catboost.cbm` | ✅ | ✅ prod | Единственный full L2+L3 |
| `game5m_recovery_catboost.cbm` | ✅ | log-only | D4b defer |
| `event_reaction_forward5d_catboost.cbm` | ✅ | advisory | Не блокирует сделки |
| `event_reaction_scenario_catboost.cbm` | ✅ | shadow | Earnings UI |
| `peer_spillover_forward5d_catboost.cbm` | ✅ | advisory | Brief context |
| `open_path_scenario_catboost.cbm` | ✅ | shadow | Не в hot path |
| `game5m_entry_catboost.cbm` | ✅ | **off** | Gate не пройден |
| `game5m_entry_catboost_v2.cbm` | ✅ | log_only | Bar TB shadow |
| `game5m_continuation_catboost.cbm` | ✅ | log_only | TAKE telemetry (2026-06-20) |

---

## 4. RESOLVE и mirror

**Mirror report** — сравнение legacy `technical_decision_effective` vs `projected_effective_if_resolve` из stack на закрытых сделках.

| Метрика | 7d | 14d | Расшифровка |
|---------|----|-----|-------------|
| Сделки со snapshot | 32 | 41 | GAME_5M с `decision_snapshot` |
| Divergence | 4 (12.5%) | 5 (12.2%) | legacy ≠ projected |
| Session veto | все | все | расхождение из-за NEAR_OPEN/CLOSE |
| Unexpected | 0 | 0 | divergence без объяснимой причины |

**Политика:** session-divergence — норма; legacy остаётся исполнителем. `RESOLVE=true` — только при росте `unexpected_divergence` или ухудшении edge.

Артефакт: `last_decision_stack_mirror_report.json`  
Cron: вс 06:10 MSK — `report_decision_stack_mirror.py --days 14`

---

## 5. План на будущее

### Ближайшие триггеры (пересмотр promotion)

| Триггер | Контур | Действие |
|---------|--------|----------|
| `n_valid ≥ 80` **и** AUC ≥ 0.52 | game5m_entry | `GAME_5M_CATBOOST_ENABLED=true` на legacy (сейчас n_valid=49) |
| rolling 14d **и** 30d ML MAE < PM | gap_forecast | policy auto переключится на ML; L3 — отдельное решение |
| `would_defer ≥ 5` + arbiter OK | multiday hold | `GAME_5M_MULTIDAY_HOLD_GATE_MODE=apply` на legacy |
| D4a go + 20+ TE с gate | recovery | PR D4b defer на legacy |
| Weekly `run_multiday_wf_game5m.py` | multiday_lr | cron **вс 06:25 MSK** → `last_multiday_wf_game5m.json` |
| `unexpected_divergence > 0` | RESOLVE | рассмотреть `DECISION_STACK_RESOLVE_ENABLED=true` |
| earnings labels ≥ 40 | earnings_grid | shadow → product tier; сейчас **33** LLM labels |

### Очередь по фазе 3 (без big-bang)

1. **Накопление** — mirror weekly, D4a cron, multiday hold telemetry  
2. **Entry CatBoost** — фаза C (analyzer, больше round-trips)  
3. **Recovery D4b** — go/no-go по таблице в [GAME_5M_TIME_EXIT_RECOVERY_PLAN.md](GAME_5M_TIME_EXIT_RECOVERY_PLAN.md)  
4. **RESOLVE** — опционально, не блокирует legacy ML  
5. **Фаза 4** — `POST /api/ml/refresh`, config audit prod

### Что не делать

- Не включать `RESOLVE=true` ради первого ML — legacy уже несёт готовые контуры  
- Не включать entry CatBoost при n_valid < 80 (AUC alone недостаточно)  
- Не включать gap ML L3 пока rolling 14d+30d не beat PM (`recommended_effective_source` в metrics JSON)  
- Не включать recovery D4b без D4a sign-off

---

## 6. Ссылки

| Документ | Тема |
|----------|------|
| [ML_GLOSSARY_RU.md](ML_GLOSSARY_RU.md) | **Словарь:** L1/L2/L3, метрики, BMO/AMH, open-path vs event 5d |
| [TRADE_ML_DATASETS_AND_TARGETS_RU.md](TRADE_ML_DATASETS_AND_TARGETS_RU.md) | **Датасеты, метки, метрики, complementarity, §0 event vs open-path** |
| [EVENT_REACTION_PIPELINE.md](EVENT_REACTION_PIPELINE.md) | ERD backfill, якоря, vol-scaled labels |
| [earnings-event-agent-lse/EARNINGS_UI_GUIDE.md](earnings-event-agent-lse/EARNINGS_UI_GUIDE.md) | UI `/earnings`, вкладки Spillover / Shadow / Fusion |
| [ML_AND_DECISION_ARCHITECTURE.md](ML_AND_DECISION_ARCHITECTURE.md) | Канон L1–L3 |
| [CONSOLIDATION_NEXT_PLAN.md](CONSOLIDATION_NEXT_PLAN.md) | Спринты 3.2–3.3 |
| [ML_CONSOLIDATION_ROLLOUT_PLAN.md](ML_CONSOLIDATION_ROLLOUT_PLAN.md) | Фазы 0–4 |
| [GAME_5M_DECISION_ARCHITECTURE.md](GAME_5M_DECISION_ARCHITECTURE.md) | Алгоритм GAME_5M |
| [DECISION_STACK_ROLLOUT_PLAN.md](DECISION_STACK_ROLLOUT_PLAN.md) | Имплементация stack |
