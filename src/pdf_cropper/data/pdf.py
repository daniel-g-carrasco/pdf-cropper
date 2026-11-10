"""The PDF as the engine holds it: the boxes of a page rewritten."""

from __future__ import annotations

_ERR_PASSWORD = 4              # FPDF_ERR_PASSWORD


def _apply_box(page, box) -> tuple[float, float]:
    """Set the boxes of one page to `box`. Return the page size in points as
    displayed, i.e. after the page's own rotation."""
    page.set_mediabox(*box)
    page.set_cropbox(*box)
    for name in ("bleedbox", "trimbox", "artbox"):  # only when the page defines them
        if getattr(page, f"get_{name}")(fallback_ok=False) is not None:
            getattr(page, f"set_{name}")(*box)
    size = (box[2] - box[0], box[3] - box[1])
    return size[::-1] if page.get_rotation() in (90, 270) else size
