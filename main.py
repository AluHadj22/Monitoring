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

# Файловый ввод‑вывод
import aiofiles
import aiofiles.os
import shutil
from fastapi.staticfiles import StaticFiles

# Веб‑фреймворк и HTTP
from fastapi import (
    FastAPI,
    Request,
    Form,
    File,
    UploadFile,
    Depends,
    HTTPException
)
from fastapi.responses import (
    HTMLResponse,
    FileResponse,
    StreamingResponse,
    RedirectResponse  # ← Теперь здесь!
)
from fastapi.templating import Jinja2Templates
from fastapi.middleware.gzip import GZipMiddleware

# База данных
from sqlalchemy.orm import Session
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
from openpyxl.utils.exceptions import InvalidFileException  # ← добавлен

# Кеширование
from cachetools import TTLCache

# Асинхронные инструменты
from concurrent.futures import ThreadPoolExecutor

# Переменные окружения
from dotenv import load_dotenv

import logging  # ← добавлен

# Загрузка переменных из .env
load_dotenv()


# Создаёт таблицы в БД, если их нет
Base.metadata.create_all(bind=engine)

# Глобальные кеши для высокой нагрузки
MANIFEST_CACHE = TTLCache(maxsize=5000, ttl=300)  # Кеш манифестов
USER_CACHE = TTLCache(maxsize=1000, ttl=180)      # Кеш пользователей
FILE_EXISTS_CACHE = TTLCache(maxsize=10000, ttl=60) # Кеш проверки файлов

# ThreadPool для блокирующих операций
IO_EXECUTOR = ThreadPoolExecutor(max_workers=50)

# Глобальная блокировка для кешей
CACHE_LOCK = asyncio.Lock()

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Запуск оптимизированного приложения...")
    yield
    print("🔧 Очистка ресурсов...")
    MANIFEST_CACHE.clear()
    USER_CACHE.clear()
    FILE_EXISTS_CACHE.clear()
    IO_EXECUTOR.shutdown()

app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=100)  # Сжатие для экономии трафика

templates = Jinja2Templates(directory="templates")
# Подключаем папку static/ по пути /static
app.mount("/static", StaticFiles(directory="static"), name="static")

#ДЛЯ ПЕРСОНАЛЬНЫХ ДАННЫХ
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

# Константы
FOOD_TYPES = ["Только завтраки", "Завтраки и обеды", "Интернаты", "Обеды"] #типы питаний
DISTRICTS = [
    "Аргун", "Ачхой-Мартановский", "Веденский", "Грозненский", "Грозный",
    "Гудермесский", "Гудермес", "Итум-Калинский", "Курчалоевский", "Надтеречный",
    "Наурский", "Ножай-Юртовский", "Серноводский", "Урус-Мартановский",
    "Шалинский", "Шаройский", "Шатойский", "Шелковской"
]#тут мои районы, можно указать свои
MONTHS = {
    "01": "Январь", "02": "Февраль", "03": "Март", "04": "Апрель",
    "05": "Май", "06": "Июнь", "07": "Июль", "08": "Август",
    "09": "Сентябрь", "10": "Октябрь", "11": "Ноябрь", "12": "Декабрь"
}

# Секретные коды для регистрации админов
REGIONAL_CODE = '' #твой код регионального
MUNICIPAL_CODE = '' #твой код районного админа

# Настраиваем логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

#Функция для обновления данных внутри файлов excel

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


# Оптимизированные утилиты
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
    
    # Обновляем кеш существования файла
    cache_key = str(dest_path)
    async with CACHE_LOCK:
        FILE_EXISTS_CACHE[cache_key] = True

async def delete_file_optimized(file_path: Path):
    """Оптимизированное удаление файла с очисткой кешей"""
    try:
        if await run_in_threadpool(file_path.exists):
            await run_in_threadpool(file_path.unlink)
            
            # Очищаем кеш существования файла
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

