"""Адаптеры платформ: токены доступа к потоку и данные, которые показывает сама платформа.

Yoda.az (str*.yodacdn.net, Flussonic): плеер берёт токен зрителя из атрибута
data-token на странице https://yoda.az/tv/. Токен привязан к IP и User-Agent
и живёт около часа — получаем его так же, как обычный браузер, и кэшируем.

Kick (kick.com/<канал>): статус эфира, зрители, название, категория и ссылка
на поток берутся из публичного API канала.

Сайты-агрегаторы (canlitv.com и т.п.) держат плеер в iframe: заходим в него
и достаём источник (JW Player `file:`, hls.js, <video src>, YouTube embed).
"""
from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from checks.resolver import Resolved
from config import settings

log = logging.getLogger(__name__)

# User-Agent, под который сайт выписывает токен (должен совпадать при запросе потока)
YODA_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
YODA_PAGE = "https://yoda.az/tv/"
_TOKEN_RE = re.compile(r'data-token="([^"]+)"')

_yoda_token: str | None = None
_yoda_exp: float = 0.0
_yoda_fetched: float = 0.0


def is_yoda(host: str) -> bool:
    return host == "yodacdn.net" or host.endswith(".yodacdn.net")


def yoda_headers() -> dict[str, str]:
    return {"User-Agent": YODA_UA, "Referer": "https://yoda.az/", "Origin": "https://yoda.az"}


def _token_exp(token: str) -> float:
    """Срок действия из полезной нагрузки токена (base64-JSON до точки)."""
    try:
        raw = token.split(".")[0]
        raw += "=" * (-len(raw) % 4)
        return float(json.loads(base64.urlsafe_b64decode(raw)).get("exp", 0))
    except (ValueError, json.JSONDecodeError):
        return time.time() + 600


async def yoda_token(client: httpx.AsyncClient, force: bool = False) -> str | None:
    """Токен зрителя Yoda (кэш до истечения срока минус 5 минут)."""
    global _yoda_token, _yoda_exp, _yoda_fetched
    now = time.time()
    if not force and _yoda_token and now < _yoda_exp - 300:
        return _yoda_token
    if _yoda_token and now - _yoda_fetched < 60:
        return _yoda_token  # не дёргаем сайт чаще раза в минуту
    _yoda_fetched = now
    try:
        # параметр r — как обновление страницы в браузере: иначе кэш отдаёт чужой токен
        resp = await client.get(YODA_PAGE, params={"r": secrets.randbelow(10**9)},
                                headers={"User-Agent": YODA_UA}, timeout=settings.timeouts.http,
                                follow_redirects=True)
        match = _TOKEN_RE.search(resp.text)
    except httpx.HTTPError as exc:
        log.warning("Yoda: не удалось открыть страницу плеера: %s", type(exc).__name__)
        return None
    if not match:
        log.warning("Yoda: на странице плеера нет data-token (HTTP %s)", resp.status_code)
        return None
    _yoda_token = match.group(1)
    _yoda_exp = _token_exp(_yoda_token)
    return _yoda_token


def invalidate_yoda_token() -> None:
    """Пометить токен устаревшим (обновится при следующем запросе, но не чаще раза в минуту)."""
    global _yoda_exp
    _yoda_exp = 0.0


