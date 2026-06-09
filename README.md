# LSE Trading System

Торговая и аналитическая система: **PostgreSQL + pgvector**, дневные и **5m** данные (Yahoo), **новости** в `knowledge_base`, **песочница** (портфель + игра **GAME_5M**), **Telegram-бот** и **веб** (карточки 5m, мониторинг).

---

## Быстрый старт

```bash
pip install -r requirements.txt
cp config.env.example config.env   # задать DATABASE_URL, ключи Telegram/LLM по необходимости
python init_db.py                  # БД lse_trading, начальные котировки
python web_app.py                  # http://localhost:8080 (порт в compose может быть 8080)
```

Подробнее: [QUICKSTART.md](QUICKSTART.md).

### Интерфейсы

- **Telegram:** настройка, команды, чаты — [docs/TELEGRAM_BOT_SETUP.md](docs/TELEGRAM_BOT_SETUP.md)
- **Веб:** карточки 5m, мониторинг — [WEB_INTERFACE.md](WEB_INTERFACE.md)

---

## Архитектура и потоки данных

**С чего начать:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — компоненты, таблицы БД, обзорная схема dataflow.

**Детальные бизнес-процессы (Mermaid):** [BUSINESS_PROCESSES.md](BUSINESS_PROCESSES.md).

```
Внешние API (Yahoo, RSS, News…) → cron / скрипты → PostgreSQL (quotes, knowledge_base)
                                              ↓
                    recommend_5m / game_5m / execution_agent → trade_history, portfolio_state
                                              ↓
                              Telegram · web_app · отчёты
```

---

## Документация (иерархия)

**Версия:** [VERSION.md](VERSION.md) **v2.0.0** — статус процессов, ML-консолидация, ближайший план.

### Основа
| Документ | Содержание |
|----------|------------|
| [VERSION.md](VERSION.md) | Версия проекта, сводка статусов, история |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Компоненты, хранилища, dataflow |
| [BUSINESS_PROCESSES.md](BUSINESS_PROCESSES.md) | Процессы, диаграммы, **§0 статус** |
| [docs/PROJECT_STATUS_AND_ROADMAP.md](docs/PROJECT_STATUS_AND_ROADMAP.md) | Живой ops-срез |
| [docs/CONSOLIDATION_NEXT_PLAN.md](docs/CONSOLIDATION_NEXT_PLAN.md) | Спринты 3.2–3.3 |
| [docs/DATABASE_SCHEMA.md](docs/DATABASE_SCHEMA.md) | Таблицы `lse_trading`, поля |
| [QUICKSTART.md](QUICKSTART.md) | Установка и первый запуск |

### Конфигурация и данные
| Документ | Содержание |
|----------|------------|
| [config.env.example](config.env.example) | Шаблон переменных окружения |
| [DATA_UPDATES.md](DATA_UPDATES.md) | Обновление котировок, cron |
| [docs/CONFIG_OPTIONS_ANALYSIS.md](docs/CONFIG_OPTIONS_ANALYSIS.md) | Обзор опций конфига |

### Новости и база знаний
| Документ | Содержание |
|----------|------------|
| [docs/NEWS.md](docs/NEWS.md) | Источники, пайплайн в `knowledge_base` |
| [docs/NEWS_SIGNAL_ARCHITECTURE.md](docs/NEWS_SIGNAL_ARCHITECTURE.md) | Целевая архитектура новостного сигнала, dataflow, этапы имплементации |
| [docs/KNOWLEDGE_BASE_FIELDS.md](docs/KNOWLEDGE_BASE_FIELDS.md) | Поля KB, кроны backfill |
| [docs/VECTOR_KB_USAGE.md](docs/VECTOR_KB_USAGE.md) | Векторный поиск, embedding |

