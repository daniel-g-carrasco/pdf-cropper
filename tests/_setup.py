"""Shared test helpers: put src/ on sys.path, tell the platform. Every test
module (but test_crop.py, which also runs by itself in the CI and in the
Flatpak's build) starts with `import _setup`.

This file is PDF Cropper's own (it has no fingerprint). The common tests,
the copies of the model's comune/tests listed in impronte/comune.json, ask
it for ROOT, WINDOWS, PACKAGE and shared(name): they never name the
package."""

import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PACKAGE = "pdf_cropper"
WINDOWS = sys.platform == "win32"


def shared(name: str):
    """The common module of that name ("i18n", "app.updates"):
    pdf_cropper.<name>."""
    return importlib.import_module(f"{PACKAGE}.{name}")


# No test writes in the user's own folders (tests/_userfolders.py, a copy of
# the model's). They are watched as the environment names them now, before
# anything changes it: a run that writes next to the installed PDF
# Cropper's own files (its settings.ini, its errors.log) fails at its exit.
# Then the tests get folders of their own: the four XDG variables (config,
# state, data, cache) point into one temporary folder, removed at the exit
# (app/paths.py and app/config.py let them win on Windows too). The guard
# looks at folders, not at Windows' registry: no test of PDF Cropper writes
# there (the context-menu entry, app/windows_shell.py, is never registered
# by a test).
import _userfolders  # noqa: E402
from pdf_cropper import EXE_NAME, SLUG, TEXTDOMAIN, i18n  # noqa: E402

_userfolders.watch(SLUG, EXE_NAME)
_userfolders.isolate()
# the catalogues of this checkout, for the tests that read a language
# (translation_for); the messages themselves in the source strings
i18n.setup(TEXTDOMAIN, ROOT, "en")
os.environ.setdefault("LANGUAGE", "en")
