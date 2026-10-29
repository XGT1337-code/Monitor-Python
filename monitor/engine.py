"""Движок мониторинга: отдельная asyncio-задача на каждый эфир.

Одна битая ссылка не влияет на остальные: все исключения ловятся внутри воркера.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from checks.diagnostics import CheckResult, Health, StreamChecker
from checks.validators import HostGuard
from config import settings
from monitor.alerts import ActiveProblem, AlertManager
from monitor.state_machine import Kind, State, StreamStateMachine
from storage.db import Database

log = logging.getLogger(__name__)


@dataclass
class StreamView:
    """Снимок состояния эфира для команд бота."""
    name: str
    url: str
    state: State
    since: float
    last_up_ts: float | None
    muted_until: float
    fail_count: int
    last: CheckResult | None


class StreamWorker:
    def __init__(self, monitor: Monitor, name: str, url: str, sm: StreamStateMachine,
                 muted_until: float) -> None:
        self.monitor = monitor
        self.name = name
        self.url = url
        self.sm = sm
        self.muted_until = muted_until
        self.checker = StreamChecker(name, url, monitor.client, monitor.guard)
        self.last: CheckResult | None = None
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._loop(), name=f"stream:{self.name}")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _loop(self) -> None:
        # Небольшой разнос старта, чтобы эфиры не проверялись синхронно
        await asyncio.sleep((hash(self.name) % 1000) / 1000 * 2)
        while True:
            started = time.monotonic()
            result = await self._run_check()
            try:
                await self._process(result)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка обработки результата %s", self.name)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, self.monitor.interval - elapsed))

    async def _run_check(self) -> CheckResult:
        try:
            return await asyncio.wait_for(self.checker.run(), settings.timeouts.check_total)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            r = CheckResult(ts=time.time(), url=self.url)
            r.fail(f"Yoxlama {settings.timeouts.check_total:.0f} san-yə sığmadı")
            return r.finalize(time.perf_counter())
        except Exception as exc:  # noqa: BLE001
            log.exception("Сбой проверки %s", self.name)
            r = CheckResult(ts=time.time(), url=self.url)
            r.fail(f"Yoxlamanın daxili xətası: {type(exc).__name__}")
            return r.finalize(time.perf_counter())

    async def _process(self, r: CheckResult) -> None:
        db, alerts = self.monitor.db, self.monitor.alerts
        self.last = r
        await db.add_metric(self.name, r.health.value, r.latency_ms, r.bitrate_kbps,
                            r.probe.fps if r.probe and r.probe.ok else None,
                            r.probe.height if r.probe and r.probe.ok else None)
        for n in r.notices:
            await alerts.notice(self.name, n)

        prev_state = self.sm.state
        t = self.sm.feed(r)
        # Антифлаппинг: одиночные неудачи пишем в журнал, но не алертим
        if r.health == Health.FAIL and self.sm.state != State.DOWN:
            await db.add_event(self.name, "CHECK_FAILED", "INFO",
                               f"{self.sm.fail_count}/{settings.fail_threshold}: {r.reasons[0] if r.reasons else ''}")
        alerts.update_reasons(self.name, r.reasons)

        if t is not None:
            if t.kind == Kind.DOWN:
                await db.open_incident(self.name, t.since, "; ".join(t.reasons))
            elif t.kind == Kind.RECOVERED:
                await db.close_incident(self.name, r.ts)
            await alerts.handle_transition(self.name, t)
        if t is not None or self.sm.state != prev_state:
            await db.update_state(self.name, self.sm.state.value, self.sm.since, self.sm.last_up_ts)

    def view(self) -> StreamView:
        return StreamView(self.name, self.url, self.sm.state, self.sm.since, self.sm.last_up_ts,
                          self.muted_until, self.sm.fail_count, self.last)


class Monitor:
    def __init__(self, db: Database, alerts: AlertManager, client: httpx.AsyncClient, guard: HostGuard) -> None:
        self.db = db
        self.alerts = alerts
        self.client = client
        self.guard = guard
        self.interval = settings.default_interval
        self.workers: dict[str, StreamWorker] = {}
        alerts.is_muted = self.is_muted
        alerts.url_of = lambda name: self.workers[name].url if name in self.workers else ""

    async def start(self) -> None:
        saved = await self.db.get_setting("interval")
        if saved and saved.isdigit():
            self.interval = min(settings.max_interval, max(settings.min_interval, int(saved)))
        for row in await self.db.list_streams():
            try:
                state = State(row.state)
            except ValueError:
                state = State.UNKNOWN
            sm = StreamStateMachine(state, row.state_since, row.last_up_ts)
            if state in (State.DOWN, State.DEGRADED):
                # проблема была до рестарта — продолжаем напоминания и пришлём RECOVERED
                self.alerts.active[row.name] = ActiveProblem(
                    state, row.state_since or time.time(), ["vəziyyət yenidən başladılmadan sonra bərpa olundu"],
                    last_up_ts=row.last_up_ts, last_sent=time.time(), sent=True,
                )
            worker = StreamWorker(self, row.name, row.url, sm, row.muted_until)
            self.workers[row.name] = worker
            worker.start()
        log.info("Мониторинг запущен: %d эфир(ов), интервал %d с", len(self.workers), self.interval)

    async def stop(self) -> None:
        await asyncio.gather(*(w.stop() for w in self.workers.values()), return_exceptions=True)

    async def add(self, name: str, url: str) -> None:
        await self.db.add_stream(name, url)
        worker = StreamWorker(self, name, url, StreamStateMachine(), 0.0)
        self.workers[name] = worker
        worker.start()

    async def remove(self, name: str) -> bool:
        worker = self.workers.pop(name, None)
        if worker:
            await worker.stop()
        self.alerts.forget(name)
        return await self.db.remove_stream(name) or worker is not None

    async def set_interval(self, seconds: int) -> None:
        self.interval = seconds
        await self.db.set_setting("interval", str(seconds))

    async def mute(self, name: str, minutes: int) -> float:
        until = time.time() + minutes * 60 if minutes > 0 else 0.0
        await self.db.set_mute(name, until)
        if name in self.workers:
            self.workers[name].muted_until = until
        return until

    def is_muted(self, name: str) -> bool:
        w = self.workers.get(name)
        return bool(w and w.muted_until > time.time())

    def find(self, name: str) -> StreamWorker | None:
        if name in self.workers:
            return self.workers[name]
        lowered = name.lower()
        for key, w in self.workers.items():
            if key.lower() == lowered:
                return w
        return None

    def views(self) -> list[StreamView]:
        return [w.view() for w in sorted(self.workers.values(), key=lambda w: w.name.lower())]
