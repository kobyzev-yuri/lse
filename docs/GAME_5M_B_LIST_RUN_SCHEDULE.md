# GAME_5M — расписание прогонов списка B (2026-07)

Операционный календарь после фиксации стратегии **12.07.2026** ([GAME_5M_ML_STRATEGY_PLAN.md](GAME_5M_ML_STRATEGY_PLAN.md)): список **A остановлен**, развиваем только **B**.

---

## 1. Сводка вех

| Дата (MSK) | Время | Прогон | Артефакт | Решение |
|------------|-------|--------|----------|---------|
| **14.07.2026 (вт)** | **07:15** | **B2 continuation go/no-go #1** | `last_game5m_b2_continuation_gonogo.json` | apply / caution / defer |
| **18.07.2026 (сб)** | **07:15** | B1 hold H3 + B3 multiday — kickoff bake-off | analyzer + post-mortem B/C | план counterfactual |
| **21.07.2026 (пн)** | **07:15** | **B2 backup review** (если 14.07 defer) | тот же JSON | apply или log_only до конца июля |
| **21–25.07** | — | B1 bake-off window | ручной разбор SELL + `hold_quality_ml` | один exit в apply к концу июля |
| **август** | — | B4 entry PnL-label | dataset + train | только если B1/B2 дали сигнал |

**Еженедельно (уже в cron):** B6 light analyzer — вс **06:45 MSK** → `analyzer_7d_light.json`.

---

## 2. B2 continuation — прогон 14.07 (primary)

### 2.1. Автоматика

Cron на VM (`crontab/lse-docker.crontab`):

```cron
# B2 continuation go/no-go — 14.07 primary, 21.07 backup (MSK 07:15, до US RTH)
15 7 14 7 * flock -n /tmp/lse_b2_gonogo.lock /home/ai8049520/lse/scripts/cron_game5m_b2_continuation_gonogo.sh
15 7 21 7 * flock -n /tmp/lse_b2_gonogo.lock /home/ai8049520/lse/scripts/cron_game5m_b2_continuation_gonogo.sh
```

### 2.2. Ручной запуск

```bash
ssh gcp-lse
docker exec lse-bot python3 /app/scripts/run_game5m_b2_continuation_gonogo_review.py --days 30
docker exec lse-bot cat /app/logs/ml/ml_data_quality/last_game5m_b2_continuation_gonogo.json | python3 -m json.tool
```

Лог на хосте: `logs/game5m_b2_continuation_gonogo.log`.

### 2.3. Гейты (G2, список B)

| # | Gate | Go если |
|---|------|---------|
| G2a | Live telemetry | ≥**8** TAKE (GAME_5M) с `continuation_ml` в SELL context (**цель 15**) |
| G2b | Predict health | доля `status=ok` ≥ **80%** |
| G2c | Offline model | `auc_valid` ≥ **0.55** (текущая модель 12.07: **0.752**, n=160) |
| G2d | Backtest | `continuation_take_delay_backtest.delta_log_return_mean` ≥ 0 (offline, без комиссий) |

**Вердикт скрипта:** `go` | `caution` | `defer` | `no_go` + `recommended_action`.

### 2.4. Действия после прогона

| Вердикт | Действие |
|---------|----------|
| **go** | Один bundle: `GAME_5M_CONTINUATION_ML_GATE_MODE=apply` (+ запись в [GAME_5M_AGENT_TUNING_LOG.md](GAME_5M_AGENT_TUNING_LOG.md)); deploy push→VM |
| **caution** | Ops vote: apply с observe 3–5 сессий **или** defer до 21.07 |
| **defer** | Оставить `log_only`; backup **21.07** |
| **no_go** | `log_only`; не трогать A; τ/модель — отдельная гипотеза |

**Не делать в том же окне:** entry fusion (A), recovery apply (A), B1 apply одновременно с B2.

### 2.5. Доп. проверки оператора (14.07)

```bash
# SQL presets в /sql → Continuation ML
curl -sS 'http://127.0.0.1:8080/api/analyzer?strategy=GAME_5M&days=30&use_llm=0&light=1' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('continuation_ml_live_review',{}).get('mode'), d.get('continuation_ml_live_review',{}).get('trades_with_continuation_ml'))"
```

---

## 3. B1 hold H3 + B3 multiday — окно 15–21.07

**Старт разбора:** 18.07 (суббота, после недели RTH).

| Контур | Что смотреть | Критерий apply (конец июля) |
|--------|--------------|-----------------------------|
| **B1** Hold H3 | SELL context `hold_quality_ml`, ≥15 строк telemetry | counterfactual defer TIME_EXIT не режет winners |
| **B3** Multiday hold | `GAME_5M_MULTIDAY_HOLD_GATE_MODE=log_only` | 5+ сессий observe; не конфликт с B2 |

**Правило:** к **концу июля** — **один** exit-контур в apply (B2 **или** B1), не оба без A/B теста.

Команды:

```bash
docker exec lse-bot python3 /app/scripts/run_game5m_light_analyzer.py --days 14
docker exec lse-bot cat /app/logs/ml/ml_data_quality/last_unified_trust_arbiter.json | python3 -m json.tool | head -80
```

---

## 4. B5 / B6 — фон (без apply)

| Контур | Cron | Примечание |
|--------|------|------------|
| B5 earnings grid | nightly ERD + вс 06:00 prod eval | labels ≥40 |
| B6 light analyzer | вс 06:45 | observability only |

---

## 5. Чеклист «прогон выполнен»

- [ ] `last_game5m_b2_continuation_gonogo.json` на VM с `generated_at_utc` за сегодня
- [ ] Вердикт и gate_checks прочитаны
- [ ] Строка в [GAME_5M_AGENT_TUNING_LOG.md](GAME_5M_AGENT_TUNING_LOG.md) (даже при defer)
- [ ] При **go** — один bundle, не смешивать с A
- [ ] `git push` → `deploy_from_github.sh` (не scp)

---

*Создано: 2026-07-13. Связано с [GAME_5M_ML_STRATEGY_PLAN.md](GAME_5M_ML_STRATEGY_PLAN.md) §8.*
