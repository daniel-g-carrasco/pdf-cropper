"""The preferences: an INI file in the per-user configuration directory."""

from __future__ import annotations

import configparser
import os
import sys
from pathlib import Path

from pdf_cropper.core.margins import DEFAULT_MARGIN_MM


def config_dir() -> Path:
    """Per-user configuration directory, following platform conventions:
    $XDG_CONFIG_HOME on Linux (Flatpak points it inside the sandbox),
    %LOCALAPPDATA% on Windows."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pdf-cropper"


class Settings:
    """Tiny persistent key/value store (settings.ini). No file means defaults."""

    DEFAULTS = {
        "language": "auto",            # auto (desktop language) | it | en
        "color_scheme": "auto",        # auto (follow the system) | light | dark
        # Windows: the system title bar (and so Snap) rather than GTK's
        "system_title_bar": "true",
        "margin_mm": str(DEFAULT_MARGIN_MM),  # white border left around the content
        "check_updates": "true",       # Windows: look for a new release daily
        "last_update_check": "",       # ISO date of the last automatic check
    }

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else config_dir() / "settings.ini"
        self._cp = configparser.ConfigParser()
        self._cp["general"] = dict(self.DEFAULTS)
        try:
            self._cp.read(self.path, encoding="utf-8")
        except (OSError, configparser.Error):
            pass

    def get(self, key: str) -> str:
        return self._cp["general"].get(key, self.DEFAULTS.get(key, ""))

    def get_bool(self, key: str) -> bool:
        return self.get(key).strip().lower() in ("1", "true", "yes", "on")

    def set(self, key: str, value) -> None:
        if isinstance(value, bool):
            value = "true" if value else "false"
        self._cp["general"][key] = str(value)
        self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                self._cp.write(f)
        except OSError:
            pass  # preferences are a convenience, never fatal
