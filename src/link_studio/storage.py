from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def default_storage_settings_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "link-studio/storage.json"


class StorageSettings:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_storage_settings_path()
        self.screenshot_directory: Path | None = None
        self.recording_directory: Path | None = None
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        self.screenshot_directory = self._path_or_none(raw.get("screenshot_directory"))
        self.recording_directory = self._path_or_none(raw.get("recording_directory"))

    @staticmethod
    def _path_or_none(value: object) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        return Path(value).expanduser()

    def set_directory(self, kind: str, directory: Path | None) -> None:
        if kind not in {"screenshot", "recording"}:
            raise ValueError(f"unsupported output directory: {kind}")
        if directory is not None:
            directory = directory.expanduser().resolve()
        if kind == "screenshot":
            self.screenshot_directory = directory
        else:
            self.recording_directory = directory
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "screenshot_directory": (
                str(self.screenshot_directory) if self.screenshot_directory else None
            ),
            "recording_directory": (
                str(self.recording_directory) if self.recording_directory else None
            ),
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix="storage-", suffix=".json", dir=self.path.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
            temporary_path.replace(self.path)
        finally:
            temporary_path.unlink(missing_ok=True)
