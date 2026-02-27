# Запуск сервисов для игры (пока только SNDK)

Пошаговая подготовка и запуск всего, что нужно для быстрой игры 5m по SNDK: бот, рассылка сигналов, игра в trade_history, опционально веб и cron.

## 1. Подготовка конфигурации

Скопируйте пример и задайте обязательные переменные:

```bash
cp config.env.example config.env
nano config.env   # или любой редактор
```

**Обязательно для игры с SNDK:**

| Переменная | Описание |
|------------|----------|
| `DATABASE_URL` | PostgreSQL (база `lse_trading` создаётся при init_db) |
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather |
| `TELEGRAM_SIGNAL_CHAT_ID` или `TELEGRAM_SIGNAL_CHAT_IDS` | Куда слать сигналы (личка/группа). Или `TELEGRAM_DASHBOARD_CHAT_ID` / первый из `TELEGRAM_ALLOWED_USERS` |
| `TICKERS_FAST=SNDK` | Пока только SNDK для быстрой игры (без других тикеров) |

**По желанию:**

- `TELEGRAM_ALLOWED_USERS=user_id1,user_id2` — ограничение доступа к боту
- `TELEGRAM_SIGNAL_MENTIONS=@user1,@user2` — упоминания в сообщении о сигнале
- `USE_LLM_NEWS=true` и `OPENAI_API_KEY` — свежие новости от LLM перед решением

## 2. База данных

Инициализация схемы и таблиц (в т.ч. `trade_history` для игры):

```bash
python init_db.py
```

При необходимости обновите котировки (для SNDK и VIX):

```bash
python update_prices.py SNDK,^VIX
```

## 3. Проверка 5m по SNDK

Убедиться, что по SNDK есть 5m данные (в торговые часы США обычно есть):

```bash
python scripts/check_fast_tickers_5m.py
```

Должно быть: `SNDK: ок, баров за 1 дн.: ...`

## 4. Запуск Telegram-бота

Бот даёт команды `/recommend5m SNDK`, `/chart5m SNDK`, `/dashboard`, получает сигналы и ведёт игру в trade_history.

```bash
python scripts/run_telegram_bot.py
```

Оставить в фоне (опционально):

```bash
nohup python scripts/run_telegram_bot.py >> logs/telegram_bot.log 2>&1 &
echo $! > .telegram_bot.pid
```

## 5. Cron (рассылка сигналов и игра)

Установить задачи (игра 5m — пока раз в час пн–пт, новости и т.д.):

```bash
./setup_cron.sh
```

Проверить:

```bash
crontab -l
```

Ручной прогон сигнала/игры (без ожидания cron):

```bash
python scripts/send_sndk_signal_cron.py
```

Логи: `logs/cron_sndk_signal.log`

## 6. Веб-интерфейс (опционально)

API рекомендации 5m и мониторинг:

```bash
python web_app.py
# http://localhost:8000  — главная, /monitor — дашборд с автообновлением
```

## Краткий чеклист

1. [ ] `config.env` с `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_SIGNAL_CHAT_ID` (или `TELEGRAM_SIGNAL_CHAT_IDS`), `TICKERS_FAST=SNDK`
2. [ ] `python init_db.py`
3. [ ] `python scripts/check_fast_tickers_5m.py` — SNDK с 5m
4. [ ] `python scripts/run_telegram_bot.py` (или в фоне)
5. [ ] `./setup_cron.sh` для рассылки сигналов и игры по расписанию
6. [ ] При необходимости: `python web_app.py`

После этого игра по SNDK работает: cron по расписанию проверяет 5m, при BUY/STRONG_BUY шлёт уведомление и пишет вход в trade_history; при SELL или через 2 дня — выход и PnL. Сделки портфельной игры (тикеры MEDIUM/LONG по cron 9, 13, 17) тоже отправляют уведомления в те же чаты (`TELEGRAM_SIGNAL_CHAT_IDS`); в боте `/history [тикер] [N]` показывает историю с фильтром по тикеру и отображением стратегии.

---

## Завершение игры и анализ

- **Остановить бота:** Ctrl+C в терминале с `run_telegram_bot.py`.
- **Отключить авто-сигналы и игру по расписанию:** `crontab -e` → закомментировать строку с `send_sndk_signal_cron.py`.
- **Посмотреть результаты:** см. раздел «Завершение игры и анализ результатов» в [GAME_SNDK.md](GAME_SNDK.md) — SQL-сводки по win rate и среднему PnL, пример на Python через `get_recent_results`, и что менять в коде для улучшения стратегии.
