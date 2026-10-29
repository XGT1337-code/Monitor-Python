"""SQLite-хранилище: эфиры, журнал событий (SIEM), метрики, инциденты, настройки."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    name        TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    created_at  REAL NOT NULL,
    muted_until REAL NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'UNKNOWN',
    state_since REAL,
    last_up_ts  REAL
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    stream   TEXT,
    type     TEXT NOT NULL,
    severity TEXT NOT NULL,
    details  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_stream ON events(stream, ts);
CREATE TABLE IF NOT EXISTS metrics (
    ts           REAL NOT NULL,
    stream       TEXT NOT NULL,
    health       TEXT NOT NULL,
    latency_ms   REAL,
    bitrate_kbps REAL,
    fps          REAL,
    height       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_metrics_stream ON metrics(stream, ts);
CREATE TABLE IF NOT EXISTS incidents (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    stream   TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts   REAL,
    reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_stream ON incidents(stream, start_ts);
CREATE TABLE IF NOT EXISTS authorized_users (
    user_id INTEGER PRIMARY KEY,
    name    TEXT,
    ts      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id INTEGER PRIMARY KEY,
    name    TEXT,
    ts      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class StreamRow:
    name: str
    url: str
    created_at: float
    muted_until: float
    state: str
    state_since: float | None
    last_up_ts: float | None


@dataclass
class EventRow:
    id: int
    ts: float
    stream: str | None
    type: str
    severity: str
    details: str


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("База не открыта")
        return self._db

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ---------- эфиры ----------
    async def add_stream(self, name: str, url: str) -> None:
        await self.db.execute(
            "INSERT INTO streams(name, url, created_at) VALUES (?, ?, ?)", (name, url, time.time())
        )
        await self.db.commit()

    async def remove_stream(self, name: str) -> bool:
        cur = await self.db.execute("DELETE FROM streams WHERE name = ?", (name,))
        # незакрытые инциденты удалённого эфира закрываем
        await self.db.execute(
            "UPDATE incidents SET end_ts = ? WHERE stream = ? AND end_ts IS NULL", (time.time(), name)
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def list_streams(self) -> list[StreamRow]:
        async with self.db.execute("SELECT * FROM streams ORDER BY name") as cur:
            return [StreamRow(**dict(r)) for r in await cur.fetchall()]

    async def get_stream(self, name: str) -> StreamRow | None:
        async with self.db.execute("SELECT * FROM streams WHERE name = ?", (name,)) as cur:
            row = await cur.fetchone()
        return StreamRow(**dict(row)) if row else None

    async def set_mute(self, name: str, until_ts: float) -> None:
        await self.db.execute("UPDATE streams SET muted_until = ? WHERE name = ?", (until_ts, name))
        await self.db.commit()

    async def update_state(self, name: str, state: str, since: float, last_up_ts: float | None) -> None:
        await self.db.execute(
            "UPDATE streams SET state = ?, state_since = ?, last_up_ts = ? WHERE name = ?",
            (state, since, last_up_ts, name),
        )
        await self.db.commit()

    # ---------- журнал событий ----------
    async def add_event(self, stream: str | None, type_: str, severity: str, details: str = "") -> None:
        try:
            await self.db.execute(
                "INSERT INTO events(ts, stream, type, severity, details) VALUES (?, ?, ?, ?, ?)",
                (time.time(), stream, type_, severity, details[:2000]),
            )
            await self.db.commit()
        except Exception:  # noqa: BLE001 — журнал не должен ронять мониторинг
            log.exception("Не удалось записать событие %s", type_)

    async def recent_events(self, limit: int = 20, stream: str | None = None) -> list[EventRow]:
        if stream:
            sql, args = "SELECT * FROM events WHERE stream = ? ORDER BY id DESC LIMIT ?", (stream, limit)
        else:
            sql, args = "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        async with self.db.execute(sql, args) as cur:
            return [EventRow(**dict(r)) for r in await cur.fetchall()]

    async def count_events(self, since_ts: float, severity: str) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM events WHERE ts >= ? AND severity = ?", (since_ts, severity)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    # ---------- метрики ----------
    async def add_metric(self, stream: str, health: str, latency_ms: float | None,
                         bitrate_kbps: float | None, fps: float | None, height: int | None) -> None:
        try:
            await self.db.execute(
                "INSERT INTO metrics(ts, stream, health, latency_ms, bitrate_kbps, fps, height) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), stream, health, latency_ms, bitrate_kbps, fps, height),
            )
            await self.db.commit()
        except Exception:  # noqa: BLE001
            log.exception("Не удалось записать метрику %s", stream)

    async def metric_stats(self, stream: str, since_ts: float) -> dict[str, Any]:
        async with self.db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN health = 'FAIL' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN health = 'DEGRADED' THEN 1 ELSE 0 END) AS degraded, "
            "AVG(latency_ms) AS avg_latency, AVG(bitrate_kbps) AS avg_bitrate "
            "FROM metrics WHERE stream = ? AND ts >= ?",
            (stream, since_ts),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else {}

    # ---------- инциденты (для аптайма и MTTR) ----------
    async def open_incident(self, stream: str, start_ts: float, reason: str) -> None:
        await self.db.execute(
            "INSERT INTO incidents(stream, start_ts, reason) VALUES (?, ?, ?)", (stream, start_ts, reason)
        )
        await self.db.commit()

    async def close_incident(self, stream: str, end_ts: float) -> None:
        await self.db.execute(
            "UPDATE incidents SET end_ts = ? WHERE stream = ? AND end_ts IS NULL", (end_ts, stream)
        )
        await self.db.commit()

    async def incidents_since(self, stream: str, since_ts: float) -> list[dict[str, Any]]:
        """Инциденты, пересекающиеся с окном [since_ts, now]."""
        async with self.db.execute(
            "SELECT * FROM incidents WHERE stream = ? AND (end_ts IS NULL OR end_ts >= ?) ORDER BY start_ts",
            (stream, since_ts),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ---------- пользователи, получившие доступ по паролю ----------
    async def add_authorized(self, user_id: int, name: str) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO authorized_users(user_id, name, ts) VALUES (?, ?, ?)",
            (user_id, name[:100], time.time()),
        )
        await self.db.commit()

    async def remove_authorized(self, user_id: int) -> bool:
        cur = await self.db.execute("DELETE FROM authorized_users WHERE user_id = ?", (user_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def list_authorized(self) -> list[tuple[int, str, float]]:
        async with self.db.execute("SELECT user_id, name, ts FROM authorized_users ORDER BY ts") as cur:
            return [(int(r[0]), r[1] or "", float(r[2])) for r in await cur.fetchall()]

    # ---------- подписчики на алерты ----------
    async def add_subscriber(self, chat_id: int, name: str) -> bool:
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO subscribers(chat_id, name, ts) VALUES (?, ?, ?)", (chat_id, name[:100], time.time())
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def remove_subscriber(self, chat_id: int) -> bool:
        cur = await self.db.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def list_subscribers(self) -> list[int]:
        async with self.db.execute("SELECT chat_id FROM subscribers") as cur:
            return [int(r[0]) for r in await cur.fetchall()]

    # ---------- настройки ----------
    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        async with self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    # ---------- обслуживание ----------
    async def purge(self, older_than_ts: float) -> None:
        await self.db.execute("DELETE FROM metrics WHERE ts < ?", (older_than_ts,))
        await self.db.execute("DELETE FROM events WHERE ts < ?", (older_than_ts,))
        await self.db.execute(
            "DELETE FROM incidents WHERE end_ts IS NOT NULL AND end_ts < ?", (older_than_ts,)
        )
        await self.db.commit()
