# Тетрадка — спецификация редактора уровней (фаза D)

Статус: **отложено до запроса Насти** (вопрос 14 в `nastya/NOTEBOOK_QUESTIONS_FOR_NASTYA.md`).

## Сейчас

- Правка уровней: [`nastya/notebook/notebook_data.json`](../nastya/notebook/notebook_data.json) → commit → deploy.
- **Макро-гейты:** UI на вкладке «Вердикт» → `PATCH /api/notebook/tickers/{sym}/signals` → overlay [`local/notebook/ticker_overrides.json`](../local/notebook/ticker_overrides.json) (на VM volume, не в git).
- Flash Crash: вкладка Watchlist на `/notebook` (правило из ТЗ).

## Когда попросят UI-редактор уровней

Минимальный MVP:

1. `PATCH /api/notebook/tickers/{sym}/levels` — body `{ "buyDip": number|null, "sell": number|null, "note": "..." }` (auth как у web).
2. Тот же overlay `local/notebook/ticker_overrides.json` (поле `levels`), чтобы не коммитить с VM.
3. Кнопка на карточке тикера: «Сохранить уровни» → перезагрузка вердикта.
4. Аудит: кто/когда изменил (уже есть для signals: `updated_by` / `updated_at_utc`).

Не делать: авто-BUY, смешение с GAME_5M UI.
