import json
import logging
import os
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon import connection as tl_connection
from sqlalchemy.orm import Session

from ai.image_similarity import compute_phash, record_citations
from ai.prefilter import PostMeta, PrefilterConfig, evaluate_text_metrics
from db.database import get_database
from db.models import Image, Post

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


class TelegramParser:
    """Парсер для сбора постов из Telegram каналов."""
    
    def __init__(self, api_id: int, api_hash: str, session_name: str = None):
        self.api_id = api_id
        self.api_hash = api_hash
        import os as _os

        # 1) Highest priority — StringSession from env (great for Docker / CI).
        session_string = _os.environ.get('TELEGRAM_SESSION_STRING')
        if session_string:
            self.session = StringSession(session_string)
            self.session_name = '<StringSession>'
        else:
            # 2) Fallback — .session file on disk.
            # Allow override via env (useful in Docker where /app/sessions is a volume).
            # Fallback to ./sessions/parser_session if the directory exists, otherwise legacy ./parser_session.
            if session_name is None:
                session_name = _os.environ.get('TELEGRAM_SESSION_NAME')
            if session_name is None:
                sessions_dir = _os.path.join(_os.getcwd(), 'sessions')
                if _os.path.isdir(sessions_dir):
                    session_name = _os.path.join('sessions', 'parser_session')
                else:
                    session_name = 'parser_session'
            # Make sure parent directory exists.
            parent = _os.path.dirname(session_name)
            if parent:
                _os.makedirs(parent, exist_ok=True)
            self.session = session_name
            self.session_name = session_name
        self.downloads_folder = 'downloads'

        # Статистика последнего прогона (для воронки предфильтра).
        self.last_run_stats = {'collected': 0, 'rejected_l0': 0}

        # Build proxy configuration from env
        self.connection_cls, self.proxy = _build_proxy()
    
    async def parse_channels(self, channels: list, limit: int = 100, parse_depth: str = 'today', parse_from_date: str = None):
        """
        Парсинг списка каналов.
        
        Args:
            channels: Список каналов для парсинга
            limit: Максимальное количество сообщений для проверки
            parse_depth: Глубина парсинга ('today', '3days', 'from_date')
            parse_from_date: Дата начала парсинга (формат: YYYY-MM-DD), используется если parse_depth='from_date'
        
        Returns:
            Количество собранных постов
        """
        print("🔌 Подключение к Telegram...")
        
        client_kwargs = {"connection_retries": 3, "timeout": 30}
        if self.connection_cls is not None:
            client_kwargs["connection"] = self.connection_cls
        if self.proxy is not None:
            client_kwargs["proxy"] = self.proxy

        async with TelegramClient(self.session, self.api_id, self.api_hash, **client_kwargs) as client:
            print("✓ Подключение успешно")
            
            # Создаем папку для загрузок
            os.makedirs(self.downloads_folder, exist_ok=True)
            
            # Определяем границу дат на основе parse_depth
            today = date.today()
            cutoff_date = None
            
            if parse_depth == 'today':
                cutoff_date = today
                print(f"📅 Парсинг за сегодня ({today})")
            elif parse_depth == '3days':
                cutoff_date = today - timedelta(days=3)
                print(f"📅 Парсинг за последние 3 дня (с {cutoff_date})")
            elif parse_depth == 'from_date' and parse_from_date:
                try:
                    cutoff_date = datetime.strptime(parse_from_date, '%Y-%m-%d').date()
                    print(f"📅 Парсинг с {cutoff_date}")
                except ValueError:
                    cutoff_date = today
                    print(f"⚠️  Неверный формат даты, используется сегодня ({today})")
            else:
                cutoff_date = today
                print(f"📅 Парсинг за сегодня ({today})")
            
            total_posts = 0
            rejected_l0 = 0
            db = get_database()

            # L0-предфильтр: единый конфиг на весь прогон парсинга.
            prefilter_cfg = PrefilterConfig.from_settings()
            
            for channel_name in channels:
                print(f"\n📡 Парсинг канала: {channel_name}")
                
                try:
                    channel = await client.get_entity(channel_name)
                    channel_title = getattr(channel, 'title', None) or channel_name
                    # ВАЖНО: для приватных каналов / каналов без публичного username
                    # channel.username == None. hasattr вернёт True, поэтому раньше
                    # post_url формировался как https://t.me/None/<id>.
                    # Берём fallback: исходный alias из настроек либо c-формат с id.
                    channel_username = getattr(channel, 'username', None)
                    if not channel_username:
                        # Если пользователь передал alias вида "@somechannel" / "somechannel" —
                        # используем его. Иначе — приватный канал, работаем через c/<id>/.
                        if isinstance(channel_name, str) and channel_name.strip().lstrip('@'):
                            channel_username = channel_name.strip().lstrip('@')
                        else:
                            channel_username = None
                    channel_id = getattr(channel, 'id', None)
                    
                    async for message in client.iter_messages(channel, limit=limit):
                        # Проверяем дату
                        message_date = message.date.date()
                        
                        if parse_depth == 'today':
                            # Только сегодняшние посты
                            if message_date != today:
                                if message_date < today:
                                    print(f"  ⏸️  Достигнуты вчерашние посты")
                                    break
                                continue
                        else:
                            # За период (3 дня или с определенной даты)
                            if message_date < cutoff_date:
                                print(f"  ⏸️  Достигнут лимит дат ({cutoff_date})")
                                break
                        
                        # Проверяем наличие текста и фото
                        if message.text and message.photo:
                            print(f"  📥 Найден пост ID: {message.id}")

                            # L0 — дешёвый отсев по тексту/метрикам ДО скачивания.
                            metrics = self._extract_metrics(message)
                            decision = evaluate_text_metrics(
                                PostMeta(
                                    text=message.text or '',
                                    views=metrics['views'],
                                    er=metrics['er'],
                                ),
                                prefilter_cfg,
                            )
                            if not decision.accepted:
                                # Не скачиваем медиа: лёгкая rejected-запись для воронки.
                                with db.get_session() as session:
                                    self._save_rejected_post(
                                        session=session,
                                        channel_title=channel_title,
                                        channel_username=channel_username,
                                        channel_id=channel_id,
                                        message=message,
                                        metrics=metrics,
                                        reason=decision.reason,
                                    )
                                rejected_l0 += 1
                                print(f"  ⛔ L0 отсев: {decision.reason}")
                                continue
                            
                            # Скачиваем изображение
                            image_path = await message.download_media(file=self.downloads_folder)
                            
                            if image_path:
                                # Сохраняем в БД
                                with db.get_session() as session:
                                    self._save_post_to_db(
                                        session=session,
                                        channel_title=channel_title,
                                        channel_username=channel_username,
                                        channel_id=channel_id,
                                        message=message,
                                        image_path=image_path,
                                        metrics=metrics,
                                    )
                                
                                total_posts += 1
                                print(f"  ✓ Пост сохранен в БД")
                
                except Exception as e:
                    print(f"  ❌ Ошибка при парсинге {channel_name}: {e}")
                    continue
            
            print(
                f"\n✓ Парсинг завершен. Собрано постов: {total_posts}, "
                f"отклонено на L0: {rejected_l0}"
            )
            self.last_run_stats = {'collected': total_posts, 'rejected_l0': rejected_l0}
            return total_posts
    
    @staticmethod
    def _build_post_url(channel_username, channel_id, message_id) -> str:
        """Формируем URL поста.

        Если у канала есть публичный username — https://t.me/<username>/<id>.
        Иначе (приватный канал или без алиаса) — https://t.me/c/<chat_id>/<id>,
        это формат внутренних "приватных" ссылок Telegram.
        """
        if channel_username and channel_username != 'None':
            return f"https://t.me/{channel_username}/{message_id}"
        if channel_id is not None:
            # Telegram ожидает "сырой" ID без префикса -100 в публичной ссылке
            cid = abs(int(channel_id))
            cid_str = str(cid)
            if cid_str.startswith('100'):
                cid_str = cid_str[3:]
            return f"https://t.me/c/{cid_str}/{message_id}"
        return ''

    @staticmethod
    def _extract_metrics(message) -> dict:
        """Считает метрики поста (views/ER/реакции) из сообщения Telethon.

        Доступно ДО скачивания медиа — используется и для L0, и при сохранении.
        """
        views = message.views if hasattr(message, 'views') and message.views else 0
        forwards = message.forwards if hasattr(message, 'forwards') and message.forwards else 0
        replies = message.replies.replies if hasattr(message, 'replies') and message.replies else 0

        # Считаем реакции + детализируем
        reactions = 0
        reaction_details = []
        if hasattr(message, 'reactions') and message.reactions and getattr(message.reactions, 'results', None):
            for reaction in message.reactions.results:
                count = getattr(reaction, 'count', 0) or 0
                reactions += count
                emoji = ''
                ri = getattr(reaction, 'reaction', None)
                if ri is not None:
                    emoji = getattr(ri, 'emoticon', '') or getattr(ri, 'document_id', '') or ''
                reaction_details.append({'reaction': str(emoji), 'count': count})

        engagement = forwards + replies + reactions
        er = (engagement / views * 100) if views > 0 else 0.0
        return {
            'views': views,
            'forwards': forwards,
            'replies': replies,
            'reactions': reactions,
            'engagement': engagement,
            'er': er,
            'reaction_details': reaction_details,
        }

    def _save_rejected_post(self, session: Session, channel_title: str, channel_username: str, message, metrics: dict, reason: str, channel_id: int | None = None):
        """Лёгкая запись об отклонённом на L0 посте — без скачивания медиа.

        Нужна для аналитики воронки предфильтра. Если пост уже сохранён
        (например, ранее прошёл фильтр) — не трогаем его.
        """
        existing = session.query(Post).filter_by(
            channel=channel_title,
            telegram_post_id=message.id,
        ).first()
        if existing is not None:
            return

        post = Post(
            channel=channel_title,
            telegram_post_id=message.id,
            text=message.text,
            date=message.date,
            views=metrics['views'],
            forwards=metrics['forwards'],
            replies=metrics['replies'],
            reactions=metrics['reactions'],
            engagement=metrics['engagement'],
            er=metrics['er'],
            image_path='',
            post_url=self._build_post_url(channel_username, channel_id, message.id),
            has_reactions=bool(metrics['reaction_details']),
            reaction_details=json.dumps(metrics['reaction_details'], ensure_ascii=False),
            image_hash='',
            prefilter_status='rejected',
            prefilter_stage='L0',
            prefilter_reason=(reason or '')[:500],
        )
        session.add(post)

    def _save_post_to_db(self, session: Session, channel_title: str, channel_username: str, message, image_path: str, channel_id: int | None = None, metrics: dict | None = None):
        """Сохранение поста в базу данных."""

        post_url = self._build_post_url(channel_username, channel_id, message.id)

        # Метрики могли быть посчитаны на стадии L0 — переиспользуем, иначе считаем.
        if metrics is None:
            metrics = self._extract_metrics(message)
        views = metrics['views']
        forwards = metrics['forwards']
        replies = metrics['replies']
        reactions = metrics['reactions']
        engagement = metrics['engagement']
        er = metrics['er']
        reaction_details = metrics['reaction_details']

        # Perceptual hash for citation detection (Phase 5)
        image_hash = compute_phash(image_path) or ''

        existing_post = session.query(Post).filter_by(
            channel=channel_title,
            telegram_post_id=message.id
        ).first()

        if existing_post:
            existing_post.views = views
            existing_post.forwards = forwards
            existing_post.replies = replies
            existing_post.reactions = reactions
            existing_post.engagement = engagement
            existing_post.er = er
            existing_post.has_reactions = bool(reaction_details)
            existing_post.reaction_details = json.dumps(reaction_details, ensure_ascii=False)
            if image_hash and not existing_post.image_hash:
                existing_post.image_hash = image_hash
            if not existing_post.image_path:
                existing_post.image_path = image_path
            # Пост ранее был отклонён на L0, но теперь прошёл фильтр — реабилитируем.
            if existing_post.prefilter_status == 'rejected':
                existing_post.prefilter_status = 'pending'
                existing_post.prefilter_stage = None
                existing_post.prefilter_reason = None
                if not existing_post.images:
                    session.add(Image(post_id=existing_post.id, file_path=image_path))
            post = existing_post
        else:
            post = Post(
                channel=channel_title,
                telegram_post_id=message.id,
                text=message.text,
                date=message.date,
                views=views,
                forwards=forwards,
                replies=replies,
                reactions=reactions,
                engagement=engagement,
                er=er,
                image_path=image_path,
                post_url=post_url,
                has_reactions=bool(reaction_details),
                reaction_details=json.dumps(reaction_details, ensure_ascii=False),
                image_hash=image_hash,
            )
            session.add(post)
            session.flush()  # we need post.id

            image = Image(
                post_id=post.id,
                file_path=image_path,
            )
            session.add(image)

        # Cross-channel citation detection (Phase 5) — only for new images
        if image_hash and not existing_post:
            try:
                created = record_citations(
                    session,
                    new_post_id=post.id,
                    new_channel=channel_title,
                    image_hash=image_hash,
                )
                if created:
                    logger.info("  🔁 Найдено цитирований: %s (post=%s)", created, post.id)
            except Exception as exc:
                logger.warning("Citation detection failed for post %s: %s", post.id, exc)
