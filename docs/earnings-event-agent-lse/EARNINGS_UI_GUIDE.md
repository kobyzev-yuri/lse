# Earnings Intelligence — руководство по странице `/earnings`

Страница: **`/earnings`** (FastAPI → `templates/earnings_intelligence.html`).

**Словарь терминов:** [ML_GLOSSARY_RU.md](../ML_GLOSSARY_RU.md) — BMO/AMH, spillover (кросс-влияние на peers), shadow, advisory, event 5d vs open-path. Earnings-лексика — [EARNINGS_EVENT_AGENT_DESIGN.md](./EARNINGS_EVENT_AGENT_DESIGN.md) §10.

Назначение: календарь отчётов → материалы (8-K, transcript) → LLM-факты → **peer spillover** (реакция аналогов) → ML-слои (**advisory** — подсказка без автоблока). **Не исполняет сделки** (`execution_blocked: true`) и **не заменяет** `/corr` (дневная корреляция котировок).

---

## Быстрая карта вкладок

| Вкладка | Вопрос, на который отвечает | API |
|---------|------------------------------|-----|
| **События** | Что было / что готово по каждому earnings? LLM vs ML **scenario** (сценарий) в таблице | `GET /api/earnings/intelligence` |
| **Peer graph** | Кого считаем «пиром» (аналогом) лидера и с каким **weight** (весом)? | `GET /api/earnings/peer-graph` |
| **Spillover** | Факт 1d/5d пиров + **ML pred 5d**; якорь peer-календаря (BMO/AMH) | `GET /api/earnings/spillover/{symbol}` |
| **Shadow** | Насколько **classifier** (классификатор) угадывает сценарий **после созревания** 5d? | `GET /api/earnings/shadow-report` |
| **Fusion** | Сводный advisory (подсказка): регрессия + scenario ML + brief | `GET /api/earnings/fusion/{symbol}` |
| **ML слои** | Статус пайплайна данных и моделей (L1/L2) | `GET /api/earnings/ml-layers` |

Event Brief (деталь события): `GET /api/earnings/brief/{symbol}?event_date=YYYY-MM-DD` — открывается кнопкой **Brief** на вкладке События.

---

## 1. Вкладка «События»

### Что видите

- **Summary:** universe (21 equity), сколько тикеров с materials, сколько событий с LLM, число строк в таблице.
- **Строка «Без материалов»:** тикеры universe, у которых **нет ни одного** parsed/extracted material (не «нет на это событие»).
- **Таблица:** каждая строка = одно событие `knowledge_base` (EARNINGS) с датой ≤ сегодня.
- **Фильтры:** Все / GAME_5M / Portfolio — по группе тикера в config.

### Колонки

| Колонка | Значение |
|---------|----------|
| Materials | Число полезных материалов (`parsed` / `extracted`) на **эту дату** |
| LLM | `yes` если есть `management_tone` в `earnings_event_detail` |
| Tone | bullish / bearish / neutral (из LLM) |
| **LLM scenario** | Hint из extract (`scenario_hints`) или `final_label` — **не** CatBoost |
| **ML scenario** | Pred **scenario classifier** (`quotes_regime_earnings_v1`); **proba** (вероятность) в UI; «—» = нет features / модель off |

> **LLM vs ML scenario:** LLM читает transcript/8-K; ML — табличный CatBoost по `features_before`. Оба advisory; расхождение нормально на pilot-объёме labels.

### Пример (prod, META 2026-04-29)

1. Нажмите **Brief** на строке META.
2. Откроется панель:
   - **Headline:** `META capex / AI infra signal — watch peer spillover`
   - **LLM vs ML scenario:** две колонки — hint из extract и pred CatBoost classifier (proba, expected source sign)
   - **Source outcomes:** фактические 1d/5d **log-return** (лог-доходность) META из quotes; якорь source — BMO → close T−1, AMH → close T ([ML_GLOSSARY_RU.md](../ML_GLOSSARY_RU.md) §4.2)
   - **Peer spillover:** таблица пиров — **1d fact · 5d fact · 5d ML · Sign** (совпадение знака fact vs CatBoost pred); fact 5d peer — от `peer_outcome_anchor_date`, не от календарной даты отчёта source