async def generate_federal_html_stream(uid: int, base_path: Path, manifest: dict):
    """Потоковая генерация HTML для федерального мониторинга"""
    yield f"""
    <html>
        <head>
            <meta charset="utf-8">
            <title>Файлы учреждения {uid}</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f5f5f5; }}
                .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
                h1 {{ color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
                .year-section {{ margin: 20px 0; padding: 15px; background: #f8f9fa; border-radius: 6px; }}
                .month-section {{ margin: 10px 0; padding: 10px; background: #fff; border-left: 4px solid #3498db; }}
                .file-list {{ list-style: none; padding: 0; }}
                .file-item {{ padding: 8px 12px; margin: 5px 0; background: #f8f9fa; border-radius: 4px; display: flex; justify-content: space-between; align-items: center; }}
                .file-link {{ color: #2980b9; text-decoration: none; font-weight: bold; }}
                .file-link:hover {{ color: #1a5276; text-decoration: underline; }}
                .file-date {{ color: #7f8c8d; font-size: 0.9em; }}
                .no-files {{ color: #95a5a6; font-style: italic; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>📁 Файлы учреждения {uid}</h1>
                <hr>
    """
    
    files = await list_directory_files_optimized(base_path)
    grouped_files = {}
    
    for f in files:
        if f.name == "manifest.json":
            continue
            
        file_meta = manifest.get(f.name, {})
        date_str = file_meta.get("upload_datetime", "")
        
        try:
            dt = datetime.strptime(date_str, "%d.%m.%Y %H:%M") if date_str else datetime.fromtimestamp(await run_in_threadpool(f.stat).st_mtime)
        except Exception:
            dt = datetime.now()
        
        assigned_year = file_meta.get("assigned_year", str(dt.year))
        assigned_month = file_meta.get("assigned_month", dt.strftime("%m"))
        month_name = MONTHS.get(assigned_month, assigned_month)

        grouped_files.setdefault(assigned_year, {}).setdefault(month_name, []).append({
            "filename": f.name,
            "date": dt.strftime("%d.%m.%Y %H:%M"),
            "size": await run_in_threadpool(f.stat).st_size
        })
    
    # Сортировка и потоковая выдача
    for year in sorted(grouped_files.keys(), reverse=True):
        yield f'<div class="year-section"><h2>📅 {year} год</h2>'
        
        for month in sorted(grouped_files[year].keys(), reverse=True):
            yield f'<div class="month-section"><h3>📊 {month}</h3><ul class="file-list">'
            
            for file_info in sorted(grouped_files[year][month], key=lambda x: x["date"], reverse=True):
                size_kb = file_info["size"] // 1024
                yield (
                    f'<li class="file-item">'
                    f'<a class="file-link" href="{file_info["filename"]}">📄 {file_info["filename"]}</a>'
                    f'<div><span class="file-date">{file_info["date"]}</span>'
                    f'<span style="margin-left: 15px; color: #27ae60;">{size_kb} KB</span></div>'
                    f'</li>'
                )
            
            yield '</ul></div>'
        yield '</div>'
    
    if not grouped_files:
        yield '<div class="no-files">📭 Нет доступных файлов</div>'
    
    yield '</div></body></html>'

