# Импульс премаркета для раннего RTH (игра 5m)

## Зачем

В первые минуты после 9:30 ET в текущей RTH-сессии может быть **мало 5m-баров**, а полный ряд `momentum_2h_pct` тянет **вчерашний подъём** → риск входа «на хвосте» и разворота.

Уже используется **импульс только по барам текущего RTH-дня** (`momentum_rth_today_pct`), когда накопилось ≥ `GAME_5M_MOMENTUM_MIN_SESSION_BARS` баров.

## Дополнение

Пока 5m-баров сессии мало, опционально подмешивается **дрейф премаркета по 1m** (Yahoo `prepost=True`):

- `get_premarket_intraday_momentum_pct(ticker)` — отношение **последнего** close до 9:30 ET к **первому** close в том же срезе (тот же календарный день ET), **не** гэп к вчерашнему close.

Если этот % **> `GAME_5M_PREMARKET_MOMENTUM_BUY_MIN`** (по умолчанию 0.5) и **≥ `GAME_5M_PREMARKET_MOMENTUM_BLOCK_BELOW`** (по умолчанию −2.0), и RSI не перекуплен — разрешён импульсный **BUY** с пояснением в `reasoning`.

## Конфиг (`config.env`)

| Переменная | По умолчанию | Смысл |
|------------|--------------|--------|
| `GAME_5M_EARLY_USE_PREMARKET_MOMENTUM` | `true` | Включить ветку раннего BUY по премаркету |
| `GAME_5M_PREMARKET_MOMENTUM_BUY_MIN` | `0.5` | Мин. %% дрейфа премаркета для BUY |
| `GAME_5M_PREMARKET_MOMENTUM_BLOCK_BELOW` | `-2.0` | Если премаркет уже просел сильнее — не BUY по этой ветке |

Отключить премаркет-ветку: `GAME_5M_EARLY_USE_PREMARKET_MOMENTUM=false`.

## Поля

- `premarket_intraday_momentum_pct` в выходе `get_decision_5m` / карточках (если считался).
- Реализация: `services/premarket.py`, правила — `services/recommend_5m.py`.
