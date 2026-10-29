"""Опрос vMix HTTP API: http://<VMIX_HOST>:8088/api (XML).

В vMix должен быть включён Web Controller: Settings → Web Controller → Enable (порт 8088).
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

from config import settings


@dataclass
class VmixInput:
    number: str
    title: str
    type: str
    state: str          # Running / Paused / Completed
    muted: bool
    meter_db: float | None


@dataclass
class VmixStatus:
    reachable: bool = False
    error: str | None = None
    version: str = ""
    edition: str = ""
    streaming: bool | None = None
    stream_channels: dict[str, bool] = field(default_factory=dict)   # channel1..3 (vMix 24+)
    recording: bool | None = None
    external: bool | None = None
    fade_to_black: bool | None = None
    master_muted: bool | None = None
    master_volume: float | None = None
    master_db: float | None = None       # уровень мастер-выхода (по метрам vMix), dB
    inputs: list[VmixInput] = field(default_factory=list)
    active: VmixInput | None = None

    @property
    def inputs_count(self) -> int:
        return len(self.inputs)

    @property
    def active_input(self) -> str:
        return f"{self.active.number}: {self.active.title}" if self.active else ""


def _flag(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() == "true"


def _meter_db(*values: str | None) -> float | None:
    """Метры vMix — линейная амплитуда 0..1; переводим максимум каналов в dB."""
    nums = []
    for v in values:
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            pass
    if not nums:
        return None
    peak = max(nums)
    return -120.0 if peak <= 0 else round(20 * math.log10(peak), 1)


def parse_vmix(xml: bytes) -> VmixStatus:
    root = ET.fromstring(xml)
    st = VmixStatus(reachable=True,
                    version=(root.findtext("version") or "").strip(),
                    edition=(root.findtext("edition") or "").strip())
    streaming = root.find("streaming")
    if streaming is not None:
        st.streaming = _flag(streaming.text)
        st.stream_channels = {k: _flag(v) for k, v in streaming.attrib.items() if k.startswith("channel")}
    st.recording = _flag(root.findtext("recording"))
    st.external = _flag(root.findtext("external"))
    st.fade_to_black = _flag(root.findtext("fadeToBlack"))

    master = root.find("./audio/master")
    if master is not None:
        st.master_muted = _flag(master.get("muted"))
        try:
            st.master_volume = float(master.get("volume", ""))
        except ValueError:
            pass
        st.master_db = _meter_db(master.get("meterF1"), master.get("meterF2"))

    for inp in root.findall("./inputs/input"):
        st.inputs.append(VmixInput(
            number=inp.get("number", ""), title=inp.get("title", "") or (inp.text or "").strip(),
            type=inp.get("type", ""), state=inp.get("state", ""), muted=_flag(inp.get("muted")) or False,
            meter_db=_meter_db(inp.get("meterF1"), inp.get("meterF2")),
        ))
    active = (root.findtext("active") or "").strip()
    st.active = next((i for i in st.inputs if i.number == active), None)
    return st


async def fetch_vmix(client: httpx.AsyncClient) -> VmixStatus:
    if not settings.vmix_host:
        return VmixStatus(error="VMIX_HOST təyin edilməyib")
    url = f"http://{settings.vmix_host}:{settings.vmix_port}/api"
    try:
        resp = await client.get(url, timeout=settings.timeouts.vmix)
        resp.raise_for_status()
        if len(resp.content) > 5 * 1024 * 1024:
            return VmixStatus(error="vMix: cavab çox böyükdür")
        return parse_vmix(resp.content)
    except httpx.HTTPError as exc:
        return VmixStatus(error=f"vMix API əlçatan deyil: {type(exc).__name__}")
    except ET.ParseError:
        return VmixStatus(error="vMix API: yanlış XML")
