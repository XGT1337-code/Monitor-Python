"""Конфигурация приложения.

Секреты читаются ТОЛЬКО из переменных окружения (.env).
Токен бота больше нигде в коде не фигурирует — используйте settings.bot_token.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _parse_user_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return frozenset(ids)


def _parse_time(raw: str, default: dtime) -> dtime:
    try:
        hh, mm = raw.strip().split(":")
        return dtime(int(hh), int(mm))
    except (ValueError, AttributeError):
        return default


@dataclass(frozen=True)
class Thresholds:
    """Пороги качества эфира — ниже них эфир считается DEGRADED."""
    min_video_bitrate_kbps: int = _env_int("MIN_VIDEO_BITRATE_KBPS", 500)
    min_fps: float = _env_float("MIN_FPS", 20.0)
    min_height: int = _env_int("MIN_HEIGHT", 360)
    require_audio: bool = _env_bool("REQUIRE_AUDIO", True)
    # Детекторы ffmpeg
    silence_noise_db: float = _env_float("SILENCE_NOISE_DB", -50.0)
    silence_min_duration: float = _env_float("SILENCE_MIN_DURATION", 2.0)
    low_volume_db: float = _env_float("LOW_VOLUME_DB", -45.0)      # средняя громкость ниже — «тихо»
    black_min_duration: float = _env_float("BLACK_MIN_DURATION", 2.0)
    black_pixel_threshold: float = _env_float("BLACK_PIXEL_THRESHOLD", 0.10)
    freeze_noise: float = _env_float("FREEZE_NOISE", 0.001)
    freeze_min_duration: float = _env_float("FREEZE_MIN_DURATION", 2.0)
    # Сеть / HLS
    tcp_latency_warn_ms: int = _env_int("TCP_LATENCY_WARN_MS", 1000)
    hls_max_live_latency_sec: float = _env_float("HLS_MAX_LIVE_LATENCY_SEC", 60.0)
    hls_stale_factor: float = _env_float("HLS_STALE_FACTOR", 3.0)   # плейлист «протух» после N*TARGETDURATION
    hls_stale_min_sec: float = _env_float("HLS_STALE_MIN_SEC", 20.0)
    segment_slow_ratio: float = _env_float("SEGMENT_SLOW_RATIO", 0.8)  # загрузка сегмента / его длительность
    tls_expiry_warn_days: int = _env_int("TLS_EXPIRY_WARN_DAYS", 14)
    # Сервер мониторинга
    cpu_alert_percent: float = _env_float("CPU_ALERT_PERCENT", 90.0)
    ram_alert_percent: float = _env_float("RAM_ALERT_PERCENT", 90.0)
    disk_alert_percent: float = _env_float("DISK_ALERT_PERCENT", 90.0)
    # vMix: мастер-выход тише этого уровня = тишина
    vmix_silence_db: float = _env_float("VMIX_SILENCE_DB", -60.0)


@dataclass(frozen=True)
class Timeouts:
    """Таймауты всех сетевых операций и внешних процессов (секунды)."""
    dns: float = _env_float("TIMEOUT_DNS", 5)
    tcp: float = _env_float("TIMEOUT_TCP", 5)
    http: float = _env_float("TIMEOUT_HTTP", 10)
    tls: float = _env_float("TIMEOUT_TLS", 8)
    segment: float = _env_float("TIMEOUT_SEGMENT", 20)
    ffprobe: float = _env_float("TIMEOUT_FFPROBE", 20)
    ffmpeg_extra: float = _env_float("TIMEOUT_FFMPEG_EXTRA", 20)   # сверх длительности сэмпла
    ytdlp: float = _env_float("TIMEOUT_YTDLP", 45)
    vmix: float = _env_float("TIMEOUT_VMIX", 5)
    check_total: float = _env_float("TIMEOUT_CHECK_TOTAL", 90)     # жёсткий лимит одной проверки


@dataclass(frozen=True)
class Settings:
    # --- секреты и окружение ---
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_user_ids: frozenset[int] = _parse_user_ids(os.getenv("ALLOWED_USER_IDS", ""))
    # Доп. чаты для алертов (группы/каналы, ID вида -100...). Бот должен быть добавлен туда.
    alert_chat_ids: frozenset[int] = _parse_user_ids(os.getenv("ALERT_CHAT_IDS", ""))
    vmix_host: str = os.getenv("VMIX_HOST", "").strip()
    vmix_port: int = _env_int("VMIX_PORT", 8088)
    daily_report_time: dtime = _parse_time(os.getenv("DAILY_REPORT_TIME", "09:00"), dtime(9, 0))

    # --- пути ---
    data_dir: Path = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
    log_dir: Path = Path(os.getenv("LOG_DIR", BASE_DIR / "logs"))
    ffprobe_path: str = os.getenv("FFPROBE_PATH", "ffprobe")
    ffmpeg_path: str = os.getenv("FFMPEG_PATH", "ffmpeg")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # --- мониторинг ---
    default_interval: int = _env_int("CHECK_INTERVAL", 10)
    min_interval: int = 5
    max_interval: int = 60
    max_streams: int = _env_int("MAX_STREAMS", 50)
    fail_threshold: int = _env_int("FAIL_THRESHOLD", 3)          # DOWN после N неудач подряд
    degrade_threshold: int = _env_int("DEGRADE_THRESHOLD", 2)    # DEGRADED после N проверок подряд
    recover_threshold: int = _env_int("RECOVER_THRESHOLD", 2)    # выход из DOWN после N успешных
    media_confirm: int = _env_int("MEDIA_CONFIRM", 2)            # чёрный экран/фриз/тишина подтверждаются N анализами
    ffprobe_every: int = _env_int("FFPROBE_EVERY", 3)            # для HLS ffprobe раз в N проверок
    analyze_every: int = _env_int("ANALYZE_EVERY", 1)            # black/freeze/silence раз в N проверок (~0.6 с CPU)
    segment_every: int = _env_int("SEGMENT_EVERY", 3)            # скачивание сегмента HLS раз в N проверок
    sample_seconds: float = _env_float("SAMPLE_SECONDS", 4)      # длина сэмпла для ffmpeg-детекторов
    max_ffmpeg_procs: int = _env_int("MAX_FFMPEG_PROCS", 3)      # одновременных ffmpeg/ffprobe
    tls_check_every_sec: int = _env_int("TLS_CHECK_EVERY_SEC", 300)
    resolve_ttl_sec: int = _env_int("RESOLVE_TTL_SEC", 900)      # кэш прямой ссылки yt-dlp
    max_segment_bytes: int = _env_int("MAX_SEGMENT_BYTES", 30 * 1024 * 1024)

    # --- алерты / SIEM ---
    # Какие события бот присылает сам: CRITICAL — только красные (падение эфира),
    # WARNING — ещё и жёлтые (качество, смена IP, сертификат и т.п.). Остальное — только в журнал.
    alert_min_severity: str = (os.getenv("ALERT_MIN_SEVERITY", "CRITICAL").strip().upper() or "CRITICAL")
    send_recovered: bool = _env_bool("SEND_RECOVERED", True)       # 🟢 после красного алерта
    silence_critical: bool = _env_bool("SILENCE_CRITICAL", True)   # тишина в звуке = эфир упал (🔴)
    status_every_min: int = _env_int("STATUS_EVERY_MIN", 60)       # периодический статус, 0 — выкл.
    remind_every_sec: int = _env_int("REMIND_EVERY_SEC", 300)
    correlation_window_sec: float = _env_float("CORRELATION_WINDOW_SEC", 15)
    correlation_min_streams: int = _env_int("CORRELATION_MIN_STREAMS", 2)
    notice_cooldown_sec: int = _env_int("NOTICE_COOLDOWN_SEC", 3600)
    alert_on_ip_change: bool = _env_bool("ALERT_ON_IP_CHANGE", True)
    system_poll_sec: int = _env_int("SYSTEM_POLL_SEC", 30)
    vmix_poll_sec: int = _env_int("VMIX_POLL_SEC", 5)
    vmix_silence_sec: int = _env_int("VMIX_SILENCE_SEC", 15)       # тишина на мастере дольше N с = 🔴
    # Проверять FTB/mute/тишину даже без включённого Stream (для тестов)
    vmix_check_always: bool = _env_bool("VMIX_CHECK_ALWAYS", False)
    retention_days: int = _env_int("RETENTION_DAYS", 30)
    trusted_domains: tuple[str, ...] = tuple(
        d.strip().lower() for d in os.getenv(
            "TRUSTED_REDIRECT_DOMAINS",
            "youtube.com,youtu.be,googlevideo.com,ytimg.com,google.com,yoda.az,"
            "akamaized.net,cloudfront.net,fastly.net,cloudflare.com",
        ).split(",") if d.strip()
    )

    # --- firewall ---
    # OPEN_ACCESS=true: ботом пользуются все (просмотр, /check, подписка на алерты),
    # а управление (/add, /remove, /mute, /interval) — только админы из ALLOWED_USER_IDS
    open_access: bool = _env_bool("OPEN_ACCESS", False)
    # ACCESS_PASSWORD задан: доступ выдаётся тому, кто ввёл пароль после /start (имеет приоритет над OPEN_ACCESS)
    access_password: str = os.getenv("ACCESS_PASSWORD", "").strip()
    password_max_attempts: int = _env_int("PASSWORD_MAX_ATTEMPTS", 5)     # за окно ниже
    password_lock_sec: int = _env_int("PASSWORD_LOCK_SEC", 900)           # блокировка подбора, 15 мин
    admin_commands: tuple[str, ...] = ("/add", "/remove", "/mute", "/interval", "/users", "/kick")

    @property
    def multi_user(self) -> bool:
        """Ботом пользуются не только админы (открытый доступ или вход по паролю)."""
        return self.open_access or bool(self.access_password)
    rate_limit_count: int = _env_int("RATE_LIMIT_COUNT", 20)     # команд
    rate_limit_window: int = _env_int("RATE_LIMIT_WINDOW", 60)   # за N секунд
    check_cooldown_sec: int = _env_int("CHECK_COOLDOWN_SEC", 15) # /check не чаще
    allowed_schemes: tuple[str, ...] = ("http", "https", "rtmp", "rtmps", "srt")

    thresholds: Thresholds = field(default_factory=Thresholds)
    timeouts: Timeouts = field(default_factory=Timeouts)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "monitor.db"

    @property
    def whitelist_empty(self) -> bool:
        return not self.allowed_user_ids


settings = Settings()


def validate_settings() -> None:
    """Проверка обязательных параметров при старте (без вывода значений секретов)."""
    if not settings.bot_token:
        sys.stderr.write("ОШИБКА: TELEGRAM_BOT_TOKEN не задан в .env\n")
        sys.exit(1)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
