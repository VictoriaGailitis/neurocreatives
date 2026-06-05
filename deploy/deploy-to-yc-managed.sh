#!/bin/bash

# Скрипт деплоя Neurocreatives на Yandex Cloud VPS
# Вариант с ВНЕШНЕЙ базой — Yandex Cloud Managed PostgreSQL.
# Использует docker-compose.production.yml (только сервис app, без контейнерного Postgres).
#
# Отличия от deploy-to-yc.sh:
#   - НЕ поднимает контейнер с PostgreSQL (нет тома postgres_data на VM)
#   - Требует, чтобы DATABASE_URL в .env указывал на кластер Managed PostgreSQL
#   - Все docker-compose команды выполняются с флагом -f docker-compose.production.yml
#
# IP: 84.201.150.6
# Path: /apps/neurocreatives

set -e

VPS_HOST="84.201.150.6"
VPS_USER="yc-user"
SSH_KEY="~/.ssh/yc-yacloud"
APP_DIR="/apps/neurocreatives"
COMPOSE_FILE="docker-compose.production.yml"

echo "🚀 Начало деплоя Neurocreatives на Yandex Cloud (Managed PostgreSQL)..."

# Подключение к VPS и выполнение команд
ssh -i $SSH_KEY $VPS_USER@$VPS_HOST << 'ENDSSH'
    set -e

    COMPOSE_FILE="docker-compose.production.yml"
    REPO_URL="https://github.com/VictoriaGailitis/neurocreatives.git"

    echo "📁 Создание директории приложения..."
    sudo mkdir -p /apps/neurocreatives
    sudo chown -R yc-user:yc-user /apps/neurocreatives

    cd /apps

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
        echo "❗ ВАЖНО: создан .env из шаблона. Отредактируйте /apps/neurocreatives/.env перед запуском!"
        echo "   Обязательно укажите DATABASE_URL на кластер Managed PostgreSQL, например:"
        echo "   DATABASE_URL=postgresql://<user>:<password>@<host>.mdb.yandexcloud.net:6432/<db>?sslmode=verify-full"
        echo ""
        echo "   А также: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_STRING, OPENAI_API_KEY"
        echo ""
        echo "🛑 Деплой остановлен. Заполните .env и запустите скрипт повторно."
        exit 1
    fi

    # Проверка, что DATABASE_URL задан и НЕ указывает на локальный контейнер db
    echo "🔎 Проверка DATABASE_URL для Managed PostgreSQL..."
    DB_URL="$(grep -E '^DATABASE_URL=' .env | head -n1 | cut -d= -f2-)"
    if [ -z "$DB_URL" ]; then
        echo "❌ В .env отсутствует или пуст DATABASE_URL. Для Managed-БД он обязателен."
        echo "   Пример: DATABASE_URL=postgresql://user:pass@host.mdb.yandexcloud.net:6432/db?sslmode=verify-full"
        exit 1
    fi
    case "$DB_URL" in
        *@db:*)
            echo "❌ DATABASE_URL указывает на контейнерный хост 'db'."
            echo "   Для варианта с Managed PostgreSQL укажите внешний хост (*.mdb.yandexcloud.net)."
            echo "   Если нужен контейнерный Postgres — используйте deploy/deploy-to-yc.sh."
            exit 1
            ;;
    esac

    # Скачивание корневого сертификата Yandex Cloud (нужен для sslmode=verify-full)
    YC_CERT="$HOME/.postgresql/root.crt"
    if [ ! -f "$YC_CERT" ]; then
        echo "🔐 Установка корневого сертификата Yandex Cloud..."
        mkdir -p "$HOME/.postgresql"
        curl -fsSL "https://storage.yandexcloud.net/cloud-certs/CA.pem" \
            --output "$YC_CERT" && chmod 0600 "$YC_CERT" || \
            echo "⚠️  Не удалось скачать сертификат. Если используете sslmode=verify-full, установите его вручную."
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
    $DC -f "$COMPOSE_FILE" down || true

    echo "🔨 Сборка нового образа..."
    $DC -f "$COMPOSE_FILE" build --no-cache

    echo "🚀 Запуск контейнера..."
    $DC -f "$COMPOSE_FILE" up -d

    echo "🧹 Очистка старых образов..."
    docker system prune -f

    echo "✅ Деплой завершен!"
    echo ""
    echo "📊 Статус контейнера:"
    $DC -f "$COMPOSE_FILE" ps

    echo ""
    echo "📋 Последние логи:"
    $DC -f "$COMPOSE_FILE" logs --tail=20

    echo ""
    echo "🌐 Приложение доступно на:"
    echo "   http://84.201.150.6:8000"
ENDSSH

echo ""
echo "✅ Скрипт выполнен успешно!"
echo ""
echo "📝 Следующие шаги:"
echo "1. (Если .env только что создан) отредактируйте его на сервере и укажите Managed DATABASE_URL:"
echo "   ssh -i ~/.ssh/yc-yacloud yc-user@84.201.150.6"
echo "   nano /apps/neurocreatives/.env"
echo ""
echo "2. Перезапустите контейнер после редактирования .env:"
echo "   cd /apps/neurocreatives && docker-compose -f docker-compose.production.yml restart"
echo ""
echo "3. (Опционально) прогоните backfill предфильтра после обновления:"
echo "   curl -s -X POST http://localhost:8000/api/prefilter-backfill"
echo ""
echo "4. Проверьте логи:"
echo "   docker-compose -f docker-compose.production.yml logs -f"