```bash
curl -s 'https://<host>/api/earnings/brief/META?event_date=2026-04-29' | jq '.scenario.id, .scenario_ml.predicted_scenario, [.peer_spillover_ml[]|select(.peer_spillover_ml_status=="ok")|{peer:.peer_ticker,pred:.peer_forward_log_ret_5d_pred}][:3]'
```

### Кнопки в строке

- **Brief** — Event Brief + опционально блок регрессии (см. ограничения ниже).
- **Fusion** — переход на вкладку Fusion с этим тикером и датой.
- **Spillover** — вкладка Spillover с этим source.

---

## 2. Вкладка «Peer graph»

### Что это

Направленный граф **`peer_graph_edge`**: `source → target`, вес 0–1, тип связи (`ai_infra_supply`, `ai_infra_customer`, …). Задан каталогом + seed (**69 рёбер**, 16 sources) — см. [PEER_GRAPH_PRINCIPLES.md](./PEER_GRAPH_PRINCIPLES.md); **не** выучен из котировок.

### Как читать

- **Sources (out-degree):** NVDA → 18 пиров — «лидеры» с наибольшим числом исходящих рёбер.
- **Edges:** список рёбер; ширина полоски ≈ weight. Показаны **первые 40** из полного списка.
- Клик по source → Spillover history для этого тикера.

### Пример

```
NVDA → MU   (ai_infra_supply, weight 0.9)
META → NVDA (hyperscaler_capex, weight 0.85)
```

**Не путать с `/corr`:** граф — структурная гипотеза «кто связан с отчётом»; корреляция — скользящая статистика цен.

---

## 3. Вкладка «Spillover»

### Что это

**Event-study** (исследование реакции на событие): для каждого **прошлого** earnings **source**-тикера — forward **log-return** пиров и **CatBoost pred 5d** (`peer_spillover_ml`).

**Важно — якорь peer (не путать с датой отчёта в таблице):**

| Source phase | Отчёт source | Peer торгует реакцию | Fact 5d peer считается от |
|--------------|--------------|----------------------|---------------------------|
| **BMO** (before market open) | Tue | Tue open | **Mon close** peer |
| **AMH** (after close) | Tue | Wed open | **Tue close** peer |

В API/Brief поле **`peer_outcome_anchor_date`** — дата этого якоря. Колонка **event_date** в UI — календарь KB; forward returns **не** «от event_date напрямую» для peer.

**Пример — NVDA BMO Tue → AMD:**

```text
event_date в UI: Tue (дата отчёта NVDA)
source_market_phase: BMO
AMD forward_log_ret_5d: от Mon close AMD (peer_outcome_anchor_date = Mon)
```

Подробнее: [ML_GLOSSARY_RU.md](../ML_GLOSSARY_RU.md) §4.3, [EVENT_REACTION_PIPELINE.md](../EVENT_REACTION_PIPELINE.md).

### Как пользоваться

1. Задайте **Контекст** (тикер + дата) — сверху блок «Контекст · …» с LLM/ML scenario и peer ML table.
2. **Загрузить историю** — до 8 последних событий с 2026-01-01; в шапке каждого event: LLM scenario, ML scenario, source 5d.

### Пример (META 2026-04-29, prod `be72376`)

- LLM: `capex_positive_for_infra_peers` · ML classifier: то же, proba ~85%
- Peer table: MU fact 5d vs ML pred; колонка **Sign** ✓/✗
- API: 16 peer ML preds со status `ok`

