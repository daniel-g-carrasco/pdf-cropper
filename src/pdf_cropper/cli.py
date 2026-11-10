"""The command line: files and folders cropped without a window, the
options, the update check printed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pdf_cropper import __version__
from pdf_cropper.app.crop import crop_file, iter_pdf, load_engine
from pdf_cropper.app.updates import UpdateError, fetch_latest_release, is_newer
from pdf_cropper.core.margins import (
    CropError,
    DEFAULT_MARGIN_MM,
    DEFAULT_THRESHOLD,
    MAX_MARGIN_MM,
    NothingToCrop,
    describe,
)

from .i18n import _


def run_check_update() -> int:
    """`--check-update`: print the latest release. Exit 0 when GitHub answered
    (up to date or not), 3 when the network/TLS layer failed."""
    print(_("Installed version: {version}").format(version=__version__))
    try:
        rel = fetch_latest_release()
    except UpdateError as e:
        print(_("Update check failed: {error}").format(error=e), file=sys.stderr)
        return 0 if e.reached else 3
    if is_newer(rel["version"]):
        print(_("Available: {version}  ({url})").format(version=rel["version"], url=rel["url"]))
    else:
        print(_("Latest version: {version} (up to date)").format(version=rel["version"]))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run_cli(paths, overwrite: bool, margin_mm: float, threshold: int) -> int:
    files = iter_pdf(paths)
    if not files:
        print(_("No PDF files found."), file=sys.stderr)
        return 1
    try:
        load_engine()
    except CropError as e:
        print(f"ERR   {e}", file=sys.stderr)
        return 2
    n_ok = n_skip = n_err = 0
    for f in files:
        try:
            result = crop_file(f, margin_mm, overwrite, threshold)
            print(f"OK    {result.dest}  ({describe(result)})")
            n_ok += 1
        except FileExistsError as e:
            print(_("SKIP  {file}  (already exists: {name}; use --overwrite)").format(
                file=f, name=Path(str(e)).name))
            n_skip += 1
        except NothingToCrop:
            print(_("SKIP  {file}  (no white margins to remove)").format(file=f))
            n_skip += 1
        except (CropError, OSError) as e:
            print(f"ERR   {f}  ({e})", file=sys.stderr)
            n_err += 1
    print("\n" + _("Cropped: {ok}  Skipped: {skipped}  Errors: {errors}").format(
        ok=n_ok, skipped=n_skip, errors=n_err))
    return 0 if n_err == 0 else 1


def _margin_arg(text: str) -> float:
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        raise argparse.ArgumentTypeError(_("not a number: {value}").format(value=text)) from None
    if not 0.0 <= value <= MAX_MARGIN_MM:
        raise argparse.ArgumentTypeError(
            _("must be between 0 and {max}").format(max=int(MAX_MARGIN_MM)))
    return value


def _threshold_arg(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(_("not a number: {value}").format(value=text)) from None
    if not 1 <= value <= 255:
        raise argparse.ArgumentTypeError(_("must be between 1 and 255"))
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf-cropper",
        description=_("Crop the white margins of PDF pages, many files at once."),
        epilog=_("Without arguments the graphical interface (GTK 4) starts. "
                 "Examples: pdf-cropper drawing.pdf | "
                 "pdf-cropper --margin 5 --overwrite folder/"),
    )
    parser.add_argument("paths", nargs="*", metavar=_("FILE_OR_FOLDER"),
                        help=_("PDF files or folders to scan (recursive)"))
    parser.add_argument("--margin", type=_margin_arg, default=None, metavar="MM",
                        help=_("white border to leave around the content, in "
                               "millimetres (default: {default})").format(
                                   default=DEFAULT_MARGIN_MM))
    parser.add_argument("--threshold", type=_threshold_arg, default=DEFAULT_THRESHOLD,
                        metavar="N",
                        help=_("grey level (1-255) below which a pixel counts as "
                               "content; lower it for scans with a dirty "
                               "background (default: {default})").format(
                                   default=DEFAULT_THRESHOLD))
    parser.add_argument("--overwrite", action="store_true",
                        help=_("overwrite existing cropped files"))
    parser.add_argument("--gui", action="store_true",
                        help=_("force the graphical interface"))
    parser.add_argument("--check-update", action="store_true",
                        help=_("check whether a newer version exists and exit"))
    parser.add_argument("--register", action="store_true",
                        help=_("(Windows) add the entry to the File Explorer "
                               "context menu for the current user and exit"))
    parser.add_argument("--unregister", action="store_true",
                        help=_("(Windows) remove the context-menu entry and exit"))
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    return parser
