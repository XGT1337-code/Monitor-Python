"""Команды Telegram-бота. Команды — на английском, тексты для пользователя — на азербайджанском."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time

import httpx
from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.formatting import (
    SEVERITY_ICON, STATE_ICON, STATE_NAME, chunks, esc, fmt_duration, fmt_time, render_check, short_url,
)
from bot.middlewares import URL_RE, AuthStore
from bot.notifier import Notifier
from checks.diagnostics import StreamChecker
from checks.validators import HostGuard, UrlValidationError
from config import settings
from monitor.engine import Monitor
from monitor.reports import build_list, build_report, build_status
from monitor.system import SystemMonitor, VmixMonitor
from storage.db import Database

log = logging.getLogger(__name__)
router = Router(name="main")

_NAME_RE = re.compile(r"^[\w .\-()#/+:]{1,64}$", re.UNICODE)


def _help_text() -> str:
    every = settings.status_every_min
    return (
        "🛰 <b>Canlı yayımların monitorinqi</b>\n"
        "Bot yayımları avtomatik yoxlayır və problem olanda xəbər verir.\n\n"

        "<b>🔔 Xəbərdarlıqlar necə işləyir</b>\n"
        "🔴 <b>Yayım dayananda və ya səs itəndə</b> bot dərhal yazır (təxminən 45–60 san ərzində): "
        "səbəb, son işləmə vaxtı və mənbə linki.\n"
        "⏰ Problem həll olunana qədər hər 5 dəqiqədən bir xatırladır.\n"
        "🟢 Yayım bərpa olunanda neçə müddət dayandığını yazır.\n"
        + (f"🕐 Hər {'saat' if every == 60 else f'{every} dəqiqə'} bütün yayımların statusunu özü göndərir.\n"
           if every > 0 else "")
        + "🟡 Keyfiyyət xəbərdarlıqları (aşağı bitreyt, zəif səs, qara ekran, IP dəyişməsi) mesaj kimi gəlmir — "
        "onlar /events jurnalında və statusda görünür.\n"
        f"📊 Hər gün saat {settings.daily_report_time:%H:%M}-da sutkalıq hesabat gəlir.\n"
        + ("🎛 <b>vMix</b> də yoxlanılır (hər 5 san): proqram cavab verirmi, Stream açıqdırmı, Fade To Black, "
           "Master səsi söndürülübmü və ya səssizdirmi. Problem olanda 🔴, düzələndə 🟢 gəlir.\n"
           if settings.vmix_host else "")
        + "\n"

        "<b>📋 Əmrlər</b>\n\n"

        "🔎 <b>Yoxlama</b>\n"
        "• Sadəcə <b>linki göndərin</b> — bot onu dərhal tam yoxlayacaq.\n"
        "• /check &lt;link&gt; — eyni şey: şəbəkə, sertifikat, pleylist, video, audio, qara ekran, "
        "donmuş görüntü və səssizlik yoxlanılır. 10–30 saniyə çəkir.\n\n"

        "➕ <b>Yayımların idarəsi</b>\n"
        "• /add &lt;link&gt; &lt;ad&gt; — yayımı daimi monitorinqə əlavə edir. Bot onu hər "
        f"{settings.default_interval} saniyədən bir yoxlayacaq.\n"
        "   <i>Nümunə:</i> <code>/add https://biznestv.az/en/canli Biznes TV</code>\n"
        "• /remove &lt;ad&gt; — yayımı monitorinqdən çıxarır.\n"
        "   <i>Nümunə:</i> <code>/remove Biznes TV</code>\n\n"

        "📡 <b>Status</b>\n"
        "• /status — düymələrlə menyu: hər yayım bir düymədir 🟢/🟡/🔴. Basanda ətraflı hesabat açılır, "
        "altında «Yenilə», «1 saat səssiz» və «Siyahıya qayıt» düymələri var. "
        "«📡 Ümumi status» — bütün yayımların ətraflı statusu bir mesajda.\n"
        "• /status &lt;ad&gt; — bir yayımın hesabatını dərhal açır.\n"
        "   <i>Nümunə:</i> <code>/status Biznes TV</code>\n\n"

        "📜 <b>Jurnal və hesabatlar</b>\n"
        "• /events — son 20 hadisə (bütün xəbərdarlıqlar, o cümlədən 🟡).\n"
        "• /events &lt;N&gt; — son N hadisə (maksimum 100). <i>Nümunə:</i> <code>/events 50</code>\n"
        "• /report — son 24 saatın hesabatı: işləmə faizi, dayanma sayı, cəmi dayanma və orta bərpa vaxtı.\n\n"

        "🔇 <b>Planlı işlər</b>\n"
        "• /mute &lt;ad&gt; &lt;dəqiqə&gt; — yayım üzrə xəbərdarlıqları müvəqqəti söndürür "
        "(hadisələr jurnala yazılmağa davam edir).\n"
        "   <i>Nümunə:</i> <code>/mute Biznes TV 60</code>\n"
        "• /mute &lt;ad&gt; 0 — xəbərdarlıqları yenidən açır.\n\n"

        "⚙️ <b>Ayarlar</b>\n"
        f"• /interval &lt;saniyə&gt; — yoxlama tezliyi ({settings.min_interval}–{settings.max_interval} san). "
        "<i>Nümunə:</i> <code>/interval 15</code>\n"
        "• /testalert — test xəbərdarlığı göndərir (mesajların çatdığını yoxlamaq üçün).\n"
        "• /chatid — bu çatın ID-si (xəbərdarlıqları qrupa da göndərmək üçün).\n"
        "• /help — bu kömək.\n\n"

        "<b>🔗 Hansı linklər dəstəklənir</b>\n"
        "YouTube və digər platformaların səhifələri, Kick, pleyerli saytlar (məs. biznestv.az, canlitv.com — "
        "pleyer iframe-in içində olsa da), Yoda.az, HLS (.m3u8), RTMP, SRT, HTTP-FLV/MP4.\n"
        "YouTube və Kick üçün platformanın öz məlumatı da göstərilir: efirdədirmi, neçə nəfər baxır, "
        "efirin adı, nə vaxt başlayıb, kanalın izləyiciləri.\n"
        "Cloudflare bot-qoruması olan saytlar (məs. com.com.tr) avtomatik yoxlanıla bilmir — bot bunu ayrıca yazır."
    )


async def _reply_long(message: Message, text: str) -> None:
    for part in chunks(text):
        await message.answer(part, disable_web_page_preview=True)


def _access_text() -> str:
    if not settings.multi_user:
        return ""
    how = ("Giriş şifrə ilə verilir: /start yazıb şifrəni göndərin." if settings.access_password
           else "Bot hamı üçün açıqdır.")
    return ("\n\n<b>👥 Giriş</b>\n"
            f"{how} /start — xəbərdarlıqlara abunə olmaq, /stop — abunəlikdən çıxmaq.\n"
            "🔒 /add, /remove, /mute, /interval və «1 saat səssiz» düyməsi yalnız adminlər üçündür.\n"
            "🔒 /users — girişi olanların siyahısı · /kick &lt;id&gt; — girişi geri almaq.")


@router.message(Command("users"))
async def cmd_users(message: Message, db: Database) -> None:
    users = await db.list_authorized()
    subs = set(await db.list_subscribers())
    lines = [f"👥 <b>Şifrə ilə giriş alanlar ({len(users)})</b>", ""]
    if not users:
        lines.append("Hələ heç kim yoxdur.")
    for uid, name, ts in users:
        lines.append(f"• <code>{uid}</code> {esc(name)} · {fmt_time(ts, True)}"
                     + (" · 🔔" if uid in subs else " · 🔕"))
    lines.append(f"\nAdminlər: {', '.join(f'<code>{a}</code>' for a in sorted(settings.allowed_user_ids)) or '—'}")
    lines.append("Girişi geri almaq: /kick &lt;id&gt;")
    await _reply_long(message, "\n".join(lines))


@router.message(Command("kick"))
async def cmd_kick(message: Message, command: CommandObject, db: Database, auth: AuthStore) -> None:
    raw = (command.args or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("İstifadə: /kick &lt;id&gt; — ID-ləri /users göstərir")
        return
    uid = int(raw)
    if uid in settings.allowed_user_ids:
        await message.answer("Admini /kick ilə çıxarmaq olmaz — onu .env-dəki ALLOWED_USER_IDS-dən silin.")
        return
    removed = await auth.revoke(uid)
    await db.remove_subscriber(uid)
    if removed:
        await db.add_event(None, "ACCESS_REVOKED", "WARNING", f"user_id={uid} (admin {message.from_user.id})")
        await message.answer(f"🚫 <code>{uid}</code> üçün giriş geri alındı. Yenidən girmək üçün şifrə lazım olacaq.")
    else:
        await message.answer(f"<code>{uid}</code> siyahıda yoxdur. Siyahı: /users")


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database) -> None:
    if settings.multi_user:
        u = message.from_user
        if await db.add_subscriber(message.chat.id, u.full_name if u else str(message.chat.id)):
            await db.add_event(None, "SUBSCRIBED", "INFO",
                               f"chat_id={message.chat.id} @{(u.username if u else '') or '-'}")
        await message.answer("✅ Siz xəbərdarlıqlara abunə oldunuz: yayım dayananda, saatlıq status və "
                             "gündəlik hesabat bura gələcək. Çıxmaq üçün: /stop")
    await _reply_long(message, _help_text() + _access_text())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await _reply_long(message, _help_text() + _access_text())


@router.message(Command("stop"))
async def cmd_stop(message: Message, db: Database) -> None:
    if await db.remove_subscriber(message.chat.id):
        await db.add_event(None, "UNSUBSCRIBED", "INFO", f"chat_id={message.chat.id}")
        await message.answer("🔕 Abunəlikdən çıxdınız, xəbərdarlıqlar artıq gəlməyəcək. Yenidən: /start")
    elif message.from_user and message.from_user.id in settings.allowed_user_ids:
        await message.answer("Siz adminsiniz — adminlər xəbərdarlıqları həmişə alır "
                             "(.env-dəki ALLOWED_USER_IDS).")
    else:
        await message.answer("Siz abunə deyilsiniz. Abunə olmaq: /start")


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject, monitor: Monitor, guard: HostGuard,
                  db: Database) -> None:
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("İstifadə: /add &lt;link&gt; &lt;ad&gt;\n"
                             "Nümunə: <code>/add https://site.az/live.m3u8 Biznes TV</code>")
        return
    url, name = parts[0], parts[1].strip()
    if not _NAME_RE.match(name):
        await message.answer("Ad: 1–64 simvol (hərf, rəqəm, boşluq, . - _ ( ) # / + :)")
        return
    if monitor.find(name):
        await message.answer(f"«{esc(name)}» adlı yayım artıq var. /remove ilə silin və ya başqa ad seçin.")
        return
    if len(monitor.workers) >= settings.max_streams:
        await message.answer(f"Yayım limiti dolub: {settings.max_streams}")
        return
    try:
        await guard.validate(url)
    except UrlValidationError as exc:
        await db.add_event(None, "URL_REJECTED", "WARNING", f"/add {short_url(url)}: {exc}")
        await message.answer(f"⛔ {esc(exc)}")
        return
    await monitor.add(name, url)
    await db.add_event(name, "STREAM_ADDED", "INFO", f"{short_url(url, 200)} (user {message.from_user.id})")
    await message.answer(f"✅ <b>{esc(name)}</b> monitorinqə əlavə edildi.\n"
                         f"Yoxlama hər {monitor.interval} san. Status: <code>/status {esc(name)}</code>")


@router.message(Command("remove"))
async def cmd_remove(message: Message, command: CommandObject, monitor: Monitor, db: Database) -> None:
    name = (command.args or "").strip()
    if not name:
        await message.answer("İstifadə: /remove &lt;ad&gt;")
        return
    worker = monitor.find(name)
    real = worker.name if worker else name
    if await monitor.remove(real):
        await db.add_event(real, "STREAM_REMOVED", "INFO", f"user {message.from_user.id}")
        await message.answer(f"🗑 <b>{esc(real)}</b> monitorinqdən çıxarıldı.")
    else:
        await message.answer(f"«{esc(name)}» tapılmadı. Siyahı: /status")


async def _do_check(message: Message, url: str, client: httpx.AsyncClient, guard: HostGuard,
                    db: Database) -> None:
    try:
        await guard.validate(url)
    except UrlValidationError as exc:
        await db.add_event(None, "URL_REJECTED", "WARNING",
                           f"/check {short_url(url)}: {exc} (user {message.from_user.id})")
        await message.answer(f"⛔ {esc(exc)}")
        return
    status_msg = await message.answer("⏳ Tam diaqnostika aparılır (~30 san-yə qədər)…")
    checker = StreamChecker("check", url, client, guard, one_shot=True)
    try:
        result = await asyncio.wait_for(checker.run(full=True), settings.timeouts.check_total)
        text = render_check(result, "Diaqnostika")
    except asyncio.TimeoutError:
        text = f"🔴 Diaqnostika {settings.timeouts.check_total:.0f} san-yə sığmadı"
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка /check")
        text = f"🔴 Diaqnostikanın daxili xətası: {esc(type(exc).__name__)}"
    parts = chunks(text)
    try:
        await status_msg.edit_text(parts[0], disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        await message.answer(parts[0], disable_web_page_preview=True)
    for part in parts[1:]:
        await message.answer(part, disable_web_page_preview=True)


@router.message(Command("check"))
async def cmd_check(message: Message, command: CommandObject, client: httpx.AsyncClient,
                    guard: HostGuard, db: Database) -> None:
    url = (command.args or "").strip().split(maxsplit=1)[0] if command.args else ""
    if not url:
        await message.answer("İstifadə: /check &lt;link&gt; — və ya sadəcə linki göndərin")
        return
    await _do_check(message, url, client, guard, db)


def _sid(name: str) -> str:
    """Короткий id эфира для callback_data (лимит Telegram — 64 байта)."""
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


def _by_sid(monitor: Monitor, sid: str):
    return next((w for w in monitor.workers.values() if _sid(w.name) == sid), None)


def _list_keyboard(monitor: Monitor) -> InlineKeyboardMarkup:
    """По кнопке на эфир: нажатие открывает подробный отчёт."""
    rows = [[InlineKeyboardButton(text=f"{STATE_ICON.get(v.state.value, '⚪')} {v.name}"
                                  + (" 🔇" if v.muted_until > time.time() else ""),
                                  callback_data=f"s:{_sid(v.name)}")]
            for v in monitor.views()]
    rows.append([InlineKeyboardButton(text="📡 Ümumi status", callback_data="all"),
                 InlineKeyboardButton(text="🔄 Yenilə", callback_data="list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _stream_keyboard(name: str, muted: bool) -> InlineKeyboardMarkup:
    sid = _sid(name)
    mute_btn = (InlineKeyboardButton(text="🔔 Xəbərdarlıqları aç", callback_data=f"m:{sid}:0") if muted
                else InlineKeyboardButton(text="🔇 1 saat səssiz", callback_data=f"m:{sid}:60"))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Yenilə", callback_data=f"s:{sid}"), mute_btn],
        [InlineKeyboardButton(text="⬅️ Siyahıya qayıt", callback_data="list")],
    ])


async def _stream_report(w, db: Database) -> str:
    """Подробный отчёт по одному эфиру (для /status <ad> и кнопки в меню /status)."""
    v = w.view()
    now = time.time()
    lines = [f"{STATE_ICON.get(v.state.value, '⚪')} <b>{esc(v.name)}</b> — "
             f"{STATE_NAME.get(v.state.value, v.state.value)}"
             + (f" ({fmt_duration(now - v.since)})" if v.since else ""),
             f"🔗 {esc(short_url(v.url, 150))}",
             f"Son dəfə efirdə: {fmt_time(v.last_up_ts, True)}",
             f"Ardıcıl uğursuz: {v.fail_count}/{settings.fail_threshold} · yoxlama sayı: {w.checker.check_no}"]
    if v.muted_until > now:
        lines.append(f"🔇 Xəbərdarlıqlar {fmt_time(v.muted_until)}-dək söndürülüb")
    lines.append("")
    lines.append(render_check(v.last, "Son yoxlama") if v.last else "Hələ yoxlama olmayıb.")
    events = await db.recent_events(5, stream=v.name)
    if events:
        lines.append("\n<b>📜 Son hadisələr</b>")
        for e in events:
            lines.append(f"{fmt_time(e.ts, True)} {SEVERITY_ICON.get(e.severity, '⚪')} {esc(e.type)} — "
                         f"{esc(e.details[:150])}")
    return "\n".join(lines)


@router.message(Command("status"))
async def cmd_status(message: Message, command: CommandObject, monitor: Monitor, db: Database,
                     system: SystemMonitor) -> None:
    name = (command.args or "").strip()
    if not name:
        # без аргумента — меню с кнопками: эфиры, общий статус, mute
        await message.answer(build_list(monitor), reply_markup=_list_keyboard(monitor),
                             disable_web_page_preview=True)
        return
    w = monitor.find(name)
    if not w:
        await message.answer(f"«{esc(name)}» tapılmadı. Siyahı: /status")
        return
    parts = chunks(await _stream_report(w, db))
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        await message.answer(part, disable_web_page_preview=True,
                             reply_markup=_stream_keyboard(w.name, w.muted_until > time.time()) if last else None)


# ---------------------------------------------------------------- inline-кнопки
async def _edit_or_send(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Обновляем сообщение с кнопками; если текст длинный — шлём частями новым сообщением."""
    parts = chunks(text)
    if len(parts) == 1:
        try:
            await call.message.edit_text(parts[0], reply_markup=markup, disable_web_page_preview=True)
            return
        except TelegramBadRequest as exc:
            if "not modified" in str(exc):
                return
    for i, part in enumerate(parts):
        await call.message.answer(part, disable_web_page_preview=True,
                                  reply_markup=markup if i == len(parts) - 1 else None)


