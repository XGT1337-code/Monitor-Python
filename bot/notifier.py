"""Рассылка алертов всем пользователям из whitelist."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter

from bot.formatting import chunks
from config import settings
from storage.db import Database

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot: Bot, db: Database | None = None) -> None:
        self.bot = bot
        self.db = db
        self._lock = asyncio.Lock()   # последовательная отправка — не упираемся в flood-limit

    async def send(self, text: str) -> None:
        # Админы + доп. группы/каналы + подписчики (в режиме OPEN_ACCESS)
        subscribers: list[int] = []
        if settings.multi_user and self.db is not None:
            try:
                subscribers = await self.db.list_subscribers()
            except Exception:  # noqa: BLE001
                log.exception("Не удалось получить подписчиков")
        targets = list(dict.fromkeys([*settings.allowed_user_ids, *settings.alert_chat_ids, *subscribers]))
        if not targets:
            log.info("Алерт не отправлен: нет получателей (ALLOWED_USER_IDS/ALERT_CHAT_IDS пусты)")
            return
        async with self._lock:
            for chat_id in targets:
                for part in chunks(text):
                    await self._send_one(chat_id, part)

    async def _send_one(self, chat_id: int, text: str) -> None:
        for attempt in range(3):
            try:
                await self.bot.send_message(chat_id, text, disable_web_page_preview=True)
                log.info("Алерт отправлен в чат %s: %s", chat_id, text.split("\n", 1)[0][:80])
                return
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(exc.retry_after, 60))
            except TelegramForbiddenError:
                # пользователь заблокировал бота — убираем из подписчиков
                log.info("Чат %s заблокировал бота — удаляю из подписчиков", chat_id)
                if self.db is not None:
                    await self.db.remove_subscriber(chat_id)
                return
            except TelegramAPIError as exc:
                log.warning("Не удалось отправить алерт пользователю %s: %s", chat_id, exc)
                return
            except Exception:  # noqa: BLE001 — сеть до Telegram может моргнуть
                log.exception("Ошибка отправки алерта (попытка %s)", attempt + 1)
                await asyncio.sleep(2 * (attempt + 1))
