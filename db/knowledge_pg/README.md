# PostgreSQL: база знаний (NYSE / общая с LSE)

Канонический путь в монорепо **lse**: `db/knowledge_pg/`. Тот же набор файлов может дублироваться в чекауте **nyse** (симлинк `lse/nyse` → отдельный репозиторий).

SQL-слой дополняет уже существующую схему **lse**: `init_db.py` создаёт `knowledge_base` (в т.ч. `embedding vector(768)`, `outcome_json`), расширение **`vector`**, таблицу **`quotes`** (дневные свечи). Здесь — **идемпотентные** миграции для NYSE-контура, дедупа, часовых свечей и лога сигналов под калибровку / RAG.

## Когда что применять

1. Новый инстанс: сначала **`python init_db.py`** из корня **lse** (создаёт БД, `vector`, `knowledge_base`, `quotes`).
2. Затем из этого каталога: **`./apply.sh`** (или по одному файлу через `psql`), чтобы добавить поля и таблицы ниже.

Если БД уже давно в проде — только миграции **010+** (они через `IF NOT EXISTS`).

**Важно:** `010` создаёт уникальный индекс `(ticker, link)` при непустой `link`. Если в таблице уже есть **дубликаты** по этой паре, миграция упадёт — сначала почистить дубли (см. `scripts/cleanup_manual_duplicates.py` и аналоги).

## Содержимое `sql/`

| Файл | Назначение |
|------|------------|
| `001_extension_vector.sql` | `CREATE EXTENSION IF NOT EXISTS vector` (на случай голого инстанса без `init_db`). |
| `010_knowledge_base_nyse.sql` | Поля к **`knowledge_base`**: биржа, внешний id, хэш текста, сырой JSON источника; частичный UNIQUE на дедуп. |
| `020_market_bars.sql` | **`market_bars_daily`** и **`market_bars_1h`** — OHLCV отдельно от legacy `quotes` (универсальные `exchange` + `symbol`). |
| `030_news_signal_log.sql` | **`news_signal_log`** — решение + ссылка на строки KB для разметки forward-return. |
| `040_indexes.sql` | Индексы под выборки по времени/тикеру/бирже. |

## pgvector / RAG

- Эмбеддинги **уже** могут жить в **`knowledge_base.embedding`** (768-d, см. `services/vector_kb.py`).
- Для **chunk-RAG** (длинные тексты) можно позже добавить таблицу чанков; в `010` закладывается `content_sha256` для связки «один документ — много векторов».

## Переменные окружения для `apply.sh`

- `DATABASE_URL` — postgresql://… (как в lse `config.env`), **или**
- Компоненты: `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`.

## Связь с tradenews

Экспорт: SQL/view → JSONL точек (`ticker`, `decision_ts`, статьи). См. `tradenews/docs/news_impulse_plan.md` §6.
