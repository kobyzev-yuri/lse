# Регламент: «Фундамент» + «Ожидания от репорта» (Рабочая тетрадка)

Две вкладки по схеме Насти (02.08.2026):

1. **Фундамент** — паспорт эмитента (~20 сек, редко, эталон стиля NBIS).
2. **Ожидания от репорта** — вокруг даты earnings (на что смотреть + за что наказали в прошлый раз).

Вывод на экране = **сохранённые поля** (read-only карточка сверху). LLM / Seeking Alpha карточку не пишут.

Связанные файлы: [`TRADING_NOTEBOOK.md`](TRADING_NOTEBOOK.md), [`nastya/FUNDAMENT_SCHEMA_NASTYA.md`](../nastya/FUNDAMENT_SCHEMA_NASTYA.md), UI `/notebook`, API `PATCH …/fundament`, `PATCH …/report-expect`, `GET …/fundament/suggest`.

---

## 0. Где Настя что правит («тезис» ≠ одно поле)

В UI слово **«тезис»** встречается в разных местах. Это **разные** сущности.

| Что | Где в `/notebook` | Поля | Кнопка сохранения | Связь с Вердиктом |
|-----|-------------------|------|-------------------|-------------------|
| **Фундамент (паспорт)** | вкладка **«Фундамент»** | A–E: биржа/HQ/листинг, tagline, клиенты, 4 метрики, маржа, финансирование, плюсы/риски, опц. `filing_url` | **OK · сохранить фундамент** | **Не** гейт. Справка для удержания |
| **Ожидания от репорта** | вкладка **«Ожидания от репорта»** | watch (драйвер…гайденс + тактика) + last (вердикт / за что / риск) | **OK · сохранить ожидания** | **Не** гейт. Мостик к тактике перед/после earnings |
| **Макро-тезис плана** | вкладка **«Тактика входа / выхода»** | **Макро-контекст (тезис словами)** | **OK · сохранить план** | Тумблер **тезис** на **Вердикте** = вкл/выкл этого макро (`macroAlive`). Выкл → СТОП |

**Эталон стиля паспорта:** NBIS на **Фундаменте**.

**Примеры вкладки ожиданий (пока):** MSFT и META (отчёты ~29 июл 2026).

**Что править, если «сломался AI/макро-тезис»:** текст в **Тактике** → макро; тумблер **тезис** на **Вердикте** выкл. Параллельно: **Риски** на паспорте + Block B на **Ожиданиях** (§5).

Yahoo «Подтянуть» — черновик **цифр плиток** паспорта. SEC companyfacts — не в UI. Плюсы/риски/нарратив автоматом не пишутся.

---

## 1. Цель

| Это | Это не |
|-----|--------|
| Паспорт: суть → 4 метрики → финансирование → плюсы/риски | Гейт Вердикта (Buy/Sell) |
| Ожидания: драйверы отчёта + память «наказания» | Тактика входа/выхода (отдельная вкладка) |
| Ручной текст Насти + черновик цифр Yahoo | Автосигнал «покупай / продавай» |
| | Тумблер «тезис» на Вердикте (макро из Тактики, §0) |

**Анти-цели:** не гейтить BUY/SELL по фундаменту; не автогенерить плюсы/риски / ожидания из LLM / Seeking Alpha без ручного approve.

---

## 2. Шаблон полей

### 2.0 Фундамент — паспорт (блоки A–E)

| Блок | Поле(я) | Кнопка / источник | Настя |
|------|---------|-------------------|-------|
| A | `exchange`, `hq_ru`, `listing_origin_ru` | **Yahoo** «Подтянуть» | правка; **слой** только на Профиле |
| A | имя / `sym` | read-only из тикера | — |
| B | `tagline` | Yahoo summary (черновик) | переписать модель своими словами |
| B | `key_clients_ru` | нет стабильного API | **вручную** (10-K / пресс-релиз) |
| B | `margin_ru` | Yahoo % | смысл «откуда маржа» |
| C | `metrics[4]` | Yahoo BS/CF | сверка с filing после earnings |
| D | `financing_ru` | Yahoo кэш/долг/D/E | нарратив: кто платит рост / точка разрыва |
| E | `pluses`, `risks` | — | **только Настя** (+ консенсус Инвестдома) |

