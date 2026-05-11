from db.database import SessionLocal
from db.models import Channel, Post, Image

session = SessionLocal()

# Проверим каналы
channels = session.query(Channel).filter(Channel.username.in_(['ekavygodno', 'looks_ali'])).all()
print('\n=== КАНАЛЫ ===')
for ch in channels:
    print(f'{ch.title} (@{ch.username})')
    posts_count = session.query(Post).filter(Post.channel_id == ch.id).count()
    images_count = session.query(Image).join(Post).filter(Post.channel_id == ch.id).count()
    print(f'  Постов: {posts_count}')
    print(f'  Изображений: {images_count}')
    
    # Найдем посты без изображений
    posts_without_images = session.query(Post).filter(
        Post.channel_id == ch.id,
        ~Post.images.any()
    ).limit(5).all()
    
    if posts_without_images:
        print(f'  Посты без изображений (первые 5):')
        for post in posts_without_images:
            text_preview = post.text[:50] if post.text else 'No text'
            print(f'    Post ID: {post.telegram_id}, Text: {text_preview}...')

session.close()
