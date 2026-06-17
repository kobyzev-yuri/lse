# Trust Arbiter — единые критерии доверия для оператора и decision_stack

**Статус:** MVP в коде (2026-06-17)  
**Связано:** [ML_AND_DECISION_ARCHITECTURE.md](ML_AND_DECISION_ARCHITECTURE.md) (L1–L3), [GAME_5M_DECISION_ARCHITECTURE.md](GAME_5M_DECISION_ARCHITECTURE.md), [earnings-event-agent-lse/EARNINGS_PRODUCT_ROADMAP.md](earnings-event-agent-lse/EARNINGS_PRODUCT_ROADMAP.md)

**Проблема:** рекомендаций много (readiness JSON, fusion, shadow, `ml_production_arbiter`, brief), но нет **одной шкалы «насколько верить сейчас»** по контуру и по игре. Оператор в Telegram и `decision_stack` должны опираться на **одни и те же числа**.

---

## 1. Три слоя + Trust (между L2 и L3)

```text
L1 Retrain     → модель и данные свежие
L2 Gates       → метрики train/OOS (AUC, RMSE, n_train) — «модель обучаема»
L2.5 Trust     → историческая правота на созревших фактах — «модели можно верить в проде»
L3 Product     → gate_mode apply + weight в decision_stack / legacy flags
```

| Слой | Вопрос | Пример ORCL 10.06 |
|------|--------|-------------------|
| L2 | Модель прошла порог train? | scenario acc 63%, grid ready ✅ |
| L2.5 | На прошлых событиях sign/PnL ок? | gap_up sign hit ~55% (слабо) |
| L3 | Влиять на сделку? | earnings `execution_blocked`, GAME_5M multiday `apply` |

**Правило:** L2 `ready=true` **не** даёт L3 `apply`, пока L2.5 Trust ниже порога для контура (кроме явного ops override).

---

## 2. Trust Index (0…1) — четыре компонента

Для каждого `contour_id` и опционально **контекста** (`scenario_class`, `surface`, `horizon`):

| Компонент | Код | Источник | Смысл |
|-----------|-----|----------|--------|
| **Data maturity** | `T_data` | n_train, n_matured shadow, n_labels | Достаточно фактов для вывода |
| **Model quality** | `T_model` | L2 gates (AUC, RMSE, class acc) | Качество на holdout при train |
| **Historical hit** | `T_hit` | rolling shadow / post-mortem | Правота pred vs fact на созревших событиях |
| **Context fit** | `T_ctx` | alignment, regime, conflict rate | Текущий кейс похож на успешные |

**Итог:**

```text
trust_score = w_d·T_data + w_m·T_model + w_h·T_hit + w_c·T_ctx   (веса по контуру)
```

**Уровни доверия (для оператора и арбитра):**

| trust_score | Метка | `gate_mode` рекомендация | weight в stack |
|-------------|-------|--------------------------|----------------|
| ≥ 0.75 | **high** | `apply` (если L2 ready) | 1.0 |
| 0.45–0.74 | **medium** | `caution` / log_only с пометкой | 0.35 |
| 0.25–0.44 | **low** | `log_only` | 0.1 |
| < 0.25 | **insufficient** | `none` — не показывать как сигнал | 0 |

Сейчас в коде: `stack_readiness` + `weight_for_readiness` (production=1, caution=0.35) — **без** `T_hit`. Trust Arbiter **дополняет** это, не ломая dual-track.

---

## 3. Надёжные пороги по контурам (минимальный n)

Пока `n_matured` ниже порога — `T_hit` = **insufficient**, арбитр не повышает trust выше **medium** даже при хорошем L2.

| contour_id | surface | n_min (matured / OOS) | T_hit метрика | apply порог T_hit |
|------------|---------|------------------------|---------------|-------------------|
| `catboost_entry_5m` | GAME_5M | 80 сделок с context | sign accuracy OOS | ≥ 0.55 |
| `multiday_lr` | GAME_5M | 200 point-days WF | mean sign 1d OOS | ≥ 0.52 |
| `gap_forecast` | GAME_5M | 30 дней rolling | beat PM baseline 14d | > 50% дней |
| `recovery_ml` | GAME_5M | 50 TIME_EXIT rows | defer policy AUC | ≥ 0.55 |
| `portfolio_catboost` | PORTFOLIO | 40 closed trades | RMSE + sign | gate ready |
| `event_reaction` | EARNINGS | 50 events 5d | sign regression 5d | ≥ 0.55 |
| `earnings_scenario` | EARNINGS | 50 events 5d | scenario sign accuracy | ≥ 0.60 |
| `peer_spillover` | EARNINGS | 80 (event,peer) rows | peer sign accuracy | ≥ 0.55 |
| `open_path` | EARNINGS | 100 rule labels | shadow sign | ≥ 0.55 |

