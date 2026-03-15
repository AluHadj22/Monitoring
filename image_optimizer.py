import asyncio
from pathlib import Path
from PIL import Image
import io
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
import os

# Создаем отдельный executor для обработки изображений
IMAGE_EXECUTOR = ThreadPoolExecutor(max_workers=4)

# Настройки оптимизации
THUMBNAIL_SIZES = {
    'small': (100, 100),    # Для превью в списках
    'medium': (300, 300),    # Для карточек
    'large': (800, 800),     # Для просмотра
}

# Качество сжатия
JPEG_QUALITY = 85
PNG_COMPRESSION = 6

async def optimize_image_async(
    input_path: Path,
    output_path: Path = None,
    max_size: tuple = (1200, 1200),
    quality: int = 85
):
    """
    Асинхронная оптимизация изображения
    """
    if output_path is None:
        output_path = input_path.parent / f"optimized_{input_path.name}"
    
    # Запускаем в отдельном потоке, чтобы не блокировать asyncio
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
                
                # Возвращаем информацию о размере
                original_size = input_path.stat().st_size
                new_size = output_path.stat().st_size
                
                return {
                    'original_size': original_size,
                    'new_size': new_size,
                    'saved_percent': (1 - new_size/original_size) * 100,
                    'output_path': output_path
                }
        except Exception as e:
            print(f"Ошибка оптимизации {input_path}: {e}")
            return None
    
    return await loop.run_in_executor(IMAGE_EXECUTOR, _optimize)

async def generate_thumbnails_async(
    image_path: Path,
    output_dir: Path = None
):
    """
    Генерация нескольких версий изображения
    """
    if output_dir is None:
        output_dir = image_path.parent / 'thumbnails'
    
    output_dir.mkdir(exist_ok=True)
    
    # Базовое имя файла без расширения
    stem = image_path.stem
    ext = image_path.suffix
    
    tasks = []
    results = {}
    
    for size_name, dimensions in THUMBNAIL_SIZES.items():
        output_path = output_dir / f"{stem}_{size_name}{ext}"
        
        async def _create_thumbnail(sname=size_name, dim=dimensions, out=output_path):
            result = await optimize_image_async(
                image_path,
                output_path=out,
                max_size=dim,
                quality=75 if sname == 'small' else 85
            )
            return sname, result
        
        tasks.append(_create_thumbnail())
    
    # Ждем завершения всех задач
    thumbnails = await asyncio.gather(*tasks)
    
    return dict(thumbnails)

async def get_optimized_image_url(
    original_path: Path,
    size: str = 'medium',
    base_url: str = ''
):
    """
    Получение URL оптимизированной версии изображения
    """
    thumb_dir = original_path.parent / 'thumbnails'
    stem = original_path.stem
    ext = original_path.suffix
    
    thumbnail_path = thumb_dir / f"{stem}_{size}{ext}"
    
    # Если уменьшенная версия не существует - создаем
    if not await asyncio.to_thread(thumbnail_path.exists):
        await generate_thumbnails_async(original_path)
    
    # Формируем URL
    relative_path = thumbnail_path.relative_to(Path(__file__).parent)
    return f"{base_url}/{relative_path}".replace('\\', '/')

class ImageCache:
    """Кеш для обработанных изображений"""
    
    def __init__(self, cache_dir: Path, max_size_mb: int = 500):
        self.cache_dir = Path(cache_dir) / 'image_cache'
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_size_mb = max_size_mb
        self._cache_info = {}
        
    def _get_cache_key(self, original_path: Path, size: str) -> str:
        """Создание ключа кеша на основе содержимого файла"""
        stat = original_path.stat()
        # Комбинируем путь, размер, время изменения и запрошенный размер
        key = f"{original_path}_{stat.st_size}_{stat.st_mtime}_{size}"
        return hashlib.md5(key.encode()).hexdigest()
    
    async def get_or_create_thumbnail(
        self,
        original_path: Path,
        size: str = 'medium'
    ) -> Path:
        """Получение из кеша или создание уменьшенной версии"""
        
        cache_key = self._get_cache_key(original_path, size)
        cached_path = self.cache_dir / f"{cache_key}_{size}{original_path.suffix}"
        
        # Проверяем наличие в кеше
        if await asyncio.to_thread(cached_path.exists):
            return cached_path
        
        # Создаем уменьшенную версию
        dimensions = THUMBNAIL_SIZES.get(size, THUMBNAIL_SIZES['medium'])
        
        await optimize_image_async(
            original_path,
            output_path=cached_path,
            max_size=dimensions,
            quality=75 if size == 'small' else 85
        )
        
        # Очистка кеша при превышении лимита
        await self._cleanup_if_needed()
        
        return cached_path
    
    async def _cleanup_if_needed(self):
        """Очистка кеша при превышении лимита"""
        total_size = 0
        files = []
        
        for file_path in self.cache_dir.glob('*'):
            if file_path.is_file():
                size = file_path.stat().st_size
                total_size += size
                files.append((file_path, size))
        
        # Если превышен лимит - удаляем самые старые файлы
        if total_size > self.max_size_mb * 1024 * 1024:
            # Сортируем по времени создания
            files.sort(key=lambda x: x[0].stat().st_ctime)
            
            # Удаляем пока не освободим 30% места
            target_size = total_size * 0.7  # оставляем 70%
            current_size = total_size
            
            for file_path, size in files:
                if current_size <= target_size:
                    break
                try:
                    file_path.unlink()
                    current_size -= size
                except:
                    pass

# Создаем глобальный экземпляр кеша
IMAGE_CACHE = ImageCache(Path(__file__).parent / 'cache')