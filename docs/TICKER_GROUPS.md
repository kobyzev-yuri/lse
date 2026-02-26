# Группы тикеров: быстрая игра, средние дистанции, вдолгую

## Назначение

Тикеры из `/tickers` (зарегистрированные в БД) распределены по трём группам по стилю торговли:

| Группа | Стиль | Тикеры (по умолчанию) | Логика |
|--------|--------|------------------------|--------|
| **Быстрая игра** | Интрадей, 5m | SNDK, LITE | Короткие интервалы (день–два), 5m индикаторы, GAME_5m (send_sndk_signal_cron). Нужны 5m данные Yahoo. |
| **Средние дистанции** | Смешанный вариант | ALAB, MU, TER | Среднесрочный горизонт, портфельная игра (trading_cycle_cron). |
| **Вдолгую** | Свинг, дневные | MSFT, GBPUSD=X, GC=F, ^VIX | Дневные/недельные решения, портфельная игра: акции, forex, товары, VIX. |

## Конфигурация

В `config.env` задайте списки под ваши зарегистрированные инструменты (как в `/tickers`):

```bash
# Быстрая игра (5m) — только тикеры с 5m данными
TICKERS_FAST=SNDK,LITE

# Средние дистанции
TICKERS_MEDIUM=ALAB,MU,TER

# Вдолгую (свинг)
TICKERS_LONG=MSFT,GBPUSD=X,GC=F,^VIX
```

Если не задано — используются дефолты из `services/ticker_groups.py` (см. выше).

## Условие для быстрых тикеров: наличие 5m данных

Для быстрой игры и рассылки сигналов нужны **5-минутные котировки** (как для `/chart5m`). Источник — Yahoo Finance; не у всех тикеров есть 5m.

- **Cron** `send_sndk_signal_cron.py` обрабатывает только те тикеры из `TICKERS_FAST`, по которым **есть 5m данные** (проверка `has_5m_data()` в `services/recommend_5m.py`). Остальные в этом запуске пропускаются с предупреждением в логе.
- **Проверка вручную:**  
  `python scripts/check_fast_tickers_5m.py` — выводит, у каких быстрых тикеров есть 5m, у каких нет. Тикеры без 5m лучше убрать из `TICKERS_FAST`, пока нет другого источника интрадей-данных.

## Использование в коде

```python
from services.ticker_groups import get_tickers_fast, get_tickers_medium, get_tickers_long, get_all_ticker_groups

fast = get_tickers_fast()     # из TICKERS_FAST, напр. ["SNDK", "LITE"]
medium = get_tickers_medium() # из TICKERS_MEDIUM, напр. ["ALAB", "MU", "TER"]
long_ = get_tickers_long()    # из TICKERS_LONG, напр. ["MSFT", "GBPUSD=X", "GC=F", "^VIX"]
all_ = get_all_ticker_groups()  # быстрые → средние → долгие, без дубликатов
```

Дашборд, рекомендации 5m, game_* и стратегии могут опираться на эти списки (сейчас дашборд использует `DASHBOARD_WATCHLIST`; при расширении можно собирать watchlist из `get_all_ticker_groups()` или разделять блоки «быстрые» / «средние» / «долгие»).

## Как добавить новый тикер (например AMD)

Чтобы новый тикер участвовал в **учёте цен** и **новостях**:

### 1. Цены (таблица `quotes`, `/tickers`, обновление по крону)

- **Один раз** загрузить котировки для тикера:
  ```bash
  cd /path/to/lse && python update_prices.py AMD
  ```
  После этого тикер появится в `quotes`, в `/tickers` и будет обновляться кроном `update_prices_cron.py` вместе с остальными.

- Либо добавить тикер в `DEFAULT_TICKERS` в `init_db.py` и при следующем `python init_db.py` (или ручном вызове `seed_data()`) он подтянется при первичной загрузке.

### 2. Группа (игры и список для новостей)

В `config.env` добавьте тикер в нужную группу:

- **Быстрая игра (5m):** `TICKERS_FAST=SNDK,LITE,AMD` — только если по нему есть 5m данные (Yahoo).
- **Средние:** `TICKERS_MEDIUM=ALAB,MU,TER,AMD`
- **Вдолгую:** `TICKERS_LONG=MSFT,GBPUSD=X,GC=F,^VIX,AMD`

От группы зависят: GAME_5m (только FAST), портфельная игра (MEDIUM + LONG или `TRADING_CYCLE_TICKERS`).

### 3. Новости

- **Investing.com News** — тикеры берутся из **всех групп** (FAST + MEDIUM + LONG). Достаточно добавить AMD в одну из переменных выше; при необходимости добавьте ключевые слова в `config.env`:
  ```bash
  INVESTING_NEWS_TICKER_KEYWORDS=AMD:Advanced Micro Devices
  ```
  (для части тикеров ключевые слова уже заданы в коде в `services/investing_news_fetcher.py`.)

- **Alpha Vantage:** в `config.env` добавьте тикер в список для новостей:
  ```bash
  EARNINGS_TRACK_TICKERS=MSFT,SNDK,MU,LITE,ALAB,TER,AMD
  ```

- **LLM-новости** (если используете): добавьте в
  ```bash
  LLM_NEWS_TICKERS=SNDK,AMD
  ```

### Кратко для AMD

1. `python update_prices.py AMD` — один раз для цен.
2. В `config.env`: добавить `AMD` в `TICKERS_MEDIUM` (или в `TICKERS_LONG`).
3. При необходимости: добавить `AMD` в `EARNINGS_TRACK_TICKERS` и/или `LLM_NEWS_TICKERS`.

После этого AMD будет в `/tickers`, в портфельной игре (если в MEDIUM/LONG) и в новостях (Investing.com по группе, Alpha Vantage и LLM — по конфигу).

## См. также

- [GAME_SNDK.md](GAME_SNDK.md) — игра по 5m, быстрые тикеры
- [TELEGRAM_BOT_SETUP.md](TELEGRAM_BOT_SETUP.md) — дашборд, рассылка сигналов
- [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md) — бюджет и лимиты компании для игры