# Middleware для мониторинга производительности
@app.middleware("http")
async def performance_middleware(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    
    if process_time > 1.0:  # Логируем только медленные запросы
        print(f"⏱️ SLOW_REQUEST: {request.method} {request.url} - {process_time:.3f}s")
    
    response.headers["X-Process-Time"] = f"{process_time:.3f}s"
    return response

# --- ФЕДЕРАЛЬНЫЙ МОНИТОРИНГ (ОПТИМИЗИРОВАННЫЙ) ---

@app.get("/{uid}/food/", response_class=HTMLResponse)
async def federal_index(uid: int):
    """Оптимизированная версия для федерального мониторинга"""
    BASE_DIR = Path(__file__).resolve().parent
    base_path = BASE_DIR / str(uid) / "food"

    # Быстрая проверка существования директории
    if not await run_in_threadpool(base_path.exists):
        return HTMLResponse(content="<html><body><h1>📭 Нет доступных файлов</h1></body></html>")

    # Кешированное чтение manifest
    manifest_path = base_path / "manifest.json"
    manifest = await read_manifest_optimized(manifest_path)

    # Потоковая генерация HTML
    return StreamingResponse(
        generate_federal_html_stream(uid, base_path, manifest),
        media_type="text/html"
    )

@app.get("/{uid}/food/{filename}")
async def get_federal_file(uid: int, filename: str):
    """Оптимизированная отдача файлов с кешированием"""
    BASE_DIR = Path(__file__).resolve().parent
    file_path = BASE_DIR / str(uid) / "food" / filename

    # Кешированная проверка существования файла
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

# --- РЕГИСТРАЦИЯ И АВТОРИЗАЦИЯ (ОПТИМИЗИРОВАННЫЕ) ---

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
    director_name: str = Form(...),  # Новое поле!
    district: str = Form(...),
    food_type: str = Form(...),
    secret_code: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    # Проверка существования пользователя
    existing_user = await run_in_threadpool(
        lambda: db.query(models.User).filter(models.User.email == email).first()
    )
    if existing_user:
        return "Пользователь с таким email уже существует"

    # Определение роли
    role = "user"
    if secret_code == REGIONAL_CODE:
        role = "regional_admin"
    elif secret_code == MUNICIPAL_CODE:
        role = "municipal_admin"

    # Создание пользователя
    hashed_pw = auth.get_password_hash(password)
    new_user = models.User(
        email=email,
        hashed_password=hashed_pw,
        unit_name=unit_name,
        director_name=director_name,  # Сохраняем ФИО директора
        district=district,
        food_type=food_type,
        role=role
    )

    await run_in_threadpool(lambda: db.add(new_user))
    await run_in_threadpool(db.commit)
    await run_in_threadpool(db.refresh, new_user)

    # Создание директорий
    BASE_DIR = Path(__file__).resolve().parent
    school_dir = BASE_DIR / str(new_user.id)
    food_dir = school_dir / "food"
    await run_in_threadpool(lambda: food_dir.mkdir(parents=True, exist_ok=True))

    return RedirectResponse("/login", status_code=303)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = await run_in_threadpool(
        lambda: db.query(models.User).filter(models.User.email == email).first()
    )
    
    if not user or not auth.verify_password(password, user.hashed_password):
        return "Неверный логин или пароль"

    if "admin" in user.role:
        return RedirectResponse(f"/admin?admin_id={user.id}", status_code=303)
    return RedirectResponse(f"/dashboard?uid={user.id}", status_code=303)

# --- АДМИН-ПАНЕЛЬ И РАССЫЛКА (ОПТИМИЗИРОВАННЫЕ) ---

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request, admin_id: int, q: str = "", db: Session = Depends(get_db)):
    admin = await get_cached_user(admin_id, db)
    if not admin:
        return RedirectResponse("/login")

    # Оптимизированный запрос с фильтрацией
    query = db.query(models.User).filter(models.User.role == "user")
    if admin.role == "municipal_admin":
        query = query.filter(models.User.district == admin.district)

    if q:
        query = query.filter(models.User.unit_name.ilike(f"%{q}%"))

    schools = await run_in_threadpool(query.all)

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "admin": admin,
        "schools": schools,
        "food_types": FOOD_TYPES,
        "months": MONTHS,
        "search_query": q
    })

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

    # 1. Сохраняем оригиналы всех файлов во временную папку
    temp_uploads = BASE_DIR / "temp_uploads"
    await asyncio.to_thread(lambda: temp_uploads.mkdir(parents=True, exist_ok=True))

    original_paths = {}
    for file in files:
        if not file.filename:
            continue
        orig_path = temp_uploads / file.filename
        await save_uploaded_file_optimized(file, orig_path)
        original_paths[file.filename] = orig_path

    # 2. Для каждой школы — своя копия оригинала + модификация
    for school in schools:
        food_path = BASE_DIR / str(school.id) / "food"
        await run_in_threadpool(lambda: food_path.mkdir(parents=True, exist_ok=True))
        manifest_path = food_path / "manifest.json"

        manifest = await read_manifest_optimized(manifest_path)

        for file in files:
            if not file.filename:
                continue

            # Берём оригинал из сохранённых
            orig_path = original_paths[file.filename]
            dest_path = food_path / file.filename

            # Копируем оригинал в целевую папку (не модифицированный!)
            await asyncio.to_thread(lambda: shutil.copy2(orig_path, dest_path))

            # Определяем тип файла и дату
            file_type = None
            date_str = None
            if file.filename.startswith("tm"):
                file_type = "tm"
            elif file.filename.startswith("kp"):
                file_type = "kp"
            else:
                try:
                    date_parts = file.filename.split("-")[:3]
                    date_str = f"{date_parts[0]}-{date_parts[1]}-{date_parts[2]}"
                except:
                    pass

            # Модифицируем именно dest_path (копию для этой школы)
            await update_excel_content(
                dest_path,
                school.unit_name,
                school.director_name,
                year,
                date_str
            )

            manifest[file.filename] = {
                "assigned_year": year,
                "assigned_month": month,
                "uploader_name": uploader_name,
                "uploader_ip": uploader_ip,
                "upload_datetime": get_msk_time().strftime("%d.%m.%Y %H:%M")
            }

        await write_manifest_optimized(manifest_path, manifest)

    # 3. Очищаем временные оригиналы
    try:
        await asyncio.to_thread(lambda: shutil.rmtree(temp_uploads))
    except:
        pass

    return RedirectResponse(f"/admin?admin_id={admin_id}", status_code=303)


