"""A PDF on disk cropped into a copy next to it, with the PDF engine
(PDFium): each page rendered and measured, then the boxes written on a
second, never-rendered instance; and the PDF files of the folders given.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from pdf_cropper.core.margins import (
    CropError,
    CropResult,
    DEFAULT_MARGIN_MM,
    DEFAULT_THRESHOLD,
    MM,
    NothingToCrop,
    SUFFIX,
    cropped_name,
    ink_bbox,
)
from pdf_cropper.data.pdf import _ERR_PASSWORD, _apply_box
from pdf_cropper.i18n import _

_MAX_PIXELS = 16_000_000       # per rendered page: precision vs memory/time
_MAX_SCALE = 3.0               # pixels per point on small pages
_MIN_GAIN = 0.5                # points: smaller crops are not worth a new file
_CHUNK = 1 << 20               # 1 MiB: I/O granularity for progress reporting


def load_engine():
    """Import pypdfium2 (the PDFium bindings). Kept out of the module level
    so that --help, --version and the GUI start-up do not pay for it."""
    try:
        import pypdfium2
    except (ImportError, OSError) as e:
        raise CropError(_("the PDF engine is not available ({error}); "
                          "install it with: pip install pypdfium2").format(error=e)) from None
    return pypdfium2


def _measure_page(page, margin: float, threshold: int):
    """Find the box around the ink of one page, `margin` points away.
    Return (left, bottom, right, top) in PDF user space, or None when the
    page is to be left alone (blank, or nothing to gain)."""
    x0, y0, x1, y1 = page.get_bbox()  # MediaBox ∩ CropBox: what viewers show
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        return None
    rotation = page.get_rotation()
    scale = min(_MAX_SCALE, math.sqrt(_MAX_PIXELS / (width * height)))
    # Counter-rotate: the bitmap is then laid out like the unrotated page,
    # whose coordinate system is the one the boxes are expressed in.
    bitmap = page.render(scale=scale, rotation=(360 - rotation) % 360, grayscale=True)
    try:
        w, h = bitmap.width, bitmap.height
        ink = ink_bbox(bitmap.buffer, w, h, bitmap.stride, threshold)
    finally:
        bitmap.close()
    if ink is None:
        return None
    sx, sy = w / width, h / height  # the bitmap size is rounded up: not exactly `scale`
    left = max(x0, x0 + ink[0] / sx - margin)
    right = min(x1, x0 + ink[2] / sx + margin)
    top = min(y1, y1 - ink[1] / sy + margin)
    bottom = max(y0, y1 - ink[3] / sy - margin)
    if max(left - x0, x1 - right, y1 - top, bottom - y0) < _MIN_GAIN:
        return None
    return left, bottom, right, top


def crop_file(src: Path, margin_mm: float = DEFAULT_MARGIN_MM, overwrite: bool = False,
              threshold: int = DEFAULT_THRESHOLD, progress=None) -> CropResult:
    """Crop the white margins of src into <name>_cropped.pdf next to it.

    progress(fraction), if given, is called with values from 0.0 to 1.0
    while the source is read, its pages are analysed and the output is
    written: large files on network shares can take a while.
    Raises FileExistsError when the output exists and overwrite is False,
    NothingToCrop when no page has margins to remove, CropError for
    unsupported/corrupt input, OSError on I/O problems.
    """
    src = Path(src)
    dest = cropped_name(src)
    if dest.exists() and not overwrite:
        raise FileExistsError(str(dest))
    pdfium = load_engine()

    def report(fraction):
        if progress:
            progress(fraction)

    # Read the file ourselves: progress on slow shares, no file kept locked,
    # and no path-encoding surprises inside the C library.
    size = max(src.stat().st_size, 1)
    raw = bytearray()
    with open(src, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            raw += chunk
            report(0.1 * min(len(raw), size) / size)

    data = bytes(raw)
    del raw
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        if getattr(e, "err_code", None) == _ERR_PASSWORD:
            raise CropError(_("the PDF is password-protected")) from None
        raise CropError(_("not a readable PDF document")) from None

    # Pass 1: measure. Rendering is not read-only: PDFium generates an
    # appearance stream for every annotation that lacks one and adds it to
    # the document (AutoCAD drawings are full of them; saved after rendering,
    # they came out up to 80% larger). Hence the boxes are written (pass 2)
    # on a second, never-rendered instance of the same bytes.
    boxes = {}
    try:
        pages = len(pdf)
        for index in range(pages):
            page = pdf[index]
            try:
                box = _measure_page(page, margin_mm * MM, threshold)
            finally:
                page.close()
            if box is not None:
                boxes[index] = box
            report(0.1 + 0.8 * (index + 1) / pages)
    except pdfium.PdfiumError as e:
        raise CropError(str(e)) from None
    finally:
        pdf.close()
    if not boxes:
        raise NothingToCrop(str(src))

    # Pass 2: write.
    tmp = dest.with_name(dest.name + ".part")
    first_size = None
    pdf = pdfium.PdfDocument(data)
    try:
        for index, box in boxes.items():
            page = pdf[index]
            try:
                shown = _apply_box(page, box)
            finally:
                page.close()
            first_size = first_size or shown
        with open(tmp, "wb") as out:
            pdf.save(out)
        os.replace(tmp, dest)
    except pdfium.PdfiumError as e:
        raise CropError(str(e)) from None
    finally:
        pdf.close()
        try:
            tmp.unlink()
        except OSError:
            pass
    report(1.0)
    return CropResult(dest, pages, len(boxes), first_size[0] / MM, first_size[1] / MM)


def iter_pdf(paths) -> list[Path]:
    """Expand files/folders into a flat list of PDF files (folders: recursive).

    Inside folders the copies this tool wrote (*_cropped.pdf) are left out,
    so the same folder can be processed again without cropping the crops.
    Files named explicitly are always taken."""
    found: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            found.extend(sorted(
                x for x in p.rglob("*")
                if x.is_file() and x.suffix.lower() == ".pdf"
                and not x.stem.lower().endswith(SUFFIX)
            ))
        elif p.is_file():
            found.append(p)
    return found
