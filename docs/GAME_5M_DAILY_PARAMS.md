# Ежедневный пересчёт параметров 5m (PCT / DAYS)

Потолок тейка (GAME_5M_TAKE_PROFIT_PCT_<TICKER>) и макс. дней (GAME_5M_MAX_POSITION_DAYS_<TICKER>) можно пересчитывать каждый день по свежим 5m-данным и подставлять в игру без правки config.env.

## Зачем

- Параметры, зафиксированные в конфиге, со временем могут выбиваться из оптимальных (режим волатильности, смена тренда).
- Пересчёт по последним N сессиям даёт актуальные «подсказки»; при желании игра может подхватывать их автоматически.

## Как считается (поточнее)

1. **Потолок тейка (PCT)**  
   По каждой сессии (9:30–16:00 ET): макс. рост от открытия до хая сессии в %. По тикеру: медиана, p70, p80. **Предлагаемый потолок** = округлённый p70 (достижим в ~70% сессий), в диапазоне 2–10%.  
   Логика: `services/suggest_5m_params.compute_take_profit_suggestions()` (используется в `suggest_take_profit_caps_5m.py` и в `daily_5m_params.py`).

2. **Макс. дни (DAYS)**  
   Для каждого «входа» (open сессии S): на какой день T впервые max(High с S по T) ≥ open_S × (1 + take_pct/100). Собираем «дней до достижения тейка», считаем медиану, p70, p80. **Предлагаемый макс. дней** = ceil(p80), не более 7.  
   Логика: `services/suggest_5m_params.compute_max_days_suggestions()`. Для расчёта используется **текущий** потолок тейка (из конфига или из только что посчитанных подсказок в daily_5m_params).

## Ежедневный скрипт

```bash
# Пересчёт и запись в local/suggested_5m_params.json
python scripts/daily_5m_params.py

# Без записи (только вывод в stdout)
python scripts/daily_5m_params.py --no-write

# С отправкой сводки в Telegram (TELEGRAM_SIGNAL_CHAT_IDS)
python scripts/daily_5m_params.py --telegram

# Больше сессий для расчёта (если есть данные)
python scripts/daily_5m_params.py --sessions-take 10 --sessions-days 30
```

Рекомендуемый **cron**: раз в день после закрытия US, например 00:30 MSK или 17:30 ET:

```cron
30 0 * * 1-5  cd /path/to/lse && python scripts/daily_5m_params.py --telegram
```

## Подхват подсказок игрой 5m

В `config.env`:

```env
# При true игра 5m берёт потолок тейка и макс. дни из local/suggested_5m_params.json,
# если файл обновлён не более 25 ч назад. Иначе используются значения из config (GAME_5M_TAKE_PROFIT_PCT_*, GAME_5M_MAX_POSITION_DAYS_*).
USE_SUGGESTED_5M_PARAMS=true
```

- Файл пишется скриптом `daily_5m_params.py` (поле `updated_utc`, ключи `take_pct`, `max_days` по тикерам).
- Если `USE_SUGGESTED_5M_PARAMS` не включён или файл старее 25 ч — используются только параметры из config.env.
- Приоритет в коде: подсказка из файла (если включено и файл свежий) → GAME_5M_*_<TICKER> → общий GAME_5M_*.

## Ручной подбор (без ежедневного скрипта)

- Потолки тейка: `python scripts/suggest_take_profit_caps_5m.py` (или с `--tickers SNDK,MU`).
- Макс. дни: `python scripts/suggest_max_position_days_5m.py` (или с `--sessions 25`).

Результат можно вручную перенести в config.env (GAME_5M_TAKE_PROFIT_PCT_<TICKER>, GAME_5M_MAX_POSITION_DAYS_<TICKER>).
