"""Форматирование сообщений (HTML parse_mode). Всё пользовательское экранируется.

Тексты для пользователя — на азербайджанском.
"""
from __future__ import annotations

import html
from datetime import datetime

from checks.diagnostics import LIVE_STATUS_NAME, CheckResult, Health
from checks.resolver import Resolved

SEVERITY_ICON = {"INFO": "🔵", "WARNING": "🟡", "CRITICAL": "🔴"}
STATE_ICON = {"UP": "🟢", "RECOVERED": "🟢", "DEGRADED": "🟡", "DOWN": "🔴", "UNKNOWN": "⚪"}
# Названия состояний для пользователя
STATE_NAME = {"UP": "İŞLƏYİR", "RECOVERED": "BƏRPA OLUNDU", "DEGRADED": "KEYFİYYƏT PROBLEMİ",
              "DOWN": "DAYANIB", "UNKNOWN": "YOXLANILIR"}
HEALTH_ICON = {Health.OK: "🟢", Health.DEGRADED: "🟡", Health.FAIL: "🔴"}
HEALTH_NAME = {Health.OK: "QAYDASINDADIR", Health.DEGRADED: "KEYFİYYƏT PROBLEMİ", Health.FAIL: "İŞLƏMİR"}
KIND_NAME = {"hls": "HLS", "page": "səhifə", "direct": "birbaşa axın", "rtmp": "RTMP", "rtmps": "RTMPS",
             "srt": "SRT", "unknown": "naməlum"}
TG_LIMIT = 4000


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def fmt_time(ts: float | None, with_date: bool = False) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M:%S" if with_date else "%H:%M:%S")


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d} gün")
    if h:
        parts.append(f"{h} saat")
    if m:
        parts.append(f"{m} dəq")
    if s or not parts:
        parts.append(f"{s} san")
    return " ".join(parts[:3])


