"""Валидация URL и защита от SSRF.

Разрешены только схемы из settings.allowed_schemes. Хосты, резолвящиеся в
приватные/loopback/link-local/зарезервированные адреса, запрещены — кроме VMIX_HOST.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from config import settings

DEFAULT_PORTS = {"http": 80, "https": 443, "rtmp": 1935, "rtmps": 443, "srt": 9000}
MAX_URL_LEN = 2048


class UrlValidationError(ValueError):
    """URL не прошёл проверку (сообщение безопасно показывать пользователю)."""


@dataclass(frozen=True)
class ParsedUrl:
    url: str
    scheme: str
    host: str
    port: int
    path: str


def parse_url(url: str) -> ParsedUrl:
    """Синтаксическая проверка URL без сетевых запросов."""
    url = (url or "").strip()
    if not url or len(url) > MAX_URL_LEN:
        raise UrlValidationError("URL boşdur və ya çox uzundur")
    if any(ch in url for ch in ("\r", "\n", "\t", " ")):
        raise UrlValidationError("URL-də icazəsiz simvollar var")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UrlValidationError(f"Yanlış URL: {exc}") from None
    scheme = parts.scheme.lower()
    if scheme not in settings.allowed_schemes:
        raise UrlValidationError(
            f"«{scheme or '—'}» sxeminə icazə yoxdur. İcazəlilər: {', '.join(settings.allowed_schemes)}"
        )
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UrlValidationError("URL-də host yoxdur")
    return ParsedUrl(url=url, scheme=scheme, host=host, port=port or DEFAULT_PORTS[scheme], path=parts.path)


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified or not ip.is_global
    )


async def resolve_host(host: str, timeout: float | None = None) -> list[str]:
    """Асинхронный DNS-резолв с таймаутом. Возвращает отсортированный список IP."""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(
        loop.getaddrinfo(host, None, type=socket.SOCK_STREAM),
        timeout or settings.timeouts.dns,
    )
    return sorted({info[4][0] for info in infos})


class HostGuard:
    """Проверка хостов на SSRF с кэшем (чтобы не резолвить на каждой проверке)."""

    _CACHE_TTL = 300

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, str | None]] = {}
        self._vmix_ips: set[str] = set()

    async def _vmix_addresses(self) -> set[str]:
        if not settings.vmix_host:
            return set()
        if not self._vmix_ips:
            try:
                self._vmix_ips = set(await resolve_host(settings.vmix_host))
            except Exception:  # noqa: BLE001
                return {settings.vmix_host}
        return self._vmix_ips | {settings.vmix_host.lower()}

    async def check_host(self, host: str) -> None:
        """Бросает UrlValidationError, если хост ведёт в приватную сеть."""
        host = host.lower().rstrip(".")
        now = time.monotonic()
        cached = self._cache.get(host)
        if cached and now - cached[0] < self._CACHE_TTL:
            if cached[1]:
                raise UrlValidationError(cached[1])
            return
        error = await self._evaluate(host)
        self._cache[host] = (now, error)
        if len(self._cache) > 1000:
            self._cache.clear()
        if error:
            raise UrlValidationError(error)

    async def _evaluate(self, host: str) -> str | None:
        vmix = await self._vmix_addresses()
        if host in vmix:
            return None
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
            return "localhost/lokal şəbəkəyə giriş qadağandır"
        try:
            ips = await resolve_host(host)
        except (asyncio.TimeoutError, OSError):
            return None  # не резолвится — это проблема доступности, а не SSRF; проверки покажут DNS-ошибку
        for raw in ips:
            if raw in vmix:
                continue
            ip = ipaddress.ip_address(raw.split("%")[0])
            if _is_forbidden_ip(ip):
                return f"{host} hostu daxili ünvana ({raw}) yönəlir — qadağandır (SSRF qorunması)"
        return None

    async def validate(self, url: str) -> ParsedUrl:
        parsed = parse_url(url)
        await self.check_host(parsed.host)
        return parsed


def registrable_domain(host: str) -> str:
    """Грубое определение «базового» домена (example.com, example.com.az)."""
    labels = host.lower().rstrip(".").split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in {"com", "net", "org", "gov", "edu", "co", "ac"}:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_trusted_redirect(origin_host: str, target_host: str) -> bool:
    """Редирект считается «известным», если домен тот же или из списка доверенных."""
    target = target_host.lower()
    if registrable_domain(target) == registrable_domain(origin_host):
        return True
    return any(target == d or target.endswith("." + d) for d in settings.trusted_domains)
