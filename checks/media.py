"""Анализ потока через ffprobe/ffmpeg (asyncio subprocess, жёсткие таймауты)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass

from config import settings

log = logging.getLogger(__name__)

_semaphore: asyncio.Semaphore | None = None


def ffmpeg_semaphore() -> asyncio.Semaphore:
    """Ограничение числа одновременных ffmpeg/ffprobe — защита CPU сервера."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.max_ffmpeg_procs)
    return _semaphore


@dataclass
class ProcResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    not_found: bool = False


async def run_proc(args: list[str], timeout: float) -> ProcResult:
    """Запуск внешнего процесса; при таймауте процесс гарантированно убивается."""
    # Windows: без этого флага при запуске бота через pythonw (автозапуск)
    # каждый ffmpeg/ffprobe/yt-dlp открывает мелькающее чёрное окно консоли
    extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **extra,
        )
    except FileNotFoundError:
        return ProcResult(None, "", "", not_found=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        return ProcResult(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
    except asyncio.TimeoutError:
        return ProcResult(None, "", "", timed_out=True)
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except Exception:  # noqa: BLE001
                log.warning("Процесс %s не завершился после kill", args[0])


def _input_options(url: str, headers: dict[str, str] | None) -> list[str]:
    """Общие опции входа: таймаут чтения и HTTP-заголовки (для ссылок от yt-dlp)."""
    opts = ["-rw_timeout", str(int(settings.timeouts.ffprobe * 1_000_000))]
    if url.startswith(("http://", "https://")) and headers:
        ua = headers.get("User-Agent")
        if ua:
            opts += ["-user_agent", ua]
        extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k.lower() != "user-agent")
        if extra:
            opts += ["-headers", extra]
    return opts


def _fps(rate: str | None) -> float | None:
    try:
        num, den = (rate or "0/0").split("/")
        return round(int(num) / int(den), 2) if int(den) else None
    except (ValueError, ZeroDivisionError):
        return None


@dataclass
class ProbeResult:
    ok: bool = False
    error: str | None = None
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_bitrate_kbps: float | None = None
    total_bitrate_kbps: float | None = None
    has_video: bool = False
    has_audio: bool = False
    audio_codec: str | None = None
    audio_channels: int | None = None
    audio_sample_rate: int | None = None


async def ffprobe(url: str, headers: dict[str, str] | None = None, audio_url: str = "") -> ProbeResult:
    """ffprobe потока; если аудио идёт отдельной HLS-дорожкой — пробуем её тоже и объединяем."""
    res = await _ffprobe_one(url, headers)
    if not audio_url or not res.ok or res.has_audio:
        return res
    audio = await _ffprobe_one(audio_url, headers)
    if audio.ok and audio.has_audio:
        res.has_audio = True
        res.audio_codec, res.audio_channels = audio.audio_codec, audio.audio_channels
        res.audio_sample_rate = audio.audio_sample_rate
    return res


async def _ffprobe_one(url: str, headers: dict[str, str] | None = None) -> ProbeResult:
    args = [
        settings.ffprobe_path, "-v", "error", "-hide_banner",
        "-print_format", "json", "-show_streams", "-show_format",
        "-analyzeduration", "5000000", "-probesize", "5000000",
        *_input_options(url, headers), url,
    ]
    async with ffmpeg_semaphore():
        proc = await run_proc(args, settings.timeouts.ffprobe + 5)
    if proc.not_found:
        return ProbeResult(error="ffprobe tapılmadı (ffmpeg quraşdırın)")
    if proc.timed_out:
        return ProbeResult(error=f"ffprobe: {settings.timeouts.ffprobe:.0f} san vaxt bitdi — axın oxunmur")
    if proc.returncode != 0:
        msg = (proc.stderr.strip().splitlines() or ["naməlum xəta"])[-1]
        return ProbeResult(error=f"ffprobe: {msg}"[:300])
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return ProbeResult(error="ffprobe: yanlış JSON")

    res = ProbeResult(ok=True)
    fmt_br = data.get("format", {}).get("bit_rate")
    if fmt_br and str(fmt_br).isdigit():
        res.total_bitrate_kbps = int(fmt_br) / 1000
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and not res.has_video:
            res.has_video = True
            res.video_codec = st.get("codec_name")
            res.width, res.height = st.get("width"), st.get("height")
            res.fps = _fps(st.get("avg_frame_rate")) or _fps(st.get("r_frame_rate"))
            br = st.get("bit_rate")
            if br and str(br).isdigit():
                res.video_bitrate_kbps = int(br) / 1000
        elif st.get("codec_type") == "audio" and not res.has_audio:
            res.has_audio = True
            res.audio_codec = st.get("codec_name")
            res.audio_channels = st.get("channels")
            try:
                res.audio_sample_rate = int(st.get("sample_rate") or 0) or None
            except ValueError:
                pass
    if not res.has_video and not res.has_audio:
        res.ok = False
        res.error = "ffprobe: axında nə video, nə audio var"
    return res


@dataclass
class MediaAnalysis:
    ok: bool = False
    error: str | None = None
    sample_sec: float = 0.0
    black_sec: float = 0.0
    freeze_sec: float = 0.0
    silence_sec: float = 0.0
    mean_volume_db: float | None = None
    max_volume_db: float | None = None

    @property
    def black(self) -> bool:
        return self.black_sec >= settings.thresholds.black_min_duration

    @property
    def frozen(self) -> bool:
        return self.freeze_sec >= settings.thresholds.freeze_min_duration

    @property
    def silent(self) -> bool:
        return self.silence_sec >= settings.thresholds.silence_min_duration or (
            self.mean_volume_db is not None and self.mean_volume_db <= -80
        )


_BLACK_RE = re.compile(r"black_duration:\s*([\d.]+)")
_FREEZE_START_RE = re.compile(r"freeze_start:\s*([\d.]+)")
_FREEZE_DUR_RE = re.compile(r"freeze_duration:\s*([\d.]+)")
_SIL_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_DUR_RE = re.compile(r"silence_duration:\s*([\d.]+)")
_MEAN_RE = re.compile(r"mean_volume:\s*(-?[\d.]+|-inf) dB")
_MAX_RE = re.compile(r"max_volume:\s*(-?[\d.]+|-inf) dB")


def _db(match: re.Match | None) -> float | None:
    if not match:
        return None
    return -120.0 if match.group(1) == "-inf" else float(match.group(1))


async def analyze_media(
    url: str, has_video: bool, has_audio: bool, headers: dict[str, str] | None = None,
    audio_url: str = "",
) -> MediaAnalysis:
    """Сэмпл N секунд: blackdetect + freezedetect + silencedetect + volumedetect."""
    th = settings.thresholds
    sample = settings.sample_seconds
    args = [settings.ffmpeg_path, "-hide_banner", "-nostats", "-loglevel", "info",
            *_input_options(url, headers), "-t", str(sample), "-i", url]
    audio_input = "0"
    if audio_url and has_audio:
        # аудио отдельной дорожкой (YouTube и др.) — второй вход
        args += [*_input_options(audio_url, headers), "-t", str(sample), "-i", audio_url]
        audio_input = "1"
    if has_video:
        args += ["-map", "0:v:0", "-filter:v",
                 # 640px: при сильном уменьшении мелкое движение (таймкод, бегущая строка) теряется
                 f"fps=10,scale=640:-2,"
                 f"blackdetect=d={th.black_min_duration}:pix_th={th.black_pixel_threshold},"
                 f"freezedetect=n={th.freeze_noise}:d={th.freeze_min_duration}"]
    if has_audio:
        args += ["-map", f"{audio_input}:a:0", "-filter:a",
                 f"silencedetect=noise={th.silence_noise_db}dB:d={th.silence_min_duration},volumedetect"]
    if not has_video and not has_audio:
        return MediaAnalysis(error="təhlil üçün trek yoxdur")
    args += ["-f", "null", "-"]

    async with ffmpeg_semaphore():
        proc = await run_proc(args, sample + settings.timeouts.ffmpeg_extra)
    if proc.not_found:
        return MediaAnalysis(error="ffmpeg tapılmadı (ffmpeg quraşdırın)")
    if proc.timed_out:
        return MediaAnalysis(error="ffmpeg: nümunə təhlilinin vaxtı bitdi")
    err = proc.stderr
    if proc.returncode != 0:
        msg = (err.strip().splitlines() or ["xəta"])[-1]
        return MediaAnalysis(error=f"ffmpeg: {msg}"[:300])

    res = MediaAnalysis(ok=True, sample_sec=sample)
    res.black_sec = sum(float(x) for x in _BLACK_RE.findall(err))
    # Фриз/тишина могут не закончиться внутри сэмпла — тогда считаем до конца сэмпла
    freeze_durs = [float(x) for x in _FREEZE_DUR_RE.findall(err)]
    freeze_starts = [float(x) for x in _FREEZE_START_RE.findall(err)]
    res.freeze_sec = sum(freeze_durs)
    if len(freeze_starts) > len(freeze_durs):
        res.freeze_sec += max(0.0, sample - freeze_starts[-1])
    sil_durs = [float(x) for x in _SIL_DUR_RE.findall(err)]
    sil_starts = [max(0.0, float(x)) for x in _SIL_START_RE.findall(err)]
    res.silence_sec = sum(sil_durs)
    if len(sil_starts) > len(sil_durs):
        res.silence_sec += max(0.0, sample - sil_starts[-1])
    res.mean_volume_db = _db(_MEAN_RE.search(err))
    res.max_volume_db = _db(_MAX_RE.search(err))
    return res
