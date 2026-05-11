from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from typing import Generator
import logging
import os

from db.models import Base, Settings, Channel
from db.migrations import run_migrations

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_url: str):
        """Инициализация подключения к базе данных."""
        self.engine = create_engine(db_url, echo=False)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
    
    def create_tables(self):
        """Создание всех таблиц в базе данных + применение лёгких миграций."""
        Base.metadata.create_all(bind=self.engine)
        print("✓ Таблицы созданы успешно")
        try:
            run_migrations(self.engine)
            print("✓ Миграции схемы применены")
        except Exception as exc:
            logger.warning("Schema migrations failed: %s", exc)
    
    def drop_tables(self):
        """Удаление всех таблиц из базы данных."""
        Base.metadata.drop_all(bind=self.engine)
        print("✓ Таблицы удалены")
    
    @contextmanager
    def get_session(self) -> Generator[Session, None, None]:
        """Контекстный менеджер для работы с сессией БД."""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()


# Глобальный экземпляр базы данных
_db_instance = None


def init_database(db_url: str = None):
    """Инициализация глобального экземпляра базы данных."""
    global _db_instance
    if db_url is None:
        # По умолчанию используем PostgreSQL
        db_url = os.getenv(
            'DATABASE_URL',
            'postgresql://postgres:postgres@localhost:5432/neurocreatives'
        )
    # Заменяем postgresql:// на postgresql+psycopg:// для использования psycopg3
    if db_url.startswith('postgresql://'):
        db_url = db_url.replace('postgresql://', 'postgresql+psycopg://')
    elif db_url.startswith('postgresql+psycopg://'):
        pass  # Already correct format
    _db_instance = Database(db_url)
    return _db_instance


def get_database() -> Database:
    """Получение глобального экземпляра базы данных."""
    global _db_instance
    if _db_instance is None:
        raise RuntimeError("База данных не инициализирована. Вызовите init_database() сначала.")
    return _db_instance


def get_db_session() -> Generator[Session, None, None]:
    """Dependency для FastAPI."""
    db = get_database()
    with db.get_session() as session:
        yield session


DEFAULT_SEARCH_KEYWORDS = (
    "marketing\nмаркетинг\nSMM\nтаргет\n"
    "бренд\nbranding\nайдентика\n"
    "реклама\nadvertising\nкреатив\ncreative\nads\n"
    "копирайтинг\nдизайн рекламы\nмедиабаинг\nperformance"
)


def init_default_settings():
    """Идемпотентная инициализация настроек по умолчанию.

    Создаёт только отсутствующие ключи — не затирает уже сохранённые
    значения пользователя.
    """
    db = get_database()
    with db.get_session() as session:
        existing_keys = {row.key for row in session.query(Settings).all()}

        defaults = {
            # Prompt for image analysis (OpenAI API key is read from .env: OPENAI_API_KEY)
            'analysis_prompt': 'Что на этом фото?',
            # Channel filtering (Phase 2 / 7)
            'channel_min_subscribers': '5000',
            'channel_min_er': '2.0',
            'channel_require_reactions': 'true',
            'channel_min_posts_per_week': '1.0',
            'channel_search_keywords': DEFAULT_SEARCH_KEYWORDS,
            # AI image filtering (Phase 3)
            'ai_filter_enabled': 'true',
            'ai_min_solo_appeal': '4',
            'ai_reject_text_screenshots': 'true',
            'ai_reject_news_photos': 'true',
            'ai_reject_unethical': 'true',
        }

        added = 0
        for key, value in defaults.items():
            if key not in existing_keys:
                session.add(Settings(key=key, value=value))
                added += 1

        if added:
            session.commit()
            print(f"✓ Дефолтные настройки добавлены: +{added}")
        else:
            print("✓ Настройки уже существуют")


def seed_channels_from_env(env_channels: list[str] | None) -> int:
    """Однократно добавляет в реестр (`channels`) каналы из `.env` (CHANNELS_TO_PARSE).

    Идемпотентно: если канал с таким `username` уже есть в БД — не трогаем его
    (чтобы не «оживлять» удалённый пользователем канал). Возвращает количество
    добавленных записей. После того как канал попал в БД — он становится
    редактируемым через UI (вкл/выкл, переименование, удаление) и переживает
    рестарт контейнера независимо от значения переменной окружения.
    """
    if not env_channels:
        return 0
    cleaned = [
        (ch or '').strip().lstrip('@')
        for ch in env_channels
    ]
    cleaned = [c for c in cleaned if c]
    if not cleaned:
        return 0

    db = get_database()
    added = 0
    with db.get_session() as session:
        existing = {
            (u or '').lower()
            for (u,) in session.query(Channel.username).all()
        }
        for ch in cleaned:
            if ch.lower() in existing:
                continue
            session.add(Channel(
                username=ch,
                title=ch,
                is_active=True,
                discovered_via='manual',
            ))
            existing.add(ch.lower())
            added += 1
        if added:
            session.commit()
    if added:
        print(f"✓ Каналы из .env добавлены в реестр: +{added}")
    return added
