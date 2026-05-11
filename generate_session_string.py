"""
Полностью автоматическая генерация TELEGRAM_SESSION_STRING.

Запусти один раз ЛОКАЛЬНО:

    python generate_session_string.py

Скрипт:
  1. Спросит телефон → код → 2FA-пароль (это умеет только живой человек,
     Telegram не даёт это автоматизировать).
  2. Сгенерирует StringSession.
  3. САМ запишет/обновит TELEGRAM_SESSION_STRING в .env (создаст файл, если нет).
  4. Покажет, что строка сохранена — больше ничего делать не нужно.

ВНИМАНИЕ: строка = эквивалент пароля от Telegram-аккаунта.
Не коммить .env в git (он уже в .gitignore / .codeassistantignore).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon import connection as tl_connection

import config

ENV_PATH = Path(__file__).parent / ".env"
ENV_KEY = "TELEGRAM_SESSION_STRING"


def _build_proxy() -> tuple[object | None, dict | None]:
    """Read proxy settings from env. Returns (connection_class, proxy).

    Supported envs (any one of these — the first non-empty wins):
      • TELEGRAM_PROXY=socks5://user:pass@host:port
      • TELEGRAM_PROXY=socks5://host:port             (no auth)
      • TELEGRAM_PROXY=mtproxy://host:port:secret_hex
      • TELEGRAM_PROXY=tg://proxy?secret=...&server=host&port=port  (MTProxy link from Telegram)
      • plus generic HTTPS_PROXY / HTTP_PROXY (treated as http proxy).
    """
    raw = (
        os.environ.get("TELEGRAM_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )
    if not raw:
        return None, None

    from urllib.parse import urlparse, parse_qs

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()

    # Handle tg://proxy?secret=... format (MTProxy link from Telegram)
    if scheme == "tg" and parsed.netloc == "proxy":
        query = parse_qs(parsed.query)
        secret = query.get("secret", [None])[0]
        if not secret:
            print(f"❌ MTProxy-ссылка без secret: {raw!r}")
            return None, None
        # tg://proxy links usually don't include host/port in the URL itself,
        # they're meant to be used with Telegram's built-in proxy discovery.
        # For Telethon we need explicit host:port, so we'll require them.
        # Format: tg://proxy?secret=...&server=host&port=port
        host = query.get("server", [None])[0]
        port = query.get("port", [None])[0]
        if not host or not port:
            print(f"❌ MTProxy-ссылка без server/port: {raw!r}")
            print(f"   Формат: tg://proxy?secret=...&server=host&port=port")
            return None, None
        try:
            port = int(port)
        except ValueError:
            print(f"❌ Неверный порт в MTProxy-ссылке: {port!r}")
            return None, None
        print(f"🌐 Использую MTProxy: {host}:{port}")
        return tl_connection.ConnectionTcpMTProxyRandomizedIntermediate, (host, port, secret)

    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        print(f"⚠️  Не смог разобрать TELEGRAM_PROXY={raw!r}, игнорирую.")
        return None, None

    if scheme in ("socks5", "socks4", "http"):
        # Telethon expects a tuple (proxy_type, host, port[, rdns, user, pass]).
        try:
            import socks  # PySocks
        except ImportError:
            print("❌ Для SOCKS-прокси нужен пакет PySocks: pip3 install pysocks")
            sys.exit(1)
        proxy_types = {
            "socks5": socks.SOCKS5,
            "socks4": socks.SOCKS4,
            "http": socks.HTTP,
        }
        proxy_tuple = (
            proxy_types[scheme],
            host,
            port,
            True,  # rdns — резолвить DNS через прокси
            parsed.username or None,
            parsed.password or None,
        )
        print(f"🌐 Использую {scheme.upper()}-прокси: {host}:{port}")
        return None, proxy_tuple

    if scheme == "mtproxy":
        # mtproxy://host:port:secret_hex  — secret лежит в path первым сегментом
        secret = parsed.path.lstrip("/") or parsed.password
        if not secret:
            print(f"❌ MTProxy без secret: {raw!r}")
            sys.exit(1)
        print(f"🌐 Использую MTProxy: {host}:{port}")
        return tl_connection.ConnectionTcpMTProxyRandomizedIntermediate, (host, port, secret)

    print(f"⚠️  Неизвестная схема прокси: {scheme!r}, игнорирую.")
    return None, None


def upsert_env_var(path: Path, key: str, value: str) -> str:
    """Добавляет или обновляет переменную key=value в .env, сохраняя остальные строки.

    Возвращает 'updated' или 'added' — что именно сделали.
    """
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    found = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        # пропускаем пустые и комментарии (но обрабатываем закомментированный ключ
        # отдельно — оставляем как есть, добавим новую активную строку ниже)
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        existing_key = stripped.split("=", 1)[0].strip()
        if existing_key == key:
            lines[i] = f"{key}={value}"
            found = True
            break

    if not found:
        # гарантируем перевод строки перед добавлением
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"{key}={value}")

    # перезаписываем .env
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ставим разумные права (только владелец) — чтобы случайно не утекло
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    return "updated" if found else "added"


def main() -> None:
    print("=" * 60)
    print("  Автоматическая генерация TELEGRAM_SESSION_STRING")
    print("=" * 60)
    print(f"  API_ID  : {config.TELEGRAM_API_ID}")
    print(f"  .env    : {ENV_PATH}")
    print("=" * 60)
    print()

    connection_cls, proxy = _build_proxy()
    client_kwargs: dict = {
        "connection_retries": 5,
        "timeout": 60,
        "retry_delay": 2,
        "auto_reconnect": True,
    }
    
    # If using MTProxy, try without proxy first as it may be causing the issue
    if connection_cls is not None and proxy is not None:
        print("⚠️  Обнаружен MTProxy. Если возникнут проблемы с подключением,")
        print("   попробуйте отключить прокси в .env и запустить скрипт снова.")
        print()
        client_kwargs["connection"] = connection_cls
        client_kwargs["proxy"] = proxy

    try:
        with TelegramClient(
            StringSession(),
            config.TELEGRAM_API_ID,
            config.TELEGRAM_API_HASH,
            **client_kwargs,
        ) as client:
            me = client.get_me()
            session_string = client.session.save()
    except ValueError as e:
        if "readexactly size can not be less than zero" in str(e):
            print("❌ Ошибка MTProxy-подключения (известная проблема с некоторыми прокси).")
            print("   Попробуйте:")
            print("   1. Отключить прокси в .env (закомментировать TELEGRAM_PROXY)")
            print("   2. Использовать другой прокси")
            print("   3. Подключиться без прокси (если Telegram не заблокирован)")
            sys.exit(1)
        raise

    action = upsert_env_var(ENV_PATH, ENV_KEY, session_string)

    print()
    print("✅ Готово!")
    print(f"   Аккаунт      : {(me.first_name or '')} {(me.last_name or '')}".rstrip())
    if getattr(me, "username", None):
        print(f"   Username     : @{me.username}")
    print(f"   ID           : {me.id}")
    print(f"   {ENV_KEY:13s}: {action} в {ENV_PATH.name}")
    print()
    print("👉 Перезапусти приложение — строка из .env подхватится автоматически.")
    print("   Файл .env с этой строкой нельзя коммитить в git!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⏹  Отменено пользователем.")
        sys.exit(1)
