# CatBoost и итоговый технический сигнал (игра 5m)

## Поля в `get_decision_5m`

| Поле | Смысл |
|------|--------|
| `decision` | Решение **чистых правил** (RSI, импульс, новости KB, сессия и т.д.). |
| `technical_decision_core` | То же, что `decision` (явная копия для крона и API). |
| `technical_decision_effective` | Сигнал **для входа и для LLM** после опционального слияния с CatBoost. |
| `catboost_entry_proba_good` | Вероятность «благоприятного» исхода (если модель загружена и статус `ok`). |
| `catboost_fusion_mode` / `catboost_fusion_note` | Режим слияния и короткое пояснение. |

## Выход из позиции

Тейк / стоп / SELL в `send_sndk_signal_cron` используют **`technical_decision_core`** (правила), а не `technical_decision_effective`, чтобы CatBoost не отключал логику закрытия при «мягком» HOLD по ML.

## Конфиг

- `GAME_5M_CATBOOST_FUSION` (по умолчанию `none`):
  - **`none`** — `technical_decision_effective` = `decision`.
  - **`hold_if_buy_below_p`** — если правила дали `BUY`/`STRONG_BUY`, CatBoost в статусе `ok` и `catboost_entry_proba_good` **ниже** `GAME_5M_CATBOOST_HOLD_BELOW_P` (по умолчанию `0.45`), то **effective** = `HOLD` (рассылка входа не уйдёт).
- При отсутствии модели, `feature_mismatch`, `predict_error` и т.д. слияние **не** понижает сигнал (остаётся базовое решение).

## LLM (`GAME_5M_ENTRY_STRATEGY=llm`)

В промпт передаются:

- итоговый сигнал: `technical_signal` = **effective**;
- при отличии от правил — базовый сигнал и строка CatBoost (см. `llm_service.analyze_trading_situation`).

## Отладка «без ошибок»

- Смотреть `catboost_signal_status` и `catboost_signal_note` в payload / логах.
- Ошибки загрузки и предикта логируются уровнем **warning** (не только debug).
- После замены файла `.cbm` модель подхватывается за счёт **mtime** в кэше загрузки.

См. также `docs/ML_GAME5M_CATBOOST.md`.
