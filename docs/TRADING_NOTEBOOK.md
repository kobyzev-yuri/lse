# Рабочая тетрадка

Отдельный UI для ручной дисциплины Насти: группы тикеров, заранее записанные уровни Buy Dip / Sell, вердикт по сравнению с live Close.

**Не смешивать** с GAME_5M, «Портфель · карточки» и «Анализ · коридоры» — другой визуальный язык и другая логика (ручной план, не интрадей-бот).

## URL

| Путь | Назначение |
|------|------------|
| `/notebook` | Страница тетрадки |
| `/api/notebook` | JSON: группы, тикеры, дайджест (`?prices=0` без Close) |
| `/api/notebook/prices?tickers=MSFT,NBIS` | Только closes |

В общей шапке сайта — ссылка **Тетрадка**. На самой странице портфельных субтабов нет.

## Принцип

- Уровни вписываются **вручную** (команда) в JSON.
- **Close** из `quotes` (fallback yfinance) — только справочная цена на карточке и во вкладке «Вердикт».
- Вердикт **не** является авто-сигналом BUY: сравнивает Close с ручным Buy Dip / Sell + Environment / макро-гейты.
- Дайджест / инвестдома — структура под будущий Seeking Alpha ingest; пока ручное наполнение JSON.

## Данные

Файл: [`nastya/notebook/notebook_data.json`](../nastya/notebook/notebook_data.json)

Образцы MVP:

- **MSFT** (G1): Buy Dip **$350**, Sell **$450**
- **META** (G1): уровни TBD
- **SNDK / LITE** (G2): ориентиры +$250 / +$150, помечены «уточнить»
- **NBIS**: эталон секции **Фундамент** (кэш / FCF / долг / плюсы / риски)

Правки уровней: править JSON → commit → deploy. Редактор в UI — вне MVP.

## Код

- `services/trading_notebook.py` — load JSON, merge prices
- `templates/trading_notebook.html` — порт UI из Claude-шаблона
- `docs/trading-notebook (3).html` — исходный дизайн-макет
