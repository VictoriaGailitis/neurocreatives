#!/bin/bash

# Скрипт для первоначальной настройки Yandex Cloud VPS
# Запускать от пользователя с sudo правами

set -e

echo "🚀 Начало настройки VPS для Neurocreatives..."

# Обновление системы
echo "📦 Обновление пакетов..."
sudo apt-get update
sudo apt-get upgrade -y

# Установка Docker (+ Compose v2 plugin)
echo "🐳 Установка Docker..."
sudo apt-get install -y apt-transport-https ca-certificates curl software-properties-common
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
# docker-compose-plugin даёт команду "docker compose" (v2), которая не страдает
# от бага legacy v1 "Not supported URL scheme http+docker".
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Добавление текущего пользователя в группу docker
sudo usermod -aG docker $USER

# Установка Nginx
echo "🌐 Установка Nginx..."
sudo apt-get install -y nginx

# Установка Git
echo "📂 Установка Git..."
sudo apt-get install -y git

# Создание директории проекта
echo "📁 Создание директории проекта..."
sudo mkdir -p /opt/neurocreatives
sudo chown -R $USER:$USER /opt/neurocreatives

# Клонирование репозитория
echo "📥 Клонирование репозитория..."
cd /opt
git clone https://github.com/VictoriaGailitis/neurocreatives.git

# Создание .env файла
echo "⚙️  Создание .env файла..."
cd /opt/neurocreatives
cp .env.example .env
echo "❗ Отредактируйте файл /opt/neurocreatives/.env с вашими настройками!"

# Настройка Nginx
echo "🌐 Настройка Nginx..."
sudo cp deploy/nginx.conf /etc/nginx/sites-available/neurocreatives
echo "❗ Отредактируйте файл /etc/nginx/sites-available/neurocreatives"
echo "   Замените 'your-domain.com' на ваш домен"
echo ""
echo "Затем выполните:"
echo "  sudo ln -s /etc/nginx/sites-available/neurocreatives /etc/nginx/sites-enabled/"
echo "  sudo nginx -t"
echo "  sudo systemctl reload nginx"

# Настройка SSL с Let's Encrypt (опционально)
echo ""
echo "📜 Для настройки SSL сертификата:"
echo "  sudo apt-get install -y certbot python3-certbot-nginx"
echo "  sudo certbot --nginx -d your-domain.com -d www.your-domain.com"

# Запуск приложения
echo ""
echo "🚀 Для запуска приложения:"
echo "  cd /opt/neurocreatives"
echo "  docker compose up -d"

echo ""
echo "✅ Настройка VPS завершена!"
echo ""
echo "📝 Следующие шаги:"
echo "1. Отредактируйте /opt/neurocreatives/.env"
echo "2. Отредактируйте /etc/nginx/sites-available/neurocreatives"
echo "3. Активируйте Nginx конфигурацию"
echo "4. Настройте SSL с certbot (опционально)"
echo "5. Запустите приложение с docker compose up -d"
echo ""
echo "🔐 Для настройки GitHub Actions добавьте в Secrets:"
echo "  VPS_HOST - IP адрес вашего VPS"
echo "  VPS_USERNAME - имя пользователя SSH"
echo "  VPS_SSH_KEY - приватный SSH ключ"
