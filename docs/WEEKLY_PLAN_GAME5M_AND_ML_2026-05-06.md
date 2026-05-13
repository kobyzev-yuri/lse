> **Актуальный срез планов:** [PROJECT_STATUS_AND_ROADMAP.md](PROJECT_STATUS_AND_ROADMAP.md) (обновляется чаще этого файла).  
> Ниже — **снимок недели 2026-05-06** (контекст решений той даты), не заменяет чеклисты в `GAME_5M_TIME_EXIT_RECOVERY_PLAN.md` и earnings-плане.

## Что уже реализовано (состояние на 2026-05-06)

### 5m: CatBoost entry (осторожное влияние)

- В `get_decision_5m()` добавляются поля:
  - `catboost_signal_status`, `catboost_entry_proba_good`
  - `technical_decision_core` (чистые правила) и `technical_decision_effective` (после CatBoost)
  - `catboost_fusion_mode`, `catboost_fusion_note`
- В прод-конфиге включён режим **осторожного понижения**:
  - `GAME_5M_CATBOOST_FUSION=hold_if_buy_below_p` и порог `GAME_5M_CATBOOST_HOLD_BELOW_P`
  - CatBoost может только **BUY→HOLD**, и **не влияет на выходы** (выходы должны опираться на core).
- Артефакты nightly ML для 5m пишутся в **persist**:
  - модель: `/app/logs/ml/models/game5m_entry_catboost.cbm` + `.meta.json`
  - датасеты: `/app/logs/ml/datasets/game5m_stuck_dataset.csv`, `game5m_continuation_dataset.csv` (+ `.meta.json`)
  - тренд метрик: `/app/logs/ml/logs/game5m_daily_ml_report.jsonl`
- Из обучения/датасетов исключены инцидентные закрытия с окном выхода **[09:25..09:30) ET** (по `exit_bar_start_et/exit_bar_end_et` в `exit_context_json`).

### 5m: борьба с висяками и упущенной прибылью (аналитический контур)

- Анализатор включает блоки:
  - `game5m_hanger_v2_review` (диагностика “висяков” по SELL context_json)
  - `continuation_gate_review` (недобор после тейка/ранние закрытия)
  - `game5m_hanger_tune_json_review` (эффективность hanger JSON, капы и смесь TAKE_PROFIT vs TAKE_PROFIT_SUSPEND)
  - `catboost_entry_backtest` (проверка P на закрытых сделках по saved context_json)
- В `ANALYZER_METRIC_DEFINITIONS` зафиксирован смысл CatBoost-полей и разделение `core` vs `effective`.

### Портфель: CatBoost expected return (advisory)

- Обучение портфельной модели (daily expected return) работает и пишет:
  - модель: `/app/logs/ml/models/portfolio_return_catboost.cbm` + `.meta.json`
  - тренд метрик: `/app/logs/ml/logs/portfolio_daily_ml_report.jsonl`
  - pipeline log: `/app/logs/portfolio_daily_ml_pipeline.log`
- В runtime включение advisory делается через:
  - `PORTFOLIO_CATBOOST_ENABLED=true`
  - `PORTFOLIO_CATBOOST_MODEL_PATH=/app/logs/ml/models/portfolio_return_catboost.cbm`
- В UI портфельных карточек ML‑поля отображаются при `portfolio_ml_status=ok`.

## План на ближайшую неделю (цель: меньше висяков и меньше недобора без “резких” правил)

### 1) CatBoost (5m) — измерить эффект “BUY→HOLD” фильтра

- Метрики:
  - доля BUY/STRONG_BUY, пониженных до HOLD (count + список кейсов)
  - сравнение win-rate/avg_realized_pct до/после по окнам 5–7 торговых дней
  - “ошибка упущенной сделки”: сколько раз effective=HOLD, но сделка дала бы TAKE (оценка через backtest на закрытых/симуляция на бумаге)
- Интеграция с анализатором:
  - добавить в отчёт агрегат: сколько сигналов было бы “понижено” при текущем пороге, и каков PnL таких сделок в истории (через `catboost_entry_backtest` + правило порога).

### 2) Висяки (hanger/stale) — ограничить ущерб и не срезать winners

- Early de-risk:
  - логировать “would_trigger” и собрать выборку 20+ закрытий, затем включать/калибровать `GAME_5M_EARLY_DERISK_*`.
- Hanger JSON:
  - держать JSON свежим (offline regen по bundle) и измерять эффект по `TAKE_PROFIT_SUSPEND` vs обычный TAKE.

### 3) Недобор (continuation/underprofit) — перейти от log-only к управляемому эксперименту

- Continuation gate:
  - копить статистику `would_extend_take=true` и фактический “недобор после закрытия”.
  - критерий перевода из log-only: медианная выгода и ограничение на просадку/время удержания.

### 4) Портфельная ML — advisory, но с прозрачными метриками

- Ввести регулярный cron (после закрытия US) для `train_portfolio_catboost.py` с логом в `/app/logs/portfolio_daily_ml_pipeline.log`.
- В вебе фиксировать:
  - текущую версию модели (trained_at, horizon, corr_window)
  - последние метрики из JSONL (RMSE/MAE/top-decile hit)
- Дальше — только после стабильных метрик на walk-forward: обсуждать “мягкие” правила (например подсветка/ранжирование кандидатов), но не автоторговлю.

