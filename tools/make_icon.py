#!/usr/bin/env python3
"""Generate the raster application icons from the scalable one (PyGObject with
GdkPixbuf and librsvg, which GTK brings, draws the SVG; Pillow writes the
installer bitmaps):

  data/icons/hicolor/<N>x<N>/apps/<APP_ID>.png  icon-theme PNGs (64, 128, 256)
  assets/icon.ico, assets/icon.png              Windows exe / installer icon
  installer/wizard-small-<dpi>.bmp              Inno Setup wizard header image
  installer/wizard-large-<dpi>.bmp              Inno Setup finish-page image

The master is data/icons/hicolor/scalable/apps/<APP_ID>.svg, exported as a
plain SVG from the Inkscape master assets/icona.svg: edit that, export, run
this. Design: the family's square canvas with blue and yellow scissors cutting a white piece of paper; flat shapes, the light on the left and the shadow on the
right, on a canvas that stands 480 of 512.

The theme's PNGs keep the margin the SVG has, as Linux expects. Windows shrinks
the icon to 16-24 pixels in the taskbar, where a margin is only a smaller
icon: the .ico is drawn with the view closed on the canvas (FULL_VIEW), and
for 16, 20 and 24 pixels from assets/icon-small.svg, drawn on the 16 pixel grid.
"""

import io
import struct
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402
from PIL import Image  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_ID = "io.github.daniel_g_carrasco.pdf-cropper"
SVG = ROOT / "data" / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg"
SMALL_SVG = ROOT / "assets" / "icon-small.svg"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SMALL = (16, 20, 24)
FULL_VIEW = ('viewBox="0 0 512 512"', 'viewBox="16 16 480 480"')
WHITE = (255, 255, 255, 255)


def png(svg_text: str, size: int) -> bytes:
    """The SVG drawn on a transparent square of `size` pixels, as PNG bytes."""
    loader = GdkPixbuf.PixbufLoader()
    loader.set_size(size, size)
    loader.write(svg_text.encode("utf-8"))
    loader.close()
    _ok, data = loader.get_pixbuf().save_to_bufferv("png", [], [])
    return bytes(data)


def image(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGBA")


def ico(images: list[tuple[int, bytes]]) -> bytes:
    """An .ico holding each (size, PNG) as it is (Windows Vista and later read PNG entries)."""
    offset = 6 + 16 * len(images)
    entries, payload = b"", b""
    for size, data in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset + len(payload))
        payload += data
    return struct.pack("<HHH", 0, 1, len(images)) + entries + payload


def main() -> None:
    text = SVG.read_text(encoding="utf-8")
    windows = text.replace(*FULL_VIEW)
    small = SMALL_SVG.read_text(encoding="utf-8") if SMALL_SVG.is_file() else None

    out = ROOT / "assets"
    out.mkdir(exist_ok=True)
    (out / "icon.ico").write_bytes(ico([(size, png(small if small and size in SMALL else windows, size))
                                        for size in SIZES]))
    image(png(windows, 256)).save(out / "icon.png")
    print(f"written: {out / 'icon.ico'} and icon.png")

    for size in (64, 128, 256):
        dest = ROOT / "data" / "icons" / "hicolor" / f"{size}x{size}" / "apps" / f"{APP_ID}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(png(text, size))
        print(f"written: {dest.relative_to(ROOT)}")

    # Inno Setup wizard bitmaps (24-bit BMP, white background), one per DPI
    # scale; the sizes are the ones documented for WizardSmallImageFile and
    # WizardImageFile. Setup picks the closest match at run time.
    master = image(png(windows, 1024))
    small_sizes = {100: (55, 58), 125: (64, 68), 150: (83, 80), 175: (92, 97),
                   200: (110, 106), 225: (119, 123), 250: (138, 140)}
    large_sizes = {100: (164, 314), 125: (192, 386), 150: (246, 459), 175: (273, 556),
                   200: (328, 604), 225: (355, 700), 250: (410, 797)}
    for kind, sizes in (("small", small_sizes), ("large", large_sizes)):
        for dpi, (w, h) in sizes.items():
            canvas = Image.new("RGBA", (w, h), WHITE)
            side = min(w, h) - 6 if kind == "small" else int(w * 0.62)
            icon = master.resize((side, side), Image.LANCZOS)
            y = (h - side) // 2 if kind == "small" else int(h * 0.18)
            canvas.alpha_composite(icon, ((w - side) // 2, y))
            dest = ROOT / "installer" / f"wizard-{kind}-{dpi}.bmp"
            canvas.convert("RGB").save(dest, format="BMP")
        print(f"written: installer/wizard-{kind}-<dpi>.bmp ({len(sizes)} sizes)")


if __name__ == "__main__":
    main()
