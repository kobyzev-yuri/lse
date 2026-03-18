"""
Универсальный загрузчик конфигурации для LSE Trading System
Использует локальный config.env или fallback к ../brats/config.env.
Если файла нет (например Cloud Run) — возвращает пустой dict, значения берутся из переменных окружения.
"""

import logging
import os
import re
from pathlib import Path
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)

# Ключи config.env, которые можно редактировать из веб-интерфейса (параметры стратегий, игры 5m, порт, флаги).
# Секреты (KEY, TOKEN, PASSWORD) не включать сюда или отображать маскированно.
EDITABLE_CONFIG_KEYS = [
    "TRADING_CYCLE_ENABLED",
    "USE_LLM",
    "GAME_5M_STOP_LOSS_ENABLED",
    "GAME_5M_STOP_TO_TAKE_RATIO",
    "GAME_5M_STOP_LOSS_PCT",
    "GAME_5M_STOP_LOSS_MIN_PCT",
    "GAME_5M_MAX_POSITION_DAYS",
    "GAME_5M_TAKE_PROFIT_PCT",
    "GAME_5M_TAKE_PROFIT_MIN_PCT",
    "GAME_5M_COOLDOWN_MINUTES",
    "GAME_5M_SESSION_END_EXIT_MINUTES",
    "GAME_5M_SESSION_END_MIN_PROFIT_PCT",
    "TICKERS_FAST",
    "GAME_5M_TICKERS",
    "GAME_5M_CORRELATION_CONTEXT",
    "COMMISSION_RATE",
    "WEB_PORT",
    "LOG_LEVEL",
    "PREMARKET_ALERT_TELEGRAM",
    "PREMARKET_ENTRY_PREVIEW_5M",
    "CRON_WATCHDOG_TELEGRAM",
    "PORTFOLIO_TAKE_PROFIT_PCT",
    "SENTIMENT_AUTO_CALCULATE",
    "SENTIMENT_METHOD",
]


def get_config_file_path(config_file: Optional[str] = None) -> Optional[Path]:
    """Возвращает путь к config.env или None, если файл не найден."""
    if config_file is None:
        local_config = Path(__file__).parent / "config.env"
        if local_config.exists():
            return local_config
        brats_config = Path(__file__).parent.parent / "brats" / "config.env"
        if brats_config.exists():
            return brats_config
        return None
    p = Path(config_file)
    return p if p.exists() else None


def update_config_key(key: str, value: str) -> bool:
    """
    Обновляет или добавляет ключ в config.env. Сохраняет комментарии и порядок строк.
    Возвращает True при успехе, False если файл недоступен для записи.
    """
    path = get_config_file_path()
    if not path:
        return False
    key = key.strip()
    if not key:
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        logger.warning("Не удалось прочитать config.env: %s", e)
        return False
    found = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k == key:
                lines[i] = f"{key}={value}\n"
                found = True
                break
    if not found:
        lines.append(f"{key}={value}\n")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        return True
    except OSError as e:
        logger.warning("Не удалось записать config.env: %s", e)
        return False


def load_config(config_file: Optional[str] = None) -> Dict[str, str]:
    """
    Загружает конфигурацию из config.env

    Args:
        config_file: Путь к файлу конфигурации (если None, ищет локальный config.env)

    Returns:
        dict с параметрами конфигурации. Если файл не найден — пустой dict (для Cloud Run: только env).
    """
    if config_file is None:
        local_config = Path(__file__).parent / "config.env"
        if local_config.exists():
            config_file = str(local_config)
        else:
            brats_config = Path(__file__).parent.parent / "brats" / "config.env"
            if brats_config.exists():
                config_file = str(brats_config)
            else:
                logger.debug("config.env не найден, конфиг только из переменных окружения")
                return {}

    config_path = Path(config_file)
    if not config_path.exists():
        logger.debug("config.env не найден: %s", config_path)
        return {}

    config = {}
    with open(config_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
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
    db_url = os.getenv("DATABASE_URL") or config.get("DATABASE_URL", "postgresql://postgres:1234@localhost:5432/brats")
    
    # Парсим DATABASE_URL и меняем базу данных на lse_trading
    match = re.match(r'postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)', db_url)
    if match:
        user, password, host, port, _ = match.groups()
        # В контейнере lse-bot Postgres доступен по имени сервиса postgres, не localhost (cron и бот)
        if host in ("localhost", "127.0.0.1") and Path("/app/scripts/run_telegram_bot.py").exists():
            host = "postgres"
        db_url_lse = f"postgresql://{user}:{password}@{host}:{port}/lse_trading"
        # В БД храним московское время; при отображении конвертируем в ET (см. trade_ts_to_et, docs/TIMEZONES.md)
        db_url_lse += "?options=-c%20timezone%3DEurope%2FMoscow"
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


def get_dynamic_config_value(key: str, default: Any = None, entity: str = 'GLOBAL', engine=None) -> Any:
    """
    Получает динамическое значение конфигурации из БД strategy_parameters (RLM).
    Если значения нет в БД или engine не передан, возвращает значение из config.env.
    """
    if engine is not None:
        try:
            from sqlalchemy import text
            with engine.connect() as conn:
                res = conn.execute(
                    text("""
                        SELECT parameter_value FROM strategy_parameters 
                        WHERE parameter_name = :key 
                          AND (target_entity = 'GLOBAL' OR target_entity = :entity)
                        ORDER BY 
                            CASE WHEN target_entity = :entity THEN 1 ELSE 0 END DESC, 
                            valid_from DESC
                        LIMIT 1
                    """),
                    {"key": key, "entity": entity}
                ).fetchone()
                
                if res and res[0] is not None:
                    val = res[0]
                    # Если словарь или массив — возвращаем как есть
                    if isinstance(val, (dict, list)):
                        return val
                    # Если в JSONB была записана строка, она может быть с кавычками
                    return str(val).strip('"\'')
        except Exception as e:
            import logging
            logging.getLogger(__name__).debug(f"Ошибка загрузки динамического параметра {key}: {e}")
            pass
            
    return get_config_value(key, default)


def get_use_llm_for_analyst(engine=None) -> bool:
    """
    Включён ли LLM для аналитика (прогноз BUY/HOLD в боте, trading_cycle и т.д.).
    Читает USE_LLM из config.env или из глобальных параметров БД (strategy_parameters), если engine передан.
    false / 0 / no → не применять LLM; иначе — применять.
    """
    raw = (get_dynamic_config_value("USE_LLM", "true", engine=engine) or "true").strip().lower()
    return raw in ("1", "true", "yes")
