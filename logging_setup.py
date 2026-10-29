"""Логирование: файл с ротацией + консоль, с обязательной маскировкой токена.

aiogram/aiohttp/httpx могут писать в лог URL вида https://api.telegram.org/bot<TOKEN>/...
Фильтр и форматтер заменяют токен (и любой похожий на токен Telegram фрагмент) на ***.
"""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler

from config import settings

# Шаблон токена Telegram: <bot_id>:<35 символов>
_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")


def redact(text: str) -> str:
    """Убирает токен из произвольной строки."""
    if not text:
        return text
    if settings.bot_token and settings.bot_token in text:
        text = text.replace(settings.bot_token, "***")
    return _TOKEN_RE.sub("***", text)


class TokenRedactingFilter(logging.Filter):
    """Фильтр: подменяет сообщение записи на версию без токена."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — кривые args не должны ронять логирование
            message = str(record.msg)
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        return True


class RedactingFormatter(logging.Formatter):
    """Форматтер: финальная зачистка (в т.ч. traceback исключений)."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging() -> None:
    fmt = RedactingFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    token_filter = TokenRedactingFilter()

    file_handler = RotatingFileHandler(
        settings.log_dir / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    # Windows-консоль по умолчанию не в UTF-8 — кириллица превращается в мусор
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    handlers: list[logging.Handler] = [file_handler]
    if sys.stderr is not None:  # при запуске через pythonw (автозапуск) консоли нет
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(fmt)
        handler.addFilter(token_filter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(settings.log_level)
    for handler in handlers:
        root.addHandler(handler)

    # Шумные библиотеки — потише (httpx на INFO пишет каждый запрос с URL)
    for noisy in ("httpx", "httpcore", "aiosqlite", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
