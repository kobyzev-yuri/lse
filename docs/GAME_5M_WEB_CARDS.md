# Карточки 5m для просмотра в Telegram

Веб-страница с карточками по каждому тикеру игры 5m, удобная для открытия в Telegram (in-app browser): компактный мониторинг, технические параметры, опционально — вывод LLM по кнопке и мини-графики.

---

## Цели

- **Один экран на тикер:** карточка с ключевыми параметрами для принятия решения.
- **Upside / Downside / «Когда дроп»:** на основе существующих полей 5m (тейк, стоп, откат от хая, сессия).
- **Понимание «на чём рост/падение»:** краткое техническое обоснование (reasoning) и, по желанию, развёрнутый вывод LLM (как prompt_entry) — **по кнопке**, чтобы не дергать LLM при каждом открытии.
- **Компактность:** карточки скроллятся сверху вниз; при необходимости — встроенный график (премаркет или 5m по сессии).

---

## Единый источник параметров

Все параметры 5m (upside, downside, prob_up/prob_down, RSI, импульс, стоп/тейк и т.д.) считаются **только в одном месте** — в `get_decision_5m()` (`services/recommend_5m.py`). Список полей для отображения задаётся константой `TECHNICAL_SIGNAL_KEYS`; общий payload для веба и Telegram собирается функцией `get_5m_card_payload(d5, ticker)`.

- **Веб (карточки):** `/api/game5m/cards` вызывает `get_decision_5m()` и формирует карточки через `get_5m_card_payload()`.
- **Telegram (recommend5m, signal5m):** те же данные: `get_decision_5m()` → `get_5m_card_payload()` для таблицы; для одного тикера — `get_5m_technical_signal()` (подмножество тех же ключей).
- **Cron (send_sndk_signal_cron):** использует `get_decision_5m()` → `get_5m_card_payload()`, `build_5m_close_context()`; текст алерта — `build_5m_entry_signal_text(d5, ticker, mentions)` из `services/signal_message_5m.py`; контекст входа — `build_full_entry_context(d5)` из `deal_params_5m.py`.
- **Тексты сообщений 5m:** единый модуль `services/signal_message_5m.py`: полный алерт «ВХОД 5m» — `build_5m_entry_signal_text()`; короткая карточка для чата — `build_5m_technical_short_text()`. Их используют cron и бот (Telegram), при необходимости — веб и другие приложения.

Таким образом, данные и форматирование сообщений 5m идут из одних и тех же модулей во всех приложениях.

---

## Данные в карточке (без LLM)

Источник: `get_decision_5m(ticker, days=5)`; для отображения — `get_5m_card_payload(d5, ticker)` (поля из `TECHNICAL_SIGNAL_KEYS`).

| Блок | Поля | Пояснение |
|------|------|-----------|
| **Решение** | decision, entry_advice, entry_advice_reason | BUY/HOLD/SELL, ALLOW/CAUTION/AVOID |
| **Upside** | estimated_upside_pct_day, suggested_take_profit_price | Точный апсайд на день, цель по цене |
| **Downside** | estimated_downside_pct_day, prob_up, prob_down | Риск просадки за день %; вероятности направления (P(up)/P(down)). Стоп (правило) — stop_loss_pct, только если GAME_5M_STOP_LOSS_ENABLED |
| **«Когда дроп» / откат** | pullback_from_high_pct, session_high, recent_bars_low_min | Откат от хая сессии %, уровень хая; при приближении к стопу — «риск дропа» |
| **Техника** | price, rsi_5m, momentum_2h_pct, period_str | Цена, RSI, импульс 2ч, период данных |
| **Контекст** | kb_news_impact, market_session.session_phase | Влияние новостей, премаркет/регулярная сессия |
| **График** | опционально | Ссылка на `/api/chart5m/{ticker}` как img или мини-sparkline |

LLM не вызывается при загрузке страницы. По кнопке «Вывод LLM» — запрос к API, который один раз вызывает анализ с корреляциями (аналог prompt_entry по тикеру) и раскрывает блок под карточкой.

---

## API

