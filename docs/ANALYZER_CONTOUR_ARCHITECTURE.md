# Архитектура контуров анализатора (Analyzer Contours)

Единая терминология для блоков отчёта `/analyzer`, арбитров, reviews и будущего `promotion_gate`.

**Rollout:** [ARCHITECTURE_OPTIMIZATION_ROLLOUT_PLAN.md](ARCHITECTURE_OPTIMIZATION_ROLLOUT_PLAN.md).

---

## 1. Три роли блоков

| Роль | Суффикс в API | Вопрос | Пример |
|------|---------------|--------|--------|
| **Оценка модели** | `model_eval` | Прогноз честный на OOS? | walk-forward multiday ridge |
| **Диагностика** | `diagnostic` | Что пошло не так в сделках? | `time_exit_early` + OHLC контрфакт |
| **Политика** | `policy_gate` | Можно ли усилить влияние в проде? | multiday entry/hold gates, gap forecast |

**Правило:** в `payload.contours[<contour_id>][<role>]` — объект формата `AnalyzerBlock`.

---

## 2. Контур (CalibrationContour)

Контур — логически связанный цикл: телеметрия → оценка → (опционально) promote.

| Поле | Описание |
|------|----------|
| `id` | `multiday_lr`, `gap_forecast`, `time_exit_early`, … |
| `phase` | A–E из [ML_CALIBRATION_PHASES.md](ML_CALIBRATION_PHASES.md) |
| `kind` | `model` \| `policy` \| `product` \| `calibration_log` |
| `telemetry` | `context_json` / таблица БД / offline OOS |
| `builders` | опционально: `model_eval`, `diagnostic`, `policy_gate` |
| `dependencies` | id контуров, которые должны выполниться раньше |

`PRODUCT_IDEAS` ([product_ideas_registry.py](../services/product_ideas_registry.py)) — подмножество `kind=product`.

---

## 3. Контракт `AnalyzerBlock`

```json
{
  "contour_id": "multiday_lr",
  "role": "policy_gate",
  "mode": "ok",
  "phase": "D",
  "overall_verdict": "ready_for_entry_apply",
  "metrics": {},
  "thresholds": {},
  "rationale_ru": "...",
  "next_steps_ru": ["..."],
  "promotion": {
    "eligible": true,
    "proposed_env": { "GAME_5M_MULTIDAY_ENTRY_GATE_MODE": "apply" },
    "requires": ["multiday_lr.model_eval.overall_verdict in (ready,caution)"]
  },
  "conclusion_ru": "..."
}
```

**Diagnostic** — без `promotion`, с `tuning_candidates[]`:

```json
{
  "tuning_candidates": [{
    "env_key": "GAME_5M_STALE_REVERSAL_MAX_PNL_PCT",
    "proposed": "-0.8",
    "source": "time_exit_early_diagnostic",
    "evidence": { "trade_ids": [123] },
    "confidence": "medium"
  }]
}
```

`auto_config_override` = merge(`tuning_candidates`, эвристики, LLM после фильтров).

---

## 4. Таблица alias (legacy → contours)

| Legacy key (корень payload) | contours |
|-----------------------------|----------|
| `multiday_lr_reality_check` | `multiday_lr.model_eval` |
| `multiday_lr_gates_arbiter` | `multiday_lr.entry_policy_gate` + `hold_policy_gate` (или один policy_gate с подблоками) |
| `game5m_gap_forecast_arbiter` | `gap_forecast.policy_gate` |
| `product_ideas_arbiter` | `product_ideas.policy_gate` |
| `time_exit_early_review` | `time_exit_early.diagnostic` |
| `ml_production_arbiter` | `ml_contours_summary` (агрегатор) |

На период миграции (фазы 1–13) **оба** ключа заполняются.

---

## 5. ContourRegistry

Целевой модуль: `services/analyzer_contours/registry.py`.

```python
@dataclass(frozen=True)
class ContourSpec:
    id: str
    phase: str
    kind: str
    title_ru: str
    legacy_payload_keys: tuple[str, ...]
    dependencies: tuple[str, ...]
    builders: ContourBuilders  # model_eval, diagnostic, policy_gate — опционально
```

Сборка: `attach_analyzer_contours(payload, ctx, registry=DEFAULT_REGISTRY)`.

Порядок в registry задаёт зависимости (например `multiday_lr.model_eval` перед `multiday_lr.policy_gate`).

---

## 6. promotion_gate (взаимодействие)

`policy_gate` выставляет `promotion.eligible` и `proposed_env`.

`promotion_gate` (отдельный модуль):

1. Собирает candidates из всех contours.
2. Проверяет `config_parameter_def`, active experiment, DENY_KEYS.
3. Пишет `payload.promotion_plan` — **без apply**.

Autotune / UI / operator читают только `promotion_plan` (фаза 11+).

---

## 7. LLM

- Вход: `promotion_plan` + краткий summary `contours`.
- LLM **ранжирует** и поясняет; не создаёт новые env-ключи вне plan.
- При `insufficient_data` / `remove` — не предлагать включение apply.

---

## 8. Добавление нового контура (чеклист)

1. Запись в `ANALYZER_CONTOURS` / расширение `PRODUCT_IDEAS`.
2. Телеметрия в cron / `get_decision_5m` или DDL.
3. `services/analyzer_contours/<id>.py` — builders.
4. Строка в registry + legacy keys.
5. Тест на ожидаемый `overall_verdict`.
6. Строка в [ML_CALIBRATION_PHASES.md](ML_CALIBRATION_PHASES.md).
7. UI: `renderContourBlock` (фаза 10).

---

## 9. Связанные файлы (текущий код)

| Файл | Роль |
|------|------|
| `services/trade_effectiveness_analyzer.py` | сбор отчёта, diagnostics |
| `services/analyzer_ml_arbiter.py` | multiday, gap, ml summary |
| `services/analyzer_product_ideas_arbiter.py` | продуктовые идеи |
| `services/product_ideas_registry.py` | реестр идей |
| `services/game5m_tuning_policy.py` | лимиты шага tune |
