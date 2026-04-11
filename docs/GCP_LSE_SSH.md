# SSH-доступ к VM LSE на GCP (дамп БД, `export_lse_gcp_kb_quotes.sh`)

Скрипт **`scripts/export_lse_gcp_kb_quotes.sh`** ходит на сервер по **`ssh $SSH_TARGET`** (по умолчанию **`SSH_TARGET=gcp-lse`**). Удобно завести alias в **`~/.ssh/config`**.

## Пример `~/.ssh/config` (текущий хост)

Публичный ключ с вашей машины должен быть в **`authorized_keys`** на VM (у вас: ключ в файле **`~/.ssh/1234`** — это **имя файла приватного ключа**, не пароль).

```ssh-config
# Новый хост GCP для LSE — внешний IP см. HostName; ключ в authorized_keys на VM
Host gcp-lse
    HostName 104.154.205.58
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

`\copy … TO STDOUT` пишет во **временный файл `*.part.<pid>`**, затем при успехе он переименовывается в `*.csv`. Пока поток не закончился, **в терминале нет прогресса** — это нормально. Скрипт перед выгрузкой печатает **COUNT(*)** по окну дней; во время записи смотрите рост **`.part.*`**:  
`watch -n2 'ls -lh tradenews/datasets/lse_gcp_dump/*.part.* 2>/dev/null'`.

**Пустые 0-байтные `knowledge_base_*.csv`** — неудачный прогон (ошибка SSH/psql, прерывание). Их можно удалить. При сбое скрипт сохраняет **`*.stderr.log`** и выходит с ошибкой, не подменяя итог пустым CSV.

Выгрузка KB + quotes в **`tradenews/datasets/lse_gcp_dump/`** (из корня репозитория **`lse/`**):

```bash
export SSH_TARGET=gcp-lse   # опционально — это значение по умолчанию в скрипте
export DAYS=90
./scripts/export_lse_gcp_kb_quotes.sh
```

Другой ключ или пользователь: переопределите **`SSH_TARGET=user@104.x.x.x`** или поправьте `Host gcp-lse` локально.

### «Connection timed out» / banner exchange

Сообщение вроде **`Connection timed out during banner exchange`** значит, что **TCP до порта 22 не доходит** — это **не** ошибка Docker и не пустая база.

Проверьте по порядку:

1. **VM запущена** в Google Cloud Console (Compute Engine → VM instances).
2. **Внешний IP** — у инстанса может быть **ephemeral** и смениться после перезапуска; обновите **`HostName`** в `~/.ssh/config`.
3. **Firewall GCP** — правило **ingress tcp:22** с подходящим source (0.0.0.0/0 или ваш IP). VPC network → Firewall.
4. С вашей сети: **`nc -vz <External-IP> 22`** или **`ssh -v gcp-lse`** (последние строки лога).

После **reboot** у инстанса с **ephemeral** IP часто меняется **HostName** — обновите блок `Host gcp-lse` (сейчас в примере ниже актуальный IP; при следующей смене поправьте снова).

## Безопасность

Если репозиторий **публичный** — не храните здесь реальные IP/имена пользователей; вынесите в wiki или шаблон с плейсхолдерами. Для **приватного** clone — по договорённости команды.
