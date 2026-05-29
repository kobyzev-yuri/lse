# Earnings Intelligence — руководство по странице `/earnings`

Страница: **`/earnings`** (FastAPI → `templates/earnings_intelligence.html`).

Назначение: календарь отчётов → материалы (8-K, transcript) → LLM-факты → peer spillover → ML-слои (advisory). **Не исполняет сделки** и **не заменяет** `/corr` (дневная корреляция котировок).

---

## Быстрая карта вкладок

| Вкладка | Вопрос, на который отвечает | API |
|---------|------------------------------|-----|
| **События** | Что было / что готово по каждому earnings? | `GET /api/earnings/intelligence` |
| **Peer graph** | Кого считаем «пиром» лидера и с каким весом? | `GET /api/earnings/peer-graph` |
| **Spillover** | Как пиры **фактически** отреагировали после отчёта лидера? | `GET /api/earnings/spillover/{symbol}` |
| **Shadow** | Насколько classifier угадывает сценарий **после созревания** 5d? | `GET /api/earnings/shadow-report` |
| **Fusion** | Сводный advisory: регрессия + scenario ML + brief | `GET /api/earnings/fusion/{symbol}` |
| **ML слои** | Статус пайплайна данных и моделей | `GET /api/earnings/ml-layers` |

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
| Scenario | Первый `scenario_hints` или `final_label` |

### Пример (prod, META 2026-04-29)

1. Нажмите **Brief** на строке META.
2. Откроется панель:
   - **Headline:** `META capex / AI infra signal — watch peer spillover`
   - **Scenario:** `capex_positive_for_infra_peers`
   - **Source outcomes:** фактические 1d/5d log-return META из quotes
   - **Peer spillover:** таблица пиров (MU, NVDA, …) с 1d/5d от даты отчёта META

```bash
curl -s 'https://<host>/api/earnings/brief/META?event_date=2026-04-29' | jq '.headline, .scenario, .peer_spillover_outcomes[:3]'
```

### Кнопки в строке

- **Brief** — Event Brief + опционально блок регрессии (см. ограничения ниже).
- **Fusion** — переход на вкладку Fusion с этим тикером и датой.
- **Spillover** — вкладка Spillover с этим source.

---

## 2. Вкладка «Peer graph»

### Что это

Направленный граф **`peer_graph_edge`**: `source → target`, вес 0–1, тип связи (`ai_infra_supply`, `hyperscaler_capex`, …). Задан каталогом + seed (96 рёбер на prod), **не** выучен из котировок.

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

**Event-study:** для каждого **прошлого** earnings source-тикера считаем forward log-return **пиров** от **даты отчёта source**, горизонты 1d и 5d (торговые дни).

### Как пользоваться

1. Выберите source (например **NVDA**).
2. **Загрузить историю** — до 8 последних событий с 2026-01-01.

### Пример (NVDA 2026-05-20, prod)

- Source 5d: фактическая доходность NVDA после отчёта.
- Peers: MU, AMD, … — их 1d/5d от 2026-05-20.
- Scenario/tone из LLM extract того события.

```bash
curl -s 'https://<host>/api/earnings/spillover/NVDA?since=2026-01-01&limit=3' | jq '.events[0] | {date: .event_date, scenario: .top_scenario, source_5d: .source_forward_log_ret_5d}'
```

Если **«Нет past events с spillover»** — нет KB earnings для тикера в окне или нет quotes для пиров.

---

## 4. Вкладка «Shadow»

### Что это

**Live shadow report** — offline-оценка **scenario classifier** на событиях, где уже есть:

- `features_before` с `quotes_regime_earnings_v1`
- созревший `forward_log_ret_5d` в `outcomes_after`

Сравниваем **предсказанный сценарий** vs **факт** (знак 5d return, совпадение класса с LLM label). Считаем **pseudo-PnL** после round-trip transaction costs (default 20 bps × 2).

**Shadow ≠ торговля.** Отчёт advisory; `trading_gate` — внутренний порог качества, не разрешение на сделки.

### Кнопки

