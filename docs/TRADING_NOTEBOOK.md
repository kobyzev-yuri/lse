# Рабочая тетрадка — план и вопросы Насте

Отдельный UI для ручной дисциплины: группы тикеров, заранее записанные уровни Buy Dip / Sell, вердикт по live Close. **Не смешивать** с GAME_5M / «Портфель · карточки» / коридорами.

**UI:** `/notebook` (в шапке сайта — «Тетрадка»).

Дубликат вопросов для копипаста в чат: [`nastya/NOTEBOOK_QUESTIONS_FOR_NASTYA.md`](../nastya/NOTEBOOK_QUESTIONS_FOR_NASTYA.md).

---

## Статус (2026-07-26)

| Область | Статус | Комментарий |
|---------|--------|-------------|
| UI каркас `/notebook` | **готово** | вкладки, группы, образцы |
| Close → вердикт | **готово** | quotes API |
| SA ingest → KB | **готово** | cron `--mode sa` каждые 2 ч |
| Дайджест LLM утро | **готово** | будни 12:30 UTC ≈ 08:30 ET, `--from-kb-only` |
| Telegram `/digest` | **готово** | кэш, без повторного LLM |
| Мульти-источник + дедуп | **готово** | все NEWS из KB (Yahoo/Marketaux/Investing/SA/…) + MACRO; дедуп link/title до LLM |
| Прокси макро (SPY/QQQ/VIX) в ingest | **не делали** | опционально, если мало макро в KB |
| Email SA 35–40/день | **ждём п.7** | не подключено |
| Списки/уровни Насти | **частично** | G1=portfolio, G2=GAME_5M уже в JSON; уровни/G3/Новые — ждём п.1–5 |
| Фундамент / дома / env-ручное | **ждём п.11–13** | эталон NBIS есть; VIX уже авто |
| UI-редактор уровней | **фаза D** | спека готова, код по запросу |
| UI макро-гейтов (`macroAlive` / `sentimentBroken`) | **сделано** | `PATCH /api/notebook/tickers/{sym}/signals` → `local/notebook/ticker_overrides.json` |

---

## Вопросы Насте

Ответы нужны, чтобы заменить образцы и сузить процесс. Рядом — что уже решено без ответа.

### Группы и тикеры

| # | Вопрос | Наш статус |
|---|--------|------------|
| 1 | Финальный список по **G1 / G2 / G3 / Новые** (или Excel) — что в тетрадке, что только Watchlist? | **частично** — G1/G2 = portfolio / GAME_5M; G3/Новые и тонкая правка — открыты |
| 2 | Пересечения допустимы? (сейчас тикер может быть и в portfolio, и в 5m.) | **открыт** |
| 3 | «Новые стоки» — только Most Active / SA candidates, или ещё ручной список? | **открыт** |

Черновик тикеров из чата Насти (анализ графиков, не канон тетрадки): AAOI…WDC в `nastya/tz.txt` (есть INTC*, ASML*, MU* и др.).

### Уровни и вердикт

| # | Вопрос | Наш статус |
|---|--------|------------|
| 4 | Buy Dip / Sell для ядра; MSFT **350 / 450** — ок? | **открыт** — в образце уже 350/450 |
| 5 | SNDK **+$250** / LITE **+$150** — целевая прибыль или абсолют? Колонка «вход факт»? | **открыт** |
| 6 | Вердикт: Close vs уровни + env/макро хватит, или ещё объём / RVOL / коридоры? | **открыт** — коридоры в тетрадку не тащим, пока не попросит |

### Дайджест

| # | Вопрос | Наш статус |
|---|--------|------------|
| 7 | Достаточен поток из KB (SA API + Yahoo/Marketaux/Investing и др.), или обязателен **email 35–40 писем/день**? | **частично закрыт** — мульти-источник KB + дедуп уже в проде; email только если скажет «нужен» |
| 8 | Хватает утреннего **08:30 ET**, или ещё вечерний / чаще Telegram? | **открыт** — утро уже есть |
| 9 | Кому слать: только UI, или Telegram (кому)? `/digest` уже читает кэш. | **открыт** |
| 10 | «Новые тикеры» в дайджесте → сразу G3 с пустыми уровнями или только «к рассмотрению»? | **открыт** — в промпте сейчас «к рассмотрению» |

