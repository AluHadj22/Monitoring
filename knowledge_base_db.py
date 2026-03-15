import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from dotenv import load_dotenv

load_dotenv()

# Используем ту же базу данных
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://postgres:Toshiba3377@localhost:5432/food_management"
)

# Создаем отдельный engine для библиотеки знаний
kb_engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=5,
    pool_pre_ping=True
)

KBSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=kb_engine)
KBBase = declarative_base()

# Импортируем модели из основного файла
from models import (
    KnowledgeBaseCategory,
    KnowledgeBaseDocument,
    KnowledgeBaseFavorite,
    KnowledgeBaseSearchLog,
    KnowledgeBaseAdmin,
    KnowledgeBaseComment
)

def init_kb_db():
    """Инициализация таблиц библиотеки знаний"""
    KBBase.metadata.create_all(bind=kb_engine)

def get_kb_db():
    """Получение сессии для библиотеки знаний"""
    db = KBSessionLocal()
    try:
        yield db
    finally:
        db.close()