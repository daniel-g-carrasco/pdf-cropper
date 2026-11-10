"""Localisation: GNU gettext, catalogues in po/, compiled by tools/compile_po.py.
The one module every layer may import.
"""

from __future__ import annotations

import gettext
import os
import sys
from pathlib import Path

from pdf_cropper import PACKAGE_DIR, TEXTDOMAIN

_translation: gettext.NullTranslations = gettext.NullTranslations()     # chosen by setup_i18n()


def _(message: str) -> str:
    """The message in the language in use. A function, not a name bound
    again by setup_i18n(): the modules that imported it before the
    language was chosen must speak that language too."""
    return _translation.gettext(message)


LANGUAGES = ("en", "it")


def _locale_dirs():
    base = Path(getattr(sys, "_MEIPASS", None) or PACKAGE_DIR.parent.parent)
    yield base / "share" / "locale"   # PyInstaller bundle
    yield base / "locale"             # source checkout, after tools/compile_po.py
    for prefix in ("/app", "/usr/local", "/usr"):
        yield Path(prefix) / "share" / "locale"


def detect_language() -> str:
    """Language of the user's desktop: the Windows display language, or the
    usual environment variables elsewhere. English when in doubt."""
    if sys.platform == "win32":
        try:
            import ctypes
            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
            return "it" if langid == 0x10 else "en"
        except (AttributeError, OSError):
            return "en"
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            return "it" if value.split(":")[0].lower().startswith("it") else "en"
    return "en"


def setup_i18n(choice: str = "auto") -> str:
    """Install the translation for `choice` ("auto", "it" or "en") and return
    the language in use. Must run before any user-visible string is built."""
    global _translation
    lang = choice if choice in LANGUAGES else detect_language()
    if sys.platform == "win32" or choice in LANGUAGES:
        # GTK's own strings (dialog buttons, file chooser) come from libintl,
        # which honours LANGUAGE: keep them in step with ours.
        os.environ["LANGUAGE"] = lang
    translation = gettext.NullTranslations()
    for directory in _locale_dirs():
        if gettext.find(TEXTDOMAIN, str(directory), languages=[lang]):
            translation = gettext.translation(TEXTDOMAIN, str(directory), languages=[lang])
            break
    _translation = translation
    return lang