Опц. **`filing_url`** — 10-K / 10-Q / 20-F / IR PDF (§2.1).

**Правило:** всё, что есть в Yahoo/KB — только по кнопке. Настя = суждение и поля без API.

### 2.0b Ожидания от репорта

| Блок | Поле | Что писать |
|------|------|------------|
| A | `watch.driver_ru` | Главная метрика бизнеса + почему |
| A | `watch.revenue_arr_ru` | % и абсолют роста |
| A | `watch.leading_ru` | Backlog / контракты / мощности |
| A | `watch.capex_ru` | Сумма + направление |
| A | `watch.margin_path_ru` | Маржа / путь к прибыли |
| A | `watch.guidance_ru` | Диапазон + подняли/подтвердили/срезали |
| A | `watch.tactics_map_ru` | Сильный vs ловушка → тактика |
| B | `last.date_verdict_ru` | Квартал · BEAT/MISS |
| B | `last.why_ru` | За что наказали / наградили |
| B | `last.risk_shift_ru` | Куда сместился риск (одна строка) |

Стиль: **кратко**, как NBIS / сиды MSFT·META — не доклад.

### 2.1 Где взять PDF (ссылка SEC / IR)

В UI на вкладке **Фундамент** кнопка **«SEC / filings (EDGAR)»**:

- если в поле `filing_url` уже сохранена ссылка → открывает её («Открыть сохранённый filing»);
- иначе → поиск EDGAR по тикеру:

```text
https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={TICKER}&owner=exclude&count=40
```

Пример: [SEC EDGAR · MSFT](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=MSFT&owner=exclude&count=40).

**Как дойти до PDF:** в списке выбрать форму **10-K** (год) или **10-Q** (квартал) → Documents → primary document / PDF (часто `.htm` или `.pdf`). Прямую URL вставить в **`filing_url`** и сохранить фундамент.

**Альтернатива:** PDF с IR сайта эмитента (`investor.*`, annual / quarterly report) — тоже ок; положить ту же ссылку в `filing_url`.

Companyfacts / кнопка «Подтянуть из SEC» — **не** источник PDF и **не** истина по цифрам (§3.3).

### 2.2 Intrinio vs ручная правка (что править руками)

| Что | Intrinio Individual | Без Intrinio / всегда вручную |
|-----|---------------------|-------------------------------|
| **4 метрики** (кэш, FCF, прямой долг, запас) | Кандидат на API-сверку цифр (§3.5); не замена PDF | Yahoo «Подтянуть» → после earnings поправить value/note из 10-K/10-Q/IR |
| **Паспорт A/B/D/E + Ожидания** | Не пишет | **Всегда Настя** |
| **`filing_url`** | Не нужен для API | Вручную / кнопка SEC (§2.1) |

**Individual ($150/mo)** достаточно для внутренней сверки US Fundamentals (1 seat, без display). Startup/Enterprise не берём без витрины данных / SLA. Пока ключа нет — рабочий путь: Yahoo + ручная правка метрик из PDF.

---

## 3. Источники правды

| Слой | Источник | Роль |
|------|----------|------|
| **Основной черновик цифр** | **Yahoo / yfinance** (`Подтянуть из Yahoo`) | Рабочий default для G1+: кэш, FCF (annual CF), долг (BS LT±current), current ratio. Note: «Yahoo …» |
| SEC companyfacts (API only) | `GET …/fundament/suggest?source=edgar` | **Не в UI.** После фикса FCF часто = Yahoo FY; кэш/долг часто нет. См. §3.3 |
| Правда после earnings | **10-K / 10-Q / IR PDF** (§2.1: кнопка SEC / `filing_url`) | Ручная сверка |
| Tagline, маржа, финансирование, плюсы, риски, паспорт A, ожидания | **Только Настя** | Текст карточек |
| Макро-контекст плана | **Настя / редактор** на вкладке **Тактика** | Текст для тумблера Вердикта |
| Seeking Alpha / дайджест LLM | Новости и контекст | **Не** источник истины по цифрам / ожиданиям |

