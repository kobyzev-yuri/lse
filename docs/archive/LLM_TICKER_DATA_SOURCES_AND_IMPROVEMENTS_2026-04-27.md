# Источники Ticker Data для LLM и идеи по улучшению

Документ описывает, откуда берутся параметры вроде **VolumeVsAvg**, **RelVolume**, **PerfWeek**, **ATR** и др. в структурированном промпте для анализа рынка (формат с TickerData / Earnings / Calendar / News), и как улучшить их сбор и использование в нашем контуре (lse).

---

## 1. О каком промпте речь

Промпт из запроса — это **отдельный формат** (JSON-схема с `action`, `position`, `analysis` по ticker_data / earnings / calendar / news). В репозитории **lse** такого блока «Ticker Data» с полями VolumeVsAvg, PerfWeek, RSI14, SMA20Pct и т.д. **нет** — у нас используется промпт из `services/llm_service.py` и `analyst_agent.py` (текстовый блок «Технические данные» + новости + кластер). Ниже — что означают поля из того внешнего формата и как их можно добавить или улучшить у нас.

---

## 2. Откуда берутся параметры (типичные источники)

| Поле | Обычное определение | Источник данных |
|------|----------------------|-----------------|
| **VolumeVsAvg** | Текущий объём к среднему за N дней. Часто: `(volume_today / avg_volume_20d) * 100` или просто отношение. Значение 49.86 → объём ≈50% от среднего; 253 → 2.53× среднего. | Дневные объёмы из `quotes.volume` или Yahoo; среднее за 20 дней по тем же данным. |
| **RelVolume** | То же по смыслу: `volume_today / avg_daily_volume` (без *100). 2.76 = 276% от среднего. | Те же данные, другой масштаб. |
| **PerfWeek** | Изменение за период (например 5 дней): `(close_now / close_5d_ago - 1) * 100`. | `quotes`: последний close и close 5 дней назад. |
| **RSI14** | RSI по 14 периодам (дневные свечи). | У нас уже есть: `quotes.rsi` или расчёт по close (rsi_calculator). |
| **SMA20Pct, SMA50Pct** | Расстояние цены от скользящих в %: `(close - sma) / sma * 100`. | Нужны sma_20, sma_50: либо в `quotes` (как в finviz_parser), либо считаем по close. |
| **ATR** | Average True Range (типично 14 периодов): среднее от max(high−low, |high−prev_close|, |low−prev_close|). | Нужны дневные high, low, close. В lse: `quotes` имеет open, high, low, close — ATR можно считать. |
| **Beta** | Чувствительность к бенчмарку: Cov(ticker, benchmark) / Var(benchmark). | У нас уже есть: `cluster_manager.get_correlation_and_beta_matrix()` — можно брать beta тикера к выбранному бенчмарку (например QQQ или MU). |
| **DistFrom5DHigh / DistFrom5DLow** | Расстояние текущей цены от макс. High и мин. Low за 5 дней, в %: `(close - high_5d)/high_5d*100`, `(close - low_5d)/low_5d*100`. | По последним 5 дням из `quotes`: max(high), min(low), последний close. |
| **Volatility5D** | Волатильность за 5 дней (например std лог-доходностей в %). | У нас есть `quotes.volatility_5` (и средняя за 20 дней в analyst). |
| **Range1D** | Дневной диапазон: high−low за последний день, в % от close. | `quotes`: последняя строка high, low, close. |
| **IntradayChange** | Изменение за текущий день (например (close−open)/open*100 или от предыдущего close). | Текущий день: open/close из quotes или из yfinance при подтяжке «сегодня». |

Итого: **VolumeVsAvg** и **RelVolume** — это отношение текущего объёма к среднему объёму за N дней; источник — дневные объёмы (у нас в `quotes.volume` и при подтяжке из yfinance).

---

## 3. Что уже есть в lse

- **Цена, RSI, волатильность:** `analyst_agent.get_last_5_days_quotes()` → close, sma_5, volatility_5, rsi; плюс `get_average_volatility_20_days()`.
- **Кластер и корреляция:** `cluster_recommend.get_correlation_matrix()`, в промпт попадает cluster_note.
- **Beta:** считается в `cluster_manager.get_correlation_and_beta_matrix()`, но **в промпт analyst/LLM не передаётся**.
- **Объём:** в `quotes` есть `volume`, но **средний объём и VolumeVsAvg/RelVolume не считаются и в промпт не подаются**.
- **ATR, SMA20/50, DistFrom5DHigh/Low, PerfWeek, Range1D, IntradayChange:** не формируются в единый блок для LLM.

