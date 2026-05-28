---
name: earnings intelligence
overview: "Добавить поверх текущего event-reaction ML слой earnings intelligence: сбор материалов отчёта/call, структурированное извлечение фактов, peer/cross-impact и сценарный вывод для UI/бота. Это закроет задачу шефа про earnings call, guidance, capex и влияние отчёта одного эмитента на группу."
todos:
  - id: materials-schema
    content: "Спроектировать таблицу или JSON-структуру для earnings materials: press release, presentation, transcript, follow-up, source URLs."
    status: completed
  - id: extraction-schema
    content: "Зафиксировать LLM extraction JSON для call/report: guidance, capex, tone, Q&A concerns, affected tickers, scenario hints."
    status: completed
  - id: peer-graph-v0
    content: Задать peer_graph_edge v0 для AI/chips/infrastructure связей и META/NVDA/ASML/ARM/SNDK кейсов.
    status: completed
  - id: scenario-labels-v0
    content: Добавить пилотную разметку сценариев для 5-10 исторических earnings событий и их peer reactions.
    status: in_progress
  - id: event-brief-output
    content: "Сделать формат Event Brief для веба/бота: scenario, evidence, affected tickers, horizons, expected log-return."
    status: in_progress
isProject: false
---

# Earnings Intelligence Plan

## Промежуточные итоги (2026-05-28)

Этап **materials → ingest → LLM extract → peer graph** выполнен на prod для pilot META/NVDA.

| Шаг | Статус | Артефакты / prod |
|-----|--------|------------------|
| 1. Materials registry | ✅ | `earnings_material`, `services/earnings_material_catalog.py`, `scripts/sync_earnings_material_registry.py` |
| 2. Hybrid ingest | ✅ | HTML + **pypdf** (`services/earnings_material_parser.py`), `scripts/ingest_earnings_materials.py` |
| 3. LLM extractor | ✅ pilot | `services/earnings_material_extractor.py`, `scripts/extract_earnings_material_facts.py` → `earnings_event_detail` |
| 4. Peer graph v0 | ✅ | `services/peer_graph_catalog.py`, `scripts/seed_peer_graph_edges.py` — **27 рёбер** в prod |
| 5. Cron | ✅ | `crontab/lse-docker.crontab`: sync / ingest / extract каждые 2–6 ч |
| 6. Token audit | ✅ | `scripts/audit_earnings_materials_pipeline.py --symbols META,NVDA` — event-level ~**27k tok/событие** |

**Pilot extraction (prod):**

| Ticker | Date | tone | scenario (LLM) | affected |
|--------|------|------|----------------|----------|
| META | 2026-04-29 | bullish | capex_positive_for_infra_peers | 9 |
| NVDA | 2026-05-20 | bullish | gap_up_follow_through | 13 |

**LLM cost (claude-sonnet-4-6, ProxyAPI):** ~25k input + ~2.5k output ≈ **~27k tok/event** (transcript PDF + press/SEC; без дубля Fool). Legacy «1 pass на материал» ≈ 120k tok на META+NVDA.

**Известные gaps:**

- KB calendar может отставать от IR (NVDA 2026-05-20 пришлось добавить вручную) — нужен auto-ensure KB в sync.
- `discover-links` для ARM шумный (много PDF без текста) — фильтр в backlog.
- Старый failed URL META presentation (404) — можно пометить `skipped`.

**Следующий этап:** scenario labels v0 → Event Brief JSON → peer outcomes в brief → UI/бот.

---

## Что Уже Есть
- В `[docs/earnings-event-agent-lse/EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md](/media/cnn/home/cnn/lse/docs/earnings-event-agent-lse/EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md)` уже зафиксирован MVP: `event_reaction_dataset`, `features_before`, `outcomes_after`, CatBoost-регрессия `forward_log_ret_5d`.
- В `[scripts/sql/ml_event_analytics_schema.sql](/media/cnn/home/cnn/lse/scripts/sql/ml_event_analytics_schema.sql)` уже есть таблицы под расширение: `earnings_event_detail`, `peer_graph_edge`, `market_regime_daily`, `event_reaction_dataset`.
- В `[docs/earnings-event-agent-lse/PUBLIC_IR_EARNINGS_SOURCES.md](/media/cnn/home/cnn/lse/docs/earnings-event-agent-lse/PUBLIC_IR_EARNINGS_SOURCES.md)` уже собраны IR-источники META, ASML, ARM, SNDK, NVDA и других.

## Что Предложить По Задаче Шефа
- Сделать не просто “прогноз логдоходности”, а карточку события: `Earnings Event Brief`.
- Для каждого отчёта хранить материалы: press release, presentation, transcript, follow-up transcript, SEC/IR ссылки.
- LLM использовать как extractor: достать факты, а не принимать торговое решение.
- Основной вывод: сценарий реакции и влияние на peers, например `capex_positive_for_infra_peers`, `beat_selloff_pullback`, `guide_breakdown`, `gap_up_fade`.
- Для META-like кейса явно выделить “source ticker reaction” и “affected tickers reaction”: META падает, но MU/SNDK/AMD/LITE получают позитивный spillover.