- **Загрузить** — JSON с диска (`last_earnings_scenario_shadow.json`, обновляется cron/eval).
- **Пересчитать (refresh)** — полный пересчёт (~20–30 с), `?refresh=true`.

### Пример интерпретации (prod, 2026-05-28)

| Метрика | Значение | Смысл |
|---------|----------|--------|
| Matured | 27 | Событий с features + 5d outcome |
| Sign acc | ~67% | Знак pred vs фактический 5d |
| Class acc | ~87% | Точное имя сценария vs LLM label |
| Mean pseudo PnL (log) | ~+0.027 | Средний «если бы торговали по знаку» минус costs |
| Shadow quality gate | quality ok / below threshold | Пороги `ML_READINESS_EARNINGS_SHADOW_*`; badge **«Shadow quality · advisory only»** — не разрешение на сделки |

Строка таблицы: META · дата · pred scenario · actual 5d · ✓/✗ sign · ✓/✗ class · mean peer 5d.

---

## 5. Вкладка «Fusion»

### Что это

**Advisory bundle** для одного события:

1. **Regression ML** — CatBoost `forward_log_ret_5d` (`quotes_regime_v1`, product-модель).
2. **Scenario ML** — multi-class classifier (`quotes_regime_earnings_v1`).
3. **Advisory** — alignment / conviction; **`execution_blocked: true` всегда**.

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
  "scenario_ml": { "predicted_scenario": "gap_up_follow_through", "scenario_classifier_status": "ok" }
}
```

### Пример «пустых» данных (DELL 2026-05-28, день отчёта)

- `management_tone`: null — extract ещё не прошёл или нет materials.
- `source_outcomes.forward_log_ret_5d`: null — горизонт не созрел.
- Scenario ML может быть `no_features` если нет earnings_v1 backfill на эту дату.

**Не баг UI** — нехватка данных по свежему событию.

---

## 6. Вкладка «ML слои»

Справочник статусов пайплайна (не прогноз по тикеру).

| Слой | Статус (prod) | Роль |
|------|---------------|------|
| `quotes_regime_earnings_v1` | active | Признаки для scenario classifier |
| CatBoost регрессия | active | Product advisory; `/api/ml/event-reaction/{ticker}?event_date=YYYY-MM-DD` в Brief |
| UP/DOWN/FLAT | active | Правило по порогу на фактическом 5d |
| LLM scenario hints | active | Extract → `earnings_event_detail` |
| Scenario classifier | active (pilot) | Multi-class, мало labels (~15) |

Prod snapshot: ~498 rows dataset, ~267 earnings_v1 features, ~15 LLM scenario labels applied.

---

## Spillover vs Shadow (кратко)

| | Spillover | Shadow |
|---|-----------|--------|
| **Вопрос** | Как **пиры** отреагировали на отчёт **лидера**? | Насколько **classifier** угадал сценарий/знак? |
| **Данные** | Quotes пиров от event_date | features + matured 5d source |
| **UI** | Spillover tab | Shadow tab |
| **Trading** | Описание рынка | Метрика качества ML |

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

- [EARNINGS_INTELLIGENCE_PLAN.md](./EARNINGS_INTELLIGENCE_PLAN.md) — архитектура и roadmap
- [EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md](./EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md) — MVP event_reaction
- [EVENT_REACTION_PIPELINE.md](../EVENT_REACTION_PIPELINE.md) — регреssion prod path

## Cron (prod)

| Время | Скрипт |
|-------|--------|
| :18 / :20 / :25 каждые 2–6 ч | sync / ingest / extract materials |
| :30 */6 | `run_earnings_ml_refresh.py` (dry-run по умолчанию) |
| 23:52 пн–пт | full earnings grid train |

Полный eval: `ML_READINESS_TRAIN_MODE=full python3 scripts/run_earnings_intelligence_prod_eval.py`

**ML-слои (ridge vs event regression vs classifier):** [TRADE_ML_DATASETS_AND_TARGETS_RU.md](../TRADE_ML_DATASETS_AND_TARGETS_RU.md) §4–§7.

**План ближайшей сессии:** [EARNINGS_PLAN_2026-05-29.md](./EARNINGS_PLAN_2026-05-29.md)
