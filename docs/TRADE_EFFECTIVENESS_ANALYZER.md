# Анализатор эффективности сделок (краткая методика)

Цель: оценить, где стратегия теряет деньги и где недобирает прибыль, чтобы приоритизировать улучшения порогов и логики.

Подход внедрения: итеративный.
- Шаг 1: устраняем **грубые промахи** (крупные убытки, выход заметно ниже MFE 5m-окна, ранние выходы).
- Шаг 2: делаем **параметрическую тонкую настройку** (пороги RSI/vol/prob/news и подтверждения выхода).
- Шаг 3: повторяем цикл на новых данных (неделя за неделей) и закрепляем только стабильные улучшения.

### План этапов (зафиксировано)

**Этап 1 (внедрён):**
- Повысить порог выжидания по волатильности: `GAME_5M_VOLATILITY_WAIT_MIN` (по умолчанию `0.7`).
- Добавить подтверждение SELL по RSI: `GAME_5M_SELL_CONFIRM_BARS` (по умолчанию `2` бара).
- Цель: снизить грубые промахи по ранним/шумовым сигналам.

**Этап 2 (следующий):**
- Уточнение выхода у тейка (1m guard / trailing) — уменьшить **недобор** после фиксации; счётчик `late_polling_signals` в коде — это **не** cron, а «выход далеко от max High окна» (см. `metric_definitions` в JSON отчёта).
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
  - узкий режим (3–4 дня, фильтр по тикерам/`trade_id`): `scripts/analyze_trades_focused.py`, Web `GET /api/analyzer/focused`

### CatBoost в отчёте (`catboost_entry_backtest`)

Первый шаг **проверки прогноза благоприятности входа** (до LLM и до ручной настройки порогов):

- Для стратегий **`GAME_5M`** и **`ALL`** (в режиме ALL только сделки с `entry_strategy=GAME_5M`) по каждой закрытой сделке вызывается `predict_entry_favorability_from_saved_context` — та же модель, что в проде на входе, но признаки **только из `context_json` на BUY** (корреляция не дозаполняется из «текущей» матрицы).
- В JSON: сводная **калибровка** (средний P при win/loss, квантили P vs win rate), массив **`per_trade`** (статус CatBoost, P, `realized_pct`, при наличии — `estimated_upside_pct_day_at_entry`, `prob_up_at_entry`, укороченный `price_forecast_5m_summary_excerpt`), опционально **`price_context_at_entry`** — корреляция сохранённого upside на входе с фактическим результатом (справочно).
- Для **`PORTFOLIO`** блок осознанно **пропускается** (модель не про дневной портфель); отдельная модель — по плану в `docs/ML_GAME5M_CATBOOST.md` §9.

### Hanger v2 и continuation gate

Новые SELL `context_json` могут содержать:

- `position_state_v2` — диагностика Hanger v2 (`normal_hold`, `recoverable_hanger`, `stale_reversal`) на момент выхода;
- `continuation_gate` — log-only решение, закрывать тейк сейчас или рассматривать extended take.

Analyzer отдаёт два блока:

- `game5m_hanger_v2_review`: state counts, stale/reversal exits, recoverable hanger cases, top cases и кандидаты параметров `GAME_5M_STALE_REVERSAL_*` / `GAME_5M_HANGER_V2_*`;
- `continuation_gate_review`: сколько сделок gate хотел бы продлить, какой был `missed_upside`, и какие `GAME_5M_CONTINUATION_*` / take-параметры стоит проверять.

Live-лог во время сессии:

```bash
python scripts/analyze_hanger_tactic_log.py logs/cron_sndk_signal.log --tail-lines 5000
```

Правило настройки: не менять много ручек сразу. Сначала смотрим live-log в сессии, после закрытий — `/analyzer?strategy=GAME_5M&days=1`, затем выбираем один небольшой набор параметров и сравниваем следующий snapshot.

## Узкий анализ (выбранные сделки / короткое окно)

Функция `analyze_trade_effectiveness_focused(days, strategy, tickers=..., trade_ids=..., use_llm=...)`:

- Сначала загружаются закрытые сделки за `days` (как в глобальном отчёте), затем применяется фильтр:
  - `tickers` — только эти тикеры (регистр не важен);
  - `trade_ids` — только строки с указанным `trade_id` закрывающей сделки (как в `TradePnL`);
  - если оба списка пустые, анализируется всё окно (как мини-версия недельного отчёта).
