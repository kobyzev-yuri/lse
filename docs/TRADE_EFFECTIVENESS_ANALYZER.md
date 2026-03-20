# Анализатор эффективности сделок (краткая методика)

Цель: оценить, где стратегия теряет деньги и где недобирает прибыль, чтобы приоритизировать улучшения порогов и логики.

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

## 3) Агрегированные метрики

- `win_rate_pct`, `sum_net_pnl_usd`, `avg/median_realized_pct`, `avg_log_return`;
- суммарные и средние `missed_upside` / `avoidable_loss`;
- разрез `by_exit_signal` (например `TIME_EXIT`, `TAKE_PROFIT`, `SELL`);
- риск-флаги:
  - `late_polling_signals` (выход заметно ниже внутрисделочного High),
  - `high_vol_losses_count` (убытки при высокой `volatility_5m_pct`),
  - `weak_prob_up_losses_count`,
  - `negative_news_losses_count`.

## 4) Что отдаёт интерфейс

- Короткая сводка;
- `top_losses`;
- `top_missed_upside`;
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

## 6) Ограничения

- 5m Yahoo ограничен коротким окном истории (до ~7 дней).
- `missed_upside`/`avoidable_loss` — диагностические оценки, не «идеальный исполнимый» backtest.
- LLM-рекомендации вспомогательные; решения о порогах подтверждать статистикой.

## 7) Ответ LLM

- В промпте просим **только JSON** без markdown.
- На практике модель иногда оборачивает ответ в ` ```json ... ``` `; парсер в `_parse_llm_json_response()` снимает fence и извлекает объект `{...}`, чтобы в `llm.analysis` был структурированный JSON, а не одна строка `raw_text`.
