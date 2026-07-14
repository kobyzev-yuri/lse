# Portfolio 20d Trend Overlay — план (rule MVP → CatBoost)

Цель: не резать melt-up ралли (ALAB/MU/TER) на раннем trailing/take и не покупать late-chase у 20d high (INTC Jun-24).

## Проблема (prod, июнь 2026)

| Тикер | Симптом | Корень |
|-------|---------|--------|
| ALAB, TER | +8–16% и выход, дальше +50–100% | TRAILING_TAKE arm 8% / pullback 3% |
| MU | 0 portfolio swing, только GAME_5M | нет multiday hold / широкого trailing |
| INTC | BUY у 135 → −22% | late chase после +80% за 20d |
| NBIS | crash с пика | breakdown без ускоренного выхода |

Дневные стратегии + узкий trailing не различают **melt_up** и **breakdown**.

---

## Фаза 0 — rule-based (внедрено)

**Модуль:** `services/portfolio_trend_regime.py`

Режимы по 20d close:
- `melt_up` — ret_20d ≥ 12%, ≥12 дней выше SMA20
- `trend_up` — ret_20d ≥ 5%
- `breakdown` — ret_20d ≤ −5% или close < SMA20 при отриц. ret
- `neutral` — остальное

**Вход:**
- `portfolio_trend_late_chase_blocks_buy` — ret_20d ≥ 25% и near 20d high (`PORTFOLIO_TREND_LATE_CHASE_*`)
- Снимок `portfolio_trend_*` в `context_json` на BUY (`merge_portfolio_buy_context`)

**Выход:** `services/portfolio_exit_policy.py`
- regime с входа (`regime_from_context`) → ML take cap / trailing arm / pullback
- melt_up: cap take до 35%, trailing arm 14% / pullback 7%
- breakdown: cap 12%, arm 5% / pullback 2%

**UI / analyzer:**
- Карточки + chart API: `portfolio_trend_*`
- `/api/analyzer?strategy=PORTFOLIO` → `portfolio_trend_regime_review`

**Ключи:** `config.env.example` секция `PORTFOLIO_TREND_*`

---

## Фаза 1 — CatBoost horizon 20d (обучение) — **сделано (shadow .cbm)**

По схеме `portfolio_catboost` (5d), расширить горизонт до **20 торговых дней** (~1 месяц).

| Шаг | Файл | Действие | Статус |
|-----|------|----------|--------|
| 1.1 | `services/portfolio_ml_features.py` | label `forward_return_20d` (log-return) | ok (`--horizon-days`) |
| 1.2 | `scripts/train_portfolio_catboost.py` | `--horizon-days 20`, отдельный `.cbm` / meta | ok |
| 1.3 | `config.env.example` | `PORTFOLIO_CATBOOST_20D_*` | ok |
| 1.4 | JSONL | append metrics с `horizon=20` | ok |

Прод shadow: `/app/logs/ml/models/portfolio_return_catboost_20d.cbm` (2026-07-13).

---

## Фаза 2 — readiness + runtime log_only — **в коде**

| Шаг | Компонент | Статус |
|-----|-----------|--------|
| 2.1 | `run_ml_train_readiness_cron.py` — train 20d + `ML_READINESS_PORTFOLIO_20D_*` | ok |
| 2.2 | decision_stack contour `portfolio_trend_catboost` (telemetry) | ok |
| 2.3 | `predict_portfolio_expected_return_20d()` → `portfolio_ml_20d_*` | ok |
| 2.4 | soft regime hint vs rule (log_only, no apply) | ok |

BUY context / карточки / analyzer `portfolio_trend_regime_review` пишут 20d snapshot. **Без block/exit fusion.**

---

## Фаза 3 — analyzer + карточки (расширение)

| Блок analyzer | Содержание |
|---------------|------------|
| `portfolio_trend_regime_review` | rule + CatBoost score 20d, regime_counts |
| `portfolio_catboost_status` | расширить: 5d + 20d meta |
| `portfolio_ml_entry_review` | калибровка entry vs expected_20d |

Карточка: `portfolio_ml_expected_return_20d_pct`, `portfolio_trend_catboost_score`.

---

## Фаза 4 — bake-off и apply

Контрфакт на ALAB/MU/TER/INTC/NBIS (апр–июль 2026):
- rule-only vs rule+CatBoost 20d
- метрики: captured % of 20d MFE, late-chase blocks, trailing giveback

Критерий apply:
- readiness **production** для `portfolio_trend_catboost`
- analyzer arbiter **ready** или **caution** с явным sign-off
- не хуже 5d-only на win-rate / missed upside

---

## Приоритет внедрения

1. **Сейчас:** Фаза 0 deploy + мониторинг `portfolio_trend_regime_review`
2. **Неделя 1:** Фаза 1 train 20d dry-run + meta
3. **Неделя 2:** Фаза 2 readiness cron, log_only predict на BUY
4. **Неделя 3:** Фаза 3 analyzer, карточки
5. **После bake-off:** Фаза 4 — block/exit fusion apply

---

## Операции

```bash
# Локально
pytest tests/test_portfolio_trend_regime.py tests/test_portfolio_exit_policy.py -q

# Deploy (после push main)
ssh ai8049520@104.154.205.58 "cd /home/ai8049520/lse && ./scripts/deploy_from_github.sh"

# Analyzer
curl -s 'http://localhost:8080/api/analyzer?strategy=PORTFOLIO&days=30' | jq '.portfolio_trend_regime_review'
```

Связанные документы: `docs/ML_PORTFOLIO_CATBOOST.md`, `docs/GAME_5M_ML_STRATEGY_PLAN.md` (паттерн readiness→analyzer).
