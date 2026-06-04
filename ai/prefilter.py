"""
Smart pre-filter pipeline configuration and cheap (non-AI) filtering stages.

The pipeline has four levels, from cheapest to most expensive:

    L0 — text / metrics gate (this module, runs in the parser BEFORE download).
    L1 — perceptual-hash dedup (ai/image_similarity.py; collapses duplicates into
         the most popular post of the group).
    L2 — cheap AI gate (ai/image_classifier.py: ImageClassifier + evaluate()).
    L3 — full creative analysis (ai/image_analysis.py: analyze_image()).

This module owns:
  * PrefilterConfig — единый источник настроек (с дефолтами и чтением из БД Settings).
  * evaluate_text_metrics() — реализация уровня L0.
  * Decision — общий результат «принять / отклонить» с причиной и кодом стадии.

Все настройки задаются разумными дефолтами и редактируются через UI (вкладка
«Предфильтр») / эндпоинт /api/prefilter-settings.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Defaults (согласованы с пользователем). Каждое значение редактируемо в UI.
# --------------------------------------------------------------------------- #
DEFAULT_MIN_TEXT_LEN = 30
DEFAULT_STOPWORDS = [
    "розыгрыш",
    "конкурс",
    "подпишись",
    "erid",
    "вакансия",
    "ищем сотрудника",
]
DEFAULT_MIN_VIEWS = 0
DEFAULT_MIN_ER = 0.0
DEFAULT_LANGUAGES = ["ru", "en"]
DEFAULT_DEDUPE_THRESHOLD = 10


@dataclass
class Decision:
    """Результат любой стадии предфильтра."""

    accepted: bool
    stage: str = ""       # L0 / L1 / L2 (пусто, если accepted)
    reason: str = ""      # человекочитаемая причина отклонения

    @classmethod
    def accept(cls) -> "Decision":
        return cls(True, "", "")

    @classmethod
    def reject(cls, stage: str, reason: str) -> "Decision":
        return cls(False, stage, reason)


@dataclass
class PostMeta:
    """Лёгкие метаданные поста, доступные ДО скачивания картинки."""

    text: str = ""
    views: int = 0
    er: float = 0.0


@dataclass
class PrefilterConfig:
    """Единый конфиг четырёхуровневого предфильтра.

    L0/L1 живут здесь; L2-настройки (ai_*) переиспользуются из
    :class:`ai.image_classifier.FilterConfig`, чтобы не дублировать логику.
    """

    enabled: bool = True

    # L0 — text / metrics
    min_text_len: int = DEFAULT_MIN_TEXT_LEN
    stopwords: list[str] = field(default_factory=lambda: list(DEFAULT_STOPWORDS))
    min_views: int = DEFAULT_MIN_VIEWS
    min_er: float = DEFAULT_MIN_ER
    languages: list[str] = field(default_factory=lambda: list(DEFAULT_LANGUAGES))

    # L1 — phash dedup
    dedupe_enabled: bool = True
    dedupe_threshold: int = DEFAULT_DEDUPE_THRESHOLD

    @classmethod
    def from_settings(cls) -> "PrefilterConfig":
        """Читает настройки из таблицы Settings; недостающие — дефолты."""
        get = _get_setting
        return cls(
            enabled=_to_bool(get("prefilter_enabled"), True),
            min_text_len=_to_int(get("prefilter_min_text_len"), DEFAULT_MIN_TEXT_LEN),
            stopwords=_to_list(get("prefilter_stopwords"), DEFAULT_STOPWORDS),
            min_views=_to_int(get("prefilter_min_views"), DEFAULT_MIN_VIEWS),
            min_er=_to_float(get("prefilter_min_er"), DEFAULT_MIN_ER),
            languages=_to_list(get("prefilter_languages"), DEFAULT_LANGUAGES),
            dedupe_enabled=_to_bool(get("prefilter_dedupe_enabled"), True),
            dedupe_threshold=_to_int(
                get("prefilter_dedupe_threshold"), DEFAULT_DEDUPE_THRESHOLD
            ),
        )

    def to_settings_dict(self) -> dict[str, str]:
        """Сериализация для сохранения в Settings / отдачи в UI."""
        return {
            "prefilter_enabled": "true" if self.enabled else "false",
            "prefilter_min_text_len": str(self.min_text_len),
            "prefilter_stopwords": ", ".join(self.stopwords),
            "prefilter_min_views": str(self.min_views),
            "prefilter_min_er": str(self.min_er),
            "prefilter_languages": ", ".join(self.languages),
            "prefilter_dedupe_enabled": "true" if self.dedupe_enabled else "false",
            "prefilter_dedupe_threshold": str(self.dedupe_threshold),
        }


# --------------------------------------------------------------------------- #
# L0 — text / metrics gate
# --------------------------------------------------------------------------- #
def evaluate_text_metrics(meta: PostMeta, cfg: PrefilterConfig) -> Decision:
    """Дешёвый отсев по тексту и метрикам ДО скачивания картинки.

    Правила (любое сработавшее → reject на стадии L0):
      * текст короче ``min_text_len``;
      * текст содержит стоп-слово;
      * просмотры ниже ``min_views`` (если порог > 0);
      * ER ниже ``min_er`` (если порог > 0);
      * язык текста не входит в whitelist (если whitelist непустой).
    """
    if not cfg.enabled:
        return Decision.accept()

    text = (meta.text or "").strip()

    # 1) Минимальная длина текста.
    if cfg.min_text_len > 0 and len(text) < cfg.min_text_len:
        return Decision.reject("L0", f"text too short ({len(text)}<{cfg.min_text_len})")

    # 2) Стоп-слова (регистронезависимо, по вхождению подстроки).
    lowered = text.lower()
    for sw in cfg.stopwords:
        sw_norm = sw.strip().lower()
        if sw_norm and sw_norm in lowered:
            return Decision.reject("L0", f"stopword: {sw_norm}")

    # 3) Метрики просмотров.
    if cfg.min_views > 0 and (meta.views or 0) < cfg.min_views:
        return Decision.reject("L0", f"views {meta.views}<{cfg.min_views}")

    # 4) ER.
    if cfg.min_er > 0 and (meta.er or 0.0) < cfg.min_er:
        return Decision.reject("L0", f"er {meta.er:.2f}<{cfg.min_er}")

    # 5) Язык (грубая эвристика по алфавиту, без внешних зависимостей).
    if cfg.languages:
        lang = detect_language(text)
        allowed = {l.strip().lower() for l in cfg.languages if l.strip()}
        if lang and allowed and lang not in allowed:
            return Decision.reject("L0", f"language {lang} not in {sorted(allowed)}")

    return Decision.accept()


_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_LATIN_RE = re.compile(r"[a-z]", re.IGNORECASE)


def detect_language(text: str) -> Optional[str]:
    """Очень грубое определение языка: ru / en / None.

    Считаем доли кириллических и латинских букв. Без внешних библиотек —
    этого достаточно для отсева очевидно неподходящего по языку контента.
    Возвращает None, если букв слишком мало для уверенного решения.
    """
    if not text:
        return None
    cyr = len(_CYRILLIC_RE.findall(text))
    lat = len(_LATIN_RE.findall(text))
    if cyr + lat < 5:
        return None
    return "ru" if cyr >= lat else "en"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _get_setting(key: str) -> Optional[str]:
    """Читает одну настройку из БД Settings. Безопасно к ошибкам."""
    try:
        from db.database import get_database
        from db.models import Settings

        db = get_database()
        with db.get_session() as session:
            row = session.query(Settings).filter(Settings.key == key).first()
            return row.value if row else None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to read setting %s: %s", key, exc)
        return None


def _to_bool(v: Optional[str], default: bool) -> bool:
    if v is None or v == "":
        return default
    return str(v).strip().lower() in {"true", "1", "yes", "y", "да"}


def _to_int(v: Optional[str], default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _to_float(v: Optional[str], default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _to_list(v: Optional[str], default: list[str]) -> list[str]:
    if v is None:
        return list(default)
    items = [p.strip() for p in str(v).split(",") if p.strip()]
    # Пустая строка означает «явно очищено» → пустой список (например, языки = любые).
    return items


# --------------------------------------------------------------------------- #
# Backfill — прогон существующих постов через L0 + L1 (идемпотентно)
# --------------------------------------------------------------------------- #
def backfill_prefilter(session, cfg=None, batch_size: int = 200) -> dict:
    """Идемпотентно прогоняет уже существующие посты через L0 (текст/метрики)
    и L1 (phash-дедуп), проставляя prefilter_status/stage/reason и сворачивая
    дубли в самый популярный пост группы.

    Обрабатываются только посты в статусе ``pending`` (дефолт после миграции),
    поэтому команду безопасно запускать повторно: уже размеченные посты
    (passed / rejected / duplicate) пропускаются.

    Посты обрабатываются от самых популярных к менее популярным, чтобы лидеры
    групп дублей размечались раньше своих последователей.

    Возвращает словарь ``{"processed": int, "duplicates": int}``.
    """
    from db.models import Image as ImageModel
    from db.models import Post
    from ai.image_similarity import (
        collapse_into_leader,
        compute_phash,
        find_duplicate_group_leader,
    )

    if cfg is None:
        cfg = PrefilterConfig.from_settings()

    processed = 0
    duplicates = 0

    while True:
        rows = (
            session.query(Post)
            .filter(
                (Post.prefilter_status == "pending")
                | (Post.prefilter_status.is_(None))
            )
            .order_by(
                Post.engagement.desc(),
                Post.er.desc(),
                Post.views.desc(),
            )
            .limit(batch_size)
            .all()
        )
        if not rows:
            break

        for post in rows:
            # L0 — текст/метрики ДО какой-либо тяжёлой работы.
            meta = PostMeta(
                text=post.text or "",
                views=post.views or 0,
                er=post.er or 0.0,
            )
            decision = evaluate_text_metrics(meta, cfg)
            if not decision.accepted:
                post.prefilter_status = "rejected"
                post.prefilter_stage = decision.stage
                post.prefilter_reason = decision.reason
                processed += 1
                continue

            # L1 — phash-дедуп (если включён и есть/можно посчитать хеш).
            if cfg.dedupe_enabled:
                if not post.image_hash:
                    path = post.image_path
                    if not path:
                        img = (
                            session.query(ImageModel)
                            .filter(ImageModel.post_id == post.id)
                            .first()
                        )
                        path = img.file_path if img else None
                    if path:
                        post.image_hash = compute_phash(path) or ""

                if post.image_hash:
                    leader = find_duplicate_group_leader(
                        session,
                        post.image_hash,
                        exclude_post_id=post.id,
                        threshold=cfg.dedupe_threshold,
                    )
                    # Лидером не может быть L0-отклонённый пост.
                    if leader is not None and leader.prefilter_status != "rejected":
                        actual = collapse_into_leader(session, post, leader)
                        if actual is not post:
                            duplicates += 1
                        processed += 1
                        continue

            # Прошёл L0 и дублей не нашлось → помечаем прошедшим до AI-стадий.
            post.prefilter_status = "passed"
            post.prefilter_stage = "L1"
            post.prefilter_reason = ""
            processed += 1

        session.commit()

    return {"processed": processed, "duplicates": duplicates}
