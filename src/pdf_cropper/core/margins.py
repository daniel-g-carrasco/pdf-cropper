"""The cropping core: the box around the ink of a rendered page, the margin
left around it, what the cropped file is called and how a result is told.
Values in, values out: no file, no PDF engine.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import NamedTuple

from pdf_cropper.i18n import _

# The white margin is found by looking at the page the way a person does:
# render it, then take the bounding box of everything that is not white. That
# is what makes it robust (white fills, clipped drawings and off-page objects
# do not count), and PDFium renders a whole A0 drawing in a fraction of a
# second. Only the page boxes are then rewritten.
SUFFIX = "_cropped"            # appended to the name of the output file
MM = 72 / 25.4                 # PDF points per millimetre
DEFAULT_MARGIN_MM = 2.0        # white border left around the content
DEFAULT_THRESHOLD = 250        # grey levels below this (0-255) count as ink
MAX_MARGIN_MM = 50.0


class CropError(ValueError):
    """The file cannot be processed: not a PDF, damaged, password-protected,
    or the PDF engine is missing."""


class NothingToCrop(Exception):
    """No page has white margins to remove (blank pages, or content that
    already reaches the page edges)."""


class CropResult(NamedTuple):
    dest: Path
    pages: int      # pages in the document
    cropped: int    # pages whose boxes were shrunk
    width: float    # size of the first cropped page as displayed, in mm
    height: float


def ink_bbox(buf, width: int, height: int, stride: int,
             threshold: int = DEFAULT_THRESHOLD):
    """Bounding box of the pixels darker than `threshold` in an 8-bit
    greyscale bitmap: (left, top, right, bottom) in pixels, right and bottom
    exclusive, or None when the bitmap is blank.

    The buffer is first mapped to one byte per pixel, 1 for ink and 0 for
    paper, so that every search below is a bytes.find() running in C."""
    ink = b"\x01"
    data = bytes(buf).translate(bytes(1 if v < threshold else 0 for v in range(256)))
    if stride != width:  # drop the row padding, whose content is undefined
        data = b"".join(data[r * stride:r * stride + width] for r in range(height))
    first = data.find(ink)
    if first < 0:
        return None
    top, bottom = first // width, data.rfind(ink) // width
    left, right = width, -1
    for row in range(top, bottom + 1):
        start = row * width
        i = data.find(ink, start, start + left)        # only left of the best so far
        if i >= 0:
            left = i - start
        j = data.rfind(ink, start + right + 1, start + width)  # only right of it
        if j >= 0:
            right = j - start
    return left, top, right + 1, bottom + 1


def cropped_name(src: Path) -> Path:
    """tavola.pdf -> tavola_cropped.pdf, next to the source."""
    src = Path(src)
    return src.with_name(src.stem + SUFFIX + (src.suffix or ".pdf"))


def describe(result: CropResult) -> str:
    """Short account of a result: the new page size, or how many pages changed."""
    if result.pages == 1:
        return _("{width} × {height} mm").format(
            width=round(result.width), height=round(result.height))
    return _("{cropped} of {pages} pages").format(
        cropped=result.cropped, pages=result.pages)


def clamp_margin(value) -> float:
    """Margin in millimetres from user input: a number between 0 and
    MAX_MARGIN_MM, the default when it cannot be read."""
    try:
        value = float(str(value).replace(",", "."))
    except ValueError:
        return DEFAULT_MARGIN_MM
    if not math.isfinite(value):
        return DEFAULT_MARGIN_MM
    return min(max(value, 0.0), MAX_MARGIN_MM)
