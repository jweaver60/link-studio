from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any


class AudioError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AudioSource:
    name: str
    description: str
    volume_percent: int
    muted: bool
    channels: int
    sample_rate: int
    is_insta360: bool


def _run_pactl(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["pactl", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError(f"PipeWire/PulseAudio control is unavailable: {exc}") from exc
    if result.returncode:
        raise AudioError(result.stderr.strip() or f"pactl exited with {result.returncode}")
    return result.stdout


def _percent(value: Any) -> int:
    if not isinstance(value, str):
        return 100
    match = re.search(r"(\d+)%", value)
    return int(match.group(1)) if match else 100


def _sample_spec(specification: Any) -> tuple[int, int]:
    if not isinstance(specification, str):
        return 1, 48000
    channels_match = re.search(r"(\d+)ch", specification)
    rate_match = re.search(r"(\d+)Hz", specification)
    return (
        int(channels_match.group(1)) if channels_match else 1,
        int(rate_match.group(1)) if rate_match else 48000,
    )


def discover_audio_sources() -> list[AudioSource]:
    try:
        raw = json.loads(_run_pactl("-f", "json", "list", "sources"))
    except (AudioError, json.JSONDecodeError):
        return []
    sources = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or item.get("monitor_source"):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            continue
        description = item.get("description")
        description = description if isinstance(description, str) else name
        volume = item.get("volume", {})
        first_channel = next(iter(volume.values()), {}) if isinstance(volume, dict) else {}
        percent = _percent(
            first_channel.get("value_percent") if isinstance(first_channel, dict) else None
        )
        channels, rate = _sample_spec(item.get("sample_specification"))
        properties = item.get("properties", {})
        vendor_id = properties.get("device.vendor.id") if isinstance(properties, dict) else ""
        sources.append(
            AudioSource(
                name=name,
                description=description,
                volume_percent=percent,
                muted=bool(item.get("mute", False)),
                channels=channels,
                sample_rate=rate,
                is_insta360=vendor_id == "0x2e1a" or "insta360" in description.casefold(),
            )
        )
    return sources


def preferred_audio_source(sources: list[AudioSource]) -> AudioSource | None:
    return next(
        (source for source in sources if source.is_insta360), sources[0] if sources else None
    )


def set_source_volume(source_name: str, percent: int) -> int:
    if not 0 <= percent <= 150:
        raise ValueError("microphone volume must be 0..150%")
    _run_pactl("set-source-volume", source_name, f"{percent}%")
    return percent


def set_source_mute(source_name: str, muted: bool) -> bool:
    _run_pactl("set-source-mute", source_name, "1" if muted else "0")
    return muted