```bash
curl -s 'https://<host>/api/earnings/spillover/META?limit=1' | jq '.events[0] | {date:.event_date, llm:.top_scenario, ml:.scenario_ml.predicted_scenario, peer_ml:([.peer_spillover_ml[]|select(.peer_spillover_ml_status=="ok")]|length)}'
```

Если **«Нет past events с spillover»** — нет KB earnings для тикера в окне или нет quotes для пиров.

---

## 4. Вкладка «Shadow»

### Что это

**Live shadow report** — offline-оценка **scenario classifier** на событиях source (не peer), где уже есть:

- `features_before` с `quotes_regime_earnings_v1`
- созревший `forward_log_ret_5d` в `outcomes_after` (event 5d — горизонт ~5 торговых дней)

Сравниваем **предсказанный сценарий** vs **факт** (знак 5d return, совпадение класса с LLM label). Считаем **pseudo-PnL** (условный PnL «если бы торговали») после round-trip **transaction costs** (издержки сделки, default 20 bps × 2).

**Shadow** (только лог/метрики) **≠ торговля.** Отчёт advisory; `trading_gate` — внутренний порог качества, не разрешение на сделки.

> **Shadow vs Spillover:** Shadow — качество classifier по **source**; Spillover — fact/pred по **peers**. См. таблицу в конце документа.

### Кнопки

- **Загрузить** — JSON с диска (`last_earnings_scenario_shadow.json`, обновляется cron/eval).
- **Пересчитать (refresh)** — полный пересчёт (~20–30 с), `?refresh=true`.

### Пример интерпретации (prod, 2026-06-07 после full refresh)

| Метрика | Значение | Смысл |
|---------|----------|--------|
| Matured | ~41 | Событий с features + созревший 5d outcome |
| Sign acc | ~70% | **Sign accuracy** — знак pred vs фактический 5d source |
| Class acc | ~56% (train valid) | Точное имя сценария vs LLM label; мало labels (~33) |
| Mean pseudo PnL (log) | см. JSON | Средний «если бы торговали по знаку» минус costs |
| Shadow quality gate | quality ok / below threshold | Пороги `ML_READINESS_EARNINGS_SHADOW_*`; badge **«Shadow quality · advisory only»** |

Строка таблицы: META · дата · pred scenario · actual 5d · ✓/✗ sign · ✓/✗ class · mean peer 5d.

---

## 5. Вкладка «Fusion»

### Что это

**Advisory bundle** (сводка-подсказка) для одного события:

1. **Regression ML** — CatBoost `forward_log_ret_5d` (**event 5d** — прогноз на ~5 торговых дней; `quotes_regime_v1`, product-модель).
2. **Scenario ML** — multi-class **classifier** (`quotes_regime_earnings_v1`); блок **LLM vs ML scenario**.
3. **Peer spillover ML** — таблица fact vs pred 5d по каждому peer из графа (якорь peer — §3).
4. **Advisory** — alignment / conviction; **`execution_blocked: true` всегда** (бот не торгует по Fusion).

### Как пользоваться

Выберите ticker → **Fusion advisory**. Из таблицы Событий кнопка **Fusion** передаёт **event_date**.

### Пример (NVDA 2026-05-20)

```json
{
  "advisory": {
    "summary": "regression 5d log-ret +0.0123 · scenario gap_up_follow_through",
    "alignment": "aligned_or_weak",
    "conviction": "medium",
    "execution_blocked": true
  },
  "regression_ml": { "forward_log_ret_5d_pred": 0.012 },
  "scenario_ml": { "predicted_scenario": "gap_up_follow_through", "scenario_classifier_status": "ok" },
  "peer_spillover_ml": [{ "peer_ticker": "MU", "peer_spillover_ml_status": "ok", "peer_forward_log_ret_5d_pred": 0.05 }]
}
```

### Пример «пустых» данных (DELL 2026-05-28, день отчёта)

- `management_tone`: null — extract ещё не прошёл или нет materials.
- `source_outcomes.forward_log_ret_5d`: null — горизонт не созрел.
- Scenario ML может быть `no_features` если нет earnings_v1 backfill на эту дату.

