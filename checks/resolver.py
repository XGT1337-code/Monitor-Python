"""Получение прямой ссылки на поток со страниц платформ (YouTube и др.) через yt-dlp."""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field

from checks.media import run_proc
from config import settings


@dataclass
class Resolved:
    ok: bool = False
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    title: str = ""
    extractor: str = ""
    live_status: str = ""      # is_live / was_live / not_live / is_upcoming / post_live
    error: str | None = None
    resolved_at: float = 0.0
    ttl: float = 0.0           # сколько секунд ссылка/данные считаются свежими (0 — RESOLVE_TTL_SEC)
    # Данные, которые показывает сама платформа (для отчёта)
    channel: str = ""
    viewers: int | None = None
    followers: int | None = None
    started_at: float | None = None
    details: list[str] = field(default_factory=list)   # прочие факты: категория, лайки, качество…
    via: list[str] = field(default_factory=list)       # цепочка: страница → плеер → поток

    @property
    def is_live(self) -> bool:
        return self.live_status in ("is_live", "")  # пустой — платформа не сообщает статус


def _int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _ytdlp_details(info: dict) -> list[str]:
    """Факты со страницы платформы, которые отдаёт yt-dlp (YouTube и др.)."""
    out = []
    if info.get("width") and info.get("height"):
        out.append(f"Platformada keyfiyyət: {info['width']}x{info['height']}"
                   + (f" @{info['fps']:g} fps" if info.get("fps") else ""))
    if info.get("like_count") is not None:
        out.append(f"Bəyənmə: {info['like_count']}")
    if info.get("categories"):
        out.append("Kateqoriya: " + ", ".join(map(str, info["categories"][:3])))
    if info.get("availability") and info["availability"] != "public":
        out.append(f"Giriş: {info['availability']}")
    return out


async def resolve_stream(page_url: str) -> Resolved:
    """yt-dlp как подпроцесс — надёжно убивается по таймауту, не блокирует event loop."""
    args = [
        sys.executable, "-m", "yt_dlp",
        "-j", "--no-playlist", "--no-warnings", "--socket-timeout", "10",
        # YouTube отдаёт видео и аудио раздельно — берём пару, а ниже предпочитаем master-манифест
        "-f", "bv*+ba/b",
        page_url,
    ]
    proc = await run_proc(args, settings.timeouts.ytdlp)
    if proc.not_found:
        return Resolved(error="yt-dlp quraşdırılmayıb")
    if proc.timed_out:
        return Resolved(error=f"yt-dlp: {settings.timeouts.ytdlp:.0f} san vaxt bitdi")
    if proc.returncode != 0:
        lines = [ln for ln in proc.stderr.strip().splitlines() if ln.strip()]
        msg = lines[-1] if lines else "axın alınmadı"
        return Resolved(error=f"yt-dlp: {msg.replace('ERROR: ', '')}"[:300])
    try:
        info = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return Resolved(error="yt-dlp: yanlış cavab")

    # Master HLS-манифест содержит и видео-варианты, и аудиогруппы — мониторим его
    fmts = info.get("requested_formats") or [info]
    url = (
        next((f.get("manifest_url") for f in fmts if f.get("manifest_url")), None)
        or info.get("manifest_url") or info.get("url") or fmts[0].get("url") or ""
    )
    headers = info.get("http_headers") or fmts[0].get("http_headers") or {}
    live_status = info.get("live_status") or ("is_live" if info.get("is_live") else "")
    if not url:
        return Resolved(error="yt-dlp: axın linki tapılmadı", live_status=live_status)
    return Resolved(
        ok=True,
        url=url,
        headers={k: str(v) for k, v in headers.items()},
        title=str(info.get("title") or "")[:200],
        extractor=str(info.get("extractor_key") or info.get("extractor") or ""),
        live_status=live_status,
        resolved_at=time.time(),
        channel=str(info.get("channel") or info.get("uploader") or "")[:100],
        viewers=_int(info.get("concurrent_view_count")),
        followers=_int(info.get("channel_follower_count")),
        started_at=info.get("release_timestamp") if live_status == "is_live" else None,
        details=_ytdlp_details(info),
    )
