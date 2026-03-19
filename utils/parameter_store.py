import logging
import json
from typing import List, Dict, Any
from sqlalchemy import create_engine, text
from config_loader import get_database_url

logger = logging.getLogger(__name__)

# Единый источник для CRUD параметров стратегий (таблица strategy_parameters).
# Используйте get_parameter_store() и методы list_all / save / delete_by_id из кода или SQL —
# веб-редактор strategy_parameters снят; не дублируйте SQL по проекту.


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

    def list_all(self) -> List[Dict[str, Any]]:
        """
        Список всех записей strategy_parameters (ID, стратегия, target, parameters, updated_at).
        Единая функция для веб-API, отчётов и т.д.
        """
        if self.engine is None:
            return []
        out = []
        try:
            with self.engine.connect() as conn:
                result = conn.execute(
                    text("""
                        SELECT id, strategy_name, target_identifier, parameters, updated_at
                        FROM strategy_parameters
                        ORDER BY strategy_name, target_identifier
                    """)
                )
                for row in result:
                    params = row[3]
                    if params is not None and not isinstance(params, dict):
                        try:
                            params = json.loads(params) if isinstance(params, str) else dict(params)
                        except Exception:
                            params = {}
                    updated = row[4]
                    out.append({
                        "id": row[0],
                        "strategy_name": row[1],
                        "target_identifier": row[2],
                        "parameters": params or {},
                        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") and updated else None,
                    })
        except Exception as e:
            logger.warning("Ошибка list_all strategy_parameters: %s", e)
        return out

    def save(
        self,
        strategy_name: str,
        target_identifier: str,
        parameters: Dict[str, Any],
    ) -> None:
        """
        Сохранить параметры стратегии (INSERT или UPDATE по паре strategy_name, target_identifier).
        Очищает кэш после сохранения.
        """
        if self.engine is None:
            raise RuntimeError("ParameterStore engine not initialized")
        strategy_name = (strategy_name or "").strip()
        target_identifier = (target_identifier or "GLOBAL").strip()
        params_json = json.dumps(parameters) if isinstance(parameters, dict) else json.dumps({})
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO strategy_parameters (strategy_name, target_identifier, parameters, updated_at)
                        VALUES (:strategy_name, :target_identifier, CAST(:parameters AS jsonb), CURRENT_TIMESTAMP)
                        ON CONFLICT (strategy_name, target_identifier)
                        DO UPDATE SET parameters = EXCLUDED.parameters, updated_at = CURRENT_TIMESTAMP
                    """),
                    {
                        "strategy_name": strategy_name,
                        "target_identifier": target_identifier,
                        "parameters": params_json,
                    },
                )
            self.clear_cache()
        except Exception as e:
            logger.warning("Ошибка save strategy_parameters: %s", e)
            raise

    def delete_by_id(self, param_id: int) -> None:
        """Удалить запись по id. Очищает кэш."""
        if self.engine is None:
            raise RuntimeError("ParameterStore engine not initialized")
        try:
            with self.engine.begin() as conn:
                conn.execute(text("DELETE FROM strategy_parameters WHERE id = :id"), {"id": param_id})
            self.clear_cache()
        except Exception as e:
            logger.warning("Ошибка delete_by_id strategy_parameters: %s", e)
            raise


_param_store = None

def get_parameter_store() -> ParameterStore:
    global _param_store
    if _param_store is None:
        _param_store = ParameterStore()
    return _param_store
