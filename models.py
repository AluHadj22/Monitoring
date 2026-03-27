from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, BigInteger
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True)  # Явно задаём длину
    hashed_password = Column(String(255))  # Явно задаём длину
    role = Column(String(50), default="user")  # user, municipal_admin, regional_admin

    unit_name = Column(String(200), nullable=True)  # Название школы (до 200 символов)
    director_name = Column(String(100), nullable=True)  # ФИО директора (до 100 символов)

    # НОВОЕ ПОЛЕ - регион (субъект РФ)
    region = Column(String(200), nullable=True, index=True)
    
    district = Column(String(100), nullable=True)  # Район (опционально)
    food_type = Column(String(50), nullable=True)  # Тип питания (опционально)
    url_1c = Column(
        String(255),
        default="https://cemon.ru/MSHP/ru/",
        nullable=True  # Ссылка на 1С (может быть пустой)
    )
    
    # Связь с дашбордами
    dashboards = relationship("Dashboard", back_populates="creator")
    # Связь с отчётами
    reports = relationship("Report", back_populates="creator")


class Dashboard(Base):
    __tablename__ = "dashboards"
    
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text)
    slug = Column(String(100), unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_published = Column(Boolean, default=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    layout_data = Column(Text, default="{}")  # JSON с расположением элементов
    theme = Column(String(50), default="light")
    
    # Связи
    creator = relationship("User", back_populates="dashboards")
    elements = relationship("DashboardElement", back_populates="dashboard", cascade="all, delete-orphan")


class DashboardElement(Base):
    __tablename__ = "dashboard_elements"
    
    id = Column(Integer, primary_key=True, index=True)
    dashboard_id = Column(Integer, ForeignKey("dashboards.id", ondelete="CASCADE"), nullable=False)
    element_type = Column(String(50), nullable=False)  # chart, text, list, table
    chart_type = Column(String(50))  # line, bar, pie, doughnut
    title = Column(String(255))
    content = Column(Text, default="{}")  # JSON с данными
    settings = Column(Text, default="{}")  # JSON с настройками (цвета, размеры)
    position_x = Column(Integer, default=0)
    position_y = Column(Integer, default=0)
    width = Column(Integer, default=4)  # в условных единицах сетки
    height = Column(Integer, default=4)
    order_index = Column(Integer, default=0)
    
    # Связи
    dashboard = relationship("Dashboard", back_populates="elements")


class KnowledgeBaseCategory(Base):
    """Категории документов в библиотеке знаний"""
    __tablename__ = "knowledge_base_categories"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    icon = Column(String(100), default="📁")
    color = Column(String(50), default="#667eea")
    order_index = Column(Integer, default=0)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Связи
    documents = relationship("KnowledgeBaseDocument", back_populates="category", cascade="all, delete-orphan")


class KnowledgeBaseDocument(Base):
    """Документы в библиотеке знаний"""
    __tablename__ = "knowledge_base_documents"
    
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(1000), nullable=False)
    description = Column(Text, nullable=True)
    category_id = Column(Integer, ForeignKey("knowledge_base_categories.id", ondelete="SET NULL"), nullable=True, index=True)
    
    # Метаданные
    document_type = Column(String(100), default="document", index=True)
    file_extension = Column(String(50), nullable=True)
    file_size = Column(BigInteger, default=0)
    file_path = Column(String(1000), nullable=False)
    cover_image_path = Column(String(1000), nullable=True)
    
    # Статистика
    downloads_count = Column(BigInteger, default=0, index=True)
    views_count = Column(BigInteger, default=0, index=True)
    
    # Для поиска
    tags = Column(String(1000), nullable=True)
    
    # Кто загрузил
    uploaded_by = Column(String(500), nullable=True)
    uploaded_by_email = Column(String(500), nullable=True)
    
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
    user_email = Column(String(500), nullable=False, index=True)
    document_id = Column(Integer, ForeignKey("knowledge_base_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Связи
    document = relationship("KnowledgeBaseDocument")


class KnowledgeBaseSearchLog(Base):
    """Лог поиска для аналитики"""
    __tablename__ = "knowledge_base_search_log"
    
    id = Column(Integer, primary_key=True, index=True)
    query = Column(String(1000), nullable=False)
    user_email = Column(String(500), nullable=True, index=True)
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


# ========== МОДЕЛИ ДЛЯ УНИВЕРСАЛЬНОЙ СИСТЕМЫ УПРАВЛЕНИЯ ОТЧЁТНОСТЬЮ ==========

class ReportCategory(Base):
    """Категории отчётов"""
    __tablename__ = "report_categories"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(500), nullable=False)
    description = Column(Text)
    icon = Column(String(50), default="📊")
    color = Column(String(20), default="#667eea")
    parent_id = Column(Integer, ForeignKey("report_categories.id", ondelete="CASCADE"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    order_index = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    
    # Связи
    parent = relationship("ReportCategory", remote_side=[id], backref="children")
    reports = relationship("Report", back_populates="category", cascade="all, delete-orphan")


class Report(Base):
    """Универсальные отчёты"""
    __tablename__ = "reports"
    
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(1000), nullable=False)
    description = Column(Text)
    category_id = Column(Integer, ForeignKey("report_categories.id", ondelete="SET NULL"), nullable=True)
    
    # Тип отчёта (питание, несчастные случаи, кадетское образование и т.д.)
    report_type = Column(String(100), default="standard", index=True)
    
    # ========== НОВОЕ ПОЛЕ - регион, к которому привязан отчёт ==========
    region = Column(String(200), nullable=True, index=True)
    
    # Период
    year = Column(Integer, index=True)
    month = Column(Integer, nullable=True)
    quarter = Column(Integer, nullable=True)
    
    # Данные отчёта в JSON (универсальное хранение)
    data = Column(Text, default="{}")
    
    # Файлы, прикреплённые к отчёту (пути к файлам) - для обратной совместимости
    file_paths = Column(Text, default="[]")
    
    # Статистика просмотров
    views_count = Column(BigInteger, default=0, index=True)
    
    # Статус
    status = Column(String(50), default="draft", index=True)
    is_published = Column(Boolean, default=False, index=True)
    
    # Метаданные
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Связи
    category = relationship("ReportCategory", back_populates="reports")
    creator = relationship("User", back_populates="reports")
    files = relationship("ReportFile", back_populates="report", cascade="all, delete-orphan")
    versions = relationship("ReportVersion", back_populates="report", cascade="all, delete-orphan")
    comments = relationship("ReportComment", back_populates="report", cascade="all, delete-orphan")


class ReportFile(Base):
    """Файлы, прикреплённые к отчётам"""
    __tablename__ = "report_files"
    
    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = Column(String(500), nullable=False)
    original_name = Column(String(500), nullable=False)
    file_path = Column(String(1000), nullable=False)
    file_size = Column(BigInteger, default=0)
    file_type = Column(String(100))
    uploaded_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    
    # Связи
    report = relationship("Report", back_populates="files")


class ReportVersion(Base):
    """История версий отчётов"""
    __tablename__ = "report_versions"
    
    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number = Column(Integer, default=1)
    data_snapshot = Column(Text, default="{}")
    changed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    changed_at = Column(DateTime, default=datetime.utcnow)
    change_comment = Column(String(500))
    
    # Связи
    report = relationship("Report", back_populates="versions")


class ReportTemplate(Base):
    """Шаблоны для создания отчётов"""
    __tablename__ = "report_templates"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(500), nullable=False)
    description = Column(Text)
    report_type = Column(String(100), default="standard")
    structure = Column(Text, default="{}")
    fields = Column(Text, default="[]")
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ReportComment(Base):
    """Комментарии к отчётам"""
    __tablename__ = "report_comments"
    
    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    user_name = Column(String(500))
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Связи
    report = relationship("Report", back_populates="comments")