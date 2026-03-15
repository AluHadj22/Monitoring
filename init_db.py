import os
from database import engine, Base
from models import *
from knowledge_base_db import init_kb_db
from dotenv import load_dotenv

load_dotenv()

print("🚀 Создание таблиц в PostgreSQL...")

# Создаем основные таблицы
Base.metadata.create_all(bind=engine)
print("✅ Основные таблицы созданы")

# Создаем таблицы библиотеки знаний
init_kb_db()
print("✅ Таблицы библиотеки знаний созданы")

print("🎉 Готово! База данных инициализирована.")