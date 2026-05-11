# Earnings / event agent (LSE)

Материалы по целевому контуру **анализа реакции рынка на earnings и смежные события** в проекте LSE (не автотрейдинг как главная цель).

| Файл | Назначение |
|------|------------|
| [EARNINGS_EVENT_AGENT_DESIGN.md](EARNINGS_EVENT_AGENT_DESIGN.md) | Дизайн: данные, слои, связь с `knowledge_base`, GAME_5M, LLM |
| [zadachi_dlya_agenta_voprosy_otvety.pdf](zadachi_dlya_agenta_voprosy_otvety.pdf) | Вопросы из «Задачи для агента» и ответы в формате PDF |
| [zadachi_dlya_agenta_voprosy_otvety.html](zadachi_dlya_agenta_voprosy_otvety.html) | Источник для пересборки PDF (см. ниже) |

## Пересборка PDF из HTML

При наличии Chrome/Chromium:

```bash
google-chrome --headless --no-sandbox --disable-gpu \
  --print-to-pdf=docs/earnings-event-agent-lse/zadachi_dlya_agenta_voprosy_otvety.pdf \
  "file://$(pwd)/docs/earnings-event-agent-lse/zadachi_dlya_agenta_voprosy_otvety.html"
```