# --- ЛИЧНЫЙ КАБИНЕТ ШКОЛЫ (ОПТИМИЗИРОВАННЫЙ) ---

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    uid: int,
    year: str = "2025",
    month: str = "05",
    db: Session = Depends(get_db)
):
    user = await get_cached_user(uid, db)
    if not user:
        return RedirectResponse("/login")

    BASE_DIR = Path(__file__).resolve().parent
    food_path = BASE_DIR / str(uid) / "food"
    await run_in_threadpool(lambda: food_path.mkdir(parents=True, exist_ok=True))

    # Кешированное чтение manifest
    manifest_path = food_path / "manifest.json"
    manifest = await read_manifest_optimized(manifest_path)

    # Принудительное обновление кеша (на случай ручных изменений manifest)
    async with CACHE_LOCK:
        MANIFEST_CACHE[str(manifest_path)] = manifest.copy()

    # Оптимизированное получение файлов
    files = await list_directory_files_optimized(food_path)
    grouped_files = {}

    for f in files:
        if f.name == "manifest.json":
            continue

        file_meta = manifest.get(f.name, {})
        upload_time = file_meta.get("upload_datetime", get_msk_time().strftime("%d.%m.%Y %H:%M"))

        # Исправленный fallback: используем текущую дату, если поля отсутствуют
        assigned_year = file_meta.get("assigned_year", str(get_msk_time().year))
        assigned_month = file_meta.get("assigned_month", f"{get_msk_time().month:02d}")
        uploader_name = file_meta.get("uploader_name", user.unit_name)
        uploader_ip = file_meta.get("uploader_ip", "—")
        month_name = MONTHS.get(assigned_month, assigned_month)

        grouped_files.setdefault(assigned_year, {}).setdefault(month_name, []).append({
            "filename": f.name,
            "date": upload_time,
            "uploader": uploader_name,
            "ip": uploader_ip,
        })

    monitoring_url = f"{request.base_url}{uid}/food/"

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "files_grouped": grouped_files,
        "period": f"{year}-{month}",
        "year": year,
        "month": month,
        "months": MONTHS,
        "monitoring_url": monitoring_url
    })

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

    # Параллельная обработка файлов
    for file in files:
        if not file.filename:
            continue
        
        dest_path = food_path / file.filename
        await save_uploaded_file_optimized(file, dest_path)

        manifest[file.filename] = {
            "assigned_year": year,
            "assigned_month": month,
            "uploader_name": uploader_name,
            "uploader_ip": client_ip,
            "upload_datetime": get_msk_time().strftime("%d.%m.%Y %H:%M")
        }

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

    # Параллельное удаление файлов
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
    director_name: str = Form(""),  # Дефолтное значение — пустая строка
    unit_name: str | None = Form(None),
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(models.User.id == uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # Обновляем только если значение передано (и не пустое для unit_name)
    if director_name != "":  # Если не пустая строка
        user.director_name = director_name

    if unit_name is not None and unit_name.strip() != "":
        user.unit_name = unit_name.strip()

    db.commit()
    db.refresh(user)

    return RedirectResponse(f"/dashboard?uid={uid}", status_code=303)




# Валидация email 
def is_valid_email(email: str) -> bool:
    if not email or '@' not in email:
        return False
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return re.match(pattern, email) is not None

# Определение SMTP‑конфигурации по домену
def get_smtp_config(email: str) -> dict:
    domain = email.lower().split('@')[-1]

    # Настройки для популярных провайдеров
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

    # Для неизвестных доменов: пробуем стандартный SMTP
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

        reset_url = f"https://АДРЕС/reset-password/{token}" #ТУТ Я УКАЗЫВАЮ СВОЕ ДОМЕННОЕ ИМЯ, ТЕБЕ НАДО ТВОЕ
        safe_email = email.replace('<', '&lt;').replace('>', '&gt;')

        # HTML-письмо
        html_content = f"""
        <html>
        <body>
            <h2>Сброс пароля</h2>
            <p>Чтобы сменить пароль, перейдите по ссылке:</p>
            <a href="{reset_url}">Сменить пароль</a>
            <p>Ссылка действительна 1 час.</p>
            <p>Ваш email: {safe_email}</p>
        </body>
        </html>
        """
        
        message = MIMEText(f"""
<html>
<body>
    <h2>Сброс пароля</h2>
    <p>Чтобы сменить пароль, перейдите по ссылке:</p>
    <a href="{reset_url}">Сменить пароль</a>
    <p>Ссылка действительна 1 час.</p>
    <p>Ваш email: {safe_email}</p>
</body>
</html>
""", "html", "utf-8")  # Тип "html", а не "plain"
        message["Subject"] = "Сброс пароля"
        message["From"] = os.getenv("SMTP_USERNAME")  # Простая строка!
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

# СБРОС ПАРОЛЯ
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

    # Проверка минимальной длины пароля
    if len(password) < 6:
        raise HTTPException(
            status_code=400,
            detail="Пароль должен быть не менее 6 символов"
        )

    # Хешируем новый пароль
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

# Эндпоинт для проверки здоровья
@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": get_msk_time().isoformat(),
        "cache_stats": {
            "manifest_cache": len(MANIFEST_CACHE),
            "user_cache": len(USER_CACHE),
            "file_exists_cache": len(FILE_EXISTS_CACHE)
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8000,
        workers=4,
        loop="asyncio"
    )
