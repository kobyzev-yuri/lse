# План: Money Management для GAME_5M

**Статус:** черновик roadmap (2026-06).  
**Контур:** размер позиции на **BUY** (и опционально докуп) — **не** замена entry/exit ML из [GAME_5M_PREDICTOR_DATASET_PLAN.md](GAME_5M_PREDICTOR_DATASET_PLAN.md).

**Связанные документы:**

| Тема | Документ |
|------|----------|
| Текущий фикс. ~$10k | `services/game_5m.py` → `GAME_NOTIONAL_USD`, `record_entry` |
| Portfolio MM | [PORTFOLIO_GAME.md](PORTFOLIO_GAME.md) §8, [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md) |
| Entry context (vol, news) | [GAME_5M_DEAL_PARAMS_JSON.md](GAME_5M_DEAL_PARAMS_JSON.md) |
| Решения BUY/HOLD | [GAME_5M_DECISION_ARCHITECTURE.md](GAME_5M_DECISION_ARCHITECTURE.md) |
| ML gates (отдельно) | [GAME_5M_PREDICTOR_DATASET_PLAN.md](GAME_5M_PREDICTOR_DATASET_PLAN.md) §14 |

---

## 1. Проблема (as-is)

| Факт | Значение |
|------|----------|
| Формула | `quantity = max(1, floor(GAME_NOTIONAL_USD / price))`, `GAME_NOTIONAL_USD = 10_000` |
| Номинал на prod | ~**$8112 – $9999** (465 BUY), avg ~$9656 — из‑за целых акций |
| Зависимость от edge | **нет** — STRONG_BUY = BUY = один размер |
| Vol / sentiment | влияют на **вход**, не на **qty** |
| Прозрачность | в `context_json` **нет** `position_sizing` — «почему 22 акции» не видно |
| RiskManager | **не** используется в GAME_5M (только Portfolio) |
| Докуп | опционально `GAME_5M_ALLOW_PYRAMID_*` — каждый лот снова ~$10k |

**Вывод:** это **fixed notional sizing**, не money management. MM = осознанное изменение $‑риска под vol, качество setup и лимиты.

---

## 2. Цель и принципы

**Цель:** масштабировать размер позиции под **риск и качество идеи**, сохраняя сравнимость отчётов и dual-track (shadow → apply).

| Принцип | Содержание |
|---------|------------|
| Разделение слоёв | **Timing** (BUY/HOLD/exit ML) ≠ **Sizing** (qty). Sizing не отменяет gate входа. |
| Log-returns + costs | Backtest и analyzer — в % и log-ret; sizing задаёт **$ exposure**, не меняет правила тейка/стопа. |
| Dual-track | Сначала `position_sizing` в `context_json` (log_only); `apply` меняет `quantity` только после sign-off. |
| Объяснимость | Каждый BUY: `base_notional`, множители, `final_notional`, `quantity`, `sizing_mode`. |
| Без CNN/графиков | Только tabular: vol, sentiment, stop_pct, опционально CatBoost P — уже в `d5`. |
| Не ждать ML gates | MM **не блокируется** promotion review 14.07 для CatBoost entry/continuation. |
| Согласование с Portfolio | Фаза 4 — общий `RiskManager` / capital model; GAME_5M может оставаться paper с virtual capital. |

**Явно не делаем в v1:**

- Kelly full / авто-leverage без cap;
- sizing на exit (partial take) — отдельный трек;
- замена `GAME_NOTIONAL_USD` на ML-regressor «предскажи цену».

---

## 3. Целевая архитектура

```text
get_decision_5m → BUY/STRONG_BUY (как сейчас)
       ↓
build_full_entry_context(d5)
       ↓
compute_game5m_position_sizing(d5, price)   ← новый модуль
       ↓
record_entry(..., entry_context += position_sizing)
       ↓
quantity = sizing.final_quantity  (apply)
       или legacy qty + sizing shadow (log_only)
```

**Артефакты (целевые):**

