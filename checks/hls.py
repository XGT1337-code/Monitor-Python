"""Разбор HLS-плейлистов (m3u8) и проверка сегментов."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin

import httpx

from checks.validators import HostGuard, UrlValidationError, parse_url
from config import settings

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


@dataclass
class Variant:
    url: str
    bandwidth: int = 0
    resolution: str = ""
    audio_group: str = ""
    audio_url: str = ""     # отдельная аудиодорожка (EXT-X-MEDIA TYPE=AUDIO), как у YouTube


@dataclass
class Segment:
    url: str
    duration: float
    program_date_time: float | None = None
    gap: bool = False


@dataclass
class Playlist:
    is_master: bool = False
    variants: list[Variant] = field(default_factory=list)
    media_sequence: int = 0
    target_duration: float = 0.0
    segments: list[Segment] = field(default_factory=list)
    ended: bool = False
    discontinuities: int = 0

    @property
    def last_index(self) -> int:
        """Номер последнего сегмента (media sequence + количество - 1)."""
        return self.media_sequence + len(self.segments) - 1

    @property
    def gaps(self) -> int:
        return sum(1 for s in self.segments if s.gap)


def is_m3u8(body: bytes, content_type: str = "", url: str = "") -> bool:
    return (
        body.lstrip()[:7] == b"#EXTM3U"
        or "mpegurl" in content_type
        or url.split("?")[0].lower().endswith(".m3u8")
    )


_PAGE_M3U8_RE = re.compile(r"""[^\s"'<>`\\(),]+\.m3u8(?:\?[^\s"'<>`\\]*)?""", re.IGNORECASE)


def find_playlist_urls(body: bytes, base_url: str) -> list[str]:
    """Поиск ссылок на .m3u8 в HTML/JS страницы (плееры на Next.js, hls.js, video.js и т.п.).

    Нужен для сайтов, которые не поддерживает yt-dlp (например, biznestv.az).
    """
    text = body.decode("utf-8", "replace").replace("\\/", "/").replace('\\"', '"').replace("\\u002F", "/")
    found: list[str] = []
    for raw in _PAGE_M3U8_RE.findall(text):
        url = urljoin(base_url, raw.lstrip("=:"))
        if url.startswith(("http://", "https://")) and url not in found:
            found.append(url)
    # master-плейлисты обычно предпочтительнее — ставим их первыми
    found.sort(key=lambda u: 0 if any(k in u.lower() for k in ("master", "playlist", "index", "live")) else 1)
    return found[:5]


def _parse_pdt(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_playlist(text: str, base_url: str) -> Playlist:
    pl = Playlist()
    pending_duration: float | None = None
    pending_pdt: float | None = None
    pending_gap = False
    pending_variant: Variant | None = None
    audio_groups: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = {k: v.strip('"') for k, v in _ATTR_RE.findall(line.split(":", 1)[1])}
            if attrs.get("TYPE") == "AUDIO" and attrs.get("URI"):
                group = attrs.get("GROUP-ID", "")
                # DEFAULT=YES имеет приоритет, иначе первая дорожка группы
                if group not in audio_groups or attrs.get("DEFAULT") == "YES":
                    audio_groups[group] = urljoin(base_url, attrs["URI"])
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pl.is_master = True
            attrs = {k: v.strip('"') for k, v in _ATTR_RE.findall(line.split(":", 1)[1])}
            try:
                bw = int(attrs.get("BANDWIDTH", "0"))
            except ValueError:
                bw = 0
            pending_variant = Variant(url="", bandwidth=bw, resolution=attrs.get("RESOLUTION", ""),
                                      audio_group=attrs.get("AUDIO", ""))
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                pl.media_sequence = int(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                pl.target_duration = float(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("#EXTINF:"):
            try:
                pending_duration = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                pending_duration = 0.0
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending_pdt = _parse_pdt(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-GAP"):
            pending_gap = True
        elif line.startswith("#EXT-X-DISCONTINUITY") and not line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE"):
            pl.discontinuities += 1
        elif line.startswith("#EXT-X-ENDLIST"):
            pl.ended = True
        elif not line.startswith("#"):
            absolute = urljoin(base_url, line)
            if pending_variant is not None:
                pending_variant.url = absolute
                pl.variants.append(pending_variant)
                pending_variant = None
            elif pending_duration is not None:
                pl.segments.append(Segment(absolute, pending_duration, pending_pdt, pending_gap))
                pending_duration, pending_pdt, pending_gap = None, None, False
    for v in pl.variants:
        v.audio_url = audio_groups.get(v.audio_group, "")
    return pl


def pick_variant(pl: Playlist) -> Variant | None:
    """Мониторим самый качественный вариант (если упадёт он — зритель это заметит)."""
    if not pl.variants:
        return None
    return max(pl.variants, key=lambda v: v.bandwidth)


@dataclass
class SegmentResult:
    url: str
    bytes: int = 0
    duration: float = 0.0
    ttfb_ms: float | None = None
    total_ms: float | None = None
    bitrate_kbps: float | None = None
    error: str | None = None


async def fetch_segment(
    client: httpx.AsyncClient, seg: Segment, guard: HostGuard, headers: dict[str, str] | None = None
) -> SegmentResult:
    """Скачивает последний сегмент: замер скорости, реального битрейта, доступности."""
    res = SegmentResult(url=seg.url, duration=seg.duration)
    try:
        await guard.check_host(parse_url(seg.url).host)
    except UrlValidationError as exc:
        res.error = f"Seqment: {exc}"
        return res
    start = time.perf_counter()
    try:
        async with client.stream(
            "GET", seg.url, headers=headers, timeout=settings.timeouts.segment, follow_redirects=True
        ) as resp:
            res.ttfb_ms = (time.perf_counter() - start) * 1000
            if resp.status_code >= 400:
                res.error = f"Seqment: HTTP {resp.status_code}"
                return res
            async for chunk in resp.aiter_bytes():
                res.bytes += len(chunk)
                if res.bytes > settings.max_segment_bytes:
                    break
        res.total_ms = (time.perf_counter() - start) * 1000
        if seg.duration > 0 and res.bytes:
            res.bitrate_kbps = res.bytes * 8 / seg.duration / 1000
    except httpx.TimeoutException:
        res.error = "Seqment: yükləmə vaxtı bitdi"
    except httpx.HTTPError as exc:
        res.error = f"Seqment: {type(exc).__name__}"
    return res