def chunks(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Разбивает длинный текст по строкам под лимит Telegram."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


def short_url(url: str, limit: int = 70) -> str:
    return url if len(url) <= limit else url[: limit - 1] + "…"


def fmt_count(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.1f}K" if n >= 10_000 else str(n)


def render_platform(rs: Resolved, now: float) -> list[str]:
    """Что показывает сама платформа: статус эфира, зрители, название, канал и т.д."""
    status = LIVE_STATUS_NAME.get(rs.live_status, rs.live_status)
    lines = [f"\n<b>📺 Platforma</b>: {esc(rs.extractor or '—')} · {esc(status)}"]
    if rs.channel:
        lines.append(f"Kanal: {esc(rs.channel)}"
                     + (f" · {fmt_count(rs.followers)} izləyici" if rs.followers is not None else ""))
    if rs.title and rs.title != rs.url:
        lines.append(f"Efir: {esc(rs.title)}")
    if rs.viewers is not None:
        lines.append(f"👁 İndi baxır: <b>{rs.viewers}</b>")
    if rs.started_at:
        lines.append(f"Efir başlayıb: {fmt_time(rs.started_at, True)} ({fmt_duration(now - rs.started_at)} əvvəl)")
    lines += [esc(d) for d in rs.details]
    if rs.via:
        lines.append("Mənbə: " + " → ".join(f"<code>{esc(short_url(u.split('?')[0], 80))}</code>" for u in rs.via))
    if rs.resolved_at and now - rs.resolved_at > 90:
        lines.append(f"<i>platforma məlumatı {fmt_time(rs.resolved_at)} vəziyyətinə</i>")
    return lines


def render_check(r: CheckResult, title: str = "") -> str:
    """Подробный отчёт о проверке (для /check и /status)."""
    lines = [f"{HEALTH_ICON[r.health]} <b>{esc(title or 'Diaqnostika')}</b> — {HEALTH_NAME[r.health]}",
             f"<code>{esc(short_url(r.url, 120))}</code>",
             f"Növ: {esc(KIND_NAME.get(r.kind, r.kind))} · yoxlama {r.duration_ms / 1000:.1f} san · {fmt_time(r.ts)}"]
    if r.fail_reasons:
        lines.append("\n<b>❌ Problemlər:</b>")
        lines += [f"• {esc(x)}" for x in r.fail_reasons]
    if r.degrade_reasons:
        lines.append("\n<b>⚠️ Keyfiyyət problemləri:</b>")
        lines += [f"• {esc(x)}" for x in r.degrade_reasons]

    lines.append("\n<b>🌐 Şəbəkə</b>")
    if r.dns:
        lines.append(f"DNS: {esc(', '.join(r.dns.ips[:4]) or r.dns.error)}"
                     + (f" ({r.dns.ms:.0f} ms)" if r.dns.ms is not None else ""))
    if r.tcp:
        lines.append(f"TCP: {f'{r.tcp.ms:.0f} ms' if r.tcp.ms is not None else esc(r.tcp.error)}")
    if r.http:
        h = r.http
        status = f"HTTP {h.status}" if h.status else "HTTP —"
        lines.append(f"{status}" + (f" · {h.ms:.0f} ms" if h.ms else "")
                     + (f" · {esc(h.content_type.split(';')[0])}" if h.content_type else ""))
        for code, target in h.redirects:
            lines.append(f"↪ {code} → <code>{esc(short_url(target))}</code>")
    if r.tls:
        t = r.tls
        if t.error:
            lines.append(f"TLS: {esc(t.error)}")
        else:
            lines.append(f"TLS: {fmt_time(t.not_after, True)} tarixinədək ({t.days_left:.0f} gün) · {esc(t.issuer)}")

    if r.resolved and (r.resolved.ok or r.resolved.channel):
        lines += render_platform(r.resolved, r.ts)

    if r.playlist:
        p = r.playlist
        lines.append("\n<b>📃 HLS</b>")
        if r.variant:
            lines.append(f"Variant: {esc(r.variant.resolution or '?')} · {r.variant.bandwidth / 1000:.0f} kbit/s")
        lines.append(f"Media sequence: {p['media_sequence']} · seqment sayı {p['segments']} · "
                     f"target {p['target_duration']:g} san")
        if p.get("age_sec"):
            lines.append(f"Pleylist {p['age_sec']:.0f} san əvvəl yenilənib")
        if p.get("live_latency_sec") is not None:
            lines.append(f"Yayım gecikməsi: {p['live_latency_sec']:.1f} san")
        if p.get("discontinuities"):
            lines.append(f"Kəsintilər (DISCONTINUITY): {p['discontinuities']}")
    if r.segment and not r.segment.error:
        s = r.segment
        lines.append(f"Seqment: {s.bytes / 1024:.0f} KB, {(s.total_ms or 0) / 1000:.2f} san-yə "
                     f"(TTFB {s.ttfb_ms or 0:.0f} ms)"
                     + (f" · {s.bitrate_kbps:.0f} kbit/s" if s.bitrate_kbps else ""))

    if r.probe and r.probe.ok:
        p = r.probe
        lines.append("\n<b>🎬 Axın</b>" + (" (keş)" if r.probe_cached else ""))
        if p.has_video:
            lines.append(f"Video: {esc(p.video_codec)} {p.width}x{p.height} · {p.fps or '?'} fps")
        else:
            lines.append("Video: yoxdur")
        if p.has_audio:
            lines.append(f"Audio: {esc(p.audio_codec)} · {p.audio_channels or '?'} kanal · "
                         f"{p.audio_sample_rate or '?'} Hz")
        else:
            lines.append("Audio: yoxdur")
        br = r.bitrate_kbps
        if br:
            lines.append(f"Bitreyt: {br:.0f} kbit/s")
    if r.media and r.media.ok:
        m = r.media
        lines.append(f"\n<b>🔍 {m.sample_sec:g} saniyəlik nümunə</b>")
        lines.append(f"Qara ekran: {m.black_sec:.1f} san · donma: {m.freeze_sec:.1f} san · "
                     f"səssizlik: {m.silence_sec:.1f} san")
        if m.mean_volume_db is not None:
            lines.append(f"Səs səviyyəsi: orta {m.mean_volume_db:.1f} dB"
                         + (f" · maks {m.max_volume_db:.1f} dB" if m.max_volume_db is not None else ""))
    if r.info:
        lines.append("\n<b>ℹ️ Qeydlər</b>")
        lines += [f"• {esc(x)}" for x in r.info]
    if r.notices:
        lines += [f"• {esc(n.text)}" for n in r.notices]
    return "\n".join(lines)
