# Neurocreatives

**AI-платформа для сбора и анализа рекламных креативов из Telegram.**

Neurocreatives подписывается на пул Telegram-каналов, скачивает посты с изображениями, прогоняет картинки через OpenAI Vision и показывает результаты в веб-интерфейсе с фильтрами, поиском, дедупликацией и аналитикой.

> Этот файл — единственный источник правды по проекту: фичи, архитектура, локальный запуск (venv / Docker / **Podman**), деплой на VM (Yandex Cloud) и инструкции по безопасности секретов.

---

## Содержание

1. [Что умеет проект (аудит фич)](#что-умеет-проект-аудит-фич)
2. [Архитектура и стек](#архитектура-и-стек)
3. [Структура репозитория](#структура-репозитория)
4. [Переменные окружения](#переменные-окружения)
5. [⚠️ Безопасность секретов перед `git push`](#️-безопасность-секретов-перед-git-push)
6. [Локальный запуск](#локальный-запуск)
   - [Вариант A. Docker Compose](#вариант-a-docker-compose)
   - [Вариант B. Podman / podman-compose](#вариант-b-podman--podman-compose)
   - [Вариант C. Python venv (без контейнеров)](#вариант-c-python-venv-без-контейнеров)
7. [Авторизация Telethon (StringSession)](#авторизация-telethon-stringsession)
8. [Деплой на виртуальную машину](#деплой-на-виртуальную-машину)
   - [Вариант 1. Скрипт `setup-vps.sh` + Docker Compose](#вариант-1-скрипт-setup-vpssh--docker-compose)
   - [Вариант 2. Ручной деплой (Docker / Podman + Nginx + systemd)](#вариант-2-ручной-деплой-docker--podman--nginx--systemd)
   - [Вариант 3. CI/CD через GitHub Actions](#вариант-3-cicd-через-github-actions)
9. [Обновление / откат / бэкап](#обновление--откат--бэкап)
10. [Траблшутинг](#траблшутинг)

---

## Что умеет проект (аудит фич)

### Сбор данных
- **Парсинг Telegram-каналов** через Telethon ([`parser/telegram_parser.py`](parser/telegram_parser.py:1)): скачивание постов, медиа (single + альбомы), реакций, просмотров, репостов.
- **Поддержка StringSession и `.session`-файлов** — авторизация работает как локально, так и в headless-контейнерах ([`generate_session_string.py`](generate_session_string.py:1), [`create_session.py`](create_session.py:1)).
- **Прокси для Telegram** (HTTP / SOCKS5 / MTProxy) через переменные `HTTP_PROXY`, `HTTPS_PROXY`, `TELEGRAM_PROXY`.
- **Channel Discovery** ([`parser/channel_discovery.py`](parser/channel_discovery.py:1)) — автопоиск новых каналов по ключевым словам и критериям (минимум подписчиков, доля рекламы и т. п.).

### AI-анализ
- **OpenAI Vision** ([`ai/image_analysis.py`](ai/image_analysis.py:1)) — описание креативов, извлечение текста, классификация.
- **Классификатор «реклама / не реклама»** ([`ai/image_classifier.py`](ai/image_classifier.py:1)).
- **Перцептивные хеши и дедупликация** ([`ai/image_similarity.py`](ai/image_similarity.py:1)) — `imagehash` для поиска повторов и кросс-канальных цитирований.

### Хранение и API
- **PostgreSQL 16** ([`db/models.py`](db/models.py:1), [`db/database.py`](db/database.py:1)) — модели: `Channel`, `Post`, `Image`, `Analysis`, `Settings`, `ScheduleLog`, `CrossChannelCitation`.
- **Миграции** ([`db/migrations.py`](db/migrations.py:1)) применяются автоматически при старте приложения.
- **FastAPI** ([`api/server.py`](api/server.py:1)) — REST-эндпоинты для постов, каналов, аналитики, настроек, ручного запуска парсера/анализа, SSE-стримов прогресса.

### Веб-интерфейс
- **SPA на Jinja + vanilla JS** ([`web/templates/index.html`](web/templates/index.html:1), [`web/static/app.js`](web/static/app.js:1)) — лента креативов в стиле Yandex Ads, фильтры, модалки, страница настроек.
- **Раздача загруженных медиа** через `/downloads` и `/static`.

### Автоматизация
- **APScheduler** ([`scheduler/scheduler.py`](scheduler/scheduler.py:1)) — фоновые крон-задачи парсинга и анализа, лог запусков в `schedule_logs`.
- **Healthcheck** на `GET /api/stats` встроен в [`Dockerfile`](Dockerfile:23) и используется и в Docker, и в Podman.

### Эксплуатация
- **Docker Compose** для локалки ([`docker-compose.yml`](docker-compose.yml:1)) и для прод-VM ([`docker-compose.production.yml`](docker-compose.production.yml:1)).
- **Nginx-конфиг** ([`deploy/nginx.conf`](deploy/nginx.conf:1)) с TLS, security-хедерами, кешированием статики.
- **Скрипты подготовки VPS** ([`deploy/setup-vps.sh`](deploy/setup-vps.sh:1)) и деплоя ([`deploy/deploy-to-yc.sh`](deploy/deploy-to-yc.sh:1)).
- **GitHub Actions** ([`.github/workflows/deploy.yml`](.github/workflows/deploy.yml:1)) — авто-деплой по push в `main`.

---

## Архитектура и стек

```
┌──────────────┐    HTTPS    ┌─────────────────┐
│   Browser    │ ──────────▶ │     Nginx       │  (443 → 8000)
└──────────────┘             └────────┬────────┘
                                      │ reverse proxy
                                      ▼
                          ┌───────────────────────┐
                          │   FastAPI (uvicorn)   │
                          │   api/server.py       │
                          │   ├─ Scheduler (APS)  │
                          │   ├─ Telethon parser  │
                          │   └─ OpenAI Vision    │
                          └──────────┬────────────┘
                                     │ SQLAlchemy
                                     ▼
                          ┌───────────────────────┐
                          │   PostgreSQL 16       │
                          │   (контейнер или MDB) │
                          └───────────────────────┘
```

- **Backend:** Python 3.13, FastAPI, SQLAlchemy 2, Telethon, OpenAI SDK, APScheduler, Pillow, imagehash.
- **Frontend:** Jinja2 + vanilla JS/CSS.
- **DB:** PostgreSQL 16 (локально — контейнер `postgres:16-alpine`, прод — Yandex Cloud Managed PostgreSQL).
- **Runtime:** Docker / Podman, Nginx, systemd (опционально).
- **CI/CD:** GitHub Actions + SSH-деплой.

---

## Структура репозитория

```
neurocreatives/
├── api/                  FastAPI app + роуты
├── ai/                   image analysis / classifier / similarity
├── db/                   модели, сессия, миграции
├── parser/               Telethon-парсер + channel discovery
├── scheduler/            APScheduler-задачи
├── web/                  templates + статика (UI)
├── deploy/               nginx.conf, setup-vps.sh, deploy-to-yc.sh
├── .github/workflows/    CI/CD
├── Dockerfile
├── docker-compose.yml             ← локалка (app + postgres)
├── docker-compose.production.yml  ← прод (только app, БД внешняя)
├── .env.example          ← шаблон секретов (КОММИТИТСЯ)
├── .gitignore            ← блокирует .env, .env.*, *.session, *.db, credentials.json
├── .dockerignore         ← не пускает секреты внутрь образа
├── requirements.txt
├── config.py             ← читает env, валидирует обязательные переменные
├── run.py                ← локальный entrypoint (uvicorn)
└── main.py
```

---

## Переменные окружения

Полный шаблон — в [`.env.example`](.env.example:1). Минимум для запуска:

| Переменная | Обязательна | Назначение |
|---|---|---|
| `TELEGRAM_API_ID` | ✅ | `api_id` с https://my.telegram.org |
| `TELEGRAM_API_HASH` | ✅ | `api_hash` с https://my.telegram.org |
| `TELEGRAM_SESSION_STRING` | прод-рекомендуется | StringSession от Telethon, чтобы не таскать `.session`-файл |
| `OPENAI_API_KEY` | опционально | ключ OpenAI (можно задать через UI Settings) |
| `DATABASE_URL` | ✅ | строка подключения к PostgreSQL |
| `HOST`, `PORT` | — | по умолчанию `0.0.0.0:8000` |
| `CHANNELS_TO_PARSE` | опционально | список каналов через запятую |
| `HTTP_PROXY` / `HTTPS_PROXY` / `TELEGRAM_PROXY` | опционально | прокси для Telegram |

Локальный `DATABASE_URL` для compose-стека:
```
postgresql://neurocreatives:neurocreatives@db:5432/neurocreatives
```

Продовый (Yandex Cloud Managed PostgreSQL):
```
postgresql+psycopg://USER:PASSWORD@HOST:6432/DB?sslmode=verify-full&target_session_attrs=read-write
```

---

## Локальный запуск

### Вариант A. Docker Compose

Требования: Docker 20.10+ / Docker Desktop, Docker Compose v2.

```bash
# 1. Скопируй шаблон env и заполни секреты
cp .env.example .env
$EDITOR .env

# 2. Подними стек (app + postgres)
docker compose up -d --build

# 3. Открой UI
open http://localhost:8000      # macOS
xdg-open http://localhost:8000  # Linux

# 4. Логи
docker compose logs -f app
```

Стек ([`docker-compose.yml`](docker-compose.yml:1)):
- `app` — FastAPI на `:8000`
- `db` — PostgreSQL 16 на `:5433` (хост) → `:5432` (контейнер)
- volumes: `postgres_data` (БД), `./downloads`, `./sessions`

Остановка / очистка:
```bash
docker compose down              # стоп
docker compose down -v           # стоп + удалить БД
```

---

### Вариант B. Podman / podman-compose

Podman — drop-in замена Docker, работает без daemon и rootless. Все `Dockerfile` и `docker-compose.yml` совместимы.

**Установка:**
```bash
# macOS
brew install podman podman-compose
podman machine init && podman machine start

# Ubuntu/Debian
sudo apt-get install -y podman
pip install --user podman-compose

# Fedora/RHEL
sudo dnf install -y podman podman-compose
```

**Запуск:**
```bash
cp .env.example .env
$EDITOR .env

# Аналог docker compose up -d --build
podman-compose up -d --build

# либо нативный podman-way с pod-ом
podman compose up -d --build     # podman 4.4+

# Логи / статус
podman-compose ps
podman-compose logs -f app
```

**Особенности rootless Podman:**
- Порты ниже 1024 недоступны без `sudo` или `sysctl net.ipv4.ip_unprivileged_port_start=80`. У нас `:8000` — проблемы нет.
- SELinux (Fedora/RHEL): добавь `:Z` к bind-mount-ам, если получаешь permission denied:
  ```yaml
  volumes:
    - ./downloads:/app/downloads:Z
    - ./sessions:/app/sessions:Z
  ```
  (положи это в `docker-compose.override.yml` — он gitignored).
- Healthcheck `curl` уже стоит в `Dockerfile`, podman его уважает.
- На macOS podman работает поверх QEMU VM (`podman machine`) — первый старт может занять ~1 минуту.

**Альтернатива: голый podman без compose**
```bash
# Сеть и БД
podman network create neurocreatives-net
podman volume create neurocreatives_pgdata

podman run -d --name neurocreatives-db --network neurocreatives-net \
  -e POSTGRES_DB=neurocreatives \
  -e POSTGRES_USER=neurocreatives \
  -e POSTGRES_PASSWORD=neurocreatives \
  -v neurocreatives_pgdata:/var/lib/postgresql/data \
  -p 5433:5432 \
  postgres:16-alpine

# Образ приложения
podman build -t neurocreatives:local .

podman run -d --name neurocreatives-app --network neurocreatives-net \
  --env-file .env \
  -e DATABASE_URL=postgresql://neurocreatives:neurocreatives@neurocreatives-db:5432/neurocreatives \
  -v "$PWD/downloads:/app/downloads:Z" \
  -v "$PWD/sessions:/app/sessions:Z" \
  -p 8000:8000 \
  neurocreatives:local
```

---

### Вариант C. Python venv (без контейнеров)

Нужны Python 3.11+ и доступный PostgreSQL (локально или удалённо).

```bash
python3 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
$EDITOR .env                       # как минимум TELEGRAM_API_ID/HASH и DATABASE_URL

# Первичная авторизация в Telegram (создаст .session)
python create_session.py
# либо headless-вариант → StringSession в .env
python generate_session_string.py

python run.py                      # uvicorn на 0.0.0.0:8000
```

---

## Авторизация Telethon (StringSession)

В контейнерах **рекомендуется** StringSession — он не требует интерактивного ввода кода каждый раз.

```bash
# на машине, где есть терминал (локалка)
python generate_session_string.py
# Скрипт спросит код из Telegram и допишет TELEGRAM_SESSION_STRING= в .env
```

Дальше копируешь `.env` (или только эту строку) на сервер — и контейнер стартует без интерактива. Файл `parser_session.session` тоже работает (bind-mount `./sessions:/app/sessions`), но менее удобен для CI/CD.

---

## Деплой на виртуальную машину

Целевая платформа — **Yandex Cloud Compute** (Ubuntu 22.04, 2 vCPU / 2–4 GB RAM / 20 GB SSD). Инструкции применимы к любой Linux VM с публичным IP.

### Чек-лист перед деплоем

- [ ] DNS A-запись домена указывает на IP VM (если нужен HTTPS).
- [ ] Открыты порты 22 / 80 / 443 в security group VM.
- [ ] Готов SSH-ключ для доступа.
- [ ] Готов `.env` со всеми реальными секретами (НЕ коммитим — заливаем `scp`).
- [ ] Решено, где будет БД: контейнер на той же VM (compose-стек) **или** Yandex Cloud Managed PostgreSQL (`docker-compose.production.yml`).
- [ ] `TELEGRAM_SESSION_STRING` уже сгенерирован.

---

### Вариант 1. Скрипт `setup-vps.sh` + Docker Compose

Самый быстрый путь.

```bash
# 1. На своей машине: подключись по SSH
ssh user@VM_IP

# 2. На VM: скачай и запусти скрипт подготовки
curl -O https://raw.githubusercontent.com/<your-org>/neurocreatives/main/deploy/setup-vps.sh
chmod +x setup-vps.sh
./setup-vps.sh
```

Скрипт ставит Docker, Docker Compose, Nginx, Git и клонирует репозиторий в `/opt/neurocreatives` (см. [`deploy/setup-vps.sh`](deploy/setup-vps.sh:1)).

```bash
# 3. Залей .env (с локалки)
scp .env user@VM_IP:/opt/neurocreatives/.env

# 4. На VM: запусти контейнеры
cd /opt/neurocreatives
docker compose -f docker-compose.production.yml up -d --build
docker compose -f docker-compose.production.yml logs -f
```

**Nginx + TLS:**
```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/neurocreatives
sudo sed -i 's/your-domain.com/example.com/g' /etc/nginx/sites-available/neurocreatives
sudo ln -s /etc/nginx/sites-available/neurocreatives /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d example.com -d www.example.com
```

После Certbot раскомментируй `ssl_certificate*` строки в [`deploy/nginx.conf`](deploy/nginx.conf:11) и снова `reload nginx`.

---

### Вариант 2. Ручной деплой (Docker / Podman + Nginx + systemd)

Если ты не хочешь полагаться на `setup-vps.sh`.

**2.1. Поставь рантайм** (выбери один):

```bash
# Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker

# ИЛИ Podman (rootless, без daemon)
sudo apt-get install -y podman
pip3 install --user podman-compose
```

**2.2. Залей код:**
```bash
sudo mkdir -p /opt/neurocreatives && sudo chown $USER:$USER /opt/neurocreatives
git clone https://github.com/<your-org>/neurocreatives.git /opt/neurocreatives
cd /opt/neurocreatives
scp .env user@VM_IP:/opt/neurocreatives/.env   # с локалки
```

**2.3. Запусти:**
```bash
# Docker
docker compose -f docker-compose.production.yml up -d --build

# Podman
podman-compose -f docker-compose.production.yml up -d --build
```

**2.4. systemd-юнит для автозапуска** (`/etc/systemd/system/neurocreatives.service`):
```ini
[Unit]
Description=Neurocreatives stack
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/neurocreatives
ExecStart=/usr/bin/docker compose -f docker-compose.production.yml up -d
ExecStop=/usr/bin/docker compose -f docker-compose.production.yml down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now neurocreatives
```

Для **rootless Podman** используй `systemctl --user` и `podman generate systemd --new --files --name neurocreatives-app`.

**2.5. Nginx + Let's Encrypt** — см. в [Варианте 1](#вариант-1-скрипт-setup-vpssh--docker-compose).

---

### Вариант 3. CI/CD через GitHub Actions

Workflow уже лежит в [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml:1). При push в `main` он подключается по SSH к VM и делает `git pull` + `docker compose up -d --build`.

**Что задать в GitHub → Settings → Secrets and variables → Actions:**

| Secret | Значение |
|---|---|
| `VPS_HOST` | публичный IP VM |
| `VPS_USER` | SSH-пользователь (например `yc-user`) |
| `YC_SSH_KEY` | **приватный** ключ в PEM, целиком (`-----BEGIN OPENSSH PRIVATE KEY-----` … `END`) |

**Важно:** `.env` на сервере workflow **не трогает** — он должен быть положен туда вручную (`scp`). Это сделано специально, чтобы секреты никогда не проходили через GitHub.

Запуск вручную: вкладка **Actions** → `Deploy to Yandex Cloud VPS` → **Run workflow**.

---

## Обновление / откат / бэкап

**Обновление:**
```bash
cd /opt/neurocreatives
git pull
docker compose -f docker-compose.production.yml up -d --build
docker compose -f docker-compose.production.yml logs --tail=100 -f
```

**Откат на предыдущий коммит:**
```bash
git log --oneline | head
git checkout <commit-sha>
docker compose -f docker-compose.production.yml up -d --build
```

**Бэкап БД** (если PostgreSQL в контейнере):
```bash
docker exec neurocreatives-db pg_dump -U neurocreatives neurocreatives \
  | gzip > backup-$(date +%F).sql.gz
```

**Восстановление:**
```bash
gunzip -c backup-2026-01-01.sql.gz \
  | docker exec -i neurocreatives-db psql -U neurocreatives -d neurocreatives
```

Для Yandex Cloud Managed PostgreSQL пользуйся встроенными автобэкапами в консоли.

---

## Траблшутинг

| Симптом | Что делать |
|---|---|
| `RuntimeError: Environment variable TELEGRAM_API_ID is required` | Не заполнен `.env`. Скопируй `.env.example` → `.env` и пропиши значения. |
| Контейнер `app` рестартится | `docker compose logs app` → чаще всего нет коннекта к БД (`DATABASE_URL`) или Telegram (нужен `TELEGRAM_SESSION_STRING` / прокси). |
| `Telethon: 2FA required` / `PhoneCodeInvalidError` | Запусти `python generate_session_string.py` локально и положи строку в `.env`. |
| 502 Bad Gateway от Nginx | Контейнер `app` не слушает `:8000`. Проверь `docker compose ps` и `curl http://localhost:8000/api/stats`. |
| `permission denied` на bind-mount-ах в Podman (SELinux) | Добавь суффикс `:Z` к volume-ам в `docker-compose.override.yml`. |
| Healthcheck падает | `docker compose logs app`, проверь, что миграции прошли и БД доступна. |
| OpenAI ошибки `401` / `quota` | Перегенерируй ключ на platform.openai.com и обнови `OPENAI_API_KEY` в `.env` + `docker compose restart app`. |
| Парсер не видит каналы | Аккаунт, чья сессия используется, должен быть **подписан** на эти каналы. |

---

## Лицензия и контакты

Внутренний проект. Перед публикацией в open-source: ротируй все ключи, ещё раз прогони `gitleaks`, удали `.env.production` из истории.
