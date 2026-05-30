# Peer graph v0: принципы построения и каталог рёбер

> **Связанные документы:** [EARNINGS_INTELLIGENCE_PLAN.md](./EARNINGS_INTELLIGENCE_PLAN.md) · [EARNINGS_EVENT_AGENT_DESIGN.md](./EARNINGS_EVENT_AGENT_DESIGN.md) §4.3 · [EARNINGS_UI_GUIDE.md](./EARNINGS_UI_GUIDE.md) (Peer graph / Spillover) · [EARNINGS_LLM_ML_LABELS_AND_TRAINING.md](./EARNINGS_LLM_ML_LABELS_AND_TRAINING.md) · [DATABASE_SCHEMA.md](../DATABASE_SCHEMA.md) (`peer_graph_edge`) · код: [`services/peer_graph_catalog.py`](../../services/peer_graph_catalog.py)

**Версия каталога:** MVP v0 · **69 рёбер** · **16 лидеров (source)** · дата документа: 2026-05-30

---

## 1. Зачем нужен peer graph

Earnings одного **лидера** (hyperscaler, GPU, memory, equipment) часто двигает не только его котировку, но и **группу связанных эмитентов**. Классический кейс MVP:

> **META** падает на опасениях по capex, но **MU / SNDK / AMD / LITE** получают **позитивный spillover** — рынок читает рост AI-infra spend как спрос на memory, storage, compute и networking.

Peer graph отвечает на вопрос: **«кого считать affected ticker’ом после отчёта source?»** — до того, как смотрим фактические исходы (Spillover) или строим ML-признаки.

Это **не** замена `/corr` (корреляция котировок) и **не** ML-кластеризация. Граф задаёт **экспертную гипотезу supply chain / тематического влияния** на дневном горizonte earnings.

---

## 2. Принципы построения графа

### 2.1. Направленность: source → target

Ребро **направленное**: `source_ticker` — эмитент, чей **отчёт / call** является событием; `target_ticker` — эмитент, на которого ожидаем **кросс-влияние** (spillover).

- Brief и Spillover API всегда работают от **выбранного source**.
- Обратные связи (например MU → NVDA и NVDA → MU) **допустимы**, если экономический смысл разный: NVDA earnings → memory demand vs MU earnings → GPU customer sentiment.

### 2.2. Тематический фокус MVP: AI infra / chips

Universe v0 покрывает GAME_5M + portfolio + extras из [`earnings_intelligence_universe.py`](../../services/earnings_intelligence_universe.py). Граф сфокусирован на:

| Кластер | Примеры source | Типичные targets |
|---------|----------------|------------------|
| Hyperscaler capex | META, MSFT, AMZN | NVDA, MU, AMD, LITE |
| GPU / compute leader | NVDA, AMD | MU, ASML, ARM, hyperscalers |
| Memory cycle | MU, SNDK | NVDA, peers памяти |
| Semi equipment | ASML, TER | NVDA, AMD, INTC |
| Networking | LITE, CIEN, ALAB | NVDA, META |

### 2.3. relation_type — семантика связи

Тип ребра фиксирует **механизм** влияния (не корреляцию):

| relation_type | Смысл |
|---------------|-------|
| `ai_infra_supply` | Source сигнализирует спрос на infra у target (capex, capacity) |
| `ai_infra_customer` | Target — заказчик GPU/infra source |
| `ai_infra_peer` | Сопоставимый игрок той же темы (compute, AI infra) |
| `memory_peer` | Прямой пир в цикле памяти |
| `cpu_peer` | CPU-конкуренты (AMD ↔ INTC) |
| `cloud_peer` / `megacap_peer` | Мегакап / облако без прямого supply chain |
| `semi_equipment` | Test & measurement / litho equipment chain |
| `networking_peer` | Оптика / connectivity |
| `enterprise_peer` | Enterprise software (ORCL ↔ MSFT) |
| `sector_etf` | ETF (SMH, SOXX) — **в ML/aggregate**, не equity в Brief spillover table |