### 3.1 Практика: SEC companyfacts часто не годится

По аудиту ядра G1 (2026-07-31) и работе в UI:

1. **Таймауты / 404 / нет CIK** — кнопка SEC часто пустая; карточка должна жить на **Yahoo**.
2. **Несопоставимые периоды** — SEC часто отдаёт **10-Q YTD** FCF при Yahoo **annual FY** (пример GOOGL). В note явно: `≠ Yahoo FY`.
3. **Разный состав** — SEC кэш часто *cash only*, Yahoo — *cash+STI*; долг SEC иногда **устаревший** `LongTermDebt` (годы назад → $0 / $1M: ORCL, TER, AMD).
4. **Статическая карта CIK** — промахи ломают тикер целиком (ALAB был с неверным CIK). Нужен fallback `company_tickers.json` или ручная правка карты.
5. **Эталон совпадения Yahoo↔SEC** по G1 — по сути **MSFT**; остальным не ожидать 1:1.

**Правило для Насти:** цифры карточки брать из **Yahoo-черновика** (или вручную из IR/10-K PDF по §2.1). Кнопка **SEC / filings** — только открыть EDGAR/PDF, не автоцифры. При расхождении Yahoo vs filing — верить filing/IR (или Yahoo annual, если период совпадает), не XBRL companyfacts.

### 3.2 Рекомендации по источникам (roadmap)

| Приоритет | Что | Зачем |
|-----------|-----|--------|
| **P0 (сейчас)** | Опираться на **yfinance** как на основной авточерновик | Стабильнее и полнее по G1, чем companyfacts |
| **P0** | Сверка после earnings — **вручную по 10-K/10-Q/IR**, не по кнопке SEC XBRL | Companyfacts ≠ надёжный полный сверщик |
| **P1** | FMP demo в `config.security.env`; при 402 на G1 — Starter | Cross-check, не truth |
| **P1** | Intrinio: письмо `sales@intrinio.com` / consultation — план **Individual** | Self-serve trial сломан; см. §3.5 |
| **P2** | SEC companyfacts оставить в коде (`?source=edgar`) для ops/отладки FCF | UI снят |
| **Не делать** | LLM/SA → авто цифры или плюсы/риски без approve | См. §7 |
| **Не брать сейчас** | Intrinio Startup/Enterprise | Нет нужды в display/SLA |

### 3.4 Источники: draft vs результат (2026-07-31)

| Слой | Источник | Роль |
|------|----------|------|
| **Черновик** | Yahoo / yfinance | Оставляем. Быстрый autofill 4 метрик |
| **Итог карточки** | 10-K / 10-Q / IR PDF | Primary truth; `filing_url` + note «сверено» |
| **Cross-check API** | FMP (сейчас demo/free в `config.security.env`) | Второй глаз к Yahoo/filing; **не** замена PDF |
| **SEC companyfacts** | API only, UI снят | Не использовать как истину |
| **Intrinio** | Пока **без ключа** (self-serve trial на сайте не открылся) | Кандидат на качественный SEC-normalized cross-check — см. §3.5 |

