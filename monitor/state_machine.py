"""Конечный автомат состояния эфира с антифлаппингом.

UNKNOWN → UP ⇄ DEGRADED
UP/DEGRADED → DOWN      (только после FAIL_THRESHOLD неудач подряд)
DOWN → RECOVERED → UP   (после RECOVER_THRESHOLD успешных проверок подряд)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from checks.diagnostics import CheckResult, Health
from config import settings


class State(str, Enum):
    UNKNOWN = "UNKNOWN"
    UP = "UP"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    RECOVERED = "RECOVERED"


class Kind(str, Enum):
    STARTED = "started"
    DOWN = "down"
    DEGRADED = "degraded"
    RECOVERED = "recovered"
    QUALITY_RESTORED = "quality_restored"


@dataclass
class Transition:
    kind: Kind
    old: State
    new: State
    reasons: list[str] = field(default_factory=list)
    fail_count: int = 0
    duration: float | None = None      # длительность простоя/деградации, с
    last_up_ts: float | None = None
    since: float = 0.0


class StreamStateMachine:
    def __init__(self, state: State = State.UNKNOWN, since: float | None = None,
                 last_up_ts: float | None = None) -> None:
        self.state = state
        self.since = since or 0.0
        self.last_up_ts = last_up_ts
        self.fail_count = 0          # FAIL подряд
        self.reach_count = 0         # не-FAIL подряд (для выхода из DOWN)
        self.ok_count = 0            # OK подряд (для выхода из DEGRADED)
        self.degrade_count = 0       # DEGRADED подряд
        self.first_fail_ts = 0.0
        self.last_reasons: list[str] = []
        # Восстановление после рестарта бота: DOWN остаётся DOWN до реального восстановления
        if state == State.DOWN:
            self.fail_count = settings.fail_threshold
            self.first_fail_ts = self.since

    def feed(self, r: CheckResult) -> Transition | None:
        now = r.ts
        if r.reasons:
            self.last_reasons = list(r.reasons)

        if r.health == Health.FAIL:
            self.fail_count += 1
            self.reach_count = self.ok_count = 0
            if self.fail_count == 1:
                self.first_fail_ts = now
            if self.state != State.DOWN and self.fail_count >= settings.fail_threshold:
                old = self.state
                self.state, self.since = State.DOWN, self.first_fail_ts
                return Transition(Kind.DOWN, old, State.DOWN, list(r.fail_reasons),
                                  self.fail_count, last_up_ts=self.last_up_ts, since=self.since)
            return None

        # Эфир доступен (OK или DEGRADED)
        self.last_up_ts = now
        self.fail_count = 0
        self.reach_count += 1
        if r.health == Health.DEGRADED:
            self.degrade_count += 1
            self.ok_count = 0
        else:
            self.degrade_count = 0
            self.ok_count += 1

        if self.state == State.DOWN:
            if self.reach_count < settings.recover_threshold:
                return None
            downtime = now - self.since
            new = State.DEGRADED if r.health == Health.DEGRADED else State.RECOVERED
            self.state, self.since = new, now
            return Transition(Kind.RECOVERED, State.DOWN, new, list(r.degrade_reasons),
                              duration=downtime, last_up_ts=now, since=now)

        if r.health == Health.DEGRADED:
            if self.state != State.DEGRADED and self.degrade_count >= settings.degrade_threshold:
                old = self.state
                self.state, self.since = State.DEGRADED, now
                return Transition(Kind.DEGRADED, old, State.DEGRADED, list(r.degrade_reasons), since=now)
            return None

        # OK
        if self.state == State.DEGRADED and self.ok_count >= settings.recover_threshold:
            duration = now - self.since
            self.state, self.since = State.UP, now
            return Transition(Kind.QUALITY_RESTORED, State.DEGRADED, State.UP, duration=duration, since=now)
        if self.state == State.RECOVERED and self.ok_count >= settings.recover_threshold:
            self.state, self.since = State.UP, self.since  # тихий переход, время «в эфире» не сбрасываем
            return None
        if self.state == State.UNKNOWN:
            self.state, self.since = State.UP, now
            return Transition(Kind.STARTED, State.UNKNOWN, State.UP, since=now)
        return None
