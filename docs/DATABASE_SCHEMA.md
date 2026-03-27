# Схема базы данных `lse_trading`

Контекст: [ARCHITECTURE.md](ARCHITECTURE.md) — как таблицы участвуют в потоках данных.

Источник правды по структуре: **`init_db.py`** (создание таблиц и расширение **pgvector**). Дополнительные колонки могут добавляться миграциями в `scripts/` (например `migrate_add_news_fields.py`, `migrate_add_kb_ingested_at.py`). Имя БД по умолчанию: **`lse_trading`**.

---

## Расширения PostgreSQL

| Расширение | Назначение |
|------------|------------|
| **vector** | Векторный тип для `knowledge_base.embedding` (pgvector; образ Docker `pgvector/pgvector`). |

---

## Таблица `quotes`

Дневные котировки (Yahoo и др.). Уникальность: **`(date, ticker)`**.

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | SERIAL | PK |
| date | TIMESTAMP | Дата свечи |
| ticker | VARCHAR(10) | Тикер |
| open, high, low, close | DECIMAL | OHLC |
| volume | BIGINT | Объём |
| sma_5 | DECIMAL | SMA 5 дней |
| volatility_5 | DECIMAL | Волатильность 5 дней |
| rsi | DECIMAL(5,2) | RSI (0–100), Finviz и др. |
| macd, macd_signal, macd_hist | DECIMAL | MACD |
| bbands_upper, bbands_middle, bbands_lower | DECIMAL | Полосы Боллинджера |
| adx | DECIMAL(5,2) | ADX |
| stoch_k, stoch_d | DECIMAL(5,2) | Стохастик |

---

## Таблица `knowledge_base`

Новости и события; эмбеддинги и исходы — в той же таблице.

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | SERIAL | PK |
| ts | TIMESTAMP | Время публикации/события |
| ticker | VARCHAR(10) | Тикер или MACRO/US_MACRO |
| source | VARCHAR(100) | Источник |
| content | TEXT | Текст |
| sentiment_score | DECIMAL(3,2) | Тональность |
| insight | TEXT | Краткий вывод |
| event_type | VARCHAR(50) | NEWS, EARNINGS и т.д. |
| importance | VARCHAR(10) | HIGH / MEDIUM / LOW |
| link | TEXT | URL |
| region | VARCHAR(20) | Регион (миграция; не все строки заполнены) |
| ingested_at | TIMESTAMPTZ | Момент загрузки записи в БД (крон) |
| embedding | vector(768) | Вектор для семантического поиска |
| outcome_json | JSONB | Исход события (цена через N дней и т.д.) |

Индекс для поиска: **`kb_embedding_idx`** (ivfflat по `embedding`, при достаточном числе строк с заполненным embedding — см. `init_db.py`).

Подробнее по полям и кронам: [KNOWLEDGE_BASE_FIELDS.md](KNOWLEDGE_BASE_FIELDS.md), [NEWS.md](NEWS.md).

---

## Таблица `portfolio_state`

Текущее состояние портфеля (симуляция).

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | SERIAL | PK |
| ticker | VARCHAR(20) | UNIQUE; тикер или **CASH** для баланса |
| quantity | DECIMAL | Количество |
| avg_entry_price | DECIMAL | Средняя цена входа |
| last_updated | TIMESTAMP | Обновление |

---

## Таблица `trade_history`

История сделок (портфельная игра, **GAME_5M**, и др.). Разделение по **`strategy_name`**.

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | SERIAL | PK |
| ts | TIMESTAMP | Время сделки (naive; смысл задаёт **ts_timezone**) |
| ticker | VARCHAR(20) | Тикер |
| side | VARCHAR(10) | BUY / SELL |
| quantity | DECIMAL | Объём |
| price | DECIMAL | Цена исполнения |
| commission | DECIMAL | Комиссия |
| signal_type | VARCHAR(20) | STRONG_BUY, TAKE_PROFIT, TIME_EXIT и т.д. |
| total_value | DECIMAL | Номинал сделки |
| sentiment_at_trade | DECIMAL | Sentiment на момент сделки |
| strategy_name | VARCHAR(50) | Например GAME_5M, Momentum, … |
| ts_timezone | VARCHAR(50) | Таймзона метки `ts` (по умолчанию `Europe/Moscow`) |
| take_profit_usd, stop_loss_usd, mfe_usd, mae_usd | DECIMAL | Уровни в USD (схема init_db) |
| take_profit, stop_loss, mfe, mae | DECIMAL | Альтернативные имена из миграции телеметрии; **report_generator** и **execution_agent** используют **`take_profit`**, **`stop_loss`**, **`mfe`**, **`mae`** без суффикса |
| context_json | JSONB | Контекст входа (прогноз 5m, тейк % и т.д.) |

Подробные **примеры JSON**, различие полного/старого формата, эволюция полей и замечания о потерях параметров: [GAME_5M_DEAL_PARAMS_JSON.md](GAME_5M_DEAL_PARAMS_JSON.md) (§5–7).

**Таймзоны:** в БД **`ts`** хранится в **московском времени** (или согласно **`ts_timezone`**); для графиков и UI перевод в **ET** делается при чтении (`trade_ts_to_et` и т.д.). См. [TIMEZONES.md](TIMEZONES.md).

---

## Таблица `strategy_parameters`

Динамические параметры (RLM, глобальные и по сущности).

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | SERIAL | PK |
| target_entity | VARCHAR(20) | GLOBAL, кластер, тикер |
| parameter_name | VARCHAR(50) | Имя параметра |
| parameter_value | JSONB | Значение |
| valid_from, valid_to | TIMESTAMP | Интервал действия |
| updated_by | VARCHAR(50) | Кто обновил |

**config_loader.get_dynamic_config_value** и **risk_manager** читают эту схему. Отдельный модуль **`utils/parameter_store.py`** в некоторых инсталляциях ожидает другую форму таблицы (`strategy_name`, `target_identifier`, `parameters`); при ошибках SQL проверьте фактическую структуру: `\d strategy_parameters` в psql.

---

## Экспорт и восстановление

- Таблицы LSE для переноса: см. **`scripts/export_pg_dump.sh`** (список `-t public.*`).
- Полное восстановление: **`scripts/restore_pg_dump.sh`**, для сторонних хостов: **`scripts/kerim_setup_db_from_dump.sh`**.

---

## Связанные документы

- [NEWS.md](NEWS.md) — knowledge_base и пайплайн новостей  
- [KNOWLEDGE_BASE_FIELDS.md](KNOWLEDGE_BASE_FIELDS.md) — поля KB и кроны  
- [TIMEZONES.md](TIMEZONES.md) — `trade_history.ts` и отображение  
- [MIGRATE_SERVER.md](MIGRATE_SERVER.md) — дамп/restore при переносе VM  
