# config.py
import os
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Environment variable {name} is required. "
            f"Set it in .env (see .env.example) or in the host environment."
        )
    return value


# --- Настройки для Telegram ---
# Получи свои api_id и api_hash на my.telegram.org
TELEGRAM_API_ID = int(_require('TELEGRAM_API_ID'))
TELEGRAM_API_HASH = _require('TELEGRAM_API_HASH')

# Список Telegram каналов для парсинга
# Важно: твой аккаунт должен быть подписан на эти каналы
CHANNELS_TO_PARSE = [
    c.strip() for c in os.getenv('CHANNELS_TO_PARSE', '').split(',') if c.strip()
]

# --- Настройки для OpenAI ---
# Получи API ключ на platform.openai.com (можно оставить пустым и задать через UI Settings)
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')

# --- Настройки базы данных ---
# PostgreSQL connection string. Локально через docker compose дефолт ниже,
# в продакшене обязательно переопределяй через DATABASE_URL в .env.
DATABASE_URL = os.getenv(
    'DATABASE_URL',
    'postgresql://neurocreatives:neurocreatives@db:5432/neurocreatives',
)

# --- Настройки сервера ---
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', '8000'))
