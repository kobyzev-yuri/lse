# Экспорт новостей и свечей из PostgreSQL (LSE) для tradenews / тестов

Кратко по **структуре проекта lse** и **таблицам**, откуда за месяц можно забрать данные для офлайн-метрик. Подключение к БД на VM (Docker) обычно через **SSH-туннель** — не коммитьте пароли и `DATABASE_URL` в репозиторий.

---

## 1. Структура репозитория lse (релевантное БД)

| Область | Пути |
|---------|------|
| Схема БД, создание таблиц | `init_db.py` |
| Docker: Postgres + бот | `docker-compose.yml` — сервис `postgres`, БД `lse_trading`, порт **на хосте** по умолчанию `127.0.0.1:5432` |
| Конфиг URL | `config.env` / `config_loader.py` — `DATABASE_URL` |
| Запись новостей | `services/rss_news_fetcher.py`, `services/newsapi_fetcher.py`, скрипты `scripts/add_manual_news.py` и др. → **`knowledge_base`** |
| Котировки | `update_prices.py`, `services/alphavantage_fetcher.py` и др. → **`quotes`** |

---

## 2. Таблица новостей: `knowledge_base`

Основные колонки (фактическая схема эволюционировала миграциями; полный набор смотрите `\d knowledge_base` на сервере):

| Колонка | Назначение |
|---------|------------|
| `id` | PK |
| `ts` | Время события / публикации (как в источнике) |
| `ticker` | Тикер |
| `source` | Источник |
| `content` | Текст (часто заголовок + тело + ссылка в одном поле) |
| `sentiment_score` | 0..1 (где заполнено) |
| `insight` | Краткий вывод |
| `event_type`, `region`, `importance` | Классификация |
| `link` | URL, дедупликация |
| `ingested_at` | Когда строка попала в БД |
| `embedding` | pgvector (768), для поиска |
| `outcome_json` | Исходы событий (если используется) |

**Важно:** формат **не тот же**, что nyse `serialize_news_article` (там отдельные `title`, `summary`, `provider_id`). Для tradenews нужен **конвертер**: из `content`/`link`/`ts` собрать dict под фикстуру или свою схему точки датасета.

---

## 3. Таблица свечей: `quotes`

Дневные данные (см. `init_db.py`):

| Колонка | Назначение |
|---------|------------|
| `date`, `ticker` | Уникальная пара `UNIQUE(date, ticker)` |
| `open`, `high`, `low`, `close`, `volume` | OHLCV |
| `sma_5`, `volatility_5`, `rsi` | Индикаторы |
| при миграциях | `macd`, `bbands_*`, `adx`, `stoch_*` и др. |

Для **лог-доходностей** tradenews сейчас удобнее **yfinance** на локали; альтернатива — считать forward return из **`quotes.close`** по `date` (нужно согласовать таймзону `date` с `decision_ts_utc`).

---

## 4. Доступ к Postgres на GCP (Docker на VM)

На VM в `docker-compose` порт Postgres часто проброшен как **`127.0.0.1:5432`** — с вашего ноутбука напрямую к БД не подключиться, только:

1. **SSH-туннель** (пример, хост из вашего `~/.ssh/config`):
   ```bash
   ssh -N -L 15432:127.0.0.1:5432 gcp-lse
   ```
   Локально тогда: `postgresql://postgres:ПАРОЛЬ@127.0.0.1:15432/lse_trading`

2. Или **один раз выполнить экспорт на VM**:
   ```bash
   docker exec -i lse-postgres psql -U postgres -d lse_trading -c "COPY (...)"
   ```
   и `scp` файлы на машину с tradenews.

Пароль — из `POSTGRES_PASSWORD` / `config.env` на сервере (не хранить в git).

---

## 5. Пример: выгрузка за последний месяц (CSV)

Подставьте границы дат и при необходимости таймзону (`ts` / `date` в БД — проверьте на сервере).

**Новости:**

```sql
\copy (SELECT id, ts, ticker, source, content, sentiment_score, event_type, region, importance, link, ingested_at
       FROM knowledge_base
       WHERE ts >= NOW() - INTERVAL '30 days'
       ORDER BY ts) TO '/tmp/kb_last30d.csv' WITH CSV HEADER;
```

**Свечи:**

```sql
\copy (SELECT date, ticker, open, high, low, close, volume, rsi
       FROM quotes
       WHERE date >= NOW() - INTERVAL '30 days'
       ORDER BY ticker, date) TO '/tmp/quotes_last30d.csv' WITH CSV HEADER;
```

На VM без файла на хосте можно:

```bash
docker exec lse-postgres psql -U postgres -d lse_trading -c "\copy (...) TO STDOUT WITH CSV HEADER" > kb_last30d.csv
```

(многострочный `\copy` в `-c` неудобен — проще интерактивный `psql` или маленький скрипт Python с `DATABASE_URL`.)

---

## 6. Связка с tradenews

1. **Цены:** либо как сейчас — **yfinance** по `ticker` + `decision_ts`, либо построить ряд из **`quotes`** и считать \(\ln(P_{t+h}/P_t)\) в том же часовом поясе, что `date`.
2. **Новости:** сгруппировать строки `knowledge_base` по `(ticker, decision_ts)` (например окно 48h до решения), смаппить в JSON-массив статей для `DatasetPoint.articles_snapshot` / файла в `datasets/articles/`.
3. Тикеры «по всем» — взять `SELECT DISTINCT ticker FROM quotes UNION SELECT DISTINCT ticker FROM knowledge_base` с фильтром по интересующему списку (GAME_5M и т.д.).

Следующий шаг в коде (по желанию): скрипт `tradenews/scripts/import_lse_kb_csv.py` или SQL→JSONL в репозитории lse под ваш формат точек.

---

## 7. Автоматический перенос в `tradenews/datasets/lse_gcp_dump/`

Из корня репозитория `lse/` (нужен SSH по ключу к VM, контейнер `lse-postgres`):

```bash
export SSH_TARGET=user@host   # или Host из ~/.ssh/config
export DAYS=90
./scripts/export_lse_gcp_kb_quotes.sh
```

В CSV **нет** колонки `embedding`. Подробности: `tradenews/datasets/lse_gcp_dump/README.md`.

После выгрузки для набора тикеров (**PREMARKET_STRESS_TICKERS** + **TICKERS_LONG** + **TICKERS_FAST**) см. **`tradenews/datasets/tickers_game_universe.txt`** и скрипт **`tradenews/scripts/build_game_dataset_from_kb_dump.sh`** → JSONL точек для `build_eval_from_points.py`.

---

## 8. Безопасность

- Не коммитьте **IP**, **ключи**, **пароли**, полный **`DATABASE_URL`**.
- Для команды используйте шаблон: SSH config alias + переменные окружения на машине разработчика.
