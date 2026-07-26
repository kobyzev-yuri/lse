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
- Дайджест: пайплайн SA Finance (RapidAPI) + LLM (ProxyAPI / Claude|GPT из config) → `local/notebook/digest_latest.json`; UI подхватывает поверх JSON-образца.
- Инвестдома — пока вручную; email Seeking Alpha — отдельно.

## Новости → дайджест

Временно до уточнения групп у Насти:

| Код | Состав |
|-----|--------|
| Group 1 | portfolio (`TRADING_CYCLE_TICKERS` или MEDIUM+LONG) |
| Group 2 | GAME_5M (`GAME_5M_TICKERS` / `TICKERS_FAST`) |
| Group 3 | **union** 1∪2 (пересечения ок, теги `portfolio` / `game_5m`) |

```bash
# ключ RapidAPI в config.env: SEEKING_ALPHA_RAPIDAPI_KEY или RAPIDAPI_KEY
python scripts/run_notebook_news_digest.py --universe-only
python scripts/run_notebook_news_digest.py --tickers MSFT,SNDK --no-llm   # fetch only
python scripts/run_notebook_news_digest.py --max-tickers 8                # + LLM digest
```

LLM: тот же `OPENAI_*` / `ANTHROPIC_MODEL` (ProxyAPI). Выход дайджеста: signals / risks / macro / newtickers (+ trashNote), как во вкладке «Дайджесты».

## Данные

Файл: [`nastya/notebook/notebook_data.json`](../nastya/notebook/notebook_data.json)

Образцы MVP:

- **MSFT** (G1): Buy Dip **$350**, Sell **$450**
- **META** (G1): уровни TBD
- **SNDK / LITE** (G2): ориентиры +$250 / +$150, помечены «уточнить»
- **NBIS**: эталон секции **Фундамент** (кэш / FCF / долг / плюсы / риски)

Правки уровней: править JSON → commit → deploy. Редактор в UI — вне MVP.

## Код

- `services/trading_notebook.py` — load JSON, merge prices, overlay digest_latest
- `services/seeking_alpha_finance.py` — RapidAPI SA Finance client
- `services/notebook_news_digest.py` — universe + LLM digest
- `scripts/run_notebook_news_digest.py` — CLI пайплайна
- `templates/trading_notebook.html` — порт UI из Claude-шаблона
- `docs/trading-notebook (3).html` — исходный дизайн-макет
