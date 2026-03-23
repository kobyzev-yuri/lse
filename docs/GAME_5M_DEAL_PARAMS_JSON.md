# Параметры сделки 5m: что сохраняем в JSON (context_json)

Два формата: **полный дамп** для новых сделок (уровень prompt_entry game_5m), **упрощённый** для старых (что удалось восстановить/бэкфилл). В будущий контекст подмешивать можно оба — через нормализатор.

---

## 1. Два формата и нормализатор

| Формат | Когда | Маркер | Использование |
|--------|--------|--------|----------------|
| **Полный** | Новые BUY (с текущего кода) | `deal_params_version: 1` | Полный снимок решения: decision, reasoning, price, стоп/тейк, entry_advice, session_phase, kb_news_impact и др. |
| **Упрощённый** | Старые записи, бэкфилл импульса | нет version | Только momentum_2h_pct, rsi_5m, volatility_5m_pct, session_high, period_str (и что удалось восстановить) |

**Единая схема для технического и LLM-входа:** оба типа входа сохраняют один и тот же набор полей в `context_json` (через `build_full_entry_context(d5, correlation_entry_features=...)`). Отличие только в том, что при LLM-входе: `entry_strategy="llm"`, в решении используются `decision`/`reasoning` от LLM, и при наличии добавляются поля `llm_key_factors` и т.п. Так можно сравнивать и фильтровать сделки по стратегии без разбора двух разных форматов.

**Корреляция (как у LLM):** крон после `get_cluster_decisions_5m` передаёт матрицу в `extract_correlation_features_for_5m_entry` → в JSON попадают числовые агрегаты `cb_corr_*` (см. §2). Тот же расчёт при необходимости дозаполняется в `get_decision_5m` для CatBoost (`ensure_correlation_features_for_catboost`).

**Подмешивание в контекст:** всегда через `services.deal_params_5m`:

- `normalize_entry_context(ctx)` — принимает `context_json` (str или dict), возвращает единый dict; у обоих форматов после нормализации есть `entry_impulse_pct` (из `momentum_2h_pct`, если нет явного).
- `get_entry_impulse_pct(ctx)` — возвращает `float | None` импульса при входе из любого формата.

Отчёты (/closed_impulse, PnL) используют эти функции, чтобы одинаково обрабатывать старые и новые записи.

---

## 2. Что сохраняем (текущее состояние)

### При BUY (record_entry → context_json)

**Новые сделки (полный дамп):** `services.deal_params_5m.build_full_entry_context(d5, correlation_entry_features=...)` пишет в context_json снимок уровня prompt_entry:

- `deal_params_version: 1`, `entry_impulse_pct` (= momentum_2h_pct), `decision`, `reasoning` (до 500 символов), `price`
- `momentum_2h_pct`, `rsi_5m`, `volatility_5m_pct`, `session_high`, `period_str`
- `stop_loss_enabled`, `stop_loss_pct`, `take_profit_pct`, `entry_advice`, `entry_advice_reason`
- `high_5d`, `low_5d`, `pullback_from_high_pct`, `last_bar_high/low`, `recent_bars_high_max/low_min`
- `bars_count`, `kb_news_impact`, `session_phase` (из `market_session.session_phase`), `estimated_upside_pct_day`, `suggested_take_profit_price`
- при наличии: `premarket_gap_pct`, `minutes_until_open`, `prev_close`, `llm_insight`, `llm_sentiment`
- **корреляция для ML/CatBoost** (если крон передал кластерную матрицу — тот же универс, что для LLM, 30 дн. log-returns):
  - `cb_corr_mean_game_peers`, `cb_corr_max_game_peer`, `cb_corr_min_game_peer`, `cb_corr_std_game_peers`, `cb_corr_n_game_peers` — по тикерам игры 5m (кроме себя);
  - `cb_corr_mean_universe`, `cb_corr_n_universe` — по всем символам матрицы (игра + портфель + `GAME_5M_CORRELATION_CONTEXT`).

**Старые / бэкфилл (упрощённый):** только часть полей (например momentum_2h_pct, rsi_5m, volatility_5m_pct, session_high, period_str). Нормализатор подставляет `entry_impulse_pct` из `momentum_2h_pct`.

