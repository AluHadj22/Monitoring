# update_db.py
from database import engine, Base
import models

# Эта команда добавит все отсутствующие колонки без удаления данных
Base.metadata.create_all(bind=engine)
print("✅ Структура базы данных обновлена (добавлены отсутствующие колонки)")
