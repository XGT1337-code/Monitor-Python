"""SIEM-логика алертов: журнал, дедупликация, напоминания, mute, корреляция падений.

Бот сам присылает только события с важностью не ниже ALERT_MIN_SEVERITY
(по умолчанию CRITICAL — красные). Остальное пишется в журнал (/events)
и видно в статусе (/status, ежечасная сводка).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from bot.formatting import SEVERITY_ICON, esc, fmt_duration, fmt_time
from bot.notifier import Notifier
from checks.diagnostics import Notice
from checks.network import tcp_check
from checks.vmix import VmixStatus
from config import settings
from monitor.state_machine import Kind, State, Transition
from storage.db import Database

log = logging.getLogger(__name__)
_RANK = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}


def pushes(severity: str) -> bool:
    """Отправлять ли событие этой важности в Telegram (иначе — только журнал)."""
    return _RANK.get(severity, 0) >= _RANK.get(settings.alert_min_severity, 2)


@dataclass
class ActiveProblem:
    state: State
    since: float
    reasons: list[str] = field(default_factory=list)
    fail_count: int = 0
    last_up_ts: float | None = None
    last_sent: float = 0.0
    sent: bool = False

    @property
    def severity(self) -> str:
        return "CRITICAL" if self.state == State.DOWN else "WARNING"


def _src(url: str) -> str:
    """Строка со ссылкой на источник эфира."""
    return f"\nMənbə: {esc(url)}" if url else ""


def _reason(reasons: list[str]) -> str:
    return reasons[0] if reasons else "məlumat yoxdur"


class AlertManager:
    def __init__(self, db: Database, notifier: Notifier) -> None:
        self.db = db
        self.notifier = notifier
        self.active: dict[str, ActiveProblem] = {}
        self._pending_down: dict[str, ActiveProblem] = {}
        self._flush_task: asyncio.Task | None = None
        self._notice_ts: dict[str, float] = {}
        # Внедряются из Monitor/SystemMonitor (избегаем циклических импортов)
        self.is_muted: Callable[[str], bool] = lambda name: False
        self.vmix_status: Callable[[], VmixStatus | None] = lambda: None
        self.url_of: Callable[[str], str] = lambda name: ""

    # ------------------------------------------------------------ переходы
    async def handle_transition(self, name: str, t: Transition) -> None:
        try:
            await self._handle(name, t)
        except Exception:  # noqa: BLE001 — алерты не должны ронять воркер
            log.exception("Ошибка обработки перехода %s для %s", t.kind, name)

    async def _handle(self, name: str, t: Transition) -> None:
        if t.kind == Kind.STARTED:
            await self.db.add_event(name, "MONITOR_STARTED", "INFO", "Yayım efirdədir, monitorinq aktivdir")
            return

        if t.kind == Kind.DOWN:
            reason = _reason(t.reasons)
            await self.db.add_event(name, "STREAM_DOWN", "CRITICAL",
                                    f"{reason} ({t.fail_count} ardıcıl yoxlama)")
            problem = ActiveProblem(State.DOWN, t.since, t.reasons, t.fail_count, t.last_up_ts)
            self.active[name] = problem
            # Не шлём сразу — ждём окно корреляции, вдруг падают несколько эфиров
            self._pending_down[name] = problem
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flush_correlation())
            return

        if t.kind == Kind.DEGRADED:
            await self.db.add_event(name, "STREAM_DEGRADED", "WARNING", "; ".join(t.reasons))
            problem = ActiveProblem(State.DEGRADED, t.since, t.reasons)
            self.active[name] = problem
            await self._send_problem(name, problem)
            return

        if t.kind == Kind.RECOVERED:
            await self.db.add_event(name, "STREAM_RECOVERED", "INFO",
                                    f"Dayanma müddəti {fmt_duration(t.duration)}")
            was = self.active.pop(name, None)
            pending = self._pending_down.pop(name, None)
            if t.new == State.DEGRADED:
                self.active[name] = ActiveProblem(State.DEGRADED, t.since, t.reasons)
            if pending is not None or was is None or not was.sent or not settings.send_recovered:
                # о падении не сообщали (корреляция, mute) — фиксируем только в журнале
                if t.new == State.DEGRADED:
                    await self._send_problem(name, self.active[name])
                return
            text = (f"🟢 <b>RECOVERED</b> | {esc(name)}\n"
                    f"Yayım bərpa olundu\n"
                    f"Dayanma müddəti: <b>{fmt_duration(t.duration)}</b> ({fmt_time(was.since)}-dən)"
                    + _src(self.url_of(name)))
            if t.new == State.DEGRADED:
                text += "\n🟡 Amma keyfiyyət pisdir:\n" + "\n".join(f"• {esc(x)}" for x in t.reasons)
                if pushes("WARNING"):
                    self.active[name].sent = True
                    self.active[name].last_sent = time.time()
            if not self.is_muted(name):
                await self.notifier.send(text)
            return

        if t.kind == Kind.QUALITY_RESTORED:
            await self.db.add_event(name, "QUALITY_RESTORED", "INFO",
                                    f"Keyfiyyət problemi {fmt_duration(t.duration)} davam etdi")
            was = self.active.pop(name, None)
            if was and was.sent and not self.is_muted(name):
                await self.notifier.send(f"🟢 <b>OK</b> | {esc(name)}\nYayımın keyfiyyəti bərpa olundu "
                                         f"({fmt_duration(t.duration)} davam etdi)" + _src(self.url_of(name)))

    def update_reasons(self, name: str, reasons: list[str]) -> None:
        problem = self.active.get(name)
        if problem and reasons:
            problem.reasons = list(reasons)

    def forget(self, name: str) -> None:
        self.active.pop(name, None)
        self._pending_down.pop(name, None)

    # ------------------------------------------------------------ отправка
    def _problem_text(self, name: str, p: ActiveProblem, reminder: bool = False) -> str:
        head = "⏰ Xatırlatma | " if reminder else ""
        if p.state == State.DOWN:
            lines = [f"{head}🔴 <b>CRITICAL</b> | {esc(name)}",
                     f"Yayım dayandı ({p.fail_count or settings.fail_threshold} ardıcıl yoxlama)"
                     if not reminder else f"Yayım hələ də əlçatan deyil ({fmt_duration(time.time() - p.since)})",
                     f"Səbəb: {esc(_reason(p.reasons))}",
                     f"Son dəfə efirdə: {fmt_time(p.last_up_ts)}"]
            hint = self._vmix_hint()
            if hint:
                lines.append(hint)
        else:
            lines = [f"{head}🟡 <b>WARNING</b> | {esc(name)}",
                     "Yayımın keyfiyyəti pisləşdi" if not reminder
                     else f"Keyfiyyət problemi davam edir ({fmt_duration(time.time() - p.since)})"]
            lines += [f"• {esc(x)}" for x in p.reasons[:6]]
        return "\n".join(lines) + _src(self.url_of(name))

    def _vmix_hint(self) -> str:
        """Где искать проблему: по состоянию vMix в момент падения эфира."""
        vm = self.vmix_status()
        if vm is None:
            return ""
        if not vm.reachable:
            return "🎛 vMix cavab vermir — problem mənbədə (vMix kompüteri)"
        if vm.streaming is False:
            return "🎛 vMix yayım ETMİR — Stream düyməsini yoxlayın"
        if vm.streaming:
            return "🎛 vMix yayım edir ✅ — problem vMix ilə platforma arasındadır (internet/CDN/platforma)"
        return ""

    async def _send_problem(self, name: str, p: ActiveProblem) -> None:
        if self.is_muted(name) or not pushes(p.severity):
            return
        await self.notifier.send(self._problem_text(name, p))
        p.sent, p.last_sent = True, time.time()

    async def _flush_correlation(self) -> None:
        """Через окно корреляции решаем: один общий алерт или индивидуальные."""
        await asyncio.sleep(settings.correlation_window_sec)
        batch, self._pending_down = self._pending_down, {}
        visible = {n: p for n, p in batch.items() if not self.is_muted(n)}
        try:
            if len(visible) >= settings.correlation_min_streams:
                await self._send_correlated(visible)
            else:
                for name, problem in visible.items():
                    await self._send_problem(name, problem)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка отправки коррелированного алерта")

    async def _send_correlated(self, items: dict[str, ActiveProblem]) -> None:
        internet = await self._internet_ok()
        vmix = self.vmix_status()
        if not internet:
            cause = "Monitorinq serverinin internetə çıxışı yoxdur — problem bizim tərəfdədir"
        elif vmix and not vmix.reachable:
            cause = "vMix cavab vermir — problem mənbədədir (vMix kompüteri)"
        elif vmix and vmix.reachable and vmix.streaming is False:
            cause = "vMix yayım ETMİR (streaming=False) — problem mənbədədir"
        elif vmix and vmix.reachable and vmix.streaming:
            cause = "vMix yayım edir, amma platformalarda yayım yoxdur — problem internetdə/CDN-dədir"
        else:
            cause = "Ehtimal ki, şəbəkə/mənbə (vMix) problemi"
        lines = [f"🔴 <b>CRITICAL</b> | Kütləvi dayanma: {len(items)} yayım", esc(cause), ""]
        for name, p in items.items():
            lines.append(f"• <b>{esc(name)}</b> — {esc(_reason(p.reasons))}")
            if self.url_of(name):
                lines.append(f"   {esc(self.url_of(name))}")
        lines.append("")
        lines.append(f"Monitorinq serverində internet: {'✅ var' if internet else '❌ yoxdur'}")
        if vmix is not None:
            if vmix.reachable:
                lines.append(f"vMix: streaming={vmix.streaming} · recording={vmix.recording} · "
                             f"active={esc(vmix.active_input)}")
            else:
                lines.append(f"vMix: {esc(vmix.error or 'əlçatan deyil')}")
        await self.db.add_event(None, "CORRELATED_OUTAGE", "CRITICAL",
                                f"{cause}: {', '.join(items)}")
        await self.notifier.send("\n".join(lines))
        now = time.time()
        for p in items.values():
            p.sent, p.last_sent = True, now

    @staticmethod
    async def _internet_ok() -> bool:
        for host, port in (("1.1.1.1", 443), ("8.8.8.8", 443)):
            if (await tcp_check(host, port)).error is None:
                return True
        return False

    # ------------------------------------------------------------ напоминания
    async def reminder_loop(self) -> None:
        while True:
            await asyncio.sleep(20)
            now = time.time()
            for name, p in list(self.active.items()):
                try:
                    if name in self._pending_down or self.is_muted(name) or not pushes(p.severity):
                        continue
                    if not p.sent:
                        # mute закончился, а проблема осталась — сообщаем
                        await self._send_problem(name, p)
                    elif now - p.last_sent >= settings.remind_every_sec:
                        await self.notifier.send(self._problem_text(name, p, reminder=True))
                        p.last_sent = now
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка напоминания для %s", name)

    # ------------------------------------------------------------ разовые события
    async def notice(self, stream: str | None, n: Notice) -> None:
        """Событие с дедупликацией по ключу (смена IP, TLS, редирект, vMix, сервер)."""
        key = f"{stream}|{n.key}"
        now = time.time()
        if now - self._notice_ts.get(key, 0) < settings.notice_cooldown_sec:
            return
        self._notice_ts[key] = now
        if len(self._notice_ts) > 5000:
            self._notice_ts = {k: v for k, v in self._notice_ts.items()
                               if now - v < settings.notice_cooldown_sec}
        await self.db.add_event(stream, n.type, n.severity, n.text)
        if not pushes(n.severity) or (stream and self.is_muted(stream)):
            return  # ниже порога — только в журнал
        title = esc(stream) if stream else "Monitorinq serveri"
        await self.notifier.send(f"{SEVERITY_ICON.get(n.severity, '⚪')} <b>{n.severity}</b> | {title}\n"
                                 f"{esc(n.text)}" + (_src(self.url_of(stream)) if stream else ""))