### При SELL (close_position → context_json)

| Поле | Описание |
|------|----------|
| `momentum_2h_pct` | На момент закрытия |
| `rsi_5m` | На момент закрытия |
| `bar_high`, `bar_low` | High/Low последних баров (для тейка/стопа) |
| `exit_bar_close` | Close бара на момент выхода |
| `volatility_5m_pct` | На момент закрытия |
| `period_str` | Период данных |
| `session_high` | Хай сессии на момент закрытия |

---

## 2. Что показывает prompt_entry game_5m (на входе)

По каждому тикеру в отчёте выводятся (из того же d5 = get_decision_5m):

- **Решение:** decision, reasoning  
- **Мета:** price, RSI(5m), импульс 2ч, волатильность, стоп %, тейк %, период данных  
- **Корреляция:** с другими тикерами (матрица), цена/RSI по ним  
- **Контекст:** период данных, влияние новостей (KB), премаркет (если есть), LLM (если USE_LLM)  
- **Совет по входу:** entry_advice (ALLOW/CAUTION/AVOID), entry_advice_reason  

То есть «параметры сделки» в смысле prompt_entry — это по сути **весь контекст решения** на момент входа (и при закрытии — на момент выхода).

---

## 3. Предлагаемый полный набор для context_json (для обсуждения)

Чтобы по записи в БД можно было восстановить контекст, как в prompt_entry, и анализировать сделки, можно договориться сохранять следующее.

### 3.1 При BUY (единый «снимок решения»)

**Уже сохраняем:**  
momentum_2h_pct, rsi_5m, volatility_5m_pct, session_high, period_str  

**Имеет смысл добавить (из d5):**

| Поле | Зачем |
|------|--------|
| `decision` | BUY/STRONG_BUY — как именно вошли |
| `reasoning` | Краткое обоснование (можно обрезать до 200–500 символов) |
| `price` | Дублирует колонку price, но сохраняет контекст «цена на момент решения» |
| `stop_loss_pct`, `take_profit_pct` | Фактические стоп/тейк на момент входа (уже считаются от импульса) |
| `entry_advice`, `entry_advice_reason` | ALLOW/CAUTION/AVOID и причина — как в prompt_entry |
| `high_5d`, `low_5d` | Хай/лоу за 5 дней (опционально) |
| `pullback_from_high_pct` | Откат от хая сессии |
| `recent_bars_high_max`, `recent_bars_low_min` | Для анализа «почему тейк/стоп на этом уровне» |
| `session_phase` | REGULAR / NEAR_CLOSE / PRE_MARKET и т.д. (из market_session) |
| `kb_news_impact` | Влияние новостей (KB) — одна строка |
| `bars_count` | Число баров в окне |

**Опционально (если не раздувать JSON):**  
premarket_gap_pct, minutes_until_open (если PRE_MARKET), llm_insight / llm_sentiment (если вызывали LLM).  

**Не сохранять в JSON (или только ссылкой):**  
полные kb_news, llm_news_content (длинные тексты), correlation_matrix (объёмно).

### 3.2 При SELL

**Уже сохраняем:**  
momentum_2h_pct, rsi_5m, bar_high, bar_low, exit_bar_close, volatility_5m_pct, period_str, session_high  

**Можно добавить:**  
decision на момент закрытия (HOLD/SELL), session_phase, краткий reasoning (если есть).  

Для SELL основная ценность — цена выхода (в колонке), тип выхода (signal_type: TAKE_PROFIT/STOP_LOSS/TIME_EXIT/SELL) и контекст баров (bar_high/bar_low, exit_bar_close), остальное — по желанию.

---

## 4. Код

- **Построение полного дампа:** `services/deal_params_5m.py` — `build_full_entry_context(d5)`.
- **Чтение в отчётах:** `normalize_entry_context(ctx)`, `get_entry_impulse_pct(ctx)` — один и тот же код работает с полным и упрощённым форматом.
- **Запись при BUY:** `scripts/send_sndk_signal_cron.py` вызывает `build_full_entry_context(d5, correlation_entry_features=corr_feats)` (матрица из `get_cluster_decisions_5m`) и передаёт результат в `record_entry(..., entry_context=...)`.
