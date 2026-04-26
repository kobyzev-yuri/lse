"""
Универсальный загрузчик конфигурации для LSE Trading System
Использует локальный config.env или fallback к ../brats/config.env.
Опционально подмешивает ключи из NYSE_CONFIG_PATH (тот же env, что в репозитории nyse) —
пустые/отсутствующие в LSE заполняются из nyse/config.env (например MARKETAUX_API_KEY).
Опционально перекрывает значениями из config.secrets.env (или путь в LSE_CONFIG_SECRETS /
CONFIG_SECRETS_FILE): удобно держать API-ключи и DATABASE_URL отдельно от основного конфига.
Затем опционально config.security.env (LSE_CONFIG_SECURITY / CONFIG_SECURITY_FILE) — последний
среди файлов приоритет (после secrets); кэш load_config учитывает его mtime.
Порядок приоритета для get_config_value: переменные окружения процесса, затем объединённый dict файлов.
Если файла нет (например Cloud Run) — возвращает пустой dict, значения берутся из переменных окружения.
"""

import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

# Кэш полного merge load_config(): при тысячах вызовов get_config_value() в реплее
# повторное чтение огромного config.env неприемлемо. Инвалидация по mtime файлов
# (основной config, NYSE overlay, secrets, security) + clear_load_config_cache() после записи config.env.
_LOAD_CONFIG_CACHE: Optional[tuple[tuple[Any, ...], Dict[str, str]]] = None


def clear_load_config_cache() -> None:
    """Сброс кэша load_config (после ручного редактирования файлов или в тестах)."""
    global _LOAD_CONFIG_CACHE
    _LOAD_CONFIG_CACHE = None


def _stat_mtime_ns(path: Path) -> Optional[int]:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _load_config_fingerprint(
    *,
    primary: Optional[Path],
    primary_kind: str,
) -> tuple[Any, ...]:
    """
    primary_kind: none | missing | dir | file
    primary: путь к основному config (если есть); для missing/dir — тот же путь для стабильного ключа.
    """
    parts: list[Any] = []
    if primary_kind == "file" and primary is not None:
        parts.append(("primary", str(primary.resolve()), _stat_mtime_ns(primary)))
    elif primary_kind == "dir" and primary is not None:
        parts.append(("primary_dir", str(primary.resolve()), _stat_mtime_ns(primary)))
    elif primary_kind == "missing" and primary is not None:
        parts.append(("primary_missing", str(primary.resolve())))
    else:
        parts.append(("primary_none",))

    raw_nyse = (os.environ.get("NYSE_CONFIG_PATH") or "").strip()
    parts.append(("NYSE_CONFIG_PATH", raw_nyse))
    if raw_nyse:
        np = Path(raw_nyse).expanduser()
        if np.is_file():
            parts.append(("nyse_file", str(np.resolve()), _stat_mtime_ns(np)))
        else:
            parts.append(("nyse_notfile", str(np)))

    sec = _secrets_overlay_path()
    if sec and sec.is_file():
        parts.append(("secrets", str(sec.resolve()), _stat_mtime_ns(sec)))
    else:
        parts.append(("secrets_none",))

    secu = _security_overlay_path()
    if secu and secu.is_file():
        parts.append(("security", str(secu.resolve()), _stat_mtime_ns(secu)))
    else:
        parts.append(("security_none",))

    return tuple(parts)


