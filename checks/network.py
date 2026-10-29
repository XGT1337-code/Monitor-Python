"""Сетевые проверки: DNS, TCP, HTTP (с ручными редиректами), TLS-сертификат."""
from __future__ import annotations

import asyncio
import hashlib
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

from checks.validators import HostGuard, UrlValidationError, parse_url, resolve_host
from config import settings

USER_AGENT = "Mozilla/5.0 (StreamMonitor/1.0; +https://t.me/Alert_Checker_RealTime_bot)"
# Типы контента «бесконечных» потоков — читаем только начало
_STREAM_CT = ("video/", "audio/", "application/octet-stream", "flv", "mp2t")


@dataclass
class DnsResult:
    ips: list[str] = field(default_factory=list)
    ms: float | None = None
    error: str | None = None


@dataclass
class TcpResult:
    ms: float | None = None
    error: str | None = None


@dataclass
class HttpResult:
    status: int | None = None
    final_url: str = ""
    redirects: list[tuple[int, str]] = field(default_factory=list)
    content_type: str = ""
    body: bytes = b""
    ms: float | None = None
    error: str | None = None
    blocked: str | None = None     # редирект заблокирован SSRF-фильтром


@dataclass
class TlsResult:
    not_after: float | None = None
    days_left: float | None = None
    issuer: str = ""
    subject: str = ""
    fingerprint: str = ""
    error: str | None = None


def make_http_client() -> httpx.AsyncClient:
    """Общий HTTP-клиент: без автоследования редиректов (проверяем каждый хоп сами)."""
    return httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(settings.timeouts.http),
        headers={"User-Agent": USER_AGENT},
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )


async def dns_check(host: str) -> DnsResult:
    start = time.perf_counter()
    try:
        ips = await resolve_host(host)
        return DnsResult(ips=ips, ms=(time.perf_counter() - start) * 1000)
    except asyncio.TimeoutError:
        return DnsResult(error=f"DNS: {host} həll edilmədi (vaxt bitdi)")
    except OSError as exc:
        return DnsResult(error=f"DNS: {host} tapılmadı ({exc.strerror or exc})")


async def tcp_check(host: str, port: int) -> TcpResult:
    start = time.perf_counter()
    writer = None
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), settings.timeouts.tcp)
        return TcpResult(ms=(time.perf_counter() - start) * 1000)
    except asyncio.TimeoutError:
        return TcpResult(error=f"TCP: {host}:{port} qoşulma vaxtı bitdi")
    except OSError as exc:
        return TcpResult(error=f"TCP: {host}:{port} əlçatan deyil ({exc.strerror or exc})")
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except Exception:  # noqa: BLE001
                pass


async def http_fetch(
    client: httpx.AsyncClient,
    url: str,
    guard: HostGuard,
    *,
    headers: dict[str, str] | None = None,
    max_body: int = 2 * 1024 * 1024,
    max_redirects: int = 5,
) -> HttpResult:
    """GET с ручной обработкой редиректов: каждый хоп проходит SSRF-проверку."""
    result = HttpResult(final_url=url)
    start = time.perf_counter()
    current = url
    try:
        for _ in range(max_redirects + 1):
            async with client.stream("GET", current, headers=headers) as resp:
                if resp.is_redirect and "location" in resp.headers:
                    target = urljoin(current, resp.headers["location"])
                    result.redirects.append((resp.status_code, target))
                    try:
                        parsed = parse_url(target)
                        await guard.check_host(parsed.host)
                    except UrlValidationError as exc:
                        result.blocked = str(exc)
                        result.status = resp.status_code
                        result.error = f"Yönləndirmə bloklandı: {exc}"
                        return result
                    current = target
                    continue
                result.status = resp.status_code
                result.final_url = current
                result.content_type = resp.headers.get("content-type", "").lower()
                limit = 64 * 1024 if result.content_type.startswith(_STREAM_CT) else max_body
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= limit:
                        break
                result.body = b"".join(chunks)[:limit]
                result.ms = (time.perf_counter() - start) * 1000
                return result
        result.error = f"HTTP: həddən çox yönləndirmə (> {max_redirects})"
    except httpx.TimeoutException:
        result.error = f"HTTP: vaxt bitdi ({settings.timeouts.http:.0f} san)"
    except httpx.HTTPError as exc:
        result.error = f"HTTP: {type(exc).__name__}: {exc}"[:300]
    except ssl.SSLError as exc:
        result.error = f"TLS: {exc}"[:300]
    result.ms = (time.perf_counter() - start) * 1000
    return result


def _x509_name(parts: tuple) -> str:
    """Человекочитаемое имя из структуры getpeercert()."""
    flat = {k: v for rdn in parts for (k, v) in rdn}
    return ", ".join(f"{k}={flat[k]}" for k in ("commonName", "organizationName") if k in flat) or str(flat)


async def tls_check(host: str, port: int = 443) -> TlsResult:
    ctx = ssl.create_default_context()
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=host), settings.timeouts.tls
        )
        sslobj = writer.get_extra_info("ssl_object")
        der = sslobj.getpeercert(binary_form=True)
        info = sslobj.getpeercert()
        not_after = ssl.cert_time_to_seconds(info["notAfter"])
        return TlsResult(
            not_after=not_after,
            days_left=(not_after - time.time()) / 86400,
            issuer=_x509_name(info.get("issuer", ())),
            subject=_x509_name(info.get("subject", ())),
            fingerprint=hashlib.sha256(der).hexdigest()[:32],
        )
    except ssl.SSLCertVerificationError as exc:
        return TlsResult(error=f"TLS: sertifikat yoxlamadan keçmədi ({exc.verify_message})")
    except asyncio.TimeoutError:
        return TlsResult(error="TLS: əl sıxma vaxtı bitdi")
    except (OSError, ssl.SSLError, KeyError, ValueError) as exc:
        return TlsResult(error=f"TLS: {exc}"[:300])
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except Exception:  # noqa: BLE001
                pass


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
