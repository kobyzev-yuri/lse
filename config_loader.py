"""
Универсальный загрузчик конфигурации для LSE Trading System
Использует локальный config.env или fallback к ../brats/config.env.
Если файла нет (например Cloud Run) — возвращает пустой dict, значения берутся из переменных окружения.
"""

import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

# Секреты и чувствительные значения: не показывать/не сохранять через веб /parameters.
# Список редактируемых ключей берётся из config.env.example (все незакомментированные KEY=value), кроме этого набора.
CONFIG_ENV_WEB_BLOCKLIST = frozenset(
    {
        "DATABASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_GPT_KEY",
        "NEWSAPI_KEY",
        "ALPHAVANTAGE_KEY",
        "GEMINI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "INVESTING_NEWS_PROXY",  # может содержать user:pass
    }
)

# Если config.env.example недоступен (редкие тесты) — минимальный fallback.
_FALLBACK_EDITABLE_BASE_KEYS = (
    "RESTART_CMD",
    "TRADING_CYCLE_ENABLED",
    "USE_LLM",
    "WEB_PORT",
    "LOG_LEVEL",
    "TICKERS_FAST",
)


def _path_config_env_example() -> Path:
    return Path(__file__).resolve().parent / "config.env.example"


def _parse_editable_base_keys_from_example() -> List[str]:
    p = _path_config_env_example()
    if not p.is_file():
        logger.warning("config.env.example не найден (%s), fallback список ключей для /parameters", p)
        return list(_FALLBACK_EDITABLE_BASE_KEYS)
    keys: List[str] = []
    seen: set[str] = set()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Не прочитать config.env.example: %s", e)
        return list(_FALLBACK_EDITABLE_BASE_KEYS)
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k = s.split("=", 1)[0].strip()
        if not k or not k.replace("_", "").isalnum() or not k[0].isalpha():
            continue
        if not k[0].isupper():
            continue
        if k in CONFIG_ENV_WEB_BLOCKLIST:
            continue
        if k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


_EDITABLE_BASE_KEYS_CACHE: Optional[List[str]] = None


def get_editable_base_keys_from_example() -> List[str]:
    """Ключи для веб-редактора: порядок как в config.env.example."""
    global _EDITABLE_BASE_KEYS_CACHE
    if _EDITABLE_BASE_KEYS_CACHE is None:
        _EDITABLE_BASE_KEYS_CACHE = _parse_editable_base_keys_from_example()
    return list(_EDITABLE_BASE_KEYS_CACHE)


def editable_base_key_set() -> frozenset:
    return frozenset(get_editable_base_keys_from_example())


# Префиксы пер-тикерных ключей (суффикс = тикер из TICKERS_FAST, напр. GAME_5M_TAKE_PROFIT_PCT_SNDK)
_GAME_5M_TICKER_KEY_PREFIXES = (
    "GAME_5M_TAKE_PROFIT_PCT_",
    "GAME_5M_MAX_POSITION_DAYS_",
)


def _tickers_fast_list(cfg: Optional[Dict[str, str]] = None) -> List[str]:
    if cfg is None:
        cfg = load_config()
    raw = (cfg.get("TICKERS_FAST") or "").strip()
    out: List[str] = []
    for t in raw.split(","):
        u = t.strip().upper()
        if u and u not in out:
            out.append(u)
    return out


def get_editable_config_keys_expanded() -> List[str]:
    """
    Базовые ключи из config.env.example (без blocklist) + для каждого тикера из TICKERS_FAST строки
    GAME_5M_TAKE_PROFIT_PCT_<TICKER> и GAME_5M_MAX_POSITION_DAYS_<TICKER> (если ещё нет в базовом списке).
    """
    cfg = load_config()
    base = list(get_editable_base_keys_from_example())
    seen = set(base)
    for ticker in _tickers_fast_list(cfg):
        for prefix in _GAME_5M_TICKER_KEY_PREFIXES:
            k = f"{prefix}{ticker}"
            if k not in seen:
                base.append(k)
                seen.add(k)
    return base


def is_editable_config_env_key(key: str) -> bool:
    """Можно ли править ключ через API /api/config/env."""
    key = (key or "").strip()
    if not key:
        return False
    if key in editable_base_key_set():
        return True
    for prefix in _GAME_5M_TICKER_KEY_PREFIXES:
        if key.startswith(prefix) and len(key) > len(prefix):
            suffix = key[len(prefix) :]
            if re.fullmatch(r"[A-Z0-9=\^]+", suffix):
                return True
    return False


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
