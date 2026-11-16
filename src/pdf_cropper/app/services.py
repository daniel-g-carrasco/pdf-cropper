"""PDF Cropper's own instances of the services that know no app.

paths, log, errorlog, updates, minisign, win32, diagnostics (and i18n at
the root, atomic in data) are the shared modules: copies, byte for byte,
of the model's comune/ (app-template), listed with their fingerprints in
impronte/comune.json and held to them by tests/test_impronte.py. They are
never edited here: a change goes to the model and comes back with its
tools/impronte.py --apply. They take a name, an id, a version as
arguments, so that the same files serve every app of mine, GTK or Qt.
This is where they meet the names in the package's __init__
(tests/test_services.py)."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from pdf_cropper import EXE_NAME, GITHUB_REPO, PUBLIC_KEY, SLUG, __version__

from . import paths


class Paths(paths.Paths):
    """The shared folders, with PDF Cropper's settings file in them: an INI
    file, settings.ini, as it has always been (app/config.py)."""

    @property
    def config_file(self) -> Path:
        return self.config / "settings.ini"


def app_paths(environ=None) -> Paths:
    """Where PDF Cropper keeps its files. On Windows the settings stay where
    they have always been, %LOCALAPPDATA%\\pdf-cropper\\settings.ini (not
    the roaming folder the shared Paths gives); the state (errors.log) is
    the shared one, %LOCALAPPDATA%\\pdf-cropper\\state. On Linux
    ~/.config/pdf-cropper and ~/.local/state/pdf-cropper (in the Flatpak,
    under ~/.var/app). The XDG variables win everywhere, for the tests."""
    environ = os.environ if environ is None else environ
    found = Paths.from_env(SLUG, EXE_NAME, environ)
    if os.name == "nt" and not environ.get("XDG_CONFIG_HOME"):
        found = dataclasses.replace(found, config=found.state.parent)
    return found


def project():
    """Whose releases an update comes from (Windows: on Linux the Flatpak
    brings the updates)."""
    from .updates import Project
    return Project(repo=GITHUB_REPO, slug=SLUG, exe_name=EXE_NAME, version=__version__, public_key=PUBLIC_KEY)
