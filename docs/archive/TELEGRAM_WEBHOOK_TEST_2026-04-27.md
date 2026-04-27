# Пошаговый тест: webhook вместо polling

Переключение с polling на webhook и проверка работы. Выполнять по шагам.

---

## Шаг 0. Остановить бота с polling (если запущен)

Если бот сейчас запущен через `./start_telegram_bot.sh` или `python scripts/run_telegram_bot.py`:

- В том терминале нажать **Ctrl+C**, либо
- Найти процесс: `pgrep -af run_telegram_bot` / `pgrep -af "api/bot_app"` и завершить его.

Иначе при включённом webhook polling всё равно не будет получать обновления (Telegram шлёт только на webhook).

---

## Шаг 1. Запустить webhook-сервер (терминал 1)

В **первом** терминале из корня проекта:

```bash
cd /home/cnn/lse
./start_telegram_bot_webhook.sh
```

Должно появиться что-то вроде:
`Uvicorn running on http://0.0.0.0:8080` и `LSE Telegram Bot API инициализирован`.

**Оставить этот терминал открытым.** Сервер должен работать всё время теста.

---

## Шаг 2. Запустить туннель (терминал 2)

Во **втором** терминале из корня проекта:

```bash
cd /home/cnn/lse
./scripts/run_tunnel.sh
```

Через несколько секунд в выводе появится строка вида:

```
>>> Публичный URL: https://XXXX-XX-XX-XX.trycloudflare.com
>>> Webhook для Telegram: https://XXXX-XX-XX-XX.trycloudflare.com/webhook
>>> Настройка: python scripts/setup_webhook.py --url https://XXXX-XX-XX-XX.trycloudflare.com/webhook
```

**Скопировать** полный URL webhook (с `/webhook` на конце). Он понадобится на шаге 3.

**Оставить туннель запущенным** — при закрытии URL перестанет работать.

---

## Шаг 3. Установить webhook в Telegram (терминал 3 или тот же 2)

В **третьем** терминале (или во втором, если туннель уже показал URL):

```bash
cd /home/cnn/lse
python scripts/setup_webhook.py --url https://ВАШ_URL_ИЗ_ШАГА_2/webhook
```

Подставить свой URL вместо `https://ВАШ_URL_ИЗ_ШАГА_2/webhook`.

Ожидаемый вывод: `✅ Webhook успешно настроен`.

Проверка (подставить свой URL):

```bash
curl https://ВАШ_URL_ИЗ_ШАГА_2/webhook/info
```

Должен вернуться JSON с полем `"url": "https://..."`.

---

## Шаг 4. Проверить бота в Telegram

1. Открыть Telegram и написать боту (например `/start` или любой текст).
2. Бот должен ответить.
3. В **терминале 1** (webhook-сервер) при этом появятся логи запросов `POST /webhook`.

Если ответа нет — проверить:
- терминал 1 и 2 не закрыты (сервер и туннель работают);
- URL в `setup_webhook.py` был с `https://` и с `/webhook` в конце;
- `curl .../webhook/info` возвращает корректный ответ.

---

## Шаг 5. Вернуться на polling (когда тест закончен)

1. Удалить webhook (чтобы Telegram снова отдавал обновления через getUpdates):

   ```bash
   cd /home/cnn/lse
   python scripts/setup_webhook.py --delete
   ```

2. Остановить webhook-сервер и туннель: **Ctrl+C** в терминалах 1 и 2.

3. Запустить бота в режиме polling:

   ```bash
   ./start_telegram_bot.sh
   ```

После этого бот снова работает в режиме polling.

---

## Краткая шпаргалка

| Шаг | Действие | Где |
|-----|----------|-----|
| 0 | Остановить polling-бота (Ctrl+C) | — |
| 1 | `./start_telegram_bot_webhook.sh` | Терминал 1 |
| 2 | `./scripts/run_tunnel.sh` → скопировать URL | Терминал 2 |
| 3 | `python scripts/setup_webhook.py --url https://XXX.trycloudflare.com/webhook` | Терминал 3 |
| 4 | Написать боту в Telegram | Telegram |
| 5 | `python scripts/setup_webhook.py --delete`; Ctrl+C в 1 и 2; `./start_telegram_bot.sh` | Когда тест закончен |