---

## 4. Предложения по улучшению сбора и использования

### 4.1 VolumeVsAvg / RelVolume

- **Сбор:** по каждому тикеру брать последние 20–30 дней из `quotes` (или из yfinance при отсутствии), считать `avg_volume = mean(volume)` за окно, за «текущий» день — последний доступный объём.
- **Формула:** `VolumeVsAvg = (volume_current / avg_volume) * 100` (или RelVolume = без *100).
- **Использование:** добавить в `technical_data` в analyst_agent и в промпт LLM одну строку, например: «Объём к среднему (20 дн.): X%» — чтобы модель учитывала аномально высокий/низкий объём.

### 4.2 ATR

- **Сбор:** по `quotes` (date, high, low, close) за последние 14+ дней считать True Range и среднее (ATR(14)).
- **Использование:** передавать в technical_data и в промпт; полезно для оценки волатильности и уровней стоп-лосса (как в boss_dashboard по «1–2 ATR»).

### 4.3 DistFrom5DHigh / DistFrom5DLow, PerfWeek

- **Сбор:** из последних 5 строк `quotes` по тикеру: max(high), min(low), последний close; close 5 дней назад для PerfWeek.
- **Использование:** в промпт добавить «Расстояние от хая 5д: X%», «от лоу 5д: Y%», «Изменение за 5д: Z%» — помогает LLM понимать, у дна или у потолка торгуется бумага.

### 4.4 SMA20 / SMA50 и расстояния

- **Сбор:** если в `quotes` нет sma_20/sma_50 — считать по close за 20/50 дней; иначе брать из БД (finviz_parser умеет sma_20).
- **Использование:** SMA20Pct, SMA50Pct в промпт — стандартные поля для «расстояния от тренда».

### 4.5 Beta

- **Сбор:** уже есть в `cluster_manager.get_correlation_and_beta_matrix(tickers, days)` — брать beta тикера относительно выбранного бенчмарка (например QQQ или MU).
- **Использование:** добавить в technical_data и в промпт одну строку: «Beta к [бенчмарк]: X» — чтобы LLM учитывала чувствительность к рынку.

### 4.6 Единый структурированный блок TickerData

- Имеет смысл ввести в analyst_agent (или в отдельном модуле) формирование **словаря TickerData** по одному тикеру: Price, Change1D/3D/5D, Volatility5D, VolumeVsAvg, RelVolume, DistFrom5DHigh/Low, PerfWeek, RSI14, SMA20Pct, SMA50Pct, ATR, Beta, Range1D, IntradayChange.
- Этот блок можно:
  - передавать в существующий текстовый промпт как доп. абзац;
  - либо использовать для перехода к формату «строго JSON» (как в том промпте с action/position/analysis), если позже решите унифицировать с внешним сервисом.

### 4.7 Earnings и Calendar

- В том промпте отдельно идут блоки Earnings и Economics Calendar. У нас: `alphavantage_fetcher.fetch_earnings_calendar`, календарь в БД; парсер экономических событий в `investing_calendar_parser`.
- Улучшение: при вызове LLM для тикера подмешивать в контекст «Ближайшие earnings: дата», «Ближайшие макро-события (важность, валюта)» — чтобы модель учитывала катализаторы и риски по времени.

---

## 5. Краткий чеклист

| Что | Где считать | Куда подать |
|-----|-------------|------------|
| VolumeVsAvg / RelVolume | quotes.volume за 20 дн., avg и последний объём | technical_data + промпт LLM |
| ATR | quotes high/low/close за 14 дн. | technical_data + промпт |
| DistFrom5DHigh/Low, PerfWeek | quotes за 5 дн. | technical_data + промпт |
| SMA20Pct, SMA50Pct | quotes.close или sma_20/sma_50 из БД | technical_data + промпт |
| Beta | cluster_manager.get_correlation_and_beta_matrix | technical_data + промпт |
| Earnings / Calendar | БД + alphavantage + calendar parser | Отдельный абзац в user_prompt |

Так мы приближаем наш контур к полноте того структурированного промпта (TickerData + контекст) и улучшаем обоснованность решений LLM за счёт объёма, волатильности (ATR), положения в диапазоне (5d high/low) и чувствительности к рынку (Beta).
