# Стандартные библиотеки Python
import os
import tempfile
import re
import json
import asyncio
import time
import secrets
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict
from contextlib import asynccontextmanager
from fastapi.responses import JSONResponse  # Уже есть, не меняем
from fastapi.middleware.cors import CORSMiddleware  # Добавить, если нет

# Файловый ввод‑вывод
import aiofiles
import aiofiles.os
import shutil

# Веб‑фреймворк и HTTP
from fastapi import (
    FastAPI,
    Request,
    Form,
    File,
    UploadFile,
    Depends,
    HTTPException,
    Response
)
from fastapi.responses import (
    HTMLResponse,
    FileResponse,
    StreamingResponse,
    RedirectResponse,
    JSONResponse
)
from fastapi.templating import Jinja2Templates
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.cors import CORSMiddleware

# База данных
from sqlalchemy.orm import Session
from sqlalchemy import or_, func, and_
from database import engine, Base, get_db
import models
from models import User

# Аутентификация и безопасность
from jose import JWTError, jwt
import auth

# Почта и SMTP
import aiosmtplib
from email.mime.text import MIMEText
from email.headerregistry import Address

# Работа с Excel
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

# Кеширование
from cachetools import TTLCache

# Асинхронные инструменты
from concurrent.futures import ThreadPoolExecutor

# Переменные окружения
from dotenv import load_dotenv

# Сессии
from starlette.middleware.sessions import SessionMiddleware

import logging

from fastapi.staticfiles import StaticFiles

# Импортируем отдельную БД для библиотеки знаний
from knowledge_base_db import (
    get_kb_db, 
    KnowledgeBaseCategory, 
    KnowledgeBaseDocument, 
    KnowledgeBaseFavorite,
    KnowledgeBaseComment,
    KnowledgeBaseSearchLog,
    KnowledgeBaseAdmin,
    init_kb_db  # Правильное название функции
)

# НОВЫЙ ИМПОРТ для оптимизации изображений
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("⚠️  PIL не установлен. Оптимизация изображений отключена. Установите: pip install Pillow")

# Инициализируем БД библиотеки знаний при запуске
init_kb_db()

# Загрузка переменных из .env
load_dotenv()

# Создаёт таблицы в БД, если их нет
Base.metadata.create_all(bind=engine)

# Глобальные кеши для высокой нагрузки
MANIFEST_CACHE = TTLCache(maxsize=5000, ttl=300)  # Кеш манифестов
USER_CACHE = TTLCache(maxsize=1000, ttl=180)      # Кеш пользователей
FILE_EXISTS_CACHE = TTLCache(maxsize=10000, ttl=60) # Кеш проверки файлов
IMAGE_RESPONSE_CACHE = TTLCache(maxsize=200, ttl=3600)  # НОВЫЙ: Кеш для изображений (1 час)

# ThreadPool для блокирующих операций
IO_EXECUTOR = ThreadPoolExecutor(max_workers=50)
IMAGE_EXECUTOR = ThreadPoolExecutor(max_workers=4)  # НОВЫЙ: Отдельный пул для изображений

# Глобальная блокировка для кешей
CACHE_LOCK = asyncio.Lock()

# Константа для доступа к админке дашбордов
DASHBOARD_ADMIN_CODE = ""

# НОВЫЕ КОНСТАНТЫ для оптимизации изображений
THUMBNAIL_SIZES = {
    'small': (150, 150),    # Для превью
    'medium': (400, 400),    # Для списков
    'large': (800, 800),     # Для просмотра
}
JPEG_QUALITY = 85
PNG_COMPRESSION = 6
MAX_IMAGE_SIZE = (1200, 1200)  # Максимальный размер изображения

# ВАЖНО: ОПРЕДЕЛЯЕМ run_in_threadpool РАНЬШЕ, ЧТОБЫ ОНА БЫЛА ДОСТУПНА ВСЕМ
async def run_in_threadpool(func, *args, **kwargs):
    """Запуск блокирующих операций в threadpool"""
    loop = asyncio.get_event_loop()
    if kwargs:
        return await loop.run_in_executor(IO_EXECUTOR, lambda: func(*args, **kwargs))
    else:
        return await loop.run_in_executor(IO_EXECUTOR, func, *args)

def get_msk_time():
    return datetime.utcnow() + timedelta(hours=3)

async def get_cached_user(user_id: int, db: Session) -> Optional[models.User]:
    """Оптимизированное получение пользователя с кешированием"""
    cache_key = f"user_{user_id}"
    
    async with CACHE_LOCK:
        if cache_key in USER_CACHE:
            return USER_CACHE[cache_key]
        
        user = await run_in_threadpool(lambda: db.query(models.User).filter(models.User.id == user_id).first())
        if user:
            USER_CACHE[cache_key] = user
        return user

async def read_manifest_optimized(file_path: Path) -> dict:
    """Оптимизированное чтение manifest с кешированием"""
    cache_key = str(file_path)
    
    async with CACHE_LOCK:
        if cache_key in MANIFEST_CACHE:
            return MANIFEST_CACHE[cache_key].copy()
        
        manifest = {}
        exists = await run_in_threadpool(file_path.exists)
        
        if exists:
            try:
                async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                    content = await f.read()
                    manifest = json.loads(content) if content else {}
            except Exception:
                pass
        
        MANIFEST_CACHE[cache_key] = manifest.copy()
        return manifest

async def write_manifest_optimized(file_path: Path, manifest: dict):
    """Оптимизированная запись manifest с обновлением кеша"""
    cache_key = str(file_path)
    
    async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
        await f.write(json.dumps(manifest, ensure_ascii=False, indent=2))
    
    async with CACHE_LOCK:
        MANIFEST_CACHE[cache_key] = manifest.copy()

async def save_uploaded_file_optimized(file: UploadFile, dest_path: Path):
    """Оптимизированное сохранение файла"""
    content = await file.read()
    async with aiofiles.open(dest_path, "wb") as buffer:
        await buffer.write(content)
    
    cache_key = str(dest_path)
    async with CACHE_LOCK:
        FILE_EXISTS_CACHE[cache_key] = True

async def delete_file_optimized(file_path: Path):
    """Оптимизированное удаление файла с очисткой кешей"""
    try:
        if await run_in_threadpool(file_path.exists):
            await run_in_threadpool(file_path.unlink)
            
            cache_key = str(file_path)
            async with CACHE_LOCK:
                if cache_key in FILE_EXISTS_CACHE:
                    del FILE_EXISTS_CACHE[cache_key]
    except Exception:
        pass

async def list_directory_files_optimized(path: Path) -> List[Path]:
    """Асинхронное получение списка файлов в директории"""
    if not await run_in_threadpool(path.exists):
        return []
    
    try:
        items = await run_in_threadpool(lambda: list(path.iterdir()))
        files = []
        for item in items:
            if await run_in_threadpool(item.is_file):
                files.append(item)
        return files
    except OSError:
        return []

# НОВЫЕ ФУНКЦИИ ДЛЯ ОПТИМИЗАЦИИ ИЗОБРАЖЕНИЙ
async def optimize_image_async(
    input_path: Path,
    output_path: Path = None,
    max_size: tuple = MAX_IMAGE_SIZE,
    quality: int = JPEG_QUALITY
):
    """
    Асинхронная оптимизация изображения
    """
    if not HAS_PIL:
        # Если PIL не установлен, просто копируем файл
        if output_path and output_path != input_path:
            await asyncio.to_thread(shutil.copy2, input_path, output_path)
        return {
            'original_size': input_path.stat().st_size,
            'new_size': input_path.stat().st_size,
            'saved_percent': 0,
            'output_path': output_path or input_path
        }
    
    if output_path is None:
        output_path = input_path.parent / f"optimized_{input_path.name}"
    
    loop = asyncio.get_event_loop()
    
    def _optimize():
        try:
            # Открываем изображение
            with Image.open(input_path) as img:
                # Конвертируем в RGB если нужно
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                
                # Изменяем размер, сохраняя пропорции
                img.thumbnail(max_size, Image.Resampling.LANCZOS)
                
                # Определяем формат и сохраняем с оптимизацией
                format = 'JPEG' if input_path.suffix.lower() in ['.jpg', '.jpeg'] else 'PNG'
                
                save_kwargs = {
                    'format': format,
                    'optimize': True
                }
                
                if format == 'JPEG':
                    save_kwargs['quality'] = quality
                    save_kwargs['progressive'] = True
                else:
                    save_kwargs['compress_level'] = PNG_COMPRESSION
                
                img.save(output_path, **save_kwargs)
                
                original_size = input_path.stat().st_size
                new_size = output_path.stat().st_size
                
                return {
                    'original_size': original_size,
                    'new_size': new_size,
                    'saved_percent': (1 - new_size/original_size) * 100 if original_size > 0 else 0,
                    'output_path': output_path
                }
        except Exception as e:
            logger.error(f"Ошибка оптимизации {input_path}: {e}")
            # В случае ошибки копируем оригинал
            if output_path != input_path:
                shutil.copy2(input_path, output_path)
            return {
                'original_size': input_path.stat().st_size,
                'new_size': input_path.stat().st_size,
                'saved_percent': 0,
                'output_path': output_path
            }
    
    return await loop.run_in_executor(IMAGE_EXECUTOR, _optimize)

async def get_thumbnail_path(original_path: Path, size: str = 'medium') -> Path:
    """
    Получение пути к уменьшенной версии изображения
    """
    thumb_dir = original_path.parent / 'thumbnails'
    thumb_dir.mkdir(exist_ok=True)
    
    stem = original_path.stem
    ext = original_path.suffix
    
    thumbnail_path = thumb_dir / f"{stem}_{size}{ext}"
    
    # Если уменьшенная версия не существует или оригинал новее - создаем
    if not thumbnail_path.exists() or (
        original_path.stat().st_mtime > thumbnail_path.stat().st_mtime
    ):
        dimensions = THUMBNAIL_SIZES.get(size, THUMBNAIL_SIZES['medium'])
        await optimize_image_async(
            original_path,
            output_path=thumbnail_path,
            max_size=dimensions,
            quality=75 if size == 'small' else 85
        )
    
    return thumbnail_path

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Запуск оптимизированного приложения...")
    yield
    print("🔧 Очистка ресурсов...")
    MANIFEST_CACHE.clear()
    USER_CACHE.clear()
    FILE_EXISTS_CACHE.clear()
    IMAGE_RESPONSE_CACHE.clear()
    IO_EXECUTOR.shutdown()
    IMAGE_EXECUTOR.shutdown()  # НОВЫЙ: Очистка пула для изображений

app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)  # Увеличен minimum_size для сжатия

# НОВЫЙ: Добавляем CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Добавляем middleware для сессий (ВАЖНО: после GZipMiddleware)
app.add_middleware(
    SessionMiddleware, 
    secret_key=os.getenv("SECRET_KEY", "your-very-secret-key-change-in-production-12345")
)

# НОВЫЙ: Класс для статических файлов с кешированием
class CachedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            # Добавляем заголовки кеширования
            response.headers["Cache-Control"] = "public, max-age=3600"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