## Архитектура
```mermaid
flowchart TD
  IrSources[IR pages and filings] --> MaterialStore[earnings materials]
  MaterialStore --> LlmExtractor[LLM structured extractor]
  LlmExtractor --> EarningsDetail[earnings_event_detail]
  LlmExtractor --> KnowledgeBase[knowledge_base with snippets]
  EarningsDetail --> EventDataset[event_reaction_dataset]
  PeerGraph[peer_graph_edge] --> EventDataset
  Quotes[quotes and market regime] --> EventDataset
  EventDataset --> RegressionHead[forward log-return regression]
  EventDataset --> ScenarioHead[scenario classifier]
  ScenarioHead --> EventBrief[Earnings Event Brief]
  RegressionHead --> EventBrief
```

## Минимальный Следующий Инкремент
- `Earnings materials registry`: добавлена таблица `earnings_material` в `scripts/sql/ml_event_analytics_schema.sql` и starter seed `scripts/seed_earnings_material_registry.py` для ссылок/статусов скачивания материалов.
- `LLM extraction schema`: фиксированный JSON: revenue/EPS surprise, guidance up/down/inline, capex, AI demand, margin pressure, inventory, management tone, Q&A concerns, affected tickers.
- `Peer graph v0`: вручную задать связи для AI infra/chips: META/NVDA/ASML/ARM -> MU/SNDK/AMD/LITE/INTC/QCOM и веса/тип связи.
- `Scenario labels v0`: начать с 6 классов из дизайна: `beat_selloff_pullback`, `beat_revaluation_down`, `miss_or_guide_breakdown`, `gap_up_follow_through`, `gap_up_fade`, `cross_earnings_contagion`.
- `Event Brief UI/Bot`: показывать по событию: источник, тезисы call, scenario, affected tickers, expected log-return 1/5/20d, confidence, invalidation.

## Что Не Делать Сразу
- Не пытаться обучать LLM. LLM только читает и структурирует документы.
- Не делать hard-block сделок на первом этапе. Только advisory/shadow.
- Не смешивать ежедневный macro-calendar ridge с earnings-call intelligence: это разные слои, их можно соединить позже в event_fusion.

## Практичный MVP На 1-2 Темы
- Начать с META capex -> infra/chips и NVDA earnings -> AI basket.
- Исторические кейсы: META 29.04.2026, ASML 15.04.2026, ARM 06.05.2026, SNDK 30.04.2026, NVDA 20.05.2026.
- Для каждого кейса вручную/LLM заполнить extracted JSON и проверить, как менялись source ticker и peers на 1d/2d/5d/20d log-returns.

## Порядок Реализации
1. **Материалы:** зафиксировать, где храним ссылки/файлы earnings: press release, presentation, transcript, follow-up transcript, SEC/IR.
   Реализация: `earnings_material` хранит `source_url`, `material_type`, `parse_status`, `local_path`, `content_sha256`, `content_text` и связь с `knowledge_base_id`, если событие уже есть в KB.
2. **Hybrid ingest:** `scripts/ingest_earnings_materials.py` + `services/earnings_material_parser.py` (HTML + pypdf PDF). Audit: `scripts/audit_earnings_materials_pipeline.py`.
3. **Extractor:** `services/earnings_material_extractor.py`, `scripts/extract_earnings_material_facts.py` — JSON в `earnings_event_detail.guidance_summary` / `affected_tickers`.
4. **Peer graph:** `services/peer_graph_catalog.py`, `scripts/seed_peer_graph_edges.py`.
5. **Outcomes:** для source ticker и affected tickers считать log-returns 1d/2d/5d/20d — **следующий шаг** (brief + peer spillover).
6. **Scenario labels:** `scripts/apply_earnings_scenario_labels.py` — LLM `scenario_hints` → `event_reaction_dataset.final_label`.
7. **Event Brief:** `services/earnings_event_brief.py`, `scripts/build_earnings_event_brief.py` — JSON для UI/бота.
8. **Analyzer readiness:** качество сценариев, покрытие материалов, OOS/PnL после transaction costs держать в анализаторе, не в карточке.

## Результат Для Пользователя
- В карточке/боте будет не просто “макро-календарь · фичей: 25”, а отдельный блок:
  - `Earnings intelligence: META capex positive for AI infra peers`
  - `Affected: MU, SNDK, AMD, LITE`
  - `Scenario: cross_earnings_contagion`
  - `Expected peer reaction: 1d/5d log-return`
  - `Evidence: call quote / guidance / capex line`

