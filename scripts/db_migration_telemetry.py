import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from init_db import load_config

def migrate_telemetry():
    """
    ЭТАП 1.1 и 1.2: Добавление телеметрии в trade_history 
    и создание таблицы strategy_parameters.
    """
    db_url_lse, *_ = load_config()
    engine = create_engine(db_url_lse)
    
    with engine.begin() as conn:
        print("Начинаем миграцию Этапа 1 (Телеметрия RLM)...")
        
        # 1.1 Добавление колонок телеметрии в trade_history
        telemetry_columns = [
            ('take_profit', 'DECIMAL', 'Уровень фиксации прибыли (если был задан)'),
            ('stop_loss', 'DECIMAL', 'Уровень ограничения убытка (если был задан)'),
            ('mfe', 'DECIMAL', 'Максимальная плавающая прибыль (Max Favorable Excursion)'),
            ('mae', 'DECIMAL', 'Максимальный плавающий убыток (Max Adverse Excursion)'),
            ('context_json', 'JSONB', 'Снимок состояния рынка в момент входа (RSI, Sentiment и т.д.)')
        ]
        
        for col_name, col_type, col_comment in telemetry_columns:
            result = conn.execute(text(f"""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='trade_history' AND column_name='{col_name}'
            """))
            if not result.fetchone():
                try:
                    conn.execute(text(f"ALTER TABLE trade_history ADD COLUMN {col_name} {col_type}"))
                    print(f"✅ Колонка {col_name} ({col_type}) добавлена в таблицу trade_history")
                except Exception as e:
                    if 'already exists' not in str(e).lower() and 'duplicate' not in str(e).lower():
                        print(f"⚠️ Предупреждение при добавлении колонки {col_name}: {e}")
            else:
                print(f"ℹ️ Колонка {col_name} уже существует в trade_history")

        # 1.2 Создание таблицы strategy_parameters
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS strategy_parameters (
                    id SERIAL PRIMARY KEY,
                    strategy_name VARCHAR(50) NOT NULL,
                    target_identifier VARCHAR(50) NOT NULL, -- 'GLOBAL', 'CLUSTER:MEMORY_CHIPS', 'TICKER:MU'
                    parameters JSONB NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_by VARCHAR(50) DEFAULT 'system',
                    UNIQUE(strategy_name, target_identifier)
                );
            """))
            print("✅ Таблица strategy_parameters создана или уже существует")
        except Exception as e:
            print(f"⚠️ Предупреждение при создании strategy_parameters: {e}")
            
    print("✅ Миграция Этапа 1 (ДБ) успешно завершена!")

if __name__ == "__main__":
    migrate_telemetry()
