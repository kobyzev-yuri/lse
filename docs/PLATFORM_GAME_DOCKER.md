# Platform API (Kerim): сеть и проверка из контейнера LSE

Связка: **отдельный** контейнер с образом `platform` (документация у Kerim: `kdoc.md`, `platform doc.md`). LSE в репозитории только делает **HTTP POST** на `/game` — **без** образа Kerim в GitHub.

## 0. Образ Kerim на сервере (не через git)

Файл **`platform.tar`** в репозиторий **не кладём** (см. `.gitignore`: `kerimsrv/*.tar`).

На сервере — **отдельная директория**, например `/opt/platform-game` или `~/platform-game`:

```bash
# с машины, где лежит platform.tar:
scp platform.tar user@your-server:/opt/platform-game/

# на сервере:
ssh user@your-server
sudo mkdir -p /opt/platform-game
cd /opt/platform-game
sudo docker load -i platform.tar
```

Дальше — запуск контейнера (см. ниже, порт **18080** на хосте). Код LSE и `config.env` деплоятся как обычно (`git pull` / ваш скрипт); URL Platform задаёте в `PLATFORM_GAME_API_URL` на этой же машине.

## 1. Конфликт порта 8080

В `docker-compose.yml` сервис **lse** уже пробрасывает **хост `8080` → веб LSE** (`uvicorn`). Образ Kerim в инструкции тоже слушает **8080 внутри** контейнера.

**На одном сервере** поднимайте Kerim так, чтобы **на хосте** был **другой** порт, например **18080**:

```bash
# образ уже загружен: docker load -i platform.tar
docker run -d --name platform-game --restart unless-stopped -p 127.0.0.1:18080:8080 platform
```

Проверка с хоста:

```bash
curl -sS -X POST http://127.0.0.1:18080/game \
  -H 'Content-Type: application/json' \
  -d '{"positions":[{"orderType":"MARKET","market":{"instrument":"TSLA","direction":"LONG","createdAt":"2026-03-21T12:00:00Z","takeProfit":320,"stopLoss":290,"units":5}}]}'
```

Ожидается JSON с ключами `notOpened`, `opened`, `closed`.

## 2. Доступ из контейнера `lse-bot` до хоста

Внутри контейнера **`127.0.0.1:18080` — это не хост**, а сам контейнер. Нужен адрес **Linux bridge к хосту**:

- часто срабатывает: **`http://172.17.0.1:18080/game`**
- или добавьте в сервис `lse` в `docker-compose.yml`:

```yaml
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

и в `config.env`:

```env
PLATFORM_GAME_API_URL=http://host.docker.internal:18080/game
```

(порт **18080** — тот, что вы пробросили на хост для Kerim.)

## 3. Прямой тест POST из контейнера LSE (до включения крона)

```bash
docker exec lse-bot python scripts/test_platform_game_api.py --url http://172.17.0.1:18080/game
```

или после записи `PLATFORM_GAME_API_URL` в `config.env`:

```bash
docker exec lse-bot python scripts/test_platform_game_api.py
```

Успех: HTTP 200 и тело JSON с тремя списками.

## 4. Включение на реальных входах 5m

В `config.env`:

```env
PLATFORM_GAME_API_ENABLED=true
PLATFORM_GAME_API_URL=http://172.17.0.1:18080/game
PLATFORM_GAME_API_TIMEOUT_SEC=15
```

Перезапуск бота/крона (как у вас принято: `docker compose restart lse`). После этого при новом входе `send_sndk_signal_cron` отправит в Telegram второе сообщение с ответом `/game`.

## 5. Не «прокидывают порт Kerim в порт LSE»

Обычно **не** делают `ports` Kerim внутрь образа LSE. Два независимых контейнера; LSE обращается по **URL по сети**. Альтернатива — общая Docker-сеть и имя сервиса `http://platform-game:8080/game`, если оба описаны в одном compose.
