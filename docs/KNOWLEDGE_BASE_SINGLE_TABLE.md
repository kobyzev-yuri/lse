# Архитектурное решение: одна таблица для новостей (knowledge_base)

## Проблема

Сейчас новости хранятся в двух таблицах:

| Таблица          | Колонки относительно knowledge_base |
|------------------|-------------------------------------|
| **knowledge_base** | id, ts, ticker, source, content, sentiment_score, insight, event_type, importance, link |
| **trade_kb**       | те же ts, ticker, event_type, content **плюс** только **embedding** и **outcome_json** |

По сути **trade_kb — это не отдельная сущность**, а та же самая «новость/событие» с двумя дополнительными полями (вектор для поиска и результат анализа исхода). Всё остальное дублируется, плюс синхронизация, дедупликация и два места чтения — лишняя сложность и риск расхождений.

## Решение

**Одна таблица — knowledge_base.** В неё добавляем два поля:

- **embedding** `vector(768)` — NULL для записей, для которых ещё не посчитан эмбеддинг; индекс ivfflat по `(embedding)` с условием `WHERE embedding IS NOT NULL`.
- **outcome_json** `JSONB` — результат анализа исхода (цена через N дней и т.д.); NULL, если анализ не выполнялся.

Итог:

- Один источник правды: все источники пишут в knowledge_base.
- Векторный поиск: `SELECT ... FROM knowledge_base WHERE embedding IS NOT NULL ORDER BY embedding <=> :query LIMIT N`.
- Анализ исходов: обновление `outcome_json` в той же строке.
- Никакой синхронизации между таблицами и никакого «подмешивания» trade_kb в get_recent_news.

## Отличия от текущей схемы (не существенные для «двух разных сущностей»)

- **Ручные записи «только в trade_kb»**: сейчас это строки без knowledge_base_id. В однотабличной схеме — просто строки в knowledge_base с `source = 'MANUAL'` (или отдельным значением), без дублирования в другой таблице.
- **Производительность**: индекс по вектору в большой таблице — нормальная практика; при необходимости можно ограничивать выборку по времени/тикеру до применения векторного поиска.

## План миграции (без кода, только шаги)

1. **Схема**  
   В knowledge_base добавить колонки `embedding vector(768)` и `outcome_json JSONB` (миграция в init_db или отдельный скрипт).

2. **Перенос данных**  
   Для каждой строки в trade_kb с заполненным `knowledge_base_id`: обновить в knowledge_base строку с этим id, проставив `embedding` и `outcome_json` из trade_kb. Для строк trade_kb без knowledge_base_id (ручные): вставить соответствующие строки в knowledge_base (content, ts, ticker, event_type, source='trade_kb' или 'MANUAL'), скопировать embedding и outcome_json.

3. **Код**  
   - VectorKB: `add_event` → INSERT в knowledge_base (с embedding); `search_similar` → SELECT из knowledge_base WHERE embedding IS NOT NULL; `sync_from_knowledge_base` заменить на «проставить embedding в knowledge_base где embedding IS NULL» (без второй таблицы).
   - NewsImpactAnalyzer: читать/писать ts, outcome_json из knowledge_base по id события.
   - AnalystAgent.get_recent_news: читать только из knowledge_base; убрать объединение с trade_kb.
   - Бот: убрать подпись «Векторная БЗ» / source= trade_kb — все записи из одной таблицы.

4. **Очистка**  
   Удалить таблицу trade_kb (или оставить только для отката, затем удалить). Убрать из init_db создание/миграции trade_kb и knowledge_base_id.

5. **Крон**  
   Заменить задачу «sync_vector_kb» на «backfill embedding в knowledge_base» (тот же скрипт, но обновляющий knowledge_base, а не вставляющий в trade_kb).

---

## Реализовано

Миграция выполнена:

- В **init_db** таблица trade_kb больше не создаётся; в knowledge_base добавлены колонки **embedding** и **outcome_json**, создаётся индекс **kb_embedding_idx**.
- Скрипт **scripts/migrate_trade_kb_to_knowledge_base.py** переносит данные из существующей trade_kb в knowledge_base и удаляет trade_kb. Запускать один раз на существующей БД перед обновлением кода.
- **VectorKB** пишет и читает только из knowledge_base; `sync_from_knowledge_base()` делает backfill embedding (UPDATE по строкам с `embedding IS NULL`).
- **NewsImpactAnalyzer** и **analyze_event_outcomes_cron** работают с knowledge_base.
- **get_recent_news** и бот читают только knowledge_base; подпись «Векторная БЗ» убрана.

**Итог:** одна таблица knowledge_base — единственное хранилище новостей/событий с опциональными embedding и outcome_json.
