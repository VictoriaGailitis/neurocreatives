"""
Lightweight schema migrations for Neurocreatives.

The project doesn't use Alembic yet; instead we run idempotent ALTER TABLE
statements that add new columns / indexes when they're missing. This makes
new model fields safe to roll out on existing PostgreSQL databases without
manual psql intervention.
"""
from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


# (table, column, ddl-fragment-after-ADD-COLUMN-IF-NOT-EXISTS)
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    # Post — channel registry, reactions, image hash
    ("posts", "channel_id", "INTEGER"),
    ("posts", "subscribers_at_time", "INTEGER DEFAULT 0"),
    ("posts", "has_reactions", "BOOLEAN DEFAULT FALSE"),
    ("posts", "reaction_details", "TEXT"),
    ("posts", "image_hash", "VARCHAR(64)"),
    ("posts", "citation_count", "INTEGER DEFAULT 0"),

    # Analysis — classification + tags + target creative + moderation
    ("analysis", "is_ad_creative", "BOOLEAN"),
    ("analysis", "is_text_screenshot", "BOOLEAN DEFAULT FALSE"),
    ("analysis", "is_news_photo", "BOOLEAN DEFAULT FALSE"),
    ("analysis", "is_infographic", "BOOLEAN DEFAULT FALSE"),
    ("analysis", "has_brand_logo", "BOOLEAN DEFAULT FALSE"),
    ("analysis", "has_overlay_text", "BOOLEAN DEFAULT FALSE"),
    ("analysis", "ethics_flag", "BOOLEAN DEFAULT FALSE"),
    ("analysis", "ethics_reason", "VARCHAR(500)"),
    ("analysis", "image_is_key_factor", "BOOLEAN"),
    ("analysis", "solo_image_appeal", "DOUBLE PRECISION"),
    ("analysis", "tags", "TEXT"),
    ("analysis", "target_creative_description", "TEXT"),
    ("analysis", "target_creative_ready", "BOOLEAN DEFAULT FALSE"),
    ("analysis", "moderation_status", "VARCHAR(50) DEFAULT 'pending'"),
    ("analysis", "moderation_comment", "TEXT"),
    ("analysis", "moderated_at", "TIMESTAMP"),
]


_INDEX_MIGRATIONS: list[tuple[str, str]] = [
    ("idx_posts_image_hash", "CREATE INDEX IF NOT EXISTS idx_posts_image_hash ON posts (image_hash)"),
    ("idx_posts_channel", "CREATE INDEX IF NOT EXISTS idx_posts_channel ON posts (channel)"),
    ("idx_posts_date", "CREATE INDEX IF NOT EXISTS idx_posts_date ON posts (date)"),
    (
        "idx_citation_pair",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_citation_pair "
        "ON cross_channel_citations (original_post_id, citing_post_id)",
    ),
    # GIN full-text search indexes (Phase 6) — Russian config
    (
        "idx_posts_text_search",
        "CREATE INDEX IF NOT EXISTS idx_posts_text_search ON posts "
        "USING GIN (to_tsvector('russian', COALESCE(text, '')))",
    ),
    (
        "idx_analysis_search",
        "CREATE INDEX IF NOT EXISTS idx_analysis_search ON analysis "
        "USING GIN (to_tsvector('russian', "
        "COALESCE(scene, '') || ' ' || COALESCE(objects, '') || ' ' || "
        "COALESCE(tags, '') || ' ' || COALESCE(target_creative_description, '')))",
    ),
]


def _add_columns(conn, migrations: Iterable[tuple[str, str, str]]) -> None:
    for table, column, ddl in migrations:
        stmt = f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}'
        try:
            conn.execute(text(stmt))
        except Exception as exc:  # pragma: no cover - depends on dialect
            logger.warning("Migration skipped for %s.%s: %s", table, column, exc)


def _create_indexes(conn, migrations: Iterable[tuple[str, str]]) -> None:
    for name, stmt in migrations:
        try:
            conn.execute(text(stmt))
        except Exception as exc:  # pragma: no cover
            logger.warning("Index %s skipped: %s", name, exc)


def run_migrations(engine: Engine) -> None:
    """Apply column / index migrations. Safe to call multiple times."""
    if engine.dialect.name != 'postgresql':
        logger.info("Skipping schema migrations: dialect=%s (only PostgreSQL supported)", engine.dialect.name)
        return

    with engine.begin() as conn:
        _add_columns(conn, _COLUMN_MIGRATIONS)
        _create_indexes(conn, _INDEX_MIGRATIONS)

    logger.info("✓ Schema migrations applied")
