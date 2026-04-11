# SSH-доступ к VM LSE на GCP (дамп БД, `export_lse_gcp_kb_quotes.sh`)

Скрипт **`scripts/export_lse_gcp_kb_quotes.sh`** ходит на сервер по **`ssh $SSH_TARGET`** (по умолчанию **`SSH_TARGET=gcp-lse`**). Удобно завести alias в **`~/.ssh/config`**.

## Пример `~/.ssh/config` (текущий хост)

Публичный ключ с вашей машины должен быть в **`authorized_keys`** на VM (у вас: ключ в файле **`~/.ssh/1234`** — это **имя файла приватного ключа**, не пароль).

```ssh-config
# Новый хост GCP для LSE — внешний IP см. HostName; ключ в authorized_keys на VM
Host gcp-lse
    HostName 104.197.235.201
    User ai8049520
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    ServerAliveInterval 60
    ServerAliveCountMax 3
    IdentityFile ~/.ssh/1234
    IdentitiesOnly yes
```

Проверка:

```bash
ssh gcp-lse 'docker ps --format "{{.Names}}" | grep -q lse-postgres && echo OK'
```

### Почему после «Exporting knowledge_base…» долго тишина

`\copy … TO STDOUT` гонит весь CSV в один поток в локальный файл. Пока поток не закончился, **в терминале нет прогресса** — это нормально. Скрипт перед выгрузкой печатает **COUNT(*)** по окну дней; во время записи можно в другом окне: `watch -n2 ls -lh tradenews/datasets/lse_gcp_dump/knowledge_base_last*.csv` (последний файл).

Выгрузка KB + quotes в **`tradenews/datasets/lse_gcp_dump/`** (из корня репозитория **`lse/`**):

```bash
export SSH_TARGET=gcp-lse   # опционально — это значение по умолчанию в скрипте
export DAYS=90
./scripts/export_lse_gcp_kb_quotes.sh
```

Другой ключ или пользователь: переопределите **`SSH_TARGET=user@104.x.x.x`** или поправьте `Host gcp-lse` локально.

## Безопасность

Если репозиторий **публичный** — не храните здесь реальные IP/имена пользователей; вынесите в wiki или шаблон с плейсхолдерами. Для **приватного** clone — по договорённости команды.