@router.callback_query(F.data == "list")
async def cb_list(call: CallbackQuery, monitor: Monitor) -> None:
    await _edit_or_send(call, build_list(monitor), _list_keyboard(monitor))
    await call.answer()


@router.callback_query(F.data == "all")
async def cb_all(call: CallbackQuery, monitor: Monitor, system: SystemMonitor, vmix: VmixMonitor) -> None:
    back = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Siyahıya qayıt",
                                                                       callback_data="list")]])
    await _edit_or_send(call, build_status(monitor, system, vmix=vmix), back)
    await call.answer()


@router.callback_query(F.data.startswith("s:"))
async def cb_stream(call: CallbackQuery, monitor: Monitor, db: Database) -> None:
    w = _by_sid(monitor, call.data[2:])
    if w is None:
        await call.answer("Yayım tapılmadı (silinib?)", show_alert=True)
        return
    await _edit_or_send(call, await _stream_report(w, db), _stream_keyboard(w.name, w.muted_until > time.time()))
    await call.answer()


@router.callback_query(F.data.startswith("m:"))
async def cb_mute(call: CallbackQuery, monitor: Monitor, db: Database) -> None:
    _, sid, minutes = call.data.split(":")
    w = _by_sid(monitor, sid)
    if w is None:
        await call.answer("Yayım tapılmadı (silinib?)", show_alert=True)
        return
    until = await monitor.mute(w.name, int(minutes))
    if int(minutes):
        await db.add_event(w.name, "MUTED", "INFO", f"{minutes} dəq (user {call.from_user.id}, düymə)")
        await call.answer(f"🔇 {fmt_time(until)}-dək xəbərdarlıq gəlməyəcək")
    else:
        await db.add_event(w.name, "UNMUTED", "INFO", f"user {call.from_user.id}, düymə")
        await call.answer("🔔 Xəbərdarlıqlar yenidən açıldı")
    await _edit_or_send(call, await _stream_report(w, db), _stream_keyboard(w.name, int(minutes) > 0))