**FMP сейчас:** ключ `FMP_API_KEY` в `config.security.env` (не в git). Demo/free: FCF совпал с Yahoo; на части тикеров G1 — HTTP 402; `totalDebt` включает leases → для сверки брать LT+ST debt. Апгрейд при необходимости: [FMP Pricing → Starter](https://site.financialmodelingprep.com/developer/docs/pricing) (~$22/mo annual Personal).

### 3.5 Intrinio — предложение плана и контакты (trial кнопка не работает)

Self-serve **Start Free Trial** на [intrinio.com/pricing](https://intrinio.com/pricing) у нас не открылся → идём через sales/consultation, **пока без Intrinio в коде**.

**Какой план нужен нам**

| Нужда | План | Комментарий |
|-------|------|-------------|
| Внутренняя сверка Fundamentals для тетрадки (1 разработчик), без витрины данных наружу | **Individual ($150/mo)** | Включает **US Fundamentals** (standardized + as-reported из SEC). 1 seat, no external display — ок для ops |
| Если данные когда-то показываем клиентам / commercial display | **Startup** (от $333/mo → дороже) | Избыточно на старте |
| Кастомные фиды / SLA | Enterprise $1250+ | Не нужно |

Нам достаточно **Individual + US Fundamentals** (уже в составе Individual на прайсе). Цены: [intrinio.com/pricing](https://intrinio.com/pricing). Продукт: [US Fundamentals](https://intrinio.com/products/us-fundamentals).

**Куда писать, если trial/checkout сломан**

1. **Sales (срочно / trial вручную):** `sales@intrinio.com` — на [Request a consultation](https://intrinio.com/request-a-consultation) прямо указано: *For urgent inquiries, email sales@intrinio.com*.  
2. **Consultation form:** https://intrinio.com/request-a-consultation — ответ ~1 business day, custom proposal.  
3. **Support (аккаунт/ключ после signup):** `support@intrinio.com` + live chat на сайте ([help](https://help.intrinio.com/i-just-signed-up-now-what-1)).  
4. **Account / ключи после активации:** https://account.intrinio.com/ → API keys (production + sandbox).  
5. Альтернативный self-serve URL (Startup): https://account.intrinio.com/pricing/startup — если Individual trial мёртв, попробовать этот путь или попросить sales включить **Individual trial**.

**Черновик письма sales**

> Subject: Individual plan trial — US Fundamentals only (API broken self-serve)  
> We need a short trial of **Individual ($150/mo)** focused on **US Fundamentals** (balance sheet / cash flow / as-reported) for internal research notebook cross-checks vs SEC 10-K. Self-serve “Start Free Trial” on https://intrinio.com/pricing does not complete. Please enable trial + API keys for one seat, non-display use.

**Решение сейчас:** Yahoo draft + filing truth; FMP demo в security env; Intrinio — после ответа sales / рабочего trial. Не брать Startup/Enterprise без display-требований.

### 3.3 Вердикт SEC 2026-07-31 (после починки парсера)

Починили: FY-prefer FCF, отсев старого долга, cash+STI prefer, CIK из `company_tickers.json`.

**Прогон G1 + GOOGL на проде:**

| Метрика | Итог |
|---------|------|
| **FCF** | У всех FY и **точное совпадение с Yahoo** (вкл. GOOGL $73.3B) |
| **Долг** | Хорошо у MSFT/AMD/META; плохо у ORCL ($7B vs Yahoo $130B), TER ($0), ALAB (нет тега) |
| **Кэш** | Часто *cash only* ≠ Yahoo *cash+STI* (кроме MSFT/GOOGL) |

**Вердикт:** SEC companyfacts **не подходит как полный черновик 4 метрик** в UI. FCF-сверка рабочая, но кэш/долг систематически вводят в заблуждение.

**Решение:** кнопку **«Подтянуть из SEC»** убрать из интерфейса. Оставить ссылку **SEC / filings** для ручной сверки. API `?source=edgar` сохранить для отладки. Дальше — Yahoo + IR PDF; spike FMP (§3.4).

---

## 4. Охват и роли

- **Обязательный** полный NBIS-стиль на **Фундаменте** — для **ядра по запросу Насти** (тикеры с позицией / активным планом в G1–G2 в приоритете).
- Остальные тикеры тетрадки — пустая карточка или Yahoo-черновик **без** «сохранить фундамент», пока карточка не утверждена.
- G3 / Watchlist — только по запросу.

| Роль | Делает |
|------|--------|
| Система | **Yahoo draft** (плитки); ссылка filings; overlay; read-only карточки |
| Настя | **Фундамент** A–E + **Ожидания** A–B → сохранить |
| Алексей / ops | Не подменяет текст; CIK/деплой; FMP spike (§3.4) |

---

## 5. Когда обновлять

| Событие | Действие |
|---------|----------|
| Тикер добавлен в ядро / «нужен фундамент» | **Yahoo draft** → Настя правит паспорт → **OK · сохранить фундамент** |
| Перед earnings | Заполнить / обновить **Ожидания** (watch) → сохранить |
| **T+1…T+2 после earnings** | Сверить 4 метрики с **filing/IR**; обновить Block B ожиданий; риски паспорта при сломе |
| Квартальный review | Пройти ядро: устаревшие note «Yahoo, не сверено» — долг |
| SEC кнопка пустая / note «≠ Yahoo FY» | Игнорировать SEC; оставить Yahoo или вписать из IR |

### «Наказание» (отчёт ударил по истории)

Не отдельный авто-гейт. Практика:

1. На **Ожиданиях** Block B: дата+вердикт, за что, куда сместился риск → сохранить.
2. На **Фундаменте:** усилить **Риски**; при необходимости поправить **Маржа** / **Финансирование**.
3. Если нельзя исполнять план по уровням: на **Тактике** обновить макро-текст; на **Вердикте** выключить тумблер **тезис** (`macroAlive`).

---

## 6. Definition of Done

**Паспорт (Фундамент)** заполнен, если:

- читается за **~20 секунд**;
- есть осмысленный **tagline** (+ по возможности HQ / листинг);
- **4 метрики** с value и коротким note;
- заполнены **маржа** и **финансирование**;
- **≥2 плюса** и **≥2 риска**;
- после последнего earnings цифры либо сверены с **filing/IR**, либо в note явно «черновик Yahoo, не сверено».

**Ожидания** готовы вокруг даты, если:

- заполнены драйвер + хотя бы 2 поля watch + `tactics_map_ru`;
- после отчёта — Block B (`date_verdict_ru`, `why_ru`, `risk_shift_ru`).

(Макро на Тактике / тумблер Вердикта в DoD фундамента **не** входят.)

---

## 7. Риски полной автоматики (почему не делаем)

- Ложные цифры (TTM vs квартал, GAAP vs non-GAAP; SEC YTD vs Yahoo FY).
- Чужой нарратив (LLM/SA ≠ голос Насти) для паспорта **и** ожиданий.
- «Красивая» карточка без сверки после отчёта.
- Иностранные эмитенты без 10-Q ломают ожидание «для всех».
- Смешение справки Фундамента с тумблером/гейтом Вердикта.
- Доверие к SEC companyfacts как к «истине» при битых тегах/периодах.

---

## 8. Фазы продукта

**Фаза 1 (сделано):** регламент; Yahoo draft; UI паспорт + ожидания; сиды NBIS / MSFT / META; guide.

**Фаза 2 (код есть, UI снят):** `suggest_fundament_from_edgar` + FY-prefer; кнопка в UI **убрана** по вердикту §3.3. Ссылка EDGAR filings остаётся.

**Фаза 3 (дальше):**

1. **Контент:** заполнить G1 (и ядро по запросу Насти) по DoD §6 — опора на Yahoo.
2. **Vendor spike:** выбрать 1–2 fundamentals API (Polygon / FMP / Intrinio), сравнить G1 кэш·FCF·долг с Yahoo и 10-K; решить replace vs keep Yahoo.
3. Не возвращать SEC XBRL-кнопку без решения по кэшу/долгу (не только FCF).
4. Не автозаполнять **Ожидания** из LLM/SA без approve.

---

## 9. Статус вопроса Насте п.11

**Закрыт регламентом:** полный фундамент — **по запросу / для ядра**, не обязательно сразу все G2. См. §4. Где править текст — §0. Источник цифр по умолчанию — **Yahoo**; SEC — опционально (§3). Схема двух вкладок — уточнение 02.08.2026.
