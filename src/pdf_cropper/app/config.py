"""The preferences: an INI file in the per-user configuration directory."""

from __future__ import annotations

import configparser
import io
from pathlib import Path

from pdf_cropper.core.margins import DEFAULT_MARGIN_MM
from pdf_cropper.data import atomic

from . import log, services


def config_dir() -> Path:
    """Per-user configuration directory (app/services.py): %LOCALAPPDATA%
    on Windows, $XDG_CONFIG_HOME on Linux (the Flatpak points it inside
    the sandbox)."""
    return services.app_paths().config


class Settings:
    """Tiny persistent key/value store (settings.ini). No file means defaults."""

    DEFAULTS = {
        "language": "auto",            # auto (desktop language) | it | en
        "color_scheme": "auto",        # auto (follow the system) | light | dark
        # Windows: the system title bar (and so Snap) rather than GTK's
        "system_title_bar": "true",
        "margin_mm": str(DEFAULT_MARGIN_MM),  # white border left around the content
        "check_updates": "true",       # Windows: look for a new release, every 4 hours at most
        "last_update_check": "",       # when the last automatic check was made (updates.check_stamp)
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
        """The file written whole or not at all (data/atomic.py: a temporary
        file renamed over it, the rename tried again for a moment when
        Windows refuses it, an antivirus or the indexer holding the file
        just written). A failure is said on stderr (errors.log), never fatal:
        the preferences are a convenience."""
        text = io.StringIO()
        self._cp.write(text)
        try:
            atomic.write_bytes(self.path, text.getvalue().encode("utf-8"))
        except OSError as error:
            log.complain("saving the preferences", error)

    # what the troubleshooting text of the About window says (app/diagnostics.py)
    notices = ()

    @property
    def theme(self) -> str:
        return self.get("color_scheme")

    @property
    def language(self) -> str:
        return self.get("language")

    @property
    def check_updates(self) -> bool:
        return self.get_bool("check_updates")