**Не баг UI** — нехватка данных по свежему событию.

---

## 6. Вкладка «ML слои»

Справочник статусов пайплайна (не прогноз по тикеру). Термины L1/L2 — [ML_GLOSSARY_RU.md](../ML_GLOSSARY_RU.md) §1.

| Слой | Статус (prod) | Роль |
|------|---------------|------|
| `quotes_regime_earnings_v1` | active | **Feature builder** — признаки для scenario classifier |
| CatBoost регрессия | active | Product advisory (**event 5d**); `/api/ml/event-reaction/{ticker}?event_date=YYYY-MM-DD` в Brief |
| UP/DOWN/FLAT | active | Rule-метка по **vol-scaled** порогу на фактическом 5d |
| LLM scenario hints | active | Extract → `earnings_event_detail` |
| Scenario classifier | active | Multi-class; Events / Brief / Fusion / Spillover |
| Peer spillover ML | active | CatBoost **regressor** per peer; Brief / Spillover / Fusion |

Prod snapshot (2026-06-07): ~471 ERD backfill; earnings_v1 features; **33** LLM scenario labels (autoprep gate **≥40**); peer spillover **188** train rows, sign acc valid **≈85%**; shadow **41** matured, sign **≈70%**. `overall_grid_ready` ✅, `overall_earnings_autoprep_ready` ❌.

> **Event 5d vs open-path:** open-path classifier **не** на этой странице — shadow в GAME_5M контуре. См. [TRADE_ML_DATASETS_AND_TARGETS_RU.md](../TRADE_ML_DATASETS_AND_TARGETS_RU.md) §0.

---

## Spillover vs Shadow (кратко)

| | Spillover | Shadow |
|---|-----------|--------|
| **Вопрос** | Как **пиры** (аналоги) отреагировали (fact) и что pred **spillover ML**? | Насколько **classifier** угадал сценарий/знак **source**? |
| **Объект** | Peer-тикеры (MU, AMD, …) | Source-тикер отчёта (NVDA, META, …) |
| **Якорь** | `peer_outcome_anchor_date` (BMO/AMH) | leak-safe source anchor (BMO → T−1) |
| **Данные** | Quotes пиров + CatBoost pred | features + matured 5d source |
| **UI** | Spillover tab (fact + ML cols) | Shadow tab |
| **Trading** | Описание рынка + advisory pred | Метрика качества ML |

---

## Аудит: заглушки и неполная реализация (2026-05-29)

Проверка: smoke всех API на prod (`lse-bot`).

| Элемент | Статус | Что происходит |
|---------|--------|----------------|
| Таблица событий | ✅ prod | Реальные KB + materials + LLM флаги |
| Event Brief панель | ✅ P0 | evidence, guidance, capex, affected, status partial |
| Brief → CatBoost regression | ✅ P0 | `?event_date=` в API и UI; явное сообщение если нет features |
| Peer graph | ✅ P0 | «Показано 40 из N» рёбер |
| Spillover dropdown | ✅ P0 | Source = union(events, graph sources) |
| Shadow gate label | ✅ P0 | «Shadow quality · advisory only», gate → «Shadow quality gate» |
| Shadow refresh | ✅ | Работает; долгий запрос без progress bar |
| Fusion | ✅ advisory | `execution_blocked` всегда; regression/scenario могут быть partial |
| Fusion без event_date | ⚠️ | Берёт **последнюю** KB дату (вкл. сегодня) — для DELL в день отчёта LLM пустой |
| ML layers | ⚠️ P1 | **Нет** строк про Shadow/Fusion/readiness JSON |
| Scenario classifier | ⚠️ pilot | ~3 класса в модели при большем числе LLM scenario types |
| Materials gaps | ⚠️ data | ~24% universe без materials; future dates — SEC после отчёта |
| Fool ingest | ⚠️ ops | 429 rate limit; junk URLs в discover-links (ARM) |
| Связь с GAME_5M | ❌ by design | Нет auto-signal в бот; fusion advisory only |