@router.message(Command("interval"))
async def cmd_interval(message: Message, command: CommandObject, monitor: Monitor, db: Database) -> None:
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer(f"Hazırkı interval: {monitor.interval} san.\n"
                             f"Dəyişmək: /interval &lt;{settings.min_interval}–{settings.max_interval}&gt;")
        return
    sec = int(raw)
    if not settings.min_interval <= sec <= settings.max_interval:
        await message.answer(f"İnterval {settings.min_interval}–{settings.max_interval} san arasında olmalıdır")
        return
    await monitor.set_interval(sec)
    await db.add_event(None, "INTERVAL_CHANGED", "INFO", f"{sec} san (user {message.from_user.id})")
    await message.answer(f"✅ Yoxlama intervalı: {sec} san")


@router.message(Command("events"))
async def cmd_events(message: Message, command: CommandObject, db: Database) -> None:
    raw = (command.args or "").strip()
    limit = min(100, max(1, int(raw))) if raw.isdigit() else 20
    events = await db.recent_events(limit)
    if not events:
        await message.answer("Jurnal boşdur.")
        return
    lines = [f"📜 <b>Hadisələr jurnalı</b> (son {len(events)})", ""]
    for e in reversed(events):
        stream = f"[{esc(e.stream)}] " if e.stream else ""
        lines.append(f"<code>{fmt_time(e.ts, True)}</code> {SEVERITY_ICON.get(e.severity, '⚪')} "
                     f"<b>{e.severity}</b> {stream}{esc(e.type)}"
                     + (f" — {esc(e.details[:200])}" if e.details else ""))
    await _reply_long(message, "\n".join(lines))


