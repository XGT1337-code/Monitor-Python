"""Оркестратор проверок одного эфира: сеть → TLS → плейлист/страница → ffprobe → ffmpeg-детекторы.

StreamChecker хранит состояние между проверками (известные IP, сертификаты,
рост media sequence, счётчики подтверждения чёрного экрана/фриза/тишины).
"""
from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import dataclass, field
from enum import Enum

import httpx

from checks import hls, platforms
from checks.media import MediaAnalysis, ProbeResult, analyze_media, ffprobe
from checks.network import (
    DnsResult, HttpResult, TcpResult, TlsResult, dns_check, host_of, http_fetch, tcp_check, tls_check,
)
from checks.resolver import Resolved, resolve_stream
from checks.validators import HostGuard, UrlValidationError, is_trusted_redirect, parse_url
from config import settings

_DIRECT_EXT = (".flv", ".mp4", ".ts", ".m4s", ".mkv", ".aac", ".mp3")
_STREAM_CT = ("video/", "audio/", "application/octet-stream", "flv", "mp2t")


# Официальные диапазоны CDN: смена IP внутри одной CDN — норма (балансировка), а не инцидент
_CDN_RANGES = {
    "Cloudflare": [ipaddress.ip_network(n) for n in (
        "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
        "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
        "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
        "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
        "2a06:98c0::/29", "2c0f:f248::/32",
    )],
}


def cdn_of(ips: set[str]) -> str | None:
    """Имя CDN, если ВСЕ адреса принадлежат ей, иначе None."""
    for cdn, nets in _CDN_RANGES.items():
        try:
            if ips and all(any(ipaddress.ip_address(ip) in n for n in nets) for ip in ips):
                return cdn
        except ValueError:
            return None
    return None


LIVE_STATUS_NAME = {"is_live": "efirdədir", "not_live": "efir yoxdur", "is_upcoming": "planlaşdırılıb",
                    "was_live": "efir bitib", "post_live": "efir bitib", "": "—"}


