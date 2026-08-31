from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .audio import discover_audio_sources
from .camera import Camera
from .preview import discover_virtual_camera_devices
from .theme import load_current_omarchy_palette


def state_directory() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state_home / "link-studio"


def log_path() -> Path:
    return state_directory() / "link-studio.log"


def configure_logging() -> Path:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("link_studio")
    if not root.handlers:
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    return path


def _command(arguments: list[str]) -> str:
    if shutil.which(arguments[0]) is None:
        return f"{arguments[0]} is not installed\n"
    try:
        result = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"command failed: {exc}\n"
    output = (result.stdout or "") + (result.stderr or "")
    return output.replace(str(Path.home()), "~")


def diagnostic_report(camera: Camera) -> dict[str, Any]:
    palette = load_current_omarchy_palette()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "link_studio_version": __version__,
        "python": sys.version,
        "platform": platform.platform(),
        "kernel": platform.release(),
        "device": camera.device.as_dict(),
        "camera_state": camera.read_state(),
        "audio_sources": [asdict(source) for source in discover_audio_sources()],
        "virtual_camera_devices": discover_virtual_camera_devices(),
        "omarchy_theme": palette.as_dict() if palette else None,
    }


def create_support_bundle(camera: Camera, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"link-studio-support-{datetime.now():%Y%m%d-%H%M%S}.zip"
    report = diagnostic_report(camera)
    command_outputs = {
        "v4l2-formats.txt": _command(
            ["v4l2-ctl", "--device", camera.device.path, "--list-formats-ext"]
        ),
        "usb.txt": _command(["lsusb", "-d", "2e1a:"]),
        "gstreamer.txt": _command(["gst-inspect-1.0", "--version"]),
        "pipewire.txt": _command(["pactl", "info"]),
        "orca-runtime.json": _command(["orca-ide", "status", "--json"]),
    }
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics.json", json.dumps(report, indent=2, sort_keys=True) + "\n")
        for name, content in command_outputs.items():
            archive.writestr(name, content)
        current_log = log_path()
        if current_log.is_file():
            archive.write(current_log, "link-studio.log")
        for index in range(1, 4):
            rotated = current_log.with_name(f"{current_log.name}.{index}")
            if rotated.is_file():
                archive.write(rotated, f"link-studio.log.{index}")
    return destination
