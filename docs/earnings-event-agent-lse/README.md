# Earnings / event agent (LSE)

Материалы по целевому контуру **анализа реакции рынка на earnings и смежные события** в проекте LSE (не автотрейдинг как главная цель).

## Что открывать на GitHub

GitHub **не рендерит** обычный `.html` в репозитории как страницу (показывается исходный код). Для чтения в браузере на GitHub используйте **Markdown**:

| Файл | Назначение |
|------|------------|
| **[zadachi_dlya_agenta_voprosy_otvety.md](zadachi_dlya_agenta_voprosy_otvety.md)** | Вопросы из «Задачи для агента» и ответы — **кликабельные ссылки** на другие файлы репо |
| [EARNINGS_EVENT_AGENT_DESIGN.md](EARNINGS_EVENT_AGENT_DESIGN.md) | Дизайн: данные, слои, связь с `knowledge_base`, GAME_5M, LLM |

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
