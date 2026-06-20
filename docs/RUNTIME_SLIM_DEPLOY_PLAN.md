# План: зачистка runtime и slim-deploy (без docs/tests в образе)

**Статус:** roadmap (2026-06). **Цель:** уменьшить диск/RAM на GCP VM, ускорить `docker build`, не тащить в prod то, что нужно только разработчикам.

**Связанные документы:** [DEPLOY.md](DEPLOY.md), [archive/CLEANUP_CONFIG_AND_CODE_PLAN_2026-05-07.md](archive/CLEANUP_CONFIG_AND_CODE_PLAN_2026-05-07.md) (аудит конфига — параллельный трек).

---

## 1. Три слоя «где лежит код»

На VM сейчас **три независимых места** — путать их нельзя:

```text
┌─────────────────────────────────────────────────────────────┐
│  HOST: ~/lse (git clone)                                     │
│  · deploy_from_github.sh, crontab shell wrappers             │
│  · config.env, logs/, local/ (volumes)                       │
│  · .git/ — весь репозиторий (~170M+ с историей)            │
└───────────────────────────┬─────────────────────────────────┘
                            │ docker compose build (context = .)
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  IMAGE lse-bot:latest  →  /app                               │
│  · COPY . .  — СЕЙЧАС копируется ВСЁ (docs, tests, archive) │
│  · Python runtime + torch + catboost                         │
└───────────────────────────┬─────────────────────────────────┘
                            │ docker exec / uvicorn / cron
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  RUNTIME volumes (не в образе, но на диске хоста)            │
│  · ./local → /app/local (models, ledger, datasets)           │
│  · ./logs  → /app/logs                                       │
│  · ./config.env                                              │
└─────────────────────────────────────────────────────────────┘
```

| Слой | Docs нужны? | Tests? | Archive? | Главный рычаг |
|------|-------------|--------|----------|---------------|
| Git на хосте | да (dev) | да | да | sparse checkout (опционально) |
| Docker `/app` | **нет** (кроме 1 файла UI) | **нет** | **нет** | **`.dockerignore`** |
| `local/` volume | нет | нет | нет | retention policy |

**Вывод:** документация в git **остаётся** для Cursor/GitHub; **в образ и в hot path cron её не кладём**.

---

## 2. Текущая проблема (факты из репо)

| Факт | Риск |
|------|------|
| `Dockerfile`: `COPY . .` | В образ попадает `docs/` (~2.6M), `tests/` (~1M), `scripts/archive/`, audit txt |
| **Нет `.dockerignore`** | Любой мусор на хосте (напр. `scripts/bin/` 38M cloudflared) тоже в образ |
| `local/datasets/` untracked | Если появится на хосте до build — уедет в образ |
| `.git/` | При build context может раздувать context (BuildKit частично фильтрует, но не полагаемся) |
| Stub-скрипты в `scripts/*.py` → archive | Дубли + шум в `/app/scripts` |
| `tradenews/` gitignored | Соседний проект; если лежит в дереве — в образ |

**Единственная runtime-зависимость от docs (код):**

```python
# web_app.py — читает markdown для страницы earnings UI
docs/earnings-event-agent-lse/EARNINGS_UI_GUIDE.md
```

Остальные упоминания `docs/...` в Python — **строки в JSON/metadata**, не `open()`.

---

## 3. Принципы slim-deploy

1. **Allowlist в образе**, не denylist «на глаз»: явно перечислить что нужно runtime.
2. **Docs/tests/archives — dev-only**; в prod-контейнер не попадают.
3. **Один источник правды для deploy:** `git push` → `deploy_from_github.sh` → rebuild; slim — через `.dockerignore`, не через `scp`/`docker cp`.
4. **Volumes отдельно:** `local/` и `logs/` — политика retention на хосте, не в git.
5. **Проверка после каждой фазы:** smoke cron + earnings UI + один ML nightly dry-run.

---

## 4. Что исключать из Docker-образа

### 4.1 Обязательно исключить

| Путь | ~размер | Причина |
|------|---------|---------|
| `docs/**` | 2.6M | Не используется runtime (кроме одного MD — см. §4.3) |
| `docs/archive/` | 520K | Исторические планы |
| `tests/` | ~1M | pytest только CI/dev |
| `scripts/archive/` | 128K | Incident/legacy scripts |
| `.git/` | varies | Не нужен в `/app` |
| `.cursor/`, `.cursorrules` | small | IDE |
| `local/datasets/` | varies | Research CSV; volume mount |
| `scripts/bin/` | до 38M | cloudflared binary (gitignored) |
| `tradenews/` | varies | Отдельный проект |
| `*.md` в корне кроме runtime | small | README/VERSION — не для bot |
| `docs/reports/`, `docs/audit_*.txt` | ~540K | Артефакты аудита |
| `kerimsrv/`, `moex` | varies | Side projects |

### 4.2 Оставить в образе (runtime)