| Артефакт | Путь |
|----------|------|
| Sizing engine | `services/game5m_position_sizing.py` |
| Schema version | `POSITION_SIZING_SCHEMA_VERSION` в том же модуле |
| Hook | `services/game_5m.py` → `record_entry` |
| Config | `config.env.example` → блок `GAME_5M_SIZING_*` |
| Analyzer | `game5m_position_sizing_review` в trade_effectiveness_analyzer |
| SQL | пресеты в `services/sql_console_presets.py` |
| Tests | `tests/test_game5m_position_sizing.py` |

---

## 4. Модель sizing (v1 — rule-based)

### 4.1 Базовый номинал

```text
base_notional = GAME_5M_SIZING_BASE_NOTIONAL_USD   # default 10000, замена GAME_NOTIONAL_USD
```

### 4.2 Vol-scale (inverse vol targeting, capped)

```text
vol = max(volatility_5m_pct, GAME_5M_SIZING_VOL_FLOOR_PCT)
vol_scale = clip(GAME_5M_SIZING_VOL_TARGET_PCT / vol,
                   GAME_5M_SIZING_VOL_SCALE_MIN,
                   GAME_5M_SIZING_VOL_SCALE_MAX)
```

- **Низкий vol** → `vol_scale > 1` → **крупнее** позиция (при том же «риск-бюджете»).
- **Высокий vol** → `vol_scale < 1` → меньше.

Опционально: `atr_5m_pct` вместо или вместе с `volatility_5m_pct` (max из двух для консерватизма).

### 4.3 Sentiment multiplier (из уже имеющегося сигнала)

Источники (по приоритету):

1. `kb_news_impact` (строка после `apply_kb_news_to_game5m_decision`);
2. fallback: `llm_sentiment` / avg sentiment KB, если есть.

| Класс | `sentiment_mult` |
|-------|------------------|
| very negative / «вход отложен» | **0** (страховка; BUY не должен пройти) |
| negative / «ослаблен» / «осторожность» | **0.6** |
| neutral | **1.0** |
| positive / «поддержка входа» | **1.15** |

Cap: `GAME_5M_SIZING_SENTIMENT_MULT_MAX` (default 1.25).

### 4.4 Confidence (optional v1.1)

Множитель от CatBoost entry (shadow, не fusion apply):

```text
confidence_mult = clip(0.85 + 0.3 × (P_good - 0.5), 0.75, 1.15)
```

Только если `catboost_entry_proba_good` / v2 в `d5` и `GAME_5M_SIZING_USE_CATBOOST_CONFIDENCE=true`.

### 4.5 Risk-based cap (stop distance)

```text
stop_pct = max(stop_loss_pct from d5, GAME_5M_SIZING_STOP_FLOOR_PCT)
risk_usd = GAME_5M_SIZING_RISK_PER_TRADE_USD   # напр. 200 (= 2% от 10k virtual)
risk_cap_notional = risk_usd / (stop_pct / 100)
```

Итог:

```text
raw_notional = base_notional × vol_scale × sentiment_mult × confidence_mult
final_notional = min(raw_notional, risk_cap_notional)
final_notional = clip(final_notional, MIN_NOTIONAL, MAX_NOTIONAL)
quantity = max(1, floor(final_notional / price))
```

### 4.6 Докуп (pyramiding)

| Политика | Поведение |
|----------|-----------|
| `same_as_entry` | каждый BUY-lot — полная формула |
| `half_pyramid` (default v1) | докуп = `final_notional × 0.5` |
| `off` | без изменений sizing при докупе = как первый вход |

Env: `GAME_5M_SIZING_PYRAMID_MODE=same|half|legacy_fixed`.

---

## 5. Контракт `context_json.position_sizing`

Ключ на BUY (и в `deal_params` через merge в `record_entry`):