Проверка API: новость [Intel CapEx / Foundry 4618091](https://seekingalpha.com/news/4618091-intels-capex-increase-positive-for-foundry-business-chip-equipment-makers) приходит по `ticker_slug=intc`. Отдельной геополитической ленты у RapidAPI нет (`/v1/news/list` → 422); макро/гео — из тикерных лент + MACRO в KB + опционально прокси SPY/QQQ/VIX позже.

### Фундамент / инвестдома / окружение

| # | Вопрос | Наш статус |
|---|--------|------------|
| 11 | Фундамент (эталон NBIS): все G2 или по запросу? | **открыт** |
| 12 | Инвестдома: ручной JSON или авто (какие дома/поля)? | **открыт** |
| 13 | Environment: **VIX уже из yfinance/quotes** (авто). Кто обновляет вручную ФРС / внезапные cut PT? Нужен полуавто-статус VIX на карточке? | **частично закрыт** — VIX авто; ручное остальное ждём |

### Процесс

| # | Вопрос | Наш статус |
|---|--------|------------|
| 14 | Кто правит уровни день за днём? Нужен UI-редактор (фаза D)? | **открыт** — спека [`NOTEBOOK_PHASE_D_EDITOR.md`](NOTEBOOK_PHASE_D_EDITOR.md) |
| 15 | Flash Crash вне Watchlist: правило как в шаблоне или отложить? | **открыт** — вкладка Watchlist уже есть |

---

## Источники новостей (LSE → `knowledge_base` → дайджест)

Все ленты пишут в одну таблицу `knowledge_base`. Утренний дайджест читает **NEWS** по тикерам тетрадки (+ опционально `MACRO`), **все `source`**, затем дедуп (link / ticker+title) и LLM.

| Источник | Cron / mode | Что даёт | В дайджесте |
|----------|-------------|----------|-------------|
| **Seeking Alpha Finance** (RapidAPI) | `35 */2` · `--mode sa` | Тикерные новости SA по universe тетрадки | да (`source=Seeking Alpha Finance`) |
| **Yahoo + Marketaux** | `5 */2` · `--mode tickers` | Тикерные новости (Motley Fool, Zacks, Reuters, Yahoo, …) | да (разные `source`) |
| **Investing.com News** (+ calendar) | `0 */2` · `--mode investing` | Лента + экономкалендарь | новости — да; календарь — другой `event_type` |
| **RSS ЦБ / Alpha Vantage** | `*/15` · `--mode core-fast` | Макро/календарные потоки | если попали как NEWS/MACRO по тикерам |
| **NewsAPI** | cron **выключен** (`# newsapi`) | макро/equity при включении | сейчас почти не кормит |
| **Email SA 35–40/день** | нет | — | **ждём п.7** |
| Отдельная SA «геополитика» лента | нет | RapidAPI `/v1/news/list` → 422 | нет; макро из тикеров + `MACRO` |

**Дайджест:** будни `30 12` UTC ≈ **08:30 ET** · `run_notebook_news_digest.py --from-kb-only` · Telegram `/digest` из кэша.

Конфиг: `NOTEBOOK_NEWS_KB_ALL_SOURCES=1` (все источники), `NOTEBOOK_NEWS_INCLUDE_MACRO=1`, откат SA-only → `NOTEBOOK_NEWS_KB_ALL_SOURCES=0`.

---

## Тикеры по группам

Источник правды UI: [`nastya/notebook/notebook_data.json`](../nastya/notebook/notebook_data.json).  
Разбиение **текущее (прод):** G1 = equities **portfolio**, G2 = **GAME_5M**. G3 / Новые — пусто (п.1 Насти на тонкую настройку).

### Текущее разбиение

| Группа | Тикеры | База списка |
|--------|--------|-------------|
| **G1 · Пассивное (удержание)** | **ALAB, AMD, AMZN, META, MSFT, ORCL, TER** | portfolio (equities; без `BZ=F` / `CL=F` / `GBPUSD=X` / `GC=F` / `^VIX`) |
| **G2 · Активное** | **ASML, CIEN, LITE, MU, NBIS, SNDK** | GAME_5M |
| **G3 · Кандидаты** | *(пусто)* | — |
| **Новые · первичный анализ** | *(пусто)* | — |

**Пересечение:** `MU` есть и в portfolio, и в GAME_5M → в тетрадке **G2** (активное), в тегах `portfolio` + `GAME_5M`.

**Уровни (уже вписаны в образцах):** MSFT Buy Dip **$350** / Sell **$450**; LITE Buy Dip ~**$650**, Sell ≈ вход **+$150**; SNDK Sell ≈ вход **+$250**; остальным — TBD / вручную. NBIS — эталон фундамента.

Universe дайджеста / SA ingest: все 13 тикеров выше (+ строка `MACRO` из KB).

### Связанные списки LSE (канон конфига)

| Список | Equities |
|--------|----------|
| Portfolio | ALAB, AMD, AMZN, META, MSFT, MU, ORCL, TER |
| GAME_5M | ASML, CIEN, LITE, MU, NBIS, SNDK |

### Черновик Насти (графики, не канон)

Из `nastya/tz.txt` — общий пул для возможного G3/Новые (в исходнике часть помечена `*`):

`AAOI, AEIS, ALAB*, AMD*, AMKR, AMZN*, ANET, ARM*, ASML*, AVGO*, CDNS*, CIEN, COHR, CRDO, CRWV*, DDOG*, DELL, ENTG, GOOGL*, INTC*, INTU*, KLAC*, LITE, LRCX*, META*, MRVL*, MSFT*, MU*, MXL, NBIS*, NOW, NVDA*, ONTO, ORCL, PLTR*, QCOM*, RBLX, SMCI, SNDK*, SNPS*, TSM, WDC`

---

## Уже в проде (кратко)

| Что | Где |
|-----|-----|
| UI | `/notebook` |
| Данные групп/уровней | `nastya/notebook/notebook_data.json` |
| Close → вердикт | quotes API |
| Ingest + дайджест | cron выше; дедуп в `notebook_news_digest.dedupe_news_items` |
| Telegram | `/digest` ← кэш |

---

## План реализации

### Фаза A — списки Насти *(блокер: п.1–5)*

- Заменить образцы в `notebook_data.json` на её G1/G2/G3/Новые + уровни.
- Ingest/дайджест уже читают этот JSON.
- Обновить канон в этом документе.

### Фаза B — дайджест

- **Сделано:** SA → KB; Yahoo/Marketaux/Investing и др. → KB; утренний LLM из всех источников; дедуп link/title; промпт Buy Dip / Hold / пауза / макро / гео; UI + `/digest`.
- **Опционально:** ingest-прокси SPY/QQQ/TLT/VIX для чистого макро/гео.
- **Ждём п.7–10:** email SA; второй срез дня; кому слать; политика newtickers → G3.

### Фаза C — карточки *(блокер: п.11–13)*

- **Фундамент** (шаблон NBIS): вручную в JSON.
- **Инвестдома**: firm / rate / pt / quote / tac — вручную, пока нет авто.
- **Environment:** VIX авто; ФРС / cut PT — вручную или полуавто позже.

### Фаза D — удобство *(по запросу, п.14–15)*

- UI-редактор уровней — [`NOTEBOOK_PHASE_D_EDITOR.md`](NOTEBOOK_PHASE_D_EDITOR.md).
- Flash Crash — вкладка уже есть.
- Связка дайджест → карточка тикера — по мере нужды.

### Вне плана

- Смешение с GAME_5M / range-regime.
- Авто-BUY по дайджесту или Close.

---

## Критерий «принята Настей»

- Её группы и уровни на `/notebook`.
- Утренний дайджест читабелен и совпадает с ожидаемой тактикой.
- Понятно, кто обновляет уровни / env / дома без разработчика каждый день.

## Код (справка)

- `services/trading_notebook.py`, `services/seeking_alpha_finance.py`, `services/notebook_news_digest.py` (`dedupe_news_items`)
- `scripts/run_notebook_news_digest.py`, `scripts/fetch_news_cron.py --mode sa|tickers|investing|…`
- `templates/trading_notebook.html`
