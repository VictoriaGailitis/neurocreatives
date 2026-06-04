"""
Модуль планировщика автоматического парсинга и анализа изображений.
Использует APScheduler для управления расписанием задач.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import json
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
import pytz

from db.database import get_database
from db.models import Settings, ScheduleLog
from parser.telegram_parser import TelegramParser
from ai.image_analysis import ImageAnalyzer
import config

logger = logging.getLogger(__name__)

# Глобальный экземпляр планировщика
_scheduler: Optional[BackgroundScheduler] = None
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# ID задачи в планировщике
SCHEDULE_JOB_ID = 'auto_parse_and_analyze'
ANALYSIS_JOB_ID = 'auto_analyze_images'


def _run_scheduled_task():
    """
    Выполнение запланированной задачи парсинга и анализа.
    Эта функция запускается по расписанию.
    """
    logger.info("🕐 Запуск запланированной задачи парсинга и анализа...")
    
    try:
        # Импортируем функцию run_parser_task из API модуля
        from api.server import run_parser_task
        
        # Создаем новый event loop для асинхронных задач
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # Вызываем run_parser_task с параметром run_type='auto'
            loop.run_until_complete(run_parser_task(run_type='auto'))
            logger.info("✅ Запланированная задача выполнена успешно")
            
        finally:
            loop.close()
            
    except Exception as e:
        logger.error(f"❌ Ошибка при выполнении запланированной задачи: {e}", exc_info=True)


def _run_analysis_task():
    """
    Выполнение задачи анализа изображений через единый оркестратор пайплайна.

    Раньше здесь вызывался «сырой» ``analyze_image`` в обход AI-гейта (L2),
    из-за чего планировщик анализировал нерелевантные креативы. Теперь
    используется тот же путь L1→L2→L3, что и в ручном запуске
    (:meth:`ImageAnalyzer.analyze_all_unanalyzed`).

    Возвращает dict-метрики воронки (rejected_l1/l2, passed_full) для записи
    в ``ScheduleLog`` вызывающим кодом.
    """
    logger.info("🔍 Запуск задачи анализа изображений (пайплайн L1→L2→L3)...")

    try:
        analyzer = ImageAnalyzer()
        metrics = analyzer.analyze_all_unanalyzed(return_metrics=True)
        logger.info(
            "✅ Анализ завершён: passed_full=%s, rejected_l1=%s, rejected_l2=%s",
            metrics.get("passed_full", 0),
            metrics.get("rejected_l1", 0),
            metrics.get("rejected_l2", 0),
        )
        return metrics
    except Exception as e:
        logger.error(f"❌ Ошибка при выполнении задачи анализа: {e}", exc_info=True)
        return {"rejected_l1": 0, "rejected_l2": 0, "passed_full": 0, "total": 0}


def _get_schedule_settings() -> Dict[str, Any]:
    """
    Получить настройки расписания из базы данных.
    
    Returns:
        Dict с настройками расписания
    """
    db = get_database()
    session = db.SessionLocal()
    
    try:
        # Получаем все настройки, связанные с расписанием
        settings = session.query(Settings).filter(
            Settings.key.like('schedule_%')
        ).all()
        
        result = {}
        for setting in settings:
            key = setting.key.replace('schedule_', '')
            
            # Парсим значение в зависимости от ключа
            if key == 'enabled':
                result[key] = setting.value.lower() == 'true'
            elif key == 'days':
                try:
                    result[key] = json.loads(setting.value) if setting.value else []
                except:
                    result[key] = []
            elif key == 'end_date':
                try:
                    result[key] = datetime.fromisoformat(setting.value) if setting.value else None
                except:
                    result[key] = None
            else:
                result[key] = setting.value
        
        # Значения по умолчанию
        defaults = {
            'enabled': False,
            'frequency': 'daily',
            'time': '08:00',
            'days': [],
            'end_type': 'indefinite',
            'end_date': None
        }
        
        for key, default_value in defaults.items():
            if key not in result:
                result[key] = default_value
        
        return result
        
    finally:
        session.close()


def _create_trigger(settings: Dict[str, Any]):
    """
    Создать trigger для APScheduler на основе настроек.
    
    Args:
        settings: Настройки расписания
        
    Returns:
        Trigger объект для APScheduler
    """
    frequency = settings.get('frequency', 'daily')
    time_str = settings.get('time', '08:00')
    days = settings.get('days', [])
    end_type = settings.get('end_type', 'indefinite')
    end_date = settings.get('end_date')
    
    # Парсим время
    try:
        hour, minute = map(int, time_str.split(':'))
    except:
        hour, minute = 8, 0
    
    # Определяем дату окончания
    end_date_dt = None
    if end_type == 'until_date' and end_date:
        if isinstance(end_date, str):
            try:
                end_date_dt = datetime.fromisoformat(end_date)
            except:
                pass
        elif isinstance(end_date, datetime):
            end_date_dt = end_date
    
    # Создаем trigger в зависимости от частоты
    if frequency == 'every_5_minutes':
        # Каждые 5 минут (для тестирования)
        trigger = IntervalTrigger(
            minutes=5,
            timezone=MOSCOW_TZ,
            end_date=end_date_dt
        )
    
    elif frequency == 'daily':
        # Каждый день в указанное время
        trigger = CronTrigger(
            hour=hour,
            minute=minute,
            timezone=MOSCOW_TZ,
            end_date=end_date_dt
        )
    
    elif frequency == 'weekly':
        # Каждую неделю (понедельник) в указанное время
        trigger = CronTrigger(
            day_of_week='mon',
            hour=hour,
            minute=minute,
            timezone=MOSCOW_TZ,
            end_date=end_date_dt
        )
    
    elif frequency == 'biweekly':
        # Каждые 2 недели (понедельник) в указанное время
        # APScheduler не поддерживает "каждые 2 недели" напрямую,
        # поэтому используем интервал 14 дней с начальной датой
        start_date = datetime.now(MOSCOW_TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Находим ближайший понедельник
        days_until_monday = (0 - start_date.weekday()) % 7
        if days_until_monday == 0 and start_date < datetime.now(MOSCOW_TZ):
            days_until_monday = 7
        start_date += timedelta(days=days_until_monday)
        
        trigger = IntervalTrigger(
            weeks=2,
            start_date=start_date,
            end_date=end_date_dt,
            timezone=MOSCOW_TZ
        )
    
    elif frequency == 'monthly':
        # Каждый месяц (1 число) в указанное время
        trigger = CronTrigger(
            day=1,
            hour=hour,
            minute=minute,
            timezone=MOSCOW_TZ,
            end_date=end_date_dt
        )
    
    elif frequency == 'custom':
        # Пользовательские дни недели
        if not days or len(days) == 0:
            # Если дни не указаны, используем все дни
            days = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
        
        # Преобразуем числовые дни в названия (0=mon, 1=tue, etc.)
        day_names = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
        day_of_week = []
        for day in days:
            if isinstance(day, int) and 0 <= day <= 6:
                day_of_week.append(day_names[day])
            elif isinstance(day, str):
                day_of_week.append(day)
        
        trigger = CronTrigger(
            day_of_week=','.join(day_of_week),
            hour=hour,
            minute=minute,
            timezone=MOSCOW_TZ,
            end_date=end_date_dt
        )
    
    else:
        # По умолчанию - каждый день
        trigger = CronTrigger(
            hour=hour,
            minute=minute,
            timezone=MOSCOW_TZ,
            end_date=end_date_dt
        )
    
    return trigger


def start_scheduler():
    """
    Запустить планировщик.
    Читает настройки из БД и настраивает расписание.
    """
    global _scheduler
    
    if _scheduler is not None and _scheduler.running:
        logger.warning("⚠️ Планировщик уже запущен")
        return
    
    try:
        # Создаем планировщик
        _scheduler = BackgroundScheduler(timezone=MOSCOW_TZ)
        
        # Получаем настройки расписания
        settings = _get_schedule_settings()
        
        if settings.get('enabled', False):
            # Создаем trigger
            trigger = _create_trigger(settings)
            
            # Добавляем задачу
            _scheduler.add_job(
                _run_scheduled_task,
                trigger=trigger,
                id=SCHEDULE_JOB_ID,
                name='Автоматический парсинг и анализ',
                replace_existing=True,
                max_instances=1  # Только одна задача одновременно
            )
            
            logger.info(f"✅ Планировщик настроен: {settings.get('frequency')} в {settings.get('time')} МСК")
        else:
            logger.info("ℹ️ Планировщик отключен в настройках")
        
        # Добавляем задачу автоматического анализа (каждые 10 минут)
        # Это будет работать независимо от настроек расписания парсинга
        _scheduler.add_job(
            _run_analysis_task,
            trigger=IntervalTrigger(minutes=10, timezone=MOSCOW_TZ),
            id=ANALYSIS_JOB_ID,
            name='Автоматический анализ изображений',
            replace_existing=True,
            max_instances=1  # Только одна задача одновременно
        )
        logger.info("✅ Задача автоанализа настроена: каждые 10 минут")
        
        # Запускаем планировщик
        _scheduler.start()
        logger.info("🚀 Планировщик запущен")
        
    except Exception as e:
        logger.error(f"❌ Ошибка при запуске планировщика: {e}", exc_info=True)
        if _scheduler:
            _scheduler.shutdown(wait=False)
            _scheduler = None
        raise


def stop_scheduler():
    """
    Остановить планировщик.
    """
    global _scheduler
    
    if _scheduler is None:
        logger.warning("⚠️ Планировщик не запущен")
        return
    
    try:
        logger.info("🛑 Остановка планировщика...")
        _scheduler.shutdown(wait=True)
        _scheduler = None
        logger.info("✅ Планировщик остановлен")
    except Exception as e:
        logger.error(f"❌ Ошибка при остановке планировщика: {e}", exc_info=True)


def update_schedule(settings: Dict[str, Any]):
    """
    Обновить расписание планировщика.
    
    Args:
        settings: Новые настройки расписания
    """
    global _scheduler
    
    if _scheduler is None or not _scheduler.running:
        logger.warning("⚠️ Планировщик не запущен, запускаем...")
        start_scheduler()
        return
    
    try:
        # Удаляем существующую задачу, если есть
        if _scheduler.get_job(SCHEDULE_JOB_ID):
            _scheduler.remove_job(SCHEDULE_JOB_ID)
            logger.info("🗑️ Старое расписание удалено")
        
        # Если расписание включено, добавляем новую задачу
        if settings.get('enabled', False):
            trigger = _create_trigger(settings)
            
            _scheduler.add_job(
                _run_scheduled_task,
                trigger=trigger,
                id=SCHEDULE_JOB_ID,
                name='Автоматический парсинг и анализ',
                replace_existing=True,
                max_instances=1
            )
            
            logger.info(f"✅ Расписание обновлено: {settings.get('frequency')} в {settings.get('time')} МСК")
        else:
            logger.info("ℹ️ Планировщик отключен")
            
    except Exception as e:
        logger.error(f"❌ Ошибка при обновлении расписания: {e}", exc_info=True)
        raise


def get_scheduler_status() -> Dict[str, Any]:
    """
    Получить статус планировщика.
    
    Returns:
        Dict со статусом планировщика
    """
    global _scheduler
    
    if _scheduler is None:
        return {
            'running': False,
            'next_run': None
        }
    
    job = _scheduler.get_job(SCHEDULE_JOB_ID)
    
    return {
        'running': _scheduler.running,
        'next_run': job.next_run_time.isoformat() if job and job.next_run_time else None
    }


def is_scheduler_running() -> bool:
    """
    Проверить, запущен ли планировщик.
    
    Returns:
        True если планировщик запущен, иначе False
    """
    global _scheduler
    return _scheduler is not None and _scheduler.running