- В JSON добавляется `game_5m_config_hints`: эвристики «какие ключи `GAME_5M_*` разумно пересмотреть» по паттернам выходов и метрикам выборки.
- При `use_llm=True` LLM получает фокус-инструкцию и должен вернуть `config_env_proposals` с полными именами ключей `config.env` (в т.ч. пер-тикерные `GAME_5M_TAKE_PROFIT_PCT_<TICKER>`).
- `format_trade_effectiveness_text` показывает заголовок «Узкий анализ», строку фильтра и блок эвристик, если они есть.

CLI:

```bash
python3 scripts/analyze_trades_focused.py --days 4 --tickers SNDK --llm
python3 scripts/analyze_trades_focused.py --days 3 --trade-ids 12041,12055 --json-out local/focused.json
```

Web: `GET /api/analyzer/focused?days=4&tickers=SNDK,AAPL&use_llm=0` (списки через запятую).

### JSON для локального углублённого анализа (KB, Ollama, Qwen и т.д.)

- Сохраните отчёт: `scripts/analyze_trades_focused.py ... --json-out report.json` или `GET /api/analyzer/focused?...` / `GET /api/analyzer?...`.
- По умолчанию в JSON есть сводка и `top_cases` (ограниченные топы). Чтобы получить **все сделки окна/фильтра** с полями цен, PnL, `missed_upside`, снимком `decision_rule_params` и т.д., добавьте **`include_trade_details=1`** (Web) или флаг **`--include-trade-details`** в CLI — в корне появится массив **`trade_effects`**.
- Дальше локальный пайплайн на вашей стороне: прочитать `report.json`, отфильтровать по тикеру (`jq '.trade_effects[] | select(.ticker=="SNDK")'`), к промпту приложить выдержки из `docs/GAME_5M_CALCULATIONS_AND_REPORTING.md` или фрагменты `services/recommend_5m.py` / `services/game_5m.py`, вызвать локальную модель. Серверный LLM анализатора при этом не обязателен (`use_llm=0`).

### Регулярные снимки на сервере (cron) и шаг настройки

- **`scripts/snapshot_analyzer_report.py`** — тот же JSON, что **`GET /api/analyzer`**. Два способа:
  - **Локально** — импорт анализатора (нужен venv с `pip install -r requirements.txt`, доступ к БД с хоста).
  - **HTTP** — только stdlib: задайте **`ANALYZER_SNAPSHOT_URL=http://127.0.0.1:ПОРТ/api/analyzer`** (или флаг **`--url ...`**) — удобно, если cron на хосте без numpy, а uvicorn в Docker/на том же сервере уже слушает порт.
  - Если **`ANALYZER_SNAPSHOT_URL` задан в окружении**, скрипт **по умолчанию всегда идёт по HTTP**, даже без `--url` (ответ = код в **контейнере**, не обязательно совпадает с `git pull` на хосте). Чтобы после `pull` снять отчёт **с диска**: **`--local`** или **`env -u ANALYZER_SNAPSHOT_URL python3 scripts/snapshot_analyzer_report.py ...`**
  Пишет `analyzer_{STRATEGY}_{N}d_{UTC}.json` и обновляет **`latest.json`** в каталоге.
  - Каталог: **`ANALYZER_SNAPSHOT_DIR`** или по умолчанию **`local/analyzer_snapshots/`** в корне репозитория (рядом с `~/lse`, в `.gitignore`, не уходит в git).
  - По умолчанию в снимок входит **`trade_effects`**; для лёгких файлов: `--no-trade-details`. LLM по умолчанию выключен; для cron обычно **не** передавать `--llm`.
  - Пример из каталога игры: `cd ~/lse && python3 scripts/snapshot_analyzer_report.py --days 7`  
  - Другой путь явно: `ANALYZER_SNAPSHOT_DIR=$HOME/lse/local/analyzer_snapshots python3 scripts/snapshot_analyzer_report.py --days 7`
- **Вариант «строго по HTTP»** (если скрипт без БД): `curl -sS "https://ХОСТ/api/analyzer?days=7&strategy=GAME_5M&include_trade_details=1" -o "$DIR/snap.json"` — эквивалент по содержимому, если веб смотрит в ту же БД.
- **`scripts/analyzer_tune_apply.py`** — «обработчик» одного шага: читает сохранённый отчёт с заполненным `auto_config_override.updates` и пишет в **`config.env`** только ключи из **белого списка** редактируемых (`is_editable_config_env_key`). Пишет **`tune_state.json`** рядом (или в `ANALYZER_SNAPSHOT_DIR`): что применено и когда. **Не блокирует** процесс на дни: «ждать результат» = через неделю новый снимок + сравнение.
  - Один ключ: `--from-json report.json --index 0`  
  - Все предложения из списка: `--from-json report.json` (осторожно).
  - Пробный прогон: `--dry-run`
  - Пропуск ключей (например крон уже поминутный): **`export ANALYZER_TUNE_SKIP_KEYS=GAME_5M_SIGNAL_CRON_MINUTES`**
