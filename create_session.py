"""
Удобное создание Telegram-сессии для парсера.

Запусти один раз ЛОКАЛЬНО (там, где удобно ввести код из Telegram):

    python create_session.py

Скрипт создаст файл sessions/parser_session.session.
После этого:
  • Локальный запуск — больше ничего не нужно, парсер сам подхватит сессию.
  • Docker / сервер — скопируй sessions/parser_session.session
    в папку sessions/ на сервере (она монтируется как volume).

Никакие коды/пароли никуда не сохраняются — только итоговый .session файл.
"""

import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

import config

SESSION_DIR = "sessions"
SESSION_NAME = "parser_session"
SESSION_PATH = os.path.join(SESSION_DIR, SESSION_NAME)


def _print_header() -> None:
    print("=" * 60)
    print("  Создание сессии Telegram для neurocreatives-парсера")
    print("=" * 60)
    print(f"  API_ID:   {config.TELEGRAM_API_ID}")
    print(f"  Файл:     {SESSION_PATH}.session")
    print("=" * 60)


async def _ensure_authorized(client: TelegramClient) -> None:
    """Авторизуем клиента, если ещё не авторизован."""
    if await client.is_user_authorized():
        return

    phone = input("📱 Введи номер телефона (в формате +71234567890): ").strip()
    try:
        await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        print("❌ Неверный формат номера. Попробуй ещё раз.")
        sys.exit(1)

    code = input("✉️  Введи код, который пришёл в Telegram: ").strip()
    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        # Включена двухфакторная аутентификация
        password = input("🔒 Введи пароль 2FA (Cloud Password): ").strip()
        await client.sign_in(password=password)


async def main() -> None:
    _print_header()
    os.makedirs(SESSION_DIR, exist_ok=True)

    client = TelegramClient(
        SESSION_PATH,
        config.TELEGRAM_API_ID,
        config.TELEGRAM_API_HASH,
    )

    try:
        await client.connect()
    except ApiIdInvalidError:
        print("❌ Неверные TELEGRAM_API_ID / TELEGRAM_API_HASH в .env или config.py")
        sys.exit(1)

    try:
        await _ensure_authorized(client)
        me = await client.get_me()
        print()
        print("✅ Сессия успешно создана!")
        print(f"   Аккаунт : {me.first_name or ''} {me.last_name or ''}".rstrip())
        if getattr(me, "username", None):
            print(f"   Username: @{me.username}")
        print(f"   ID      : {me.id}")
        print(f"   Файл    : {os.path.abspath(SESSION_PATH)}.session")
        print()
        print("👉 Если запускаешь в Docker/на сервере — скопируй этот файл")
        print(f"   в папку {SESSION_DIR}/ на целевой машине.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹  Отменено пользователем.")