### 2.4. weight — сила экспертной гипотезы

`weight` ∈ **[0.5, 0.9]** — **ручная** оценка силы связи для MVP:

- **0.85–0.90** — ключевой supply chain (META→MU, NVDA→MU, AMD→NVDA)
- **0.70–0.80** — сильный, но вторичный канал
- **0.50–0.65** — слабее / косвенный peer

Weight **не** калибруется по историческим corr на этапе v0. План Phase B+: weighted spillover score для validation ([EARNINGS_PRODUCT_ROADMAP.md](./EARNINGS_PRODUCT_ROADMAP.md) B9).

### 2.5. Что сознательно не входит в v0

- **Полный сектор** — только curated edges, не «все из TICKER_GROUPS».
- **Динамические веса** — `valid_from` / `valid_to` в схеме есть, в каталоге MVP = open-ended.
- **Bidirectional merge** — каждое направление задаётся явно.
- **Котировочная корреляция** — отдельный контур `/corr`.

### 2.6. Критерий «хорошего» ребра

1. **Объяснимость:** можно описать механизм в одном предложении (см. `meta.mvp_case` в каталоге).
2. **Actionable для Brief:** после earnings source пользователь понимает, **кого смотреть** в Spillover.
3. **Проверяемость:** по истории event_reaction_dataset можно сравнить sign(source 5d) vs sign(peer 5d).
4. **Согласованность с LLM hints:** `affected_tickers` из extract должны пересекаться с graph (не обязаны совпадать полностью).

---

## 3. Источник истины и пайплайн

```
peer_graph_catalog.py (PEER_GRAPH_EDGES)
        │
        ▼
scripts/seed_peer_graph_edges.py  ──►  PostgreSQL peer_graph_edge
        │
        ├── earnings_event_brief.py     → peer_spillover_outcomes (1d/5d log-ret)
        ├── /api/earnings/peer-graph    → UI вкладка Peer graph
        ├── /api/earnings/spillover/{symbol} → UI вкладка Spillover
        └── quotes_regime_earnings_v1   → peer_graph_out_degree, peer momentum
```

**Правило:** правки графа — только через `PEER_GRAPH_EDGES` + seed + deploy. Не править `peer_graph_edge` вручную на prod без коммита в каталог.

---

## 4. Сводка: лидер → число пиров

| Source (лидер) | Пиров | Главный кейс |
|----------------|-------|--------------|
| **ALAB** | 3 | поставщик AI-infra (memory, optics, equipment) |
| **AMD** | 6 | пир AI-infra / compute |
| **AMZN** | 5 | заказчик AI-infra (hyperscaler/GPU) |
| **ARM** | 3 | пир AI-infra / compute |
| **ASML** | 3 | заказчик AI-infra (hyperscaler/GPU) |
| **CIEN** | 2 | networking-пир |
| **INTC** | 3 | CPU-конкурент |
| **LITE** | 2 | поставщик AI-infra (memory, optics, equipment) |
| **META** | 8 | META capex -> memory |
| **MSFT** | 7 | заказчик AI-infra (hyperscaler/GPU) |
| **MU** | 4 | заказчик AI-infra (hyperscaler/GPU) |
| **NBIS** | 2 | пир AI-infra / compute |
| **NVDA** | 11 | NVDA demand -> HBM/memory |
| **ORCL** | 2 | enterprise software |
| **SNDK** | 4 | пир AI-infra / compute |
| **TER** | 4 | полупроводниковое оборудование |

Полные таблицы — ниже. ETF-цели (SMH, SOXX) учитываются в агрегатах, но не как equity peers в Brief.

---

## 5. Каталог рёбер по лидерам

*Автогенерация из `PEER_GRAPH_EDGES` — при изменении каталога пересоберите этот раздел или сверьте с кодом.*

