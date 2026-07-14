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

1. ~~Фаза 0 deploy~~ / ~~Фаза 1 train~~ / ~~Фаза 2 log_only runtime~~
2. **Регулярное дообучение 20d (включено):** nightly write + probe
3. **20d в решении (apply):** prospect tiers + block avoid/weak
4. **После bake-off:** ужесточение take/fusion из 20d

### Почему раньше не влияло на игру

Gate был `DECISION_STACK_PORTFOLIO_TREND_CATBOOST_GATE_MODE=log_only` — только telemetry в snapshot.
Теперь `apply`: veto/block на `avoid` / слабый 20d score; `prefer` — приоритетные тикеры (см. analyzer `priority_top`).

---

## Регулярное дообучение 20d (ops)

| Когда | Что |
|-------|-----|
| **23:55 MSK** будни | `run_ml_refresh_dispatcher` → `run_portfolio_ml_refresh` пишет **5d + 20d** (если continuous/trigger) |
| **23:58 MSK** будни | `run_ml_train_readiness_cron` — **всегда пишет 20d** при `PORTFOLIO_CATBOOST_20D_NIGHTLY_WRITE=true` (даже если readiness dry_run) |
| **00:05 MSK** вт–сб | `run_portfolio_20d_session_probe.py` → `last_portfolio_20d_session_probe.json` (extremes + regimes) |

Ключи: `PORTFOLIO_CATBOOST_20D_CONTINUOUS_TRAIN`, `PORTFOLIO_CATBOOST_20D_NIGHTLY_WRITE`.

**Как смотреть влияние на игру:** analyzer `portfolio_trend_regime_review.session_probe` + BUY `context_json.portfolio_ml_20d_*` vs фактические exits (пока log_only — корреляции вручную / bake-off).

---

## Предложения улучшения: portfolio дневка (5d)

| # | Изменение | Зачем |
|---|-----------|--------|
| **D1** | `--drop-price-level` для horizon=5 (как 20d) | убрать close/log_close (~13% importance), поднять ranking |
| **D2** | гейт по **Spearman IC** + `pred_std_valid` в readiness | не пропускать модели с hit top-decile < baseline |
| **D3** | исключить FX (`EURUSD=X`…) из train universe 5d/20d | шум для equity game |
| **D4** | отдельно калибровать score scale 5d vs hold threshold 42 | сейчас score сжат 42–54 |

Сначала D1+D2 (одна переобучка + compare spread/hit), потом D3.

---

## Предложения улучшения: GAME_5M (5м) — **ЗАКРЫТО / не плодим ветку**

| # | Что это было | Вердикт |
|---|--------------|---------|
| **G1** | Не опираться на entry v1 (AUC≈0.46) | **УЖЕ СДЕЛАНО** — список A freeze, `FUSION=none` |
| **G2** | Promotion v2 после go/no-go | **НЕ отдельный G-трек** — это **B2 continuation** / B-list calendar; не дублируем |
| **G3** | Новые bar features (ret/SMA, vol-norm) | **FROZEN** — недели R&D без быстрого edge; A уже no-go на fusion |
| **G4** | Per-ticker калибровка P | **FROZEN** — нет быстрых $; после B4 PnL-label если вообще |
| **G5** | Weekly P(v2) distribution probe | **FROZEN** — nice-to-have, не приоритет vs B2/portfolio 20d |

**Правило:** не «телиться» с G3–G5. Быстрый результат по GAME_5M = только **список B** (B2 go/no-go → один exit apply). Entry CatBoost (A) не реанимируем без нового $-backtest.

D1–D4 ниже — это **portfolio дневка 5d**, не GAME_5M.

---

## Операции

```bash
# Локально
pytest tests/test_portfolio_trend_regime.py tests/test_portfolio_exit_policy.py -q

# Deploy
ssh ai8049520@104.197.166.185 "cd /home/ai8049520/lse && ./scripts/deploy_from_github.sh"

# Analyzer + probe
curl -s 'http://localhost:8080/api/analyzer?strategy=PORTFOLIO&days=30' | jq '.portfolio_trend_regime_review | {mode,ml_20d_ok_count,regime_hint_counts,session_probe:.session_probe.extreme_score_rows}'

# Ручной probe
docker exec lse-bot python scripts/run_portfolio_20d_session_probe.py
```

Связанные документы: `docs/ML_PORTFOLIO_CATBOOST.md`, `docs/GAME_5M_ML_STRATEGY_PLAN.md` (паттерн readiness→analyzer).
