# Тетрадка: пробелы автозаполнения и источников

Рабочий backlog. Цель — **последовательно** закрывать дыры, не ломая правило: цифры/факты по кнопке → сверка → суждение Насти → OK.

Связанные доки: [NOTEBOOK_FUNDAMENT_REGLEMENT.md](NOTEBOOK_FUNDAMENT_REGLEMENT.md), [earnings-event-agent-lse/](earnings-event-agent-lse/).

---

## Сейчас уже тянется по кнопке

| Карточка | Источник | Что заполняет |
|----------|----------|---------------|
| Фундамент | Yahoo + `earnings_material` / Event Brief | биржа, HQ, листинг, 4 метрики, маржа%, черновик сути/финансов, `filing_url`, CapEx/маржа-заметки из extract |
| Ожидания | Event Brief (приоритет) + KB | driver, rev/EPS, leading, CapEx, маржа, guidance, дата·BEAT/MISS·реакция, why (черновик) |

**Не пишет кнопка (намеренно):** плюсы/риски, key clients, слой/сектор на Профиле, `tactics_map_ru`, `risk_shift_ru`, авто-OK.

---

## A. Недостатки автозаполнения (качество / покрытие)

| # | Проблема | Следствие | Направление фикса |
|---|----------|-----------|-------------------|
| A1 | Brief часто `partial` — LLM extract не прогнан или materials пусты | Ожидания падают на заголовки KB; мало фактов | Дожать calendar → materials → extract на G1; алерт «no LLM» в sufficiency |
| A2 | KB-fallback = эвристика по title | Чужой/шумный заголовок в поле watch | Только brief-структура; KB — в подсказку/ссылку, не в value |
| A3 | Yahoo TTM/leases ≠ 10-K FY | Черновик метрик врёт до сверки | Note + cross-check FMP/Intrinio; после earnings — filing |
| A4 | Нет авто-обновления после нового earnings | Карточка устаревает, пока Настя не жмёт кнопку | Опц. «есть новый brief» badge / diff к сохранённому |
| A5 | Иностранные эмитенты (нет 10-Q / другой IR) | SEC-путь и surprise % дырявые | Каталог IR + 6-K/20-F; явный статус «non-US» |
| A6 | `filing_url` брал голый 8-K | Обёртка без цифр | **частично:** ranking → Ex99.1 / press_release; 8-K shell demoted |
| A7 | Суждение никогда не черновится | Пустые тактика/риск-shift | Оставить так *или* опц. draft-only под approve (не default) |
| A8 | Нет единой кнопки «всё, что есть» | Две кнопки (Фундамент / Ожидания), разный охват | Одна «синхронизировать черновики» с чеклистом источников |

---

## B. Отсутствующие / слабые источники

| # | Источник | Статус | Зачем для тетрадки |
|---|----------|--------|--------------------|
| B1 | **FMP** fundamentals | ключ demo; 402 на части G1 | Cross-check кэш/FCF/долг vs Yahoo |
| B2 | **Intrinio** | нет ключа / код не подключён | SEC-normalized цифры паспорта |
| B3 | SEC companyfacts в UI | снят (битые периоды) | Не возвращать без решения по кэшу/долгу |
| B4 | Полный IR-сайт (JS shells) | только curated catalog + light discovery | PR / slides / transcript без ручного URL |
| B5 | Motley Fool / SA full article body | SA titles в KB; Fool опц. в earnings | Лучший leading/guidance, если extract пуст |
| B6 | Consensus / estimates feed | нет отдельного API в notebook | BEAT/MISS до/вместо surprise из LLM |
| B7 | Key clients / сегменты | нет стабильного API | Остаётся ручным (10-K Note) или LLM→approve |
| B8 | Invest house consensus | вне pipeline | Плюсы/риски — только Настя + консенсус руками |

---

## C. Порядок устранения (план)

1. **Ops brief:** G1 — materials + extract; в UI sufficiency показывать `partial` / no materials. *(A1)*
2. **Жёсткий приоритет brief:** не затирать пустым KB title структурированные поля. *(A2)*
3. ~~**filing_url ranking** по Ex99/press vs 8-K shell~~ *(A6 — сделано 2026-08-03)*. Дальше: 10-K/10-Q PDF preference.
4. **FMP spike** на G1 кэш·FCF·долг → решить keep Yahoo / dual. *(B1, A3)*
5. **Badge «новый earnings»** vs сохранённый overlay. *(A4)*
6. **IR catalog** расширить non-US / JS-gap тикеры. *(A5, B4)*
7. **Intrinio** — после sales/trial. *(B2)*
8. Опционально: единая sync-кнопка + estimates feed. *(A8, B6)*

Каждый пункт — отдельный маленький PR + правка этой таблицы (статус ✓ / дата).

---

## D. Антипаттерны (не делать «заодно»)

- Авто-OK overlay без Насти.
- Писать плюсы/риски / tactics из LLM в сохранённую карточку без approve.
- Считать Yahoo или SA истиной по цифрам.
- Вернуть SEC companyfacts-кнопку без фикса периодов.
- Деплой костылём `scp` вместо git + `deploy_from_github.sh`.
