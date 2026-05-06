# Аудит: конфиг ↔ код (статус на 2026-05-06)

Цель: оптимизация кода и конфига за счёт удаления заброшенных ключей/веток **после** проверки и тестов (сегодня торговля — без рискованных деплоев).

## Инструмент

`scripts/audit_config_unused_keys.py`:

- `--mode config_to_code`: ключи в конфиге, которые не встречаются в репозитории (кандидаты на удаление из конфига/доков).
- `--mode code_to_example`: ключи, которые **читает код**, но которых **нет** в `config.env.example` (кандидаты: документировать или удалить ветку кода).

Пример:

```bash
python3 scripts/audit_config_unused_keys.py --mode code_to_example --prefix GAME_5M_ --root .
python3 scripts/audit_config_unused_keys.py --mode config_to_code --config config.env.example --prefix GAME_5M_ --root .
```

## Итоги по `GAME_5M_*` (code → example)

После того как аудит начал учитывать **закомментированные** ключи в `config.env.example`, на `2026-05-06` осталось:
**4** ключа `GAME_5M_*`, которые читаются кодом, но отсутствуют в `config.env.example`.

Список (ключ → где читается):

- `GAME_5M_BAR_HORIZON_DAYS` → `scripts/send_sndk_signal_cron.py`
- `GAME_5M_CATBOOST_APPEND_REASONING` → `services/catboost_5m_signal.py`
- `GAME_5M_CLOSE_USE_GAME5M_VWAP` → `services/game_5m.py`
- `GAME_5M_EXIT_GUARD_FIRST_MINUTES` → `services/game_5m.py`

### Интерпретация (что делать с этим списком)

Для каждого ключа выбираем одно:

- **Документировать** в `config.env.example`, если ветка реально поддерживается и используется на проде.
- **Удалить ключ + кодовую ветку**, если это заброшенный эксперимент.

Приоритет зачистки: сначала “один файл / не hot‑path / дефолт false”, затем cron‑ветки, затем `services/game_5m.py`.

## Проверка “пример DAYS”

`GAME_5M_MAX_POSITION_DAYS` и `GAME_5M_MAX_POSITION_DAYS_<TICKER>` **используются** в `services/game_5m.py` (`_max_position_days`) — это **не** мусор.

