"""
Универсальный загрузчик конфигурации для LSE Trading System
Использует локальный config.env или fallback к ../brats/config.env
"""

import os
import re
from pathlib import Path
from typing import Dict, Optional


def load_config(config_file: Optional[str] = None) -> Dict[str, str]:
    """
    Загружает конфигурацию из config.env
    
    Args:
        config_file: Путь к файлу конфигурации (если None, ищет локальный config.env)
        
    Returns:
        dict с параметрами конфигурации
    """
    if config_file is None:
        # Сначала пытаемся найти локальный config.env
        local_config = Path(__file__).parent / "config.env"
        if local_config.exists():
            config_file = str(local_config)
        else:
            # Fallback к ../brats/config.env
            brats_config = Path(__file__).parent.parent / "brats" / "config.env"
            if brats_config.exists():
                config_file = str(brats_config)
            else:
                raise FileNotFoundError(
                    f"Конфигурационный файл не найден. "
                    f"Ожидался: {local_config} или {brats_config}"
                )
    
    config_path = Path(config_file)
    if not config_path.exists():
        raise FileNotFoundError(f"Конфигурационный файл не найден: {config_path}")
    
    config = {}
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            # Пропускаем комментарии и пустые строки
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                config[key.strip()] = value.strip()
    
    return config


def get_database_url(config: Optional[Dict[str, str]] = None) -> str:
    """
    Получает URL базы данных из конфигурации
    
    Args:
        config: Словарь конфигурации (если None, загружается автоматически)
        
    Returns:
        URL базы данных для lse_trading
    """
    if config is None:
        config = load_config()
    
    db_url = config.get('DATABASE_URL', 'postgresql://postgres:1234@localhost:5432/brats')
    
    # Парсим DATABASE_URL и меняем базу данных на lse_trading
    match = re.match(r'postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)', db_url)
    if match:
        user, password, host, port, _ = match.groups()
        db_url_lse = f"postgresql://{user}:{password}@{host}:{port}/lse_trading"
        return db_url_lse
    else:
        raise ValueError(f"Неверный формат DATABASE_URL: {db_url}")


def get_config_value(key: str, default: Optional[str] = None, config: Optional[Dict[str, str]] = None) -> Optional[str]:
    """
    Получает значение конфигурации по ключу
    
    Args:
        key: Ключ конфигурации
        default: Значение по умолчанию
        config: Словарь конфигурации (если None, загружается автоматически)
        
    Returns:
        Значение конфигурации или default
    """
    if config is None:
        config = load_config()
    
    # Сначала проверяем переменные окружения, затем config
    value = os.getenv(key) or config.get(key, default)
    return value



