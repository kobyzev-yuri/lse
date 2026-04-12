# План на ближайшую сессию (следующий рабочий день)

Чеклист после синка репозиториев. Дата черновика: **2026-04-13** (подставьте актуальную).

## Репозитории

1. **lse:** `git pull`, затем `git push` если локально были коммиты (`db/knowledge_pg`, `game_5m` и т.д.).
2. **tradenews** (отдельный clone): `git pull` / `git push` — бенч, доки, `news_impulse_plan`, отчёты.

## База данных (lse)

3. Применить миграции: `cd db/knowledge_pg && export DATABASE_URL=... && ./apply.sh`  
   - Если упадёт на уникальном `(ticker, link)` — сначала почистить дубли в `knowledge_base`.
4. Убедиться, что **`CREATE EXTENSION vector`** есть (обычно уже после `init_db.py`).

## Модели и бенчмарк

5. Дождаться **`ollama pull qwen2.5:14b`** (если ещё качается).
6. Прогон **только Gemini + Qwen 14B** (из корня tradenews):

   ```bash
   PYTHONPATH=. python scripts/run_model_benchmark.py \
     --build datasets/points/<ваш_points>.jsonl \
     --out-jsonl runs/eval_gemini_qwen14.jsonl \
     --report-json runs/benchmark_gemini_qwen14.json \
     --models qwen2.5:14b google:gemini-2.0-flash
   ```

7. Краткий отчёт в **`tradenews/docs/reports/`** (таблицы + выводы, без JSONL).

## Конфиг и прод

8. Проверить **`GAME_5M_TAKE_MOMENTUM_FACTOR`** в `config.env` на сервере после деплоя (верх теперь до **2.0** в коде).
9. **ProxyAPI / PROXYAPI_KEY** в `tradenews/config.env` на машине, где гоняется облако.

## Документация и план импульса

10. Прочитать **`tradenews/docs/news_impulse_plan.md`** §6 после миграций — при необходимости дописать 1–2 предложения «что сделали / что узнали».

---

*Файл можно обновлять в конце дня: зачеркнуть пункты, перенести хвост на следующую дату.*