@router.message(Command("report"))
async def cmd_report(message: Message, db: Database, monitor: Monitor, system: SystemMonitor,
                     vmix: VmixMonitor) -> None:
    text = await build_report(db, monitor, system)
    if vmix.last is not None:
        st = vmix.last
        text += ("\n" + (f"🎛 vMix {esc(st.version)}: streaming={st.streaming} · recording={st.recording} · "
                         f"inputs={st.inputs_count} · active={esc(st.active_input)}"
                         if st.reachable else f"🎛 vMix: {esc(st.error)}"))
    await _reply_long(message, text)


@router.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject, monitor: Monitor, db: Database) -> None:
    parts = (command.args or "").rsplit(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("İstifadə: /mute &lt;ad&gt; &lt;dəqiqə&gt; (0 — yenidən aç)")
        return
    name, minutes = parts[0].strip(), min(int(parts[1]), 7 * 24 * 60)
    w = monitor.find(name)
    if not w:
        await message.answer(f"«{esc(name)}» tapılmadı. Siyahı: /status")
        return
    until = await monitor.mute(w.name, minutes)
    if minutes == 0:
        await db.add_event(w.name, "UNMUTED", "INFO", f"user {message.from_user.id}")
        await message.answer(f"🔔 <b>{esc(w.name)}</b> üçün xəbərdarlıqlar yenidən açıldı.")
    else:
        await db.add_event(w.name, "MUTED", "INFO", f"{minutes} dəq (user {message.from_user.id})")
        await message.answer(f"🔇 <b>{esc(w.name)}</b> üçün xəbərdarlıqlar {fmt_time(until)}-dək söndürüldü "
                             f"({minutes} dəq). Hadisələr jurnala yazılmağa davam edir.")


@router.message(Command("testalert"))
async def cmd_testalert(message: Message, notifier: Notifier, db: Database) -> None:
    """Проверка доставки: шлём образец алерта во все чаты получателей."""
    now = time.time()
    await notifier.send(
        "🧪 <b>TEST</b>\n"
        "🔴 <b>CRITICAL</b> | Nümunə yayım\n"
        f"Yayım dayandı ({settings.fail_threshold} ardıcıl yoxlama)\n"
        "Səbəb: Pleylistdə HTTP 404\n"
        f"Son dəfə efirdə: {fmt_time(now - 35)}\n"
        "Mənbə: https://example.com/live.m3u8"
    )
    await notifier.send("🧪 <b>TEST</b>\n🟢 <b>RECOVERED</b> | Nümunə yayım\nYayım bərpa olundu\n"
                        "Dayanma müddəti: <b>2 dəq 15 san</b>")
    await db.add_event(None, "TEST_ALERT", "INFO", f"user {message.from_user.id}")


@router.message(Command("chatid"))
async def cmd_chatid(message: Message) -> None:
    await message.answer(f"Bu çatın ID-si: <code>{message.chat.id}</code>\n"
                         "Xəbərdarlıqları qrupa/kanala da göndərmək üçün .env faylına əlavə edin: "
                         f"<code>ALERT_CHAT_IDS={message.chat.id}</code>")


@router.message(F.text.regexp(URL_RE))
async def plain_url(message: Message, client: httpx.AsyncClient, guard: HostGuard, db: Database) -> None:
    await _do_check(message, message.text.strip(), client, guard, db)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Əmri başa düşmədim. Əmrlərin siyahısı: /help")
