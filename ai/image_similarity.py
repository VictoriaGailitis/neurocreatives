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
