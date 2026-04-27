# Архив документации

Здесь лежат **устаревшие или разовые** материалы: черновики планов, инциденты, дубли summary-файлов, заметки по миграциям. Они **не поддерживаются** как актуальный источник правды.

**Актуальная точка входа:** [README.md](../../README.md) → [docs/ARCHITECTURE.md](../ARCHITECTURE.md).

| Файл | Почему в архиве |
|------|-----------------|
| `RENAME_REPO.md` | Разовая инструкция по переименованию репозитория |
| `CONFIG_MIGRATION_SUMMARY.md` | Исторический снимок миграции конфига |
| `INSTALL_FEEDPARSER_FIX.md` | Точечный фикс зависимости |
| `ClusterPlan.md` | Черновик кластерного плана |
| `QUICK_FIX.md` | Разовые быстрые правки |
| `STRATEGY_FACTORY_SUMMARY.md` | Дубль; актуально: [PORTFOLIO_GAME.md](../PORTFOLIO_GAME.md), [STRATEGIES_LLM_AND_REPORTS.md](../STRATEGIES_LLM_AND_REPORTS.md) |
| `FINVIZ_INTEGRATION_SUMMARY.md` | Дубль; актуально: [docs/FINVIZ_INTEGRATION.md](../FINVIZ_INTEGRATION.md) |
| `LLM_INTEGRATION_SUMMARY.md` | Дубль; актуально: [STRATEGIES_LLM_AND_REPORTS.md](../STRATEGIES_LLM_AND_REPORTS.md), [LLM_MODEL_SELECTION.md](../LLM_MODEL_SELECTION.md) |
| `CONFIG_AND_SENTIMENT_SUMMARY.md` | Сводка заменена разделами в `CONFIG_SETUP.md` / `NEWS.md` |
| `SENTIMENT_IMPLEMENTATION.md` | Устаревшая реализация; актуально: [SENTIMENT_NEWS.md](../SENTIMENT_NEWS.md), [NEWS.md](../NEWS.md) |
| `REPORT_CLOSED_IMPULSE_AND_PRICE_FIX.md` | Исторический фикс отчёта |
| `LITE_INCIDENT_ANALYSIS.md` | Разбор инцидента по тикеру |
| `CRON_TIME_ANALYSIS.md` | Разовый анализ расписания cron |
| `SNIPPETS_PREMARKET_CHECK.md` | Черновые сниппеты |
| `RESUME_PREMARKET_AND_RECENT.md` | Резюме премаркета; актуально: [GAME_5M_PREMARKET_AND_IMPULSE.md](../GAME_5M_PREMARKET_AND_IMPULSE.md), [CRONS_AND_TAKE_STOP.md](../CRONS_AND_TAKE_STOP.md) |
| `TELEGRAM_SANDBOX_TRADING_PLAN.md` | Старый план песочницы |
| `PRACTICAL_SOURCES_2026.md` | Черновик источников |
| `REPORT_CLOSED_IMPULSE_AND_PRICE_FIX.md` | Фикс цен в отчёте (инцидент) |
| `GAME_5M_CALCULATIONS_AND_REPORTING_2026-04-26.md` | Архив прежнего краткого описания GAME_5M; актуально: [docs/GAME_5M_CALCULATIONS_AND_REPORTING.md](../GAME_5M_CALCULATIONS_AND_REPORTING.md) |
| `GAME_5M_DYNAMIC_TAKE_EXIT_SEMANTICS_2026-04-26.md` | Архив отдельного разбора динамического тейка; объединён в [docs/GAME_5M_CALCULATIONS_AND_REPORTING.md](../GAME_5M_CALCULATIONS_AND_REPORTING.md) |
| `BOSS_DASHBOARD_VECTOR_KB_INTEGRATION_2026-04-26.md` | Промежуточный отчёт по интеграции Vector KB в Boss Dashboard; актуально смотреть код `boss_dashboard.py` и [VECTOR_KB_USAGE.md](../VECTOR_KB_USAGE.md) |
| `CLOSED_REPORT_CALCULATION_2026-04-26.md` | Старый разбор колонок closed/closed_impulse; логика GAME_5M и причин выхода перенесена в [GAME_5M_CALCULATIONS_AND_REPORTING.md](../GAME_5M_CALCULATIONS_AND_REPORTING.md) |
| `GAME_5M_CLOSED_ANALYSIS_2026-04-26.md` | Разовый анализ закрытых GAME_5M; актуально: GAME_5M_CALCULATIONS_AND_REPORTING.md и TRADE_EFFECTIVENESS_ANALYZER.md |
| `GAME_SNDK_2026-04-26.md` | Старое SNDK-only описание GAME_5M; актуально: GAME_5M_CALCULATIONS_AND_REPORTING.md |
| `next_actions_2026-04-26.md` | Черновой чеклист от 2026-04-09; не является актуальной документацией |
| `GAME5M_TAKE_5M_VS_30M_EXECUTIVE_REPORT_2026-04-26.md` | Снимок отчёта за 2026-04-18; актуально: GAME5M_TAKE_5M_VS_30M_REPORT.md |
| `PORTFOLIO_CLUSTER_SNAPSHOT_2026-04-26.md` | Разовый снимок кластеров портфеля; пересчитывается скриптом cluster_portfolio_leaders.py |
| `TRADING_AGENT_READINESS_2026-04-26.md` | Старый readiness-анализ от 2026-02-20; актуально: ARCHITECTURE.md и PORTFOLIO_GAME.md |
| `VECTOR_KB_IMPLEMENTATION_2026-04-26.md` | Старый implementation-note; актуально: VECTOR_KB_USAGE.md, NEWS.md, KNOWLEDGE_BASE_FIELDS.md |
| `PREMARKET_PLAN_2026-04-26.md` | Промежуточный план премаркета; актуально: GAME_5M_PREMARKET_AND_IMPULSE.md |
| `GAME_5M_CURRENT_PLAN_CHECKLIST_2026-04-26.md` | Промежуточный статусный чеклист GAME_5M; актуально: GAME_5M_CALCULATIONS_AND_REPORTING.md и GAME_5M_BUY_DECISION_AND_LLM.md |
| `STRATEGY_FACTORY_2026-04-27.md` | Старое описание фабрики стратегий; актуально: PORTFOLIO_GAME.md и STRATEGIES_LLM_AND_REPORTS.md |
| `LLM_GUIDANCE_2026-04-27.md` | Старый контур `get_llm_guidance`; актуально: STRATEGIES_LLM_AND_REPORTS.md и LLM_MODEL_SELECTION.md |
| `SENTIMENT_ANALYSIS_2026-04-27.md` | Старый общий обзор sentiment; актуально: SENTIMENT_NEWS.md и NEWS.md |
| `LLM_TICKER_DATA_SOURCES_AND_IMPROVEMENTS_2026-04-27.md` | Исследовательская заметка по внешнему формату TickerData; не является текущим контрактом |
| `DEPLOY_INSTRUCTIONS_2026-04-27.md` | Старое смешанное описание VM/Cloud Run; актуально: DEPLOY.md, DEPLOY_GCP.md, MIGRATE_SERVER.md |
| `CRON_TICKERS_EXPLANATION_2026-04-27.md` | Старый sandbox-разбор тикеров cron; актуально: TICKER_GROUPS.md и RUN_GAME_SERVICES.md |
| `TELEGRAM_WEBHOOK_TEST_2026-04-27.md` | Разовая пошаговая проверка webhook; актуально: TELEGRAM_BOT_SETUP.md и DEPLOY_GCP.md |
| `GAME_5M_PREMARKET_MOMENTUM_2026-04-27.md` | Короткий дубль премаркет-импульса; объединено в GAME_5M_PREMARKET_AND_IMPULSE.md |
| `GAME_5M_ENTRY_STRATEGIES_2026-04-27.md` | Короткий дубль про technical/LLM вход; актуально: GAME_5M_BUY_DECISION_AND_LLM.md |
| `SANDBOX_TRADE_EXAMPLE_2026-04-27.md` | Старый демонстрационный сценарий сделок; актуально: DATABASE_SCHEMA.md и PORTFOLIO_GAME.md |
