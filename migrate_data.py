import sqlite3
from sqlalchemy import create_engine, text
import os
from dotenv import load_dotenv

load_dotenv()

def migrate_data():
    """Перенос данных из SQLite в PostgreSQL"""
    
    print("🔍 Начинаем перенос данных...")
    
    # Подключаемся к SQLite (твоя старая база)
    try:
        sqlite_conn = sqlite3.connect('food_management_final.db')
        sqlite_conn.row_factory = sqlite3.Row
        sqlite_cursor = sqlite_conn.cursor()
        print("✅ Подключились к SQLite")
    except Exception as e:
        print(f"❌ Ошибка подключения к SQLite: {e}")
        return
    
    # Подключаемся к PostgreSQL
    try:
        pg_engine = create_engine(os.getenv("DATABASE_URL"))
        print("✅ Подключились к PostgreSQL")
    except Exception as e:
        print(f"❌ Ошибка подключения к PostgreSQL: {e}")
        return
    
    # Получаем список таблиц из SQLite
    sqlite_cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = sqlite_cursor.fetchall()
    
    for table in tables:
        table_name = table['name']
        print(f"\n📋 Перенос таблицы: {table_name}")
        
        # Пропускаем системные таблицы SQLite
        if table_name.startswith('sqlite_'):
            continue
        
        # Получаем данные из SQLite
        sqlite_cursor.execute(f"SELECT * FROM {table_name}")
        rows = sqlite_cursor.fetchall()
        
        if not rows:
            print(f"  ⏭️  Нет данных в таблице {table_name}")
            continue
        
        # Получаем названия колонок
        columns = [description[0] for description in sqlite_cursor.description]
        
        # Очищаем таблицу в PostgreSQL перед вставкой
        with pg_engine.connect() as conn:
            conn.execute(text(f"TRUNCATE TABLE {table_name} RESTART IDENTITY CASCADE;"))
            conn.commit()
        
        # Вставляем данные в PostgreSQL
        inserted = 0
        with pg_engine.connect() as conn:
            for row in rows:
                # Преобразуем строку в словарь
                data = {}
                for col in columns:
                    value = row[col]
                    # Преобразуем None в NULL
                    if value is None:
                        data[col] = None
                    else:
                        data[col] = value
                
                # Создаем INSERT запрос
                placeholders = ', '.join([':' + col for col in columns])
                columns_str = ', '.join(columns)
                
                try:
                    conn.execute(
                        text(f"INSERT INTO {table_name} ({columns_str}) VALUES ({placeholders})"),
                        data
                    )
                    inserted += 1
                except Exception as e:
                    print(f"  ⚠️  Ошибка при вставке в {table_name}: {e}")
                    print(f"     Данные: {data}")
            
            conn.commit()
        
        print(f"  ✅ Перенесено {inserted} из {len(rows)} записей")
    
    sqlite_conn.close()
    print("\n🎉 Миграция данных завершена!")

if __name__ == "__main__":
    migrate_data()