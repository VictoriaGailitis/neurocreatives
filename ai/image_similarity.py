"""
Phase 5 — Cross-channel citation detection via perceptual hashing.

We compute a 64-bit pHash for every image at parse time and look for
near-duplicates across channels. Hamming-distance comparisons are done in
Python over the candidate set, but the SQL pre-filter narrows that down
heavily by limiting the search to other channels.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class SimilarMatch:
    post_id: int
    channel: str
    distance: int
    similarity: float  # 0..1


def compute_phash(image_path: str) -> Optional[str]:
    """Return a hex-encoded 64-bit perceptual hash for the image, or None."""
    try:
        import imagehash  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError as exc:
        logger.warning("imagehash/Pillow not installed: %s", exc)
        return None
    try:
        with Image.open(image_path) as img:
            return str(imagehash.phash(img))
    except Exception as exc:
        logger.warning("phash failed for %s: %s", image_path, exc)
        return None


def hamming_distance(a: str, b: str) -> int:
    """Hamming distance between two hex hash strings (assumed equal length)."""
    if not a or not b or len(a) != len(b):
        return 64
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 64


def find_similar_posts(
    session: Session,
    image_hash: str,
    *,
    exclude_post_id: Optional[int] = None,
    exclude_channel: Optional[str] = None,
    threshold: int = 10,
    limit: int = 200,
) -> list[SimilarMatch]:
    """Find posts whose pHash is within `threshold` Hamming distance.

    Cross-channel only: when `exclude_channel` is set, posts from that
    channel are skipped (the citation table makes sense only across
    different channels).
    """
    from db.models import Post  # local import to avoid circular dep at import time

    if not image_hash:
        return []

    query = session.query(Post.id, Post.channel, Post.image_hash).filter(
        Post.image_hash.isnot(None),
        Post.image_hash != "",
    )
    if exclude_post_id is not None:
        query = query.filter(Post.id != exclude_post_id)
    if exclude_channel:
        query = query.filter(Post.channel != exclude_channel)

    matches: list[SimilarMatch] = []
    for post_id, channel, other_hash in query.limit(limit * 50).all():
        dist = hamming_distance(image_hash, other_hash)
        if dist <= threshold:
            similarity = 1.0 - dist / 64.0
            matches.append(SimilarMatch(post_id, channel, dist, similarity))

    matches.sort(key=lambda m: m.distance)
    return matches[:limit]


def record_citations(
    session: Session,
    new_post_id: int,
    new_channel: str,
    image_hash: str,
    *,
    threshold: int = 10,
) -> int:
    """Detect cross-channel citations for `new_post_id` and persist them.

    Returns the number of new citation rows created.
    """
    from db.models import CrossChannelCitation, Post

    matches = find_similar_posts(
        session,
        image_hash,
        exclude_post_id=new_post_id,
        exclude_channel=new_channel,
        threshold=threshold,
    )
    if not matches:
        return 0

    created = 0
    for m in matches:
        # Treat the older post as "original"
        original_id, citing_id = sorted([new_post_id, m.post_id])
        exists = (
            session.query(CrossChannelCitation)
            .filter(
                CrossChannelCitation.original_post_id == original_id,
                CrossChannelCitation.citing_post_id == citing_id,
            )
            .first()
        )
        if exists:
            continue
        session.add(
            CrossChannelCitation(
                original_post_id=original_id,
                citing_post_id=citing_id,
                similarity_score=round(m.similarity, 4),
            )
        )
        created += 1

    if created:
        # bump citation_count on every involved post
        post_ids = {new_post_id, *(m.post_id for m in matches)}
        for pid in post_ids:
            post = session.query(Post).filter(Post.id == pid).first()
            if post is not None:
                post.citation_count = (post.citation_count or 0) + 1

    return created


def _post_popularity(post) -> tuple:
    """Ключ популярности поста: больше engagement, затем ER, затем просмотры.

    Используется для выбора «лидера» группы дублей.
    """
    return (
        post.engagement or 0,
        post.er or 0.0,
        post.views or 0,
    )


def find_duplicate_group_leader(
    session: Session,
    image_hash: str,
    *,
    exclude_post_id: Optional[int] = None,
    threshold: int = 10,
):
    """Найти «лидера» группы near-duplicate постов по перцептивному хешу.

    В отличие от :func:`find_similar_posts` (кросс-канальные цитирования),
    дедуп L1 работает по ВСЕМ каналам: одинаковый креатив, где бы он ни был
    запощен, сворачивается в один самый популярный пост.

    Возвращает объект ``Post`` — лидера группы (самый популярный среди
    найденных дублей), либо ``None``, если дублей нет.
    """
    from db.models import Post

    if not image_hash:
        return None

    query = session.query(Post).filter(
        Post.image_hash.isnot(None),
        Post.image_hash != "",
    )
    if exclude_post_id is not None:
        query = query.filter(Post.id != exclude_post_id)

    candidates = []
    for post in query.limit(5000).all():
        dist = hamming_distance(image_hash, post.image_hash)
        if dist <= threshold:
            candidates.append(post)

    if not candidates:
        return None

    # «Лидер» — самый популярный среди уже существующих near-duplicate постов.
    candidates.sort(key=_post_popularity, reverse=True)
    return candidates[0]


def collapse_into_leader(session: Session, new_post, leader) -> object:
    """Свернуть дубли так, чтобы лидером был самый популярный пост.

    Сравнивает популярность ``new_post`` и текущего ``leader``:

      * если новый пост популярнее — лидерство переходит к нему: бывший лидер
        и все его дубли перецепляются на новый пост;
      * иначе — новый пост помечается как дубль лидера.

    Возвращает актуального лидера группы (на случай смены лидерства).
    Проставляет ``prefilter_status='duplicate'`` и ``duplicate_of`` у
    проигравших, а у лидера накапливает ``duplicate_count``.
    """
    from db.models import Post

    # Поднимаемся к корню группы, если leader сам оказался дублем.
    leader = _resolve_root_leader(session, leader)

    new_pop = _post_popularity(new_post)
    leader_pop = _post_popularity(leader)

    if new_pop > leader_pop:
        # Смена лидера: переносим бывшего лидера и его дубли на new_post.
        followers = (
            session.query(Post)
            .filter(Post.duplicate_of == leader.id)
            .all()
        )
        for f in followers:
            f.duplicate_of = new_post.id
            f.prefilter_status = "duplicate"
            f.prefilter_stage = "L1"

        leader.duplicate_of = new_post.id
        leader.prefilter_status = "duplicate"
        leader.prefilter_stage = "L1"

        new_post.prefilter_status = "passed"
        new_post.duplicate_of = None
        new_post.duplicate_count = (leader.duplicate_count or 0) + len(followers) + 1
        leader.duplicate_count = 0
        return new_post

    # Лидер остаётся прежним; new_post — дубль.
    new_post.duplicate_of = leader.id
    new_post.prefilter_status = "duplicate"
    new_post.prefilter_stage = "L1"
    leader.duplicate_count = (leader.duplicate_count or 0) + 1
    return leader


def _resolve_root_leader(session: Session, post):
    """Идём по цепочке duplicate_of вверх до корневого лидера группы."""
    from db.models import Post

    seen = set()
    current = post
    while current is not None and current.duplicate_of:
        if current.id in seen:  # защита от циклов
            break
        seen.add(current.id)
        parent = session.query(Post).filter(Post.id == current.duplicate_of).first()
        if parent is None:
            break
        current = parent
    return current


def backfill_hashes(session: Session, batch_size: int = 200) -> int:
    """Compute pHash for every existing post that has an image but no hash."""
    from db.models import Image as ImageModel
    from db.models import Post

    updated = 0
    while True:
        rows: Iterable[Post] = (
            session.query(Post)
            .filter((Post.image_hash.is_(None)) | (Post.image_hash == ""))
            .limit(batch_size)
            .all()
        )
        rows = list(rows)
        if not rows:
            break

        for post in rows:
            path = post.image_path
            if not path:
                img = (
                    session.query(ImageModel)
                    .filter(ImageModel.post_id == post.id)
                    .first()
                )
                path = img.file_path if img else None
            if not path:
                post.image_hash = ""  # mark as processed even if missing file
                continue
            h = compute_phash(path)
            post.image_hash = h or ""
            if h:
                updated += 1
        session.commit()

    return updated