**Текущий prod (июнь 2026):** earnings scenario shadow **n=44** — близко к порогу 50; peer spillover **n≈30** scored — **ещё insufficient** для `apply`; GAME_5M entry — **insufficient**.

Контекстные срезы (включаются при n≥15 в bucket):

- `scenario_class` (gap_up, capex_peers, …)
- `alignment` (aligned_or_weak, conflict)
- `source vs peer` ticker role

---

## 4. Post-mortem → T_hit (earnings)

После созревания 5d по событию пишется строка (целевой артефакт):

`last_earnings_postmortem_rows.jsonl`:

```json
{
  "symbol": "ORCL", "event_date": "2026-06-10",
  "models": {
    "regression_5d": {"pred": -0.0039, "fact": -0.0809, "sign_hit": false, "rmse_bucket": "miss_large"},
    "scenario_sign": {"pred_sign": 1, "fact_sign": -1, "hit": false, "class_hit": true},
    "peer_spillover": [
      {"peer": "NVDA", "pred": 0.041, "fact_5d": null, "sign_hit": null}
    ]
  },
  "fusion": {"alignment": "aligned_or_weak", "conviction": "low", "would_have_blocked": true}
}
```

**Агрегация (rolling 90d):**

- `T_hit(scenario_sign) = mean(sign_hit)` по source
- `T_hit(peer_spillover) = mean(sign_hit)` по (event, peer)
- `T_hit(regression) = 1 - normalized_rmse` или доля sign hit

**Практический вывод для арбитра:** при `conviction=low` и исторически `would_have_blocked` в 80% случаев с плохим fact — **не усиливать** GAME_5M long по связанным тикерам.

---

## 5. Единый арбитр (код — целевой)

Расширить паттерн `build_ml_production_arbiter` → **`build_unified_trust_arbiter`**:

**Вход:**

- `ml_train_readiness.jsonl` (L2)
- `last_earnings_scenario_shadow.json` + post-mortem JSONL
- `multiday_lr_reality_check`, gap forecast metrics
- `ml_contours_status.json`
- опционально: текущий тикер / событие / `decision_snapshot`

**Выход** (тот же контракт, что `make_analyzer_block`):

```json
{
  "arbiter_version": "trust_v1",
  "generated_at_utc": "...",
  "surfaces": {
    "GAME_5M": {
      "overall_trust": "medium",
      "contours": [
        {
          "contour_id": "multiday_lr",
          "trust_score": 0.62,
          "trust_label": "medium",
          "T_data": 0.8, "T_model": 0.7, "T_hit": 0.55, "T_ctx": 0.5,
          "recommended_gate_mode": "apply",
          "n_matured": 210,
          "conclusion_ru": "Multiday ridge: WF sign ок, entry apply на legacy."
        }
      ]
    },
    "EARNINGS": { "...": "..." },
    "PORTFOLIO": { "...": "..." }
  },
  "operator_digest_ru": "… одно сообщение …",
  "decision_stack_weights": {
    "multiday_lr": 0.35,
    "event_reaction": 0.1,
    "earnings_scenario": 0.1
  }
}
```

**Интеграция L3:** `decision_stack` читает `decision_stack_weights` или вычисляет `weight = weight_for_readiness(readiness) * trust_score`.

---

## 6. Telegram для оператора (один экран)

**Частота:** 1× утро MSK + по событию (earnings brief / gate flip).

**Шаблон `operator_digest_ru`:**

```text
LSE Trust · 2026-06-17

GAME_5M (торгуем)
  multiday_lr     medium 0.62  apply entry  WF sign 54%
  catboost_entry  low     0.28  log_only   n=137 AUC 0.69
  gap_forecast    low     0.31  caution    PM baseline лучше ML

PORTFOLIO
  portfolio_cb    high    0.81  apply      gate ready

EARNINGS (advisory, не блокирует бот)
  scenario shadow medium 0.68  n=44 sign 77%
  regression 5d   low     0.35  neutral часто, RMSE>порог
  peer spillover  low     0.40  n=30 scored

Событие (если было вчера)
  ORCL 10.06: scenario sign ✗ fact −7.8% · fusion low conv · не входить

Итог: торговать по GAME_5M multiday; earnings — только контекст; ORCL — осторожность cloud.
```

