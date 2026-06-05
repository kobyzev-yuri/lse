# Earnings / event agent (LSE)

Материалы по целевому контуру **анализа реакции рынка на earnings и смежные события** в проекте LSE (не автотрейдинг как главная цель).

## Что открывать на GitHub

GitHub **не рендерит** обычный `.html` в репозитории как страницу (показывается исходный код). Для чтения в браузере на GitHub используйте **Markdown**:

| Файл | Назначение |
|------|------------|
| **[zadachi_dlya_agenta_voprosy_otvety.md](zadachi_dlya_agenta_voprosy_otvety.md)** | Вопросы из «Задачи для агента» и ответы — **кликабельные ссылки** на другие файлы репо |
| [PUBLIC_IR_EARNINGS_SOURCES.md](PUBLIC_IR_EARNINGS_SOURCES.md) | **Ссылки под рукой:** сводная таблица IR по акциям из `TICKER_GROUPS` + ASML/ARM; примеры кварталов (META, ASML, ARM, SNDK, NVDA) |
| [EARNINGS_EVENT_AGENT_DESIGN.md](EARNINGS_EVENT_AGENT_DESIGN.md) | Дизайн: данные, слои, связь с `knowledge_base`, GAME_5M, LLM |
| [PEER_GRAPH_PRINCIPLES.md](PEER_GRAPH_PRINCIPLES.md) | **Peer graph v0:** принципы построения, relation_type, weight, таблица лидер → все пиры |
| [EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md](EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md) | **Рабочий план:** фазы 1–5 (статус выполнения), источники, cron, CatBoost MVP |
| [EARNINGS_PRODUCT_ROADMAP.md](EARNINGS_PRODUCT_ROADMAP.md) | Продуктовая зрелость earnings (phase C/D) |
| [../ML_AND_DECISION_ARCHITECTURE.md](../ML_AND_DECISION_ARCHITECTURE.md) | Канон: контуры `earnings_grid`, cron, L1–L3 |
| Dated plans `EARNINGS_PLAN_2026-05-*` | Архив → stub в этой папке, полный текст в [../archive/](../archive/) |

## PDF и HTML (печать / офлайн)

| Файл | Назначение |
|------|------------|
| [zadachi_dlya_agenta_voprosy_otvety.pdf](zadachi_dlya_agenta_voprosy_otvety.pdf) | Экспорт для печати; пересоберите после правок HTML (ссылки в PDF ведут на `github.com/.../blob/main/...`) |
| [zadachi_dlya_agenta_voprosy_otvety.html](zadachi_dlya_agenta_voprosy_otvety.html) | Источник для PDF; ссылки абсолютные на GitHub |

## Пересборка PDF из HTML

При наличии Chrome/Chromium:

```bash
google-chrome --headless --no-sandbox --disable-gpu \
  --print-to-pdf=docs/earnings-event-agent-lse/zadachi_dlya_agenta_voprosy_otvety.pdf \
  "file://$(pwd)/docs/earnings-event-agent-lse/zadachi_dlya_agenta_voprosy_otvety.html"
```
