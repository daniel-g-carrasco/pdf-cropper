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
import time
from pathlib import Path

__version__ = "1.1.5"

APP_ID = "io.github.daniel_g_carrasco.pdf-cropper"
APP_NAME = "PDF Cropper"
GITHUB_REPO = "daniel-g-carrasco/pdf-cropper"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
DOWNLOADS_URL = f"https://github.com/{GITHUB_REPO}/releases/download/"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
TEXTDOMAIN = "pdf-cropper"

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
