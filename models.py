from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, BigInteger
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(50), default="user", index=True)  # Добавлен индекс

    unit_name = Column(String(500), nullable=True)  # Увеличена длина
    director_name = Column(String(255), nullable=True)  # Увеличена длина

    district = Column(String(200), nullable=True, index=True)  # Добавлен индекс
    food_type = Column(String(100), nullable=True)
    url_1c = Column(
        String(500),
        default="https://cemon.ru/MSHP/ru/",
        nullable=True
    )
    
    # Связь с дашбордами
    dashboards = relationship("Dashboard", back_populates="creator", cascade="all, delete-orphan")

class Dashboard(Base):
    __tablename__ = "dashboards"
    
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(500), nullable=False)  # Увеличена длина
    description = Column(Text)
    slug = Column(String(200), unique=True, index=True, nullable=False)  # Увеличена длина
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_published = Column(Boolean, default=False, index=True)  # Добавлен индекс
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    layout_data = Column(Text, default="{}")
    theme = Column(String(100), default="light")
    
    # Связи
    creator = relationship("User", back_populates="dashboards")
    elements = relationship("DashboardElement", back_populates="dashboard", cascade="all, delete-orphan")

class DashboardElement(Base):
    __tablename__ = "dashboard_elements"
    
    id = Column(Integer, primary_key=True, index=True)
    dashboard_id = Column(Integer, ForeignKey("dashboards.id", ondelete="CASCADE"), nullable=False, index=True)
    element_type = Column(String(100), nullable=False)
    chart_type = Column(String(100))
    title = Column(String(500))
    content = Column(Text, default="{}")
    settings = Column(Text, default="{}")
    position_x = Column(Integer, default=0)
    position_y = Column(Integer, default=0)
    width = Column(Integer, default=4)
    height = Column(Integer, default=4)
    order_index = Column(Integer, default=0)
    
    # Связи
    dashboard = relationship("Dashboard", back_populates="elements")


class KnowledgeBaseCategory(Base):
    """Категории документов в библиотеке знаний"""
    __tablename__ = "knowledge_base_categories"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(500), nullable=False)  # Увеличена длина
    description = Column(Text, nullable=True)
    icon = Column(String(100), default="📁")
    color = Column(String(50), default="#667eea")  # Добавлено поле color
    order_index = Column(Integer, default=0)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Связи
    documents = relationship("KnowledgeBaseDocument", back_populates="category", cascade="all, delete-orphan")


class KnowledgeBaseDocument(Base):
    """Документы в библиотеке знаний"""
    __tablename__ = "knowledge_base_documents"
    
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(1000), nullable=False)  # Увеличена длина
    description = Column(Text, nullable=True)
    category_id = Column(Integer, ForeignKey("knowledge_base_categories.id", ondelete="SET NULL"), nullable=True, index=True)
    
    # Метаданные
    document_type = Column(String(100), default="document", index=True)
    file_extension = Column(String(50), nullable=True)
    file_size = Column(BigInteger, default=0)  # Изменено на BigInteger для больших файлов
    file_path = Column(String(1000), nullable=False)
    cover_image_path = Column(String(1000), nullable=True)
    
    # Статистика
    downloads_count = Column(BigInteger, default=0, index=True)  # Изменено на BigInteger
    views_count = Column(BigInteger, default=0, index=True)  # Изменено на BigInteger
    
    # Для поиска
    tags = Column(String(1000), nullable=True)
    
    # Кто загрузил
    uploaded_by = Column(String(500), nullable=True)  # Изменено, т.к. может быть email
    uploaded_by_email = Column(String(500), nullable=True)  # Добавлено поле
    
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_published = Column(Boolean, default=True, index=True)
    is_featured = Column(Boolean, default=False, index=True)
    
    # Связи
    category = relationship("KnowledgeBaseCategory", back_populates="documents")


class KnowledgeBaseFavorite(Base):
    """Избранные документы пользователей"""
    __tablename__ = "knowledge_base_favorites"
    
    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String(500), nullable=False, index=True)  # Изменено с user_id
    document_id = Column(Integer, ForeignKey("knowledge_base_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Связи
    document = relationship("KnowledgeBaseDocument")


class KnowledgeBaseSearchLog(Base):
    """Лог поиска для аналитики"""
    __tablename__ = "knowledge_base_search_log"
    
    id = Column(Integer, primary_key=True, index=True)
    query = Column(String(1000), nullable=False)
    user_email = Column(String(500), nullable=True, index=True)  # Изменено с user_id
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class KnowledgeBaseAdmin(Base):
    """Администраторы библиотеки знаний"""
    __tablename__ = "knowledge_base_admins"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(500), unique=True, nullable=False, index=True)
    name = Column(String(500), nullable=True)
    access_code = Column(String(500), nullable=False)
    last_login = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class KnowledgeBaseComment(Base):
    """Комментарии к документам"""
    __tablename__ = "knowledge_base_comments"
    
    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("knowledge_base_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    user_name = Column(String(500), nullable=False)
    user_email = Column(String(500), nullable=True)
    content = Column(Text, nullable=False)
    is_approved = Column(Boolean, default=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    # Связи
    document = relationship("KnowledgeBaseDocument")