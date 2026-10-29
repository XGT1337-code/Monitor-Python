"""Точка входа: Telegram-бот мониторинга прямых эфиров (мини-SIEM + мини-firewall)."""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand

from config import settings, validate_settings
from logging_setup import setup_logging

log = logging.getLogger("main")

COMMANDS = [
    BotCommand(command="status", description="Yayımlar menyusu (düymələrlə)"),
    BotCommand(command="check", description="Linkin birdəfəlik yoxlanışı"),
    BotCommand(command="add", description="Yayımı monitorinqə əlavə et"),
    BotCommand(command="remove", description="Yayımı monitorinqdən çıxar"),
    BotCommand(command="events", description="Hadisələr jurnalı"),
    BotCommand(command="report", description="Son 24 saatın hesabatı"),
    BotCommand(command="mute", description="Xəbərdarlıqları müvəqqəti söndür"),
    BotCommand(command="interval", description="Yoxlama intervalı"),
    BotCommand(command="testalert", description="Test xəbərdarlığı"),
    BotCommand(command="help", description="Kömək və əmrlərin izahı"),
    BotCommand(command="start", description="Xəbərdarlıqlara abunə ol"),
    BotCommand(command="stop", description="Abunəlikdən çıx"),
]


def _guarded(coro, name: str) -> asyncio.Task:
    """Фоновая задача, которая не умирает молча: исключение логируется."""
    async def runner():
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Фоновая задача %s упала", name)
    return asyncio.create_task(runner(), name=name)


def _lower_priority() -> None:
    """Пониженный приоритет процесса: на сервере с vMix процессор всегда достаётся vMix.

    Дочерние ffmpeg/ffprobe/yt-dlp наследуют этот приоритет.
    """
    try:
        import psutil
        proc = psutil.Process()
        if sys.platform == "win32":
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            proc.nice(10)
    except Exception:  # noqa: BLE001 — не критично
        log.warning("Не удалось понизить приоритет процесса")


async def main() -> None:
    validate_settings()
    setup_logging()
    _lower_priority()

    # Импорты после настройки логирования
    from bot.handlers import router
    from bot.middlewares import AccessMiddleware, AuthStore, RateLimitMiddleware
    from bot.notifier import Notifier
    from checks.network import make_http_client
    from checks.validators import HostGuard
    from monitor.alerts import AlertManager
    from monitor.engine import Monitor
    from monitor.reports import daily_report_loop, periodic_status_loop
    from monitor.system import SystemMonitor, VmixMonitor
    from storage.db import Database

    for tool, path in (("ffprobe", settings.ffprobe_path), ("ffmpeg", settings.ffmpeg_path)):
        if not shutil.which(path):
            log.warning("%s не найден в PATH — анализ потока будет недоступен (см. README)", tool)
    if settings.whitelist_empty:
        log.warning("ALLOWED_USER_IDS пуст: бот отвечает только на /start (сообщает user_id)")

    db = Database(settings.db_path)
    await db.connect()
    client = make_http_client()
    guard = HostGuard()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = Notifier(bot, db)
    alerts = AlertManager(db, notifier)
    monitor = Monitor(db, alerts, client, guard)
    system = SystemMonitor(alerts)
    vmix = VmixMonitor(alerts, client)

    dp = Dispatcher()
    auth = AuthStore(db)
    access = AccessMiddleware(db, auth)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.message.middleware(RateLimitMiddleware(db))
    dp.include_router(router)
    # Зависимости для хендлеров (внедряются по имени аргумента)
    dp.workflow_data.update(db=db, monitor=monitor, guard=guard, client=client, system=system, vmix=vmix,
                            notifier=notifier, auth=auth)

    await db.add_event(None, "BOT_STARTED", "INFO", "Bot işə salındı")
    await monitor.start()
    tasks = [
        _guarded(alerts.reminder_loop(), "reminders"),
        _guarded(system.loop(), "system"),
        _guarded(vmix.loop(), "vmix"),
        _guarded(daily_report_loop(db, monitor, system, notifier), "daily_report"),
        _guarded(periodic_status_loop(monitor, system, notifier, vmix), "periodic_status"),
    ]
    try:
        # При старте Windows сети может ещё не быть — ждём её, а не падаем
        me = None
        for attempt in range(1, 61):
            try:
                me = await bot.get_me()
                break
            except TelegramUnauthorizedError:
                log.error("Telegram отклонил токен (Unauthorized). Получите актуальный токен у @BotFather "
                          "(/mybots → API Token) и обновите TELEGRAM_BOT_TOKEN в .env")
                return
            except Exception as exc:  # noqa: BLE001 — сеть/DNS ещё не готовы
                log.warning("Нет связи с Telegram (%s), попытка %d/60 — жду 10 с", type(exc).__name__, attempt)
                await asyncio.sleep(10)
        if me is None:
            raise RuntimeError("Нет связи с Telegram 10 минут")
        log.info("Бот @%s запущен", me.username)
        try:
            await bot.set_my_commands(COMMANDS)
        except Exception:  # noqa: BLE001
            log.warning("Не удалось установить меню команд")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("Остановка…")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await monitor.stop()
        await db.add_event(None, "BOT_STOPPED", "INFO", "Bot dayandırıldı")
        await client.aclose()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    if sys.version_info < (3, 11):
        sys.exit("Требуется Python 3.11+")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        # без консоли (pythonw) traceback иначе потеряется — пишем в лог и выходим с ошибкой,
        # чтобы Планировщик перезапустил бота
        log.exception("Бот аварийно завершился")
        sys.exit(1)
