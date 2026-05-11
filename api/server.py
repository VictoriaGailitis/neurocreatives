import asyncio
from fastapi import FastAPI, Depends, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi import Request
from sqlalchemy import or_, text as sql_text
from sqlalchemy.orm import Session, joinedload
from typing import List, Optional, Dict, Any
import os
import logging
import time
from datetime import datetime
import json

from db.database import (
    get_db_session,
    get_database,
    init_database,
    init_default_settings,
    seed_channels_from_env,
)
from db.models import (
    Channel, CrossChannelCitation, Post, Image, Analysis, Settings, ScheduleLog,
)
from parser.telegram_parser import TelegramParser
from parser.channel_discovery import (
    ChannelDiscovery,
    info_to_dict,
    load_criteria_from_settings,
    load_search_keywords_from_settings,
    upsert_channel,
)
from ai.image_analysis import ImageAnalyzer
from scheduler import scheduler
import config


app = FastAPI(
    title="Neurocreatives",
    description="Платформа для сбора и анализа рекламных креативов из Telegram",
    version="1.0.0"
)

# Настройка статических файлов и шаблонов
app.mount("/static", StaticFiles(directory="web/static"), name="static")
app.mount("/downloads", StaticFiles(directory="downloads"), name="downloads")
templates = Jinja2Templates(directory="web/templates")


# Глобальные переменные для отслеживания статуса задач.
# Обогащённая структура: содержит флаги, временные метки и информацию о
# последней ошибке/прогрессе, чтобы фронтенд мог отображать статус-бар.
def _initial_job_status() -> dict:
    return {
        "running": False,
        "message": "Готов к запуску",
        "started_at": None,        # ISO-строка времени старта текущего/последнего запуска
        "finished_at": None,       # ISO-строка времени завершения последнего запуска
        "duration_seconds": None,  # длительность последнего завершённого запуска
        "last_status": None,       # 'success' | 'error' | None
        "last_error": None,        # текст последней ошибки (если была)
        "progress": None,          # человекочитаемый прогресс ("12/40")
        "run_type": None,          # 'manual' | 'auto'
    }


parser_status = _initial_job_status()
analysis_status = _initial_job_status()


def _job_started(status_obj: dict, message: str, run_type: str = "manual") -> None:
    now = datetime.utcnow().isoformat()
    status_obj["running"] = True
    status_obj["message"] = message
    status_obj["started_at"] = now
    status_obj["finished_at"] = None
    status_obj["duration_seconds"] = None
    status_obj["last_error"] = None
    status_obj["progress"] = None
    status_obj["run_type"] = run_type


def _job_finished(status_obj: dict, message: str, success: bool, error: Optional[str] = None) -> None:
    finished = datetime.utcnow()
    status_obj["running"] = False
    status_obj["message"] = message
    status_obj["finished_at"] = finished.isoformat()
    started_iso = status_obj.get("started_at")
    if started_iso:
        try:
            started = datetime.fromisoformat(started_iso)
            status_obj["duration_seconds"] = round((finished - started).total_seconds(), 1)
        except Exception:
            status_obj["duration_seconds"] = None
    status_obj["last_status"] = "success" if success else "error"
    status_obj["last_error"] = error

# Глобальный список для хранения последних логов
log_buffer = []
MAX_LOG_BUFFER_SIZE = 500

# Настройка логгера для перехвата логов
class LogCapture(logging.Handler):
    """Обработчик для перехвата логов в буфер."""
    
    def emit(self, record):
        try:
            log_entry = {
                'timestamp': datetime.fromtimestamp(record.created).strftime('%H:%M:%S'),
                'level': record.levelname,
                'message': self.format(record)
            }
            log_buffer.append(log_entry)
            # Ограничиваем размер буфера
            if len(log_buffer) > MAX_LOG_BUFFER_SIZE:
                log_buffer.pop(0)
        except Exception:
            pass

# Настраиваем корневой логгер
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Добавляем наш обработчик
log_capture = LogCapture()
log_capture.setLevel(logging.INFO)
logger.addHandler(log_capture)

# Также добавляем для uvicorn логгера
uvicorn_logger = logging.getLogger("uvicorn")
uvicorn_logger.addHandler(log_capture)
uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.addHandler(log_capture)


def add_log(level: str, message: str):
    """Добавить лог в буфер напрямую."""
    log_entry = {
        'timestamp': datetime.now().strftime('%H:%M:%S'),
        'level': level,
        'message': message
    }
    log_buffer.append(log_entry)
    if len(log_buffer) > MAX_LOG_BUFFER_SIZE:
        log_buffer.pop(0)


def _fix_post_url(post: Post) -> str:
    """Возвращает корректный URL поста.

    В старых записях post_url мог быть сохранён как https://t.me/None/<id> —
    это случалось для приватных каналов и каналов без публичного username
    (баг исправлен в parser/telegram_parser.py). Здесь мы стараемся восстановить
    рабочую ссылку на лету для уже накопленных данных.
    """
    url = post.post_url or ""
    if url and "/None/" not in url and "@None" not in url:
        return url
    # 1) Пробуем username из связанной записи Channel
    username = None
    try:
        if post.channel_obj is not None:
            username = (post.channel_obj.username or "").strip().lstrip("@") or None
    except Exception:
        username = None
    # 2) Если канал публичный — у него поле `channel` обычно содержит title.
    #    В качестве fallback используем slug по telegram_post_id через c-ссылку.
    if username:
        return f"https://t.me/{username}/{post.telegram_post_id}"
    return url  # вернём как есть, фронт сам решит, что показать


def _parse_channels_param(value: Optional[str]) -> List[str]:
    """Разобрать значение параметра ``channel`` в список каналов.

    Поддерживается:
      * одиночное значение (``channel=foo``);
      * список через запятую (``channel=foo,bar``);
      * повторное использование query-параметра (FastAPI склеит в строку).

    Пустые элементы и дубликаты отбрасываются, регистр сохраняется.
    """
    if not value:
        return []
    raw = [v.strip().lstrip('@') for v in str(value).split(',')]
    seen: set = set()
    out: List[str] = []
    for v in raw:
        if not v:
            continue
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def _relevant_creative_filter():
    """SQLAlchemy-выражение «пост потенциально подходит под целевой креатив».

    Совпадает с критериями, по которым пайплайн анализа решает, генерировать
    ли target_creative_description (см. :func:`ai.image_classifier.evaluate`):
      * is_ad_creative IS TRUE,
      * не скриншот текста,
      * не новостное фото,
      * нет этик-флага,
      * solo_image_appeal >= 4 (или ещё не оценено).

    Дополнительно мы НЕ требуем target_creative_ready, чтобы лента работала
    и для постов, у которых описание ещё не сгенерировано — но всё равно
    сужаем выдачу до «годных» кандидатов.
    """
    return (
        (Analysis.is_ad_creative == True)  # noqa: E712
        & (
            (Analysis.is_text_screenshot.is_(None))
            | (Analysis.is_text_screenshot == False)  # noqa: E712
        )
        & (
            (Analysis.is_news_photo.is_(None))
            | (Analysis.is_news_photo == False)  # noqa: E712
        )
        & (
            (Analysis.ethics_flag.is_(None))
            | (Analysis.ethics_flag == False)  # noqa: E712
        )
        & (
            (Analysis.solo_image_appeal.is_(None))
            | (Analysis.solo_image_appeal >= 4)
        )
        & (
            (Analysis.moderation_status.is_(None))
            | (Analysis.moderation_status != "rejected")
        )
    )


