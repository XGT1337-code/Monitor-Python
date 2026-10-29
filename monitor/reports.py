"""Отчёты: статус всех эфиров (по команде и раз в час), сводка за 24 часа (ежедневно)."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from bot.formatting import STATE_ICON, STATE_NAME, esc, fmt_duration, fmt_time, short_url
from bot.notifier import Notifier
from checks.diagnostics import LIVE_STATUS_NAME
from config import settings
from monitor.engine import Monitor
from monitor.state_machine import State
from monitor.system import SystemMonitor, VmixMonitor
from storage.db import Database

log = logging.getLogger(__name__)
DAY = 86400


def build_list(monitor: Monitor) -> str:
    """Шапка меню /status — сами эфиры показываются кнопками под сообщением."""
    views = monitor.views()
    if not views:
        return "Monitorinqdə yayım yoxdur. Əlavə etmək: /add &lt;link&gt; &lt;ad&gt;"
    red = sum(1 for v in views if v.state == State.DOWN)
    yellow = sum(1 for v in views if v.state == State.DEGRADED)
    return (f"📋 <b>Yayımlar menyusu</b> · 🟢 {len(views) - red - yellow} · 🟡 {yellow} · 🔴 {red}\n\n"
            "Ətraflı hesabat üçün yayımın düyməsini basın.\n"
            "Hesabatın altında: yenilə, 1 saatlıq səssiz rejim, siyahıya qayıt.")


def build_status(monitor: Monitor, system: SystemMonitor, title: str = "📡 Yayımların statusu",
                 vmix: VmixMonitor | None = None) -> str:
    """Короткий статус всех эфиров: 🟢/🟡/🔴, качество, ссылка на источник."""
    views = monitor.views()
    now = time.time()
    red = sum(1 for v in views if v.state == State.DOWN)
    yellow = sum(1 for v in views if v.state == State.DEGRADED)
    green = len(views) - red - yellow
    lines = [f"<b>{title}</b> · {datetime.now():%d.%m %H:%M}",
             f"🟢 {green} · 🟡 {yellow} · 🔴 {red} · yoxlama hər {monitor.interval} san", ""]
    if not views:
        lines.append("Monitorinqdə yayım yoxdur. Əlavə etmək: /add &lt;link&gt; &lt;ad&gt;")
    for v in views:
        line = f"{STATE_ICON.get(v.state.value, '⚪')} <b>{esc(v.name)}</b> — {STATE_NAME.get(v.state.value, v.state.value)}"
        if v.since:
            line += f" ({fmt_duration(now - v.since)})"
        r = v.last
        if r:
            details = []
            if r.probe and r.probe.ok and r.probe.height:
                details.append(f"{r.probe.width}x{r.probe.height}@{r.probe.fps or '?'}")
            if r.bitrate_kbps:
                details.append(f"{r.bitrate_kbps:.0f} kbit/s")
            if r.latency_ms is not None:
                details.append(f"{r.latency_ms:.0f} ms")
            rs = r.resolved
            if rs and rs.viewers is not None:
                details.append(f"👁 {rs.viewers}")
            if rs and rs.extractor and rs.live_status:
                details.append(f"{esc(rs.extractor)}: {LIVE_STATUS_NAME.get(rs.live_status, rs.live_status)}")
            if details:
                line += "\n    " + " · ".join(details)
            if r.reasons:
                line += f"\n    └ {esc(r.reasons[0])}"
            elif v.fail_count:
                line += f"\n    └ ardıcıl uğursuz yoxlama: {v.fail_count}"
        line += f"\n    🔗 {esc(short_url(v.url, 90))}"
        if v.muted_until > now:
            line += f"\n    🔇 {fmt_time(v.muted_until)}-dək xəbərdarlıqsız"
        lines.append(line)
    if vmix is not None and vmix.summary():
        lines.append("\n" + vmix.summary())
    s = system.last
    if s.ts:
        lines.append(f"\n🖥 Server: CPU {s.cpu:.0f}% · RAM {s.ram:.0f}% · disk {s.disk:.0f}%")
    return "\n".join(lines)


async def build_report(db: Database, monitor: Monitor, system: SystemMonitor, hours: int = 24) -> str:
    now = time.time()
    window_start = now - hours * 3600
    lines = [f"📊 <b>Son {hours} saatın hesabatı</b> ({datetime.now():%d.%m.%Y %H:%M})", ""]
    streams = await db.list_streams()
    if not streams:
        lines.append("Monitorinqdə yayım yoxdur.")
    for row in streams:
        start = max(window_start, row.created_at)
        observed = max(1.0, now - start)
        incidents = await db.incidents_since(row.name, window_start)
        downtime = 0.0
        durations: list[float] = []
        started_in_window = 0
        for inc in incidents:
            end = inc["end_ts"] or now
            downtime += max(0.0, min(end, now) - max(inc["start_ts"], start))
            if inc["start_ts"] >= window_start:
                started_in_window += 1
            if inc["end_ts"]:
                durations.append(inc["end_ts"] - inc["start_ts"])
        uptime = max(0.0, 100.0 * (1 - downtime / observed))
        mttr = sum(durations) / len(durations) if durations else None
        stats = await db.metric_stats(row.name, window_start)
        worker = monitor.find(row.name)
        state = worker.sm.state.value if worker else row.state
        lines.append(f"{STATE_ICON.get(state, '⚪')} <b>{esc(row.name)}</b>")
        lines.append(f"   İşləmə vaxtı: <b>{uptime:.2f}%</b> · dayanma sayı: {started_in_window} · "
                     f"cəmi dayanma: {fmt_duration(downtime)}")
        lines.append(f"   Orta bərpa vaxtı: {fmt_duration(mttr) if mttr else '—'}")
        total = stats.get("total") or 0
        if total:
            lines.append(f"   Yoxlama: {total} · uğursuz: {stats.get('failed') or 0} · "
                         f"keyfiyyət problemi ilə: {stats.get('degraded') or 0}")
            extras = []
            if stats.get("avg_latency"):
                extras.append(f"orta gecikmə {stats['avg_latency']:.0f} ms")
            if stats.get("avg_bitrate"):
                extras.append(f"orta bitreyt {stats['avg_bitrate']:.0f} kbit/s")
            if extras:
                lines.append("   " + " · ".join(extras))
        lines.append(f"   🔗 {esc(short_url(row.url, 90))}")
        lines.append("")

    crit = await db.count_events(window_start, "CRITICAL")
    warn = await db.count_events(window_start, "WARNING")
    lines.append(f"Jurnal: 🔴 CRITICAL {crit} · 🟡 WARNING {warn}")
    s = system.last
    if s.ts:
        lines.append(f"Server: CPU {s.cpu:.0f}% · RAM {s.ram:.0f}% · disk {s.disk:.0f}% · "
                     f"şəbəkə ↓{s.net_rx_kbps / 1000:.1f} ↑{s.net_tx_kbps / 1000:.1f} Mbit/s")
    return "\n".join(lines)


def _seconds_until(target) -> float:
    now = datetime.now()
    nxt = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


async def daily_report_loop(db: Database, monitor: Monitor, system: SystemMonitor, notifier: Notifier) -> None:
    while True:
        await asyncio.sleep(_seconds_until(settings.daily_report_time))
        try:
            await notifier.send(await build_report(db, monitor, system))
            await db.add_event(None, "DAILY_REPORT", "INFO", "Gündəlik hesabat göndərildi")
            # Заодно чистим старые данные
            await db.purge(time.time() - settings.retention_days * DAY)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка ежедневного отчёта")
        await asyncio.sleep(61)  # не отправить дважды в ту же минуту


async def periodic_status_loop(monitor: Monitor, system: SystemMonitor, notifier: Notifier,
                               vmix: VmixMonitor | None = None) -> None:
    """Статус всех эфиров раз в STATUS_EVERY_MIN минут (выравнивается по началу часа)."""
    period = settings.status_every_min * 60
    if period <= 0:
        log.info("Периодический статус отключён (STATUS_EVERY_MIN=0)")
        return
    while True:
        now = time.time()
        await asyncio.sleep(period - (now % period) + 1)  # 10:00, 11:00, ... при периоде 60 мин
        try:
            if monitor.workers:
                await notifier.send(build_status(monitor, system, "🕐 Saatlıq status", vmix))
        except Exception:  # noqa: BLE001
            log.exception("Ошибка периодического статуса")