# Секреты и чувствительные значения: не показывать/не сохранять через веб /parameters.
# Список редактируемых ключей берётся из config.env.example (все незакомментированные KEY=value), кроме этого набора.
CONFIG_ENV_WEB_BLOCKLIST = frozenset(
    {
        "DATABASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_GPT_KEY",
        "NEWSAPI_KEY",
        "MARKETAUX_API_KEY",
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
    Секреты из config.secrets.env этим API не трогаются — правьте их вручную или отдельным деплоем.
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
        clear_load_config_cache()
        return True
    except OSError as e:
        logger.warning("Не удалось записать config.env: %s", e)
        return False


def _strip_env_value_inline_comment(value: str) -> str:
    """Убрать хвост `` # коммент`` в значении (как в bash). ``#`` внутри ``"..."`` / ``'...'`` не трогаем."""
    s = value.strip()
    if not s:
        return s
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    for i, ch in enumerate(s):
        if ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i].rstrip()
    return s


def _parse_env_file(path: Path) -> Dict[str, str]:
    """Парсит KEY=value; кавычки у значения снимаем (совместимость с nyse/config.env)."""
    out: Dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Не прочитать config overlay %s: %s", path, e)
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        k = key.strip()
        v = _strip_env_value_inline_comment(value).strip('"').strip("'")
        if k:
            out[k] = v
    return out


def _secrets_overlay_path() -> Optional[Path]:
    """Файл с секретами: явный путь из env или ./config.secrets.env рядом с config_loader."""
    raw = (os.environ.get("LSE_CONFIG_SECRETS") or os.environ.get("CONFIG_SECRETS_FILE") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_file() else None
    default = Path(__file__).resolve().parent / "config.secrets.env"
    return default if default.is_file() else None


def _merge_config_secrets_overlay(config: Dict[str, str]) -> Dict[str, str]:
    """
    Непустые ключи из secrets-файла перекрывают config.env (и NYSE overlay).
    Пустые значения в secrets игнорируются.
    """
    path = _secrets_overlay_path()
    if not path:
        return config
    overlay = _parse_env_file(path)
    if not overlay:
        return config
    for k, v in overlay.items():
        if v:
            config[k] = v
    return config


def _security_overlay_path() -> Optional[Path]:
    """Опциональный второй overlay: путь из env или ./config.security.env рядом с config_loader."""
    raw = (os.environ.get("LSE_CONFIG_SECURITY") or os.environ.get("CONFIG_SECURITY_FILE") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_file() else None
    default = Path(__file__).resolve().parent / "config.security.env"
    return default if default.is_file() else None


def _merge_config_security_overlay(config: Dict[str, str]) -> Dict[str, str]:
    """
    Непустые ключи из security-файла перекрывают всё, что уже в config (в т.ч. secrets).
    Пустые значения игнорируются.
    """
    path = _security_overlay_path()
    if not path:
        return config
    overlay = _parse_env_file(path)
    if not overlay:
        return config
    for k, v in overlay.items():
        if v:
            config[k] = v
    return config


def _merge_nyse_config_overlay(config: Dict[str, str]) -> Dict[str, str]:
    """
    Подмешать nyse/config.env по NYSE_CONFIG_PATH: только ключи, которых нет в LSE или они пустые.
    Совпадает по смыслу с nyse config_loader.load_config_env (не перетирает уже заданное).
    """
    raw = (os.environ.get("NYSE_CONFIG_PATH") or "").strip()
    if not raw:
        return config
    overlay_path = Path(raw).expanduser()
    if not overlay_path.is_file():
        logger.debug("NYSE_CONFIG_PATH не файл или не найден: %s", overlay_path)
        return config
    overlay = _parse_env_file(overlay_path)
    if not overlay:
        return config
    for k, v in overlay.items():
        if not v:
            continue
        cur = (config.get(k) or "").strip()
        if not cur:
            config[k] = v
    return config


def load_config(config_file: Optional[str] = None) -> Dict[str, str]:
    """
    Загружает конфигурацию из config.env

    Args:
        config_file: Путь к файлу конфигурации (если None, ищет локальный config.env)

    Returns:
        dict с параметрами конфигурации. Если файл не найден — пустой dict (для Cloud Run: только env).

    Результат кэшируется по mtime основного файла, NYSE overlay, secrets и security; после записи через
    update_config_key() кэш сбрасывается. os.getenv в get_config_value по-прежнему перекрывает файл.
    """
    global _LOAD_CONFIG_CACHE

    config: Dict[str, str] = {}
    primary_path: Optional[Path] = None
    primary_kind = "none"

    if config_file is None:
        local_config = Path(__file__).parent / "config.env"
        if local_config.exists():
            config_file = str(local_config)
        else:
            brats_config = Path(__file__).parent.parent / "brats" / "config.env"
            if brats_config.exists():
                config_file = str(brats_config)
            else:
                config_file = None

    if config_file:
        config_path = Path(config_file)
        primary_path = config_path
        if not config_path.exists():
            logger.debug("config.env не найден: %s", config_path)
            primary_kind = "missing"
        elif config_path.is_dir():
            # Docker: если на хосте не было файла, bind-mount мог создать каталог «config.env» — open() падал бы с IsADirectoryError.
            logger.error(
                "config.env существует как каталог, а не файл (%s). Удалите каталог на хосте, "
                "создайте файл config.env (см. config.env.example). Иначе конфиг только из переменных окружения.",
                config_path,
            )
            primary_kind = "dir"
        else:
            primary_kind = "file"

    fp = _load_config_fingerprint(primary=primary_path, primary_kind=primary_kind)
    if _LOAD_CONFIG_CACHE is not None and _LOAD_CONFIG_CACHE[0] == fp:
        return dict(_LOAD_CONFIG_CACHE[1])

    if primary_kind == "file" and primary_path is not None:
        with open(primary_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    config[key.strip()] = _strip_env_value_inline_comment(value)

    if not config:
        logger.debug("Базовый config.env не загружен или пуст — возможен только env и NYSE_CONFIG_PATH")

    merged = _merge_nyse_config_overlay(config)
    with_secrets = _merge_config_secrets_overlay(merged)
    final = _merge_config_security_overlay(with_secrets)
    snapshot = dict(final)
    _LOAD_CONFIG_CACHE = (fp, snapshot)
    return dict(snapshot)


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


def get_closed_positions_report_limits() -> tuple[int, int]:
    """
    Единые лимиты отчёта закрытых позиций: /closed в Telegram, веб /reports/closed и блок на главной.
    Ключи TELEGRAM_CLOSED_REPORT_* — историческое имя; веб использует те же значения.
    """
    raw_d = (get_config_value("TELEGRAM_CLOSED_REPORT_DEFAULT") or "25").strip()
    raw_m = (get_config_value("TELEGRAM_CLOSED_REPORT_MAX") or "200").strip()
    try:
        max_lim = max(1, int(raw_m))
    except (ValueError, TypeError):
        max_lim = 200
    try:
        default_lim = max(1, int(raw_d))
    except (ValueError, TypeError):
        default_lim = 25
    if default_lim > max_lim:
        default_lim = max_lim
    return default_lim, max_lim


def get_web_closed_positions_limits() -> tuple[int, int]:
    """
    Лимиты только для веба: /reports/closed и Excel.
    Дефолт строк — как у TELEGRAM_CLOSED_REPORT_DEFAULT.
    Верхняя граница — max(TELEGRAM_CLOSED_REPORT_MAX, WEB_CLOSED_REPORT_MAX), если WEB_* задан;
    иначе совпадает с Telegram. Так можно выгружать в Excel больше строк, не меняя лимит бота.
    """
    default_lim, tg_max = get_closed_positions_report_limits()
    raw = (get_config_value("WEB_CLOSED_REPORT_MAX") or "").strip()
    if not raw:
        return default_lim, tg_max
    try:
        w = int(raw)
    except (ValueError, TypeError):
        return default_lim, tg_max
    w = max(1, min(w, 5000))
    return default_lim, max(tg_max, w)


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