**Итого P0 закрыт (2026-05-29).** Остаётся P1: materials coverage, ML layers tab, свежие события без LLM/outcomes.

---

## План доведения `/earnings` до production-quality

Приоритеты: P0 = пользователь видит правду; P1 = полнота данных; P2 = trading path (отдельное решение).

### P0 — UI / API correctness ✅ (2026-05-29)

| # | Задача | Статус |
|---|--------|--------|
| 1 | Brief: `event_date` в `/api/ml/event-reaction` | ✅ |
| 2 | Brief: evidence, guidance, capex, partial | ✅ |
| 3 | Shadow: «Shadow quality · advisory only» | ✅ |
| 4 | Peer graph: «40 из N» | ✅ |
| 5 | Spillover: union(events, graph) | ✅ |

### P1 — Data coverage (ongoing, cron)

| # | Задача | Результат |
|---|--------|-----------|
| 6 | Materials: Fool rate limit backoff; фильтр junk discover-links | Меньше failed ingest |
| 7 | Auto KB ensure + SEC 8-K на день отчёта | DELL/AVGO/… после earnings |
| 8 | Extract cron в день отчёта + `--symbols` universe | LLM tone/scenario в день D |
| 9 | Накопление labels/outcomes → nightly `run_earnings_ml_refresh` full | Растёт shadow n_matured |
| 10 | ML layers tab: добавить Shadow, Fusion, readiness paths | Одна точка статуса |

### P2 — Advisory → product (после N≥40 shadow)

| # | Задача | Критерий входа |
|---|--------|----------------|
| 11 | Peer spillover как feature в train | Стабильный sign acc на shadow |
| 12 | Расширить classes classifier | ≥30 LLM labels, ≥5 классов |
| 13 | Portfolio cards: блок «last earnings fusion» read-only | Shadow gate + manual approval |
| 14 | GAME_5M integration | Отдельный backtest + transaction costs |

### P3 — Ops

| # | Задача |
|---|--------|
| 15 | `run_earnings_intelligence_pipeline.py` в cron (или merge в prod_eval step) |
| 16 | E2E test script: все `/api/earnings/*` + assert non-empty META/NVDA fixtures |
| 17 | Telegram `/earnings` parity с web Brief (evidence snippets) |

---

## Связанные документы

- [ML_GLOSSARY_RU.md](../ML_GLOSSARY_RU.md) — словарь ML, BMO/AMH, spillover, shadow
- [EARNINGS_INTELLIGENCE_PLAN.md](./EARNINGS_INTELLIGENCE_PLAN.md) — архитектура и roadmap
- [EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md](./EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md) — MVP event_reaction
- [EVENT_REACTION_PIPELINE.md](../EVENT_REACTION_PIPELINE.md) — regression prod path, якоря, vol-scaled
- [TRADE_ML_DATASETS_AND_TARGETS_RU.md](../TRADE_ML_DATASETS_AND_TARGETS_RU.md) §0 — event 5d vs open-path

## Cron (prod)

| Время | Скрипт |
|-------|--------|
| :18 / :20 / :25 каждые 2–6 ч | sync / ingest / extract materials |
| :30 */6 | `run_earnings_ml_refresh.py` (dry-run по умолчанию) |
| 23:52 пн–пт | full earnings grid train |

Полный eval: `ML_READINESS_TRAIN_MODE=full python3 scripts/run_earnings_intelligence_prod_eval.py`

**ML-слои (ridge vs event regression vs classifier):** [TRADE_ML_DATASETS_AND_TARGETS_RU.md](../TRADE_ML_DATASETS_AND_TARGETS_RU.md) §4–§7, §0.

**Ops-статус контуров:** [ML_STATUS_REPORT.md](../ML_STATUS_REPORT.md).