- **GET /api/game5m/cards?days=5** — список карточек по всем тикерам игры 5m (только технические данные, без LLM). Ответ: `{ "tickers": [...], "cards": [ { "ticker", "decision", "price", "rsi_5m", "momentum_2h_pct", "volatility_5m_pct", "stop_loss_pct", "take_profit_pct", "estimated_upside_pct_day", "suggested_take_profit_price", "pullback_from_high_pct", "session_high", "entry_advice", "entry_advice_reason", "reasoning", "period_str", "kb_news_impact", "session_phase", "premarket_gap_pct", ... } ], "updated_at" }`.
- **GET /api/game5m/card/{ticker}/llm** — по запросу (кнопка в карточке): вывод LLM с учётом корреляций (как в prompt_entry game5m). Ответ: `{ "ticker", "llm_reasoning", "llm_key_factors", "technical_signal" }`. В контекст LLM передаётся также **средняя волатильность за 20 дней** (см. ниже).

### Средняя волатильность за 20 дней (avg_volatility_20)

Она нужна LLM для контекста («умеренная волатильность» vs «выше среднего»). Источник — таблица **quotes** (дневные данные):

- В **quotes** по каждому тикеру и дате хранится `volatility_5` — скользящее ср. кв. откл. цены закрытия за 5 дней (считается в `update_prices_cron.py` при загрузке дневных котировок).
- **Средняя за 20 дней** = AVG(volatility_5) по последним 20 записям в quotes для тикера; в промпт LLM передаётся в % от последней цены (для сопоставимости с волатильностью 5m в %).

**Чтобы данные появились:** по тикеру должны быть заполнены дневные котировки в `quotes` (скрипт `update_prices_cron.py` по расписанию). Если по тикеру нет 20 дней в quotes, поле в контексте LLM будет пустым (N/A). Тикеры игры 5m обычно входят в список обновления цен (TICKERS_FAST / GAME_5M_TICKERS).

---

## Вёрстка для Telegram

- **Viewport:** mobile-first, узкая колонка (до 420px), чтобы в Telegram WebView всё читалось без горизонтального скролла.
- **Карточки:** одна под другой, скролл сверху вниз. В каждой: заголовок (тикер + решение), блоки «Upside / Downside / Откат», краткое обоснование, кнопка «Вывод LLM»; при необходимости — маленький график (премаркет или 5m).
- **Визуал:** цвет решения (BUY — нейтрально-зелёный, HOLD — серый, SELL — красноватый), иконки или короткие подписи для upside/downside/откат. Без тяжёлых библиотек: минимум JS, при необходимости Chart.js уже есть в base.html.
- **График:** по умолчанию можно не показывать; добавить кнопку «График 5m» или встроить img с `src="/api/chart5m/{ticker}?w=400"` если API отдаёт PNG.

---

## Роуты

- **GET /game5m/cards** (или **/monitor/5m**) — HTML-страница с карточками. В Telegram боте можно отправлять ссылку на эту страницу (например при команде /monitor5m или в подсказках после /recommend5m).

---

## Реализация

- **Шаблон:** `templates/game5m_cards.html` — отдельная страница (без base), mobile-first, тёмная тема, скролл карточек.
- **Содержимое карточки:** тикер, decision (цветовая полоска BUY/HOLD/SELL), цена, RSI(5m), Upside (тейк % и цена), Downside (стоп %), импульс 2ч, откат от хая, совет по входу, краткое reasoning. Кнопка «Вывод LLM» запрашивает `/api/game5m/card/{ticker}/llm` и раскрывает блок под карточкой. Ссылка «График 5m» ведёт на `/visualization?ticker=...`.
- **Навигация:** в `base.html` добавлена ссылка «5m карточки»; на странице карточек — кнопка «Обновить» и ссылка «Мониторинг».

---

## Итог

- Мониторинговая страница: только технические параметры и выводы по правилам 5m.
- LLM — по кнопке, один запрос на тикер при необходимости.
- Карточки компактные, с интуитивными подписями (upside, downside, откат от хая / риск дропа).
- Удобно открывать в Telegram и скроллить сверху вниз.
