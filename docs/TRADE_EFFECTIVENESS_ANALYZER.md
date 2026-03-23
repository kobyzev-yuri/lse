# Анализатор эффективности сделок (краткая методика)

Цель: оценить, где стратегия теряет деньги и где недобирает прибыль, чтобы приоритизировать улучшения порогов и логики.

Подход внедрения: итеративный.
- Шаг 1: устраняем **грубые промахи** (крупные убытки, late polling, ранние выходы).
- Шаг 2: делаем **параметрическую тонкую настройку** (пороги RSI/vol/prob/news и подтверждения выхода).
- Шаг 3: повторяем цикл на новых данных (неделя за неделей) и закрепляем только стабильные улучшения.

### План этапов (зафиксировано)

**Этап 1 (внедрён):**
- Повысить порог выжидания по волатильности: `GAME_5M_VOLATILITY_WAIT_MIN` (по умолчанию `0.7`).
- Добавить подтверждение SELL по RSI: `GAME_5M_SELL_CONFIRM_BARS` (по умолчанию `2` бара).
- Цель: снизить грубые промахи по ранним/шумовым сигналам.

**Этап 2 (следующий):**
- 1m guard-проверка возле уровней выхода (уменьшить `late_polling_signals`).
- Частичный тейк + trailing для снижения `missed_upside`.

**Этап 3 (тонкая настройка):**
- Калибровка порогов `prob_up`, ATR/объёмных фильтров и news-гейтов.
- Проверка на недельных окнах с фиксацией результата до/после.

Единый источник расчёта:
- `services/trade_effectiveness_analyzer.py`
- используется одновременно:
  - в Web: `/analyzer`, `/api/analyzer`
  - в Telegram: `/analyser`
  - в CLI: `scripts/analyze_trade_effectiveness_weekly.py`

## 1) Какие сделки берём

- Источник: `public.trade_history` через `report_generator.load_trade_history()`.
- Закрытые сделки считаются через `compute_closed_trade_pnls()` (учёт комиссий и log-return).
- Фильтр периода: последние `N` дней (по умолчанию 7).
- Фильтр стратегии: `GAME_5M` (по умолчанию), либо `ALL`, либо другая стратегия.

## 2) Что считаем по каждой сделке

Для сделки с `entry_ts -> exit_ts`:
- `realized_pct` — фактический результат в % к cost-basis;
- `realized_log_return` — лог-доходность;
- на 5m-окне сделки (Yahoo 5m, ET):
  - `potential_best_pct` — максимум по High относительно входа;
  - `preventable_worst_pct` — минимум по Low относительно входа;
  - `missed_upside_pct = max(0, potential_best_pct - realized_pct)`;
  - `avoidable_loss_pct = max(0, realized_pct - preventable_worst_pct)`.

Дополнительно подмешиваются параметры входа из `context_json`:
- `rsi_5m`, `volatility_5m_pct`, `momentum_2h_pct`,
- `prob_up/prob_down`, `kb_news_impact`, `entry_advice`.
- Для новых сделок также сохраняются:
  - `decision_rule_version`
  - `decision_rule_params` (снимок порогов и конфиг-параметров, применённых при решении).

## 3) Агрегированные метрики

- `win_rate_pct`, `sum_net_pnl_usd`, `avg/median_realized_pct`, `avg_log_return`;
- суммарные и средние `missed_upside` / `avoidable_loss`;
- разрез `by_exit_signal` (например `TIME_EXIT`, `TAKE_PROFIT`, `SELL`);
- риск-флаги:
  - `late_polling_signals` (выход заметно ниже внутрисделочного High),
  - `high_vol_losses_count` (убытки при высокой `volatility_5m_pct`),
  - `weak_prob_up_losses_count`,
  - `negative_news_losses_count`.
- параметрические индикаторы:
  - `losses_with_allow_entry_count`,
  - `losses_with_high_prob_up_count`,
  - `losses_with_high_rsi_count`,
  - `decision_rule_versions`.

## 4) Что отдаёт интерфейс

- Короткая сводка;
- `top_losses`;
- `top_missed_upside`;
- `practical_parameter_suggestions` (грубые, практичные изменения порогов);
- `critical_case_analysis` (разбор критичных сделок с action item);
- опционально LLM-блок с приоритетами улучшений (`--llm` / `use_llm=1`).

## 5) Как интерпретировать

- Большой `sum_missed_upside_pct` + высокий `TIME_EXIT`:
  - проверить ранние выходы и частоту опроса цены.
- Рост `high_vol_losses_count`:
  - ужесточать вход при экстремальной 5m-волатильности.
- Много убытков при слабом `prob_up`:
  - повысить порог качества входа.
- Частые потери на негативном news-impact:
  - усиливать news-фильтр или паузу входа.

Практика применения (рекомендуемый цикл):
1. Сначала внедрять 1-2 изменения из `practical_parameter_suggestions` (без массовых правок).
2. Проверять эффект минимум на 1 неделе новых сделок.
3. Сравнивать `sum_net_pnl_usd`, `sum_missed_upside_pct`, `sum_avoidable_loss_pct`.
4. Только после подтверждения эффекта двигаться к следующей тонкой настройке.

## 6) Ограничения

- 5m Yahoo ограничен коротким окном истории (до ~7 дней).
- `missed_upside`/`avoidable_loss` — диагностические оценки, не «идеальный исполнимый» backtest.
- LLM-рекомендации вспомогательные; решения о порогах подтверждать статистикой.

## 7) Ответ LLM

- В промпте просим **только JSON** без markdown.
- На практике модель иногда оборачивает ответ в ` ```json ... ``` `; парсер в `_parse_llm_json_response()` снимает fence и извлекает объект `{...}`, чтобы в `llm.analysis` был структурированный JSON, а не одна строка `raw_text`.

## 8) Связь с кодом и параметрами сделки

- Источник правил входа/выхода для 5m: `services.recommend_5m.get_decision_5m`.
- В отчёт анализатора всегда добавляется `meta.current_decision_rule_params` (актуальные пороги из кода/config).
- Пороги RSI/импульса для `get_decision_5m` задаются в `config.env` (`GAME_5M_RSI_*`, `GAME_5M_RTH_MOMENTUM_BUY_MIN`, …) и функция `get_decision_5m_rule_thresholds()` — см. `config.env.example`.
- Интервал опроса крона игры 5m: `GAME_5M_SIGNAL_CRON_MINUTES` (должен совпадать с `*/N` в crontab для `send_sndk_signal_cron.py`, см. `setup_cron.sh`).
- Для новых сделок snapshot правил сохраняется в `context_json`, чтобы LLM и post-mortem анализ опирались на фактически применённые параметры, а не на предположения.