```json
{
  "position_sizing": {
    "schema_version": "1",
    "mode": "log_only",
    "sizing_policy": "vol_sentiment_v1",
    "base_notional_usd": 10000,
    "price": 440.29,
    "volatility_5m_pct": 0.35,
    "vol_scale": 1.14,
    "kb_news_impact": "нейтрально",
    "sentiment_mult": 1.0,
    "confidence_mult": 1.0,
    "stop_pct_used": 1.5,
    "risk_cap_notional_usd": 13333,
    "raw_notional_usd": 11400,
    "final_notional_usd": 11400,
    "legacy_quantity": 22,
    "final_quantity": 25,
    "applied": false,
    "note": "shadow: qty unchanged in trade_history"
  }
}
```

Поля `legacy_*` vs `final_*` обязательны в shadow для diff в analyzer/SQL.

---

## 6. Фазы работ

### Фаза 0 — Документ + env (1 день)

| # | Задача | Критерий |
|---|--------|----------|
| 0.1 | Этот файл + ссылка из predictor plan §16 | ✅ |
| 0.2 | Блок `GAME_5M_SIZING_*` в `config.env.example` | ключи задокументированы |
| 0.3 | Зафиксировать virtual capital для GAME_5M (default = base_notional × N trades) | одна цифра в env |

### Фаза 1 — Shadow telemetry (P0, можно сразу)

| # | Задача | Критерий |
|---|--------|----------|
| 1.1 | `services/game5m_position_sizing.py` + unit tests | synthetic vol/sentiment cases |
| 1.2 | `record_entry`: merge `position_sizing`, **qty = legacy** | `GAME_5M_SIZING_MODE=log_only` |
| 1.3 | SQL presets: sizing diff, avg mult | `/sql` |
| 1.4 | Analyzer: `game5m_position_sizing_review` | распределение mult, would vs actual notional |

**Не ждёт:** TAKE telemetry, 14.07 ML review.

### Фаза 2 — Apply rule-based sizing (после 1–2 нед shadow)

| # | Задача | Критерий |
|---|--------|----------|
| 2.1 | `GAME_5M_SIZING_MODE=apply` на VM | qty = `final_quantity` |
| 2.2 | Ops sign-off checklist (§8) | min 20 BUY с shadow, нет аномалий |
| 2.3 | Строка в [GAME_5M_AGENT_TUNING_LOG.md](GAME_5M_AGENT_TUNING_LOG.md) | дата apply |

### Фаза 3 — ML sizing (опционально, после v1 apply)

| # | Задача | Критерий |
|---|--------|----------|
| 3.1 | Dataset: y = optimal fraction vs fixed (offline, counterfactual) | JSONL export |
| 3.2 | CatBoost / ridge: `sizing_mult` shadow | не хуже rule-based на hold-out |
| 3.3 | Promotion отдельно от entry ML | свой gate в trust arbiter |

### Фаза 4 — Согласование с Portfolio

| # | Задача | Критерий |
|---|--------|----------|
| 4.1 | Опционально: `RiskManager.check_position_size` перед GAME_5M BUY | cap by max_position_size_usd |
| 4.2 | Единый отчёт exposure: GAME_5M + Portfolio | SQL / service page |
| 4.3 | Cluster cap: сумма номиналов по CORRELATION cluster | env `GAME_5M_SIZING_CLUSTER_CAP_USD` |

---

## 7. Конфиг (черновик env)

```env
# Money management GAME_5M — docs/GAME_5M_MONEY_MANAGEMENT_PLAN.md
# GAME_5M_SIZING_MODE=log_only          # log_only | apply
# GAME_5M_SIZING_BASE_NOTIONAL_USD=10000
# GAME_5M_SIZING_MIN_NOTIONAL_USD=5000
# GAME_5M_SIZING_MAX_NOTIONAL_USD=15000
# GAME_5M_SIZING_VOL_TARGET_PCT=0.35
# GAME_5M_SIZING_VOL_FLOOR_PCT=0.15
# GAME_5M_SIZING_VOL_SCALE_MIN=0.5
# GAME_5M_SIZING_VOL_SCALE_MAX=1.5
# GAME_5M_SIZING_SENTIMENT_MULT_MAX=1.25
# GAME_5M_SIZING_RISK_PER_TRADE_USD=200
# GAME_5M_SIZING_STOP_FLOOR_PCT=0.5
# GAME_5M_SIZING_USE_CATBOOST_CONFIDENCE=false
# GAME_5M_SIZING_PYRAMID_MODE=half     # same | half | legacy_fixed
```

