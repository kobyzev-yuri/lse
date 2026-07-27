# Тетрадка — редакторы гейтов вердикта (фаза D)

Статус: **уровни + Env + макро-гейты сделаны** (overlay на VM).

## Сейчас (UI на вкладке «Вердикт»)

Все правки пишутся в overlay [`local/notebook/ticker_overrides.json`](../local/notebook/ticker_overrides.json) (volume на VM, не в git). Базовый [`notebook_data.json`](../nastya/notebook/notebook_data.json) остаётся шаблоном в репо.

| Поле | API | UI |
|------|-----|-----|
| `macroAlive` / `sentimentBroken` | `PATCH /api/notebook/tickers/{sym}/signals` | переключатели |
| `buyDip` / `sell` / `note` | `PATCH /api/notebook/tickers/{sym}/levels` | форма + «Сохранить» |
| Env ФРС / cut PT (`ok`/`mid`/`bad`) | `PATCH /api/notebook/tickers/{sym}/env` | кнопки ok/mid/bad |
| VIX | — | только live (quotes/`^VIX`), override запрещён |

Порядок в payload: base → live VIX (+ soft Fed из дайджеста) → **user overlay поверх**.

Аудит: `updated_by` / `updated_at_utc` в overlay.

Flash Crash: вкладка Watchlist (правило из ТЗ; без UI-редактора).

## Не делается здесь

- Профиль / entry-exit / инвестдома / фундамент — по-прежнему JSON или отдельный запрос.
- Авто-BUY, смешение с GAME_5M UI.