### Игра 5m и кроны
| Документ | Содержание |
|----------|------------|
| [docs/PORTFOLIO_GAME.md](docs/PORTFOLIO_GAME.md) | Портфельная игра: стратегии, CatBoost-вход, ML-тейк, trailing, справочник `PORTFOLIO_*` |
| [docs/ML_PORTFOLIO_CATBOOST.md](docs/ML_PORTFOLIO_CATBOOST.md) | Portfolio CatBoost: обучение, inference, execution keys |
| [docs/RUN_GAME_SERVICES.md](docs/RUN_GAME_SERVICES.md) | Запуск бота, крона 5m, SNDK |
| [docs/CRONS_AND_TAKE_STOP.md](docs/CRONS_AND_TAKE_STOP.md) | Расписание, тейк/стоп, соответствие боту |
| [docs/GAME_5M_CALCULATIONS_AND_REPORTING.md](docs/GAME_5M_CALCULATIONS_AND_REPORTING.md) | GAME_5M: алгоритм, тейк/стоп, висяки, отчётность |
| [docs/GAME_5M_DEAL_PARAMS_JSON.md](docs/GAME_5M_DEAL_PARAMS_JSON.md) | `context_json` сделок, примеры, эволюция полей |
| [docs/TIMEZONES.md](docs/TIMEZONES.md) | `trade_history.ts` и отображение в ET |

### Деплой и внешние сервисы
| Документ | Содержание |
|----------|------------|
| [docs/DEPLOY.md](docs/DEPLOY.md) | Регулярный Docker-деплой и обновление сервера |
| [docs/DEPLOY_GCP.md](docs/DEPLOY_GCP.md) | Cloud Run webhook для Telegram-бота |
| [docs/MIGRATE_SERVER.md](docs/MIGRATE_SERVER.md) | Перенос БД, дамп/restore |
| [docs/PLATFORM_GAME_DOCKER.md](docs/PLATFORM_GAME_DOCKER.md) | Platform API (Kerim), сеть Docker |

### Прочее
| Документ | Содержание |
|----------|------------|
| [docs/TRADING_GLOSSARY.md](docs/TRADING_GLOSSARY.md) | Термины |
| [docs/RISK_MANAGEMENT.md](docs/RISK_MANAGEMENT.md) | Лимиты, `risk_limits.json` |
| [docs/BACKTESTING_GUIDE.md](docs/BACKTESTING_GUIDE.md) | Бэктестинг |
| [docs/TELEGRAM_BOT_SETUP.md](docs/TELEGRAM_BOT_SETUP.md) | Настройка бота |
| [WEB_INTERFACE.md](WEB_INTERFACE.md) | Веб-интерфейс |
| [docs/README.md](docs/README.md) | Полная навигация по docs |
| [docs/archive/README.md](docs/archive/README.md) | Устаревшие и разовые документы |

---

## Возможности (кратко)

- **Портфельная игра:** Strategy Manager, `ExecutionAgent`, `trading_cycle_cron`, стратегии Momentum / Mean Reversion / Volatile Gap / Neutral.
- **Игра 5m (GAME_5M):** `recommend_5m`, `game_5m`, крон `send_sndk_signal_cron.py`, записи в `trade_history` с `context_json`.
- **LLM:** вопросы `/ask`, опционально вход 5m при `GAME_5M_ENTRY_STRATEGY=llm`, аналитика новостей.
- **Telegram:** сигналы, `/recommend5m`, `/chart5m`, `/pending`, `/game5m`, отчёты.
- **Веб:** карточки 5m, графики, мониторинг (см. `docker-compose.yml` — порт **8080**).

---

## Конфигурация

Файл **`config.env`** в корне репозитория (см. `config_loader.py`). Ключевые переменные:

- `DATABASE_URL` — PostgreSQL
- `TELEGRAM_BOT_TOKEN`, чаты сигналов — для бота и кронов
- `OPENAI_*` — при использовании LLM
- `GAME_5M_*`, `TICKERS_FAST` — игра 5m

Расширенные заметки: [CONFIG_SETUP.md](CONFIG_SETUP.md).

---

## Частые команды

```bash
python update_prices.py MSFT,SNDK
python scripts/run_telegram_bot.py
# или docker compose up -d
```

См. также [QUICKSTART.md](QUICKSTART.md), [docs/RUN_GAME_SERVICES.md](docs/RUN_GAME_SERVICES.md).

---

## Версия

**Версия:** 2.0.0 (ML Consolidation)  
**Обновление:** 2026-06-09 — канон ML, dual-track статус, gap/multiday OOS, архив устаревших планов. Подробности: [VERSION.md](VERSION.md).