### ALAB (3 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| NVDA | 0.70 | `ai_infra_supply` | поставщик AI-infra (memory, optics, equipment) |
| AMD | 0.65 | `ai_infra_supply` | поставщик AI-infra (memory, optics, equipment) |
| META | 0.55 | `ai_infra_customer` | заказчик AI-infra (hyperscaler/GPU) |

### AMD (6 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| NVDA | 0.90 | `ai_infra_peer` | пир AI-infra / compute |
| MU | 0.80 | `ai_infra_supply` | поставщик AI-infra (memory, optics, equipment) |
| INTC | 0.75 | `cpu_peer` | CPU-конкурент |
| ASML | 0.65 | `ai_infra_supply` | поставщик AI-infra (memory, optics, equipment) |
| LITE | 0.55 | `ai_infra_supply` | поставщик AI-infra (memory, optics, equipment) |
| TER | 0.50 | `semi_equipment` | полупроводниковое оборудование |

### AMZN (5 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| NVDA | 0.80 | `ai_infra_customer` | заказчик AI-infra (hyperscaler/GPU) |
| MSFT | 0.70 | `cloud_peer` | облачный мегакап-пир |
| MU | 0.70 | `ai_infra_supply` | поставщик AI-infra (memory, optics, equipment) |
| AMD | 0.65 | `ai_infra_peer` | пир AI-infra / compute |
| ORCL | 0.50 | `enterprise_peer` | enterprise software |

### ARM (3 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| AMD | 0.65 | `ai_infra_peer` | пир AI-infra / compute |
| NVDA | 0.60 | `ai_infra_peer` | пир AI-infra / compute |
| QCOM | 0.55 | `ai_infra_peer` | пир AI-infra / compute |

### ASML (3 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| NVDA | 0.65 | `ai_infra_customer` | заказчик AI-infra (hyperscaler/GPU) |
| AMD | 0.60 | `ai_infra_customer` | заказчик AI-infra (hyperscaler/GPU) |
| INTC | 0.55 | `ai_infra_customer` | заказчик AI-infra (hyperscaler/GPU) |

### CIEN (2 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| LITE | 0.65 | `networking_peer` | networking-пир |
| META | 0.50 | `ai_infra_customer` | заказчик AI-infra (hyperscaler/GPU) |

### INTC (3 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| AMD | 0.75 | `cpu_peer` | CPU-конкурент |
| NVDA | 0.55 | `ai_infra_peer` | пир AI-infra / compute |
| TER | 0.50 | `semi_equipment` | полупроводниковое оборудование |

### LITE (2 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| NVDA | 0.60 | `ai_infra_supply` | поставщик AI-infra (memory, optics, equipment) |
| META | 0.55 | `ai_infra_customer` | заказчик AI-infra (hyperscaler/GPU) |

### META (8 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| MU | 0.85 | `ai_infra_supply` | META capex -> memory |
| NVDA | 0.80 | `ai_infra_peer` | пир AI-infra / compute |
| SNDK | 0.75 | `ai_infra_supply` | META capex -> storage |
| AMD | 0.70 | `ai_infra_peer` | META capex -> compute peers |
| LITE | 0.65 | `ai_infra_supply` | META capex -> networking |
| ASML | 0.60 | `ai_infra_supply` | поставщик AI-infra (memory, optics, equipment) |
| ARM | 0.55 | `ai_infra_peer` | пир AI-infra / compute |
| INTC | 0.50 | `ai_infra_peer` | пир AI-infra / compute |

### MSFT (7 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| NVDA | 0.85 | `ai_infra_customer` | заказчик AI-infra (hyperscaler/GPU) |
| MU | 0.75 | `ai_infra_supply` | поставщик AI-infra (memory, optics, equipment) |
| AMD | 0.70 | `ai_infra_peer` | пир AI-infra / compute |
| AMZN | 0.70 | `cloud_peer` | облачный мегакап-пир |
| META | 0.65 | `megacap_peer` | мегакап-пир |
| LITE | 0.60 | `ai_infra_supply` | поставщик AI-infra (memory, optics, equipment) |
| ORCL | 0.55 | `enterprise_peer` | enterprise software |

