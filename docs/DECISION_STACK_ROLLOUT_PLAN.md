# Decision Stack: единая точка торговых решений

> **Superseded for contour/cron matrix:** [ML_AND_DECISION_ARCHITECTURE.md](ML_AND_DECISION_ARCHITECTURE.md). Этот файл — deep-dive по **имплементации L3** (фазы 0–14).

**Статус:** фазы 0–3 в коде (snapshot, fusion, own_finalize, resolve); на проде `RESOLVE=false` — mirror + `projected_effective_if_resolve`.  
**Каноническая продуктовая концепция GAME_5M:** [GAME_5M_DECISION_ARCHITECTURE.md](GAME_5M_DECISION_ARCHITECTURE.md). Этот документ оставлен как rollout/implementation plan для `decision_stack`, а не как единственный источник алгоритма принятия решений.  
**Связано:** [ML_CONSOLIDATION_ROLLOUT_PLAN.md](ML_CONSOLIDATION_ROLLOUT_PLAN.md), [ANALYZER_CONTOUR_ARCHITECTURE.md](ANALYZER_CONTOUR_ARCHITECTURE.md), [NEWS_SIGNAL_ARCHITECTURE.md](NEWS_SIGNAL_ARCHITECTURE.md), [ML_CALIBRATION_PHASES.md](ML_CALIBRATION_PHASES.md).

---

## 1. Цель

Все контуры (правила, KB-новости, macro/gap, кластер, ML-сетки, multiday, recovery, event, бриф оператора) сходятся в **`decision_snapshot`** и одно поле **`decision_effective`** для входа/удержания/выхода.

- **Без LLM:** детерминированный `resolve_technical()` по весам и `readiness`.
- **С LLM:** тот же snapshot в промпт; LLM — интерпретация и конфликты, не пересчёт CatBoost/ridge.

---

## 2. Контракт `decision_snapshot` (v1)

```json
{
  "schema_version": 1,
  "game": "GAME_5M",
  "ticker": "NVDA",
  "ts_utc": "...",
  "core_decision": "BUY",
  "effective_decision": "HOLD",
  "resolve_mode": "mirror_legacy",
  "contributions": [
    {
      "contour_id": "rules_5m",
      "role": "core",
      "readiness": "production",
      "strength": 0.6,
      "weight": 1.0,
      "action": "signal",
      "detail": "..."
    }
  ],
  "conflicts": [],
  "llm_eligible": ["news_fusion", "cluster_context", "boss_brief"]
}
```

| Поле | Смысл |
|------|--------|
| `readiness` | `telemetry` \| `caution` \| `production` |
| `strength` | −1…+1 (медвежий…бычий) |
| `weight` | 0 при telemetry, иначе множитель |
| `action` | `signal` \| `veto` \| `downgrade` \| `boost` \| `telemetry` |

---

## 3. Фазы имплементации

| Фаза | Deliverable | Меняет прод? |
|------|-------------|--------------|
| **0** | `services/decision_stack/`, `analyzer_contours/schema.py`, план (этот файл) | нет |
| **1** | `finalize_game5m_decision_stack()` в `get_decision_5m`, `decision_snapshot` в `context_json` | нет* |
| **2** | `entry_fusion_metrics`, гейты news/macro/entry_advice, `projected_effective_if_resolve` | **сделано** (apply через env) |
| **3** | `DECISION_STACK_OWN_FINALIZE` — CatBoost+multiday в stack | **сделано** (default true) |
| **4** | `build_portfolio_decision_stack` в `execution_agent` | опционально |
| **5** | `news_signal_batch` (этап A) → contribution | нет→да |
| **6** | Cluster exposure cap (техника) + boss KB tag | caution |
| **7** | LLM overlay: `decision_llm_overlay` в snapshot | опционально |
| **8–14** | ContourRegistry в анализаторе, promotion_gate → config (см. ARCHITECTURE_OPTIMIZATION) | по gate |

\* `effective` = существующий `technical_decision_effective` (mirror).  
\** только при `DECISION_STACK_RESOLVE_ENABLED=true`.

---

## 4. Карта контуров → игры

| contour_id | GAME_5M | PORTFOLIO | readiness сейчас |
|------------|---------|-----------|------------------|
| `rules_5m` / `strategy_rules` | ✓ | ✓ | production |
| `kb_news` | ✓ | ✓ | production |
| `news_fusion` | ✓ | ✓ | caution (telemetry) |
| `entry_advice` | ✓ | ✓ | production |
| `macro_risk` | ✓ | ✓ | production |
| `gap_forecast` | ✓ | feature | caution |
| `cluster_context` | ✓ | ✓ | telemetry / LLM |
| `catboost_entry_5m` | ✓ | — | caution |
| `portfolio_catboost` | context | ✓ | caution |
| `multiday_lr` | ✓ | planned | caution |
| `recovery_ml` | exit | — | log_only |
| `event_reaction` | opt | opt | telemetry |
| `boss_brief` | LLM | LLM | operator |

---

## 5. Feature flags

```env
DECISION_STACK_ENABLED=true
DECISION_STACK_RESOLVE_ENABLED=false
DECISION_STACK_VERSION=1
```

---

## 6. Критерии готовности

**Фаза 1**
- [x] `decision_snapshot` в `context_json`
- [x] mirror: `effective_decision` == `technical_decision_effective`
- [x] `tests/test_decision_stack_game5m.py`

**Фаза 2–3 (staging)**
- [ ] `resolve_divergence=false` при согласованных гейтах или осознанный diff
- [ ] Включить `DECISION_STACK_RESOLVE_ENABLED=true` после 3–7 дней mirror
- [ ] Опционально `ENTRY_ADVICE` / `NEWS_FUSION` gate `apply` по TE

---

## 7. Порядок PR

1. Фаза 0–1 (этот PR): модуль + GAME_5M + deal_params + тест + doc.
2. Фаза 2: fusion metrics + опциональный resolve entry_advice.
3. Фаза 3–4: portfolio + перенос finalize.
4. Параллельно: фазы 6–14 из ANALYZER_CONTOUR rollout.