**Принципы UX:**

1. **Сначала игра** (что реально исполняется), потом earnings advisory.
2. **Одна строка на контур:** метка + score + gate + одна цифра истории.
3. **События** — только если post-mortem созрел (5d fact есть).
4. **Итог одной фразой** — не таблица gates.

Существующие алерты (autoprep digest, brief, labeling gaps) **вкладываются** в блок «Событие» / «EARNINGS», не отдельным шумом.

---

## 7. Арбитр в decision_stack (по играм)

| Игра | Исполнитель сейчас | Как Trust влияет |
|------|-------------------|------------------|
| **GAME_5M** | legacy `technical_decision_effective` | `trust × weight` на multiday, catboost, gap; veto только при trust≥high и gate=apply |
| **PORTFOLIO** | `execution_agent` | portfolio_cb trust≥high → BLOCK/ALLOW сильнее |
| **EARNINGS** | advisory / portfolio `event_reaction` contribution | trust<medium → strength=0, detail-only; trust medium → caution на semis 1–2d после отчёта |

**Earnings → GAME_5M (phase D, не сейчас):**

```text
IF earnings_trust(scenario) ≥ medium
   AND recent_earnings_event(peer_ticker)
   AND peer_spillover_sign_hit(peer) ≥ 0.6 historical
THEN contribution event_reaction strength *= trust_score
ELSE log_only
```

Пока `DECISION_STACK_EVENT_REACTION_GATE_MODE=log_only` и `execution_blocked` на fusion — только shadow.

---

## 8. Что уже есть vs что добавить

| Есть | Не хватает |
|------|------------|
| L2 readiness JSONL, earnings/open-path gates | Единый `trust_score` per contour |
| `ml_production_arbiter.conclusion_ru` (GAME_5M) | Earnings + peer в том же арбитре |
| Shadow `sign_accuracy` aggregate | Per-event post-mortem JSONL + rolling T_hit |
| `weight_for_readiness` в decision_stack | Умножение на trust_score |
| Fusion alignment/conviction | Связь conviction с историческим hit rate |
| Telegram brief / autoprep digest | Один утренний **Trust digest** |

**MVP roadmap (реализовано 2026-06-17):**

1. `services/earnings_event_postmortem.py` → `last_earnings_postmortem_rows.jsonl` + `last_earnings_trust_metrics.json`
2. `services/unified_trust_arbiter.py` → `last_unified_trust_arbiter.json`
3. `scripts/notify_trust_digest.py` (cron 06:05 UTC = 09:05 MSK)
4. `decision_stack`: `effective_stack_weight` / `trust_score` в contributions
5. **Analyzer UI:** вкладка Trust — позже

---

## 9. Критерии «можно верить» (чеклист ops)

Контур допускается к **apply** (ручной sign-off), когда **все**:

- [ ] L2 gate `ready=true`
- [ ] `n_matured` ≥ n_min из §3
- [ ] `T_hit` ≥ порог 90d rolling
- [ ] Нет деградации 14d vs 90d (hit_14d ≥ hit_90d − 0.1)
- [ ] Mirror / decision_snapshot без системных расхождений 7d
- [ ] Ops запись в runbook (не автоматический flip `*_ENABLED`)

Earnings autoprep gate (`overall_earnings_autoprep_ready`) — **отдельный** L2 для data pipeline; Trust scenario — **отдельно** для влияния на игры.

---

## 10. Пример ORCL 10.06 (как бы выглядел Trust)

| Контур | T_hit (иллюстрация) | Рекомендация |
|--------|---------------------|--------------|
| scenario sign | промах (bullish vs −7.8%) | −1 к bucket gap_up |
| regression | промах по величине | T_hit regression низкий |
| fusion | low conv — historically safe | **не входить** ✓ |
| peer NVDA 1d | +2.2% vs pred +4% | 5d ещё нет — не считать |

**operator_digest:** «ORCL: narrative bullish, факт −8%; доверие scenario sign по gap_up снижено; GAME_5M — без усиления cloud long.»

---

*Обновлять пороги §3 при росте n и после phase D sign-off.*