def with_token(url: str, token: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "token"] + [("token", token)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


# ---------------------------------------------------------------- Cloudflare
_CF_MARKERS = (b"Just a moment...", b"Attention Required! | Cloudflare", b"cf-browser-verification",
               b"challenge-platform", b"cf_chl_opt")


def is_bot_wall(status: int | None, body: bytes) -> bool:
    """Страница-заглушка антибот-защиты (Cloudflare): сам эфир за ней не виден."""
    return (status or 0) in (403, 429, 503) and any(m in body[:20000] for m in _CF_MARKERS)


# ---------------------------------------------------------------- Kick
def kick_slug(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in ("kick.com", "www.kick.com"):
        return None
    slug = parts.path.strip("/").split("/")[0]
    return slug.lower() if re.fullmatch(r"[\w-]{2,64}", slug or "") else None


def _kick_time(raw: object) -> float | None:
    """start_time Kick — UTC без зоны ("2026-10-12 08:00:00")."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


async def kick_resolve(client: httpx.AsyncClient, slug: str) -> Resolved:
    """Статус канала Kick из публичного API: эфир/офлайн, зрители, название, ссылка на поток."""
    api = f"https://kick.com/api/v2/channels/{slug}"
    try:
        resp = await client.get(api, headers={"User-Agent": YODA_UA, "Accept": "application/json"},
                                timeout=settings.timeouts.http, follow_redirects=True)
    except httpx.HTTPError as exc:
        return Resolved(error=f"Kick API: {type(exc).__name__}")
    if resp.status_code == 404:
        return Resolved(error=f"Kick: «{slug}» kanalı tapılmadı")
    if resp.status_code >= 400 or "json" not in resp.headers.get("content-type", ""):
        return Resolved(error=f"Kick API: HTTP {resp.status_code}")
    try:
        ch = resp.json()
    except ValueError:
        return Resolved(error="Kick API: yanlış cavab")

    live = ch.get("livestream") or None
    user = ch.get("user") or {}
    rs = Resolved(
        ok=bool(ch.get("playback_url")),
        url=str(ch.get("playback_url") or ""),
        headers={"User-Agent": YODA_UA, "Origin": "https://kick.com", "Referer": "https://kick.com/"},
        extractor="Kick",
        channel=str(user.get("username") or slug),
        followers=ch.get("followers_count") if isinstance(ch.get("followers_count"), int) else None,
        live_status="is_live" if live and live.get("is_live", True) else "not_live",
        resolved_at=time.time(),
        ttl=60,  # API лёгкий — зрителей и статус обновляем раз в минуту
        error=None if ch.get("playback_url") else "Kick: kanalın yayım linki yoxdur",
    )
    if ch.get("is_banned"):
        rs.details.append("⛔ Kanal Kick tərəfindən bloklanıb")
    if live:
        rs.title = str(live.get("session_title") or "")[:200]
        rs.viewers = live.get("viewer_count") if isinstance(live.get("viewer_count"), int) else None
        rs.started_at = _kick_time(live.get("start_time") or live.get("created_at"))
        cats = [c.get("name") for c in live.get("categories") or [] if isinstance(c, dict) and c.get("name")]
        if cats:
            rs.details.append("Kateqoriya: " + ", ".join(cats[:3]))
        if live.get("language"):
            rs.details.append(f"Dil: {live['language']}")
    else:
        recent = [c.get("name") for c in ch.get("recent_categories") or [] if isinstance(c, dict)]
        if recent:
            rs.details.append("Son kateqoriya: " + ", ".join(n for n in recent[:3] if n))
    return rs


# ---------------------------------------------------------------- плеер в iframe
_IFRAME_RE = re.compile(
    r"""<iframe[^>]+?\b(?:data-)?src\s*=\s*["']([^"']+)["']"""
    r"""|getElementById\(\s*["'][^"']*["']\s*\)\.src\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_PLAYER_SRC_RE = re.compile(
    r"""(?:\bfile|\bsrc|\bsource|\bhls|\burl)\s*[:=]\s*["']([^"']+\.(?:m3u8|mp4|flv)(?:\?[^"']*)?)["']""",
    re.IGNORECASE,
)
_YT_EMBED_RE = re.compile(r"""(?:youtube(?:-nocookie)?\.com/embed/|youtu\.be/)([\w-]{11})""")
# Эфир YouTube-канала: embed/live_stream?channel=UC… или ID канала в настройках плеера
# (com.com.tr: data-kimlik="UC…")
_YT_CHANNEL_RE = re.compile(r"""(?:live_stream\?channel=|data-kimlik=["'])(UC[\w-]{22})""")
_SKIP_IFRAME = ("googletagmanager", "doubleclick", "googlesyndication", "facebook.com/plugins",
                "/online/", "recaptcha", "disqus", "about:blank")


def player_iframes(body: bytes, base_url: str) -> list[str]:
    """Ссылки на плееры, вставленные через iframe (рекламу и счётчики пропускаем)."""
    text = body.decode("utf-8", "replace")
    found: list[str] = []
    for a, b in _IFRAME_RE.findall(text):
        url = urljoin(base_url, (a or b).strip())
        if url.startswith(("http://", "https://")) and not any(s in url for s in _SKIP_IFRAME) \
                and url not in found:
            found.append(url)
    return found[:3]


def player_sources(body: bytes, base_url: str) -> list[str]:
    """Источники из настроек плеера (JW Player `file:`, video.js `src:`) и YouTube embed."""
    text = body.decode("utf-8", "replace").replace("\\/", "/")
    found = [urljoin(base_url, u) for u in _PLAYER_SRC_RE.findall(text)]
    found += [f"https://www.youtube.com/watch?v={vid}" for vid in _YT_EMBED_RE.findall(text)]
    found += [f"https://www.youtube.com/channel/{cid}/live" for cid in _YT_CHANNEL_RE.findall(text)]
    return list(dict.fromkeys(u for u in found if u.startswith(("http://", "https://"))))[:3]
