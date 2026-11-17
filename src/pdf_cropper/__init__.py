"""PDF Cropper: trim the white margins of PDF pages, many files at once.

Every page is rendered off-screen with PDFium (through pypdfium2), the area
actually covered by ink is measured, and the page boxes (MediaBox, CropBox)
are shrunk around it, leaving a small margin. The page content itself is not
touched: vectors stay vectors, text stays text. The result is written next
to the source as <name>_cropped.pdf; the original is never modified.

Run with file/folder arguments for CLI mode, or without arguments for the
GTK 4 GUI.

The package is in four layers, and the imports go one way only (tests/
test_strati.py holds them to it): core/ works things out (the box around
the ink, the margin, the names) and data/ writes the PDF's page boxes,
both plain Python that know nobody; app/ is the services without a window
(a file cropped with the PDF engine, settings, updates, Windows' registry)
and joins the two; gui/ is the window, the one place GTK comes in. The
constants below are the app's identity.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

__version__ = "1.1.8"

WINDOWS = sys.platform == "win32"

APP_ID = "io.github.daniel_g_carrasco.pdf-cropper"
APP_NAME = "PDF Cropper"
SLUG = "pdf-cropper"                # the repository, the binary on Linux, the file names of a release
EXE_NAME = "pdf-cropper"            # the Windows executable and its folder under %LOCALAPPDATA%
TEXTDOMAIN = SLUG
ENV_PREFIX = "PDF_CROPPER"          # the developer aids: PDF_CROPPER_STARTUP_LOG and the like
GITHUB_REPO = "daniel-g-carrasco/pdf-cropper"
WEBSITE = f"https://github.com/{GITHUB_REPO}"
# The releases' signing key (minisign), the one shared by the author's apps
# (key id CEFFF9269F08F845). The secret half is the CI's MINISIGN_SECRET_KEY;
# the tests check that the release workflow verifies with this very key.
PUBLIC_KEY = "RWRF+AifJvn/zhz0o7MBObIYHgw9+sWoc4GaBK+qYnJY0AqeJm540y0a"

# Windows shell integration identifier (see the "Windows" section below and
# installer/pdf-cropper.iss, which must stay in sync with it).
WIN_VERB = "PDFCropper.crop"

# the one anchor for files found from the code's own place (a checkout's
# data/, locale/, bin/): never a chain of .parent in a module, which a move breaks
PACKAGE_DIR = Path(__file__).resolve().parent


def _mark(label: str) -> None:
    """Start-up profiling aid: append a timestamp to $PDF_CROPPER_STARTUP_LOG."""
    path = os.environ.get("PDF_CROPPER_STARTUP_LOG")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{time.time():.3f} {label}\n")
        except OSError:
            pass
