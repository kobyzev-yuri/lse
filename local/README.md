# Локальные конфигурационные файлы

Эта папка содержит локальные конфигурационные файлы, которые **НЕ попадают в git**.

## Risk Limits

### Настройка

1. Скопируйте пример конфигурации:
   ```bash
   cp local/risk_limits.example.json local/risk_limits.json
   ```

2. Отредактируйте `local/risk_limits.json` и заполните реальными значениями:
   - `risk_capacity` - лимиты по капиталу и рискам
   - `position_limits` - лимиты по позициям
   - `broker_limits` - лимиты брокера (швейцарский банк)
   - `exchange_requirements` - требования NYSE

### Использование

```python
from utils.risk_manager import get_risk_manager

risk_mgr = get_risk_manager()

# Проверка размера позиции
is_valid, error = risk_mgr.check_position_size(50000.0, "MSFT")
if not is_valid:
    print(f"Ошибка: {error}")

# Получение лимитов
max_size = risk_mgr.get_max_position_size()
max_exposure = risk_mgr.get_max_portfolio_exposure()
```

## Важно

- Все файлы в этой папке игнорируются git (см. `.gitignore`)
- Не коммитьте реальные risk limits в репозиторий
- Регулярно обновляйте `risk_limits.json` при изменении лимитов
