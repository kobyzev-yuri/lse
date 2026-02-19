"""
Миграция: добавление полей event_type, region, importance в knowledge_base
"""

from sqlalchemy import create_engine, text
from config_loader import get_database_url
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def migrate():
    """Добавляет новые поля в knowledge_base"""
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    with engine.begin() as conn:
        # Добавляем колонки если их нет
        try:
            conn.execute(text("""
                ALTER TABLE knowledge_base 
                ADD COLUMN IF NOT EXISTS event_type VARCHAR(50)
            """))
            logger.info("✅ Добавлена колонка event_type")
        except Exception as e:
            logger.warning(f"⚠️ Колонка event_type: {e}")
        
        try:
            conn.execute(text("""
                ALTER TABLE knowledge_base 
                ADD COLUMN IF NOT EXISTS region VARCHAR(20)
            """))
            logger.info("✅ Добавлена колонка region")
        except Exception as e:
            logger.warning(f"⚠️ Колонка region: {e}")
        
        try:
            conn.execute(text("""
                ALTER TABLE knowledge_base 
                ADD COLUMN IF NOT EXISTS importance VARCHAR(10)
            """))
            logger.info("✅ Добавлена колонка importance")
        except Exception as e:
            logger.warning(f"⚠️ Колонка importance: {e}")
        
        # Добавляем колонку link для отслеживания дубликатов
        try:
            conn.execute(text("""
                ALTER TABLE knowledge_base 
                ADD COLUMN IF NOT EXISTS link VARCHAR(500)
            """))
            logger.info("✅ Добавлена колонка link")
        except Exception as e:
            logger.warning(f"⚠️ Колонка link: {e}")
        
        # Создаем индексы
        try:
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_kb_event_type 
                ON knowledge_base(event_type)
            """))
            logger.info("✅ Создан индекс idx_kb_event_type")
        except Exception as e:
            logger.warning(f"⚠️ Индекс event_type: {e}")
        
        try:
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_kb_region 
                ON knowledge_base(region)
            """))
            logger.info("✅ Создан индекс idx_kb_region")
        except Exception as e:
            logger.warning(f"⚠️ Индекс region: {e}")
        
        try:
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_kb_importance 
                ON knowledge_base(importance)
            """))
            logger.info("✅ Создан индекс idx_kb_importance")
        except Exception as e:
            logger.warning(f"⚠️ Индекс importance: {e}")
        
        try:
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_kb_link 
                ON knowledge_base(link)
            """))
            logger.info("✅ Создан индекс idx_kb_link")
        except Exception as e:
            logger.warning(f"⚠️ Индекс link: {e}")
    
    logger.info("✅ Миграция завершена")
    engine.dispose()


if __name__ == "__main__":
    migrate()
