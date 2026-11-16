"""Entry point: the command line when files are given, the window otherwise."""

from __future__ import annotations

import os
import sys

from pdf_cropper.app import log
from pdf_cropper.app.config import Settings
from pdf_cropper.app.windows_shell import win_register, win_unregister
from pdf_cropper.core.margins import DEFAULT_MARGIN_MM

from . import PACKAGE_DIR, SLUG, TEXTDOMAIN, WINDOWS, _mark, i18n
from .cli import build_parser, run_check_update, run_cli
from .i18n import _

LANGUAGES = ("en", "it")        # the languages of the preference; "auto" is the desktop's


def setup_language(choice: str) -> None:
    """The catalogue for the preference (`choice`: "auto", "it" or "en"),
    before any text is built. GTK's own strings (dialog buttons, the file
    chooser) come from libintl, which honours LANGUAGE: on Windows, and
    whenever a language is chosen, it is kept in step with ours."""
    chosen = choice if choice in LANGUAGES else ""
    i18n.setup(TEXTDOMAIN, PACKAGE_DIR.parent.parent, chosen)
    if WINDOWS or chosen:
        os.environ["LANGUAGE"] = i18n.language()


def main() -> int:
    _mark("main")
    # PyInstaller --windowed builds have no console streams.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    # Redirected output uses the legacy code page on Windows: a file name or
    # a "×" it cannot encode must not abort the run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    log.setup(SLUG)
    settings = Settings()
    setup_language(settings.get("language"))
    args = build_parser().parse_args()

    if args.check_update:
        return run_check_update()
    if args.register or args.unregister:
        if sys.platform != "win32":
            print(_("Option available on Windows only."), file=sys.stderr)
            return 2
        if args.register:
            win_register()
            print(_("Context-menu entry added for the current user."))
        else:
            win_unregister()
            print(_("Context-menu entry removed for the current user."))
        return 0
    if args.paths and not args.gui:
        margin = args.margin if args.margin is not None else DEFAULT_MARGIN_MM
        return run_cli(args.paths, args.overwrite, margin, args.threshold)
    # the window's errors kept in a file too (app/errorlog.py): started from
    # the desktop or the context menu nobody reads stderr, and the Windows
    # build has none. The command line keeps speaking in its terminal.
    from pdf_cropper.app import errorlog, services
    errorlog.install(errorlog.log_file(services.app_paths().state))
    from pdf_cropper.gui.application import run_gui         # the toolkit, only when a window is wanted
    return run_gui(sys.argv, settings)


if __name__ == "__main__":
    sys.exit(main())
