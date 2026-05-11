#!/usr/bin/env python3
"""
Скрипт для удаления постов без изображений из базы данных.
"""

import sys
import os

# Добавляем текущую директорию в PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db.database import init_database, get_database
from db.models import Post, Image

def clean_posts_without_images():
    """Удаляет посты, у которых нет связанных изображений."""
    
    # Инициализируем БД
    init_database()
    db = get_database()
    
    with db.get_session() as session:
        # Находим все посты без изображений
        posts_without_images = session.query(Post).filter(
            ~Post.images.any()
        ).all()
        
        print(f"\n🔍 Найдено постов без изображений: {len(posts_without_images)}")
        
        if not posts_without_images:
            print("✓ Нет постов для удаления")
            return
        
        # Показываем примеры
        print("\n📋 Примеры постов без изображений:")
        for post in posts_without_images[:5]:
            text_preview = post.text[:60] if post.text else "Без текста"
            print(f"  - ID: {post.id}, Канал: {post.channel}, Текст: {text_preview}...")
        
        # Подтверждение
        response = input(f"\n❓ Удалить {len(posts_without_images)} постов? (yes/no): ")
        
        if response.lower() in ['yes', 'y', 'да']:
            # Удаляем посты
            count = 0
            for post in posts_without_images:
                session.delete(post)
                count += 1
            
            session.commit()
            print(f"\n✓ Удалено {count} постов без изображений")
        else:
            print("\n❌ Отменено")

if __name__ == '__main__':
    clean_posts_without_images()