### MU (4 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| NVDA | 0.85 | `ai_infra_customer` | заказчик AI-infra (hyperscaler/GPU) |
| SNDK | 0.80 | `memory_peer` | пир памяти |
| AMD | 0.70 | `ai_infra_customer` | заказчик AI-infra (hyperscaler/GPU) |
| LITE | 0.55 | `ai_infra_supply` | поставщик AI-infra (memory, optics, equipment) |

### NBIS (2 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| NVDA | 0.70 | `ai_infra_peer` | пир AI-infra / compute |
| SNDK | 0.60 | `ai_infra_peer` | пир AI-infra / compute |

### NVDA (11 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| MU | 0.90 | `ai_infra_supply` | NVDA demand -> HBM/memory |
| AMD | 0.85 | `ai_infra_peer` | пир AI-infra / compute |
| SMH | 0.85 | `sector_etf` | ETF сектора (не equity в Brief) |
| SOXX | 0.85 | `sector_etf` | ETF сектора (не equity в Brief) |
| META | 0.80 | `ai_infra_customer` | заказчик AI-infra (hyperscaler/GPU) |
| ASML | 0.75 | `ai_infra_supply` | поставщик AI-infra (memory, optics, equipment) |
| MSFT | 0.75 | `ai_infra_customer` | заказчик AI-infra (hyperscaler/GPU) |
| ARM | 0.70 | `ai_infra_peer` | пир AI-infra / compute |
| SNDK | 0.70 | `ai_infra_supply` | поставщик AI-infra (memory, optics, equipment) |
| LITE | 0.65 | `ai_infra_supply` | поставщик AI-infra (memory, optics, equipment) |
| INTC | 0.55 | `ai_infra_peer` | пир AI-infra / compute |

### ORCL (2 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| NVDA | 0.60 | `ai_infra_customer` | заказчик AI-infra (hyperscaler/GPU) |
| MSFT | 0.55 | `enterprise_peer` | enterprise software |

### SNDK (4 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| MU | 0.80 | `ai_infra_peer` | пир AI-infra / compute |
| NVDA | 0.65 | `ai_infra_customer` | заказчик AI-infra (hyperscaler/GPU) |
| LITE | 0.60 | `ai_infra_supply` | поставщик AI-infra (memory, optics, equipment) |
| NBIS | 0.55 | `ai_infra_peer` | пир AI-infra / compute |

### TER (4 пиров)

| Peer | weight | relation_type | Комментарий |
|------|--------|---------------|-------------|
| ASML | 0.65 | `semi_equipment` | полупроводниковое оборудование |
| NVDA | 0.60 | `semi_equipment` | полупроводниковое оборудование |
| AMD | 0.55 | `semi_equipment` | полупроводниковое оборудование |
| INTC | 0.50 | `semi_equipment` | полупроводниковое оборудование |

---

## 6. Использование в продуктах

| Контур | Как использует graph |
|--------|----------------------|
| **Event Brief** | `affected_tickers` + forward log-ret peers после event_date |
| **Spillover tab** | История: как targets ходили после прошлых earnings source |
| **Fusion / Shadow** | Scenario vs факт; mean peer 5d как validation |
| **ML quotes_regime_earnings_v1** | `peer_graph_out_degree`, `peer_graph_weight_sum`, peer momentum по топ-N соседям |

---

## 7. Эволюция (roadmap)

| Фаза | Задача |
|------|--------|
| **v0 (сейчас)** | Hand-curated catalog, seed, Brief/Spillover/UI |
| **B+** | Weighted spillover validation metric; расширение universe (GOOGL, ANET, …) |
| **C** | Частичная автоматизация: LLM `affected_tickers` → candidate edges → human review |
| **D** | Time-varying weights (`valid_from`/`valid_to`) при смене business mix |

См. также [EARNINGS_PRODUCT_ROADMAP.md](./EARNINGS_PRODUCT_ROADMAP.md) WS3 (peer graph) и WS6 (Spillover).
