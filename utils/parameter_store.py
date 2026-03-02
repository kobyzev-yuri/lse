import logging
import json
from sqlalchemy import create_engine, text
from config_loader import get_database_url

logger = logging.getLogger(__name__)

class ParameterStore:
    """
    Кэширующий лоадер параметров стратегий из базы данных.
    """
    def __init__(self):
        try:
            self.db_url = get_database_url()
            self.engine = create_engine(self.db_url)
            self.cache = {}
            logger.info("✅ ParameterStore инициализирован")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось инициализировать ParameterStore: {e}")
            self.engine = None
            self.cache = {}

    def get_parameters(self, strategy_name: str, target_identifier: str = 'GLOBAL') -> dict:
        """
        Возвращает словарь с параметрами стратегии из БД.
        Ищет сначала для target_identifier, затем для 'GLOBAL'.
        Возвращает пустой словарь, если ничего не найдено.
        """
        if self.engine is None:
            return {}

        cache_key = (strategy_name, target_identifier)
        if cache_key in self.cache:
            return self.cache[cache_key]

        params = {}
        try:
            with self.engine.connect() as conn:
                # Пытаемся получить параметры для специфичного таргета
                result = conn.execute(
                    text("""
                        SELECT parameters FROM strategy_parameters
                        WHERE strategy_name = :strategy_name 
                        AND target_identifier = :target_identifier
                    """),
                    {"strategy_name": strategy_name, "target_identifier": target_identifier}
                ).fetchone()

                if result and result[0]:
                    params = result[0] if isinstance(result[0], dict) else json.loads(result[0])
                elif target_identifier != 'GLOBAL':
                    # Fallback on GLOBAL
                    result_global = conn.execute(
                        text("""
                            SELECT parameters FROM strategy_parameters
                            WHERE strategy_name = :strategy_name 
                            AND target_identifier = 'GLOBAL'
                        """),
                        {"strategy_name": strategy_name}
                    ).fetchone()
                    
                    if result_global and result_global[0]:
                        params = result_global[0] if isinstance(result_global[0], dict) else json.loads(result_global[0])

                if params:
                    self.cache[cache_key] = params
                    
        except Exception as e:
            logger.warning(f"⚠️ Ошибка при чтении параметров для {strategy_name}: {e}")

        return params

    def clear_cache(self):
        self.cache.clear()

_param_store = None

def get_parameter_store() -> ParameterStore:
    global _param_store
    if _param_store is None:
        _param_store = ParameterStore()
    return _param_store
