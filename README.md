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

### Основа
| Документ | Содержание |
|----------|------------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Компоненты, хранилища, dataflow |
| [BUSINESS_PROCESSES.md](BUSINESS_PROCESSES.md) | Пошаговые процессы и диаграммы |
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
| [docs/KNOWLEDGE_BASE_FIELDS.md](docs/KNOWLEDGE_BASE_FIELDS.md) | Поля KB, кроны backfill |
| [docs/VECTOR_KB_USAGE.md](docs/VECTOR_KB_USAGE.md) | Векторный поиск, embedding |

### Игра 5m и кроны
| Документ | Содержание |
|----------|------------|
| [docs/RUN_GAME_SERVICES.md](docs/RUN_GAME_SERVICES.md) | Запуск бота, крона 5m, SNDK |
| [docs/CRONS_AND_TAKE_STOP.md](docs/CRONS_AND_TAKE_STOP.md) | Расписание, тейк/стоп, соответствие боту |
| [docs/GAME_SNDK.md](docs/GAME_SNDK.md) | Сценарий GAME_5M по быстрым тикерам |
| [docs/GAME_5M_DEAL_PARAMS_JSON.md](docs/GAME_5M_DEAL_PARAMS_JSON.md) | `context_json` сделок, примеры, эволюция полей |
| [docs/TIMEZONES.md](docs/TIMEZONES.md) | `trade_history.ts` и отображение в ET |

### Деплой и внешние сервисы
| Документ | Содержание |
|----------|------------|
| [docs/DEPLOY_INSTRUCTIONS.md](docs/DEPLOY_INSTRUCTIONS.md) | VM, Docker, Cloud Run |
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
| [ROADMAP.md](ROADMAP.md) | Планы развития |
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

**Версия:** 1.4.0  
**Обновление документации:** 2026-03-27 — выровнена иерархия README → ARCHITECTURE → тематические документы; устаревшие материалы перенесены в [docs/archive/](docs/archive/).
