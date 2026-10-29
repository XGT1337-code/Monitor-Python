"""Firewall бота: whitelist пользователей и rate limit команд."""
from __future__ import annotations

import hmac
import logging
import re
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.formatting import esc
from config import settings
from storage.db import Database

log = logging.getLogger(__name__)
URL_RE = re.compile(r"^(https?|rtmps?|srt)://\S+$", re.IGNORECASE)


def _command(text: str) -> str:
    """'/Add@Bot url name' -> '/add'."""
    return text.split()[0].split("@")[0].lower() if text and text.startswith("/") else ""


def _is_start(text: str) -> bool:
    return _command(text) == "/start"


def is_admin(user_id: int) -> bool:
    """Админ — пользователь из ALLOWED_USER_IDS (если список пуст — админов нет ограничений)."""
    return not settings.allowed_user_ids or user_id in settings.allowed_user_ids


class AuthStore:
    """Кто получил доступ по паролю (таблица authorized_users + кэш в памяти)."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._ids: set[int] | None = None

    async def _load(self) -> set[int]:
        if self._ids is None:
            self._ids = {uid for uid, _, _ in await self.db.list_authorized()}
        return self._ids

    async def is_authorized(self, user_id: int) -> bool:
        return user_id in await self._load()

    async def grant(self, user_id: int, name: str) -> None:
        await self.db.add_authorized(user_id, name)
        (await self._load()).add(user_id)

    async def revoke(self, user_id: int) -> bool:
        (await self._load()).discard(user_id)
        return await self.db.remove_authorized(user_id)


class AccessMiddleware(BaseMiddleware):
    """Доступ к боту: админы (ALLOWED_USER_IDS), вход по паролю (ACCESS_PASSWORD) или открытый режим.

    Чужие и неудачные попытки — в журнал (WARNING). Управление — только админам.
    """

    def __init__(self, db: Database, auth: AuthStore) -> None:
        self.db = db
        self.auth = auth
        self._denied_log: dict[int, float] = {}
        self._bad_attempts: dict[int, deque[float]] = defaultdict(deque)

    async def _role(self, user_id: int) -> str | None:
        """admin / user / None (нет доступа)."""
        if user_id in settings.allowed_user_ids:
            return "admin"
        if settings.access_password:
            return "user" if await self.auth.is_authorized(user_id) else None
        if settings.open_access:
            return "admin" if not settings.allowed_user_ids else "user"
        return None

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        # Нажатия inline-кнопок
        if isinstance(event, CallbackQuery):
            uid = event.from_user.id if event.from_user else 0
            role = await self._role(uid)
            admin_button = (event.data or "").startswith("m:")   # mute — управление
            if role == "admin" or (role == "user" and not admin_button):
                return await handler(event, data)
            await self.db.add_event(None, "ACCESS_DENIED", "WARNING", f"callback user_id={uid} data={event.data!r}")
            await event.answer("⛔ Bu düymə yalnız adminlər üçündür." if role == "user"
                               else "⛔ Giriş qadağandır.", show_alert=True)
            return None
        if not isinstance(event, Message) or event.from_user is None:
            return None
        user = event.from_user
        text = event.text or ""

        role = await self._role(user.id)
        if role == "admin":
            return await handler(event, data)
        if role == "user":
            if _command(text) in settings.admin_commands:
                await self.db.add_event(None, "ADMIN_DENIED", "WARNING",
                                        f"user_id={user.id} @{user.username or '-'} {text[:60]!r}")
                await event.answer("⛔ Bu əmr yalnız adminlər üçündür.\n"
                                   f"Sizin ID: <code>{user.id}</code> — admin olmaq üçün onu adminə göndərin.")
                return None
            return await handler(event, data)

        # Вход по паролю (только в личке — в группе пароль увидят все)
        if settings.access_password:
            if event.chat.type == "private":
                await self._password_flow(event, user, text, data)
            return None

        # Первичная настройка: whitelist пуст — работает только /start, который сообщает user_id
        if settings.whitelist_empty:
            if _is_start(text):
                await event.answer(
                    "👋 Bot hələ qurulmayıb.\n\n"
                    f"Sizin user_id: <code>{user.id}</code>\n\n"
                    "Onu <code>.env</code> faylına əlavə edin:\n"
                    f"<code>ALLOWED_USER_IDS={user.id}</code>\n"
                    "(bir neçəsini vergüllə) və botu yenidən başladın."
                )
                log.info("Запрос user_id при пустом whitelist: %s", user.id)
            else:
                await event.answer("⛔ İcazəli istifadəçi siyahısı boşdur. user_id-nizi öyrənmək üçün /start göndərin.")
            return None

        if user.id not in settings.allowed_user_ids:
            if event.chat.type != "private":
                return None  # в группах чужие сообщения просто игнорируем
            now = time.time()
            if now - self._denied_log.get(user.id, 0) > 60:   # не даём заспамить журнал
                first_time = user.id not in self._denied_log
                self._denied_log[user.id] = now
                await self.db.add_event(
                    None, "ACCESS_DENIED", "WARNING",
                    f"user_id={user.id} username=@{user.username or '-'} "
                    f"name={user.full_name!r} text={text[:80]!r}",
                )
                log.warning("Отказ в доступе: user_id=%s", user.id)
                if first_time:
                    await event.answer("⛔ Giriş qadağandır.")
            return None
        return await handler(event, data)

    async def _password_flow(self, event: Message, user, text: str, data: dict[str, Any]) -> None:
        """Любой текст (не команда) от пользователя без доступа — попытка ввода пароля."""
        now = time.time()
        attempts = self._bad_attempts[user.id]
        while attempts and now - attempts[0] > settings.password_lock_sec:
            attempts.popleft()
        if len(attempts) >= settings.password_max_attempts:
            wait = int(settings.password_lock_sec - (now - attempts[0])) // 60 + 1
            await event.answer(f"⛔ Çox sayda yanlış cəhd. {wait} dəqiqə sonra yenidən cəhd edin.")
            return

        if not text or text.startswith("/"):
            await event.answer("🔐 Bu bot şifrə ilə qorunur.\nŞifrəni bir mesajla göndərin:")
            return

        # сообщение с паролем убираем из чата, чтобы он не остался в переписке
        try:
            await event.delete()
        except Exception:  # noqa: BLE001
            pass
        if hmac.compare_digest(text.strip().encode(), settings.access_password.encode()):
            await self.auth.grant(user.id, user.full_name)
            await self.db.add_subscriber(event.chat.id, user.full_name)
            attempts.clear()
            await self.db.add_event(None, "ACCESS_GRANTED", "INFO",
                                    f"user_id={user.id} @{user.username or '-'} {user.full_name!r}")
            log.info("Доступ по паролю выдан: user_id=%s", user.id)
            await event.answer("✅ Şifrə düzgündür — giriş verildi!\n"
                               "Siz xəbərdarlıqlara abunə oldunuz: yayım dayananda, saatlıq status və gündəlik "
                               "hesabat bura gələcək.\n\nMenyu: /status · Kömək: /help · Abunəlikdən çıxmaq: /stop")
        else:
            attempts.append(now)
            left = settings.password_max_attempts - len(attempts)
            await self.db.add_event(None, "ACCESS_BAD_PASSWORD", "WARNING",
                                    f"user_id={user.id} @{user.username or '-'} {user.full_name!r} "
                                    f"(cəhd {len(attempts)}/{settings.password_max_attempts})")
            log.warning("Неверный пароль: user_id=%s (%d/%d)", user.id, len(attempts), settings.password_max_attempts)
            await event.answer(f"❌ Şifrə yanlışdır. Qalan cəhd: {left}" if left > 0
                               else f"⛔ Çox sayda yanlış cəhd. {settings.password_lock_sec // 60} dəqiqə gözləyin.")


class RateLimitMiddleware(BaseMiddleware):
    """Скользящее окно на все команды + отдельный кулдаун на тяжёлую /check."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._hits: dict[int, deque[float]] = defaultdict(deque)
        self._last_check: dict[int, float] = {}
        self._warned: dict[int, float] = {}

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        if not isinstance(event, Message) or event.from_user is None:
            return await handler(event, data)
        uid = event.from_user.id
        now = time.time()
        text = (event.text or "").strip()

        hits = self._hits[uid]
        while hits and now - hits[0] > settings.rate_limit_window:
            hits.popleft()
        if len(hits) >= settings.rate_limit_count:
            await self._reject(event, uid, now, f"{settings.rate_limit_window} san ərzində ən çox "
                                               f"{settings.rate_limit_count} əmr")
            return None
        hits.append(now)

        heavy = text.lower().startswith("/check") or bool(URL_RE.match(text))
        if heavy:
            wait = settings.check_cooldown_sec - (now - self._last_check.get(uid, 0))
            if wait > 0:
                await self._reject(event, uid, now, f"/check {settings.check_cooldown_sec} san-də bir dəfədən tez olmaz "
                                                   f"({wait:.0f} san gözləyin)")
                return None
            self._last_check[uid] = now
        return await handler(event, data)

    async def _reject(self, event: Message, uid: int, now: float, why: str) -> None:
        if now - self._warned.get(uid, 0) > 10:
            self._warned[uid] = now
            await event.answer(f"⏳ Çox sayda sorğu: {esc(why)}")
            await self.db.add_event(None, "RATE_LIMIT", "WARNING", f"user_id={uid}: {why}")