def _parse_tags(value: Optional[str]) -> List[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
        if isinstance(data, list):
            return [str(x) for x in data]
    except (json.JSONDecodeError, TypeError):
        pass
    return [t.strip() for t in str(value).split(',') if t.strip()]


def _serialize_analysis(a: Analysis) -> dict:
    """Convert an Analysis row to API JSON, including all Phase 3/4 fields."""
    return {
        "scene": a.scene,
        "objects": a.objects,
        "emotion": a.emotion,
        "creative_type": a.creative_type,
        "text_present": a.text_present,
        "visual_strength_score": a.visual_strength_score,
        "is_ad_creative": a.is_ad_creative,
        "is_text_screenshot": a.is_text_screenshot,
        "is_news_photo": a.is_news_photo,
        "is_infographic": a.is_infographic,
        "has_brand_logo": a.has_brand_logo,
        "has_overlay_text": a.has_overlay_text,
        "ethics_flag": a.ethics_flag,
        "ethics_reason": a.ethics_reason,
        "image_is_key_factor": a.image_is_key_factor,
        "solo_image_appeal": a.solo_image_appeal,
        "tags": _parse_tags(a.tags),
        "target_creative_description": a.target_creative_description,
        "target_creative_ready": a.target_creative_ready,
        "moderation_status": a.moderation_status,
        "moderation_comment": a.moderation_comment,
        "moderated_at": a.moderated_at.isoformat() if a.moderated_at else None,
    }


@app.on_event("startup")
async def startup_event():
    """Инициализация при запуске приложения."""
    add_log("INFO", "🚀 Запуск Neurocreatives...")
    logger.info("🚀 Запуск Neurocreatives...")
    
    # Инициализация базы данных
    db = init_database(config.DATABASE_URL)
    db.create_tables()
    
    # Инициализация настроек по умолчанию
    init_default_settings()

    # Однократно подсыпаем каналы из .env (CHANNELS_TO_PARSE) в БД-реестр.
    # После этого источником истины является таблица `channels` — её можно
    # редактировать из UI (добавлять/удалять/переименовывать), и эти
    # изменения переживают рестарт контейнера независимо от значения .env.
    try:
        seed_channels_from_env(config.CHANNELS_TO_PARSE)
    except Exception as exc:
        logger.warning(f"Не удалось сидировать каналы из .env: {exc}")
    
    # Создаем папку для загрузок
    os.makedirs("downloads", exist_ok=True)
    
    # Запуск планировщика
    try:
        scheduler.start_scheduler()
        add_log("INFO", "✓ Планировщик задач запущен")
        logger.info("✓ Планировщик задач запущен")
    except Exception as e:
        add_log("ERROR", f"⚠️ Ошибка запуска планировщика: {e}")
        logger.error(f"⚠️ Ошибка запуска планировщика: {e}")
    
    add_log("INFO", "✓ Приложение готово к работе")
    logger.info("✓ Приложение готово к работе")


@app.on_event("shutdown")
async def shutdown_event():
    """Корректная остановка при завершении приложения."""
    add_log("INFO", "🛑 Остановка приложения...")
    logger.info("🛑 Остановка приложения...")
    
    # Остановка планировщика
    try:
        scheduler.stop_scheduler()
        add_log("INFO", "✓ Планировщик остановлен")
        logger.info("✓ Планировщик остановлен")
    except Exception as e:
        logger.error(f"⚠️ Ошибка остановки планировщика: {e}")
    
    logger.info("✓ Приложение остановлено")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Главная страница."""
    return templates.TemplateResponse("index.html", {"request": request})


def _serialize_post(post: Post) -> dict:
    """Сериализация поста для ленты (включая аналитику и изображение)."""
    image_data = None
    analysis_data = None

    if post.images:
        img = post.images[0]
        image_data = {
            "id": img.id,
            "file_path": img.file_path if img.file_path and os.path.exists(img.file_path) else "/static/placeholder.png",
        }
        if img.analysis:
            analysis_data = _serialize_analysis(img.analysis)
    else:
        image_data = {"id": None, "file_path": "/static/placeholder.png"}

    return {
        "id": post.id,
        "channel": post.channel,
        "telegram_post_id": post.telegram_post_id,
        "text": post.text,
        "date": post.date.isoformat() if post.date else None,
        "views": post.views,
        "engagement": post.engagement,
        "er": round(post.er or 0, 2),
        "post_url": _fix_post_url(post),
        "citation_count": post.citation_count or 0,
        "image_hash": post.image_hash,
        "image": image_data,
        "analysis": analysis_data,
    }


def _dedupe_posts_by_hash(posts: List[Post]) -> tuple[List[Post], Dict[int, List[Post]]]:
    """Группирует посты по image_hash и оставляет «лучший» (по ER, затем по views).

    Возвращает (kept_posts, duplicates_map), где duplicates_map[best_post_id] —
    список вытесненных дублей того же кадра. Посты без image_hash пропускаются
    через фильтр без изменений (каждый сам по себе).
    """
    by_hash: Dict[str, List[Post]] = {}
    no_hash: List[Post] = []
    for p in posts:
        h = (p.image_hash or "").strip()
        if not h:
            no_hash.append(p)
        else:
            by_hash.setdefault(h, []).append(p)

    kept: List[Post] = list(no_hash)
    dup_map: Dict[int, List[Post]] = {}

    for h, group in by_hash.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        # Сортируем по убыванию: ER → views → engagement → date
        group_sorted = sorted(
            group,
            key=lambda p: (
                float(p.er or 0),
                int(p.views or 0),
                int(p.engagement or 0),
                p.date or datetime.min,
            ),
            reverse=True,
        )
        best = group_sorted[0]
        kept.append(best)
        dup_map[best.id] = group_sorted[1:]

    # Стабильная сортировка по дате (самые свежие — выше)
    kept.sort(key=lambda p: p.date or datetime.min, reverse=True)
    return kept, dup_map


@app.get("/api/creatives")
async def get_creatives(
    limit: int = 50,
    offset: int = 0,
    channel: Optional[str] = None,
    relevant_only: bool = False,
    days: Optional[int] = None,
    dedupe: bool = False,
    db: Session = Depends(get_db_session)
):
    """
    Получение списка креативов.

    Args:
        limit: Количество креативов
        offset: Смещение
        channel: Фильтр по каналу (опционально). Можно передавать несколько
            каналов, перечисленных через запятую: ``channel=a,b,c``.
        relevant_only: Если True — оставить в ленте только посты, которые
            потенциально подходят под «целевой креатив».
        days: Если задано — оставить только посты за последние N дней.
        dedupe: Если True — группировать посты по перцептивному хешу
            изображения (image_hash) и оставлять только наиболее популярный
            из дублей (по ER, затем views). Остальные дубли возвращаются
            внутри поля ``duplicates`` основного поста.
    """
    query = db.query(Post).options(
        joinedload(Post.images).joinedload(Image.analysis)
    ).order_by(Post.date.desc())

    channels_list = _parse_channels_param(channel)
    if channels_list:
        query = query.filter(Post.channel.in_(channels_list))

    if days is not None and days > 0:
        from datetime import timedelta
        since = datetime.utcnow() - timedelta(days=int(days))
        query = query.filter(Post.date >= since)

    if relevant_only:
        query = (
            query.join(Image, Image.post_id == Post.id)
                 .join(Analysis, Analysis.image_id == Image.id)
        )
        query = query.filter(_relevant_creative_filter())

    if not dedupe:
        total = query.count()
        posts = query.limit(limit).offset(offset).all()
        creatives = [_serialize_post(p) for p in posts]
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "creatives": creatives,
        }

    # Режим дедупликации: загружаем все подходящие посты, группируем по
    # image_hash, оставляем лучший из каждой группы, после чего применяем
    # пагинацию уже к схлопнутому списку.
    all_posts = query.all()
    kept, dup_map = _dedupe_posts_by_hash(all_posts)
    total = len(kept)
    page = kept[offset: offset + limit]

    creatives = []
    for p in page:
        item = _serialize_post(p)
        dups = dup_map.get(p.id, [])
        item["duplicate_count"] = len(dups)
        item["duplicates"] = [
            {
                "id": d.id,
                "channel": d.channel,
                "post_url": _fix_post_url(d),
                "date": d.date.isoformat() if d.date else None,
                "views": d.views,
                "engagement": d.engagement,
                "er": round(d.er or 0, 2),
            }
            for d in dups
        ]
        creatives.append(item)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "deduped": True,
        "creatives": creatives,
    }


@app.get("/api/creative/{creative_id}")
async def get_creative(creative_id: int, db: Session = Depends(get_db_session)):
    """Получение детальной информации о креативе."""
    post = db.query(Post).options(
        joinedload(Post.images).joinedload(Image.analysis)
    ).filter(Post.id == creative_id).first()
    
    if not post:
        raise HTTPException(status_code=404, detail="Креатив не найден")
    
    # Формируем детальный ответ
    images = []
    if post.images:
        for img in post.images:
            image_item = {
                "id": img.id,
                "file_path": img.file_path if img.file_path and os.path.exists(img.file_path) else "/static/placeholder.png",
                "analysis": None
            }
            
            if img.analysis:
                image_item["analysis"] = _serialize_analysis(img.analysis)
            
            images.append(image_item)
    else:
        # Если у поста вообще нет изображений - подставляем заглушку
        images.append({
            "id": None,
            "file_path": "/static/placeholder.png",
            "analysis": None
        })
    
    return {
        "id": post.id,
        "channel": post.channel,
        "telegram_post_id": post.telegram_post_id,
        "text": post.text,
        "date": post.date.isoformat() if post.date else None,
        "views": post.views,
        "forwards": post.forwards,
        "replies": post.replies,
        "reactions": post.reactions,
        "engagement": post.engagement,
        "er": round(post.er, 2),
        "post_url": _fix_post_url(post),
        "images": images
    }


@app.get("/api/stats")
async def get_stats(db: Session = Depends(get_db_session)):
    """Получение общей статистики."""
    from sqlalchemy import func
    
    total_posts = db.query(Post).count()
    total_images = db.query(Image).count()
    total_analyzed = db.query(Analysis).count()
    
    channels = db.query(Post.channel).distinct().all()
    channels_list = [ch[0] for ch in channels]
    
    # Вычисляем средние значения
    avg_stats = db.query(
        func.avg(Post.views).label('avg_views'),
        func.avg(Post.er).label('avg_er')
    ).first()
    
    avg_views = int(avg_stats.avg_views) if avg_stats.avg_views else 0
    avg_er = round(float(avg_stats.avg_er), 2) if avg_stats.avg_er else 0.0
    
    return {
        "total_posts": total_posts,
        "total_images": total_images,
        "total_analyzed": total_analyzed,
        "channels": channels_list,
        "avg_views": avg_views,
        "avg_er": avg_er,
        "parser_status": parser_status,
        "analysis_status": analysis_status
    }


async def run_parser_task(run_type: str = 'manual'):
    """
    Фоновая задача для запуска парсера.
    
    Args:
        run_type: Тип запуска ('manual' или 'auto')
    """
    global parser_status
    
    try:
        _job_started(parser_status, "Парсинг запущен…", run_type=run_type)
        add_log("INFO", f"📡 Запуск парсера Telegram каналов ({run_type})...")
        logger.info(f"📡 Запуск парсера Telegram каналов ({run_type})...")
        
        # Получаем настройки глубины парсинга и объединённый список каналов
        db = get_database()
        with db.get_session() as session:
            parse_depth_setting = session.query(Settings).filter(Settings.key == 'parse_depth').first()
            parse_from_date_setting = session.query(Settings).filter(Settings.key == 'parse_from_date').first()

            parse_depth = parse_depth_setting.value if parse_depth_setting else 'today'
            parse_from_date = parse_from_date_setting.value if parse_from_date_setting else None

            merged_channels, manual_channels, auto_channels = _merge_channels_for_parsing(session)

        add_log(
            "INFO",
            f"📋 Каналов к парсингу: {len(merged_channels)} "
            f"(ручных: {len(manual_channels)}, из реестра: {len(auto_channels)})"
        )

        parser = TelegramParser(
            api_id=config.TELEGRAM_API_ID,
            api_hash=config.TELEGRAM_API_HASH
        )

        count = await parser.parse_channels(
            channels=merged_channels,
            limit=100,
            parse_depth=parse_depth,
            parse_from_date=parse_from_date
        )
        
        # Логируем результат в БД
        with db.get_session() as session:
            log_entry = ScheduleLog(
                timestamp=datetime.utcnow(),
                run_type=run_type,
                status='success',
                images_parsed=count,
                images_analyzed=0
            )
            session.add(log_entry)
            session.commit()
        
        _job_finished(
            parser_status,
            f"Парсинг завершён. Собрано постов: {count}",
            success=True,
        )
        add_log("INFO", f"✅ Парсинг завершен ({run_type}). Собрано постов: {count}")
        logger.info(f"✅ Парсинг завершен ({run_type}). Собрано постов: {count}")
        
    except Exception as e:
        # Логируем ошибку в БД
        try:
            db = get_database()
            with db.get_session() as session:
                log_entry = ScheduleLog(
                    timestamp=datetime.utcnow(),
                    run_type=run_type,
                    status='error',
                    images_parsed=0,
                    images_analyzed=0,
                    error_message=str(e)
                )
                session.add(log_entry)
                session.commit()
        except:
            pass
        
        _job_finished(
            parser_status,
            f"Ошибка парсинга: {str(e)}",
            success=False,
            error=str(e),
        )
        add_log("ERROR", f"❌ Ошибка парсинга ({run_type}): {str(e)}")
        logger.error(f"❌ Ошибка парсинга ({run_type}): {str(e)}", exc_info=True)


@app.post("/api/run-parser")
async def run_parser(background_tasks: BackgroundTasks):
    """Запуск парсера Telegram каналов."""
    if parser_status["running"]:
        return {"status": "already_running", "message": "Парсер уже запущен"}
    
    # Запускаем парсер в фоне
    background_tasks.add_task(run_parser_task)
    
    return {
        "status": "started",
        "message": "Парсер запущен в фоновом режиме",
        "job": parser_status,
    }


@app.get("/api/job-status")
async def get_job_status():
    """Лёгкий эндпоинт со статусами фоновых задач (для UI status-bar).

    Возвращает обогащённую информацию по парсеру и анализу: running, message,
    started_at, finished_at, duration_seconds, last_status, last_error, run_type.
    Также добавляет server_time, чтобы клиент мог точно считать elapsed.
    """
    return {
        "server_time": datetime.utcnow().isoformat(),
        "parser": parser_status,
        "analysis": analysis_status,
    }


def _run_analysis_blocking() -> int:
    """Синхронный анализ — выполняется в отдельном потоке, чтобы НЕ блокировать
    event loop FastAPI (иначе `/api/job-status`, `/api/logs/stream`, любые другие
    запросы и сам UI «зависают» до конца анализа)."""
    analyzer = ImageAnalyzer()
    return analyzer.analyze_all_unanalyzed()


async def run_analysis_task():
    """Фоновая задача для запуска анализа.

    ВАЖНО: BackgroundTasks у FastAPI выполняет `async def` корутины прямо в
    основном event loop. Анализ синхронный и долгий (OpenAI sync client +
    SQLAlchemy), поэтому его обязательно нужно унести в thread executor через
    `asyncio.to_thread`, иначе UI висит и логи не стримятся.
    """
    global analysis_status

    try:
        _job_started(analysis_status, "Анализ запущен…", run_type="manual")
        add_log("INFO", "🔍 Запуск AI анализа изображений...")
        logger.info("🔍 Запуск AI анализа изображений...")

        # Анализатор получает OpenAI API ключ из переменной окружения OPENAI_API_KEY (.env).
        count = await asyncio.to_thread(_run_analysis_blocking)

        _job_finished(
            analysis_status,
            f"Анализ завершён. Проанализировано: {count}",
            success=True,
        )
        add_log("INFO", f"✅ Анализ завершен. Проанализировано: {count}")
        logger.info(f"✅ Анализ завершен. Проанализировано: {count}")

    except Exception as e:
        _job_finished(
            analysis_status,
            f"Ошибка анализа: {str(e)}",
            success=False,
            error=str(e),
        )
        add_log("ERROR", f"❌ Ошибка анализа: {str(e)}")
        logger.error(f"❌ Ошибка анализа: {str(e)}", exc_info=True)


@app.post("/api/run-analysis")
async def run_analysis(background_tasks: BackgroundTasks):
    """Запуск AI анализа изображений."""
    if analysis_status["running"]:
        return {"status": "already_running", "message": "Анализ уже запущен"}
    
    # Запускаем анализ в фоне
    background_tasks.add_task(run_analysis_task)
    
    return {
        "status": "started",
        "message": "Анализ запущен в фоновом режиме",
        "job": analysis_status,
    }


def _merge_channels_for_parsing(db_session: Session) -> tuple[list[str], list[str], list[str]]:
    """Возвращает (merged, manual, auto) — список каналов для парсинга.

    Единственный источник истины — таблица `channels` в БД. Канал участвует в
    парсинге, только если в реестре стоит `is_active=True`. Деление на
    manual/auto идёт по полю `discovered_via` ('manual' / 'search').

    .env (`CHANNELS_TO_PARSE`) больше НЕ читается на каждый запуск — он
    используется один раз при первом старте для сидирования реестра
    (см. `seed_channels_from_env` в startup).
    """
    registry_rows = db_session.query(Channel).all()

    merged: list[str] = []
    manual: list[str] = []
    auto: list[str] = []
    seen: set[str] = set()

    for row in registry_rows:
        u = (row.username or '').strip().lstrip('@')
        if not u:
            continue
        key = u.lower()
        if key in seen:
            continue
        is_manual = (row.discovered_via or 'manual') == 'manual'
        if is_manual:
            manual.append(u)
        else:
            auto.append(u)
        if not row.is_active:
            continue
        seen.add(key)
        merged.append(u)

    return merged, manual, auto


@app.get("/api/channels")
async def get_channels(db: Session = Depends(get_db_session)):
    """Получение объединённого списка каналов для парсинга.

    Возвращает merged-список (ручные + активные авто-найденные) для отображения
    в UI, а также раздельные подсписки и счётчики, чтобы фронт мог пометить
    источник канала.
    """
    merged, manual, auto = _merge_channels_for_parsing(db)
    return {
        "channels": merged,
        "count": len(merged),
        "manual": manual,
        "manual_count": len(manual),
        "auto": auto,
        "auto_count": len(auto),
    }


@app.post("/api/channels")
async def update_channels(channels: List[str], db: Session = Depends(get_db_session)):
    """
    Синхронизация ручного списка каналов с реестром (таблица `channels`).

    Принимает список username-алиасов (без @). Логика:
      • новые usernames добавляются в реестр (`discovered_via='manual'`,
        `is_active=True`);
      • если канал уже был в реестре, но был выключен — включаем;
      • ручные каналы (`discovered_via='manual'`), которых НЕТ в новом списке,
        деактивируются (но не удаляются — все их посты остаются в БД).
    Авто-найденные (`discovered_via='search'`) каналы этим эндпоинтом не
    затрагиваются.

    .env больше НЕ перезаписывается: после первичного сидирования значение
    `CHANNELS_TO_PARSE` игнорируется — все правки переживают рестарт за счёт
    хранения в БД.
    """
    try:
        cleaned = [ch.strip().lstrip('@') for ch in channels if ch and ch.strip()]
        cleaned_keys = {ch.lower() for ch in cleaned}

        existing = {
            (row.username or '').lower(): row
            for row in db.query(Channel).all()
        }
        added = 0
        reactivated = 0
        deactivated = 0
        for ch in cleaned:
            row = existing.get(ch.lower())
            if row is None:
                db.add(Channel(
                    username=ch,
                    title=ch,
                    is_active=True,
                    discovered_via='manual',
                ))
                added += 1
            else:
                if not row.is_active:
                    row.is_active = True
                    reactivated += 1
                # Если запись когда-то была авто-найдена, но пользователь явно
                # ввёл её в ручной список — оставляем discovered_via как есть,
                # чтобы не путать происхождение.
        # Деактивируем ручные, удалённые из textarea (auto-найденные не трогаем)
        for key, row in existing.items():
            if (row.discovered_via or 'manual') == 'manual' and key not in cleaned_keys:
                if row.is_active:
                    row.is_active = False
                    deactivated += 1
        db.commit()

        return {
            "status": "success",
            "message": (
                f"Реестр обновлён. Добавлено: {added}, включено: {reactivated}, "
                f"выключено: {deactivated}."
            ),
            "channels": cleaned,
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Ошибка при обновлении списка каналов: {str(e)}")


# Ключи настроек, которые НЕЛЬЗЯ менять/получать через API.
# Хранение/передача API-ключа OpenAI допускается только через .env (OPENAI_API_KEY).
_FORBIDDEN_SETTINGS_KEYS = {"openai_api_key"}


@app.get("/api/settings")
async def get_settings(db: Session = Depends(get_db_session)):
    """Получение всех настроек."""
    settings = db.query(Settings).all()

    result = {}
    for setting in settings:
        if setting.key in _FORBIDDEN_SETTINGS_KEYS:
            continue
        result[setting.key] = setting.value
    
    return result


@app.post("/api/settings")
async def update_settings(settings_data: dict, db: Session = Depends(get_db_session)):
    """
    Обновление настроек.
    
    Args:
        settings_data: Словарь с настройками {key: value}
    """
    try:
        for key, value in settings_data.items():
            # Запрещаем сохранение/передачу секретов через API:
            # OpenAI API ключ должен задаваться только через переменную окружения OPENAI_API_KEY.
            if key in _FORBIDDEN_SETTINGS_KEYS:
                continue
            # Ищем существующую настройку
            setting = db.query(Settings).filter(Settings.key == key).first()
            
            if setting:
                # Обновляем существующую
                setting.value = value
            else:
                # Создаем новую
                setting = Settings(key=key, value=value)
                db.add(setting)
        
        db.commit()
        
        return {
            "status": "success",
            "message": "Настройки успешно обновлены"
        }
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Ошибка при обновлении настроек: {str(e)}")


@app.get("/api/logs")
async def get_logs():
    """Получение последних логов из буфера."""
    return {
        "logs": log_buffer[-100:],  # Возвращаем последние 100 логов
        "count": len(log_buffer)
    }


@app.get("/api/logs/stream")
async def stream_logs():
    """
    SSE (Server-Sent Events) для потоковой передачи логов в реальном времени.
    """
    async def generate():
        # Отправляем начальное сообщение
        yield f"data: {{'type': 'connected', 'message': 'Подключено к потоку логов'}}\n\n"
        
        # Отправляем последние 50 логов из буфера
        for log in log_buffer[-50:]:
            import json
            yield f"data: {json.dumps(log, ensure_ascii=False)}\n\n"
        
        # Запоминаем текущую позицию
        last_index = len(log_buffer)
        
        # Бесконечный цикл для отправки новых логов
        while True:
            # Проверяем наличие новых логов
            if len(log_buffer) > last_index:
                # Отправляем новые логи
                for log in log_buffer[last_index:]:
                    import json
                    yield f"data: {json.dumps(log, ensure_ascii=False)}\n\n"
                last_index = len(log_buffer)
            
            # Отправляем heartbeat каждые 15 секунд для поддержания соединения
            yield f": heartbeat\n\n"
            
            # Небольшая задержка перед следующей проверкой
            await asyncio.sleep(1)
    
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # Отключаем буферизацию для nginx
        }
    )


@app.get("/api/schedule")
async def get_schedule(db: Session = Depends(get_db_session)):
    """
    Получить текущие настройки расписания.
    """
    try:
        # Получаем настройки расписания из БД
        settings = db.query(Settings).filter(
            Settings.key.like('schedule_%')
        ).all()
        
        result = {}
        for setting in settings:
            key = setting.key.replace('schedule_', '')
            
            # Парсим значение в зависимости от ключа
            if key == 'enabled':
                result[key] = setting.value.lower() == 'true' if setting.value else False
            elif key == 'days':
                try:
                    result[key] = json.loads(setting.value) if setting.value else []
                except:
                    result[key] = []
            else:
                result[key] = setting.value
        
        # Значения по умолчанию
        defaults = {
            'enabled': False,
            'frequency': 'daily',
            'time': '08:00',
            'days': [],
            'end_type': 'indefinite',
            'end_date': None,
            'parse_depth': 'today',
            'parse_from_date': None
        }
        
        for key, default_value in defaults.items():
            if key not in result:
                result[key] = default_value
        
        # Получаем настройки глубины парсинга
        parse_depth_setting = db.query(Settings).filter(Settings.key == 'parse_depth').first()
        parse_from_date_setting = db.query(Settings).filter(Settings.key == 'parse_from_date').first()
        
        if parse_depth_setting:
            result['parse_depth'] = parse_depth_setting.value
        if parse_from_date_setting:
            result['parse_from_date'] = parse_from_date_setting.value
        
        # Добавляем статус планировщика
        status = scheduler.get_scheduler_status()
        result['scheduler_running'] = status['running']
        result['next_run'] = status['next_run']
        
        return result
        
    except Exception as e:
        logger.error(f"Ошибка при получении настроек расписания: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")


@app.post("/api/schedule")
async def update_schedule(schedule_data: Dict[str, Any], db: Session = Depends(get_db_session)):
    """
    Обновить настройки расписания.
    
    Args:
        schedule_data: Настройки расписания
            - enabled: bool
            - frequency: str (daily/weekly/biweekly/monthly/custom)
            - time: str (HH:MM)
            - days: list (для custom)
            - end_type: str (indefinite/until_date)
            - end_date: str (ISO формат, опционально)
    """
    try:
        # Валидация данных
        if 'enabled' not in schedule_data:
            raise HTTPException(status_code=400, detail="Отсутствует поле 'enabled'")
        
        if 'frequency' not in schedule_data:
            raise HTTPException(status_code=400, detail="Отсутствует поле 'frequency'")
        
        if 'time' not in schedule_data:
            raise HTTPException(status_code=400, detail="Отсутствует поле 'time'")
        
        # Проверка формата времени
        try:
            time_parts = schedule_data['time'].split(':')
            if len(time_parts) != 2:
                raise ValueError()
            hour, minute = int(time_parts[0]), int(time_parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError()
        except:
            raise HTTPException(status_code=400, detail="Неверный формат времени. Используйте HH:MM")
        
        # Сохраняем настройки в БД
        settings_to_save = {
            'schedule_enabled': str(schedule_data['enabled']),
            'schedule_frequency': schedule_data['frequency'],
            'schedule_time': schedule_data['time'],
            'schedule_days': json.dumps(schedule_data.get('days', [])),
            'schedule_end_type': schedule_data.get('end_type', 'indefinite'),
            'schedule_end_date': schedule_data.get('end_date', ''),
            'parse_depth': schedule_data.get('parse_depth', 'today'),
            'parse_from_date': schedule_data.get('parse_from_date', '')
        }
        
        for key, value in settings_to_save.items():
            setting = db.query(Settings).filter(Settings.key == key).first()
            
            if setting:
                setting.value = value
                setting.updated_at = datetime.utcnow()
            else:
                setting = Settings(key=key, value=value)
                db.add(setting)
        
        db.commit()
        
        # Обновляем расписание в планировщике
        scheduler.update_schedule(schedule_data)
        
        add_log("INFO", f"📅 Расписание обновлено: {schedule_data['frequency']} в {schedule_data['time']}")
        logger.info(f"📅 Расписание обновлено: {schedule_data['frequency']} в {schedule_data['time']}")
        
        return {
            "status": "success",
            "message": "Расписание успешно обновлено"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при обновлении расписания: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка при обновлении расписания: {str(e)}")


@app.get("/api/schedule/logs")
async def get_schedule_logs(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db_session)
):
    """
    Получить историю автозапусков.
    
    Args:
        limit: Количество записей (по умолчанию 50)
        offset: Смещение для пагинации (по умолчанию 0)
    """
    try:
        # Получаем общее количество записей
        total = db.query(ScheduleLog).count()
        
        # Получаем записи с учетом limit и offset
        logs = db.query(ScheduleLog).order_by(
            ScheduleLog.timestamp.desc()
        ).limit(limit).offset(offset).all()
        
        result = []
        for log in logs:
            result.append({
                'id': log.id,
                'timestamp': log.timestamp.isoformat() if log.timestamp else None,
                'run_type': log.run_type if hasattr(log, 'run_type') else 'auto',
                'status': log.status,
                'images_parsed': log.images_parsed,
                'images_analyzed': log.images_analyzed,
                'error_message': log.error_message
            })
        
        return {
            'logs': result,
            'count': total,  # Общее количество записей
            'limit': limit,
            'offset': offset
        }
        
    except Exception as e:
        logger.error(f"Ошибка при получении логов расписания: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")


# =====================================================================
# Phase 2 — Channel discovery
# =====================================================================

discovery_status: Dict[str, Any] = {
    "running": False,
    "message": "Готов к запуску",
    "results": [],
    # Прогресс по ключевым словам
    "total_keywords": 0,
    "processed_keywords": 0,
    "current_keyword": "",
    # Счётчики каналов
    "checked_channels": 0,
    "accepted_channels": 0,
    "percent": 0,
    # Последние события (кольцевой буфер)
    "events": [],
}

_DISCOVERY_EVENTS_LIMIT = 50


async def run_discovery_task():
    global discovery_status
    try:
        discovery_status.update({
            "running": True,
            "message": "Поиск каналов запущен...",
            "results": [],
            "total_keywords": 0,
            "processed_keywords": 0,
            "current_keyword": "",
            "checked_channels": 0,
            "accepted_channels": 0,
            "percent": 0,
            "events": [],
        })
        add_log("INFO", "🔎 Запуск авто-поиска каналов через Telethon...")

        db = get_database()
        with db.get_session() as session:
            criteria = load_criteria_from_settings(session)
            keywords = load_search_keywords_from_settings(session)

        if not keywords:
            raise RuntimeError("Не задано ни одного ключевого слова для поиска")

        def on_progress(event: dict) -> None:
            stage = event.get("stage")
            if stage == "start":
                discovery_status["total_keywords"] = event.get("total_keywords", 0)
                discovery_status["message"] = (
                    f"Старт: {discovery_status['total_keywords']} ключевых слов"
                )
            elif stage == "keyword":
                discovery_status["processed_keywords"] = event.get("index", 0)
                discovery_status["current_keyword"] = event.get("keyword", "")
                total = max(1, discovery_status["total_keywords"])
                discovery_status["percent"] = int(
                    discovery_status["processed_keywords"] / total * 100
                )
                discovery_status["message"] = (
                    f"[{event.get('index')}/{event.get('total')}] "
                    f"«{event.get('keyword')}» — найдено чатов: {event.get('found_chats', 0)}"
                )
            elif stage == "channel":
                discovery_status["checked_channels"] += 1
                discovery_status["accepted_channels"] = event.get(
                    "accepted_total", discovery_status["accepted_channels"]
                )
                if event.get("accepted"):
                    line = (
                        f"✅ @{event.get('username')} "
                        f"({event.get('subscribers')} подп., ER {event.get('avg_er')}%)"
                    )
                else:
                    line = (
                        f"✕ @{event.get('username')} — {event.get('reason') or 'не подходит'}"
                    )
                events = discovery_status["events"]
                events.append(line)
                if len(events) > _DISCOVERY_EVENTS_LIMIT:
                    del events[: len(events) - _DISCOVERY_EVENTS_LIMIT]
            elif stage == "done":
                discovery_status["percent"] = 100

        disc = ChannelDiscovery(
            api_id=config.TELEGRAM_API_ID,
            api_hash=config.TELEGRAM_API_HASH,
        )
        results = await disc.search_and_filter(
            keywords, criteria, progress_cb=on_progress
        )

        with db.get_session() as session:
            for info in results:
                upsert_channel(session, info)

        discovery_status["results"] = [info_to_dict(r) for r in results]
        discovery_status["running"] = False
        discovery_status["percent"] = 100
        discovery_status["message"] = (
            f"Готово. Проверено: {discovery_status['checked_channels']}, "
            f"подходит: {len(results)}"
        )
        add_log("INFO", f"✅ Авто-поиск завершён. Каналов: {len(results)}")
    except Exception as exc:
        discovery_status["running"] = False
        discovery_status["message"] = f"Ошибка: {exc}"
        add_log("ERROR", f"❌ Ошибка авто-поиска: {exc}")
        logger.error("Channel discovery failed", exc_info=True)


@app.post("/api/channels/discover")
async def channels_discover(background_tasks: BackgroundTasks):
    """Запустить авто-поиск каналов в фоне (Phase 2)."""
    if discovery_status["running"]:
        return {"status": "already_running", "message": "Поиск уже выполняется"}
    background_tasks.add_task(run_discovery_task)
    return {"status": "started", "message": "Поиск каналов запущен"}


@app.get("/api/channels/discover/status")
async def channels_discover_status():
    return discovery_status


# Глобальный статус обогащения «ручных» каналов
enrich_status: Dict[str, Any] = {
    "running": False,
    "message": "",
    "total": 0,
    "done": 0,
    "ok": 0,
    "failed": 0,
    "errors": [],   # список {username, reason}
    "updated": [],  # успешно обновлённые @username
    "current": "",  # текущий канал
}


async def run_enrich_task(usernames: List[str]):
    """Подтягивает реальные метрики через Telethon и обновляет реестр.
    По каждому каналу пишет per-username результат (ok / причина).
    """
    global enrich_status
    enrich_status.update({
        "running": True, "message": "Обогащение запущено...",
        "total": len(usernames), "done": 0, "ok": 0, "failed": 0,
        "errors": [], "updated": [], "current": "",
    })
    add_log("INFO", f"📊 Обогащение каналов: {len(usernames)} шт.")
    try:
        disc = ChannelDiscovery(
            api_id=config.TELEGRAM_API_ID,
            api_hash=config.TELEGRAM_API_HASH,
        )

        def on_progress(ev: dict) -> None:
            stage = ev.get("stage")
            if stage == "item":
                u = ev.get("username") or ""
                enrich_status["current"] = u
                enrich_status["done"] += 1
                if ev.get("ok"):
                    enrich_status["ok"] += 1
                    enrich_status["updated"].append(u)
                    add_log(
                        "INFO",
                        f"  ✓ @{u}: подписчики={ev.get('subscribers')}, "
                        f"ER={ev.get('avg_er')}%",
                    )
                else:
                    enrich_status["failed"] += 1
                    reason = ev.get("reason") or "unknown"
                    enrich_status["errors"].append({"username": u, "reason": reason})
                    add_log("WARNING", f"  ✕ @{u}: {reason}")

        infos = await disc.enrich_by_usernames(
            usernames, progress_cb=on_progress
        )
        db = get_database()
        with db.get_session() as session:
            for info in infos:
                row = session.query(Channel).filter(
                    Channel.username == info.username
                ).first()
                if row is None:
                    row = Channel(
                        username=info.username,
                        title=info.title or info.username,
                        is_active=True,
                        discovered_via='manual',
                    )
                    session.add(row)
                if info.title:
                    row.title = info.title
                if info.description:
                    row.description = info.description
                # перезаписываем «нулевые» значения, ненулевые сохраняем
                if info.subscribers:
                    row.subscribers_count = info.subscribers
                if info.avg_er:
                    row.avg_er = info.avg_er
                if info.avg_views:
                    row.avg_views = info.avg_views
                if info.posts_per_week:
                    row.posts_per_week = info.posts_per_week
                row.reactions_enabled = bool(info.reactions_enabled or row.reactions_enabled)
                if not row.discovered_via:
                    row.discovered_via = 'manual'
            session.commit()
        enrich_status["message"] = (
            f"Готово. Успешно: {enrich_status['ok']} / Ошибок: {enrich_status['failed']} "
            f"из {len(usernames)}"
        )
        add_log(
            "INFO",
            f"✅ Обогащение завершено: {enrich_status['ok']}/{len(usernames)}, "
            f"ошибок: {enrich_status['failed']}",
        )
    except Exception as exc:
        enrich_status["message"] = f"Ошибка: {exc}"
        enrich_status["errors"].append({"username": "*", "reason": str(exc)})
        add_log("ERROR", f"❌ Ошибка обогащения каналов: {exc}")
        logger.error("enrich task failed", exc_info=True)
    finally:
        enrich_status["running"] = False
        enrich_status["current"] = ""


@app.post("/api/channels/enrich")
async def channels_enrich(
    background_tasks: BackgroundTasks,
    payload: Optional[Dict[str, Any]] = None,
    db: Session = Depends(get_db_session),
):
    """Запустить обогащение метрик для каналов.

    Если в payload передан `usernames: [..]` — обогатятся только они.
    Иначе берём все ручные/неполные записи из реестра (там, где
    `subscribers_count = 0` или `avg_er = 0`).
    """
    if enrich_status["running"]:
        return {"status": "already_running", "message": "Обогащение уже выполняется"}
    payload = payload or {}
    usernames: List[str] = [
        str(u or '').strip().lstrip('@')
        for u in (payload.get('usernames') or [])
    ]
    usernames = [u for u in usernames if u]
    if not usernames:
        rows = db.query(Channel.username).filter(
            Channel.username.isnot(None),
            or_(
                Channel.subscribers_count == 0,
                Channel.subscribers_count.is_(None),
                Channel.avg_er == 0,
                Channel.avg_er.is_(None),
            ),
        ).all()
        usernames = [u for (u,) in rows if u]
    if not usernames:
        return {"status": "noop", "message": "Нет каналов для обогащения"}
    background_tasks.add_task(run_enrich_task, usernames)
    return {"status": "started", "count": len(usernames)}


@app.get("/api/channels/enrich/status")
async def channels_enrich_status():
    return enrich_status


@app.get("/api/channels/registry")
async def get_channels_registry(
    active_only: bool = False,
    db: Session = Depends(get_db_session),
):
    """Список каналов из реестра (таблица `channels`).

    Источник истины — таблица `channels`. .env (`CHANNELS_TO_PARSE`) уже не
    подмешивается на лету — каналы из него попадают в БД при первом старте
    через `seed_channels_from_env`.
    """
    q = db.query(Channel).order_by(Channel.subscribers_count.desc())
    if active_only:
        q = q.filter(Channel.is_active == True)  # noqa: E712
    rows = q.all()

    # 2) Подсчёт «живой» статистики из таблицы posts для каналов,
    #    у которых в реестре пусто (типичный случай — добавленные вручную).
    #
    # ВАЖНО: парсер сохраняет посты с Post.channel = <реальный заголовок канала
    # из Telegram> (например "Ноутбуки от Михаила"), а в реестр ручные каналы
    # попадают с username/title = "noutbuki_mikhail". Поэтому матчить по
    # Post.channel == row.title бесполезно. Надёжный ключ — это post_url:
    # для публичных каналов он начинается с https://t.me/<username>/.
    from sqlalchemy import func as sa_func
    stats_by_key: Dict[str, Dict[str, Any]] = {}
    titles_by_key: Dict[str, str] = {}
    try:
        for row in rows:
            if (row.avg_er or 0) > 0 and (row.subscribers_count or 0) > 0:
                continue
            uname = (row.username or '').strip().lstrip('@')
            try:
                filters = []
                if row.id is not None:
                    filters.append(Post.channel_id == row.id)
                if uname:
                    # https://t.me/<username>/<id>
                    filters.append(Post.post_url.ilike(f"https://t.me/{uname}/%"))
                    # legacy: некоторые посты могли быть сохранены с channel=username
                    filters.append(Post.channel == uname)
                # запасной вариант: совпадение по сохранённому в реестре title
                if row.title and row.title != uname:
                    filters.append(Post.channel == row.title)
                if not filters:
                    continue
                s = db.query(
                    sa_func.avg(Post.er).label('avg_er'),
                    sa_func.avg(Post.views).label('avg_views'),
                    sa_func.count(Post.id).label('cnt'),
                    sa_func.min(Post.date).label('min_date'),
                    sa_func.max(Post.date).label('max_date'),
                    sa_func.max(Post.has_reactions).label('any_reactions'),
                ).filter(or_(*filters)).first()
            except Exception as e:
                logger.warning(f"registry: live stats query failed for channel id={row.id}: {e}")
                continue
            if not s or not s.cnt:
                continue
            # Вытаскиваем реальный заголовок канала из любого поста, чтобы
            # подменить технический title=username на человекочитаемый.
            real_title = None
            try:
                if uname:
                    p = db.query(Post.channel).filter(
                        Post.post_url.ilike(f"https://t.me/{uname}/%")
                    ).filter(Post.channel.isnot(None)).first()
                    if p and p[0] and p[0] != uname:
                        real_title = p[0]
            except Exception:
                real_title = None
            ppw = 0.0
            try:
                if s.min_date and s.max_date and s.max_date > s.min_date:
                    span_days = (s.max_date - s.min_date).total_seconds() / 86400.0
                    if span_days > 0:
                        ppw = round(s.cnt / span_days * 7.0, 2)
                elif s.cnt:
                    ppw = float(s.cnt)
            except Exception:
                ppw = 0.0
            key = uname.lower() or str(row.id)
            stats_by_key[key] = {
                'avg_er': round(float(s.avg_er or 0), 2),
                'avg_views': int(s.avg_views or 0),
                'posts_per_week': ppw,
                'reactions_enabled': bool(s.any_reactions),
                'posts_collected': int(s.cnt),
            }
            if real_title:
                titles_by_key[key] = real_title
    except Exception as e:
        logger.warning(f"registry: live stats computation skipped: {e}")
        stats_by_key = {}

    def _serialize(c: Channel) -> dict:
        key = (c.username or '').lower() or str(c.id)
        live = stats_by_key.get(key, {})
        display_title = titles_by_key.get(key) or c.title or c.username
        return {
            "id": c.id,
            "username": c.username,
            "title": display_title,
            "description": c.description,
            "subscribers_count": c.subscribers_count or 0,
            "avg_er": c.avg_er if (c.avg_er or 0) > 0 else live.get('avg_er', 0.0),
            "avg_views": c.avg_views if (c.avg_views or 0) > 0 else live.get('avg_views', 0),
            "reactions_enabled": c.reactions_enabled or live.get('reactions_enabled', False),
            "posts_per_week": c.posts_per_week if (c.posts_per_week or 0) > 0 else live.get('posts_per_week', 0.0),
            "posts_collected": live.get('posts_collected', 0),
            "category": c.category,
            "language": c.language,
            "is_active": c.is_active,
            "discovered_via": c.discovered_via,
            "last_parsed_at": c.last_parsed_at.isoformat() if c.last_parsed_at else None,
        }

    return {
        "channels": [_serialize(c) for c in rows],
        "count": len(rows),
    }


@app.post("/api/channels/registry/{channel_id}/toggle")
async def toggle_channel(channel_id: int, db: Session = Depends(get_db_session)):
    row = db.query(Channel).filter(Channel.id == channel_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Канал не найден")
    row.is_active = not row.is_active
    db.commit()
    return {"id": row.id, "is_active": row.is_active}


@app.delete("/api/channels/registry/{channel_id}")
async def delete_channel(channel_id: int, db: Session = Depends(get_db_session)):
    """Удалить канал из реестра.

    Полезно для каналов, которые не удалось найти/обогатить. Уже скачанные
    посты остаются в БД, но их связь `channel_id` обнуляется (FK -> NULL),
    чтобы не нарушить целостность.
    """
    row = db.query(Channel).filter(Channel.id == channel_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Канал не найден")
    username = row.username
    try:
        # Развязываем посты от удаляемого канала, чтобы не упасть на FK.
        db.query(Post).filter(Post.channel_id == channel_id).update(
            {Post.channel_id: None}, synchronize_session=False
        )
        db.delete(row)
        db.commit()
        add_log("WARNING", f"Канал @{username} (id={channel_id}) удалён из реестра")
        return {"status": "deleted", "id": channel_id, "username": username}
    except Exception as exc:
        db.rollback()
        logger.error("delete_channel failed", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка удаления канала: {exc}")


@app.patch("/api/channels/registry/{channel_id}")
async def patch_channel(
    channel_id: int,
    payload: Dict[str, Any],
    db: Session = Depends(get_db_session),
):
    """Редактировать запись канала в реестре.

    Допустимые поля: `username`, `title`, `description`, `category`,
    `language`, `is_active`. `username` нормализуется (без @, без пробелов)
    и должен быть уникальным.

    Сценарий замены «нерабочего» канала на другой: PATCH с новым `username`
    оставит все ранее скачанные посты прежними, но дальнейший парсинг будет
    идти под новым алиасом.
    """
    row = db.query(Channel).filter(Channel.id == channel_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Канал не найден")

    if 'username' in payload and payload['username'] is not None:
        new_u = str(payload['username']).strip().lstrip('@')
        if not new_u:
            raise HTTPException(status_code=400, detail="username не может быть пустым")
        if new_u.lower() != (row.username or '').lower():
            clash = db.query(Channel).filter(
                Channel.username.ilike(new_u), Channel.id != channel_id
            ).first()
            if clash:
                raise HTTPException(
                    status_code=409,
                    detail=f"Канал @{new_u} уже есть в реестре (id={clash.id})",
                )
            row.username = new_u
            # При смене алиаса сбрасываем агрегаты, чтобы их можно было
            # перетянуть заново через «Подтянуть статистику».
            row.subscribers_count = 0
            row.avg_er = 0.0
            row.avg_views = 0
            row.posts_per_week = 0.0

    for field_name in ('title', 'description', 'category', 'language'):
        if field_name in payload and payload[field_name] is not None:
            setattr(row, field_name, str(payload[field_name]).strip() or None)

    if 'is_active' in payload and payload['is_active'] is not None:
        row.is_active = bool(payload['is_active'])

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("patch_channel failed", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка обновления канала: {exc}")

    return {
        "id": row.id,
        "username": row.username,
        "title": row.title,
        "is_active": row.is_active,
        "discovered_via": row.discovered_via,
    }


# =====================================================================
# Phase 4 — Target creative description endpoints
# =====================================================================


@app.get("/api/creative/{creative_id}/target-description")
async def get_target_description(creative_id: int, db: Session = Depends(get_db_session)):
    post = db.query(Post).options(
        joinedload(Post.images).joinedload(Image.analysis)
    ).filter(Post.id == creative_id).first()
    if not post or not post.images:
        raise HTTPException(status_code=404, detail="Креатив не найден")
    img = post.images[0]
    a = img.analysis
    return {
        "creative_id": creative_id,
        "ready": bool(a and a.target_creative_ready),
        "description": (a.target_creative_description if a else None) or "",
    }


@app.post("/api/creative/{creative_id}/generate-description")
async def generate_target_description(
    creative_id: int,
    db: Session = Depends(get_db_session),
):
    post = db.query(Post).options(
        joinedload(Post.images).joinedload(Image.analysis)
    ).filter(Post.id == creative_id).first()
    if not post or not post.images:
        raise HTTPException(status_code=404, detail="Креатив не найден")

    img = post.images[0]
    if not img.file_path or not os.path.exists(img.file_path):
        raise HTTPException(status_code=400, detail="Файл изображения недоступен")

    try:
        analyzer = ImageAnalyzer()
        description = analyzer.generate_target_description(img.file_path)
        if not description:
            raise HTTPException(status_code=500, detail="Не удалось сгенерировать описание")

        analysis = img.analysis
        if analysis is None:
            analysis = Analysis(image_id=img.id)
            db.add(analysis)
            db.flush()
        analysis.target_creative_description = description
        analysis.target_creative_ready = True
        db.commit()
        return {"creative_id": creative_id, "description": description, "ready": True}
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        logger.error("generate_target_description failed", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/creative/{creative_id}/reanalyze")
async def reanalyze_creative(
    creative_id: int,
    db: Session = Depends(get_db_session),
):
    """Перезапустить Stage-1 (классификацию) и Stage-2 (полный анализ) для одного креатива.

    Полезно после изменения промптов или при подозрении на ошибочную классификацию.
    Существующая запись Analysis обновляется in place; modaration_status сбрасывается, если был «rejected» (фильтром).
    """
    post = db.query(Post).options(
        joinedload(Post.images).joinedload(Image.analysis)
    ).filter(Post.id == creative_id).first()
    if not post or not post.images:
        raise HTTPException(status_code=404, detail="Креатив не найден")

    img = post.images[0]
    if not img.file_path or not os.path.exists(img.file_path):
        raise HTTPException(status_code=400, detail="Файл изображения недоступен")

    try:
        from ai.image_classifier import ImageClassifier, FilterConfig, evaluate
        analyzer = ImageAnalyzer()
        classifier = ImageClassifier(api_key=analyzer.api_key, model=analyzer.model)
        cls_result = classifier.classify(img.file_path)
        filter_cfg = FilterConfig.from_settings()
        decision = evaluate(cls_result, filter_cfg) if cls_result else None

        full_result = None
        if decision is None or decision.accepted:
            full_result = analyzer.analyze_image(img.file_path)

        # Обновляем существующую Analysis или создаём новую
        analysis = img.analysis
        if analysis is None:
            analysis = Analysis(image_id=img.id)
            db.add(analysis)

        if full_result:
            analysis.scene = full_result.get("scene", "")
            objects = full_result.get("objects")
            analysis.objects = ", ".join(objects) if isinstance(objects, list) else (objects or "")
            analysis.emotion = full_result.get("emotion", "")
            analysis.creative_type = full_result.get("type", "")
            analysis.text_present = str(full_result.get("text_present", ""))
            try:
                analysis.visual_strength_score = float(full_result.get("visual_strength_score") or 0)
            except (TypeError, ValueError):
                analysis.visual_strength_score = 0.0

        if cls_result:
            analysis.is_ad_creative = cls_result.is_ad_creative
            analysis.is_text_screenshot = cls_result.is_text_screenshot
            analysis.is_news_photo = cls_result.is_news_photo
            analysis.is_infographic = cls_result.is_infographic
            analysis.has_brand_logo = cls_result.has_brand_logo
            analysis.has_overlay_text = cls_result.has_overlay_text
            analysis.ethics_flag = not cls_result.ethics_ok
            analysis.ethics_reason = cls_result.ethics_reason
            analysis.image_is_key_factor = cls_result.image_is_key_factor
            analysis.solo_image_appeal = cls_result.solo_image_appeal
            analysis.tags = json.dumps(cls_result.tags, ensure_ascii=False)

        # Сбрасываем «rejected by filter», если новая классификация прошла
        if decision and decision.accepted and analysis.moderation_status == "rejected":
            analysis.moderation_status = "pending"
            analysis.moderation_comment = ""
        elif decision and not decision.accepted:
            analysis.moderation_status = "rejected"
            analysis.moderation_comment = decision.reason

        db.commit()
        return {
            "creative_id": creative_id,
            "stage1": cls_result.__dict__ if cls_result else None,
            "stage2": full_result,
            "rejected_by_filter": bool(decision and not decision.accepted),
            "rejection_reason": decision.reason if decision and not decision.accepted else "",
        }
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        logger.error("reanalyze_creative failed", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


# =====================================================================
# Phase 4 / Moderation
# =====================================================================


@app.post("/api/creative/{creative_id}/moderate")
async def moderate_creative(
    creative_id: int,
    payload: Dict[str, Any],
    db: Session = Depends(get_db_session),
):
    """Approve/reject a creative.

    Body: {"status": "approved"|"rejected"|"pending", "comment": "..."}
    """
    status = (payload.get("status") or "").strip().lower()
    if status not in {"approved", "rejected", "pending"}:
        raise HTTPException(status_code=400, detail="status must be approved/rejected/pending")

    post = db.query(Post).options(
        joinedload(Post.images).joinedload(Image.analysis)
    ).filter(Post.id == creative_id).first()
    if not post or not post.images:
        raise HTTPException(status_code=404, detail="Креатив не найден")

    img = post.images[0]
    a = img.analysis
    if a is None:
        a = Analysis(image_id=img.id)
        db.add(a)
        db.flush()
    a.moderation_status = status
    a.moderation_comment = payload.get("comment", "") or ""
    a.moderated_at = datetime.utcnow()
    db.commit()
    return {"creative_id": creative_id, "status": status}


# =====================================================================
# Phase 5 — Citation viewing
# =====================================================================


@app.get("/api/creative/{creative_id}/citations")
async def get_citations(creative_id: int, db: Session = Depends(get_db_session)):
    rows = (
        db.query(CrossChannelCitation)
        .filter(
            or_(
                CrossChannelCitation.original_post_id == creative_id,
                CrossChannelCitation.citing_post_id == creative_id,
            )
        )
        .all()
    )
    out = []
    for r in rows:
        other_id = r.citing_post_id if r.original_post_id == creative_id else r.original_post_id
        other = db.query(Post).filter(Post.id == other_id).first()
        if not other:
            continue
        out.append({
            "post_id": other.id,
            "channel": other.channel,
            "post_url": other.post_url,
            "image_path": other.image_path,
            "similarity_score": r.similarity_score,
            "detected_at": r.detected_at.isoformat() if r.detected_at else None,
        })
    return {"creative_id": creative_id, "citations": out, "count": len(out)}


# =====================================================================
# Phase 6 — Full-text search
# =====================================================================


@app.get("/api/search")
async def search_creatives(
    q: Optional[str] = None,
    tags: Optional[str] = None,
    er_min: Optional[float] = None,
    channel: Optional[str] = None,
    moderation: Optional[str] = None,
    relevant_only: bool = False,
    days: Optional[int] = None,
    dedupe: bool = False,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db_session),
):
    """Polymorphic search over posts + analyses.

    Uses PostgreSQL full-text search when available, falls back to ILIKE.
    """
    base = (
        db.query(Post)
        .options(joinedload(Post.images).joinedload(Image.analysis))
        .order_by(Post.date.desc())
    )

    if q:
        if db.bind and db.bind.dialect.name == "postgresql":
            tsq = sql_text(
                "to_tsvector('russian', "
                "  COALESCE(posts.text, '') || ' ' || COALESCE(posts.channel, '')"
                ") @@ plainto_tsquery('russian', :q)"
            )
            base = base.filter(tsq.bindparams(q=q))
        else:
            like = f"%{q}%"
            base = base.filter(or_(Post.text.ilike(like), Post.channel.ilike(like)))

    if er_min is not None:
        base = base.filter(Post.er >= er_min)
    channels_list = _parse_channels_param(channel)
    if channels_list:
        base = base.filter(Post.channel.in_(channels_list))

    if days is not None and days > 0:
        from datetime import timedelta
        since = datetime.utcnow() - timedelta(days=int(days))
        base = base.filter(Post.date >= since)

    if tags or moderation or relevant_only:
        base = base.join(Image, Image.post_id == Post.id).join(
            Analysis, Analysis.image_id == Image.id
        )
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            if tag_list:
                # OR-семантика: пост подходит, если в его тегах есть ХОТЯ БЫ один
                # из выбранных пользователем тегов.
                base = base.filter(
                    or_(*[Analysis.tags.ilike(f'%"{t}"%') for t in tag_list])
                )
        if moderation:
            base = base.filter(Analysis.moderation_status == moderation)
        if relevant_only:
            base = base.filter(_relevant_creative_filter())

    if dedupe:
        all_rows = base.all()
        kept, dup_map = _dedupe_posts_by_hash(all_rows)
        total = len(kept)
        rows = kept[offset: offset + limit]
    else:
        total = base.count()
        rows = base.limit(limit).offset(offset).all()
        dup_map = {}

    creatives = []
    for post in rows:
        item = _serialize_post(post)
        if dedupe:
            dups = dup_map.get(post.id, [])
            item["duplicate_count"] = len(dups)
            item["duplicates"] = [
                {
                    "id": d.id,
                    "channel": d.channel,
                    "post_url": _fix_post_url(d),
                    "date": d.date.isoformat() if d.date else None,
                    "views": d.views,
                    "er": round(d.er or 0, 2),
                }
                for d in dups
            ]
        creatives.append(item)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "deduped": bool(dedupe),
        "creatives": creatives,
    }


# =====================================================================
# Channel-filter settings (Phase 7) — convenience endpoints
# =====================================================================


@app.get("/api/channel-filters")
async def get_channel_filters(db: Session = Depends(get_db_session)):
    settings = db.query(Settings).filter(
        Settings.key.in_([
            "channel_min_subscribers",
            "channel_min_er",
            "channel_require_reactions",
            "channel_min_posts_per_week",
            "channel_search_keywords",
        ])
    ).all()
    out = {s.key: s.value for s in settings}
    return {
        "min_subscribers": int(float(out.get("channel_min_subscribers", "5000") or 5000)),
        "min_er": float(out.get("channel_min_er", "2.0") or 2.0),
        "require_reactions": (out.get("channel_require_reactions", "true") or "true").lower() == "true",
        "min_posts_per_week": float(out.get("channel_min_posts_per_week", "1.0") or 1.0),
        "search_keywords": out.get("channel_search_keywords", "") or "",
    }


@app.post("/api/channel-filters")
async def update_channel_filters(
    payload: Dict[str, Any],
    db: Session = Depends(get_db_session),
):
    mapping = {
        "min_subscribers": "channel_min_subscribers",
        "min_er": "channel_min_er",
        "require_reactions": "channel_require_reactions",
        "min_posts_per_week": "channel_min_posts_per_week",
        "search_keywords": "channel_search_keywords",
    }
    for in_key, db_key in mapping.items():
        if in_key not in payload:
            continue
        value = payload[in_key]
        if isinstance(value, bool):
            value_str = "true" if value else "false"
        else:
            value_str = str(value)
        row = db.query(Settings).filter(Settings.key == db_key).first()
        if row:
            row.value = value_str
        else:
            db.add(Settings(key=db_key, value=value_str))
    db.commit()
    return {"status": "success"}


# =====================================================================
# Stats helper for citations / tags (used by UI dashboards)
# =====================================================================


@app.get("/api/stats/extended")
async def get_extended_stats(db: Session = Depends(get_db_session)):
    total_citations = db.query(CrossChannelCitation).count()
    pending = db.query(Analysis).filter(Analysis.moderation_status == "pending").count()
    approved = db.query(Analysis).filter(Analysis.moderation_status == "approved").count()
    rejected = db.query(Analysis).filter(Analysis.moderation_status == "rejected").count()
    target_ready = db.query(Analysis).filter(Analysis.target_creative_ready == True).count()  # noqa: E712
    return {
        "total_citations": total_citations,
        "moderation": {"pending": pending, "approved": approved, "rejected": rejected},
        "target_creative_ready": target_ready,
    }


# =====================================================================
# Database maintenance: clear creatives
# =====================================================================


@app.post("/api/creatives/clear")
async def clear_creatives(payload: Optional[Dict[str, Any]] = None, db: Session = Depends(get_db_session)):
    """Полная очистка базы креативов (посты, изображения, анализы, цитирования).

    Каналы, настройки и логи планировщика НЕ затрагиваются.

    Параметры (JSON body, все опциональные):
    - delete_files: bool — также удалить локальные файлы из downloads/ (default: True)
    - confirm: str — должен содержать "CLEAR" для подтверждения операции

    Возвращает количество удалённых записей по каждой таблице.
    """
    payload = payload or {}
    confirm = str(payload.get("confirm") or "").strip().upper()
    if confirm != "CLEAR":
        raise HTTPException(
            status_code=400,
            detail="Подтверждение не получено. Передайте {'confirm': 'CLEAR'} в теле запроса.",
        )

    delete_files = bool(payload.get("delete_files", True))

    stats = {
        "citations": 0,
        "analyses": 0,
        "images": 0,
        "posts": 0,
        "files_deleted": 0,
        "files_failed": 0,
    }

    try:
        # Собираем пути к файлам ДО удаления, чтобы потом физически их стереть
        file_paths: List[str] = []
        if delete_files:
            for img in db.query(Image.file_path).all():
                if img.file_path:
                    file_paths.append(img.file_path)

        # Удаление в правильном порядке (FK)
        stats["citations"] = db.query(CrossChannelCitation).delete(synchronize_session=False)
        # analysis удалится каскадом через images, но на всякий случай чистим явно
        stats["analyses"] = db.query(Analysis).delete(synchronize_session=False)
        stats["images"] = db.query(Image).delete(synchronize_session=False)
        stats["posts"] = db.query(Post).delete(synchronize_session=False)

        db.commit()

        # Удаляем файлы изображений
        if delete_files:
            downloads_root = os.path.abspath("downloads")
            for fp in file_paths:
                try:
                    abs_fp = os.path.abspath(fp)
                    # Защита: удаляем только то, что лежит внутри downloads/
                    if not abs_fp.startswith(downloads_root):
                        stats["files_failed"] += 1
                        continue
                    if os.path.exists(abs_fp):
                        os.remove(abs_fp)
                        stats["files_deleted"] += 1
                except Exception as exc:
                    logging.warning(f"Не удалось удалить файл {fp}: {exc}")
                    stats["files_failed"] += 1

        add_log("WARNING", f"База креативов очищена: {stats}")
        return {"status": "success", "deleted": stats}

    except Exception as exc:
        db.rollback()
        logging.exception("Ошибка при очистке базы креативов")
        raise HTTPException(status_code=500, detail=f"Ошибка очистки: {exc}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
