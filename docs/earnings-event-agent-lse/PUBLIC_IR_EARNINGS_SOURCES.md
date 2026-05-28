# Официальные страницы отчётности и материалов по earnings

**Назначение:** держать под рукой **первичные** страницы investor relations и примеры конкретных кварталов (press release, презентация, транскрипт колла, follow-up), чтобы вручную или в будущем пайплайне агента скачивать текст/PDF. Ссылки на сторонние агрегаторы транскриптов — только как **дополнение**, с учётом их ToS и задержки публикации относительно IR.

**Важно:** URL событий иногда меняют при редизайне сайта; перед автоматизацией проверяйте актуальность. У разных эмитентов **фискальный квартал** не совпадает с календарным — в задачах агенту фиксируйте дату события, fiscal period и FY.

**Охват:** таблица ниже совпадает с **дефолтными акциями** из [docs/TICKER_GROUPS.md](../TICKER_GROUPS.md) (FAST + MEDIUM + LONG, без дублей), плюс **ASML** и **ARM** из рабочих примеров команды. Инструменты **GBPUSD=X, GC=F, ^VIX, CL=F, BZ=F** — не корпоративные акции; для них отдельный подраздел.

## Сводная таблица: хабы IR и квартальная отчётность

| Тикер | Компания | Хаб investor relations | Где искать квартал (типично) |
|-------|----------|------------------------|------------------------------|
| MSFT | Microsoft | https://www.microsoft.com/en-us/investor | Блок про earnings / прошлые звонки, презентации к релизу |
| META | Meta Platforms | https://investor.atmeta.com/ | Investor events → страница квартала (см. пример Q1 2026 ниже) |
| GOOGL | Alphabet | https://abc.xyz/investor/ | https://abc.xyz/investor/earnings/ — выбор года и квартала |
| AMZN | Amazon | https://ir.aboutamazon.com/overview/default.aspx | Тот же IR-хаб (часто дублируется входом с https://www.amazon.com/ir ) |
| NVDA | NVIDIA | https://investor.nvidia.com/ | Financial results / quarterly reports, events |
| AMD | Advanced Micro Devices | https://ir.amd.com/ | Latest financial results; press releases; SEC filings |
| INTC | Intel | https://www.intc.com/ | Latest financial results; filings |
| DELL | Dell Technologies | https://investors.delltechnologies.com/ | Latest earnings; events (webcast) |
| AVGO | Broadcom | https://investors.broadcom.com/ | News releases; events (earnings conference call) |
| ORCL | Oracle | https://investor.oracle.com/ | Latest quarter / investor news |
| PLTR | Palantir | https://investors.palantir.com/ | https://investors.palantir.com/financials/quarterly-results |
| ANET | Arista Networks | https://investors.arista.com/ | Press releases; Events (earnings webcast) |
| ALAB | Astera Labs | https://ir.asteralabs.com/ | News releases; при необходимости SEC |
| TER | Teradyne | https://www.teradyne.com/investors | Карточки релиза ведут на https://investors.teradyne.com/… |
| MU | Micron Technology | https://investors.micron.com/ | Quarterly results; events & presentations |
| LITE | Lumentum Holdings | https://investor.lumentum.com/ | https://investor.lumentum.com/quarterly-results/default.aspx |
| SNDK | SanDisk | см. § SNDK ниже | Официальный IR после спин-оффа — сверять с сайтом эмитента / SEC |
| ASML | ASML Holding | https://www.asml.com/en/investors/financial-results | Страница квартала, напр. `/financial-results/q1-2026` |
| ARM | Arm Holdings | https://investors.arm.com/financials/quarterly-annual-results | Список кварталов, транскрипты коллов |

## Не акции: FX, индекс волатильности, фьючерсы

Для **GBPUSD=X**, **GC=F**, **^VIX**, **CL=F**, **BZ=F** нет «квартального earnings call» эмитента в смысле equity. Для сценариев анализа используются котировки, макропубликации (CPI, Fed), отчёты по запасам нефти (EIA и т.д.), а не корпоративный IR из этой таблицы.

## META — пример страницы квартала

| Что | URL |
|-----|-----|
| Хаб событий / отчётности | https://investor.atmeta.com/ |
| Q1 2026 earnings event page (JS shell; для парсера v0 мало текста) | https://investor.atmeta.com/investor-events/event-details/2026/Q1-2026-Earnings-Call/default.aspx |
| Q1 2026 press release (рабочий HTML mirror) | https://www.prnewswire.com/news-releases/meta-reports-first-quarter-2026-results-302757852.html |
| Q1 2026 earnings call transcript (Fool) | https://www.fool.com/earnings/call-transcripts/2026/04/29/meta-meta-q1-2026-earnings-call-transcript/ |
| Q1 2026 earnings call transcript (PDF, официальный CDN) | https://s21.q4cdn.com/399680738/files/doc_financials/2026/q1/META-Q1-2026-Earnings-Call-Transcript.pdf |
| Q1 2026 earnings presentation (PDF) | https://s21.q4cdn.com/399680738/files/doc_financials/2026/q1/META-Q1-2026-Earnings-Call-Presentation.pdf |

## ASML — пример страницы квартала

| Что | URL |
|-----|-----|
| Хаб финансовых результатов | https://www.asml.com/en/investors/financial-results |
| Пример: Q1 2026 (15.04.2026) | https://www.asml.com/en/investors/financial-results/q1-2026 |

## ARM

| Что | URL |
|-----|-----|
| Квартальные и годовые результаты (в т.ч. earnings call transcript) | https://investors.arm.com/financials/quarterly-annual-results |

## SNDK (SanDisk)

| Что | URL |
|-----|-----|
| Официальный IR | После спин-оффа базовый домен IR может меняться — ориентир: раздел Investors на корпоративном сайте SanDisk и публикации в SEC (8-K, earnings release). Перед закладкой «канонического» URL сверяйте с актуальным сайтом эмитента. |
| Пример стороннего транскрипта (30.04.2026; у Fool в заголовке указан fiscal **Q3 2026** — сверяйте с календарём компании) | https://www.fool.com/earnings/call-transcripts/2026/04/30/sandisk-sndk-q3-2026-earnings-transcript/ |

## NVDA — драйвер сектора AI / гиперскейлеров

| Что | URL |
|-----|-----|
| Investor relations | https://investor.nvidia.com/ |
| Q1 FY2027 press release (2026-05-20; рабочий HTML) | https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-first-quarter-fiscal-2027 |
| Q1 FY2027 press release (IR page, JS shell) | https://investor.nvidia.com/news/press-release-details/2026/NVIDIA-Announces-Financial-Results-for-First-Quarter-Fiscal-2027/default.aspx |
| Q1 FY2027 earnings call transcript (Fool) | https://www.fool.com/earnings/call-transcripts/2026/05/20/nvidia-nvda-q1-2027-earnings-transcript/ |

## Связь с документацией проекта

- Группы тикеров в конфиге: [docs/TICKER_GROUPS.md](../TICKER_GROUPS.md)
- Контур earnings/event agent: [README.md](README.md), [EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md](EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md)
