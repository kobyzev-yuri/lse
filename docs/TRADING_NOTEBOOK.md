# Рабочая тетрадка — план и вопросы Насте

Отдельный UI для ручной дисциплины: группы тикеров, заранее записанные уровни Buy Dip / Sell, вердикт по live Close. **Не смешивать** с GAME_5M / «Портфель · карточки» / коридорами.

**UI:** `/notebook` (в шапке сайта — «Тетрадка»).

Дубликат вопросов для копипаста в чат: [`nastya/NOTEBOOK_QUESTIONS_FOR_NASTYA.md`](../nastya/NOTEBOOK_QUESTIONS_FOR_NASTYA.md).

---

## Статус (2026-08-07)

| Область | Статус | Комментарий |
|---------|--------|-------------|
| UI каркас `/notebook` | **готово** | вкладки, группы, образцы |
| Close → вердикт | **готово** | quotes API |
| Sheet → KB | **готово** | cron `--mode sa_sheet` каждые 2 ч; статьи копируются в `knowledge_base` |
| Дайджест LLM утро | **готово** | раз в день 12:30 UTC ≈ 08:30 ET; корпус = Sheet за **3 дня** (`LOOKBACK=72`), `--from-kb-only` |
| Telegram `/digest` | **готово** | кэш, без повторного LLM |
| NEWS тетрадки | **Sheet only** | `NOTEBOOK_NEWS_KB_PROVIDER=sa_google_sheet` (дайджест, лента, sentiment/draft); календарь/earnings отдельно |
| Tipsters RapidAPI / bookmark | **в KB для LSE/UI** | в утренний дайджест тетрадки не входят |
| Email SA 35–40/день | **заменён Sheet** | внешний scraper → Google Sheet |
| Списки/уровни Насти | **частично** | G1=portfolio, G2=GAME_5M уже в JSON; уровни/G3/Новые — ждём п.1–5 |
| Фундамент / дома / env-ручное | **фундамент: паспорт + ожидания** | [`NOTEBOOK_FUNDAMENT_REGLEMENT.md`](NOTEBOOK_FUNDAMENT_REGLEMENT.md); схема Насти [`nastya/FUNDAMENT_SCHEMA_NASTYA.md`](../nastya/FUNDAMENT_SCHEMA_NASTYA.md); дома/env п.12–13 открыты |
| UI гейтов вердикта (уровни + Env ФРС/PT + макро) | **сделано** | `PATCH …/levels`, `…/env`, `…/signals` → `local/notebook/ticker_overrides.json` |
| UI профиль / дома / фундамент / watchlist | **не делали** | ждут отдельного запроса |

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
| 7 | Достаточен поток из Google Sheet (вместо email 35–40/день)? | **закрыт** — Sheet → KB каждые 2 ч; дайджест только из Sheet |
| 8 | Хватает утреннего **08:30 ET**, или ещё вечерний / чаще Telegram? | **открыт** — утро уже есть (раз в день) |
| 9 | Кому слать: только UI, или Telegram (кому)? `/digest` уже читает кэш. | **открыт** |
| 10 | «Новые тикеры» в дайджесте → сразу G3 с пустыми уровнями или только «к рассмотрению»? | **открыт** — в промпте сейчас «к рассмотрению» |

Tipsters RapidAPI (разделы/тикеры/bookmark) остаются опциональным обогащением KB/UI; утренний LLM тетрадки их не читает.

### Фундамент / инвестдома / окружение

| # | Вопрос | Наш статус |
|---|--------|------------|
| 11 | Фундамент (эталон NBIS): все G2 или по запросу? | **закрыт** — ядро по запросу; две вкладки (паспорт + ожидания); см. [`NOTEBOOK_FUNDAMENT_REGLEMENT.md`](NOTEBOOK_FUNDAMENT_REGLEMENT.md) |
| 12 | Инвестдома: ручной JSON или авто (какие дома/поля)? | **открыт** |
| 13 | Environment: **VIX уже из yfinance/quotes** (авто). Кто обновляет вручную ФРС / внезапные cut PT? Нужен полуавто-статус VIX на карточке? | **частично закрыт** — VIX авто; ручное остальное ждём |

### Процесс

| # | Вопрос | Наш статус |
|---|--------|------------|
| 14 | Кто правит уровни день за днём? Нужен UI-редактор (фаза D)? | **закрыт** — UI на Вердикте; см. [`NOTEBOOK_PHASE_D_EDITOR.md`](NOTEBOOK_PHASE_D_EDITOR.md) |
| 15 | Flash Crash вне Watchlist: правило как в шаблоне или отложить? | **открыт** — вкладка Watchlist уже есть |

---

## Источники новостей (тетрадка)

### Канон тетрадки

```text
Google Sheet (scraper)
    → каждые 2 ч: --mode sa_sheet
    → копии статей в knowledge_base
         (source=Seeking Alpha Finance, provider=sa_google_sheet)
    → раз в день ~08:30 ET: дайджест LLM за последние 3 дня
```

| Шаг | Когда | Что |
|-----|--------|-----|
| Ingest Sheet → KB | `40 */2` · `fetch_news_cron.py --mode sa_sheet` | Полный лист A–E; новые строки **копируются** в KB (ON CONFLICT / dedup по `external_id`) |
| Дайджест | `30 12 * * *` UTC ≈ **08:30 ET** · `run_notebook_news_digest.py --from-kb-only` | Только Sheet-строки за **72 ч (3 дня)** → дедуп → LLM → кэш `/digest` |

Таблица: https://docs.google.com/spreadsheets/d/15Vt-P0kffD9ERl17XxgEJgcEebJTu20UXALxzERlrzA/edit  
Колонки: A time, B URL, C title, D text, E symbols (`-` → `MACRO`). Тексты короткие (~1–3k символов); отдельный summary пока не делаем.

Конфиг: `NOTEBOOK_SA_SHEET_*`, `NOTEBOOK_NEWS_KB_PROVIDER=sa_google_sheet`, `NOTEBOOK_NEWS_KB_LOOKBACK_HOURS=72`.  
**Все NEWS тетрадки** (дайджест, лента, sentiment/draft из KB) — только Sheet. **Календарь и earnings** — без этого фильтра.

Лимиты утреннего LLM (дефолты): вход `NOTEBOOK_NEWS_LLM_MAX_ITEMS=1000`; корзины signals/risks ≤12, macro ≤6, newtickers ≤5.

### Остальные ленты LSE (не корпус дайджеста тетрадки)

Пишут в ту же `knowledge_base`, но утренний LLM тетрадки их не берёт:

| Источник | Cron / mode | Роль |
|----------|-------------|------|
| Seeking Alpha Finance (RapidAPI tipsters/тикеры/bookmark) | `35 */2` · `--mode sa` + UI | KB / вкладка SA разделы |
| Yahoo + Marketaux | `5 */2` · `--mode tickers` | LSE ticker headlines |
| Investing.com News + calendar | `0 */2` · `--mode investing` | LSE; календарь — отдельный `event_type` |
| RSS ЦБ / Alpha Vantage / Yahoo earnings | `core-fast` и др. | LSE / earnings dates |
| Email SA 35–40/день | — | **заменён** Google Sheet |

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

- **Фундамент** (паспорт NBIS) + **Ожидания от репорта**: UI + overlay; сиды NBIS / MSFT / META; регламент [`NOTEBOOK_FUNDAMENT_REGLEMENT.md`](NOTEBOOK_FUNDAMENT_REGLEMENT.md).
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
