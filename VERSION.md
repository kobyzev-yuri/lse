# LSE Trading System — версия проекта

| Поле | Значение |
|------|----------|
| **Версия** | **2.0.0** |
| **Кодовое имя** | ML Consolidation |
| **Дата релиза документации** | 2026-06-09 |
| **Ветка** | `main` (единственная рабочая; feature-веток на remote нет) |
| **Prod runtime** | GCP VM `104.197.166.185`, Docker `lse-bot` + `lse-postgres` |

---

## Что означает 2.0.0

Переход от «набора планов и экспериментов» к **единому канону ML + торговых решений** с dual-track runtime (legacy исполняет, decision stack пишет snapshot).

| Блок | v1.x (до 2026-06) | v2.0 (сейчас) |
|------|-------------------|---------------|
| ML-документация | 20+ планов с пересечениями | Канон [ML_AND_DECISION_ARCHITECTURE.md](docs/ML_AND_DECISION_ARCHITECTURE.md) + deep-dive |
| L1 retrain | Дубли cron / watermark | Dispatcher `23:47`, **8/8** контуров |
| L3 promotion | Ad-hoc флаги | Реестр `ml_product_runtime`, фаза 3 по контурам |
| Gap predict/fact | Display-баги после open | Frozen snapshot + naive PM baseline на карточках (`813d77c`, `ff0650f`) |
| Multiday LR | Gates в плане | Entry `apply` на legacy; OOS WF **ready** (2026-06-09) |
| Устаревшие планы | В `docs/` | Stub → [docs/archive/](docs/archive/) |

---

## Статус бизнес-процессов (сводка)

Полная таблица с Mermaid-диаграммами: [BUSINESS_PROCESSES.md](BUSINESS_PROCESSES.md) §0.

| Процесс | Статус | Примечание |
|---------|--------|------------|
| Инициализация БД / котировки | **prod** | `quotes`, pgvector, yfinance seed |
| Премаркет + intraday 5m | **prod** | `premarket_daily_features`, Yahoo 1m |
| Новости → KB | **prod** | RSS/LLM, embedding |
| GAME_5M вход / выход | **prod** | Legacy hot path; `technical_decision_effective` |
| Портфельная игра | **prod** | CatBoost L3 promoted |
| Telegram + веб-карточки | **prod** | ML gap rows, multiday 1d/2d/3d |
| ML L1 nightly retrain | **prod** | Dispatcher, все 8 контуров |
| ML L2 readiness | **prod** | `ml_train_readiness`, data-quality API |
| Decision stack (L3 shadow) | **monitoring** | `RESOLVE=false`; weekly mirror |
| Gap ML vs naive PM | **caution** | Pooled ridge обучен; beat baseline — нет на 90d OOS |
| Multiday LR gates | **prod entry** | Hold `log_only`; WF v3nm **ready** |
| Earnings / event-reaction | **advisory** | UI `/earnings`, shadow grid |
| Open-path MVP | **shadow** | Prerequisites не закрыты |

---

## Ключевые коммиты релиза 2.0.0

| Коммит | Содержание |
|--------|------------|
| `6607dad` | L1 consolidation, dispatcher nightly |
| `9373240` | Фаза 2 registry 8/8 |
| `36253c7` | Фаза 3 sprint 3.1, mirror cron |
| `ff0650f` | Gap pred/fact fix, pooled ridge train hook |
| `813d77c` | Карточки: «Прогноз ML» + «Премаркет» |

---

## Куда смотреть дальше

| Задача | Документ |
|--------|----------|
| **Ближайший план консолидации** | [docs/CONSOLIDATION_NEXT_PLAN.md](docs/CONSOLIDATION_NEXT_PLAN.md) |
| Ops-статус (живой) | [docs/PROJECT_STATUS_AND_ROADMAP.md](docs/PROJECT_STATUS_AND_ROADMAP.md) |
| ML dual-track матрица | [docs/ML_STATUS_REPORT.md](docs/ML_STATUS_REPORT.md) |
| Фазы 0–4 consolidation | [docs/ML_CONSOLIDATION_ROLLOUT_PLAN.md](docs/ML_CONSOLIDATION_ROLLOUT_PLAN.md) |
| Навигация по всем docs | [docs/README.md](docs/README.md) |

---

## История версий (кратко)

| Версия | Дата | Смысл |
|--------|------|-------|
| 1.1.0 | 2026-02 | Strategy Manager, sentiment, бэктест UI |
| 1.4.0 | 2026-03 | Иерархия README → ARCHITECTURE, первый archive |
| **2.0.0** | **2026-06** | ML consolidation, dual-track, gap/multiday hardening |

Устаревший roadmap v1.1: [docs/archive/ROADMAP_v1.1.0_2026-02-14.md](docs/archive/ROADMAP_v1.1.0_2026-02-14.md).
