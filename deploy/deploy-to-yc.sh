#!/bin/bash

# Скрипт деплоя Neurocreatives на Yandex Cloud VPS
# IP: 84.201.150.6
# Path: /apps/neurocreatives

set -e

VPS_HOST="84.201.150.6"
VPS_USER="yc-user"
SSH_KEY="~/.ssh/yc-yacloud"
APP_DIR="/apps/neurocreatives"

echo "🚀 Начало деплоя Neurocreatives на Yandex Cloud..."

# Подключение к VPS и выполнение команд
ssh -i $SSH_KEY $VPS_USER@$VPS_HOST << 'ENDSSH'
    set -e
    
    echo "📁 Создание директории приложения..."
    sudo mkdir -p /apps/neurocreatives
    sudo chown -R yc-user:yc-user /apps/neurocreatives
    
    cd /apps

    REPO_URL="https://github.com/VictoriaGailitis/neurocreatives.git"

    # Проверка существования репозитория
    if [ -d "neurocreatives/.git" ]; then
        echo "📥 Обновление репозитория..."
        cd neurocreatives
        git remote set-url origin "$REPO_URL"
        git pull origin main
    else
        echo "📥 Клонирование репозитория..."
        git clone "$REPO_URL"
        cd neurocreatives
    fi
    
    echo "⚙️  Настройка переменных окружения..."
    if [ ! -f .env ]; then
        cp .env.example .env
        echo "❗ ВАЖНО: Отредактируйте файл /apps/neurocreatives/.env"
        echo "   Добавьте настоящие значения для:"
        echo "   - TELEGRAM_API_ID"
        echo "   - TELEGRAM_API_HASH"
        echo "   - OPENAI_API_KEY"
        echo "   - DATABASE_URL"
    fi
    
    # Выбор Docker Compose: предпочитаем v2-плагин ("docker compose"),
    # т.к. legacy "docker-compose" v1 (python) ломается с новым requests/urllib3
    # ошибкой "Not supported URL scheme http+docker".
    if docker compose version >/dev/null 2>&1; then
        DC="docker compose"
    else
        DC="docker-compose"
    fi
    echo "🐳 Используется Compose: $DC"

    echo "🐳 Остановка старого контейнера..."
    $DC down || true
    
    echo "🔨 Сборка нового образа..."
    $DC build --no-cache
    
    echo "🚀 Запуск контейнера..."
    $DC up -d
    
    echo "🧹 Очистка старых образов..."
    docker system prune -f
    
    echo "✅ Деплой завершен!"
    echo ""
    echo "📊 Статус контейнера:"
    $DC ps
    
    echo ""
    echo "📋 Последние логи:"
    $DC logs --tail=20
    
    echo ""
    echo "🌐 Приложение доступно на:"
    echo "   http://84.201.150.6:8000"
ENDSSH

echo ""
echo "✅ Скрипт выполнен успешно!"
echo ""
echo "📝 Следующие шаги:"
echo "1. Отредактируйте .env файл на сервере:"
echo "   ssh -i ~/.ssh/yc-yacloud yc-user@84.201.150.6"
echo "   nano /apps/neurocreatives/.env"
echo ""
echo "2. Перезапустите контейнер после редактирования .env:"
echo "   cd /apps/neurocreatives && docker-compose restart"
echo ""
echo "3. Проверьте логи:"
echo "   docker-compose logs -f"