- **`scripts/diff_analyzer_snapshots.py before.json after.json`** — краткое сравнение полей `summary` между двумя снимками.
- **`scripts/analyzer_hypothesis_candidates.py`** — для первых этапов тюнинга: печатает **нумерованный список** кандидатов из снимка (`auto_config_override.updates` с индексом для `analyzer_tune_apply`, плюс `practical_parameter_suggestions` / `game_5m_config_hints` / `critical_case_analysis` как текст без авто-применения). По умолчанию читает **`local/analyzer_snapshots/latest.json`**; машиночитаемый вывод: **`--json`**. Скрипт **не** меняет `config.env` — только помогает выбрать один шаг, применить, дождаться новых сделок и снять следующий снимок.

Рекомендуемый цикл: снимок → **`analyzer_hypothesis_candidates.py`** (или `--json` в свой обработчик) → выбор **одного** изменения → `analyzer_tune_apply.py --index N` → перезапуск сервиса по вашему `RESTART_CMD` → через N дней новый снимок → `diff_analyzer_snapshots.py`.

## Автотюнинг (этап v0: эволюционный)

Цель v0: сделать тот же цикл **автоматическим**, но безопасным: **не более одного** изменения за итерацию, с фиксацией «baseline» и ожиданием накопления новых сделок до следующего шага.

Скрипт: **`scripts/analyzer_autotune.py`**

- Источник отчёта:
  - HTTP: `--url http://127.0.0.1:8080/api/analyzer` (рекомендуется для cron на хосте без numpy/pandas).
  - Или `latest.json` из `ANALYZER_SNAPSHOT_DIR` / `local/analyzer_snapshots` (если `--url` не задан).
- Выбор изменения: берёт `auto_config_override.updates`, фильтрует через `is_editable_config_env_key`, применяет гардрейлы по “шагу” изменения и выбирает один кандидат по предпочтениям (`ANALYZER_AUTOTUNE_PREFER_PREFIXES`).
- Память: пишет состояние в `local/autotune_state.json` (или `ANALYZER_AUTOTUNE_STATE_PATH`), и пока pending не “созреет” по числу новых сделок — **не применяет** новые изменения.

Настройки (env):
- `ANALYZER_AUTOTUNE_APPLY=0|1` — по умолчанию **0** (только выбрать и записать pending).
- `ANALYZER_AUTOTUNE_MIN_TRADES=8` — сколько новых сделок нужно после применения, чтобы пометить pending как `ready_for_review`.
- `ANALYZER_AUTOTUNE_DENY_KEYS=...` — ключи, которые автотюнинг не трогает (по умолчанию `GAME_5M_SIGNAL_CRON_MINUTES`).
- `ANALYZER_AUTOTUNE_PREFER_PREFIXES=...` — приоритеты выбора (по умолчанию пер-тикерные тейк-капы, потом factor).

Пример (dry-run, не применяет):

```bash
cd ~/lse
python3 scripts/analyzer_autotune.py --days 5 --url http://127.0.0.1:8080/api/analyzer --dry-run
```

Пример (разрешить применение одного изменения):

```bash
cd ~/lse
ANALYZER_AUTOTUNE_APPLY=1 python3 scripts/analyzer_autotune.py --days 5 --url http://127.0.0.1:8080/api/analyzer
```

Рекомендуемый режим запуска: **вне первых минут RTH**, 1–2 раза в день. После применения нужно сделать **рестарт** сервиса (через ваш `RESTART_CMD` или вручную) — автотюнер v0 рестарт не делает.

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
  - `avoidable_loss_pct = max(0, realized_pct - preventable_worst_pct)` — на прибыльных long величина часто **высокая** (результат сильно лучше минимума окна); сумма по сделкам **не** интерпретируется как «сумма убытков».

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
  - `late_polling_signals` (устаревшее имя; то же, что `exit_below_window_mfe_count`: выход заметно ниже max High 5m-окна при недоборе; **не** метрика интервала cron),
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
- `top_missed_upside` (любые сделки с большим недобором);
- `top_profitable_missed_upside` — **только выигрышные** сделки с наибольшим `missed_upside` (ранний выход в плюсе при запасе до high окна);
- в `summary`: `sum_missed_upside_pct_on_wins`, `avg_missed_upside_pct_on_wins`, `wins_with_missed_upside_ge_1pct_count`;
- `practical_parameter_suggestions` (грубые, практичные изменения порогов);
- `critical_case_analysis` (разбор критичных сделок с action item);
- `game5m_hanger_v2_review` и `continuation_gate_review` (новые блоки для Iteration 2);
- опционально LLM-блок с приоритетами улучшений (`--llm` / `use_llm=1`).