# Подключаем папку static/ с кешированием
app.mount("/static", CachedStaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ДЛЯ ПЕРСОНАЛЬНЫХ ДАННЫХ
@app.get("/privacy.html", response_class=HTMLResponse)
async def get_privacy(request: Request):
    try:
        return templates.TemplateResponse("privacy.html", {"request": request})
    except Exception:
        raise HTTPException(status_code=404, detail="Документ не найден")

@app.get("/agree.html", response_class=HTMLResponse)
async def get_agree(request: Request):
    try:
        return templates.TemplateResponse("agree.html", {"request": request})
    except Exception:
        raise HTTPException(status_code=404, detail="Документ не найден")

@app.get("/oferta.html", response_class=HTMLResponse)
async def get_oferta(request: Request):
    try:
        return templates.TemplateResponse("oferta.html", {"request": request})
    except Exception:
        raise HTTPException(status_code=404, detail="Документ не найден")
    
@app.get("/pay.html", response_class=HTMLResponse)
async def get_pay(request: Request):
    try:
        return templates.TemplateResponse("pay.html", {"request": request})
    except Exception:
        raise HTTPException(status_code=404, detail="Документ не найден")
    
# --- СТРАНИЦА АНАЛИЗА СТАТИСТИКИ ---
@app.get("/analis", response_class=HTMLResponse)
async def analis_page(request: Request):
    """Страница анализа статистики"""
    return templates.TemplateResponse("analis.html", {"request": request})
# ========== СТРАНИЦА УТИЛИТ ЕЦМП ==========
@app.get("/util", response_class=HTMLResponse)
async def utilities_page(request: Request):
    """Страница с утилитами ЕЦМП"""
    return templates.TemplateResponse("util.html", {"request": request})

# ========== НОВЫЕ СТРАНИЦЫ ==========

# Страница ФЦМПО
@app.get("/fcmp-support", response_class=HTMLResponse)
async def fcmp_support(request: Request):
    """Страница базы данных ФЦМПО"""
    # Проверяем, является ли пользователь админом
    is_admin = request.session.get("knowledge_base_admin", False)
    
    # Загружаем заявки из JSON файла
    import json
    requests_file = Path(__file__).resolve().parent / "data" / "fcmp_requests.json"
    
    requests = []
    if requests_file.exists():
        async with aiofiles.open(requests_file, "r", encoding="utf-8") as f:
            content = await f.read()
            if content:
                requests = json.loads(content)
    
    return templates.TemplateResponse("fcmp_support.html", {
        "request": request,
        "is_admin": is_admin,
        "requests": requests
    })

# Страница техподдержки
@app.get("/support", response_class=HTMLResponse)
async def support_page(request: Request):
    """Страница технической поддержки"""
    return templates.TemplateResponse("support.html", {"request": request})

# Страница инструктаж
@app.get("/tutorials", response_class=HTMLResponse)
async def tutorials_page(request: Request):
    """Страница с видеоинструкциями"""
    return templates.TemplateResponse("tutorials.html", {"request": request})

# API для работы с заявками (сохранение в JSON)
@app.post("/api/fcmp-request")
async def save_fcmp_request(request: Request):
    """Сохранение заявки в JSON файл"""
    import json
    data = await request.json()
    
    requests_file = Path(__file__).resolve().parent / "data" / "fcmp_requests.json"
    requests_file.parent.mkdir(exist_ok=True)
    
    # Загружаем существующие заявки
    requests = []
    if requests_file.exists():
        async with aiofiles.open(requests_file, "r", encoding="utf-8") as f:
            content = await f.read()
            if content:
                requests = json.loads(content)
    
    # Добавляем новую заявку
    new_request = {
        "id": len(requests) + 1,
        "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "school": data.get("school"),
        "email": data.get("email"),
        "problem": data.get("problem"),
        "status": "pending",
        "reply": None
    }
    requests.append(new_request)
    
    # Сохраняем
    async with aiofiles.open(requests_file, "w", encoding="utf-8") as f:
        await f.write(json.dumps(requests, ensure_ascii=False, indent=2))
    
    return {"status": "success", "id": new_request["id"]}

# Константы
FOOD_TYPES = ["Только завтраки", "Завтраки и обеды", "Интернаты", "Обеды"]
DISTRICTS = [
    "Аргун", "Ачхой-Мартановский", "Веденский", "Грозненский", "Грозный",
    "Гудермесский", "Гудермес", "Итум-Калинский", "Курчалоевский", "Надтеречный",
    "Наурский", "Ножай-Юртовский", "Серноводский", "Урус-Мартановский",
    "Шалинский", "Шаройский", "Шатойский", "Шелковской", "ГБОУ"
]
MONTHS = {
    "01": "Январь", "02": "Февраль", "03": "Март", "04": "Апрель",
    "05": "Май", "06": "Июнь", "07": "Июль", "08": "Август",
    "09": "Сентябрь", "10": "Октябрь", "11": "Ноябрь", "12": "Декабрь"
}

# Секретные коды для регистрации админов
REGIONAL_CODE = ""
MUNICIPAL_CODE = ""

# Настраиваем логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Функция для обновления данных внутри файлов excel
async def update_excel_content(
    file_path: Path,
    school_name: str,
    director_name: str,
    year: str,
    date_str: str = None
):
    temp_path = None

    try:
        temp_dir = file_path.parent
        temp_name = f"{file_path.stem}_temp_{os.getpid()}_{id(file_path)}{file_path.suffix}"
        temp_path = temp_dir / temp_name

        await asyncio.to_thread(shutil.copy2, file_path, temp_path)

        wb = load_workbook(temp_path)
        for sheet in wb.worksheets:
            if file_path.name.startswith("tm") and file_path.name.endswith(".xlsx"):
                if sheet["C1"].value is not None:
                    sheet["C1"] = school_name
                if sheet["H2"].value is not None:
                    sheet["H2"] = director_name
            elif file_path.name.startswith("kp") and file_path.name.endswith(".xlsx"):
                if sheet["B1"].value is not None:
                    sheet["B1"] = school_name
                if sheet["AD1"].value is not None:
                    sheet["AD1"] = year
            else:
                parts = file_path.stem.split("-")
                if len(parts) >= 3 and date_str:
                    if sheet["B1"].value is not None:
                        sheet["B1"] = school_name
                    if sheet["J1"].value is not None:
                        sheet["J1"] = date_str

        wb.save(temp_path)
        wb.close()

        await asyncio.to_thread(shutil.move, temp_path, file_path)

    except Exception as e:
        print(f"Ошибка при обновлении {file_path}: {e}")
        if temp_path and temp_path.exists():
            try:
                await asyncio.to_thread(temp_path.unlink)
            except Exception as del_err:
                print(f"Не удалось удалить временный файл {temp_path}: {del_err}")
        raise

    finally:
        if temp_path and temp_path.exists():
            try:
                await asyncio.to_thread(temp_path.unlink)
            except:
                pass

# Удаляем дубликат run_in_threadpool, так как он уже определен выше

async def generate_federal_html_stream(uid: int, base_path: Path, manifest: dict):
    """Потоковая генерация HTML для федерального мониторинга"""
    yield f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Ежедневное меню - Учреждение {uid}</title>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}

            body {{
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #f5f7fa 0%, #e9edf2 100%);
                min-height: 100vh;
                padding: 32px 24px;
            }}

            /* Контейнер */
            .dashboard-container {{
                max-width: 1440px;
                margin: 0 auto;
                display: flex;
                gap: 32px;
                flex-wrap: wrap;
            }}

            /* Основной контент */
            .main-content {{
                flex: 1;
                min-width: 0;
            }}

            /* Шапка */
            .hero-section {{
                background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
                border-radius: 24px;
                padding: 32px 40px;
                margin-bottom: 32px;
                position: relative;
                overflow: hidden;
            }}

            .hero-section::before {{
                content: '';
                position: absolute;
                top: -50%;
                right: -20%;
                width: 300px;
                height: 300px;
                background: radial-gradient(circle, rgba(79, 70, 229, 0.3) 0%, transparent 70%);
                border-radius: 50%;
                pointer-events: none;
            }}

            .hero-section::after {{
                content: '';
                position: absolute;
                bottom: -30%;
                left: -10%;
                width: 250px;
                height: 250px;
                background: radial-gradient(circle, rgba(139, 92, 246, 0.2) 0%, transparent 70%);
                border-radius: 50%;
                pointer-events: none;
            }}

            .hero-title {{
                font-size: 2rem;
                font-weight: 700;
                color: white;
                margin-bottom: 12px;
                display: flex;
                align-items: center;
                gap: 12px;
                flex-wrap: wrap;
                position: relative;
                z-index: 1;
            }}

            .hero-title span {{
                font-size: 0.85rem;
                background: rgba(255, 255, 255, 0.2);
                padding: 4px 12px;
                border-radius: 100px;
                font-weight: 500;
            }}

            .hero-meta {{
                display: flex;
                align-items: center;
                gap: 16px;
                color: rgba(255, 255, 255, 0.7);
                font-size: 0.85rem;
                position: relative;
                z-index: 1;
            }}

            .hero-meta svg {{
                width: 16px;
                height: 16px;
                stroke: currentColor;
            }}

            /* Карточки статистики */
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 20px;
                margin-bottom: 32px;
            }}

            .stat-card {{
                background: white;
                border-radius: 20px;
                padding: 20px;
                display: flex;
                align-items: center;
                gap: 16px;
                box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05), 0 1px 2px rgba(0, 0, 0, 0.03);
                transition: all 0.2s ease;
                border: 1px solid #eef2f6;
            }}

            .stat-card:hover {{
                transform: translateY(-2px);
                box-shadow: 0 8px 20px rgba(0, 0, 0, 0.08);
                border-color: #e2e8f0;
            }}

            .stat-icon {{
                width: 52px;
                height: 52px;
                background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
                border-radius: 16px;
                display: flex;
                align-items: center;
                justify-content: center;
                color: #4f46e5;
            }}

            .stat-icon svg {{
                width: 28px;
                height: 28px;
                stroke-width: 1.5;
            }}

            .stat-info h3 {{
                font-size: 0.75rem;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                color: #64748b;
                margin-bottom: 6px;
            }}

            .stat-info p {{
                font-size: 1.75rem;
                font-weight: 700;
                color: #0f172a;
                line-height: 1.2;
            }}

            /* Секция года */
            .year-section {{
                background: white;
                border-radius: 20px;
                margin-bottom: 24px;
                border: 1px solid #eef2f6;
                overflow: hidden;
                transition: all 0.2s ease;
            }}

            .year-section:hover {{
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.04);
            }}

            .year-header {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 18px 24px;
                background: #fafcfd;
                cursor: pointer;
                border-bottom: 1px solid #eef2f6;
                transition: all 0.2s ease;
            }}

            .year-header:hover {{
                background: #ffffff;
            }}

            .year-header h2 {{
                font-size: 1.25rem;
                font-weight: 600;
                color: #0f172a;
                display: flex;
                align-items: center;
                gap: 10px;
            }}

            .year-badge {{
                background: #eef2ff;
                color: #4f46e5;
                padding: 4px 10px;
                border-radius: 100px;
                font-size: 0.7rem;
                font-weight: 600;
            }}

            .toggle-icon {{
                color: #94a3b8;
                transition: transform 0.2s ease;
            }}

            .year-content {{
                padding: 24px;
                display: block;
            }}

            .year-content.active {{
                display: block;
            }}

            /* Сетка месяцев */
            .months-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
                gap: 20px;
            }}

            .month-card {{
                background: #fafcfd;
                border-radius: 16px;
                border: 1px solid #eef2f6;
                overflow: hidden;
                transition: all 0.2s ease;
            }}

            .month-card:hover {{
                transform: translateY(-2px);
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.04);
                border-color: #e2e8f0;
            }}

            .month-header {{
                background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
                padding: 14px 20px;
                display: flex;
                align-items: center;
                justify-content: space-between;
                border-bottom: 1px solid #eef2f6;
            }}

            .month-header h3 {{
                font-size: 1rem;
                font-weight: 600;
                color: #1e293b;
                display: flex;
                align-items: center;
                gap: 8px;
            }}

            .month-count {{
                background: #eef2ff;
                color: #4f46e5;
                padding: 2px 10px;
                border-radius: 100px;
                font-size: 0.7rem;
                font-weight: 600;
            }}

            .month-files {{
                padding: 16px;
            }}

            /* Файлы */
            .file-row {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 12px;
                background: white;
                border-radius: 12px;
                margin-bottom: 8px;
                border: 1px solid #f1f5f9;
                transition: all 0.2s ease;
            }}

            .file-row:hover {{
                border-color: #e2e8f0;
                box-shadow: 0 2px 6px rgba(0, 0, 0, 0.02);
            }}

            .file-info {{
                display: flex;
                align-items: center;
                gap: 12px;
                flex: 1;
                min-width: 0;
            }}

            .file-icon {{
                width: 36px;
                height: 36px;
                background: #f1f5f9;
                border-radius: 10px;
                display: flex;
                align-items: center;
                justify-content: center;
                flex-shrink: 0;
            }}

            .file-icon svg {{
                width: 18px;
                height: 18px;
                color: #4f46e5;
            }}

            .file-details {{
                flex: 1;
                min-width: 0;
            }}

            .file-name {{
                font-weight: 500;
                font-size: 0.85rem;
                color: #1e293b;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
                margin-bottom: 4px;
            }}

            .file-meta {{
                display: flex;
                align-items: center;
                gap: 12px;
                font-size: 0.7rem;
                color: #64748b;
            }}

            .file-size {{
                background: #f1f5f9;
                padding: 2px 8px;
                border-radius: 20px;
                font-weight: 500;
            }}

            .download-link {{
                background: #f1f5f9;
                padding: 8px 12px;
                border-radius: 10px;
                display: inline-flex;
                align-items: center;
                gap: 6px;
                text-decoration: none;
                color: #4f46e5;
                font-size: 0.75rem;
                font-weight: 500;
                transition: all 0.2s ease;
                flex-shrink: 0;
            }}

            .download-link:hover {{
                background: #4f46e5;
                color: white;
            }}

            .download-link svg {{
                width: 14px;
                height: 14px;
                stroke: currentColor;
            }}

            /* Сайдбар */
            .sidebar {{
                width: 360px;
                flex-shrink: 0;
            }}

            .sidebar-sticky {{
                position: sticky;
                top: 32px;
            }}

            .sidebar-card {{
                background: white;
                border-radius: 20px;
                padding: 24px;
                margin-bottom: 24px;
                border: 1px solid #eef2f6;
                box-shadow: 0 1px 3px rgba(0, 0, 0, 0.03);
            }}

            .sidebar-title {{
                font-size: 1rem;
                font-weight: 600;
                color: #0f172a;
                margin-bottom: 20px;
                padding-bottom: 12px;
                border-bottom: 2px solid #eef2f6;
                display: flex;
                align-items: center;
                gap: 10px;
            }}

            .sidebar-title svg {{
                width: 20px;
                height: 20px;
                color: #4f46e5;
            }}

            .special-card {{
                background: linear-gradient(135deg, #fffbeb 0%, #fef3c7 100%);
                border: 1px solid #fde047;
                border-radius: 16px;
                padding: 16px;
                margin-bottom: 16px;
            }}

            .special-title {{
                font-weight: 600;
                color: #b45309;
                margin-bottom: 12px;
                display: flex;
                align-items: center;
                gap: 8px;
                font-size: 0.85rem;
            }}

            .category-group {{
                margin-bottom: 20px;
            }}

            .category-group h4 {{
                font-size: 0.75rem;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                color: #4f46e5;
                margin-bottom: 12px;
            }}

            /* Карточка соответствия */
            .compliance-card {{
                background: linear-gradient(135deg, #ecfdf5 0%, #d1fae5 100%);
                border: 1px solid #6ee7b7;
                border-radius: 16px;
                padding: 20px;
                display: flex;
                gap: 16px;
                margin: 20px 0;
            }}

            .compliance-icon {{
                width: 48px;
                height: 48px;
                background: #10b981;
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                flex-shrink: 0;
            }}

            .compliance-icon svg {{
                width: 24px;
                height: 24px;
                stroke: white;
            }}

            .compliance-text strong {{
                font-size: 0.9rem;
                color: #065f46;
                display: block;
                margin-bottom: 4px;
            }}

            .compliance-text p {{
                font-size: 0.75rem;
                color: #047857;
                line-height: 1.4;
            }}

            /* Карточка ссылки */
            .link-card {{
                background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%);
                border: 1px solid #93c5fd;
                border-radius: 16px;
                padding: 16px;
                margin-top: 16px;
            }}

            .link-card a {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                text-decoration: none;
                color: #1e40af;
                font-weight: 500;
                font-size: 0.85rem;
            }}

            .link-card a:hover {{
                text-decoration: underline;
            }}

            /* Пустое состояние */
            .empty-state {{
                text-align: center;
                padding: 64px 32px;
                background: white;
                border-radius: 24px;
                border: 1px solid #eef2f6;
            }}

            .empty-icon {{
                width: 80px;
                height: 80px;
                margin: 0 auto 20px;
                color: #cbd5e1;
            }}

            .empty-state h3 {{
                font-size: 1.1rem;
                font-weight: 600;
                color: #1e293b;
                margin-bottom: 8px;
            }}

            .empty-state p {{
                color: #64748b;
                font-size: 0.85rem;
            }}

            /* Адаптивность */
            @media (max-width: 1024px) {{
                .dashboard-container {{
                    flex-direction: column;
                }}
                
                .sidebar {{
                    width: 100%;
                }}
                
                .sidebar-sticky {{
                    position: static;
                }}
            }}

            @media (max-width: 768px) {{
                body {{
                    padding: 16px;
                }}
                
                .hero-section {{
                    padding: 24px 20px;
                }}
                
                .hero-title {{
                    font-size: 1.5rem;
                }}
                
                .stats-grid {{
                    grid-template-columns: repeat(2, 1fr);
                }}
                
                .months-grid {{
                    grid-template-columns: 1fr;
                }}
                
                .file-row {{
                    flex-direction: column;
                    align-items: flex-start;
                    gap: 12px;
                }}
                
                .download-link {{
                    width: 100%;
                    justify-content: center;
                }}
            }}

            /* Анимации */
            @keyframes fadeInUp {{
                from {{
                    opacity: 0;
                    transform: translateY(12px);
                }}
                to {{
                    opacity: 1;
                    transform: translateY(0);
                }}
            }}

            .animate-in {{
                animation: fadeInUp 0.4s ease-out forwards;
                opacity: 0;
            }}
        </style>
    </head>
    <body>
        <div class="dashboard-container">
            <div class="main-content">
                <!-- Hero секция -->
                <div class="hero-section animate-in">
                    <div class="hero-title">
                        Ежедневное меню
                        <span>Учреждение №{uid}</span>
                    </div>
                    <div class="hero-meta">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect>
                            <line x1="16" y1="2" x2="16" y2="6"></line>
                            <line x1="8" y1="2" x2="8" y2="6"></line>
                            <line x1="3" y1="10" x2="21" y2="10"></line>
                        </svg>
                        {datetime.now().strftime('%d.%m.%Y %H:%M')}
                    </div>
                </div>
    """

    files = await list_directory_files_optimized(base_path)
    
    if not files:
        yield """
                <div class="empty-state animate-in">
                    <svg class="empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                        <polyline points="14 2 14 8 20 8"/>
                        <line x1="12" y1="18" x2="12" y2="12"/>
                        <line x1="9" y1="15" x2="15" y2="15"/>
                    </svg>
                    <h3>Нет доступных файлов</h3>
                    <p>В данном учреждении пока не загружены файлы меню</p>
                </div>
            </div>
        </div>
    </body>
    </html>
        """
        return

    grouped_files = {}
    tm_files_by_year = {}
    kp_files_by_year = {}
    findex_files = []
    
    total_files = 0
    total_size = 0

    for f in files:
        if f.name == "manifest.json":
            continue
        if not await run_in_threadpool(f.exists):
            continue

        file_meta = manifest.get(f.name, {})
        date_str = file_meta.get("upload_datetime", "")
        stat_result = await run_in_threadpool(f.stat)
        total_files += 1
        total_size += stat_result.st_size

        date_from_name_match = re.search(r'(\d{4})-(\d{2})-(\d{2})', f.name)
        if date_from_name_match:
            y, m, d = date_from_name_match.groups()
            dt = datetime(int(y), int(m), int(d))
            assigned_year, assigned_month = str(dt.year), str(dt.month).zfill(2)
            month_name = MONTHS.get(assigned_month, assigned_month)
            file_info = {
                "filename": f.name,
                "date": dt.strftime("%d.%m.%Y %H:%M"),
                "size": stat_result.st_size
            }
            grouped_files.setdefault(assigned_year, {}).setdefault(month_name, []).append(file_info)
            continue

        if f.name.lower() == "findex.xlsx":
            try:
                dt = (datetime.strptime(date_str, "%d.%m.%Y %H:%M")
                      if date_str
                      else datetime.fromtimestamp(stat_result.st_mtime))
            except Exception:
                dt = datetime.now()
            findex_files.append({
                "filename": f.name,
                "date": dt.strftime("%d.%m.%Y %H:%M"),
                "size": stat_result.st_size
            })
            continue

        try:
            dt = (datetime.strptime(date_str, "%d.%m.%Y %H:%M")
                  if date_str
                  else datetime.fromtimestamp(stat_result.st_mtime))
        except Exception:
            dt = datetime.now()

        assigned_year = file_meta.get("assigned_year", str(dt.year))
        assigned_month = file_meta.get("assigned_month", dt.strftime("%m"))
        month_name = MONTHS.get(assigned_month, assigned_month)
        file_info = {
            "filename": f.name,
            "date": dt.strftime("%d.%m.%Y %H:%M"),
            "size": stat_result.st_size
        }

        if re.match(r"^tm\d{4}-sm\.xlsx$", f.name):
            tm_year = f.name[2:6]
            tm_files_by_year.setdefault(tm_year, []).append(file_info)
            continue
        if re.match(r"^kp\d{4}\.xlsx$", f.name):
            kp_year = f.name[2:6]
            kp_files_by_year.setdefault(kp_year, []).append(file_info)
            continue

        grouped_files.setdefault(assigned_year, {}).setdefault(month_name, []).append(file_info)

    total_size_mb = total_size / (1024 * 1024)
    
    yield f"""
                <!-- Статистика -->
                <div class="stats-grid">
                    <div class="stat-card animate-in" style="animation-delay: 0s">
                        <div class="stat-icon">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                                <polyline points="14 2 14 8 20 8"/>
                            </svg>
                        </div>
                        <div class="stat-info">
                            <h3>Всего файлов</h3>
                            <p>{total_files}</p>
                        </div>
                    </div>
                    <div class="stat-card animate-in" style="animation-delay: 0.05s">
                        <div class="stat-icon">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                                <polyline points="7 10 12 15 17 10"/>
                                <line x1="12" y1="15" x2="12" y2="3"/>
                            </svg>
                        </div>
                        <div class="stat-info">
                            <h3>Общий объем</h3>
                            <p>{total_size_mb:.1f} MB</p>
                        </div>
                    </div>
                    <div class="stat-card animate-in" style="animation-delay: 0.1s">
                        <div class="stat-icon">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>
                                <line x1="16" y1="2" x2="16" y2="6"/>
                                <line x1="8" y1="2" x2="8" y2="6"/>
                                <line x1="3" y1="10" x2="21" y2="10"/>
                            </svg>
                        </div>
                        <div class="stat-info">
                            <h3>Лет в архиве</h3>
                            <p>{len(grouped_files)}</p>
                        </div>
                    </div>
                    <div class="stat-card animate-in" style="animation-delay: 0.15s">
                        <div class="stat-icon">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                <path d="M4 4h16v16H4z"/>
                                <path d="M8 8h8M8 12h6M8 16h4"/>
                            </svg>
                        </div>
                        <div class="stat-info">
                            <h3>Типов меню</h3>
                            <p>{len(tm_files_by_year)}</p>
                        </div>
                    </div>
                </div>
    """

    for year in sorted(grouped_files.keys(), reverse=True):
        year_total = sum(len(files) for files in grouped_files[year].values())
        
        yield f"""
                <div class="year-section animate-in">
                    <div class="year-header" onclick="toggleYear(this)">
                        <h2>
                            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>
                                <line x1="16" y1="2" x2="16" y2="6"/>
                                <line x1="8" y1="2" x2="8" y2="6"/>
                                <line x1="3" y1="10" x2="21" y2="10"/>
                            </svg>
                            {year} год
                            <span class="year-badge">{year_total} файлов</span>
                        </h2>
                        <svg class="toggle-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polyline points="6 9 12 15 18 9"></polyline>
                        </svg>
                    </div>
                    <div class="year-content">
                        <div class="months-grid">
        """
        
        for month in sorted(grouped_files[year].keys(), reverse=True):
            month_files = grouped_files[year][month]
            month_files.sort(key=lambda x: datetime.strptime(x["date"], "%d.%m.%Y %H:%M"))
            
            yield f"""
                            <div class="month-card">
                                <div class="month-header">
                                    <h3>
                                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                            <path d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2z"/>
                                        </svg>
                                        {month}
                                    </h3>
                                    <span class="month-count">{len(month_files)}</span>
                                </div>
                                <div class="month-files">
            """
            
            for file_info in month_files:
                size_kb = file_info["size"] // 1024
                file_ext = Path(file_info["filename"]).suffix.lower()
                
                yield f"""
                                    <div class="file-row">
                                        <div class="file-info">
                                            <div class="file-icon">
                                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                                                    <polyline points="14 2 14 8 20 8"/>
                                                </svg>
                                            </div>
                                            <div class="file-details">
                                                <div class="file-name">{file_info["filename"]}</div>
                                                <div class="file-meta">
                                                    <span class="file-size">{size_kb} KB</span>
                                                    <span>{file_info["date"]}</span>
                                                </div>
                                            </div>
                                        </div>
                                        <a href="{file_info["filename"]}" class="download-link" download>
                                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                                                <polyline points="7 10 12 15 17 10"/>
                                                <line x1="12" y1="15" x2="12" y2="3"/>
                                            </svg>
                                            Скачать
                                        </a>
                                    </div>
                """
            
            yield """
                                </div>
                            </div>
            """
        
        yield """
                        </div>
                    </div>
                </div>
        """

    if not grouped_files and not findex_files:
        yield """
                <div class="empty-state animate-in">
                    <svg class="empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                        <polyline points="14 2 14 8 20 8"/>
                        <line x1="12" y1="18" x2="12" y2="12"/>
                        <line x1="9" y1="15" x2="15" y2="15"/>
                    </svg>
                    <h3>Нет доступных файлов</h3>
                    <p>В данном учреждении пока не загружены файлы меню</p>
                </div>
        """

    yield """
            </div>
            <!-- /main-content -->
    """

    if any([findex_files, tm_files_by_year, kp_files_by_year]):
        yield """
            <div class="sidebar">
                <div class="sidebar-sticky">
        """

        if findex_files:
            yield """
                    <div class="sidebar-card">
                        <div class="sidebar-title">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                <path d="M4 4h16v16H4z"/>
                                <path d="M8 8h8M8 12h6M8 16h4"/>
                            </svg>
                            ФЦМПО
                        </div>
                        <div class="special-card">
                            <div class="special-title">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                    <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>
                                </svg>
                                Файл качества питания
                            </div>
            """
            
            for fi in findex_files:
                size_kb = fi["size"] // 1024
                yield f"""
                            <div class="file-row">
                                <div class="file-info">
                                    <div class="file-icon">
                                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                            <path d="M4 4h16v16H4z"/>
                                            <polyline points="8 12 12 16 16 8"/>
                                        </svg>
                                    </div>
                                    <div class="file-details">
                                        <div class="file-name">{fi["filename"]}</div>
                                        <div class="file-meta">
                                            <span class="file-size">{size_kb} KB</span>
                                            <span>{fi["date"]}</span>
                                        </div>
                                    </div>
                                </div>
                                <a href="{fi["filename"]}" class="download-link" download>
                                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                                        <polyline points="7 10 12 15 17 10"/>
                                        <line x1="12" y1="15" x2="12" y2="3"/>
                                    </svg>
                                </a>
                            </div>
                """
            
            yield """
                        </div>
                    </div>
            """

        if kp_files_by_year:
            yield """
                    <div class="sidebar-card">
                        <div class="sidebar-title">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>
                                <line x1="16" y1="2" x2="16" y2="6"/>
                                <line x1="8" y1="2" x2="8" y2="6"/>
                                <line x1="3" y1="10" x2="21" y2="10"/>
                            </svg>
                            Календари питания
                        </div>
            """
            
            for kp_year in sorted(kp_files_by_year.keys(), reverse=True):
                if not kp_files_by_year[kp_year]:
                    continue
                    
                yield f"""
                        <div class="category-group">
                            <h4>{kp_year} год</h4>
                """
                
                for fi in sorted(kp_files_by_year[kp_year], key=lambda x: x["date"], reverse=True):
                    size_kb = fi["size"] // 1024
                    yield f"""
                            <div class="file-row">
                                <div class="file-info">
                                    <div class="file-icon">
                                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                            <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>
                                            <line x1="16" y1="2" x2="16" y2="6"/>
                                            <line x1="8" y1="2" x2="8" y2="6"/>
                                            <line x1="3" y1="10" x2="21" y2="10"/>
                                        </svg>
                                    </div>
                                    <div class="file-details">
                                        <div class="file-name">{fi["filename"]}</div>
                                        <div class="file-meta">
                                            <span class="file-size">{size_kb} KB</span>
                                            <span>{fi["date"]}</span>
                                        </div>
                                    </div>
                                </div>
                                <a href="{fi["filename"]}" class="download-link" download>
                                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                                        <polyline points="7 10 12 15 17 10"/>
                                        <line x1="12" y1="15" x2="12" y2="3"/>
                                    </svg>
                                </a>
                            </div>
                    """
                
                yield """
                        </div>
                    """
            
            yield """
                    </div>
            """

        if tm_files_by_year:
            yield """
                    <div class="sidebar-card">
                        <div class="sidebar-title">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                <path d="M4 4h16v16H4z"/>
                                <path d="M8 8h8M8 12h6M8 16h4"/>
                            </svg>
                            Типовое меню
                        </div>
            """
            
            for tm_year in sorted(tm_files_by_year.keys(), reverse=True):
                if not tm_files_by_year[tm_year]:
                    continue
                    
                yield f"""
                        <div class="category-group">
                            <h4>{tm_year} год</h4>
                """
                
                for fi in sorted(tm_files_by_year[tm_year], key=lambda x: x["date"], reverse=True):
                    size_kb = fi["size"] // 1024
                    yield f"""
                            <div class="file-row">
                                <div class="file-info">
                                    <div class="file-icon">
                                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                            <path d="M4 4h16v16H4z"/>
                                            <path d="M8 8h8M8 12h6M8 16h4"/>
                                        </svg>
                                    </div>
                                    <div class="file-details">
                                        <div class="file-name">{fi["filename"]}</div>
                                        <div class="file-meta">
                                            <span class="file-size">{size_kb} KB</span>
                                            <span>{fi["date"]}</span>
                                        </div>
                                    </div>
                                </div>
                                <a href="{fi["filename"]}" class="download-link" download>
                                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                                        <polyline points="7 10 12 15 17 10"/>
                                        <line x1="12" y1="15" x2="12" y2="3"/>
                                    </svg>
                                </a>
                            </div>
                    """
                
                yield """
                        </div>
                    """
            
            yield """
                    </div>
            """

        yield """
                    <div class="sidebar-card">
                        <div class="compliance-card">
                            <div class="compliance-icon">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                    <polyline points="20 6 9 17 4 12"></polyline>
                                </svg>
                            </div>
                            <div class="compliance-text">
                                <strong>Соответствует нормам СанПиН</strong>
                                <p>Меню разработано в соответствии с требованиями санитарных правил и норм</p>
                            </div>
                        </div>
                    </div>

                    <div class="sidebar-card">
                        <div class="link-card">
                            <a href="https://opros.cemon.ru/" target="_blank">
                                <span>Опрос родителей и обучающихся ФЦМПО</span>
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
                                    <polyline points="15 3 21 3 21 9"></polyline>
                                    <line x1="10" y1="14" x2="21" y2="3"></line>
                                </svg>
                            </a>
                        </div>
                    </div>
                </div>
            </div>
        """

    yield """
        </div>
        <!-- /dashboard-container -->

        <script>
            function toggleYear(header) {
                const content = header.nextElementSibling;
                const icon = header.querySelector('.toggle-icon');
                
                if (content.style.display === 'none') {
                    content.style.display = 'block';
                    icon.style.transform = 'rotate(0deg)';
                } else {
                    content.style.display = 'none';
                    icon.style.transform = 'rotate(-90deg)';
                }
            }
            
            // Плавная анимация появления
            document.querySelectorAll('.animate-in').forEach((el, i) => {
                el.style.animationDelay = `${i * 0.05}s`;
            });
        </script>
    </body>
    </html>
    """

# НОВЫЙ МИДЛВАР ДЛЯ КЕШИРОВАНИЯ ИЗОБРАЖЕНИЙ
@app.middleware("http")
async def cache_images_middleware(request: Request, call_next):
    """Middleware для кеширования изображений"""
    
    # НЕ кешируем статические файлы сайта
    if request.url.path.startswith('/static/'):
        response = await call_next(request)
        # Добавляем простой кеш для статики
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=3600"
        return response
    
    # Для аватаров и файлов питания применяем кеширование
    if request.url.path.startswith(('/avatar/', '/food/')):
        # Проверяем заголовки кеширования
        if_none_match = request.headers.get('if-none-match')
        cache_key = f"img_{request.url.path}"
        
        if cache_key in IMAGE_RESPONSE_CACHE:
            cached = IMAGE_RESPONSE_CACHE[cache_key]
            if if_none_match and if_none_match == cached.get('etag'):
                return Response(status_code=304)
    
    response = await call_next(request)
    
    # Кешируем ответ с изображением (только для аватаров)
    if response.status_code == 200 and request.url.path.startswith('/avatar/'):
        cache_key = f"img_{request.url.path}"
        etag = hashlib.md5(str(response.body).encode()).hexdigest()
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "public, max-age=86400"
        
        IMAGE_RESPONSE_CACHE[cache_key] = {
            'etag': etag,
            'body': response.body
        }
    
    return response

@app.get("/static/logo.jpg")
async def get_logo():
    """Отдача логотипа сайта"""
    BASE_DIR = Path(__file__).resolve().parent
    logo_path = BASE_DIR / "static" / "logo.jpg"
    
    if await run_in_threadpool(logo_path.exists):
        headers = {
            "Cache-Control": "public, max-age=86400",
            "Content-Type": "image/jpeg"
        }
        return FileResponse(logo_path, headers=headers)
    
    # Если нет JPG, пробуем PNG
    logo_png = BASE_DIR / "static" / "logo.png"
    if await run_in_threadpool(logo_png.exists):
        headers = {
            "Cache-Control": "public, max-age=86400",
            "Content-Type": "image/png"
        }
        return FileResponse(logo_png, headers=headers)
    
    raise HTTPException(status_code=404, detail="Логотип не найден")


# ОСТАВЛЯЕМ СТАРЫЙ МИДЛВАР ДЛЯ СОВМЕСТИМОСТИ
@app.middleware("http")
async def performance_middleware(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    
    if process_time > 1.0:
        print(f"⏱️ SLOW_REQUEST: {request.method} {request.url} - {process_time:.3f}s")
    
    response.headers["X-Process-Time"] = f"{process_time:.3f}s"
    return response

# --- ФЕДЕРАЛЬНЫЙ МОНИТОРИНГ ---
@app.get("/{uid}/food/", response_class=HTMLResponse)
async def federal_index(uid: int):
    BASE_DIR = Path(__file__).resolve().parent
    base_path = BASE_DIR / str(uid) / "food"

    if not await run_in_threadpool(base_path.exists):
        return HTMLResponse(content="<html><body><h1>📭 Нет доступных файлов</h1></body></html>")

    manifest_path = base_path / "manifest.json"
    manifest = await read_manifest_optimized(manifest_path)

    return StreamingResponse(
        generate_federal_html_stream(uid, base_path, manifest),
        media_type="text/html"
    )

@app.get("/{uid}/food/{filename}")
async def get_federal_file(uid: int, filename: str):
    BASE_DIR = Path(__file__).resolve().parent
    file_path = BASE_DIR / str(uid) / "food" / filename

    cache_key = str(file_path)
    async with CACHE_LOCK:
        if cache_key in FILE_EXISTS_CACHE:
            file_exists = FILE_EXISTS_CACHE[cache_key]
        else:
            file_exists = await run_in_threadpool(file_path.exists)
            FILE_EXISTS_CACHE[cache_key] = file_exists

    if file_exists:
        return FileResponse(
            file_path,
            filename=filename,
            headers={"Cache-Control": "public, max-age=3600"}
        )

    raise HTTPException(status_code=404, detail="Файл не найден")

# --- ОБНОВЛЕННЫЙ ЭНДПОИНТ ДЛЯ АВАТАРА С ОПТИМИЗАЦИЕЙ ---
@app.get("/{uid}/avatar/{filename:path}")
async def get_avatar(
    request: Request,
    uid: int, 
    filename: str,
    size: str = "medium"  # small, medium, large
):
    """Отдача оптимизированного аватара школы"""
    BASE_DIR = Path(__file__).resolve().parent
    
    # Проверяем, запрашивается ли превью
    if filename.startswith('thumbnails/'):
        avatar_path = BASE_DIR / str(uid) / "avatar" / filename
    else:
        original_path = BASE_DIR / str(uid) / "avatar" / filename
        
        if not await run_in_threadpool(original_path.exists):
            raise HTTPException(status_code=404, detail="Аватар не найден")
        
        # Проверяем заголовки кеширования
        if_modified_since = request.headers.get('if-modified-since')
        if if_modified_since:
            try:
                mod_time = datetime.strptime(if_modified_since, '%a, %d %b %Y %H:%M:%S GMT')
                file_mod_time = datetime.fromtimestamp(
                    (await run_in_threadpool(original_path.stat)).st_mtime
                )
                if file_mod_time <= mod_time:
                    return Response(status_code=304)
            except:
                pass
        
        # Получаем оптимизированную версию
        try:
            avatar_path = await get_thumbnail_path(original_path, size)
        except Exception as e:
            logger.error(f"Ошибка получения оптимизированного аватара: {e}")
            avatar_path = original_path
    
    if await run_in_threadpool(avatar_path.exists):
        # Добавляем заголовки для кеширования
        headers = {
            "Cache-Control": "public, max-age=86400",  # Кеш на сутки
            "ETag": hashlib.md5(str(avatar_path.stat().st_mtime).encode()).hexdigest(),
            "Last-Modified": datetime.fromtimestamp(
                avatar_path.stat().st_mtime
            ).strftime('%a, %d %b %Y %H:%M:%S GMT')
        }
        
        return FileResponse(
            avatar_path,
            headers=headers
        )
    
    raise HTTPException(status_code=404, detail="Аватар не найден")

# --- РЕГИСТРАЦИЯ И АВТОРИЗАЦИЯ ---
@app.get("/")
async def home():
    return RedirectResponse("/login")

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {
        "request": request,
        "districts": DISTRICTS,
        "food_types": FOOD_TYPES
    })

@app.post("/register", response_class=HTMLResponse)
async def register(
    email: str = Form(...),
    password: str = Form(...),
    unit_name: str = Form(...),
    director_name: str = Form(...),
    district: str = Form(...),
    region: str = Form(...),  # ДОБАВИТЬ ЭТО ПОЛЕ
    food_type: str = Form(...),
    secret_code: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    existing_user = await run_in_threadpool(
        lambda: db.query(models.User).filter(models.User.email == email).first()
    )
    if existing_user:
        return "Пользователь с таким email уже существует"

    role = "user"
    if secret_code == REGIONAL_CODE:
        role = "regional_admin"
    elif secret_code == MUNICIPAL_CODE:
        role = "municipal_admin"

    hashed_pw = auth.get_password_hash(password)
    new_user = models.User(
        email=email,
        hashed_password=hashed_pw,
        unit_name=unit_name,
        director_name=director_name,
        district=district,
        region=region,  # ДОБАВИТЬ ЭТО
        food_type=food_type,
        role=role
    )

    await run_in_threadpool(lambda: db.add(new_user))
    await run_in_threadpool(db.commit)
    await run_in_threadpool(db.refresh, new_user)

    BASE_DIR = Path(__file__).resolve().parent
    school_dir = BASE_DIR / str(new_user.id)
    food_dir = school_dir / "food"
    avatar_dir = school_dir / "avatar"  # Создаём папку для аватаров
    
    await run_in_threadpool(lambda: food_dir.mkdir(parents=True, exist_ok=True))
    await run_in_threadpool(lambda: avatar_dir.mkdir(parents=True, exist_ok=True))

    return RedirectResponse("/login", status_code=303)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(
    request: Request,  # Добавлен request для сохранения в сессии
    email: str = Form(...), 
    password: str = Form(...), 
    db: Session = Depends(get_db)
):
    user = await run_in_threadpool(
        lambda: db.query(models.User).filter(models.User.email == email).first()
    )
    
    if not user or not auth.verify_password(password, user.hashed_password):
        return "Неверный логин или пароль"
    
    # Сохраняем в сессии для библиотеки знаний
    request.session["user_email"] = user.email
    request.session["user_id"] = user.id
    request.session["user_name"] = user.unit_name

    if "admin" in user.role:
        return RedirectResponse(f"/admin?admin_id={user.id}", status_code=303)
    return RedirectResponse(f"/dashboard?uid={user.id}", status_code=303)

# --- АДМИН-ПАНЕЛЬ ---
@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(
    request: Request,
    admin_id: int,
    q: str = "",
    page: int = 1,
    per_page: int = 60,
    db: Session = Depends(get_db)
):
    admin = await get_cached_user(admin_id, db)
    if not admin:
        return RedirectResponse("/login")

    query = db.query(models.User).filter(models.User.role == "user")
    # Фильтрация по региону для регионального админа
    if admin.role == "regional_admin":
        query = query.filter(models.User.region == admin.region)
    # Фильтрация по району для муниципального админа
    elif admin.role == "municipal_admin":
        query = query.filter(models.User.district == admin.district)
    
    # ДОБАВЛЯЕМ ПОИСК ПО НАЗВАНИЮ
    if q:
        query = query.filter(models.User.unit_name.ilike(f"%{q}%"))



    total_count = await run_in_threadpool(query.count)
    offset = (page - 1) * per_page
    schools = await run_in_threadpool(
        lambda: query.offset(offset).limit(per_page).all()
    )

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "admin": admin,
        "schools": schools,
        "total_count": total_count,
        "current_page": page,
        "per_page": per_page,
        "search_query": q,
        "food_types": FOOD_TYPES,
        "months": MONTHS,
    })

# --- МАССОВЫЕ ДЕЙСТВИЯ ---
@app.post("/bulk-upload")
async def bulk_upload(
    request: Request,
    admin_id: int = Form(...),
    target_type: str = Form(...),
    year: str = Form(...),
    month: str = Form(...),
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db)
):
    BASE_DIR = Path(__file__).resolve().parent
    admin = await get_cached_user(admin_id, db)
    if not admin:
        return RedirectResponse("/login")

    query = db.query(models.User).filter(
        models.User.food_type == target_type,
        models.User.role == "user"
    )
    if admin.role == "municipal_admin":
        query = query.filter(models.User.district == admin.district)

    schools = await run_in_threadpool(query.all)

    uploader_name = admin.unit_name if admin else f"ADMIN {admin_id}"
    uploader_ip = request.client.host if request.client else "—"
    
    current_time = get_msk_time()

    temp_uploads = BASE_DIR / "temp_uploads"
    await asyncio.to_thread(lambda: temp_uploads.mkdir(parents=True, exist_ok=True))

    original_paths = {}
    for file in files:
        if not file.filename:
            continue
        orig_path = temp_uploads / file.filename
        await save_uploaded_file_optimized(file, orig_path)
        original_paths[file.filename] = orig_path

    for school in schools:
        food_path = BASE_DIR / str(school.id) / "food"
        await run_in_threadpool(lambda: food_path.mkdir(parents=True, exist_ok=True))
        manifest_path = food_path / "manifest.json"

        manifest = await read_manifest_optimized(manifest_path)

        for file in files:
            if not file.filename:
                continue

            orig_path = original_paths[file.filename]
            dest_path = food_path / file.filename

            await asyncio.to_thread(lambda: shutil.copy2(orig_path, dest_path))

            # ========== ИСПРАВЛЕНИЕ: Сохраняем метаданные с правильными годом и месяцем ==========
            manifest[file.filename] = {
                "assigned_year": year,           # Год из формы
                "assigned_month": month,         # Месяц из формы
                "uploader_name": uploader_name,
                "uploader_ip": uploader_ip,
                "upload_datetime": current_time.strftime("%d.%m.%Y %H:%M")
            }

            # Обновляем содержимое Excel файла
            date_str = f"{year}-{month}-01"
            await update_excel_content(
                dest_path,
                school.unit_name,
                school.director_name,
                year,
                date_str
            )

        await write_manifest_optimized(manifest_path, manifest)

    try:
        await asyncio.to_thread(lambda: shutil.rmtree(temp_uploads))
    except:
        pass

    return RedirectResponse(f"/admin?admin_id={admin_id}", status_code=303)

@app.post("/admin/bulk-delete-files")
async def bulk_delete_files(
    request: Request,
    admin_id: int = Form(...),
    school_ids: List[int] = Form(...),
    delete_all: bool = Form(False),
    keep_exceptions: bool = Form(False),
    only_kp: bool = Form(False),
    only_tm_sm: bool = Form(False),
    only_findex: bool = Form(False),
    db: Session = Depends(get_db)
):
    BASE_DIR = Path(__file__).resolve().parent
    admin = await get_cached_user(admin_id, db)
    if not admin:
        return RedirectResponse("/login", status_code=303)

    # Получаем школы по ID с учетом прав админа
    schools_query = db.query(models.User).filter(
        models.User.id.in_(school_ids),
        models.User.role == "user"
    )
    if admin.role == "municipal_admin":
        schools_query = schools_query.filter(models.User.district == admin.district)
    
    schools = await run_in_threadpool(schools_query.all)

    deleted_count = 0
    errors = []
    deleted_files_list = []

    for school in schools:
        food_path = BASE_DIR / str(school.id) / "food"
        manifest_path = food_path / "manifest.json"

        if not await run_in_threadpool(food_path.exists):
            continue

        # Получаем все файлы в директории
        try:
            all_files = await list_directory_files_optimized(food_path)
        except Exception as e:
            errors.append(f"Ошибка при чтении папки школы {school.unit_name}: {str(e)}")
            continue

        # Загружаем манифест для метаданных
        manifest = await read_manifest_optimized(manifest_path)
        
        files_to_delete = []

        for file_path in all_files:
            filename = file_path.name
            
            # Пропускаем manifest.json
            if filename == "manifest.json":
                continue

            should_delete = False

            # Определяем, нужно ли удалять файл
            if delete_all:
                # Удаляем все файлы
                if keep_exceptions:
                    # Кроме исключений
                    if filename == "findex.xlsx":
                        continue
                    if re.match(r"^tm\d{4}-sm\.xlsx$", filename):
                        continue
                    if re.match(r"^kp\d{4}\.xlsx$", filename):
                        continue
                should_delete = True

            elif only_tm_sm:
                # Только tm-файлы
                if re.match(r"^tm\d{4}-sm\.xlsx$", filename):
                    should_delete = True

            elif only_findex:
                # Только findex.xlsx
                if filename == "findex.xlsx":
                    should_delete = True

            elif only_kp:
                # Только kp-файлы
                if re.match(r"^kp\d{4}\.xlsx$", filename):
                    should_delete = True

            elif keep_exceptions and not any([delete_all, only_tm_sm, only_findex, only_kp]):
                # Удаляем всё кроме исключений
                if filename not in ["findex.xlsx"] and \
                   not re.match(r"^tm\d{4}-sm\.xlsx$", filename) and \
                   not re.match(r"^kp\d{4}\.xlsx$", filename):
                    should_delete = True

            if should_delete:
                files_to_delete.append(file_path)

        # Удаляем файлы
        for file_path in files_to_delete:
            try:
                # Удаляем физический файл
                await delete_file_optimized(file_path)
                
                # Удаляем запись из манифеста
                if file_path.name in manifest:
                    manifest.pop(file_path.name)
                
                deleted_count += 1
                deleted_files_list.append(f"{school.unit_name}: {file_path.name}")
                
            except Exception as e:
                errors.append(f"Ошибка при удалении {file_path.name} у {school.unit_name}: {str(e)}")

        # Сохраняем обновленный манифест
        if files_to_delete:
            await write_manifest_optimized(manifest_path, manifest)

    # Формируем сообщение о результате
    if deleted_count > 0:
        msg = f"✅ Успешно удалено {deleted_count} файлов"
        if deleted_files_list:
            # Показываем первые 5 удаленных файлов
            sample = deleted_files_list[:5]
            msg += f": {', '.join(sample)}"
            if len(deleted_files_list) > 5:
                msg += f" и ещё {len(deleted_files_list) - 5}"
    else:
        msg = "ℹ️ Файлы для удаления не найдены"
    
    if errors:
        msg += f". ⚠️ Ошибки: {'; '.join(errors[:3])}"
        if len(errors) > 3:
            msg += f" и ещё {len(errors) - 3} ошибок"

    return RedirectResponse(
        f"/admin?admin_id={admin_id}&message={msg}",
        status_code=303
    )

# НОВЫЙ ЭНДПОИНТ: Массовое удаление файлов по месяцам
@app.post("/admin/bulk-delete-files-by-month")
async def bulk_delete_files_by_month(
    request: Request,
    admin_id: int = Form(...),
    school_ids: List[int] = Form(...),
    months: List[str] = Form(...),
    year: str = Form(...),
    delete_all_months: bool = Form(False),
    db: Session = Depends(get_db)
):
    BASE_DIR = Path(__file__).resolve().parent
    admin = await get_cached_user(admin_id, db)
    if not admin:
        return RedirectResponse("/login", status_code=303)

    # Получаем школы по ID с учетом прав админа
    schools_query = db.query(models.User).filter(
        models.User.id.in_(school_ids),
        models.User.role == "user"
    )
    if admin.role == "municipal_admin":
        schools_query = schools_query.filter(models.User.district == admin.district)
    
    schools = await run_in_threadpool(schools_query.all)

    deleted_count = 0
    errors = []
    deleted_files_list = []

    # Если выбран "Все месяцы", очищаем список и будем удалять все месяцы
    selected_months = None if delete_all_months else months

    for school in schools:
        food_path = BASE_DIR / str(school.id) / "food"
        manifest_path = food_path / "manifest.json"

        if not await run_in_threadpool(food_path.exists):
            continue

        # Получаем все файлы в директории
        try:
            all_files = await list_directory_files_optimized(food_path)
        except Exception as e:
            errors.append(f"Ошибка при чтении папки школы {school.unit_name}: {str(e)}")
            continue

        # Загружаем манифест для метаданных
        manifest = await read_manifest_optimized(manifest_path)
        
        files_to_delete = []

        for file_path in all_files:
            filename = file_path.name
            
            # Пропускаем manifest.json и специальные файлы
            if filename == "manifest.json" or \
               filename == "findex.xlsx" or \
               re.match(r"^tm\d{4}-sm\.xlsx$", filename) or \
               re.match(r"^kp\d{4}\.xlsx$", filename):
                continue

            # Получаем метаданные файла из манифеста
            file_meta = manifest.get(filename, {})
            file_year = file_meta.get("assigned_year")
            file_month = file_meta.get("assigned_month")

            # Если в манифесте нет данных, пробуем извлечь из имени файла
            if not file_year or not file_month:
                date_match = re.search(r'(\d{4})-(\d{2})-(\d{2})', filename)
                if date_match:
                    file_year, file_month, _ = date_match.groups()

            # Проверяем, соответствует ли файл критериям удаления
            if file_year == year:
                if delete_all_months or (file_month and file_month in selected_months):
                    files_to_delete.append(file_path)

        # Удаляем файлы
        for file_path in files_to_delete:
            try:
                # Удаляем физический файл
                await delete_file_optimized(file_path)
                
                # Удаляем запись из манифеста
                if file_path.name in manifest:
                    manifest.pop(file_path.name)
                
                deleted_count += 1
                deleted_files_list.append(f"{school.unit_name}: {file_path.name}")
                
            except Exception as e:
                errors.append(f"Ошибка при удалении {file_path.name} у {school.unit_name}: {str(e)}")

        # Сохраняем обновленный манифест
        if files_to_delete:
            await write_manifest_optimized(manifest_path, manifest)

    # Формируем сообщение о результате
    if deleted_count > 0:
        if delete_all_months:
            period = f"за ВСЕ месяцы {year} года"
        else:
            month_names = [MONTHS.get(m, m) for m in selected_months]
            period = f"за {', '.join(month_names)} {year} года"
        
        msg = f"✅ Успешно удалено {deleted_count} файлов {period}"
        if deleted_files_list:
            # Показываем первые 5 удаленных файлов
            sample = deleted_files_list[:5]
            msg += f": {', '.join(sample)}"
            if len(deleted_files_list) > 5:
                msg += f" и ещё {len(deleted_files_list) - 5}"
    else:
        msg = "ℹ️ Файлы для удаления не найдены"
    
    if errors:
        msg += f". ⚠️ Ошибки: {'; '.join(errors[:3])}"
        if len(errors) > 3:
            msg += f" и ещё {len(errors) - 3} ошибок"

    return RedirectResponse(
        f"/admin?admin_id={admin_id}&message={msg}",
        status_code=303
    )

# --- ЛИЧНЫЙ КАБИНЕТ ШКОЛЫ (ОБНОВЛЁННЫЙ) ---
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    uid: int,
    year: str = None,      # Изменяем на None, чтобы можно было не передавать
    month: str = None,     # Изменяем на None
    db: Session = Depends(get_db)
):
    user = await get_cached_user(uid, db)
    if not user:
        return RedirectResponse("/login")

    BASE_DIR = Path(__file__).resolve().parent
    food_path = BASE_DIR / str(uid) / "food"
    profile_path = BASE_DIR / str(uid) / "profile.json"
    
    # Проверка существования папки
    if not await run_in_threadpool(food_path.exists):
        await run_in_threadpool(lambda: food_path.mkdir(parents=True, exist_ok=True))
        manifest_path = food_path / "manifest.json"
        await write_manifest_optimized(manifest_path, {})
        print(f"📁 Создана папка для пользователя {uid} при входе в дашборд")

    # Загружаем данные профиля
    profile_data = {}
    if await run_in_threadpool(profile_path.exists):
        try:
            async with aiofiles.open(profile_path, "r", encoding="utf-8") as f:
                content = await f.read()
                profile_data = json.loads(content) if content else {}
        except Exception as e:
            logger.error(f"Ошибка загрузки profile.json для uid {uid}: {e}")

    manifest_path = food_path / "manifest.json"
    manifest = await read_manifest_optimized(manifest_path)

    async with CACHE_LOCK:
        MANIFEST_CACHE[str(manifest_path)] = manifest.copy()

    files = await list_directory_files_optimized(food_path)
    
    # ========== ИСПРАВЛЕНИЕ: Группируем файлы по годам и месяцам из manifest ==========
    grouped_files = {}
    
    for f in files:
        if f.name == "manifest.json":
            continue

        file_meta = manifest.get(f.name, {})
        
        # Получаем год и месяц из метаданных
        assigned_year = file_meta.get("assigned_year")
        assigned_month = file_meta.get("assigned_month")
        
        # Если в манифесте нет данных, пробуем извлечь из имени файла
        if not assigned_year or not assigned_month:
            date_match = re.search(r'(\d{4})-(\d{2})', f.name)
            if date_match:
                assigned_year = date_match.group(1)
                assigned_month = date_match.group(2)
            else:
                # Если не удалось определить, используем текущую дату
                assigned_year = str(get_msk_time().year)
                assigned_month = get_msk_time().strftime("%m")
        
        # Получаем имя месяца для отображения
        month_name = MONTHS.get(assigned_month, assigned_month)
        
        upload_time = file_meta.get("upload_datetime", get_msk_time().strftime("%d.%m.%Y %H:%M"))
        uploader_name = file_meta.get("uploader_name", user.unit_name)
        uploader_ip = file_meta.get("uploader_ip", "—")
        
        # Добавляем файл в группу
        grouped_files.setdefault(assigned_year, {}).setdefault(month_name, []).append({
            "filename": f.name,
            "date": upload_time,
            "uploader": uploader_name,
            "ip": uploader_ip,
        })

    # Если year и month не переданы, выбираем последний доступный год и месяц
    if not year or not month:
        # Находим последний год
        if grouped_files:
            latest_year = max(grouped_files.keys())
            # Находим последний месяц в этом году
            if grouped_files[latest_year]:
                latest_month = max(grouped_files[latest_year].keys())
                # Конвертируем название месяца обратно в номер
                for num, name in MONTHS.items():
                    if name == latest_month:
                        month = num
                        break
                year = latest_year
            else:
                year = "2025"
                month = "05"
        else:
            year = "2025"
            month = "05"

    monitoring_url = f"{request.base_url}{uid}/food/"

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "profile": profile_data,
        "files_grouped": grouped_files,
        "period": f"{year}-{month}",
        "year": year,
        "month": month,
        "months": MONTHS,
        "monitoring_url": monitoring_url
    })

# --- ОБНОВЛЕННАЯ ЗАГРУЗКА АВАТАРА С ОПТИМИЗАЦИЕЙ ---
@app.post("/profile/upload-avatar")
async def upload_avatar(
    uid: int = Form(...),
    avatar: UploadFile = File(...)
):
    BASE_DIR = Path(__file__).resolve().parent
    school_dir = BASE_DIR / str(uid)
    avatar_dir = school_dir / "avatar"
    profile_path = school_dir / "profile.json"
    
    # Создаём папки
    await run_in_threadpool(lambda: avatar_dir.mkdir(parents=True, exist_ok=True))
    
    # Удаляем старый аватар и его превью
    try:
        old_files = await run_in_threadpool(lambda: list(avatar_dir.glob("*")))
        old_files.extend(await run_in_threadpool(lambda: list(avatar_dir.glob("thumbnails/*"))))
        for old_file in old_files:
            await run_in_threadpool(old_file.unlink)
    except Exception as e:
        logger.error(f"Ошибка при удалении старого аватара: {e}")
    
    # Проверяем тип файла
    file_ext = Path(avatar.filename).suffix.lower()
    allowed_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp']
    
    if file_ext not in allowed_extensions:
        file_ext = '.jpg'  # По умолчанию
    
    # Сохраняем временный файл
    temp_path = avatar_dir / f"temp_{int(time.time())}{file_ext}"
    content = await avatar.read()
    
    async with aiofiles.open(temp_path, "wb") as f:
        await f.write(content)
    
    try:
        # Оптимизируем основное изображение
        avatar_filename = f"avatar{file_ext}"
        avatar_path = avatar_dir / avatar_filename
        
        result = await optimize_image_async(
            temp_path,
            output_path=avatar_path,
            max_size=MAX_IMAGE_SIZE,
            quality=JPEG_QUALITY
        )
        
        if result and result['saved_percent'] > 0:
            logger.info(f"Аватар оптимизирован: сэкономлено {result['saved_percent']:.1f}%")
        
        # Создаем превью разных размеров
        for size_name in THUMBNAIL_SIZES.keys():
            await get_thumbnail_path(avatar_path, size_name)
        
        # Удаляем временный файл
        await delete_file_optimized(temp_path)
        
    except Exception as e:
        logger.error(f"Ошибка при оптимизации аватара: {e}")
        # Если оптимизация не удалась, используем оригинал
        if temp_path.exists():
            avatar_path = avatar_dir / f"avatar{file_ext}"
            await asyncio.to_thread(shutil.move, str(temp_path), str(avatar_path))
    
    # Обновляем profile.json
    profile_data = {}
    if await run_in_threadpool(profile_path.exists):
        try:
            async with aiofiles.open(profile_path, "r", encoding="utf-8") as f:
                content = await f.read()
                profile_data = json.loads(content) if content else {}
        except Exception:
            profile_data = {}
    
    profile_data["avatar"] = f"avatar{file_ext}"
    
    async with aiofiles.open(profile_path, "w", encoding="utf-8") as f:
        await f.write(json.dumps(profile_data, ensure_ascii=False, indent=2))
    
    # Очищаем кеш для этого аватара
    cache_key = f"img_/{uid}/avatar/avatar{file_ext}"
    if cache_key in IMAGE_RESPONSE_CACHE:
        del IMAGE_RESPONSE_CACHE[cache_key]
    
    return RedirectResponse(f"/dashboard?uid={uid}", status_code=303)

# --- ОБНОВЛЕНИЕ ССЫЛОК ---
@app.post("/profile/update-links")
async def update_links(
    uid: int = Form(...),
    website_url: str = Form(""),
    hot_meal_url: str = Form("")
):
    BASE_DIR = Path(__file__).resolve().parent
    school_dir = BASE_DIR / str(uid)
    profile_path = school_dir / "profile.json"
    
    profile_data = {}
    if await run_in_threadpool(profile_path.exists):
        try:
            async with aiofiles.open(profile_path, "r", encoding="utf-8") as f:
                content = await f.read()
                profile_data = json.loads(content) if content else {}
        except Exception:
            profile_data = {}
    
    if website_url:
        profile_data["website_url"] = website_url
    if hot_meal_url:
        profile_data["hot_meal_url"] = hot_meal_url
    
    async with aiofiles.open(profile_path, "w", encoding="utf-8") as f:
        await f.write(json.dumps(profile_data, ensure_ascii=False, indent=2))
    
    return RedirectResponse(f"/dashboard?uid={uid}", status_code=303)

# --- ОСТАЛЬНЫЕ ЭНДПОИНТЫ ---
@app.post("/upload")
async def upload_files(
    request: Request,
    uid: int = Form(...),
    year: str = Form(...),
    month: str = Form(...),
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db)
):
    BASE_DIR = Path(__file__).resolve().parent
    food_path = BASE_DIR / str(uid) / "food"
    await run_in_threadpool(lambda: food_path.mkdir(parents=True, exist_ok=True))

    manifest_path = food_path / "manifest.json"
    manifest = await read_manifest_optimized(manifest_path)

    user = await get_cached_user(uid, db)
    uploader_name = user.unit_name if user else f"UID {uid}"
    client_ip = request.client.host if request.client else "—"

    # Получаем текущую дату для загрузки
    current_time = get_msk_time()
    
    for file in files:
        if not file.filename:
            continue
        
        # Проверяем, не существует ли уже файл с таким именем
        dest_path = food_path / file.filename
        
        # ========== ИСПРАВЛЕНИЕ: Сохраняем метаданные с правильными годом и месяцем ==========
        manifest[file.filename] = {
            "assigned_year": year,           # Год из формы
            "assigned_month": month,         # Месяц из формы
            "uploader_name": uploader_name,
            "uploader_ip": client_ip,
            "upload_datetime": current_time.strftime("%d.%m.%Y %H:%M")
        }
        
        # Сохраняем файл
        await save_uploaded_file_optimized(file, dest_path)
        
        # Обновляем содержимое Excel файла (если нужно)
        await update_excel_content(
            dest_path,
            user.unit_name,
            user.director_name,
            year,
            f"{year}-{month}-01"  # Дата в формате YYYY-MM-DD
        )

    await write_manifest_optimized(manifest_path, manifest)

    return RedirectResponse(f"/dashboard?uid={uid}&year={year}&month={month}", status_code=303)

@app.get("/delete-file")
async def delete_file(uid: int, year: str, month: str, filename: str):
    BASE_DIR = Path(__file__).resolve().parent
    food_path = BASE_DIR / str(uid) / "food"
    file_path = food_path / filename
    manifest_path = food_path / "manifest.json"

    await delete_file_optimized(file_path)

    manifest = await read_manifest_optimized(manifest_path)
    if filename in manifest:
        del manifest[filename]
        await write_manifest_optimized(manifest_path, manifest)

    # Перенаправляем обратно с сохранением года и месяца
    return RedirectResponse(
        f"/dashboard?uid={uid}&year={year}&month={month}",
        status_code=303
    )

@app.post("/delete-files")
async def delete_files(
    uid: int = Form(...),
    year: str = Form(...),
    month: str = Form(...),
    files: List[str] = Form(...)
):
    BASE_DIR = Path(__file__).resolve().parent
    folder = BASE_DIR / str(uid) / "food"
    manifest_path = folder / "manifest.json"

    manifest = await read_manifest_optimized(manifest_path)

    for filename in files:
        file_path = folder / filename
        await delete_file_optimized(file_path)
        manifest.pop(filename, None)

    await write_manifest_optimized(manifest_path, manifest)

    return RedirectResponse(
        f"/dashboard?uid={uid}&year={year}&month={month}",
        status_code=303
    )

@app.post("/profile/update")
async def update_profile(
    uid: int = Form(...),
    director_name: str = Form(""),
    unit_name: str | None = Form(None),
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(models.User.id == uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    if director_name != "":
        user.director_name = director_name

    if unit_name is not None and unit_name.strip() != "":
        user.unit_name = unit_name.strip()

    db.commit()
    db.refresh(user)

    return RedirectResponse(f"/dashboard?uid={uid}", status_code=303)

# --- СБРОС ПАРОЛЯ ---
def is_valid_email(email: str) -> bool:
    if not email or '@' not in email:
        return False
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return re.match(pattern, email) is not None

def get_smtp_config(email: str) -> dict:
    domain = email.lower().split('@')[-1]

    providers = {
        'gmail.com': {
            'hostname': 'smtp.gmail.com',
            'port': 587,
            'use_tls': False,
            'start_tls': True
        },
        'yandex.ru': {
            'hostname': 'smtp.yandex.ru',
            'port': 465,
            'use_tls': True,
            'start_tls': False
        },
        'mail.ru': {
            'hostname': 'smtp.mail.ru',
            'port': 465,
            'use_tls': True,
            'start_tls': False
        },
        'yahoo.com': {
            'hostname': 'smtp.mail.yahoo.com',
            'port': 587,
            'use_tls': False,
            'start_tls': True
        }
    }

    if domain in providers:
        return providers[domain]

    return {
        'hostname': f'smtp.{domain}',
        'port': 587,
        'use_tls': False,
        'start_tls': True
    }

async def send_reset_email(email: str, token: str):
    try:
        if not is_valid_email(email):
            raise ValueError("Некорректный email")

        reset_url = f"https://monitoring95.ru/reset-password/{token}"
        safe_email = email.replace('<', '&lt;').replace('>', '&gt;')

        message = MIMEText(f"""
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Сброс пароля</title>
    <style>
        body {{
            margin: 0;
            padding: 0;
            font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .container {{
            max-width: 480px;
            margin: 20px;
            background: white;
            border-radius: 24px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 40px 30px;
            text-align: center;
        }}
        .header h1 {{
            margin: 0;
            color: white;
            font-size: 28px;
            font-weight: 600;
            text-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .content {{
            padding: 40px 30px;
            background: white;
        }}
        .content h2 {{
            color: #333;
            font-size: 24px;
            margin: 0 0 20px 0;
            font-weight: 600;
        }}
        .content p {{
            color: #666;
            font-size: 16px;
            line-height: 1.6;
            margin: 0 0 20px 0;
        }}
        .email-info {{
            background: #f8f9fa;
            border-radius: 12px;
            padding: 15px;
            margin: 25px 0;
            border-left: 4px solid #667eea;
        }}
        .email-info p {{
            margin: 5px 0;
            color: #555;
        }}
        .email-info strong {{
            color: #333;
            font-weight: 600;
        }}
        .button {{
            display: inline-block;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white !important;
            text-decoration: none;
            padding: 16px 32px;
            border-radius: 50px;
            font-weight: 600;
            font-size: 16px;
            margin: 20px 0 10px;
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
            transition: all 0.3s ease;
        }}
        .button:hover {{
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(102, 126, 234, 0.5);
        }}
        .footer {{
            text-align: center;
            padding: 30px;
            background: #f8f9fa;
            border-top: 1px solid #eee;
        }}
        .footer p {{
            color: #999;
            font-size: 14px;
            margin: 5px 0;
        }}
        .footer a {{
            color: #667eea;
            text-decoration: none;
        }}
        .divider {{
            height: 2px;
            background: linear-gradient(90deg, transparent, #667eea, transparent);
            margin: 30px 0 20px;
        }}
        .warning {{
            color: #e74c3c !important;
            font-size: 14px !important;
            display: flex;
            align-items: center;
            gap: 8px;
            justify-content: center;
        }}
        @media only screen and (max-width: 480px) {{
            .container {{
                margin: 10px;
                border-radius: 16px;
            }}
            .header {{
                padding: 30px 20px;
            }}
            .content {{
                padding: 30px 20px;
            }}
            .button {{
                display: block;
                text-align: center;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🔐 Сброс пароля</h1>
        </div>
        
        <div class="content">
            <h2>Здравствуйте!</h2>
            
            <p>Мы получили запрос на сброс пароля для вашей учетной записи. Для создания нового пароля нажмите на кнопку ниже:</p>
            
            <div style="text-align: center;">
                <a href="{reset_url}" class="button">🔑 Сменить пароль</a>
            </div>
            
            <div class="divider"></div>
            
            <div class="email-info">
                <p><strong>📧 Email:</strong> {safe_email}</p>
                <p><strong>⏰ Срок действия:</strong> 1 час</p>
                <p><strong>🆔 Запрос создан:</strong> {datetime.now().strftime('%d.%m.%Y %H:%M')}</p>
            </div>
            
            <p class="warning">
                ⚠️ Если вы не запрашивали сброс пароля, просто проигнорируйте это письмо.
            </p>
            
            <p style="font-size: 14px; color: #999; text-align: center; margin-top: 30px;">
                Никогда не пересылайте это письмо и не сообщайте код никому.<br>
                Служба поддержки никогда не запрашивает пароли.
            </p>
        </div>
        
        <div class="footer">
            <p>© 2026 ЕЦМП Мониторинг питания. Все права защищены.</p>
            <p>
                <a href="https://monitoring95.ru/privacy.html">Политика конфиденциальности</a> • 
                <a href="https://monitoring95.ru/agree.html">Пользовательское соглашение</a>
            </p>
            <p style="font-size: 12px; margin-top: 15px;">
                Это автоматическое письмо, пожалуйста, не отвечайте на него.
            </p>
        </div>
    </div>
</body>
</html>
""", "html", "utf-8")
        message["Subject"] = "Сброс пароля"
        message["From"] = os.getenv("SMTP_USERNAME")
        message["To"] = email

        config = get_smtp_config(email)

        smtp = aiosmtplib.SMTP(
            hostname=config['hostname'],
            port=config['port'],
            username=os.getenv("SMTP_USERNAME"),
            password=os.getenv("SMTP_PASSWORD"),
            use_tls=config['use_tls'],
            start_tls=config['start_tls']
        )

        await smtp.connect()
        await smtp.send_message(message)

    except aiosmtplib.SMTPAuthenticationError as e:
        raise HTTPException(
            status_code=500,
            detail="Неверный логин или пароль SMTP. Проверьте настройки."
        )
    except aiosmtplib.SMTPServerDisconnected as e:
        raise HTTPException(
            status_code=500,
            detail="Сервер SMTP недоступен. Попробуйте позже."
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось отправить письмо: {str(e)}"
        )

@app.get("/reset-password-request", response_class=HTMLResponse)
async def reset_password_request_page(request: Request):
    return templates.TemplateResponse("reset_password_request.html", {"request": request})

@app.post("/reset-password-request")
async def reset_password_request(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db)
):
    if not is_valid_email(email):
        return templates.TemplateResponse(
            "reset_password_request.html",
            {"request": request, "error": "Некорректный email"}
        )

    user = await run_in_threadpool(
        lambda: db.query(models.User).filter(models.User.email == email).first()
    )
    
    if not user:
        return templates.TemplateResponse(
            "reset_password_request.html",
            {"request": request, "error": "Пользователь с таким email не найден"}
        )

    SECRET_KEY = os.getenv("SECRET_KEY", "your-super-secret-key-change-in-production")
    token = jwt.encode(
        {"sub": email, "exp": datetime.utcnow() + timedelta(hours=1)},
        SECRET_KEY,
        algorithm="HS256"
    )

    try:
        await send_reset_email(email, token)
    except HTTPException as e:
        return templates.TemplateResponse(
            "reset_password_request.html",
            {"request": request, "error": e.detail}
        )

    return templates.TemplateResponse(
        "reset_password_request.html",
        {"request": request, "success": "Письмо для сброса пароля отправлено!"}
    )

@app.get("/reset-password/{token}", response_class=HTMLResponse)
async def reset_password_page(
    request: Request,
    token: str
):
    SECRET_KEY = os.getenv("SECRET_KEY", "your-super-secret-key-change-in-production")

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        email = payload.get("sub")
        if not email:
            return HTMLResponse("<h2>Недействительная ссылка</h2>")
    except JWTError:
        return HTMLResponse("<h2>Недействительный или просроченный токен</h2>")

    return templates.TemplateResponse(
        "reset_password.html",
        {"request": request, "token": token, "email": email}
    )

@app.post("/reset-password")
async def reset_password(
    token: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    SECRET_KEY = os.getenv("SECRET_KEY", "your-super-secret-key-change-in-production")

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        email = payload.get("sub")
        if not email:
            raise HTTPException(status_code=400, detail="Недействительный токен")
    except JWTError:
        raise HTTPException(status_code=400, detail="Просроченный токен")

    user = await run_in_threadpool(
        lambda: db.query(models.User).filter(models.User.email == email).first()
    )
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    if len(password) < 6:
        raise HTTPException(
            status_code=400,
            detail="Пароль должен быть не менее 6 символов"
        )

    hashed_pw = auth.get_password_hash(password)
    user.hashed_password = hashed_pw

    try:
        await run_in_threadpool(db.commit)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail="Не удалось сохранить новый пароль. Попробуйте снова."
        )

    return RedirectResponse("/login", status_code=303)

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": get_msk_time().isoformat(),
        "cache_stats": {
            "manifest_cache": len(MANIFEST_CACHE),
            "user_cache": len(USER_CACHE),
            "file_exists_cache": len(FILE_EXISTS_CACHE),
            "image_cache": len(IMAGE_RESPONSE_CACHE)
        }
    }

# --- НОВЫЙ ЭНДПОИНТ ДЛЯ СТАТИСТИКИ ПРОИЗВОДИТЕЛЬНОСТИ ---
@app.get("/performance-stats")
async def get_performance_stats(request: Request):
    """Статистика производительности (только для админов)"""
    
    if not request.session.get("dashboard_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    return {
        "cache_stats": {
            "manifest_cache": len(MANIFEST_CACHE),
            "user_cache": len(USER_CACHE),
            "file_exists_cache": len(FILE_EXISTS_CACHE),
            "image_cache": len(IMAGE_RESPONSE_CACHE)
        },
        "thread_pools": {
            "io_executor": IO_EXECUTOR._max_workers,
            "image_executor": IMAGE_EXECUTOR._max_workers
        }
    }

# --- ДАШБОРДЫ ---
@app.get("/dashboards")
async def dashboards_list(request: Request, db: Session = Depends(get_db)):
    """Список всех дашбордов"""
    try:
        # Для админа показываем все дашборды, для обычных пользователей только опубликованные
        is_admin = request.session.get("dashboard_admin", False)
        
        if is_admin:
            dashboards = await run_in_threadpool(
                lambda: db.query(models.Dashboard).order_by(models.Dashboard.updated_at.desc()).all()
            )
        else:
            dashboards = await run_in_threadpool(
                lambda: db.query(models.Dashboard).filter(models.Dashboard.is_published == True).order_by(models.Dashboard.updated_at.desc()).all()
            )
        
        # Загружаем элементы для каждого дашборда
        for dashboard in dashboards:
            elements = await run_in_threadpool(
                lambda: db.query(models.DashboardElement).filter(
                    models.DashboardElement.dashboard_id == dashboard.id
                ).all()
            )
            dashboard.elements = elements
        
        return templates.TemplateResponse("dashboards_list.html", {
            "request": request,
            "dashboards": dashboards,
            "session": request.session
        })
    except Exception as e:
        print(f"Ошибка в dashboards_list: {e}")
        return templates.TemplateResponse("dashboards_list.html", {
            "request": request,
            "dashboards": [],
            "session": request.session
        })
    
@app.get("/dashboard-admin/login", response_class=HTMLResponse)
async def dashboard_login_page(request: Request):
    """Страница входа в админку дашбордов"""
    return templates.TemplateResponse("dashboard_login.html", {"request": request})

@app.post("/dashboard-admin/login")
async def dashboard_login(request: Request, access_code: str = Form(...)):
    """Вход в админку дашбордов"""
    if access_code == DASHBOARD_ADMIN_CODE:
        request.session["dashboard_admin"] = True
        return RedirectResponse("/dashboards", status_code=303)
    
    return templates.TemplateResponse("dashboard_login.html", {
        "request": request,
        "error": "Неверный код доступа"
    })

@app.get("/dashboard-admin/logout")
async def dashboard_logout(request: Request):
    """Выход из админки дашбордов"""
    request.session.pop("dashboard_admin", None)
    return RedirectResponse("/dashboards", status_code=303)

@app.get("/dashboard-admin/create")
async def create_dashboard_page(request: Request, db: Session = Depends(get_db)):
    """Страница создания нового дашборда"""
    if not request.session.get("dashboard_admin"):
        return RedirectResponse("/dashboard-admin/login", status_code=303)
    
    return templates.TemplateResponse("dashboard_editor.html", {
        "request": request,
        "dashboard": None
    })

@app.post("/dashboard-admin/save")
async def save_dashboard(request: Request, db: Session = Depends(get_db)):
    """Сохранение дашборда (автоматически публикуется)"""
    if not request.session.get("dashboard_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    try:
        data = await request.json()
        print(f"Сохраняем дашборд: {data.get('title')}")
        
        # Генерация slug из названия
        if data.get('id'):
            dashboard = await run_in_threadpool(
                lambda: db.query(models.Dashboard).filter(models.Dashboard.id == data['id']).first()
            )
            if not dashboard:
                raise HTTPException(status_code=404, detail="Дашборд не найден")
            
            dashboard.title = data['title']
            dashboard.description = data.get('description', '')
            dashboard.updated_at = datetime.utcnow()
            dashboard.layout_data = json.dumps(data.get('layout', {}))
            dashboard.is_published = True  # Автоматически публикуем при сохранении
            
            # Удаляем старые элементы
            await run_in_threadpool(
                lambda: db.query(models.DashboardElement).filter(models.DashboardElement.dashboard_id == dashboard.id).delete()
            )
        else:
            # Создаем новый дашборд
            slug_base = data['title'].lower().replace(' ', '-')
            # Удаляем специальные символы
            slug_base = re.sub(r'[^a-z0-9-]', '', slug_base)
            if not slug_base:
                slug_base = 'dashboard'
            
            slug = slug_base
            counter = 1
            
            while await run_in_threadpool(
                lambda: db.query(models.Dashboard).filter(models.Dashboard.slug == slug).first()
            ):
                slug = f"{slug_base}-{counter}"
                counter += 1
            
            dashboard = models.Dashboard(
                title=data['title'],
                description=data.get('description', ''),
                slug=slug,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                layout_data=json.dumps(data.get('layout', {})),
                is_published=True  # Автоматически публикуем при создании
            )
            db.add(dashboard)
            await run_in_threadpool(db.flush)
            print(f"Создан новый дашборд с ID: {dashboard.id}")
        
        # Сохраняем элементы
        elements_count = 0
        for idx, element_data in enumerate(data.get('elements', [])):
            element = models.DashboardElement(
                dashboard_id=dashboard.id,
                element_type=element_data['type'],
                chart_type=element_data.get('chartType'),
                title=element_data.get('title', ''),
                content=json.dumps(element_data.get('content', {}), ensure_ascii=False),
                settings=json.dumps(element_data.get('settings', {}), ensure_ascii=False),
                position_x=element_data.get('position', {}).get('x', 0),
                position_y=element_data.get('position', {}).get('y', 0),
                width=element_data.get('size', {}).get('w', 4),
                height=element_data.get('size', {}).get('h', 4),
                order_index=idx
            )
            db.add(element)
            elements_count += 1
        
        await run_in_threadpool(db.commit)
        print(f"Сохранено {elements_count} элементов для дашборда {dashboard.id}")
        
        return {"status": "success", "id": dashboard.id, "slug": dashboard.slug, "published": True}
    
    except Exception as e:
        print(f"Ошибка при сохранении дашборда: {e}")
        await run_in_threadpool(db.rollback)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dashboard-admin/edit/{dashboard_id}")
async def edit_dashboard(request: Request, dashboard_id: int, db: Session = Depends(get_db)):
    """Редактирование дашборда"""
    if not request.session.get("dashboard_admin"):
        return RedirectResponse("/dashboard-admin/login", status_code=303)
    
    dashboard = await run_in_threadpool(
        lambda: db.query(models.Dashboard).filter(models.Dashboard.id == dashboard_id).first()
    )
    
    if not dashboard:
        raise HTTPException(status_code=404, detail="Дашборд не найден")
    
    # Загружаем элементы
    elements = await run_in_threadpool(
        lambda: db.query(models.DashboardElement).filter(models.DashboardElement.dashboard_id == dashboard_id).order_by(models.DashboardElement.order_index).all()
    )
    
    dashboard_data = {
        "id": dashboard.id,
        "title": dashboard.title,
        "description": dashboard.description,
        "slug": dashboard.slug,
        "is_published": dashboard.is_published,  # Добавляем поле публикации
        "elements": []
    }
    
    for element in elements:
        dashboard_data["elements"].append({
            "id": element.id,
            "type": element.element_type,
            "chartType": element.chart_type,
            "title": element.title,
            "content": json.loads(element.content) if element.content else {},
            "settings": json.loads(element.settings) if element.settings else {},
            "position": {"x": element.position_x, "y": element.position_y},
            "size": {"w": element.width, "h": element.height}
        })
    
    return templates.TemplateResponse("dashboard_editor.html", {
        "request": request,
        "dashboard": dashboard_data
    })

@app.post("/dashboard-admin/delete/{dashboard_id}")
async def delete_dashboard(request: Request, dashboard_id: int, db: Session = Depends(get_db)):
    """Удаление дашборда"""
    if not request.session.get("dashboard_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    dashboard = await run_in_threadpool(
        lambda: db.query(models.Dashboard).filter(models.Dashboard.id == dashboard_id).first()
    )
    
    if dashboard:
        await run_in_threadpool(lambda: db.delete(dashboard))
        await run_in_threadpool(db.commit)
    
    return {"status": "success"}

@app.get("/dashboard/{slug}")
async def view_dashboard(request: Request, slug: str, db: Session = Depends(get_db)):
    """Просмотр дашборда"""
    # Пробуем найти по slug или по id
    if slug.isdigit():
        dashboard = await run_in_threadpool(
            lambda: db.query(models.Dashboard).filter(models.Dashboard.id == int(slug)).first()
        )
    else:
        dashboard = await run_in_threadpool(
            lambda: db.query(models.Dashboard).filter(models.Dashboard.slug == slug).first()
        )
    
    if not dashboard:
        raise HTTPException(status_code=404, detail="Дашборд не найден")
    
    # Проверяем доступ (если не опубликован, только админ может видеть)
    if not dashboard.is_published and not request.session.get("dashboard_admin"):
        raise HTTPException(status_code=404, detail="Дашборд не найден")
    
    # Загружаем элементы
    elements = await run_in_threadpool(
        lambda: db.query(models.DashboardElement).filter(models.DashboardElement.dashboard_id == dashboard.id).order_by(models.DashboardElement.order_index).all()
    )
    
    # Парсим JSON поля
    for element in elements:
        if element.content:
            try:
                element.content = json.loads(element.content)
            except:
                element.content = {}
        if element.settings:
            try:
                element.settings = json.loads(element.settings)
            except:
                element.settings = {}
    
    dashboard.elements = elements
    
    return templates.TemplateResponse("dashboard_view.html", {
        "request": request,
        "dashboard": dashboard,
        "session": request.session
    })

# --- БИБЛИОТЕКА ЗНАНИЙ ПО ПИТАНИЮ ---
KNOWLEDGE_BASE_ADMIN_CODE = ""

DOCUMENT_TYPES = {
    "document": "📄 Документ",
    "instruction": "📋 Инструкция",
    "order": "📌 Приказ",
    "method": "📚 Методичка",
    "presentation": "📊 Презентация",
    "video": "🎥 Видео",
    "spreadsheet": "📊 Таблица",
    "image": "🖼️ Изображение",
    "other": "📁 Другое"
}

CATEGORY_ICONS = ["📁", "📊", "📋", "📌", "📚", "🎥", "📝", "⚖️", "🍎", "🥗", "📈", "🔬", "🏫", "👨‍🍳"]

@app.get("/knowledge-base", response_class=HTMLResponse)
async def knowledge_base(
    request: Request,
    category: int = None,
    search: str = "",
    page: int = 1,
    per_page: int = 12,
    sort: str = "newest",
    kb_db: Session = Depends(get_kb_db)
):
    """Главная страница библиотеки знаний"""
    
    # Получаем все активные категории
    categories = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseCategory).filter(
            KnowledgeBaseCategory.is_active == True
        ).order_by(KnowledgeBaseCategory.order_index).all()
    )
    
    # Базовый запрос для документов
    query = kb_db.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.is_published == True)
    
    # Фильтр по категории
    if category:
        query = query.filter(KnowledgeBaseDocument.category_id == category)
        current_category = await run_in_threadpool(
            lambda: kb_db.query(KnowledgeBaseCategory).filter(KnowledgeBaseCategory.id == category).first()
        )
    else:
        current_category = None
    
    # Поиск
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            or_(
                KnowledgeBaseDocument.title.ilike(search_term),
                KnowledgeBaseDocument.description.ilike(search_term),
                KnowledgeBaseDocument.tags.ilike(search_term)
            )
        )
        
        # Логируем поиск
        user_email = request.session.get("user_email")
        search_log = KnowledgeBaseSearchLog(
            query=search,
            user_email=user_email
        )
        kb_db.add(search_log)
        await run_in_threadpool(kb_db.commit)
    
    # Сортировка
    if sort == "newest":
        query = query.order_by(KnowledgeBaseDocument.created_at.desc())
    elif sort == "popular":
        query = query.order_by(KnowledgeBaseDocument.downloads_count.desc())
    elif sort == "views":
        query = query.order_by(KnowledgeBaseDocument.views_count.desc())
    elif sort == "title":
        query = query.order_by(KnowledgeBaseDocument.title)
    
    # Пагинация
    total = await run_in_threadpool(query.count)
    offset = (page - 1) * per_page
    documents = await run_in_threadpool(
        lambda: query.offset(offset).limit(per_page).all()
    )
    
    # Получаем популярные документы
    popular_docs = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseDocument).filter(
            KnowledgeBaseDocument.is_published == True
        ).order_by(KnowledgeBaseDocument.downloads_count.desc()).limit(5).all()
    )
    
    # Недавно добавленные
    recent_docs = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseDocument).filter(
            KnowledgeBaseDocument.is_published == True
        ).order_by(KnowledgeBaseDocument.created_at.desc()).limit(5).all()
    )
    
    # Получаем email пользователя из сессии
    user_email = request.session.get("user_email")
    favorites = []
    
    if user_email:
        favs = await run_in_threadpool(
            lambda: kb_db.query(KnowledgeBaseFavorite).filter(
                KnowledgeBaseFavorite.user_email == user_email
            ).all()
        )
        favorites = [fav.document_id for fav in favs]
    
    return templates.TemplateResponse("knowledge_base.html", {
        "request": request,
        "user_email": user_email,
        "categories": categories,
        "documents": documents,
        "popular_docs": popular_docs,
        "recent_docs": recent_docs,
        "current_category": current_category,
        "favorites": favorites,
        "total": total,
        "page": page,
        "per_page": per_page,
        "search": search,
        "sort": sort,
        "document_types": DOCUMENT_TYPES,
        "total_pages": (total + per_page - 1) // per_page
    })

@app.get("/knowledge-base/admin/login", response_class=HTMLResponse)
async def knowledge_base_admin_login(request: Request):
    """Страница входа в админку библиотеки"""
    return templates.TemplateResponse("knowledge_base_admin_login.html", {"request": request})

@app.post("/knowledge-base/admin/login")
async def knowledge_base_admin_login_post(
    request: Request,
    access_code: str = Form(...),
    email: str = Form(...),
    name: str = Form(""),
    kb_db: Session = Depends(get_kb_db)
):
    """Вход в админку библиотеки"""
    if access_code == KNOWLEDGE_BASE_ADMIN_CODE:
        # Сохраняем в сессии
        request.session["knowledge_base_admin"] = True
        request.session["admin_email"] = email
        request.session["admin_name"] = name if name else "Администратор"
        
        # Сохраняем/обновляем в БД
        admin = await run_in_threadpool(
            lambda: kb_db.query(KnowledgeBaseAdmin).filter(
                KnowledgeBaseAdmin.email == email
            ).first()
        )
        
        if not admin:
            admin = KnowledgeBaseAdmin(
                email=email,
                name=name if name else "Администратор",
                access_code=hashlib.sha256(access_code.encode()).hexdigest(),
                last_login=datetime.utcnow()
            )
            kb_db.add(admin)
        else:
            admin.last_login = datetime.utcnow()
        
        await run_in_threadpool(kb_db.commit)
        
        return RedirectResponse("/knowledge-base/admin", status_code=303)
    
    return templates.TemplateResponse("knowledge_base_admin_login.html", {
        "request": request,
        "error": "Неверный код доступа"
    })

@app.get("/knowledge-base/admin/logout")
async def knowledge_base_admin_logout(request: Request):
    """Выход из админки библиотеки"""
    request.session.pop("knowledge_base_admin", None)
    request.session.pop("admin_email", None)
    request.session.pop("admin_name", None)
    return RedirectResponse("/knowledge-base", status_code=303)

@app.get("/knowledge-base/admin", response_class=HTMLResponse)
async def knowledge_base_admin_panel(
    request: Request,
    kb_db: Session = Depends(get_kb_db)
):
    """Админ-панель библиотеки знаний"""
    if not request.session.get("knowledge_base_admin"):
        return RedirectResponse("/knowledge-base/admin/login", status_code=303)
    
    admin_email = request.session.get("admin_email")
    admin_name = request.session.get("admin_name")
    
    # Статистика
    total_docs = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseDocument).count()
    )
    total_categories = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseCategory).count()
    )
    total_downloads = await run_in_threadpool(
        lambda: kb_db.query(func.sum(KnowledgeBaseDocument.downloads_count)).scalar() or 0
    )
    total_views = await run_in_threadpool(
        lambda: kb_db.query(func.sum(KnowledgeBaseDocument.views_count)).scalar() or 0
    )
    
    # Последние загруженные
    recent_docs = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseDocument).order_by(
            KnowledgeBaseDocument.created_at.desc()
        ).limit(10).all()
    )
    
    # Категории с количеством документов
    categories_stats = await run_in_threadpool(
        lambda: kb_db.query(
            KnowledgeBaseCategory,
            func.count(KnowledgeBaseDocument.id).label('doc_count')
        ).outerjoin(
            KnowledgeBaseDocument,
            KnowledgeBaseCategory.id == KnowledgeBaseDocument.category_id
        ).group_by(KnowledgeBaseCategory.id).order_by(KnowledgeBaseCategory.order_index).all()
    )
    
    return templates.TemplateResponse("knowledge_base_admin.html", {
        "request": request,
        "admin_email": admin_email,
        "admin_name": admin_name,
        "total_docs": total_docs,
        "total_categories": total_categories,
        "total_downloads": total_downloads,
        "total_views": total_views,
        "recent_docs": recent_docs,
        "categories_stats": categories_stats
    })

@app.get("/knowledge-base/admin/categories", response_class=HTMLResponse)
async def manage_categories(
    request: Request,
    kb_db: Session = Depends(get_kb_db)
):
    """Управление категориями"""
    if not request.session.get("knowledge_base_admin"):
        return RedirectResponse("/knowledge-base/admin/login", status_code=303)
    
    categories = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseCategory).order_by(
            KnowledgeBaseCategory.order_index
        ).all()
    )
    
    return templates.TemplateResponse("knowledge_base_categories.html", {
        "request": request,
        "categories": categories,
        "icons": CATEGORY_ICONS
    })

@app.post("/knowledge-base/admin/category/create")
async def create_category(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    icon: str = Form("📁"),
    color: str = Form("#667eea"),
    order_index: int = Form(0),
    kb_db: Session = Depends(get_kb_db)
):
    """Создание новой категории"""
    if not request.session.get("knowledge_base_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    category = KnowledgeBaseCategory(
        name=name,
        description=description,
        icon=icon,
        color=color,
        order_index=order_index
    )
    
    kb_db.add(category)
    await run_in_threadpool(kb_db.commit)
    
    return RedirectResponse("/knowledge-base/admin/categories", status_code=303)

@app.post("/knowledge-base/admin/category/{category_id}/update")
async def update_category(
    request: Request,
    category_id: int,
    name: str = Form(...),
    description: str = Form(""),
    icon: str = Form("📁"),
    color: str = Form("#667eea"),
    order_index: int = Form(0),
    is_active: bool = Form(True),
    kb_db: Session = Depends(get_kb_db)
):
    """Обновление категории"""
    if not request.session.get("knowledge_base_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    category = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseCategory).filter(KnowledgeBaseCategory.id == category_id).first()
    )
    
    if not category:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    
    category.name = name
    category.description = description
    category.icon = icon
    category.color = color
    category.order_index = order_index
    category.is_active = is_active
    
    await run_in_threadpool(kb_db.commit)
    
    return RedirectResponse("/knowledge-base/admin/categories", status_code=303)

@app.post("/knowledge-base/admin/category/{category_id}/delete")
async def delete_category(
    request: Request,
    category_id: int,
    kb_db: Session = Depends(get_kb_db)
):
    """Удаление категории"""
    if not request.session.get("knowledge_base_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    category = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseCategory).filter(KnowledgeBaseCategory.id == category_id).first()
    )
    
    if category:
        await run_in_threadpool(lambda: kb_db.delete(category))
        await run_in_threadpool(kb_db.commit)
    
    return RedirectResponse("/knowledge-base/admin/categories", status_code=303)

@app.get("/knowledge-base/admin/upload", response_class=HTMLResponse)
async def upload_document_page(
    request: Request,
    kb_db: Session = Depends(get_kb_db)
):
    """Страница загрузки документа"""
    if not request.session.get("knowledge_base_admin"):
        return RedirectResponse("/knowledge-base/admin/login", status_code=303)
    
    categories = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseCategory).filter(
            KnowledgeBaseCategory.is_active == True
        ).order_by(KnowledgeBaseCategory.order_index).all()
    )
    
    admin_name = request.session.get("admin_name", "Администратор")
    admin_email = request.session.get("admin_email", "")
    
    return templates.TemplateResponse("knowledge_base_upload.html", {
        "request": request,
        "categories": categories,
        "document_types": DOCUMENT_TYPES,
        "admin_name": admin_name,
        "admin_email": admin_email
    })

@app.post("/knowledge-base/admin/upload")
async def upload_document(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    category_id: int = Form(None),
    document_type: str = Form("document"),
    tags: str = Form(""),
    is_featured: bool = Form(False),
    file: UploadFile = File(...),
    cover_image: UploadFile = File(None),
    kb_db: Session = Depends(get_kb_db)
):
    """Загрузка документа в библиотеку"""
    if not request.session.get("knowledge_base_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    admin_name = request.session.get("admin_name", "Администратор")
    admin_email = request.session.get("admin_email", "")
    
    # Создаём директории для файлов библиотеки
    BASE_DIR = Path(__file__).resolve().parent
    kb_files_dir = BASE_DIR / "knowledge_base_files"
    documents_dir = kb_files_dir / "documents"
    covers_dir = kb_files_dir / "covers"
    
    await run_in_threadpool(lambda: documents_dir.mkdir(parents=True, exist_ok=True))
    await run_in_threadpool(lambda: covers_dir.mkdir(parents=True, exist_ok=True))
    
    # Сохраняем основной файл
    file_ext = Path(file.filename).suffix.lower()
    safe_filename = f"{int(time.time())}_{secrets.token_hex(8)}{file_ext}"
    file_path = documents_dir / safe_filename
    
    await save_uploaded_file_optimized(file, file_path)
    
    # Сохраняем обложку (если есть)
    cover_path = None
    if cover_image and cover_image.filename:
        cover_ext = Path(cover_image.filename).suffix.lower()
        cover_filename = f"cover_{int(time.time())}_{secrets.token_hex(8)}{cover_ext}"
        cover_path = covers_dir / cover_filename
        await save_uploaded_file_optimized(cover_image, cover_path)
    
    # Создаем запись в отдельной БД
    document = KnowledgeBaseDocument(
        title=title,
        description=description,
        category_id=category_id if category_id else None,
        document_type=document_type,
        file_extension=file_ext,
        file_size=file.size,
        file_path=str(file_path.relative_to(BASE_DIR)),
        cover_image_path=str(cover_path.relative_to(BASE_DIR)) if cover_path else None,
        tags=tags,
        uploaded_by=admin_name,
        uploaded_by_email=admin_email,
        is_featured=is_featured
    )
    
    kb_db.add(document)
    await run_in_threadpool(kb_db.commit)
    
    return RedirectResponse(f"/knowledge-base/document/{document.id}", status_code=303)

@app.get("/knowledge-base/document/{doc_id}", response_class=HTMLResponse)
async def view_document(
    request: Request,
    doc_id: int,
    kb_db: Session = Depends(get_kb_db)
):
    """Просмотр документа"""
    document = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.id == doc_id).first()
    )
    
    if not document or not document.is_published:
        # Проверяем, может админ смотрит
        if not request.session.get("knowledge_base_admin"):
            raise HTTPException(status_code=404, detail="Документ не найден")
    
    # Увеличиваем счетчик просмотров
    document.views_count += 1
    await run_in_threadpool(kb_db.commit)
    
    # Похожие документы
    similar_docs = []
    if document.category_id:
        similar_docs = await run_in_threadpool(
            lambda: kb_db.query(KnowledgeBaseDocument).filter(
                KnowledgeBaseDocument.category_id == document.category_id,
                KnowledgeBaseDocument.id != doc_id,
                KnowledgeBaseDocument.is_published == True
            ).order_by(KnowledgeBaseDocument.downloads_count.desc()).limit(4).all()
        )
    
    # Категория
    category = None
    if document.category_id:
        category = await run_in_threadpool(
            lambda: kb_db.query(KnowledgeBaseCategory).filter(
                KnowledgeBaseCategory.id == document.category_id
            ).first()
        )
    
    # Проверяем избранное
    user_email = request.session.get("user_email")
    is_favorite = False
    
    if user_email:
        fav = await run_in_threadpool(
            lambda: kb_db.query(KnowledgeBaseFavorite).filter(
                KnowledgeBaseFavorite.user_email == user_email,
                KnowledgeBaseFavorite.document_id == doc_id
            ).first()
        )
        is_favorite = fav is not None
    
    # Получаем комментарии
    comments = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseComment).filter(
            KnowledgeBaseComment.document_id == doc_id,
            KnowledgeBaseComment.is_approved == True
        ).order_by(KnowledgeBaseComment.created_at.desc()).all()
    )
    
    return templates.TemplateResponse("knowledge_base_document.html", {
        "request": request,
        "document": document,
        "category": category,
        "similar_docs": similar_docs,
        "is_favorite": is_favorite,
        "comments": comments,
        "user_email": user_email,
        "document_types": DOCUMENT_TYPES,
        "is_admin": request.session.get("knowledge_base_admin", False)
    })

@app.get("/knowledge-base/download/{doc_id}")
async def download_document(
    request: Request,
    doc_id: int,
    kb_db: Session = Depends(get_kb_db)
):
    """Скачивание документа"""
    document = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.id == doc_id).first()
    )
    
    if not document:
        raise HTTPException(status_code=404, detail="Документ не найден")
    
    # Проверяем опубликован ли документ (админы могут скачивать и неопубликованные)
    if not document.is_published and not request.session.get("knowledge_base_admin"):
        raise HTTPException(status_code=404, detail="Документ не найден")
    
    BASE_DIR = Path(__file__).resolve().parent
    file_path = BASE_DIR / document.file_path
    
    if not await run_in_threadpool(file_path.exists):
        raise HTTPException(status_code=404, detail="Файл не найден")
    
    # Увеличиваем счетчик скачиваний
    document.downloads_count += 1
    await run_in_threadpool(kb_db.commit)
    
    # Формируем имя файла для скачивания
    filename = f"{document.title}{document.file_extension}"
    
    # Кодируем имя файла для корректной обработки русских символов
    import urllib.parse
    encoded_filename = urllib.parse.quote(filename)
    
    # Возвращаем файл с правильными заголовками
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
        }
    )

@app.post("/knowledge-base/favorite/{doc_id}")
async def toggle_favorite(
    request: Request,
    doc_id: int,
    kb_db: Session = Depends(get_kb_db)
):
    """Добавить/удалить из избранного"""
    user_email = request.session.get("user_email")
    if not user_email:
        return {"status": "error", "message": "Требуется авторизация"}
    
    favorite = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseFavorite).filter(
            KnowledgeBaseFavorite.user_email == user_email,
            KnowledgeBaseFavorite.document_id == doc_id
        ).first()
    )
    
    if favorite:
        await run_in_threadpool(lambda: kb_db.delete(favorite))
        await run_in_threadpool(kb_db.commit)
        return {"status": "success", "action": "removed"}
    else:
        new_favorite = KnowledgeBaseFavorite(
            user_email=user_email,
            document_id=doc_id
        )
        kb_db.add(new_favorite)
        await run_in_threadpool(kb_db.commit)
        return {"status": "success", "action": "added"}

@app.post("/knowledge-base/comment/{doc_id}")
async def add_comment(
    request: Request,
    doc_id: int,
    content: str = Form(...),
    user_name: str = Form(""),
    kb_db: Session = Depends(get_kb_db)
):
    """Добавление комментария"""
    user_email = request.session.get("user_email")
    
    comment = KnowledgeBaseComment(
        document_id=doc_id,
        user_name=user_name if user_name else "Гость",
        user_email=user_email,
        content=content,
        is_approved=False  # Требуется модерация
    )
    
    kb_db.add(comment)
    await run_in_threadpool(kb_db.commit)
    
    return RedirectResponse(f"/knowledge-base/document/{doc_id}", status_code=303)

@app.get("/knowledge-base/admin/edit/{doc_id}", response_class=HTMLResponse)
async def edit_document_page(
    request: Request,
    doc_id: int,
    kb_db: Session = Depends(get_kb_db)
):
    """Редактирование документа"""
    if not request.session.get("knowledge_base_admin"):
        return RedirectResponse("/knowledge-base/admin/login", status_code=303)
    
    document = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.id == doc_id).first()
    )
    
    if not document:
        raise HTTPException(status_code=404, detail="Документ не найден")
    
    categories = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseCategory).filter(
            KnowledgeBaseCategory.is_active == True
        ).order_by(KnowledgeBaseCategory.order_index).all()
    )
    
    return templates.TemplateResponse("knowledge_base_edit.html", {
        "request": request,
        "document": document,
        "categories": categories,
        "document_types": DOCUMENT_TYPES
    })

@app.post("/knowledge-base/admin/edit/{doc_id}")
async def edit_document(
    request: Request,
    doc_id: int,
    title: str = Form(...),
    description: str = Form(""),
    category_id: int = Form(None),
    document_type: str = Form("document"),
    tags: str = Form(""),
    is_published: bool = Form(True),
    is_featured: bool = Form(False),
    kb_db: Session = Depends(get_kb_db)
):
    """Обновление документа"""
    if not request.session.get("knowledge_base_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    document = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.id == doc_id).first()
    )
    
    if not document:
        raise HTTPException(status_code=404, detail="Документ не найден")
    
    document.title = title
    document.description = description
    document.category_id = category_id if category_id else None
    document.document_type = document_type
    document.tags = tags
    document.is_published = is_published
    document.is_featured = is_featured
    document.updated_at = datetime.utcnow()
    
    await run_in_threadpool(kb_db.commit)
    
    return RedirectResponse(f"/knowledge-base/document/{doc_id}", status_code=303)

@app.post("/knowledge-base/admin/delete/{doc_id}")
async def delete_document(
    request: Request,
    doc_id: int,
    kb_db: Session = Depends(get_kb_db)
):
    """Удаление документа"""
    if not request.session.get("knowledge_base_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    document = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.id == doc_id).first()
    )
    
    if not document:
        raise HTTPException(status_code=404, detail="Документ не найден")
    
    # Удаляем файлы
    BASE_DIR = Path(__file__).resolve().parent
    file_path = BASE_DIR / document.file_path
    await delete_file_optimized(file_path)
    
    if document.cover_image_path:
        cover_path = BASE_DIR / document.cover_image_path
        await delete_file_optimized(cover_path)
    
    # Удаляем запись из БД
    await run_in_threadpool(lambda: kb_db.delete(document))
    await run_in_threadpool(kb_db.commit)
    
    return RedirectResponse("/knowledge-base/admin", status_code=303)

@app.get("/knowledge-base/api/search")
async def knowledge_base_search_api(
    request: Request,
    q: str = "",
    kb_db: Session = Depends(get_kb_db)
):
    """API для быстрого поиска"""
    if len(q) < 2:
        return {"results": []}
    
    search_term = f"%{q}%"
    results = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseDocument).filter(
            KnowledgeBaseDocument.is_published == True,
            or_(
                KnowledgeBaseDocument.title.ilike(search_term),
                KnowledgeBaseDocument.description.ilike(search_term),
                KnowledgeBaseDocument.tags.ilike(search_term)
            )
        ).limit(10).all()
    )
    
    return {
        "results": [
            {
                "id": doc.id,
                "title": doc.title,
                "type": DOCUMENT_TYPES.get(doc.document_type, "Документ"),
                "url": f"/knowledge-base/document/{doc.id}",
                "icon": "📄"
            }
            for doc in results
        ]
    }

@app.get("/knowledge-base/stats")
async def knowledge_base_stats(
    request: Request,
    kb_db: Session = Depends(get_kb_db)
):
    """Публичная статистика библиотеки"""
    total_docs = await run_in_threadpool(
        lambda: kb_db.query(KnowledgeBaseDocument).filter(
            KnowledgeBaseDocument.is_published == True
        ).count()
    )
    
    total_downloads = await run_in_threadpool(
        lambda: kb_db.query(func.sum(KnowledgeBaseDocument.downloads_count)).scalar() or 0
    )
    
    # Топ категорий
    top_categories = await run_in_threadpool(
        lambda: kb_db.query(
            KnowledgeBaseCategory.name,
            KnowledgeBaseCategory.icon,
            func.count(KnowledgeBaseDocument.id).label('doc_count')
        ).join(
            KnowledgeBaseDocument,
            KnowledgeBaseCategory.id == KnowledgeBaseDocument.category_id
        ).filter(
            KnowledgeBaseDocument.is_published == True
        ).group_by(KnowledgeBaseCategory.id).order_by(func.count(KnowledgeBaseDocument.id).desc()).limit(5).all()
    )
    
    return templates.TemplateResponse("knowledge_base_stats.html", {
        "request": request,
        "total_docs": total_docs,
        "total_downloads": total_downloads,
        "top_categories": top_categories
    })

# ========== УНИВЕРСАЛЬНАЯ СИСТЕМА УПРАВЛЕНИЯ ОТЧЁТНОСТЬЮ ==========
# Константа для регионального доступа
REGIONAL_REPORT_CODE = "MoinCHR3377"

# Создаём папку для файлов отчётов при старте приложения
REPORTS_DIR = Path(__file__).resolve().parent / "reports_files"
REPORTS_DIR.mkdir(exist_ok=True)

@app.get("/regional-admin/login", response_class=HTMLResponse)
async def regional_admin_login_page(request: Request):
    """Страница входа в региональную систему отчётности"""
    return templates.TemplateResponse("regional_admin_login.html", {"request": request})

@app.post("/regional-admin/login")
async def regional_admin_login(request: Request, access_code: str = Form(...)):
    """Вход в региональную систему отчётности"""
    if access_code == REGIONAL_REPORT_CODE:
        request.session["regional_admin"] = True
        request.session["regional_admin_login_time"] = datetime.now().isoformat()
        return RedirectResponse("/regional-admin/dashboard", status_code=303)
    
    return templates.TemplateResponse("regional_admin_login.html", {
        "request": request,
        "error": "Неверный код доступа"
    })

@app.get("/regional-admin/logout")
async def regional_admin_logout(request: Request):
    """Выход из региональной системы отчётности"""
    request.session.pop("regional_admin", None)
    return RedirectResponse("/regional-admin/login", status_code=303)

@app.get("/regional-admin/dashboard", response_class=HTMLResponse)
async def regional_admin_dashboard(
    request: Request,
    db: Session = Depends(get_db)
):
    """Дашборд регионального администратора"""
    if not request.session.get("regional_admin"):
        return RedirectResponse("/regional-admin/login", status_code=303)
    
    # Статистика
    total_reports = await run_in_threadpool(lambda: db.query(models.Report).count())
    total_categories = await run_in_threadpool(lambda: db.query(models.ReportCategory).count())
    total_files = await run_in_threadpool(lambda: db.query(models.ReportFile).count())
    
    # Отчёты по статусам
    draft_count = await run_in_threadpool(lambda: db.query(models.Report).filter(models.Report.status == "draft").count())
    published_count = await run_in_threadpool(lambda: db.query(models.Report).filter(models.Report.is_published == True).count())
    archived_count = await run_in_threadpool(lambda: db.query(models.Report).filter(models.Report.status == "archived").count())
    
    # Отчёты по годам
    years_stats = await run_in_threadpool(
        lambda: db.query(models.Report.year, func.count(models.Report.id))
        .group_by(models.Report.year)
        .order_by(models.Report.year.desc())
        .limit(5)
        .all()
    )
    
    # Последние 10 отчётов
    recent_reports = await run_in_threadpool(
        lambda: db.query(models.Report)
        .order_by(models.Report.created_at.desc())
        .limit(10)
        .all()
    )
    
    # Категории с количеством отчётов
    categories = await run_in_threadpool(
        lambda: db.query(models.ReportCategory)
        .filter(models.ReportCategory.is_active == True)
        .order_by(models.ReportCategory.order_index)
        .all()
    )
    
    for cat in categories:
        cat.report_count = await run_in_threadpool(
            lambda: db.query(models.Report).filter(models.Report.category_id == cat.id).count()
        )
    
    return templates.TemplateResponse("regional_admin_dashboard.html", {
        "request": request,
        "total_reports": total_reports,
        "total_categories": total_categories,
        "total_files": total_files,
        "draft_count": draft_count,
        "published_count": published_count,
        "archived_count": archived_count,
        "years_stats": years_stats,
        "recent_reports": recent_reports,
        "categories": categories,
        "months": MONTHS
    })

@app.get("/regional-admin/categories", response_class=HTMLResponse)
async def regional_admin_categories(
    request: Request,
    db: Session = Depends(get_db)
):
    """Управление категориями отчётов"""
    if not request.session.get("regional_admin"):
        return RedirectResponse("/regional-admin/login", status_code=303)
    
    categories = await run_in_threadpool(
        lambda: db.query(models.ReportCategory)
        .order_by(models.ReportCategory.order_index)
        .all()
    )
    
    for cat in categories:
        cat.report_count = await run_in_threadpool(
            lambda: db.query(models.Report).filter(models.Report.category_id == cat.id).count()
        )
    
    return templates.TemplateResponse("regional_admin_categories.html", {
        "request": request,
        "categories": categories
    })

@app.post("/regional-admin/category/create")
async def regional_admin_create_category(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    icon: str = Form("📊"),
    color: str = Form("#667eea"),
    parent_id: int = Form(None),
    order_index: int = Form(0),
    db: Session = Depends(get_db)
):
    """Создание категории отчётов"""
    if not request.session.get("regional_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    category = models.ReportCategory(
        name=name,
        description=description,
        icon=icon,
        color=color,
        parent_id=parent_id if parent_id else None,
        order_index=order_index
    )
    
    db.add(category)
    await run_in_threadpool(db.commit)
    
    return RedirectResponse("/regional-admin/categories", status_code=303)

@app.post("/regional-admin/category/{category_id}/update")
async def regional_admin_update_category(
    request: Request,
    category_id: int,
    name: str = Form(...),
    description: str = Form(""),
    icon: str = Form("📊"),
    color: str = Form("#667eea"),
    order_index: int = Form(0),
    is_active: bool = Form(True),
    db: Session = Depends(get_db)
):
    """Обновление категории"""
    if not request.session.get("regional_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    category = await run_in_threadpool(
        lambda: db.query(models.ReportCategory).filter(models.ReportCategory.id == category_id).first()
    )
    
    if category:
        category.name = name
        category.description = description
        category.icon = icon
        category.color = color
        category.order_index = order_index
        category.is_active = is_active
        await run_in_threadpool(db.commit)
    
    return RedirectResponse("/regional-admin/categories", status_code=303)

@app.post("/regional-admin/category/{category_id}/delete")
async def regional_admin_delete_category(
    request: Request,
    category_id: int,
    db: Session = Depends(get_db)
):
    """Удаление категории"""
    if not request.session.get("regional_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    category = await run_in_threadpool(
        lambda: db.query(models.ReportCategory).filter(models.ReportCategory.id == category_id).first()
    )
    
    if category:
        await run_in_threadpool(lambda: db.delete(category))
        await run_in_threadpool(db.commit)
    
    return RedirectResponse("/regional-admin/categories", status_code=303)

@app.get("/regional-admin/reports", response_class=HTMLResponse)
async def regional_admin_reports(
    request: Request,
    category_id: str = None,  # Изменяем на str, чтобы принимать пустые строки
    year: str = None,         # Изменяем на str
    status: str = None,
    search: str = "",
    page: int = 1,
    per_page: int = 20,
    db: Session = Depends(get_db)
):
    """Список отчётов"""
    if not request.session.get("regional_admin"):
        return RedirectResponse("/regional-admin/login", status_code=303)
    
    query = db.query(models.Report)
    
    # Преобразуем строки в числа, если они не пустые
    category_id_int = None
    if category_id and category_id.strip():
        try:
            category_id_int = int(category_id)
        except ValueError:
            pass
    
    year_int = None
    if year and year.strip():
        try:
            year_int = int(year)
        except ValueError:
            pass
    
    # Применяем фильтры
    if category_id_int:
        query = query.filter(models.Report.category_id == category_id_int)
    if year_int:
        query = query.filter(models.Report.year == year_int)
    if status:
        query = query.filter(models.Report.status == status)
    if search:
        query = query.filter(
            or_(
                models.Report.title.ilike(f"%{search}%"),
                models.Report.description.ilike(f"%{search}%")
            )
        )
    
    total = await run_in_threadpool(query.count)
    offset = (page - 1) * per_page
    reports = await run_in_threadpool(
        lambda: query.order_by(models.Report.created_at.desc())
        .offset(offset).limit(per_page).all()
    )
    
    # Получаем категории для фильтра
    categories = await run_in_threadpool(
        lambda: db.query(models.ReportCategory).filter(models.ReportCategory.is_active == True).all()
    )
    
    # Получаем доступные годы
    years_result = await run_in_threadpool(
        lambda: db.query(models.Report.year).distinct().order_by(models.Report.year.desc()).all()
    )
    years = [str(y[0]) for y in years_result if y[0]]
    
    return templates.TemplateResponse("regional_admin_reports.html", {
        "request": request,
        "reports": reports,
        "total": total,
        "page": page,
        "per_page": per_page,
        "categories": categories,
        "years": years,
        "selected_category": category_id if category_id else "",
        "selected_year": year if year else "",
        "selected_status": status if status else "",
        "search": search
    })

@app.get("/regional-admin/report/create", response_class=HTMLResponse)
async def regional_admin_create_report_page(
    request: Request,
    db: Session = Depends(get_db)
):
    """Страница создания отчёта"""
    if not request.session.get("regional_admin"):
        return RedirectResponse("/regional-admin/login", status_code=303)
    
    categories = await run_in_threadpool(
        lambda: db.query(models.ReportCategory).filter(models.ReportCategory.is_active == True).all()
    )
    
    # Доступные типы отчётов
    report_types = [
        {"value": "hot_meal", "name": "Горячее питание", "icon": "🍲", "description": "Отчёты по организации горячего питания"},
        {"value": "salary", "name": "Зарплата педработников", "icon": "💰", "description": "Мониторинг трудовой нагрузки и доходов"},
        {"value": "accidents", "name": "Несчастные случаи", "icon": "⚠️", "description": "Отчёты о несчастных случаях"},
        {"value": "building", "name": "Перепрофилирование", "icon": "🏫", "description": "Перепрофилирование сооружений"},
        {"value": "cadet", "name": "Кадетское образование", "icon": "🎖️", "description": "Кадетские корпуса и классы"},
        {"value": "benefits", "name": "Льготы на питание", "icon": "🎁", "description": "Региональные и муниципальные льготы"},
        {"value": "custom", "name": "Произвольный отчёт", "icon": "📝", "description": "Создать отчёт с произвольными данными"}
    ]
    
    return templates.TemplateResponse("regional_admin_report_create.html", {
        "request": request,
        "categories": categories,
        "report_types": report_types,
        "months": MONTHS
    })

@app.post("/regional-admin/report/create")
async def regional_admin_create_report(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    category_id: int = Form(None),
    report_type: str = Form("custom"),
    year: int = Form(...),
    month: int = Form(None),
    quarter: int = Form(None),
    report_data: str = Form("{}"),
    status: str = Form("draft"),
    files: List[UploadFile] = File(None),
    db: Session = Depends(get_db)
):
    """Создание нового отчёта"""
    if not request.session.get("regional_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    # Парсим данные отчёта
    try:
        if report_data and report_data.strip():
            data = json.loads(report_data)
        else:
            data = {}
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {e}, data: {report_data}")
        data = {}
    
    # Создаём отчёт
    report = models.Report(
        title=title,
        description=description,
        category_id=category_id if category_id else None,
        report_type=report_type,
        year=year,
        month=month,
        quarter=quarter,
        data=json.dumps(data, ensure_ascii=False),
        status=status,
        is_published=(status == "published")
    )
    
    db.add(report)
    await run_in_threadpool(db.flush)
    
    # Сохраняем прикреплённые файлы
    if files:
        for file in files:
            if file.filename:
                file_ext = Path(file.filename).suffix.lower()
                safe_name = f"report_{report.id}_{int(time.time())}_{secrets.token_hex(8)}{file_ext}"
                file_path = REPORTS_DIR / safe_name
                
                await save_uploaded_file_optimized(file, file_path)
                
                report_file = models.ReportFile(
                    report_id=report.id,
                    filename=safe_name,
                    original_name=file.filename,
                    file_path=str(file_path.relative_to(Path(__file__).resolve().parent)),
                    file_size=file.size,
                    file_type=file_ext[1:] if file_ext else "unknown"
                )
                db.add(report_file)
    
    await run_in_threadpool(db.commit)
    
    return RedirectResponse(f"/regional-admin/report/{report.id}", status_code=303)

@app.get("/regional-admin/report/{report_id}", response_class=HTMLResponse)
async def regional_admin_view_report(
    request: Request,
    report_id: int,
    db: Session = Depends(get_db)
):
    """Просмотр отчёта"""
    if not request.session.get("regional_admin"):
        return RedirectResponse("/regional-admin/login", status_code=303)
    
    report = await run_in_threadpool(
        lambda: db.query(models.Report).filter(models.Report.id == report_id).first()
    )
    
    if not report:
        raise HTTPException(status_code=404, detail="Отчёт не найден")
    
    # Парсим данные
    try:
        report.data = json.loads(report.data) if report.data else {}
    except:
        report.data = {}
    
    # Получаем файлы
    files = await run_in_threadpool(
        lambda: db.query(models.ReportFile).filter(models.ReportFile.report_id == report_id).all()
    )
    
    # Получаем категорию
    category = None
    if report.category_id:
        category = await run_in_threadpool(
            lambda: db.query(models.ReportCategory).filter(models.ReportCategory.id == report.category_id).first()
        )
    
    # Получаем версии
    versions = await run_in_threadpool(
        lambda: db.query(models.ReportVersion).filter(models.ReportVersion.report_id == report_id).order_by(models.ReportVersion.version_number.desc()).all()
    )
    
    # Получаем комментарии
    comments = await run_in_threadpool(
        lambda: db.query(models.ReportComment).filter(models.ReportComment.report_id == report_id).order_by(models.ReportComment.created_at.desc()).all()
    )
    
    return templates.TemplateResponse("regional_admin_report_view.html", {
        "request": request,
        "report": report,
        "category": category,
        "files": files,
        "versions": versions,
        "comments": comments,
        "months": MONTHS
    })

@app.get("/regional-admin/report/{report_id}/edit", response_class=HTMLResponse)
async def regional_admin_edit_report_page(
    request: Request,
    report_id: int,
    db: Session = Depends(get_db)
):
    """Страница редактирования отчёта"""
    if not request.session.get("regional_admin"):
        return RedirectResponse("/regional-admin/login", status_code=303)
    
    report = await run_in_threadpool(
        lambda: db.query(models.Report).filter(models.Report.id == report_id).first()
    )
    
    if not report:
        raise HTTPException(status_code=404, detail="Отчёт не найден")
    
    # Парсим данные
    try:
        report.data = json.loads(report.data) if report.data else {}
    except:
        report.data = {}
    
    categories = await run_in_threadpool(
        lambda: db.query(models.ReportCategory).filter(models.ReportCategory.is_active == True).all()
    )
    
    files = await run_in_threadpool(
        lambda: db.query(models.ReportFile).filter(models.ReportFile.report_id == report_id).all()
    )
    
    return templates.TemplateResponse("regional_admin_report_edit.html", {
        "request": request,
        "report": report,
        "categories": categories,
        "files": files,
        "months": MONTHS
    })

@app.post("/regional-admin/report/{report_id}/edit")
async def regional_admin_update_report(
    request: Request,
    report_id: int,
    title: str = Form(...),
    description: str = Form(""),
    category_id: int = Form(None),
    year: int = Form(...),
    month: int = Form(None),
    quarter: int = Form(None),
    report_data: str = Form("{}"),
    status: str = Form("draft"),
    db: Session = Depends(get_db)
):
    """Обновление отчёта"""
    if not request.session.get("regional_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    report = await run_in_threadpool(
        lambda: db.query(models.Report).filter(models.Report.id == report_id).first()
    )
    
    if not report:
        raise HTTPException(status_code=404, detail="Отчёт не найден")
    
    # Сохраняем текущую версию перед изменением
    current_version = models.ReportVersion(
        report_id=report.id,
        version_number=(await run_in_threadpool(
            lambda: db.query(models.ReportVersion).filter(models.ReportVersion.report_id == report_id).count()
        )) + 1,
        data_snapshot=json.dumps(report.data) if report.data else "{}",
        changed_at=datetime.utcnow(),
        change_comment="Автоматическое сохранение версии перед редактированием"
    )
    db.add(current_version)
    
    # Обновляем отчёт
    report.title = title
    report.description = description
    report.category_id = category_id if category_id else None
    report.year = year
    report.month = month
    report.quarter = quarter
    report.data = report_data
    report.status = status
    report.is_published = (status == "published")
    report.updated_at = datetime.utcnow()
    
    await run_in_threadpool(db.commit)
    
    return RedirectResponse(f"/regional-admin/report/{report_id}", status_code=303)

@app.post("/regional-admin/report/{report_id}/add-files")
async def regional_admin_add_report_files(
    request: Request,
    report_id: int,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db)
):
    """Добавление файлов к отчёту"""
    if not request.session.get("regional_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    report = await run_in_threadpool(
        lambda: db.query(models.Report).filter(models.Report.id == report_id).first()
    )
    
    if not report:
        raise HTTPException(status_code=404, detail="Отчёт не найден")
    
    for file in files:
        if file.filename:
            file_ext = Path(file.filename).suffix.lower()
            safe_name = f"report_{report_id}_{int(time.time())}_{secrets.token_hex(8)}{file_ext}"
            file_path = REPORTS_DIR / safe_name
            
            await save_uploaded_file_optimized(file, file_path)
            
            report_file = models.ReportFile(
                report_id=report_id,
                filename=safe_name,
                original_name=file.filename,
                file_path=str(file_path.relative_to(Path(__file__).resolve().parent)),
                file_size=file.size,
                file_type=file_ext[1:] if file_ext else "unknown"
            )
            db.add(report_file)
    
    await run_in_threadpool(db.commit)
    
    return RedirectResponse(f"/regional-admin/report/{report_id}", status_code=303)

@app.get("/regional-admin/report/{report_id}/delete-file/{file_id}")
async def regional_admin_delete_report_file(
    request: Request,
    report_id: int,
    file_id: int,
    db: Session = Depends(get_db)
):
    """Удаление файла из отчёта"""
    if not request.session.get("regional_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    file = await run_in_threadpool(
        lambda: db.query(models.ReportFile).filter(models.ReportFile.id == file_id, models.ReportFile.report_id == report_id).first()
    )
    
    if file:
        # Удаляем физический файл
        BASE_DIR = Path(__file__).resolve().parent
        file_path = BASE_DIR / file.file_path
        await delete_file_optimized(file_path)
        
        # Удаляем запись из БД
        await run_in_threadpool(lambda: db.delete(file))
        await run_in_threadpool(db.commit)
    
    return RedirectResponse(f"/regional-admin/report/{report_id}", status_code=303)

@app.post("/regional-admin/report/{report_id}/delete")
async def regional_admin_delete_report(
    request: Request,
    report_id: int,
    db: Session = Depends(get_db)
):
    """Удаление отчёта"""
    if not request.session.get("regional_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    report = await run_in_threadpool(
        lambda: db.query(models.Report).filter(models.Report.id == report_id).first()
    )
    
    if report:
        # Удаляем связанные файлы
        files = await run_in_threadpool(
            lambda: db.query(models.ReportFile).filter(models.ReportFile.report_id == report_id).all()
        )
        BASE_DIR = Path(__file__).resolve().parent
        for file in files:
            file_path = BASE_DIR / file.file_path
            await delete_file_optimized(file_path)
        
        # Удаляем отчёт (каскадно удалятся связанные записи)
        await run_in_threadpool(lambda: db.delete(report))
        await run_in_threadpool(db.commit)
    
    return RedirectResponse("/regional-admin/reports", status_code=303)

@app.post("/regional-admin/report/{report_id}/comment")
async def regional_admin_add_comment(
    request: Request,
    report_id: int,
    content: str = Form(...),
    db: Session = Depends(get_db)
):
    """Добавление комментария к отчёту"""
    if not request.session.get("regional_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    comment = models.ReportComment(
        report_id=report_id,
        user_name="Региональный администратор",
        content=content
    )
    
    db.add(comment)
    await run_in_threadpool(db.commit)
    
    return RedirectResponse(f"/regional-admin/report/{report_id}", status_code=303)

@app.get("/regional-admin/report/{report_id}/export/{format}")
async def regional_admin_export_report(
    request: Request,
    report_id: int,
    format: str,  # json, html
    db: Session = Depends(get_db)
):
    """Экспорт отчёта в разных форматах"""
    if not request.session.get("regional_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    report = await run_in_threadpool(
        lambda: db.query(models.Report).filter(models.Report.id == report_id).first()
    )
    
    if not report:
        raise HTTPException(status_code=404, detail="Отчёт не найден")
    
    try:
        report.data = json.loads(report.data) if report.data else {}
    except:
        report.data = {}
    
    if format == "json":
        return JSONResponse({
            "id": report.id,
            "title": report.title,
            "description": report.description,
            "year": report.year,
            "month": report.month,
            "quarter": report.quarter,
            "data": report.data,
            "status": report.status,
            "created_at": report.created_at.isoformat() if report.created_at else None,
            "updated_at": report.updated_at.isoformat() if report.updated_at else None
        })
    
    elif format == "html":
        # Генерируем HTML страницу с отчётом
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>{report.title}</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }}
                h1 {{ color: #333; border-bottom: 2px solid #667eea; padding-bottom: 10px; }}
                .meta {{ color: #666; margin-bottom: 20px; }}
                .data-section {{ background: #f5f5f5; padding: 20px; border-radius: 8px; margin: 20px 0; }}
                pre {{ background: #fff; padding: 15px; overflow-x: auto; }}
            </style>
        </head>
        <body>
            <h1>{report.title}</h1>
            <div class="meta">
                <strong>Год:</strong> {report.year} | 
                <strong>Статус:</strong> {report.status} |
                <strong>Создан:</strong> {report.created_at.strftime('%d.%m.%Y %H:%M') if report.created_at else '—'}
            </div>
            <p>{report.description or 'Нет описания'}</p>
            <div class="data-section">
                <h3>Данные отчёта</h3>
                <pre>{json.dumps(report.data, ensure_ascii=False, indent=2)}</pre>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html_content)
    
    else:
        raise HTTPException(status_code=400, detail="Неподдерживаемый формат экспорта")

@app.get("/regional-admin/import-report-form", response_class=HTMLResponse)
async def regional_admin_import_report_form(request: Request, db: Session = Depends(get_db)):
    """Форма для импорта отчёта"""
    if not request.session.get("regional_admin"):
        return RedirectResponse("/regional-admin/login", status_code=303)
    
    categories = await run_in_threadpool(
        lambda: db.query(models.ReportCategory).filter(models.ReportCategory.is_active == True).all()
    )
    
    return templates.TemplateResponse("regional_admin_import_report.html", {
        "request": request,
        "categories": categories
    })

@app.post("/regional-admin/import-report")
async def regional_admin_import_report(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    category_id: int = Form(None),
    year: int = Form(...),
    month: int = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """Импорт отчёта из файла (PDF, DOCX, XLSX, JSON)"""
    if not request.session.get("regional_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    # Определяем тип файла
    file_ext = Path(file.filename).suffix.lower()
    
    # Пытаемся извлечь данные из файла
    report_data = {}
    
    if file_ext == '.json':
        content = await file.read()
        try:
            report_data = json.loads(content.decode('utf-8'))
        except:
            pass
    
    # Создаём отчёт
    report = models.Report(
        title=title,
        description=description,
        category_id=category_id if category_id else None,
        year=year,
        month=month,
        data=json.dumps(report_data, ensure_ascii=False) if report_data else "{}",
        status="draft"
    )
    
    db.add(report)
    await run_in_threadpool(db.flush)
    
    # Сохраняем загруженный файл
    safe_name = f"import_{report.id}_{int(time.time())}_{secrets.token_hex(8)}{file_ext}"
    file_path = REPORTS_DIR / safe_name
    
    await save_uploaded_file_optimized(file, file_path)
    
    report_file = models.ReportFile(
        report_id=report.id,
        filename=safe_name,
        original_name=file.filename,
        file_path=str(file_path.relative_to(Path(__file__).resolve().parent)),
        file_size=file.size,
        file_type=file_ext[1:] if file_ext else "unknown"
    )
    db.add(report_file)
    
    await run_in_threadpool(db.commit)
    
    return RedirectResponse(f"/regional-admin/report/{report.id}", status_code=303)

# Добавляем ссылку на региональную админку в layout.html через контекстный процессор
@app.middleware("http")
async def add_regional_admin_link(request: Request, call_next):
    response = await call_next(request)
    return response

@app.get("/regional-admin/report/{report_id}/download-file/{file_id}")
async def regional_admin_download_report_file(
    request: Request,
    report_id: int,
    file_id: int,
    db: Session = Depends(get_db)
):
    """Скачивание файла из отчёта"""
    if not request.session.get("regional_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    file = await run_in_threadpool(
        lambda: db.query(models.ReportFile).filter(
            models.ReportFile.id == file_id,
            models.ReportFile.report_id == report_id
        ).first()
    )
    
    if not file:
        raise HTTPException(status_code=404, detail="Файл не найден")
    
    BASE_DIR = Path(__file__).resolve().parent
    file_path = BASE_DIR / file.file_path
    
    if not await run_in_threadpool(file_path.exists):
        raise HTTPException(status_code=404, detail="Файл не найден на диске")
    
    # Кодируем имя файла для корректной обработки русских символов
    import urllib.parse
    encoded_filename = urllib.parse.quote(file.original_name)
    
    return FileResponse(
        path=file_path,
        filename=file.original_name,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
        }
    )

# ========== ПУБЛИЧНЫЕ ОТЧЁТЫ ==========
@app.get("/public-reports", response_class=HTMLResponse)
async def public_reports(
    request: Request,
    category_id: str = None,
    year: str = None,
    search: str = "",
    page: int = 1,
    per_page: int = 20,
    db: Session = Depends(get_db)
):
    """Публичная страница с опубликованными отчётами"""
    
    query = db.query(models.Report).filter(
        models.Report.is_published == True,
        models.Report.status == "published"
    )
    
    # Преобразуем строки в числа, если они не пустые
    category_id_int = None
    if category_id and category_id.strip():
        try:
            category_id_int = int(category_id)
        except ValueError:
            pass
    
    year_int = None
    if year and year.strip():
        try:
            year_int = int(year)
        except ValueError:
            pass
    
    # Применяем фильтры
    if category_id_int:
        query = query.filter(models.Report.category_id == category_id_int)
    if year_int:
        query = query.filter(models.Report.year == year_int)
    if search:
        query = query.filter(
            or_(
                models.Report.title.ilike(f"%{search}%"),
                models.Report.description.ilike(f"%{search}%")
            )
        )
    
    total = await run_in_threadpool(query.count)
    offset = (page - 1) * per_page
    reports = await run_in_threadpool(
        lambda: query.order_by(models.Report.created_at.desc())
        .offset(offset).limit(per_page).all()
    )
    
    # Получаем категории для фильтра
    categories = await run_in_threadpool(
        lambda: db.query(models.ReportCategory).filter(
            models.ReportCategory.is_active == True
        ).all()
    )
    
    # Получаем доступные годы
    years_result = await run_in_threadpool(
        lambda: db.query(models.Report.year).distinct().order_by(models.Report.year.desc()).all()
    )
    years = [str(y[0]) for y in years_result if y[0]]
    
    # Загружаем данные отчётов
    for report in reports:
        try:
            report.data = json.loads(report.data) if report.data else {}
        except:
            report.data = {}
        
        if report.category_id:
            report.category = await run_in_threadpool(
                lambda: db.query(models.ReportCategory).filter(models.ReportCategory.id == report.category_id).first()
            )
    
    return templates.TemplateResponse("public_reports.html", {
        "request": request,
        "reports": reports,
        "total": total,
        "page": page,
        "per_page": per_page,
        "categories": categories,
        "years": years,
        "selected_category": category_id if category_id else "",
        "selected_year": year if year else "",
        "search": search
    })

@app.get("/public-reports/{report_id}", response_class=HTMLResponse)
async def public_report_detail(
    request: Request,
    report_id: int,
    db: Session = Depends(get_db)
):
    """Публичный просмотр отдельного отчёта"""
    
    report = await run_in_threadpool(
        lambda: db.query(models.Report).filter(
            models.Report.id == report_id,
            models.Report.is_published == True,
            models.Report.status == "published"
        ).first()
    )
    
    if not report:
        raise HTTPException(status_code=404, detail="Отчёт не найден")
    
    # Парсим данные
    try:
        report.data = json.loads(report.data) if report.data else {}
    except:
        report.data = {}
    
    # Получаем категорию
    category = None
    if report.category_id:
        category = await run_in_threadpool(
            lambda: db.query(models.ReportCategory).filter(models.ReportCategory.id == report.category_id).first()
        )
    
    # Получаем файлы
    files = await run_in_threadpool(
        lambda: db.query(models.ReportFile).filter(models.ReportFile.report_id == report_id).all()
    )
    
    return templates.TemplateResponse("public_report_detail.html", {
        "request": request,
        "report": report,
        "category": category,
        "files": files
    })

@app.get("/public-reports/download/{file_id}")
async def public_download_file(
    request: Request,
    file_id: int,
    db: Session = Depends(get_db)
):
    """Публичное скачивание файла из отчёта"""
    
    file = await run_in_threadpool(
        lambda: db.query(models.ReportFile).filter(models.ReportFile.id == file_id).first()
    )
    
    if not file:
        raise HTTPException(status_code=404, detail="Файл не найден")
    
    # Проверяем, что отчёт опубликован
    report = await run_in_threadpool(
        lambda: db.query(models.Report).filter(
            models.Report.id == file.report_id,
            models.Report.is_published == True,
            models.Report.status == "published"
        ).first()
    )
    
    if not report:
        raise HTTPException(status_code=404, detail="Файл не найден")
    
    BASE_DIR = Path(__file__).resolve().parent
    file_path = BASE_DIR / file.file_path
    
    if not await run_in_threadpool(file_path.exists):
        raise HTTPException(status_code=404, detail="Файл не найден на диске")
    
    # Увеличиваем счётчик просмотров
    report.views_count = (report.views_count or 0) + 1
    await run_in_threadpool(db.commit)
    
    # Кодируем имя файла
    import urllib.parse
    encoded_filename = urllib.parse.quote(file.original_name)
    
    return FileResponse(
        path=file_path,
        filename=file.original_name,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
        }
    )

#Поддержка ФЦМПО
# Папка для хранения данных
DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

REQUESTS_FILE = DATA_DIR / "fcmp_requests.json"

def load_requests():
    """Загрузка заявок из JSON файла"""
    try:
        if REQUESTS_FILE.exists():
            with open(REQUESTS_FILE, "r", encoding="utf-8") as f:
                content = f.read()
                if content:
                    return json.loads(content)
    except Exception as e:
        print(f"Ошибка загрузки заявок: {e}")
    return []

def save_requests(requests):
    """Сохранение заявок в JSON файл"""
    try:
        with open(REQUESTS_FILE, "w", encoding="utf-8") as f:
            json.dump(requests, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"Ошибка сохранения заявок: {e}")
        return False

# API эндпоинты
@app.post("/api/fcmp-request")
async def create_fcmp_request(request: Request):
    """Создание новой заявки"""
    try:
        data = await request.json()
        
        requests = load_requests()
        
        new_request = {
            "id": int(time.time() * 1000),
            "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "region": data.get("region", ""),
            "school": data.get("school", ""),
            "email": data.get("email", ""),
            "problem": data.get("problem", ""),
            "status": "pending",
            "reply": None,
            "reply_date": None
        }
        
        requests.append(new_request)
        save_requests(requests)
        
        return {"status": "success", "id": new_request["id"]}
    except Exception as e:
        print(f"Ошибка создания заявки: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/fcmp-admin-login")
async def fcmp_admin_login(request: Request):
    """Вход в админ-панель"""
    try:
        data = await request.json()
        code = data.get("code", "")
        
        if code == "":#напиши код для доступа к панели заявок
            request.session["fcmp_admin"] = True
            return {"status": "success"}
        return {"status": "error", "message": "Неверный код"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/fcmp-complete-request")
async def complete_fcmp_request(request: Request):
    """Отметить заявку как выполненную"""
    if not request.session.get("fcmp_admin"):
        return {"status": "error", "message": "Не авторизован"}
    
    try:
        data = await request.json()
        request_id = data.get("id")
        
        requests = load_requests()
        found = False
        for req in requests:
            if req["id"] == request_id:
                req["status"] = "completed"
                found = True
                break
        
        if found:
            save_requests(requests)
            return {"status": "success"}
        return {"status": "error", "message": "Заявка не найдена"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/fcmp-reply-request")
async def reply_fcmp_request(request: Request):
    """Ответить на заявку"""
    if not request.session.get("fcmp_admin"):
        return {"status": "error", "message": "Не авторизован"}
    
    try:
        data = await request.json()
        request_id = data.get("id")
        reply = data.get("reply", "")
        
        requests = load_requests()
        found = False
        for req in requests:
            if req["id"] == request_id:
                req["reply"] = reply
                req["reply_date"] = datetime.now().strftime("%d.%m.%Y %H:%M")
                found = True
                break
        
        if found:
            save_requests(requests)
            return {"status": "success"}
        return {"status": "error", "message": "Заявка не найдена"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/fcmp-delete-request")
async def delete_fcmp_request(request: Request):
    """Удалить заявку"""
    if not request.session.get("fcmp_admin"):
        return {"status": "error", "message": "Не авторизован"}
    
    try:
        data = await request.json()
        request_id = data.get("id")
        
        requests = load_requests()
        new_requests = [req for req in requests if req["id"] != request_id]
        
        if len(new_requests) != len(requests):
            save_requests(new_requests)
            return {"status": "success"}
        return {"status": "error", "message": "Заявка не найдена"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# Страница ФЦМПО
@app.get("/fcmp-support", response_class=HTMLResponse)
async def fcmp_support_page(request: Request):
    """Страница базы данных ФЦМПО"""
    requests = load_requests()
    is_admin = request.session.get("fcmp_admin", False)
    
    return templates.TemplateResponse("fcmp_support.html", {
        "request": request,
        "requests": requests,
        "is_admin": is_admin,
        "session": request.session
    })

# ========== ВИДЕОИНСТРУКЦИИ ==========
VIDEOS_FILE = DATA_DIR / "videos.json"

def load_videos():
    """Загрузка видео из JSON файла"""
    try:
        if VIDEOS_FILE.exists():
            with open(VIDEOS_FILE, "r", encoding="utf-8") as f:
                content = f.read()
                if content:
                    return json.loads(content)
    except Exception as e:
        print(f"Ошибка загрузки видео: {e}")
    return []

def save_videos(videos):
    """Сохранение видео в JSON файл"""
    try:
        with open(VIDEOS_FILE, "w", encoding="utf-8") as f:
            json.dump(videos, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"Ошибка сохранения видео: {e}")
        return False


# API для получения видео
@app.get("/api/videos")
async def get_videos():
    """Получение списка видео"""
    videos = load_videos()
    return {"videos": videos}

# API для сохранения видео
@app.post("/api/videos")
async def save_videos_api(request: Request):
    """Сохранение списка видео"""
    try:
        data = await request.json()
        videos = data.get("videos", [])
        save_videos(videos)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8000,
        workers=4,
        loop="asyncio"
    )