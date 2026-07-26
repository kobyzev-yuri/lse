# Тетрадка — спецификация редактора уровней (фаза D)

Статус: **отложено до запроса Насти** (вопрос 14 в `nastya/NOTEBOOK_QUESTIONS_FOR_NASTYA.md`).

## Сейчас

- Правка уровней: [`nastya/notebook/notebook_data.json`](../nastya/notebook/notebook_data.json) → commit → deploy.
- Flash Crash: вкладка Watchlist на `/notebook` (правило из ТЗ).

## Когда попросят UI-редактор

Минимальный MVP:

1. `PATCH /api/notebook/tickers/{sym}/levels` — body `{ "buyDip": number|null, "sell": number|null, "note": "..." }` (auth как у web).
2. Запись в `notebook_data.json` или отдельный overlay `local/notebook/levels_override.json` (предпочтительно overlay, чтобы не коммитить с VM).
3. Кнопка на карточке тикера: «Сохранить уровни» → перезагрузка вердикта.
4. Аудит: кто/когда изменил (простая строка в override).

Не делать: авто-BUY, смешение с GAME_5M UI.