### 4.1 Карта отчёта и варианты решений

| Блок JSON | Что отвечает | Типовые шаги (не все сразу) |
|-----------|----------------|----------------------------|
| `summary` + `top_cases` | Общий PnL, win rate, хвосты убытков и недобора | Зафиксировать период `days`; при высоком win rate смотреть `top_losses` как «слабейшие плюсы», не только убытки. |
| `practical_parameter_suggestions` | Грубые правки `GAME_5M_*` под наблюдаемые паттерны | Один ключ за итерацию → `analyzer_tune_apply` / tuning API → observe → `docs/GAME_5M_TUNING_REGLEMENT.md`. |
| `game_5m_config_hints` | Эвристики по env без обязательной связи с ML | Ручной разбор; сверка с `meta.current_decision_rule_params`. |
| `critical_case_analysis` | Несколько худших кейсов с текстовым action item | Приоритет устранения «грубых промахов» перед тонкой калибровкой. |
| `entry_underperformance_review` | Входы с плохим исходом или долгим удержанием | Ужесточение веток STRONG_BUY/BUY, `ENTRY_QUALITY_*`, лимиты дней/минут. |
| `catboost_entry_backtest` | Согласованность скора входа с фактом (`realized_pct`) | Убедиться: `GAME_5M_CATBOOST_ENABLED`, путь к `.cbm` на хосте веба, достаточно строк без `disabled`; после nightly ML сравнить калибровку P(win) vs loss. См. `docs/ML_GAME5M_CATBOOST.md`. |
| `game5m_hanger_v2_review` | `position_state_v2` vs PnL (stale / recoverable / normal) | Подкрутка `GAME_5M_STALE_REVERSAL_*`, `GAME_5M_HANGER_V2_*`, `GAME_5M_MAX_POSITION_*`, `EARLY_DERISK_*`; план фаз — `docs/GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md`. |
| `continuation_gate_review` | Log-only gate vs фактический `missed_upside` после тейка | Сначала накопить статистику при `GAME_5M_CONTINUATION_GATE_LOG_ONLY=true`; затем точечно ослаблять тейк только при `extend_take_candidate` (риск большей просадки). |
| `auto_config_override` | Готовые пары ключ→значение из эвристик анализатора | Применять только разрешённые ключи; согласовать с `analyzer_tune_apply` / веб-кнопкой «Применить». |
| `llm` (`use_llm=1`) | Структурированный JSON приоритетов и `in_algorithm_parameter_changes` | Валидация на данных отчёта; лимит ответа: `ANALYZER_LLM_MAX_COMPLETION_TOKENS` (читается из merged `config.env` через `get_config_value`). Не подменяет бэктест. |

**Связь с nightly ML:** JSONL `local/logs/game5m_daily_ml_report.jsonl` (в Docker часто `/app/local/logs/...`) описывает **объём датасетов и AUC обучения**; анализатор на закрытых сделках проверяет **эксплуатацию** модели и правил. Оба слоя нужны перед агрессивной сменой прод-конфига.

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

## 6) Ограничения и компромиссы параметров

- Любое «подтянуть потолок тейка / ослабить выход» **одновременно** увеличивает риск отката на других тикерах и в других режимах рынка; в `docs/GAME_5M_CALCULATIONS_AND_REPORTING.md` зафиксирован смысл **потолка сверху** vs искусственного «пола» снизу. Смотреть согласованно **убытки**, **выигрыши с missed** и **by_exit_signal**.
- **Kerim Platform** в репозитории — внешний HTTP `POST /game` для отчётов по спискам позиций (`services/platform_game_api.py`), а не встроенный в LSE горизонтный прогноз доходности/дропа. Чтобы калибровать глобальные константы по **вероятности ап/дроп на горизонте**, нужен отдельный контур: либо API/модель с явными `P(up_h)`, `P(drop_h)` и доверительными интервалами, либо офлайн-грид по истории сделок с учётом издержек и log-returns. Тогда пороги (`GAME_5M_*`, тейк/стоп/время) можно подстраивать под сегменты «высокий ап / высокий риск дропа» — это пока **не** часть анализатора, только идея интеграции.
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
