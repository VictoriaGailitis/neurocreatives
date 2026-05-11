from sqlalchemy import (
    Column, Integer, String, DateTime, Float, ForeignKey, Text, Boolean, Date, Index
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime

Base = declarative_base()


class Channel(Base):
    """Метаданные Telegram-каналов, прошедших или находящихся в проверке фильтров."""
    __tablename__ = 'channels'

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(255), unique=True, nullable=False)  # @username (без @)
    title = Column(String(500))
    description = Column(Text)
    subscribers_count = Column(Integer, default=0)
    avg_er = Column(Float, default=0.0)
    avg_views = Column(Integer, default=0)
    reactions_enabled = Column(Boolean, default=False)
    posts_per_week = Column(Float, default=0.0)
    category = Column(String(255))           # marketing, brand, advertising
    language = Column(String(50))
    is_active = Column(Boolean, default=True)
    last_parsed_at = Column(DateTime)
    discovered_via = Column(String(100))     # search / manual
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    posts = relationship("Post", back_populates="channel_obj")


class Post(Base):
    __tablename__ = 'posts'

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel = Column(String(255), nullable=False)
    telegram_post_id = Column(Integer, nullable=False)
    text = Column(Text)
    date = Column(DateTime, nullable=False)
    views = Column(Integer, default=0)
    forwards = Column(Integer, default=0)
    replies = Column(Integer, default=0)
    reactions = Column(Integer, default=0)
    engagement = Column(Integer, default=0)
    er = Column(Float, default=0.0)
    image_path = Column(String(500))
    post_url = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)

    # New: connection to Channel registry + reaction details + image hash for citation
    channel_id = Column(Integer, ForeignKey('channels.id'), nullable=True)
    subscribers_at_time = Column(Integer, default=0)
    has_reactions = Column(Boolean, default=False)
    reaction_details = Column(Text)          # JSON
    image_hash = Column(String(64))          # perceptual phash (hex)
    citation_count = Column(Integer, default=0)

    # Relationships
    images = relationship("Image", back_populates="post", cascade="all, delete-orphan")
    channel_obj = relationship("Channel", back_populates="posts")


Index('idx_posts_image_hash', Post.image_hash)
Index('idx_posts_channel', Post.channel)
Index('idx_posts_date', Post.date)


class Image(Base):
    __tablename__ = 'images'

    id = Column(Integer, primary_key=True, autoincrement=True)
    post_id = Column(Integer, ForeignKey('posts.id'), nullable=False)
    file_path = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    post = relationship("Post", back_populates="images")
    analysis = relationship(
        "Analysis", back_populates="image",
        uselist=False, cascade="all, delete-orphan"
    )


class Analysis(Base):
    __tablename__ = 'analysis'

    id = Column(Integer, primary_key=True, autoincrement=True)
    image_id = Column(Integer, ForeignKey('images.id'), nullable=False)
    scene = Column(String(500))
    objects = Column(Text)
    emotion = Column(String(255))
    creative_type = Column(String(255))
    text_present = Column(String(50))
    visual_strength_score = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Classification flags (Phase 3)
    is_ad_creative = Column(Boolean, default=None)
    is_text_screenshot = Column(Boolean, default=False)
    is_news_photo = Column(Boolean, default=False)
    is_infographic = Column(Boolean, default=False)
    has_brand_logo = Column(Boolean, default=False)
    has_overlay_text = Column(Boolean, default=False)
    ethics_flag = Column(Boolean, default=False)
    ethics_reason = Column(String(500))
    image_is_key_factor = Column(Boolean, default=None)
    solo_image_appeal = Column(Float)

    # Tags as JSON array string
    tags = Column(Text)

    # Target creative for generation (Phase 4)
    target_creative_description = Column(Text)
    target_creative_ready = Column(Boolean, default=False)

    # Moderation
    moderation_status = Column(String(50), default='pending')   # pending/approved/rejected
    moderation_comment = Column(Text)
    moderated_at = Column(DateTime)

    image = relationship("Image", back_populates="analysis")


class CrossChannelCitation(Base):
    """Обнаруженные кросс-канальные цитирования одного и того же визуала."""
    __tablename__ = 'cross_channel_citations'

    id = Column(Integer, primary_key=True, autoincrement=True)
    original_post_id = Column(Integer, ForeignKey('posts.id'), nullable=False)
    citing_post_id = Column(Integer, ForeignKey('posts.id'), nullable=False)
    similarity_score = Column(Float, default=1.0)  # 0..1, 1 = identical
    detected_at = Column(DateTime, default=datetime.utcnow)


Index(
    'idx_citation_pair',
    CrossChannelCitation.original_post_id,
    CrossChannelCitation.citing_post_id,
    unique=True,
)


class Settings(Base):
    __tablename__ = 'settings'

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(255), unique=True, nullable=False)
    value = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScheduleLog(Base):
    __tablename__ = 'schedule_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    run_type = Column(String(20), default='auto', nullable=False)  # auto/manual
    status = Column(String(50), nullable=False)                    # success/error
    images_parsed = Column(Integer, default=0)
    images_analyzed = Column(Integer, default=0)
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class SystemLog(Base):
    """Persistent ring-buffer of application logs.

    Хранит последние записи INFO/WARNING/ERROR, чтобы они переживали рестарт
    контейнера и были видны в UI после перезапуска.
    """
    __tablename__ = 'system_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    level = Column(String(16), nullable=False)        # INFO / WARNING / ERROR / DEBUG
    source = Column(String(64))                       # parser / analysis / api / scheduler / system
    message = Column(Text, nullable=False)


Index('idx_system_logs_created_at', SystemLog.created_at)
