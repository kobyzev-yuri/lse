# Сниппеты для проверки премаркета и связанной логики

Сохраняются для ручной проверки после доработок (план: `PREMARKET_PLAN.md`).

---

## 1. Контекст сессии и минуты до открытия

```python
from services.market_session import get_market_session_context
ctx = get_market_session_context()
print("phase:", ctx.get("session_phase"))
print("minutes_until_open:", ctx.get("minutes_until_open"))
print("et_now:", ctx.get("et_now"))
```

---

## 2. Премаркет-контекст по тикеру (цена, гэп, объём)

```python
from services.premarket import get_premarket_context
pm = get_premarket_context("SNDK")
print("prev_close:", pm.get("prev_close"))
print("premarket_last:", pm.get("premarket_last"))
print("premarket_gap_pct:", pm.get("premarket_gap_pct"))
print("minutes_until_open:", pm.get("minutes_until_open"))
print("error:", pm.get("error"))
```

---

## 3. Решение 5m с премаркетом (полный вывод)

Вызывать в часы премаркета US (до 9:30 ET), иначе премаркет-поля будут пустыми.

```python
from services.recommend_5m import get_decision_5m
d = get_decision_5m("SNDK", use_llm_news=False)
if d:
    print("decision:", d.get("decision"))
    print("reasoning:", (d.get("reasoning") or "")[:300])
    print("price:", d.get("price"))
    print("premarket_last:", d.get("premarket_last"))
    print("premarket_gap_pct:", d.get("premarket_gap_pct"))
    print("minutes_until_open:", d.get("minutes_until_open"))
    print("premarket_entry_recommendation:", d.get("premarket_entry_recommendation"))
    print("premarket_suggested_limit_price:", d.get("premarket_suggested_limit_price"))
    print("estimated_upside_pct_day:", d.get("estimated_upside_pct_day"))
    print("suggested_take_profit_price:", d.get("suggested_take_profit_price"))
    print("entry_advice:", d.get("entry_advice"))
    print("entry_advice_reason:", d.get("entry_advice_reason"))
```

---

## 4. Только совет по входу и апсайд (в любой фазе сессии)

```python
from services.recommend_5m import get_decision_5m
d = get_decision_5m("SNDK", use_llm_news=False)
if d:
    print("entry_advice:", d.get("entry_advice"), "—", d.get("entry_advice_reason"))
    print("estimated_upside_pct_day:", d.get("estimated_upside_pct_day"))
    print("suggested_take_profit_price:", d.get("suggested_take_profit_price"))
```

---

## 5. Запуск из корня проекта

```bash
cd /path/to/lse
python -c "
from services.market_session import get_market_session_context
from services.premarket import get_premarket_context
from services.recommend_5m import get_decision_5m

ctx = get_market_session_context()
print('Session:', ctx.get('session_phase'), '| minutes_until_open:', ctx.get('minutes_until_open'))

pm = get_premarket_context('SNDK')
print('Premarket SNDK:', pm.get('premarket_last'), '| gap%:', pm.get('premarket_gap_pct'))

d = get_decision_5m('SNDK', use_llm_news=False)
if d:
    print('Decision:', d.get('decision'))
    print('Entry advice:', d.get('entry_advice'), d.get('entry_advice_reason'))
    print('Upside%:', d.get('estimated_upside_pct_day'), '| Take price:', d.get('suggested_take_profit_price'))
"
```

---

## 6. Проверка премаркет-крона (в часы премаркета US)

```bash
cd /path/to/lse
python scripts/premarket_cron.py
# Ожидание: при phase=PRE_MARKET в логе — премаркет по каждому тикеру; при PREMARKET_ALERT_TELEGRAM=true — сообщение в Telegram
```

## 7. Проверка полей для API / Telegram

После изменений в `get_decision_5m` убедиться, что в ответе есть:

- `premarket_context` (при PRE_MARKET)
- `premarket_last`, `premarket_gap_pct`, `minutes_until_open`
- `premarket_entry_recommendation`, `premarket_suggested_limit_price` (при PRE_MARKET)
- `estimated_upside_pct_day`, `suggested_take_profit_price`
- `entry_advice`, `entry_advice_reason`

**Отображение:** в Telegram `/recommend5m` и в API `GET /api/recommend5m` эти поля уже передаются и (где нужно) выводятся: апсайд на день и цель по цене, блок «Вход: CAUTION/AVOID», блок «Премаркет» с рекомендацией.