## Словарь Для Этого Плана

Источник полного словаря: `[docs/earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md](/media/cnn/home/cnn/lse/docs/earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md)`.

| Термин | Что значит у нас |
|---|---|
| `earnings` | Квартальная отчётность listed company: дата/время заранее известны, событие попадает в `knowledge_base` и `event_reaction_dataset`. |
| `earnings call` / `transcript` | Текст звонка CEO/CFO с инвесторами. Для шефа это важнее сухого report, потому что там future view, guidance и ответы на вопросы. |
| `press release` | Официальный релиз с цифрами квартала. Нужен как источник EPS/revenue/guidance facts. |
| `presentation` | Слайды к отчёту; часто дают сегменты, capex, demand drivers и risk language. |
| `follow-up transcript` | Дополнительный звонок/расшифровка после основного call, если есть. |
| `guidance` | Ориентиры менеджмента на будущий квартал/год: revenue, EPS, margin, capex, demand. |
| `capex` | Capital expenditures. В META-like кейсе высокий capex может быть негативом для META, но позитивом для AI infrastructure peers. |
| `affected_tickers` | Тикеры, на которые событие source ticker может повлиять: peers, supply chain, beneficiaries, competitors. |
| `peers` | Аналоги или связанные компании. Например AI infra/chips: MU, SNDK, AMD, LITE, INTC, QCOM. |
| `cross-impact` / `spillover` | Кросс-влияние: отчёт A двигает B. Пример: META падает из-за capex worries, но suppliers/infra растут. |
| `source ticker reaction` | Реакция самой компании, которая отчиталась. Например META после своего call. |
| `affected tickers reaction` | Реакция группы/цепочки поставок на событие source ticker. Например MU/SNDK после META capex. |
| `event_reaction_dataset` | Таблица “событие + признаки до + исходы после”. Это материал для регрессии и классификатора. |
| `features_before` | JSONB с признаками до события: цена, режим рынка, earnings facts, peer graph, позже extracted call facts. |
| `outcomes_after` | JSONB с тем, что случилось после события: log-returns 1d/2d/5d/20d, drawdown, rebound, volume. |
| `final_label` | Итоговая метка сценария реакции. Сейчас есть UP/DOWN/FLAT, нужно перейти к сценариям ниже. |
| `log-return` | Логарифмическая доходность. Все прогнозы/исходы для ML считаем в log-пространстве. |
| `horizon` | Горизонт прогноза/исхода: для event layer обычно 1d/2d/5d/20d, для GAME_5M отдельно 30/60/120m. |
| `LLM extractor` | LLM не “торгует”, а структурирует документы в JSON: guidance, capex, tone, affected tickers, evidence quotes. |
| `Hybrid ingest` | Сначала пробуем локально скачать/распарсить IR/PDF/HTML; если сайт сложный, используем ScrapeGraphAI как fallback. |
| `ProxyAPI` | Наш существующий OpenAI-compatible LLM endpoint (`PROXYAPI_KEY`, `OPENAI_BASE_URL`). Подходит для LLM extractor после того, как текст уже получен. |
| `ScrapeGraphAI` | Отдельный сервис для web extraction. Требует отдельный `SGAI_API_KEY`; не заменяет ProxyAPI, а помогает достать структурированные данные со сложных страниц. |
| `evidence` | Цитата/факт из call/report, объясняющий scenario или affected tickers. |
| `RAG` | Поиск похожих исторических событий по embeddings и фильтрам: сектор, scenario, market regime. |
| `event_fusion` | Будущий слой, который склеит event intelligence с GAME_5M/portfolio сигналами перед policy. |
| `transaction costs` | Комиссии/проскальзывание. Нужны в анализаторе для trading-facing readiness. |

## Классы Сценариев v0

| Класс | Смысл |
|---|---|
| `beat_selloff_pullback` | Компания бьёт ожидания, но акция падает как откат/фиксация, без явного развала тезиса. |
| `beat_revaluation_down` | Цифры хорошие, но рынок снижает мультипликатор: capex, margin, risk, overinvestment. |
| `miss_or_guide_breakdown` | Недобор или плохой guidance, реальное ухудшение инвестиционного тезиса. |
| `gap_up_follow_through` | Позитивный гэп и продолжение роста после отчёта. |
| `gap_up_fade` | Позитивный гэп не удержался, рынок “продал новость”. |
| `cross_earnings_contagion` | Отчёт одного эмитента двигает связанных тикеров/группу. META capex -> AI infra peers — базовый пример. |

## Проверка Готовности
- Метрики качества сценариев и регрессий держать в анализаторе.
- На карточке показывать только прогноз и объяснимые факты.
- Readiness: покрытие материалов, заполненность extracted JSON, зрелые outcomes, качество scenario classifier, PnL/expectancy после transaction costs для trading-facing решений.
