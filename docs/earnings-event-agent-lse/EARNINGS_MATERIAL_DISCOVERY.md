# Поиск документов для earnings (когда только 8-K и пустые hints)

## Почему не хватает материалов

| Симптом | Причина |
|---------|---------|
| `scenario_hints: []` после extract | В LLM ушёл только короткий **8-K** (~4–5k символов) |
| Fool не находится | **429 cooldown** на Motley Fool (`/app/logs/.fool_transcript_cooldown_until`) или **дата в URL ±1 день** от календаря |
| Только `yfinance` в guidance | Extract не запускался на transcript — нет строки в `earnings_material` |

## Источники в коде (по приоритету)

1. **`services/earnings_material_catalog.py`** — проверенные URL (обходит Fool probe). Добавляйте сюда, если URL уже известен.
2. **Motley Fool** — `fool_transcript_near_date`: slug + дата; окно **±1 день** (`EARNINGS_FOOL_PROBE_DAY_WINDOW=1`).
3. **SEC 8-K primary** — тело формы (часто мало текста).
4. **SEC 8-K exhibits** — `sec_8k_exhibit_materials_near_date`: парсит `*-index.htm`, ищет exhibit с `transcript` в имени.
5. **`discover_links`** на `ir_event_page` после ingest (см. `sync --discover-links`).

## Операционные команды

```bash
# Показать кандидаты URL без записи в БД
python scripts/discover_earnings_material_sources.py --symbol NBIS --event-date 2026-05-13

# Зарегистрировать в earnings_material (sync upsert)
python scripts/discover_earnings_material_sources.py --symbol NBIS --event-date 2026-05-13 --register

# Сбросить Fool cooldown после 429 (осторожно — снова можно получить 429)
rm -f /app/logs/.fool_transcript_cooldown_until

# Дальше стандартно
python scripts/ingest_earnings_materials.py --symbol NBIS --since 2026-05-01 --limit 5
python scripts/extract_earnings_material_facts.py --symbol NBIS --event-date 2026-05-13 --force-reextract
python scripts/apply_earnings_scenario_labels.py --symbols NBIS --no-universe
```

## Ручной поиск (если автоматика пустая)

| Источник | Где искать |
|----------|------------|
| Motley Fool | `https://www.fool.com/earnings/call-transcripts/YYYY/MM/DD/` + slug тикера |
| IR компании | `investor.*` → Press release / Events / Quarterly results |
| SEC | [EDGAR](https://www.sec.gov/cgi-bin/browse-edgar) → 8-K → Exhibits → `ex99` transcript `.htm` |
| GuruFocus / Investing.com | Запасные зеркала (не в autoprep по ToS; можно внести в catalog вручную) |

Примеры зашиты в catalog: **NBIS** 2026-05-13 Fool, **AMD** 2026-05-05 Fool (+1 день) и IR press release.

## Конфиг

```env
EARNINGS_FOOL_PROBE_DAY_WINDOW=1
EARNINGS_FOOL_MAX_PROBE=18
FOOL_TRANSCRIPT_COOLDOWN_AFTER_429_HOURS=6
```

Autoprep по умолчанию **`--no-auto-fool`** (не бьёт Fool каждые 2h). Для разового обогащения: `run_earnings_intelligence_autoprep.py --auto-fool` или catalog + `--register`.
