"""
Phase 2 — automatic Telegram-channel discovery.

The original plan suggested TDLib (`python-telegram`). It requires native
`libtdjson` and a phone-number login flow which is awkward inside Docker /
CI. Telethon (already used by the parser) speaks the same MTProto API and
exposes everything we need: keyword search of public chats, chat metadata
and message history. This module provides a Telethon-based implementation
of the discovery + filtering logic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Iterable, Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon import connection as tl_connection
from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel as TLChannel
from telethon.tl.types import ChannelFull

from db.database import get_database
from db.models import Channel, Settings

logger = logging.getLogger(__name__)


def _build_proxy() -> tuple[object | None, dict | None]:
    """Read proxy settings from env. Returns (connection_class, proxy).

    Supported envs (any one of these — the first non-empty wins):
      • TELEGRAM_PROXY=socks5://user:pass@host:port
      • TELEGRAM_PROXY=socks5://host:port             (no auth)
      • TELEGRAM_PROXY=mtproxy://host:port:secret_hex
      • TELEGRAM_PROXY=tg://proxy?secret=...&server=host&port=port  (MTProxy link from Telegram)
      • plus generic HTTPS_PROXY / HTTP_PROXY (treated as http proxy).

    No default — proxy must be set via env if needed.
    """
    raw = (
        os.environ.get("TELEGRAM_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )
    if not raw:
        return None, None

    from urllib.parse import parse_qs

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()

    # Handle tg://proxy?secret=... format (MTProxy link from Telegram)
    if scheme == "tg" and parsed.netloc == "proxy":
        query = parse_qs(parsed.query)
        secret = query.get("secret", [None])[0]
        if not secret:
            logger.warning("MTProxy link without secret: %r", raw)
            return None, None
        host = query.get("server", [None])[0]
        port = query.get("port", [None])[0]
        if not host or not port:
            logger.warning("MTProxy link without server/port: %r", raw)
            return None, None
        try:
            port = int(port)
        except ValueError:
            logger.warning("Invalid port in MTProxy link: %r", port)
            return None, None
        logger.info("Using MTProxy: %s:%s", host, port)
        return tl_connection.ConnectionTcpMTProxyRandomizedIntermediate, (host, port, secret)

    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        logger.warning("Could not parse TELEGRAM_PROXY=%r, ignoring", raw)
        return None, None

    if scheme in ("socks5", "socks4", "http"):
        try:
            import socks  # PySocks
        except ImportError:
            logger.warning("PySocks required for SOCKS proxy: pip install pysocks")
            return None, None
        proxy_types = {
            "socks5": socks.SOCKS5,
            "socks4": socks.SOCKS4,
            "http": socks.HTTP,
        }
        proxy_tuple = (
            proxy_types[scheme],
            host,
            port,
            True,  # rdns — resolve DNS through proxy
            parsed.username or None,
            parsed.password or None,
        )
        logger.info("Using %s proxy: %s:%s", scheme.upper(), host, port)
        return None, proxy_tuple

    if scheme == "mtproxy":
        secret = parsed.path.lstrip("/") or parsed.password
        if not secret:
            logger.warning("MTProxy without secret: %r", raw)
            return None, None
        logger.info("Using MTProxy: %s:%s", host, port)
        return tl_connection.ConnectionTcpMTProxyRandomizedIntermediate, (host, port, secret)

    logger.warning("Unknown proxy scheme: %r, ignoring", scheme)
    return None, None


@dataclass
class FilterCriteria:
    min_subscribers: int = 5000
    min_er: float = 2.0
    require_reactions: bool = True
    min_posts_per_week: float = 1.0


@dataclass
class ChannelInfo:
    username: str
    title: str
    description: str = ""
    subscribers: int = 0
    reactions_enabled: bool = False
    avg_er: float = 0.0
    avg_views: int = 0
    posts_per_week: float = 0.0
    discovered_via: str = "search"
    raw: dict = field(default_factory=dict)


def load_criteria_from_settings(session: Session) -> FilterCriteria:
    """Read filter thresholds from the Settings table."""
    def _get(key: str, default: str) -> str:
        row = session.query(Settings).filter(Settings.key == key).first()
        return row.value if row and row.value not in (None, "") else default

    def _f(key: str, default: float) -> float:
        try:
            return float(_get(key, str(default)))
        except (TypeError, ValueError):
            return default

    def _i(key: str, default: int) -> int:
        try:
            return int(float(_get(key, str(default))))
        except (TypeError, ValueError):
            return default

    return FilterCriteria(
        min_subscribers=_i("channel_min_subscribers", 5000),
        min_er=_f("channel_min_er", 2.0),
        require_reactions=_get("channel_require_reactions", "true").lower() == "true",
        min_posts_per_week=_f("channel_min_posts_per_week", 1.0),
    )


def load_search_keywords_from_settings(session: Session) -> list[str]:
    row = session.query(Settings).filter(
        Settings.key == "channel_search_keywords"
    ).first()
    raw = (row.value if row else "") or ""
    keywords = [k.strip() for k in raw.replace(",", "\n").splitlines() if k.strip()]
    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for kw in keywords:
        key = kw.lower()
        if key not in seen:
            seen.add(key)
            out.append(kw)
    return out


class ChannelDiscovery:
    """Telethon-based discovery + statistics computation for channels."""

    def __init__(self, api_id: int, api_hash: str, session_name: str = "parser_session"):
        self.api_id = api_id
        self.api_hash = api_hash
        # StringSession from env wins over a file path — same convention as TelegramParser.
        session_string = os.environ.get("TELEGRAM_SESSION_STRING")
        if session_string:
            self.session = StringSession(session_string)
        else:
            self.session = session_name
        self.session_name = session_name

        # Build proxy configuration from env
        self.connection_cls, self.proxy = _build_proxy()

    async def search_and_filter(
        self,
        keywords: Iterable[str],
        criteria: FilterCriteria,
        *,
        per_keyword_limit: int = 10,
        history_sample: int = 50,
        progress_cb: Optional[callable] = None,
    ) -> list[ChannelInfo]:
        """Search public channels by every keyword, return only those that
        satisfy the filter criteria.

        progress_cb(event: dict) is called on every meaningful step. Events:
          {stage:"start", total_keywords:int}
          {stage:"keyword", index:int, total:int, keyword:str, found_chats:int}
          {stage:"channel", index:int, total:int, keyword:str,
           username:str, accepted:bool, reason:str|None,
           accepted_total:int}
          {stage:"done", accepted_total:int}
        """
        keywords = list(keywords)
        total_keywords = len(keywords)
        accepted: dict[str, ChannelInfo] = {}

        def emit(event: dict) -> None:
            if progress_cb is None:
                return
            try:
                progress_cb(event)
            except Exception:
                logger.exception("progress_cb raised — ignoring")

        emit({"stage": "start", "total_keywords": total_keywords})

        client_kwargs = {"connection_retries": 3, "timeout": 30}
        if self.connection_cls is not None:
            client_kwargs["connection"] = self.connection_cls
        if self.proxy is not None:
            client_kwargs["proxy"] = self.proxy

        async with TelegramClient(self.session, self.api_id, self.api_hash, **client_kwargs) as client:
            for kw_idx, kw in enumerate(keywords, start=1):
                try:
                    info = await client(SearchRequest(q=kw, limit=per_keyword_limit))
                except Exception as exc:
                    logger.warning("Search failed for %s: %s", kw, exc)
                    emit({
                        "stage": "keyword", "index": kw_idx, "total": total_keywords,
                        "keyword": kw, "found_chats": 0, "error": str(exc),
                    })
                    continue

                chats = [c for c in getattr(info, "chats", []) if isinstance(c, TLChannel)]
                emit({
                    "stage": "keyword", "index": kw_idx, "total": total_keywords,
                    "keyword": kw, "found_chats": len(chats),
                })

                for chat in chats:
                    username = getattr(chat, "username", None)
                    if not username or username in accepted:
                        continue

                    reason: Optional[str] = None
                    try:
                        ch_info = await self._enrich_channel(client, chat, history_sample)
                    except Exception as exc:
                        logger.warning("Enrich failed for @%s: %s", username, exc)
                        emit({
                            "stage": "channel", "index": kw_idx, "total": total_keywords,
                            "keyword": kw, "username": username, "accepted": False,
                            "reason": f"enrich_failed: {exc}",
                            "accepted_total": len(accepted),
                        })
                        continue

                    if self._matches(ch_info, criteria):
                        accepted[username] = ch_info
                        is_accepted = True
                    else:
                        is_accepted = False
                        reason = self._reject_reason(ch_info, criteria)

                    emit({
                        "stage": "channel", "index": kw_idx, "total": total_keywords,
                        "keyword": kw, "username": username, "title": ch_info.title,
                        "subscribers": ch_info.subscribers, "avg_er": ch_info.avg_er,
                        "posts_per_week": ch_info.posts_per_week,
                        "accepted": is_accepted, "reason": reason,
                        "accepted_total": len(accepted),
                    })

        emit({"stage": "done", "accepted_total": len(accepted)})
        return list(accepted.values())

    async def enrich_by_usernames(
        self,
        usernames: Iterable[str],
        *,
        history_sample: int = 50,
        progress_cb: Optional[callable] = None,
        request_delay: float = 0.8,
        max_flood_wait: int = 60,
    ) -> list[ChannelInfo]:
        """Получить статистику по списку каналов по их @username,
        используя те же Telethon-вызовы, что и автопоиск, но без фильтрации.

        progress_cb(event: dict) — события вида:
          {stage:"start", total:int}
          {stage:"item", username:str, ok:bool, reason:str|None,
           subscribers:int|None, avg_er:float|None}
          {stage:"done", ok:int, total:int}

        Параметры:
          request_delay — пауза между каналами (сек), чтобы не словить FloodWait.
          max_flood_wait — если Telegram попросил подождать больше этого
            количества секунд — пропускаем канал (иначе ждём и продолжаем).
        """
        usernames = [str(u or '').strip().lstrip('@') for u in usernames]
        usernames = [u for u in usernames if u]
        if not usernames:
            if progress_cb:
                try: progress_cb({"stage": "done", "ok": 0, "total": 0})
                except Exception: pass
            return []

        client_kwargs = {"connection_retries": 3, "timeout": 30}
        if self.connection_cls is not None:
            client_kwargs["connection"] = self.connection_cls
        if self.proxy is not None:
            client_kwargs["proxy"] = self.proxy

        def emit(ev: dict) -> None:
            if progress_cb is None:
                return
            try:
                progress_cb(ev)
            except Exception:
                logger.exception("enrich progress_cb raised")

        emit({"stage": "start", "total": len(usernames)})

        results: list[ChannelInfo] = []
        async with TelegramClient(self.session, self.api_id, self.api_hash, **client_kwargs) as client:
            for u in usernames:
                # 1) get_entity с обработкой FloodWait
                chat = None
                last_err: Optional[str] = None
                for attempt in range(3):
                    try:
                        chat = await client.get_entity(u)
                        break
                    except FloodWaitError as fw:
                        wait_s = int(getattr(fw, "seconds", 0) or 0)
                        if wait_s > max_flood_wait:
                            last_err = f"flood_wait {wait_s}s — пропуск"
                            logger.warning("enrich: FloodWait %ss for @%s — skip", wait_s, u)
                            break
                        logger.info("enrich: FloodWait %ss for @%s — waiting", wait_s, u)
                        await asyncio.sleep(wait_s + 1)
                        continue
                    except Exception as exc:
                        last_err = f"get_entity: {exc}"
                        logger.warning("enrich: get_entity(%s) failed: %s", u, exc)
                        break
                if chat is None:
                    emit({"stage": "item", "username": u, "ok": False, "reason": last_err or "not_found"})
                    await asyncio.sleep(request_delay)
                    continue
                if not isinstance(chat, TLChannel):
                    emit({"stage": "item", "username": u, "ok": False, "reason": "not_a_channel"})
                    await asyncio.sleep(request_delay)
                    continue

                # 2) обогащение метриками с обработкой FloodWait
                info: Optional[ChannelInfo] = None
                last_err = None
                for attempt in range(3):
                    try:
                        info = await self._enrich_channel(client, chat, history_sample)
                        break
                    except FloodWaitError as fw:
                        wait_s = int(getattr(fw, "seconds", 0) or 0)
                        if wait_s > max_flood_wait:
                            last_err = f"flood_wait {wait_s}s — пропуск"
                            break
                        await asyncio.sleep(wait_s + 1)
                        continue
                    except Exception as exc:
                        last_err = f"enrich: {exc}"
                        logger.warning("enrich: enrich(%s) failed: %s", u, exc)
                        break
                if info is None:
                    emit({"stage": "item", "username": u, "ok": False, "reason": last_err or "enrich_failed"})
                    await asyncio.sleep(request_delay)
                    continue

                info.discovered_via = "manual"
                if not info.username:
                    info.username = u
                results.append(info)
                emit({
                    "stage": "item", "username": u, "ok": True,
                    "reason": None,
                    "subscribers": info.subscribers,
                    "avg_er": info.avg_er,
                    "title": info.title,
                })
                # пауза между каналами, чтобы не упереться в FloodWait
                await asyncio.sleep(request_delay)
        emit({"stage": "done", "ok": len(results), "total": len(usernames)})
        return results

    @staticmethod
    def _matches(info: ChannelInfo, criteria: FilterCriteria) -> bool:
        if info.subscribers < criteria.min_subscribers:
            return False
        if criteria.require_reactions and not info.reactions_enabled:
            return False
        if info.avg_er < criteria.min_er:
            return False
        if info.posts_per_week < criteria.min_posts_per_week:
            return False
        return True

    @staticmethod
    def _reject_reason(info: ChannelInfo, criteria: FilterCriteria) -> str:
        if info.subscribers < criteria.min_subscribers:
            return f"подписчиков {info.subscribers} < {criteria.min_subscribers}"
        if criteria.require_reactions and not info.reactions_enabled:
            return "реакции отключены"
        if info.avg_er < criteria.min_er:
            return f"ER {info.avg_er}% < {criteria.min_er}%"
        if info.posts_per_week < criteria.min_posts_per_week:
            return f"постов/нед {info.posts_per_week} < {criteria.min_posts_per_week}"
        return "ok"

    async def _enrich_channel(
        self,
        client: TelegramClient,
        chat: TLChannel,
        history_sample: int,
    ) -> ChannelInfo:
        full = await client.get_entity(chat)  # ensure we have full entity
        # full info — gives subscriber count, reactions config, description
        try:
            from telethon.tl.functions.channels import GetFullChannelRequest

            full_info = await client(GetFullChannelRequest(channel=chat))
        except Exception as exc:
            logger.warning("GetFullChannel failed for @%s: %s", chat.username, exc)
            full_info = None

        subscribers = 0
        reactions_enabled = False
        description = ""
        if full_info is not None:
            ch_full = getattr(full_info, "full_chat", None)
            if isinstance(ch_full, ChannelFull):
                subscribers = getattr(ch_full, "participants_count", 0) or 0
                description = getattr(ch_full, "about", "") or ""
                reactions_enabled = getattr(ch_full, "available_reactions", None) is not None

        # ER + frequency from recent messages
        avg_er, avg_views, posts_per_week = await self._compute_stats(client, chat, history_sample)

        return ChannelInfo(
            username=chat.username or "",
            title=getattr(chat, "title", "") or "",
            description=description,
            subscribers=subscribers,
            reactions_enabled=reactions_enabled,
            avg_er=avg_er,
            avg_views=avg_views,
            posts_per_week=posts_per_week,
            discovered_via="search",
            raw={"chat_id": chat.id, "access_hash": getattr(chat, "access_hash", None)},
        )

    @staticmethod
    async def _compute_stats(
        client: TelegramClient,
        chat: TLChannel,
        history_sample: int,
    ) -> tuple[float, int, float]:
        total_er = 0.0
        total_views = 0
        count = 0
        timestamps: list[datetime] = []

        async for msg in client.iter_messages(chat, limit=history_sample):
            views = getattr(msg, "views", 0) or 0
            forwards = getattr(msg, "forwards", 0) or 0
            replies_obj = getattr(msg, "replies", None)
            replies = getattr(replies_obj, "replies", 0) if replies_obj else 0
            reactions_total = 0
            reactions_obj = getattr(msg, "reactions", None)
            if reactions_obj is not None and getattr(reactions_obj, "results", None):
                for r in reactions_obj.results:
                    reactions_total += getattr(r, "count", 0) or 0
            engagement = forwards + replies + reactions_total
            er = (engagement / views * 100.0) if views > 0 else 0.0
            total_er += er
            total_views += views
            count += 1
            if msg.date is not None:
                timestamps.append(msg.date)

        if count == 0:
            return 0.0, 0, 0.0

        avg_er = round(total_er / count, 2)
        avg_views = int(total_views / count)
        posts_per_week = 0.0
        if len(timestamps) >= 2:
            span = (max(timestamps) - min(timestamps)).total_seconds() / 86400.0
            if span > 0:
                posts_per_week = round(len(timestamps) / span * 7.0, 2)
        return avg_er, avg_views, posts_per_week


# ------------------------- persistence helpers -------------------------


def upsert_channel(session: Session, info: ChannelInfo) -> Channel:
    """Insert or update a Channel row from a discovered ChannelInfo."""
    row = session.query(Channel).filter(Channel.username == info.username).first()
    payload = {
        "title": info.title,
        "description": info.description,
        "subscribers_count": info.subscribers,
        "avg_er": info.avg_er,
        "avg_views": info.avg_views,
        "reactions_enabled": info.reactions_enabled,
        "posts_per_week": info.posts_per_week,
        "discovered_via": info.discovered_via,
        "is_active": True,
        "updated_at": datetime.utcnow(),
    }
    if row is None:
        row = Channel(username=info.username, **payload)
        session.add(row)
    else:
        for k, v in payload.items():
            setattr(row, k, v)
    return row


def info_to_dict(info: ChannelInfo) -> dict:
    d = asdict(info)
    d.pop("raw", None)
    return d
