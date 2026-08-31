from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def default_preset_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "link-studio/presets.json"


def default_color_preset_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "link-studio/color-presets.json"


@dataclass(slots=True)
class Preset:
    name: str
    values: dict[str, Any]


class PresetStore:
    MAX_PRESETS = 10

    def __init__(self, path: Path | None = None):
        self.path = path or default_preset_path()
        self.presets: list[Preset] = []
        self.default_index: int | None = None
        self.load()

    def load(self) -> list[Preset]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self.presets = []
            self.default_index = None
            return self.presets
        if isinstance(raw, list):
            # Versions before 1.0 stored the preset array at the document root.
            items = raw
            default_index = None
        elif isinstance(raw, dict) and isinstance(raw.get("presets"), list):
            items = raw["presets"]
            candidate = raw.get("default_index")
            default_index = candidate if isinstance(candidate, int) else None
        else:
            self.presets = []
            self.default_index = None
            return self.presets
        loaded = []
        for item in items[: self.MAX_PRESETS]:
            if not isinstance(item, dict):
                continue
            name, values = item.get("name"), item.get("values")
            if isinstance(name, str) and name.strip() and isinstance(values, dict):
                loaded.append(Preset(name.strip()[:60], values))
        self.presets = loaded
        self.default_index = (
            default_index
            if default_index is not None and 0 <= default_index < len(loaded)
            else None
        )
        return self.presets

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "version": 1,
                "default_index": self.default_index,
                "presets": [asdict(preset) for preset in self.presets],
            },
            indent=2,
            sort_keys=True,
        )
        descriptor, temporary = tempfile.mkstemp(
            prefix="presets-", suffix=".json", dir=self.path.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
            temporary_path.replace(self.path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def add(self, name: str, values: dict[str, Any]) -> Preset:
        if len(self.presets) >= self.MAX_PRESETS:
            raise ValueError(f"A maximum of {self.MAX_PRESETS} scene presets is supported")
        clean_name = name.strip()[:60]
        if not clean_name:
            raise ValueError("Preset name cannot be empty")
        preset = Preset(clean_name, dict(values))
        self.presets.append(preset)
        self.save()
        return preset

    def remove(self, index: int) -> None:
        del self.presets[index]
        if self.default_index == index:
            self.default_index = None
        elif self.default_index is not None and self.default_index > index:
            self.default_index -= 1
        self.save()

    def rename(self, index: int, name: str) -> None:
        clean_name = name.strip()[:60]
        if not clean_name:
            raise ValueError("Preset name cannot be empty")
        self.presets[index].name = clean_name
        self.save()

    def update(self, index: int, values: dict[str, Any]) -> None:
        self.presets[index].values = dict(values)
        self.save()

    def set_default(self, index: int | None) -> None:
        if index is not None and not 0 <= index < len(self.presets):
            raise IndexError(index)
        self.default_index = index
        self.save()


class ColorPresetStore(PresetStore):
    MAX_PRESETS = 20

    def __init__(self, path: Path | None = None):
        super().__init__(path or default_color_preset_path())
