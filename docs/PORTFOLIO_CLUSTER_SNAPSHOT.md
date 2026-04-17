# Снимок кластеров портфеля (LEADERS / CORE)

Зафиксировано для последующей доработки. Пересчёт:

```bash
cd ~/lse && docker compose exec lse python scripts/cluster_portfolio_leaders.py
```

Параметры этого снимка (вывод на инстансе, defaults скрипта):

- Окно стресса: **2026-02-15 … 2026-03-31** (25 торговых дней по рядам).
- **`stocks_only=True`**, тикеров в кластеризации: **15**.

## LEADERS

`TER`, `DELL`, `SNDK`

## CORE

`ALAB`, `AMD`, `ANET`, `INTC`, `AVGO`, `ORCL`, `PLTR`, `MSFT`, `META`, `GOOGL`, `AMZN`, `NVDA`

## JSON для `config.env` (вставка вручную)

```json
{
  "PORTFOLIO_LEADER_CLUSTER": ["TER", "DELL", "SNDK"],
  "PORTFOLIO_CORE_CLUSTER": [
    "ALAB",
    "AMD",
    "ANET",
    "INTC",
    "AVGO",
    "ORCL",
    "PLTR",
    "MSFT",
    "META",
    "GOOGL",
    "AMZN",
    "NVDA"
  ]
}
```

Полоса доходности в том прогоне: горизонт **21** торг. дн., lookback **504** дн. по каждому тикеру; процентили простой доходности за 21 дн. по пулу LEADERS — см. stdout скрипта при повторном запуске.