| Путь | Зачем |
|------|-------|
| `api/`, `services/`, `models/` (ORM) | bot + web + ML inference |
| `scripts/*.py` (не archive) | cron `docker exec lse-bot python scripts/...` |
| `scripts/cron_*.sh` | опционально (часть cron на **хосте** — см. §5) |
| `crontab/` | эталон для ops |
| `templates/`, `static/` | web |
| `requirements*.txt`, `config.env.example` | reference |
| `update_finviz_data.py` и пр. root `.py` | cron |
| **`docs/earnings-event-agent-lse/EARNINGS_UI_GUIDE.md`** | web_app read (временно) |

### 4.3 Рефактор (Фаза 2, опционально)

Перенести `EARNINGS_UI_GUIDE.md` → `templates/earnings_ui_guide.md` или `static/content/` — тогда **`docs/` целиком** в `.dockerignore` без исключений.

---

## 5. Что нужно на **хосте** VM (не в образе)

Минимальный набор для ops:

| На хосте `~/lse` | Нужен? |
|------------------|--------|
| `docker-compose.yml`, `Dockerfile`, `.dockerignore` | да |
| `scripts/deploy_from_github.sh` | да |
| `scripts/cron_game5m_*.sh`, `cron_weekly_*.sh` | да (cron вызывает с хоста) |
| `crontab/lse-docker.crontab` | да |
| `config.env`, `config.secrets.env` | да |
| `logs/`, `local/` | да (volumes) |
| `docs/` | **не обязателен** для торговли |
| `tests/` | **не обязателен** |
| `.git/` | да для `deploy_from_github.sh` |

**Опционально (Фаза 4):** `git sparse-checkout` на VM — только `scripts/deploy*`, `scripts/cron_*`, compose, Dockerfile, crontab; остальное только через образ. Сложнее отладка; делать после `.dockerignore`.

---

## 6. Фазы работ

### Фаза 0 — Инвентаризация на prod (1 день, без риска)

**Окно:** вне RTH или weekend.

```bash
# На VM
ssh ai8049520@104.154.205.58

# Размеры
du -sh ~/lse ~/lse/docs ~/lse/tests ~/lse/local ~/lse/logs ~/lse/scripts/bin 2>/dev/null
docker system df
docker exec lse-bot du -sh /app/docs /app/tests /app/scripts/archive 2>/dev/null

# Подтвердить единственный read docs
docker exec lse-bot grep -rn 'docs/' /app/web_app.py /app/api 2>/dev/null | head
```

**Артефакт:** `docs/runtime_slim_audit_YYYY-MM-DD.txt` (опционально, dev-only).

| # | Задача | Критерий |
|---|--------|----------|
| 0.1 | Замер disk до | baseline GB |
| 0.2 | Список файлов >10M в ~/lse и /app | таблица |
| 0.3 | Проверка earnings UI | страница открывается |

---

### Фаза 1 — `.dockerignore` + verify build (главный win, низкий риск)

| # | Задача | Файл |
|---|--------|------|
| 1.1 | Добавить `.dockerignore` | корень репо |
| 1.2 | Исключение `!docs/earnings-event-agent-lse/EARNINGS_UI_GUIDE.md` | см. шаблон §9 |
| 1.3 | `scripts/verify_docker_slim.sh` — после build проверяет отсутствие `/app/tests`, `/app/docs/archive` | CI/local |
| 1.4 | Deploy `--force`, smoke | §10 |

**Ожидаемый эффект:** −3–5M в слое COPY (больше если на хосте был `scripts/bin/`); быстрее build context; меньше RAM при распаковке слоя.

---

### Фаза 2 — Перенос единственного runtime-doc (низкий риск)

| # | Задача |
|---|--------|
| 2.1 | `EARNINGS_UI_GUIDE.md` → `templates/earnings_ui_guide.md` |
| 2.2 | Правка `web_app.py` path |
| 2.3 | `.dockerignore`: `docs/` без исключений |
| 2.4 | Smoke earnings UI |

---

### Фаза 3 — Зачистка хоста VM (ops, без изменения торговой логики)

| # | Что чистить | Действие | Осторожно |
|---|-------------|----------|-----------|
| 3.1 | `logs/*.log` | logrotate / truncate >90d | не трогать активные flock |
| 3.2 | `local/datasets/*.csv` | архив offsite / delete smoke | models *.cbm **не** удалять |
| 3.3 | `local/analyzer_snapshots/` | retention 30d | |
| 3.4 | Docker | `docker image prune -f`, `docker builder prune --filter until=720h` | не `--all` без бэкапа |
| 3.5 | `scripts/bin/cloudflared` | удалить если не используется | |
| 3.6 | Старые образы `lse-bot` | оставить latest + previous tag | |

**Не делать:** `git clean -fdx` на prod без бэкапа `config.env` и `local/`.

---

### Фаза 4 — Git hygiene (dev repo, опционально)

| # | Задача | Эффект |
|---|--------|--------|
| 4.1 | Удалить stub `scripts/*.py` с `ARCHIVED →` (оставить только `scripts/archive/`) | меньше шума |
| 4.2 | `docs/audit_*.txt` → `docs/reports/audit/` или не коммитить новые | git slimmer |
| 4.3 | Sparse checkout doc для VM | меньше disk на хосте |
| 4.4 | Orphan branch `archive/docs-2026` для старых `docs/archive/*` | радикально, не срочно |