class Health(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    FAIL = "FAIL"


@dataclass
class Notice:
    """Разовое событие (смена IP, редирект, TLS) — дедуплицируется по ключу."""
    key: str
    type: str
    severity: str
    text: str


@dataclass
class CheckResult:
    ts: float
    url: str
    kind: str = "unknown"
    health: Health = Health.OK
    fail_reasons: list[str] = field(default_factory=list)
    degrade_reasons: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    dns: DnsResult | None = None
    tcp: TcpResult | None = None
    http: HttpResult | None = None
    tls: TlsResult | None = None
    resolved: Resolved | None = None
    media_url: str = ""
    variant: hls.Variant | None = None
    playlist: dict | None = None
    segment: hls.SegmentResult | None = None
    probe: ProbeResult | None = None
    probe_cached: bool = False
    media: MediaAnalysis | None = None
    duration_ms: float = 0.0

    def fail(self, reason: str) -> None:
        self.fail_reasons.append(reason)

    def degrade(self, reason: str) -> None:
        self.degrade_reasons.append(reason)

    @property
    def reasons(self) -> list[str]:
        return self.fail_reasons or self.degrade_reasons

    @property
    def latency_ms(self) -> float | None:
        if self.tcp and self.tcp.ms is not None:
            return self.tcp.ms
        return self.http.ms if self.http else None

    @property
    def bitrate_kbps(self) -> float | None:
        """Лучшая доступная оценка битрейта: реальный сегмент → ffprobe → BANDWIDTH варианта."""
        if self.segment and self.segment.bitrate_kbps:
            return self.segment.bitrate_kbps
        if self.probe and self.probe.ok:
            if self.probe.video_bitrate_kbps:
                return self.probe.video_bitrate_kbps
            if self.probe.total_bitrate_kbps:
                return self.probe.total_bitrate_kbps
        if self.variant and self.variant.bandwidth:
            return self.variant.bandwidth / 1000
        return None

    def finalize(self, started: float) -> CheckResult:
        if self.fail_reasons:
            self.health = Health.FAIL
        elif self.degrade_reasons:
            self.health = Health.DEGRADED
        else:
            self.health = Health.OK
        self.duration_ms = (time.perf_counter() - started) * 1000
        return self


class StreamChecker:
    def __init__(self, name: str, url: str, client: httpx.AsyncClient, guard: HostGuard,
                 one_shot: bool = False) -> None:
        self.name = name
        self.url = url
        self.client = client
        self.guard = guard
        self.one_shot = one_shot
        self.check_no = 0
        # состояние между проверками
        self.known_ips: set[str] = set()
        self.tls_last: TlsResult | None = None
        self.tls_last_ts = 0.0
        self.seen_fingerprints: set[str] = set()
        self.seen_issuers: set[str] = set()
        self.resolved: Resolved | None = None
        self.hls_media_url = ""
        self.hls_last_index: int | None = None
        self.hls_last_change = 0.0
        self.last_probe: ProbeResult | None = None
        self.last_media: MediaAnalysis | None = None
        self.media_counters = {"black": 0, "frozen": 0, "silent": 0, "quiet": 0}

    # ------------------------------------------------------------------ main
    async def run(self, full: bool = False) -> CheckResult:
        started = time.perf_counter()
        self.check_no += 1
        res = CheckResult(ts=time.time(), url=self.url)
        try:
            parsed = parse_url(self.url)
        except UrlValidationError as exc:
            res.fail(str(exc))
            return res.finalize(started)

        # 1. DNS
        res.dns = await dns_check(parsed.host)
        if res.dns.error:
            res.fail(res.dns.error)
            return res.finalize(started)
        self._check_ip_change(res, parsed.host)

        # 2. TCP (SRT работает по UDP — TCP не проверяем)
        if parsed.scheme != "srt":
            res.tcp = await tcp_check(parsed.host, parsed.port)
            if res.tcp.error:
                res.fail(res.tcp.error)
                return res.finalize(started)
            if res.tcp.ms and res.tcp.ms > settings.thresholds.tcp_latency_warn_ms:
                res.degrade(f"Yüksək TCP gecikməsi: {res.tcp.ms:.0f} ms")

        # 3. TLS
        if parsed.scheme in ("https", "rtmps"):
            await self._check_tls(res, parsed.host, parsed.port, full)

        # 4. Поток
        if parsed.scheme in ("http", "https"):
            await self._check_http(res, parsed.host, full)
        else:
            res.kind = parsed.scheme
            res.media_url = self.url
            await self._probe_and_analyze(res, full, headers=None, liveness=True)
        return res.finalize(started)

    # ------------------------------------------------------------------ network
    def _check_ip_change(self, res: CheckResult, host: str) -> None:
        ips = set(res.dns.ips if res.dns else [])
        if not ips:
            return
        if self.known_ips and not (ips & self.known_ips) and settings.alert_on_ip_change:
            old = ", ".join(sorted(self.known_ips)[:4])
            new = ", ".join(sorted(ips)[:4])
            cdn = cdn_of(ips)
            if cdn and cdn == cdn_of(self.known_ips):
                res.info.append(f"{cdn} IP ünvanını dəyişdi ({old} → {new}) — CDN-in normal balanslaşdırması")
            else:
                res.notices.append(Notice(
                    key=f"ip:{','.join(sorted(ips))}", type="IP_CHANGED", severity="WARNING",
                    text=f"{host} hostunun IP ünvanı dəyişdi: {old} → {new}",
                ))
        self.known_ips |= ips
        if len(self.known_ips) > 64:
            self.known_ips = ips

    async def _check_tls(self, res: CheckResult, host: str, port: int, full: bool) -> None:
        now = time.monotonic()
        if full or self.tls_last is None or now - self.tls_last_ts >= settings.tls_check_every_sec:
            tls = await tls_check(host, port)
            self.tls_last, self.tls_last_ts = tls, now
            if not tls.error:
                th = settings.thresholds.tls_expiry_warn_days
                if tls.days_left is not None and tls.days_left < th:
                    sev = "CRITICAL" if tls.days_left < 3 else "WARNING"
                    res.notices.append(Notice(
                        key="tls_expiry", type="TLS_EXPIRING", severity=sev,
                        text=f"{host} sertifikatının müddəti {tls.days_left:.1f} gün sonra bitir",
                    ))
                if self.seen_issuers and tls.issuer not in self.seen_issuers:
                    res.notices.append(Notice(
                        key=f"tls_issuer:{tls.issuer}", type="TLS_ISSUER_CHANGED", severity="WARNING",
                        text=f"{host} sertifikatını verən dəyişdi: {tls.issuer}",
                    ))
                elif self.seen_fingerprints and tls.fingerprint not in self.seen_fingerprints:
                    res.notices.append(Notice(
                        key=f"tls_fp:{tls.fingerprint}", type="TLS_CERT_CHANGED", severity="INFO",
                        text=f"{host} üçün yeni sertifikat (SHA256 {tls.fingerprint[:16]}…)",
                    ))
                self.seen_issuers.add(tls.issuer)
                self.seen_fingerprints.add(tls.fingerprint)
        res.tls = self.tls_last
        if res.tls and res.tls.error:
            res.degrade(res.tls.error)

    def _check_redirects(self, res: CheckResult, origin_host: str, http: HttpResult) -> None:
        for _, target in http.redirects:
            thost = host_of(target)
            if thost and not is_trusted_redirect(origin_host, thost):
                res.notices.append(Notice(
                    key=f"redirect:{thost}", type="REDIRECT_UNKNOWN", severity="WARNING",
                    text=f"Naməlum domenə yönləndirmə: {thost}",
                ))

    # ------------------------------------------------------------------ http
    async def _check_http(self, res: CheckResult, host: str, full: bool) -> None:
        fetch_url, headers = self.url, None
        if platforms.is_yoda(host):
            # Yoda (Flussonic) отдаёт поток только с токеном зрителя с yoda.az/tv
            token = await platforms.yoda_token(self.client)
            if token is None:
                res.fail("Yoda: yoda.az/tv səhifəsindən giriş tokeni alınmadı")
                return
            fetch_url, headers = platforms.with_token(self.url, token), platforms.yoda_headers()

        res.http = http = await http_fetch(self.client, fetch_url, self.guard, headers=headers)
        if headers and platforms.is_yoda(host) and http.status in (401, 403):
            # 403 может быть из-за протухшего/чужого токена: берём свежий и повторяем один раз,
            # чтобы не объявлять канал упавшим из-за токена
            fresh = await platforms.yoda_token(self.client, force=True)
            if fresh and fresh != token:
                res.info.append("Yoda tokeni yeniləndi (köhnə token 403 verdi)")
                fetch_url = platforms.with_token(self.url, fresh)
                res.http = http = await http_fetch(self.client, fetch_url, self.guard, headers=headers)
        self._check_redirects(res, host, http)
        if http.error:
            res.fail(http.error)
            return
        path = http.final_url.split("?")[0].lower()
        status = http.status or 0

        if hls.is_m3u8(http.body, http.content_type, http.final_url):
            res.kind = "hls"
            if status >= 400:
                if headers and status in (401, 403):
                    platforms.invalidate_yoda_token()  # вдруг токен протух — в след. раз возьмём новый
                    res.fail(f"Yoda pleylistində HTTP {status} — kanal platformada əlçatan deyil")
                else:
                    res.fail(f"Pleylistdə HTTP {status}")
                return
            await self._check_hls(res, http.final_url, http.body, headers=headers, full=full)
            return

        if path.endswith(_DIRECT_EXT) or http.content_type.startswith(_STREAM_CT):
            res.kind = "direct"
            if status >= 400:
                res.fail(f"Axında HTTP {status}")
                return
            res.media_url = http.final_url
            await self._probe_and_analyze(res, full, headers=None, liveness=True)
            return

        # Иначе — страница платформы: получаем прямую ссылку (API платформы / yt-dlp / плеер на странице)
        res.kind = "page"
        if platforms.is_bot_wall(status, http.body):
            # Антибот-защита не пускает автоматические запросы: эфир за ней проверить нельзя,
            # но и считать его упавшим нет оснований
            res.degrade(f"{host} Cloudflare bot-yoxlaması ilə qorunur (HTTP {status}) — "
                        "yayımı avtomatik yoxlamaq mümkün deyil, səhifəni brauzerdə açın")
            return
        if status >= 400:
            res.info.append(f"Səhifə HTTP {status} qaytardı")
        await self._check_page(res, full, status)

    async def _resolve_page(self, res: CheckResult) -> Resolved:
        """Прямая ссылка на поток со страницы: API платформы → yt-dlp → код страницы → плеер в iframe."""
        slug = platforms.kick_slug(self.url)
        if slug:
            return await platforms.kick_resolve(self.client, slug)
        rs = await resolve_stream(self.url)
        if rs.ok or not (res.http and res.http.body):
            return rs
        # yt-dlp не знает сайт — ищем поток в коде страницы, затем внутри плеера (iframe)
        pages = [(res.http.final_url, res.http.body)]
        for frame in platforms.player_iframes(res.http.body, res.http.final_url):
            try:
                await self.guard.check_host(parse_url(frame).host)
            except UrlValidationError:
                continue
            fr = await http_fetch(self.client, frame, self.guard,
                                  headers={"User-Agent": platforms.YODA_UA, "Referer": res.http.final_url})
            if not fr.error and (fr.status or 0) < 400:
                pages.append((fr.final_url, fr.body))
        for page_url, body in pages:
            found = platforms.player_sources(body, page_url) or hls.find_playlist_urls(body, page_url)
            if not found:
                continue
            src = found[0]
            via = [page_url] if page_url != res.http.final_url else []
            if "youtube.com/" in src:
                # сайт встраивает YouTube — проверяем сам эфир на YouTube
                yt = await resolve_stream(src)
                yt.via = via + [src]
                if yt.ok:
                    res.info.append(f"{host_of(res.http.final_url)} YouTube pleyerini göstərir — "
                                    "yoxlanılan YouTube efiridir")
                return yt
            where = "pleyerin (iframe)" if via else "səhifənin"
            res.info.append(f"yt-dlp bu saytı dəstəkləmir — axın {where} kodunda tapıldı")
            return Resolved(ok=True, url=src, extractor=f"{host_of(page_url)} pleyeri",
                            title=src, resolved_at=time.time(), via=via + [src])
        return rs

    async def _check_page(self, res: CheckResult, full: bool, page_status: int) -> None:
        rs = self.resolved
        fresh = (
            rs is not None and rs.ok
            and time.time() - rs.resolved_at < (rs.ttl or settings.resolve_ttl_sec)
        )
        if full or not fresh:
            self.resolved = await self._resolve_page(res)
        rs = res.resolved = self.resolved
        if rs is None or not rs.ok:
            prefix = f"Səhifədə HTTP {page_status}; " if page_status >= 400 else ""
            res.fail(prefix + (rs.error if rs else "yt-dlp: nəticə yoxdur"))
            return
        if not rs.is_live:
            status = "" if rs.live_status == "not_live" else \
                f" ({LIVE_STATUS_NAME.get(rs.live_status, rs.live_status)})"
            res.fail(f"{rs.extractor or 'Platforma'}: hazırda efir yoxdur{status}")
            if not rs.ttl:
                self.resolved = None  # yt-dlp: перепроверяем каждый раз; API-платформы — по своему ttl
            return
        try:
            await self.guard.check_host(parse_url(rs.url).host)
        except UrlValidationError as exc:
            res.fail(f"Birbaşa link rədd edildi: {exc}")
            return

        headers = rs.headers or None
        if hls.is_m3u8(b"", "", rs.url) or "m3u8" in rs.url:
            pl_http = await http_fetch(self.client, rs.url, self.guard, headers=headers)
            if pl_http.error or (pl_http.status or 0) >= 400:
                self.resolved = None  # ссылка могла протухнуть — перерезолвим в следующий раз
                problem = pl_http.error or f"HTTP {pl_http.status}"
                if rs.via:
                    # сайт сам не вещает, а показывает чужой поток — называем источник
                    res.fail(f"Pleyerin mənbəyi {rs.url.split('?')[0]} cavab vermir: {problem}")
                else:
                    res.fail(f"Platformanın pleylistində {problem}")
                return
            await self._check_hls(res, pl_http.final_url, pl_http.body, headers=headers, full=full)
        else:
            res.media_url = rs.url
            await self._probe_and_analyze(res, full, headers=headers, liveness=True)
            if res.fail_reasons:
                self.resolved = None

    # ------------------------------------------------------------------ hls
    async def _fetch_media_playlist(
        self, url: str, body: bytes, headers: dict[str, str] | None, res: CheckResult
    ) -> tuple[str, hls.Playlist] | None:
        pl = hls.parse_playlist(body.decode("utf-8", "replace"), url)
        if not pl.is_master:
            return url, pl
        variant = hls.pick_variant(pl)
        if variant is None:
            res.fail("Master-pleylistdə variant yoxdur")
            return None
        res.variant = variant
        try:
            await self.guard.check_host(parse_url(variant.url).host)
        except UrlValidationError as exc:
            res.fail(f"Pleylist variantı rədd edildi: {exc}")
            return None
        h2 = await http_fetch(self.client, variant.url, self.guard, headers=headers)
        if h2.error:
            res.fail(f"Media-pleylist: {h2.error}")
            return None
        if (h2.status or 0) >= 400:
            res.fail(f"Media-pleylistdə HTTP {h2.status}")
            return None
        return h2.final_url, hls.parse_playlist(h2.body.decode("utf-8", "replace"), h2.final_url)

    async def _check_hls(self, res: CheckResult, url: str, body: bytes,
                         headers: dict[str, str] | None, full: bool) -> None:
        res.kind = "hls"
        got = await self._fetch_media_playlist(url, body, headers, res)
        if got is None:
            return
        media_url, pl = got
        res.media_url = media_url

        if pl.ended:
            res.fail("Pleylist bitib (#EXT-X-ENDLIST) — yayım sona çatıb")
            return
        if not pl.segments:
            res.fail("Pleylistdə seqment yoxdur")
            return

        # Разовая проверка: ждём один сегмент и смотрим, растёт ли плейлист
        if self.one_shot:
            first_index = pl.last_index
            await asyncio.sleep(min(max(pl.target_duration, 2.0), 8.0) + 0.5)
            again = await http_fetch(self.client, media_url, self.guard, headers=headers)
            if again.error or (again.status or 0) >= 400:
                res.fail(again.error or f"Pleylistin təkrar sorğusunda HTTP {again.status}")
                return
            pl = hls.parse_playlist(again.body.decode("utf-8", "replace"), again.final_url)
            if pl.last_index <= first_index:
                res.fail(f"Pleylist yenilənmir (media sequence {pl.media_sequence})")
        else:
            self._track_sequence(res, media_url, pl)

        # Пропущенные/дырявые сегменты
        if pl.gaps:
            res.degrade(f"{pl.gaps} seqment #EXT-X-GAP ilə işarələnib (mənbədə boşluqlar)")

        # Задержка эфира по PROGRAM-DATE-TIME
        live_latency = None
        last = pl.segments[-1] if pl.segments else None
        if last and last.program_date_time:
            # отрицательное значение = расхождение часов сервера и источника
            live_latency = max(0.0, time.time() - (last.program_date_time + last.duration))
            if live_latency > settings.thresholds.hls_max_live_latency_sec:
                res.degrade(f"Yayım gecikməsi {live_latency:.0f} san (PROGRAM-DATE-TIME üzrə)")

        res.playlist = {
            "media_sequence": pl.media_sequence,
            "last_index": pl.last_index,
            "target_duration": pl.target_duration,
            "segments": len(pl.segments),
            "discontinuities": pl.discontinuities,
            "age_sec": 0.0 if self.one_shot else time.monotonic() - self.hls_last_change,
            "live_latency_sec": live_latency,
        }

        # Последний сегмент: доступность, скорость, реальный битрейт
        if last and (full or self.one_shot or self.check_no % settings.segment_every == 1
                     or settings.segment_every <= 1):
            seg = await hls.fetch_segment(self.client, last, self.guard, headers)
            res.segment = seg
            if seg.error:
                res.fail(seg.error)
                return
            if seg.total_ms and last.duration > 0:
                ratio = seg.total_ms / 1000 / last.duration
                if ratio > settings.thresholds.segment_slow_ratio:
                    res.degrade(
                        f"Seqment yavaş yüklənir: {last.duration:.1f} san video {seg.total_ms / 1000:.1f} san-yə"
                    )

        if not res.fail_reasons:
            await self._probe_and_analyze(res, full, headers=headers, liveness=False)

    def _track_sequence(self, res: CheckResult, media_url: str, pl: hls.Playlist) -> None:
        """Свежесть плейлиста: номер последнего сегмента должен расти."""
        now = time.monotonic()
        if media_url.split("?")[0] != self.hls_media_url.split("?")[0]:
            self.hls_media_url, self.hls_last_index, self.hls_last_change = media_url, None, now
        prev = self.hls_last_index
        if prev is None or pl.last_index != prev:
            if prev is not None and pl.last_index < prev:
                res.notices.append(Notice(
                    key="hls_reset", type="HLS_SEQUENCE_RESET", severity="INFO",
                    text=f"Media sequence sıfırlandı ({prev} → {pl.last_index}) — enkoder yenidən başladılıb?",
                ))
            elif prev is not None and pl.media_sequence > prev + 1:
                res.info.append(
                    f"Yoxlamalar arasında {pl.media_sequence - prev - 1} seqment buraxıldı "
                    f"(pleylist pəncərəsi intervaldan qısadır)"
                )
            self.hls_last_index = pl.last_index
            self.hls_last_change = now
        age = now - self.hls_last_change
        th = settings.thresholds
        stale_after = max(th.hls_stale_min_sec, th.hls_stale_factor * (pl.target_duration or 6))
        if age > stale_after:
            res.fail(f"Pleylist {age:.0f} san-dir yenilənmir (media sequence {pl.media_sequence})")

    # ------------------------------------------------------------------ media
    async def _probe_and_analyze(self, res: CheckResult, full: bool,
                                 headers: dict[str, str] | None, liveness: bool) -> None:
        media_url = res.media_url
        audio_url = res.variant.audio_url if res.variant else ""
        if audio_url:
            try:
                await self.guard.check_host(parse_url(audio_url).host)
            except UrlValidationError as exc:
                res.info.append(f"Audio trek rədd edildi: {exc}")
                audio_url = ""
        lp = self.last_probe
        due = (full or liveness or lp is None or not lp.ok
               or self.check_no % max(1, settings.ffprobe_every) == 0)
        if due:
            self.last_probe = await ffprobe(media_url, headers, audio_url)
        else:
            res.probe_cached = True
        probe = res.probe = self.last_probe
        if probe is None:
            return
        if not probe.ok:
            if probe.error and "tapılmadı" in probe.error:
                res.info.append(probe.error)  # инструмент не установлен — это не падение эфира
                return
            res.fail(probe.error or "ffprobe: axın oxunmur")
            return

        th = settings.thresholds
        if probe.has_video:
            if probe.fps and probe.fps < th.min_fps:
                res.degrade(f"Aşağı FPS: {probe.fps:g} (hədd {th.min_fps:g})")
            if probe.height and probe.height < th.min_height:
                res.degrade(f"Aşağı keyfiyyət: {probe.width}x{probe.height} (hədd {th.min_height}p)")
        bitrate = res.bitrate_kbps
        if bitrate is not None and bitrate < th.min_video_bitrate_kbps:
            res.degrade(f"Aşağı bitreyt: {bitrate:.0f} kbit/s (hədd {th.min_video_bitrate_kbps})")
        if th.require_audio and not probe.has_audio:
            res.degrade("Audio trek yoxdur")

        # Тяжёлый анализ (black/freeze/silence) — раз в N проверок или пока проблема не снята
        pending = any(self.media_counters.values())
        if full or pending or self.check_no == 1 or self.check_no % max(1, settings.analyze_every) == 0:
            media = await analyze_media(media_url, probe.has_video, probe.has_audio, headers, audio_url)
            self.last_media = media
            if media.ok:
                quiet = (media.mean_volume_db is not None and not media.silent
                         and media.mean_volume_db < th.low_volume_db)
                for key, flag in (("black", media.black), ("frozen", media.frozen),
                                  ("silent", media.silent), ("quiet", quiet)):
                    self.media_counters[key] = self.media_counters[key] + 1 if flag else 0
            elif media.error:
                res.info.append(media.error)
        res.media = self.last_media

        confirm = 1 if self.one_shot else max(1, settings.media_confirm)
        m = self.last_media
        if self.media_counters["black"] >= confirm and m:
            res.degrade(f"Qara ekran ({m.sample_sec:g} san nümunədən {m.black_sec:.1f} san)")
        if self.media_counters["frozen"] >= confirm and m:
            res.degrade(f"Donmuş görüntü ({m.sample_sec:g} san-dən {m.freeze_sec:.1f} san)")
        if self.media_counters["silent"] >= confirm and m:
            # тишина в эфире по умолчанию критична: эфир считается упавшим (SILENCE_CRITICAL)
            (res.fail if settings.silence_critical else res.degrade)(f"Audioda səssizlik ({m.silence_sec:.1f} san, orta {m.mean_volume_db} dB)")
        if self.media_counters["quiet"] >= confirm and m:
            res.degrade(f"Səs çox zəifdir: orta {m.mean_volume_db} dB")
