from sqlalchemy import Column, Integer, String
from database import Base



class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True)  # Явно задаём длину
    hashed_password = Column(String(255))  # Явно задаём длину
    role = Column(String(50), default="user")  # user, municipal_admin, regional_admin

    unit_name = Column(String(200), nullable=True)  # Название школы (до 200 символов)
    director_name = Column(String(100), nullable=True)  # ФИО директора (до 100 символов)

    district = Column(String(100), nullable=True)  # Район (опционально)
    food_type = Column(String(50), nullable=True)  # Тип питания (опционально)
    url_1c = Column(
        String(255),
        default="https://cemon.ru/MSHP/ru/",
        nullable=True  # Ссылка на 1С (может быть пустой)
    )