После apply deprecate прямое использование `GAME_NOTIONAL_USD` в коде → читать из env через sizing module.

---

## 8. Promotion checklist (apply v1)

1. ≥ **20 BUY** с `position_sizing` в shadow, режим `log_only`.  
2. Распределение `final_notional / legacy_notional` без выбросов >1.6× или <0.5× (кроме явного very negative → 0).  
3. Средний `vol_scale` коррелирует с vol (sanity).  
4. Нет регрессии: cron BUY/SELL, VWAP close, analyzer trade_effects.  
5. Ops sign-off + snapshot env на VM.  
6. Обновить [ML_STATUS_REPORT.md](ML_STATUS_REPORT.md) (строка `game5m_position_sizing`).

**Можно совместить календарно** с [predictor plan §14](GAME_5M_PREDICTOR_DATASET_PLAN.md) (**2026-07-14**) или **раньше** — MM не зависит от continuation telemetry.

---

## 9. Мониторинг

| Канал | Что смотреть |
|-------|----------------|
| `/sql` | «BUY sizing shadow diff», «avg vol_scale / sentiment_mult» |
| Analyzer | `game5m_position_sizing_review` |
| BUY `context_json` | `position_sizing.final_notional_usd`, `applied` |

---

## 10. Календарь (ориентир)

| Веха | Дата | Смысл |
|------|------|--------|
| **M0 — план** | **2026-06-20** | этот документ |
| **M1 — shadow deploy** | **2026-06-23+** (первые RTH BUY после деплоя) | log_only, qty legacy |
| **M2 — shadow review** | **2026-07-07** | ≥20 BUY с sizing block |
| **M3 — apply v1** | **2026-07-14** (можно совместить с ML promotion review) | `SIZING_MODE=apply` |
| **M4 — ML sizing** | **2026-Q3** | опционально |

---

## 11. Чеклист (живой)

### Фаза 0
- [x] 0.1 план `GAME_5M_MONEY_MANAGEMENT_PLAN.md`
- [x] 0.2 env block в `config.env.example`
- [x] 0.3 ссылка из predictor plan §16

### Фаза 1 — shadow
- [ ] 1.1 `game5m_position_sizing.py` + tests
- [ ] 1.2 hook `record_entry` + cron path
- [ ] 1.3 SQL presets
- [ ] 1.4 analyzer review block

### Фаза 2 — apply
- [ ] 2.1 prod `SIZING_MODE=apply`
- [ ] 2.2 sign-off
- [ ] 2.3 tuning log + ML_STATUS_REPORT

### Фаза 3–4 — optional
- [ ] 3.x ML sizing
- [ ] 4.x RiskManager / cluster caps

---

## 12. FAQ

**Это заменит фиксированные $10k?**  
В shadow — нет (qty как сейчас). В apply — номинал станет **$5k–$15k** (typical), не всегда ровно 10k.

**Низкий vol + плохие новости?**  
Vol тянет вверх, sentiment внiz — **не** наращиваем слепо; при negative mult 0.6 итог ниже base.

**Почему qty «разный» сейчас?**  
Только цена акции: `floor(10000/price)`. После MM добавится объяснение в JSON.

**Portfolio уже имеет MM — зачем отдельный план?**  
GAME_5M — отдельная paper-игра без `ExecutionAgent`; формулы и virtual capital другие. Фаза 4 — выравнивание caps.

---

*Обновлять при закрытии пунктов; решения apply — строка в [GAME_5M_AGENT_TUNING_LOG.md](GAME_5M_AGENT_TUNING_LOG.md).*