---

### Фаза 5 — Guardrails (не regress)

| # | Задача |
|---|--------|
| 5.1 | Pre-commit или CI: `verify_docker_slim.sh` на PR |
| 5.2 | Строка в [DEPLOY.md](DEPLOY.md): «образ slim, docs не в /app» |
| 5.3 | Чеклист deploy: после build `docker exec lse-bot test ! -d /app/tests` |

---

## 7. Календарь

| Веха | Срок | Зависимость |
|------|------|-------------|
| R0 — этот план | 2026-06 | — |
| R1 — `.dockerignore` + deploy | **2026-06-28** (weekend) | вне RTH |
| R2 — перенос earnings MD | +1 нед | после R1 smoke |
| R3 — VM disk cleanup | +1 нед | snapshot/dump if needed |
| R4 — git/stub cleanup | параллельно dev | не блокирует prod |
| R5 — CI guard | после R1 | |

**Не совмещать** с promotion review ML **2026-07-14** в тот же вечер — только если R1 уже зелёный ≥3 дня.

---

## 8. Go / no-go после R1

| Проверка | Go |
|----------|-----|
| `docker exec lse-bot test ! -e /app/tests` | pass |
| `docker exec lse-bot test ! -d /app/docs/archive` | pass |
| `/earnings` UI guide renders | pass |
| `send_sndk_signal_cron.py` один dry run | no traceback |
| `run_ml_refresh_dispatcher.py --dry-run` или readiness cron | pass |
| Disk `/app` layer | меньше baseline ≥10% |

**Rollback:** redeploy previous image tag или revert `.dockerignore` + rebuild.

---

## 9. Шаблон `.dockerignore` (Фаза 1.1)

```dockerignore
# Git / IDE
.git
.gitignore
.cursor
.cursorrules
.vscode
.idea

# Dev-only
tests/
docs/
# Exception until Phase 2:
!docs/earnings-event-agent-lse/
!docs/earnings-event-agent-lse/EARNINGS_UI_GUIDE.md

# Archives & side projects
scripts/archive/
tradenews/
kerimsrv/
moex

# Local/runtime on host (mounted as volumes)
local/
logs/
config.env
config.secrets.env
config.security.env
*.sql.gz

# Binaries & caches
scripts/bin/
__pycache__/
*.py[cod]
.cache/
*.egg-info/
env/
venv/

# Docs artifacts (even if under docs/ before exclude)
docs/reports/
docs/audit_*.txt

# OS
.DS_Store
Thumbs.db
```

После **Фазы 2** удалить блок `!docs/...` и оставить `docs/`.

---

## 10. Smoke после deploy

```bash
# Host
cd ~/lse && ./scripts/deploy_from_github.sh --force

# Slim checks
docker exec lse-bot test ! -d /app/tests && echo OK_no_tests
docker exec lse-bot test ! -d /app/docs/archive && echo OK_no_doc_archive
docker exec lse-bot du -sh /app/docs 2>/dev/null || echo OK_no_docs_dir

# App
curl -sf http://127.0.0.1:8080/health || curl -sf http://127.0.0.1:8080/ | head
docker exec lse-bot python -c "import services.game_5m; print('import_ok')"

# Cron path (one job)
docker exec lse-bot python scripts/cron_watchdog.py --help >/dev/null && echo OK_cron_script
```

---

## 11. Чеклист (живой)

### Фаза 0
- [ ] 0.1 prod disk audit
- [ ] 0.2 `/app` size baseline

### Фаза 1
- [x] 1.1 `.dockerignore`
- [x] 1.3 `verify_docker_slim.sh`
- [ ] 1.4 deploy + smoke §10

### Фаза 2
- [x] 2.1–2.2 earnings MD → `templates/earnings_ui_guide.md`
- [ ] 2.4 earnings UI smoke after deploy

### Фаза 3
- [ ] 3.1–3.6 VM cleanup log

### Фаза 4–5
- [ ] stub scripts / CI guard

---

## 12. FAQ

**Docs на сервере в git clone мешают?**  
Мало (~2.6M). Критично — **не тащить их в образ** и не копировать в `/app` при каждом build. `.dockerignore` решает 80%.

**Удалять `docs/archive` из git?**  
Не обязательно для prod; для dev удобно. Orphan branch — опция Фазы 4.

**Тестовые скрипты в `scripts/` (не archive)?**  
Оставить в git; в образе допустимы **если** вызываются cron (большинство `train_*`, `build_*` — да, nightly ML). **Исключать** только `tests/` и `scripts/archive/`. Отдельный audit «скрипт не в crontab 6 месяцев» — Фаза 4.

**Нужен ли deploy docs на VM?**  
Нет для торговли. Нужен deploy **кода** через образ.

---

*Обновлять при закрытии фаз; baseline audit — приложить к первому PR с `.dockerignore`.*
