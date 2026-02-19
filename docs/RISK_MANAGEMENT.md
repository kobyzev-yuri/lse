# Risk Management в LSE Trading System

## Обзор

Система управления рисками учитывает:
- **Биржа:** NYSE (Нью-Йоркская фондовая биржа)
- **Брокер:** Швейцарский банк
- **Risk Capacity:** Локальные лимиты компании (хранятся в `local/risk_limits.json`)

## Структура хранения

### Локальные файлы (НЕ в git)

Все данные о risk limits хранятся локально и **не попадают в git**:

```
local/
├── risk_limits.json          # Реальная конфигурация (НЕ в git)
├── risk_limits.example.json  # Пример конфигурации (в git)
└── README.md                 # Инструкции
```

Файлы в `local/` игнорируются через `.gitignore`.

## Настройка Risk Limits

### 1. Создание конфигурации

```bash
# Скопировать пример
cp local/risk_limits.example.json local/risk_limits.json

# Отредактировать реальными значениями
nano local/risk_limits.json
```

### 2. Структура конфигурации

```json
{
  "risk_capacity": {
    "total_capital_usd": 1000000.0,
    "max_position_size_usd": 100000.0,
    "max_portfolio_exposure_percent": 80.0,
    "max_single_ticker_exposure_percent": 20.0,
    "max_daily_loss_usd": 50000.0,
    "max_daily_loss_percent": 5.0
  },
  "position_limits": {
    "max_positions_open": 10,
    "min_position_size_usd": 1000.0
  },
  "broker_limits": {
    "swiss_bank_name": {
      "min_trade_size_usd": 1000.0,
      "max_trade_size_usd": 500000.0,
      "commission_rate": 0.001
    }
  },
  "exchange_requirements": {
    "NYSE": {
      "trading_hours_utc": {
        "open": "13:30",
        "close": "20:00"
      },
      "timezone": "America/New_York"
    }
  }
}
```

## Использование в коде

### Базовое использование

```python
from utils.risk_manager import get_risk_manager

risk_mgr = get_risk_manager()

# Получение лимитов
max_size = risk_mgr.get_max_position_size("MSFT")
max_exposure = risk_mgr.get_max_portfolio_exposure()
max_daily_loss = risk_mgr.get_max_daily_loss()
```

### Проверка перед торговлей

```python
# Проверка размера позиции
is_valid, error = risk_mgr.check_position_size(50000.0, "MSFT")
if not is_valid:
    logger.warning(f"Risk limit: {error}")
    return

# Проверка экспозиции портфеля
current_exposure = get_current_portfolio_exposure()
is_valid, error = risk_mgr.check_portfolio_exposure(
    current_exposure, 
    new_position_size=50000.0
)
if not is_valid:
    logger.warning(f"Portfolio exposure: {error}")
    return
```

### Интеграция в ExecutionAgent

`ExecutionAgent` автоматически использует `RiskManager` для:
- Ограничения размера позиций по лимитам
- Проверки экспозиции портфеля
- Проверки торговых часов NYSE
- Соблюдения максимального количества открытых позиций

## Лимиты по умолчанию

Если `risk_limits.json` не найден, используются консервативные дефолты:

- `max_position_size_usd`: 10,000 USD
- `max_portfolio_exposure_percent`: 80%
- `max_single_ticker_exposure_percent`: 20%
- `max_daily_loss_percent`: 5%
- `max_positions_open`: 10

## Проверки перед торговлей

Система автоматически проверяет:

1. **Размер позиции:**
   - Не превышает `max_position_size_usd`
   - Не меньше `min_position_size_usd`

2. **Экспозиция портфеля:**
   - Текущая + новая позиция не превышает `max_portfolio_exposure_percent`

3. **Экспозиция по тикеру:**
   - Не превышает `max_single_ticker_exposure_percent`

4. **Количество позиций:**
   - Не превышает `max_positions_open`

5. **Торговые часы:**
   - Проверка торговых часов NYSE (если настроено)

6. **Дневные потери:**
   - Не превышают `max_daily_loss_usd` или `max_daily_loss_percent`

## Мониторинг

### Логирование

Все проверки risk limits логируются:
- ✅ Успешные проверки (INFO)
- ⚠️ Нарушения лимитов (WARNING)
- ❌ Критические ошибки (ERROR)

### Примеры логов

```
INFO: Risk Manager: загружены лимиты из /path/to/local/risk_limits.json
WARNING: Risk limit нарушен для MSFT: Размер позиции 150000.00 USD превышает лимит 100000.00 USD
WARNING: Экспозиция портфеля превышена: Экспозиция портфеля 85.00% превышает лимит 80.00%
```

## Безопасность

### Важно

- ✅ Файл `risk_limits.json` **НЕ попадает в git**
- ✅ Все чувствительные данные хранятся локально
- ✅ Пример конфигурации (`risk_limits.example.json`) безопасен для публикации
- ✅ Регулярно обновляйте `risk_limits.json` при изменении лимитов

### Проверка .gitignore

Убедитесь, что в `.gitignore` есть:

```
local/
risk_config/
risk_limits.json
*.risk.json
```

## Расширение

### Добавление новых проверок

Для добавления новых risk checks:

1. Добавьте параметры в `risk_limits.json`
2. Добавьте методы в `RiskManager`
3. Интегрируйте проверки в `ExecutionAgent`

### Пример: проверка корреляции

```python
def check_correlation_limit(self, ticker1: str, ticker2: str, correlation: float) -> tuple[bool, str]:
    """Проверяет лимит корреляции между тикерами"""
    max_corr = self.config.get("risk_parameters", {}).get("max_correlation_exposure", 0.7)
    if correlation > max_corr:
        return False, f"Корреляция {correlation:.2f} превышает лимит {max_corr:.2f}"
    return True, ""
```

## См. также

- [config.env.example](config.env.example) - Общая конфигурация системы
- [execution_agent.py](execution_agent.py) - Использование risk limits в торговле
- [utils/risk_manager.py](utils/risk_manager.py) - Модуль управления рисками

---

**Статус:** Реализовано  
**Последнее обновление:** 2026-02-19
