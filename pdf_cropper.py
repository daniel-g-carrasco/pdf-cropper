#!/usr/bin/env python3
"""PDF Cropper — trim the white margins of PDF pages, many files at once.

Every page is rendered off-screen with PDFium (through pypdfium2), the area
actually covered by ink is measured, and the page boxes (MediaBox, CropBox)
are shrunk around it, leaving a small margin. The page content itself is not
touched: vectors stay vectors, text stays text. The result is written next
to the source as <name>_cropped.pdf; the original is never modified.

Run with file/folder arguments for CLI mode, or without arguments for the
GTK 4 GUI.
"""

from __future__ import annotations

import argparse
import configparser
import gettext
import itertools
import math
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import NamedTuple

__version__ = "1.0.0"

APP_ID = "io.github.daniel_g_carrasco.pdf-cropper"
APP_NAME = "PDF Cropper"
GITHUB_REPO = "daniel-g-carrasco/pdf-cropper"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
TEXTDOMAIN = "pdf-cropper"

# Windows shell integration identifier (see the "Windows" section below and
# installer/pdf-cropper.iss, which must stay in sync with it).
WIN_VERB = "PDFCropper.crop"

_ = gettext.NullTranslations().gettext  # rebound by setup_i18n()


def _mark(label: str) -> None:
    """Start-up profiling aid: append a timestamp to $PDF_CROPPER_STARTUP_LOG."""
    path = os.environ.get("PDF_CROPPER_STARTUP_LOG")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{time.time():.3f} {label}\n")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Localisation (GNU gettext; catalogues in po/, compiled by tools/compile_po.py)
# ---------------------------------------------------------------------------

LANGUAGES = ("en", "it")


def _locale_dirs():
    base = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)
    yield base / "share" / "locale"   # PyInstaller bundle
    yield base / "locale"             # source checkout, after tools/compile_po.py
    for prefix in ("/app", "/usr/local", "/usr"):
        yield Path(prefix) / "share" / "locale"


def detect_language() -> str:
    """Language of the user's desktop: the Windows display language, or the
    usual environment variables elsewhere. English when in doubt."""
    if sys.platform == "win32":
        try:
            import ctypes
            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
            return "it" if langid == 0x10 else "en"
        except (AttributeError, OSError):
            return "en"
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            return "it" if value.split(":")[0].lower().startswith("it") else "en"
    return "en"


def setup_i18n(choice: str = "auto") -> str:
    """Install the translation for `choice` ("auto", "it" or "en") and return
    the language in use. Must run before any user-visible string is built."""
    global _
    lang = choice if choice in LANGUAGES else detect_language()
    if sys.platform == "win32" or choice in LANGUAGES:
        # GTK's own strings (dialog buttons, file chooser) come from libintl,
        # which honours LANGUAGE: keep them in step with ours.
        os.environ["LANGUAGE"] = lang
    translation = gettext.NullTranslations()
    for directory in _locale_dirs():
        if gettext.find(TEXTDOMAIN, str(directory), languages=[lang]):
            translation = gettext.translation(TEXTDOMAIN, str(directory), languages=[lang])
            break
    _ = translation.gettext
    return lang


# ---------------------------------------------------------------------------
# Cropping core
#
# The white margin is found by looking at the page the way a person does:
# render it, then take the bounding box of everything that is not white. That
# is what makes it robust (white fills, clipped drawings and off-page objects
# do not count), and PDFium renders a whole A0 drawing in a fraction of a
# second. Only the page boxes are then rewritten.
# ---------------------------------------------------------------------------

SUFFIX = "_cropped"            # appended to the name of the output file
MM = 72 / 25.4                 # PDF points per millimetre
DEFAULT_MARGIN_MM = 2.0        # white border left around the content
DEFAULT_THRESHOLD = 250        # grey levels below this (0-255) count as ink
MAX_MARGIN_MM = 50.0

_MAX_PIXELS = 16_000_000       # per rendered page: precision vs memory/time
_MAX_SCALE = 3.0               # pixels per point on small pages
_MIN_GAIN = 0.5                # points: smaller crops are not worth a new file
_CHUNK = 1 << 20               # 1 MiB: I/O granularity for progress reporting
_ERR_PASSWORD = 4              # FPDF_ERR_PASSWORD


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


def load_engine():
    """Import pypdfium2 (the PDFium bindings). Kept out of the module level
    so that --help, --version and the GUI start-up do not pay for it."""
    try:
        import pypdfium2
    except (ImportError, OSError) as e:
        raise CropError(_("the PDF engine is not available ({error}); "
                          "install it with: pip install pypdfium2").format(error=e)) from None
    return pypdfium2


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


def cropped_name(src: Path) -> Path:
    """tavola.pdf -> tavola_cropped.pdf, next to the source."""
    src = Path(src)
    return src.with_name(src.stem + SUFFIX + (src.suffix or ".pdf"))


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


def describe(result: CropResult) -> str:
    """Short account of a result: the new page size, or how many pages changed."""
    if result.pages == 1:
        return _("{width} × {height} mm").format(
            width=round(result.width), height=round(result.height))
    return _("{cropped} of {pages} pages").format(
        cropped=result.cropped, pages=result.pages)


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


# ---------------------------------------------------------------------------
# Preferences (INI file in the per-user configuration directory)
# ---------------------------------------------------------------------------


def config_dir() -> Path:
    """Per-user configuration directory, following platform conventions:
    $XDG_CONFIG_HOME on Linux (Flatpak points it inside the sandbox),
    %LOCALAPPDATA% on Windows."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pdf-cropper"


class Settings:
    """Tiny persistent key/value store (settings.ini). No file means defaults."""

    DEFAULTS = {
        "language": "auto",            # auto (desktop language) | it | en
        "color_scheme": "auto",        # auto (follow the system) | light | dark
        "native_decorations": "true",  # Windows: system title bar instead of GTK's
        "margin_mm": str(DEFAULT_MARGIN_MM),  # white border left around the content
        "check_updates": "true",       # Windows: look for a new release daily
        "last_update_check": "",       # ISO date of the last automatic check
    }

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else config_dir() / "settings.ini"
        self._cp = configparser.ConfigParser()
        self._cp["general"] = dict(self.DEFAULTS)
        try:
            self._cp.read(self.path, encoding="utf-8")
        except (OSError, configparser.Error):
            pass

    def get(self, key: str) -> str:
        return self._cp["general"].get(key, self.DEFAULTS.get(key, ""))

    def get_bool(self, key: str) -> bool:
        return self.get(key).strip().lower() in ("1", "true", "yes", "on")

    def set(self, key: str, value) -> None:
        if isinstance(value, bool):
            value = "true" if value else "false"
        self._cp["general"][key] = str(value)
        self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                self._cp.write(f)
        except OSError:
            pass  # preferences are a convenience, never fatal


# ---------------------------------------------------------------------------
# Update check (GitHub Releases API, stdlib only)
#
# Used by the Windows builds only: on Linux updates come from the package
# manager / Flatpak (GNOME Software), as the platform conventions require.
# ---------------------------------------------------------------------------


class UpdateError(Exception):
    """Problem while talking to GitHub. `reached` tells whether the server
    answered at all (HTTP error) or the network/TLS layer failed."""

    def __init__(self, message: str, reached: bool = False):
        super().__init__(message)
        self.reached = reached


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.0' -> (1, 2, 0). Non-numeric components count as 0."""
    parts = []
    for piece in text.strip().lstrip("vV").split("."):
        m = re.match(r"\d+", piece)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts)


def is_newer(candidate: str, current: str = __version__) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_installed_build() -> bool:
    """True for the Inno Setup installation (uninstaller next to the exe),
    False for the portable zip and for source checkouts."""
    return (sys.platform == "win32" and is_frozen()
            and (Path(sys.executable).parent / "unins000.exe").is_file())


def pick_asset(assets, installed: bool):
    """Choose the release asset matching this build: installer or portable zip."""
    pattern = (r"^pdf-cropper-setup-.*-windows-x64\.exe$" if installed
               else r"^pdf-cropper-.*-windows-x64-portable\.zip$")
    for a in assets:
        if re.match(pattern, a.get("name", ""), re.IGNORECASE):
            return a
    return None


def _ssl_context():
    import ssl
    ctx = ssl.create_default_context()
    # The frozen Windows build ships a CA bundle: MSYS2's OpenSSL cannot
    # always see the Windows certificate store.
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "certs" / "ca-bundle.crt"
    if bundled.is_file():
        try:
            ctx.load_verify_locations(cafile=str(bundled))
        except (ssl.SSLError, OSError):
            pass
    return ctx


def _http_get(url: str, timeout: float = 15.0):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": f"pdf-cropper/{__version__}",
        "Accept": "application/vnd.github+json",
    })
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


def fetch_latest_release(timeout: float = 15.0) -> dict:
    """Return {"version", "tag", "url", "notes", "assets": [{"name", "url", "size"}]}."""
    import json
    import urllib.error
    try:
        with _http_get(LATEST_RELEASE_API, timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        raise UpdateError(_("GitHub answered {code}").format(code=e.code),
                          reached=True) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError(str(getattr(e, "reason", e))) from None
    tag = str(data.get("tag_name") or "")
    if not tag:
        raise UpdateError(_("unexpected answer from the server"), reached=True)
    return {
        "version": tag.lstrip("vV"),
        "tag": tag,
        "url": data.get("html_url") or RELEASES_URL,
        "notes": data.get("body") or "",
        "assets": [
            {"name": a.get("name", ""), "url": a.get("browser_download_url", ""),
             "size": int(a.get("size") or 0)}
            for a in data.get("assets", [])
        ],
    }


def download_file(url: str, dest: Path, progress=None, cancelled=None) -> Path:
    """Download url to dest. progress(done, total) is called along the way;
    cancelled is an optional threading.Event."""
    import urllib.error
    dest = Path(dest)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with _http_get(url, timeout=30) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise UpdateError(_("cancelled"), reached=True)
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        tmp.replace(dest)
        return dest
    except urllib.error.HTTPError as e:
        raise UpdateError(_("download failed ({code})").format(code=e.code),
                          reached=True) from None
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(str(getattr(e, "reason", e))) from None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


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
# Windows shell integration (per-user registry, no admin rights)
#
# One context-menu entry on PDF files. Unlike a viewer, this tool has no
# business being the default app for .pdf: nothing else is registered.
# The installer writes the same keys (HKLM when installed for all users);
# these functions cover the portable build.
# ---------------------------------------------------------------------------

_VERB_KEY = rf"Software\Classes\SystemFileAssociations\.pdf\shell\{WIN_VERB}"


def _launch_command() -> str:
    """Command line that opens files with this program (registry format)."""
    if is_frozen():
        return f'"{sys.executable}" --gui "%1"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}" --gui "%1"'


def _shell_notify() -> None:
    try:
        import ctypes
        # SHCNE_ASSOCCHANGED, SHCNF_IDLIST
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except (AttributeError, OSError):
        pass


def _reg_delete_tree(root, path: str) -> None:
    import winreg
    try:
        with winreg.OpenKey(root, path) as k:
            subs = []
            while True:
                try:
                    subs.append(winreg.EnumKey(k, len(subs)))
                except OSError:
                    break
    except FileNotFoundError:
        return
    for sub in subs:
        _reg_delete_tree(root, f"{path}\\{sub}")
    try:
        winreg.DeleteKey(root, path)
    except FileNotFoundError:
        pass


def win_register() -> None:
    """Add the context-menu verb on PDF files under HKEY_CURRENT_USER. It is
    shown whatever the default PDF app is."""
    import winreg

    def put(path, value="", name=""):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

    put(_VERB_KEY, _("Crop white margins with PDF Cropper"))
    if is_frozen():
        put(_VERB_KEY, f"{sys.executable},0", "Icon")
    put(_VERB_KEY, "Player", "MultiSelectModel")  # no 15-files prompt on multi-select
    put(rf"{_VERB_KEY}\command", _launch_command())
    _shell_notify()


def win_unregister() -> None:
    """Remove what win_register() wrote (current user only)."""
    import winreg
    _reg_delete_tree(winreg.HKEY_CURRENT_USER, _VERB_KEY)
    _shell_notify()


def win_is_registered() -> bool:
    """True when the context-menu verb exists, for this user or machine-wide."""
    import winreg
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            winreg.CloseKey(winreg.OpenKey(root, rf"{_VERB_KEY}\command"))
            return True
        except OSError:
            continue
    return False


def win_allow_foreground() -> None:
    """Let the next process that asks take the foreground. Windows grants
    that right only to the process the user just launched (us): when we hand
    our files to an already running instance, it needs it to raise its window."""
    try:
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    except (AttributeError, OSError):
        pass


def win_bring_to_front(title: str) -> None:
    """Restore (if minimized) and raise this process's top-level window
    called `title`."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):
        return
    pid = os.getpid()
    buf = ctypes.create_unicode_buffer(256)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == title:
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                return 0  # found: stop enumerating
        return 1

    user32.EnumWindows(visit, 0)


def win_dark_titlebars(dark: bool) -> None:
    """Ask DWM to paint the native title bars of this process dark or light
    (Windows 10 1809+; silently ignored elsewhere)."""
    try:
        import ctypes
        from ctypes import wintypes
        user32, dwmapi = ctypes.windll.user32, ctypes.windll.dwmapi
    except (AttributeError, OSError):
        return
    pid = os.getpid()
    value = ctypes.c_int(1 if dark else 0)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (20H1+ / 1809)
                if dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                                ctypes.sizeof(value)) == 0:
                    break
        return 1

    user32.EnumWindows(visit, 0)


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


# ---------------------------------------------------------------------------
# GTK 4 GUI
# ---------------------------------------------------------------------------

# Plain GTK 4 (Windows, or Linux without libadwaita): GTK's own named colours.
_CSS = b"""
.dropzone {
    border: 2px dashed alpha(currentColor, 0.25);
    border-radius: 12px;
}
.dropzone.hover {
    border-color: @theme_selected_bg_color;
    background: alpha(@theme_selected_bg_color, 0.08);
}
.app-banner {
    background: alpha(@theme_selected_bg_color, 0.14);
    border-radius: 8px;
    padding: 6px 6px 6px 12px;
}
"""

# libadwaita: the accent colour it exports; messages are toasts, no banner.
_CSS_ADW = b"""
.dropzone {
    border: 2px dashed alpha(currentColor, 0.25);
    border-radius: 12px;
}
.dropzone.hover {
    border-color: @accent_bg_color;
    background: alpha(@accent_bg_color, 0.08);
}
/* The drop zone is an AdwStatusPage; even "compact" it would take more than
   half of the default window, leaving the results list no room. */
.dropzone > scrolledwindow > viewport > box {
    margin: 14px 12px;
}
.dropzone .icon {
    -gtk-icon-size: 56px;
    margin-bottom: 8px;
}
.dropzone .title {
    font-size: 15pt;
}
"""


def run_gui(argv, settings: Settings) -> int:
    is_win = sys.platform == "win32"
    # Native Windows decorations (system title bar): GTK honours GTK_CSD only
    # before it initialises, hence the environment variable set up front.
    use_csd = not (is_win and settings.get_bool("native_decorations"))
    if not use_csd:
        os.environ["GTK_CSD"] = "0"
    if is_win:
        # The GL renderers spend ~1 s compiling shaders before the first
        # frame on Windows; this UI needs none of their features.
        os.environ.setdefault("GSK_RENDERER", "cairo")
    theme = None  # ThemeManager, created once GTK is up (App.do_startup)

    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk, Gio, GLib, Gtk, Pango
    except (ImportError, ValueError):
        print(
            _("GTK 4 / PyGObject not available. Install:") + "\n"
            "  Debian/Ubuntu:  sudo apt install python3-gi gir1.2-gtk-4.0 "
            "gir1.2-adw-1\n"
            "  Fedora:         sudo dnf install python3-gobject gtk4 libadwaita\n"
            "  Arch:           sudo pacman -S python-gobject gtk4 libadwaita\n"
            "  Windows(MSYS2): pacman -S mingw-w64-x86_64-gtk4 "
            "mingw-w64-x86_64-python-gobject\n"
            + _("or download the portable build from the GitHub Releases.") + "\n"
            + _("Headless use:  pdf-cropper FILE_OR_FOLDER..."),
            file=sys.stderr,
        )
        return 2

    # libadwaita is the GNOME platform library: it supplies the current HIG
    # widgets and follows the system light/dark preference by itself (GTK
    # deprecated gtk-application-prefer-dark-theme in 4.20). Linux only: the
    # Windows build stays on plain GTK, which its native decorations need.
    # PDF_CROPPER_NO_ADW=1 forces the plain GTK path (testing aid).
    Adw = None
    if not is_win and not os.environ.get("PDF_CROPPER_NO_ADW"):
        try:
            gi.require_version("Adw", "1")
            from gi.repository import Adw
            # AdwDialog (About, Preferences) needs 1.5, AdwToolbarView 1.4:
            # older libadwaita takes the plain GTK 4 path below instead.
            if (Adw.MAJOR_VERSION, Adw.MINOR_VERSION) < (1, 5):
                Adw = None
        except (ImportError, ValueError):
            Adw = None
    use_adw = Adw is not None
    _mark("gtk-imported")

    import queue
    import threading

    has_filedialog = Gtk.check_version(4, 10, 0) is None
    in_flatpak = not is_win and Path("/.flatpak-info").is_file()

    # --- small helpers ----------------------------------------------------

    def set_margins(widget, value=None, **sides):
        for side in ("top", "bottom", "start", "end"):
            v = sides.get(side, value)
            if v is not None:
                getattr(widget, f"set_margin_{side}")(v)

    def close_on_escape(window):
        ctl = Gtk.EventControllerKey()

        def on_key(_ctl, keyval, _code, _state):
            if keyval == Gdk.KEY_Escape:
                window.close()
                return True
            return False
        ctl.connect("key-pressed", on_key)
        window.add_controller(ctl)

    def init_dialog(win):
        """Common set-up of secondary windows: GTK header bar when using
        client-side decorations, Escape closes, native title bar follows the
        colour scheme on Windows."""
        if use_csd:
            win.set_titlebar(Gtk.HeaderBar())
        close_on_escape(win)
        theme.watch_window(win)

    def button_row(*buttons):
        row = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        row.set_margin_top(8)
        for b in buttons:
            row.append(b)
        return row

    def open_uri(uri, parent=None):
        if has_filedialog:  # GTK >= 4.10
            Gtk.UriLauncher.new(uri).launch(parent, None, None)
        elif is_win:
            os.startfile(uri)
        else:
            Gio.AppInfo.launch_default_for_uri(uri, None)

    def host_paths(paths):
        """Inside a Flatpak, files dropped or picked through the portals may
        arrive as /run/user/UID/doc/ID/... paths. The output is written next
        to the source, so map them back to the real location whenever the
        sandbox can write there (org.freedesktop.portal.Documents.GetHostPaths)."""
        if not in_flatpak:
            return paths
        doc_root = Path(GLib.get_user_runtime_dir()) / "doc"
        rels = {}
        for p in paths:
            try:
                rels[p] = Path(p).relative_to(doc_root)
            except ValueError:
                pass
        ids = sorted({r.parts[0] for r in rels.values() if len(r.parts) >= 2})
        if not ids:
            return paths
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            reply = bus.call_sync(
                "org.freedesktop.portal.Documents",
                "/org/freedesktop/portal/documents",
                "org.freedesktop.portal.Documents", "GetHostPaths",
                GLib.Variant("(as)", (ids,)), GLib.VariantType("(a{say})"),
                Gio.DBusCallFlags.NONE, 3000, None)
            mapping = reply.unpack()[0]
        except GLib.Error:
            return paths
        out = []
        for p in paths:
            rel = rels.get(p)
            host = mapping.get(rel.parts[0]) if rel is not None and len(rel.parts) >= 2 else None
            if host is None:
                out.append(p)
                continue
            if isinstance(host, list):
                host = bytes(host)
            real = Path(os.fsdecode(bytes(host).rstrip(b"\0"))).joinpath(*rel.parts[2:])
            target_dir = real if real.is_dir() else real.parent
            out.append(str(real) if real.exists() and os.access(target_dir, os.W_OK) else p)
        return out

    # --- colour scheme ----------------------------------------------------

    class ThemeManager:
        """Light/dark colour scheme: explicit, or automatic following the
        system. With libadwaita that is AdwStyleManager's job; on plain GTK 4
        (Windows, or Linux without libadwaita) the preference is read by hand
        from the personalization key / the freedesktop settings portal."""

        def __init__(self):
            self.dark = False
            self._portal = None
            self._poll_id = 0
            self.apply()

        def apply(self):
            mode = settings.get("color_scheme")
            if use_adw:
                # libadwaita tracks the system preference itself, settings
                # portal included: DEFAULT means "follow the system".
                manager = Adw.StyleManager.get_default()
                manager.set_color_scheme({
                    "dark": Adw.ColorScheme.FORCE_DARK,
                    "light": Adw.ColorScheme.FORCE_LIGHT,
                }.get(mode, Adw.ColorScheme.DEFAULT))
                self.dark = manager.get_dark()
                return
            if mode == "dark":
                dark = True
            elif mode == "light":
                dark = False
            else:
                dark = self._system_prefers_dark()
            self._set_dark(dark)
            if is_win:  # Windows gives no change notification here: poll cheaply
                if mode == "auto" and not self._poll_id:
                    self._poll_id = GLib.timeout_add_seconds(3, self._poll)
                elif mode != "auto" and self._poll_id:
                    GLib.source_remove(self._poll_id)
                    self._poll_id = 0

        def _poll(self):
            dark = self._system_prefers_dark()
            if dark != self.dark:
                self._set_dark(dark)
            return True  # keep polling

        def _set_dark(self, dark):
            self.dark = bool(dark)
            gtk_settings = Gtk.Settings.get_default()
            if gtk_settings is not None:
                gtk_settings.set_property("gtk-application-prefer-dark-theme", self.dark)
            if is_win:
                win_dark_titlebars(self.dark)

        def watch_window(self, win):
            """Paint the native title bar of a new window in the right shade."""
            if is_win:
                win.connect("map", lambda _w: win_dark_titlebars(self.dark))

        def _system_prefers_dark(self):
            if is_win:
                import winreg
                try:
                    with winreg.OpenKey(
                            winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                        return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
                except OSError:
                    return False
            # org.freedesktop.portal.Settings works inside and outside Flatpak
            try:
                if self._portal is None:
                    self._portal = Gio.DBusProxy.new_for_bus_sync(
                        Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
                        "org.freedesktop.portal.Desktop",
                        "/org/freedesktop/portal/desktop",
                        "org.freedesktop.portal.Settings", None)
                    self._portal.connect("g-signal", self._on_portal_signal)
                reply = self._portal.call_sync(
                    "Read",
                    GLib.Variant("(ss)", ("org.freedesktop.appearance", "color-scheme")),
                    Gio.DBusCallFlags.NONE, 1000, None)
                value = reply.unpack()[0]
                while isinstance(value, GLib.Variant):
                    value = value.unpack()
                return int(value) == 1  # 0 no preference, 1 dark, 2 light
            except (GLib.Error, TypeError, ValueError):
                return False

        def _on_portal_signal(self, _proxy, _sender, signal, params):
            if signal != "SettingChanged" or settings.get("color_scheme") != "auto":
                return
            namespace, key = params.unpack()[:2]
            if (namespace, key) == ("org.freedesktop.appearance", "color-scheme"):
                self.apply()

    # --- results list row -------------------------------------------------

    class ResultRow(Gtk.ListBoxRow):
        """One file in the results list: queued → cropping → outcome."""

        def __init__(self, src, on_reveal):
            super().__init__(activatable=False)  # activatable once cropped
            self._on_reveal = on_reveal
            self.dest = None
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            set_margins(box, top=6, bottom=6, start=10, end=10)
            self.icon = Gtk.Image.new_from_icon_name("content-loading-symbolic")
            self.icon.add_css_class("dim-label")
            self.spinner = Gtk.Spinner()
            self.stack = Gtk.Stack(valign=Gtk.Align.CENTER)
            self.stack.add_named(self.icon, "icon")
            self.stack.add_named(self.spinner, "spinner")
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                            valign=Gtk.Align.CENTER)
            name = Gtk.Label(label=src.name, xalign=0.0)
            name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            self.status = Gtk.Label(label=_("Queued"), xalign=0.0)
            self.status.set_ellipsize(Pango.EllipsizeMode.END)
            self.status.add_css_class("dim-label")
            self.bar = Gtk.ProgressBar(visible=False)
            self.bar.set_margin_top(4)
            for w in (name, self.status, self.bar):
                texts.append(w)
            self.open_btn = Gtk.Button(icon_name="folder-open-symbolic",
                                       valign=Gtk.Align.CENTER, visible=False,
                                       tooltip_text=_("Open folder"))
            self.open_btn.add_css_class("flat")
            for w in (self.stack, texts, self.open_btn):
                box.append(w)
            self.set_child(box)

        def start(self):
            self.status.set_label(_("Cropping…"))
            self.bar.set_fraction(0.0)
            self.bar.set_visible(True)
            self.spinner.start()
            self.stack.set_visible_child_name("spinner")

        def progress(self, fraction):
            self.bar.set_fraction(fraction)
            self.status.set_label(_("Cropping… {percent}%").format(
                percent=int(fraction * 100)))

        def finish(self, result, err):
            """Show the outcome; return the counter to bump (0 ok, 1 skipped, 2 error)."""
            self.spinner.stop()
            self.bar.set_visible(False)
            if err is None:
                icon_name, cls = "object-select-symbolic", None
                text, outcome = _("Cropped ({detail}) → {name}").format(
                    detail=describe(result), name=result.dest.name), 0
                self.dest = result.dest.absolute()
                self.set_activatable(True)  # double-click / Enter opens the document
                self.set_tooltip_text(str(self.dest))
                self.open_btn.set_visible(True)
                self.open_btn.connect("clicked", lambda _b: self._on_reveal(self.dest))
            elif err == "exists":
                icon_name, cls = "action-unavailable-symbolic", "dim-label"
                text, outcome = _("Skipped: the cropped file already exists (enable Overwrite)"), 1
            elif err == "nothing":
                icon_name, cls = "action-unavailable-symbolic", "dim-label"
                text, outcome = _("Skipped: no white margins to remove"), 1
            else:
                icon_name, cls = "dialog-error-symbolic", "error"
                text, outcome = _("Error: {error}").format(error=err), 2
            self.icon.set_from_icon_name(icon_name)
            self.icon.remove_css_class("dim-label")
            if cls:
                self.icon.add_css_class(cls)
            self.stack.set_visible_child_name("icon")
            self.status.set_label(text)
            return outcome

    # --- main window ------------------------------------------------------

    WindowBase = Adw.ApplicationWindow if use_adw else Gtk.ApplicationWindow

    class Window(WindowBase):
        def __init__(self, app):
            super().__init__(
                application=app, title=APP_NAME,
                default_width=680, default_height=560,
            )
            self.settings = app.settings
            self._overwrite = False
            self._margin = clamp_margin(self.settings.get("margin_mm"))
            self._jobs: queue.Queue = queue.Queue()
            self._counts = [0, 0, 0]  # ok, skipped, errors
            self._pending = 0         # batches queued or running
            self._rows = {}           # row key -> ResultRow
            self._seq = itertools.count()
            self._native = None       # keep FileChooserNative alive
            self._banner_cb = None
            self._about = None
            self.connect("map", lambda _w: _mark("window-mapped"))

            header = Adw.HeaderBar() if use_adw else Gtk.HeaderBar()
            if use_adw:
                pass  # placed by the AdwToolbarView below
            elif use_csd:
                self.set_titlebar(header)
            else:  # native title bar: the header bar becomes a plain toolbar
                header.set_show_title_buttons(False)
                header.set_title_widget(Gtk.Box())

            menu = Gio.Menu()
            if is_win:
                section = Gio.Menu()
                section.append(_("Check for updates…"), "app.check-updates")
                menu.append_section(None, section)
            section = Gio.Menu()
            section.append(_("Preferences"), "app.preferences")
            menu.append_section(None, section)
            section = Gio.Menu()
            section.append(_("About {app}").format(app=APP_NAME), "app.about")
            menu.append_section(None, section)
            self.menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                           primary=True, tooltip_text=_("Main menu (F10)"))
            header.pack_end(self.menu_btn)
            self.spinner = Gtk.Spinner(tooltip_text=_("Cropping…"))
            header.pack_end(self.spinner)
            if is_win:  # Windows habit: a tap on Alt opens the main menu (F10 in GTK)
                self._alt_solo = False
                # Key events only propagate inside the surface that receives
                # them: while the menu is open that is the popover, not the
                # window, so both need a controller (capture phase, ahead of
                # GTK's own mnemonic handling of Alt).
                for widget in (self, self.menu_btn.get_popover()):
                    keys = Gtk.EventControllerKey()
                    keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
                    keys.connect("key-pressed", self._on_key_pressed)
                    keys.connect("key-released", self._on_key_released)
                    widget.add_controller(keys)
                self.connect("notify::is-active",
                             lambda _w, _p: setattr(self, "_alt_solo", False))

            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            if use_adw:
                # HIG layout: the toolbar view carries the header bar, the page
                # lives in a toast overlay so messages arrive as toasts. The
                # 16 px page margin goes on each child here, not on the box:
                # the results card needs a scrolled window wider than itself.
                self.toasts = Adw.ToastOverlay(child=content)
                view = Adw.ToolbarView()
                view.add_top_bar(header)
                view.set_content(self.toasts)
                self.set_content(view)
            else:
                set_margins(content, 16)
                root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                if not use_csd:
                    root.append(header)
                root.append(content)
                self.set_child(root)
                # --- in-app notification banner -----------------------------
                self.banner = Gtk.Revealer(
                    transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
                bbox = Gtk.Box(spacing=12)
                bbox.add_css_class("app-banner")
                self.banner_label = Gtk.Label(hexpand=True, xalign=0.0, wrap=True)
                self.banner_button = Gtk.Button(valign=Gtk.Align.CENTER)
                self.banner_button.connect("clicked", self._on_banner_button)
                close_btn = Gtk.Button(icon_name="window-close-symbolic",
                                       valign=Gtk.Align.CENTER,
                                       tooltip_text=_("Close"))
                close_btn.add_css_class("flat")
                close_btn.connect(
                    "clicked", lambda _b: self.banner.set_reveal_child(False))
                for w in (self.banner_label, self.banner_button, close_btn):
                    bbox.append(w)
                self.banner.set_child(bbox)
                content.append(self.banner)
            theme.watch_window(self)

            # --- drop zone -------------------------------------------------
            btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                           halign=Gtk.Align.CENTER)
            b_files = Gtk.Button(label=_("Choose files…"), action_name="app.open-files")
            b_folder = Gtk.Button(label=_("Choose folder…"), action_name="app.open-folder")
            btns.append(b_files)
            btns.append(b_folder)
            if use_adw:
                # AdwStatusPage is the HIG widget for this; the "compact" style
                # keeps it from taking the whole window.
                self.dropzone = Adw.StatusPage(
                    icon_name="document-open-symbolic",
                    title=_("Drop PDF files or folders here"),
                    description=_("The cropped copy is saved next to the original"),
                    child=btns, vexpand=False)
                self.dropzone.add_css_class("compact")
            else:
                self.dropzone = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                        spacing=8)
                icon = Gtk.Image.new_from_icon_name("document-open-symbolic")
                icon.set_pixel_size(48)
                icon.set_margin_top(20)
                title = Gtk.Label(label=_("Drop PDF files or folders here"))
                title.add_css_class("title-4")
                hint = Gtk.Label(label=_("The cropped copy is saved next to the original"))
                hint.add_css_class("dim-label")
                btns.set_margin_bottom(20)
                for w in (icon, title, hint, btns):
                    self.dropzone.append(w)
            self.dropzone.add_css_class("dropzone")
            if use_adw:
                set_margins(self.dropzone, top=20, bottom=0, start=16, end=16)
            else:
                set_margins(self.dropzone, top=4, bottom=4)
            content.append(self.dropzone)

            # --- results list ---------------------------------------------
            self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
            self.listbox.connect("row-activated", self._on_row_activated)
            placeholder = Gtk.Label(label=_("Cropped files will appear here"))
            placeholder.add_css_class("dim-label")
            scrolled = Gtk.ScrolledWindow(vexpand=True, child=self.listbox)
            scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            if use_adw:
                # The HIG card (boxed-list) hugs its rows, and sits inset in a
                # scrolled window wider than itself: the viewport clips at its
                # edges, so the card's shadow needs room around it. The card
                # still lines up with the drop zone at 16 px from the window.
                self.listbox.add_css_class("boxed-list")
                self.listbox.set_valign(Gtk.Align.START)
                set_margins(self.listbox, top=4, bottom=12, start=8, end=8)
                set_margins(scrolled, start=8, end=8)
                # No card while there is nothing to show: an empty white card
                # on the window background is what looks wrong.
                placeholder.set_vexpand(True)
                set_margins(placeholder, start=16, end=16)
                self.list_stack = Gtk.Stack(
                    vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
                self.list_stack.add_named(placeholder, "empty")
                self.list_stack.add_named(scrolled, "list")
                content.append(self.list_stack)
            else:
                set_margins(placeholder, top=24, bottom=24)
                self.listbox.set_placeholder(placeholder)
                content.append(Gtk.Frame(child=scrolled))

            # --- bottom bar ------------------------------------------------
            bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            if use_adw:
                set_margins(bottom, bottom=16, start=16, end=16)
            margin_label = Gtk.Label(label=_("Margin"))
            spin = Gtk.SpinButton.new_with_range(0.0, MAX_MARGIN_MM, 0.5)
            spin.set_digits(1)
            spin.set_value(self._margin)
            spin.set_valign(Gtk.Align.CENTER)
            spin.set_tooltip_text(_("White border left around the content"))
            spin.connect("value-changed", self.on_margin_changed)
            unit = Gtk.Label(label="mm")
            unit.set_margin_end(12)
            check = Gtk.CheckButton(label=_("Overwrite existing files"))
            check.connect("toggled", self.on_overwrite_toggled)
            self.summary = Gtk.Label(label="", hexpand=True, xalign=1.0)
            self.summary.set_ellipsize(Pango.EllipsizeMode.START)
            self.summary.add_css_class("dim-label")
            for w in (margin_label, spin, unit, check, self.summary):
                bottom.append(w)
            content.append(bottom)

            # --- drag & drop ----------------------------------------------
            drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
            drop.connect("drop", self.on_drop)
            drop.connect("enter", self.on_drop_enter)
            # "motion" must keep returning COPY on every pointer move:
            # without it Windows gets DROPEFFECT_NONE mid-drag and hides
            # the drag cursor/icon while hovering the window.
            drop.connect("motion", self.on_drop_motion)
            drop.connect("leave", self.on_drop_leave)
            self.add_controller(drop)

            threading.Thread(target=self._worker, daemon=True).start()

        # --- public API used by the application ------------------------------
        def enqueue(self, paths):
            paths = [p for p in paths if p]
            if not paths:
                return
            self._pending += 1
            self.spinner.start()
            self.summary.set_label(_("Cropping…"))
            self._jobs.put(paths)

        def first_shown(self):
            """One-shot tasks after the window is on screen (Windows only:
            the daily update check)."""
            if is_win:
                today = date.today().isoformat()
                if (self.settings.get_bool("check_updates")
                        and self.settings.get("last_update_check") != today):
                    self.settings.set("last_update_check", today)
                    self.check_updates(manual=False)
            return False  # one-shot GLib.idle_add

        def show_banner(self, text, button_label=None, callback=None):
            self._banner_cb = callback
            if use_adw:
                # A toast: transient on its own, persistent when it carries an
                # action. AdwToast has no signal, hence the app action.
                toast = Adw.Toast(title=text, use_markup=False)
                if button_label:
                    toast.set_button_label(button_label)
                    toast.set_action_name("app.banner-action")
                    toast.set_timeout(0)
                self.toasts.add_toast(toast)
                return
            self.banner_label.set_label(text)
            self.banner_button.set_label(button_label or "")
            self.banner_button.set_visible(bool(button_label))
            self.banner.set_reveal_child(True)

        def run_banner_action(self):
            if self._banner_cb:
                self._banner_cb()

        def _on_banner_button(self, _btn):
            self.banner.set_reveal_child(False)
            self.run_banner_action()

        def show_about(self):
            if use_adw:  # AdwAboutDialog: the current GNOME about window
                about = Adw.AboutDialog(
                    application_name=APP_NAME, application_icon=APP_ID,
                    version=__version__,
                    comments=_("Crops the white margins of PDF pages, many "
                               "files at once."),
                    website=f"https://github.com/{GITHUB_REPO}",
                    issue_url=f"https://github.com/{GITHUB_REPO}/issues",
                    license_type=Gtk.License.MIT_X11,
                    copyright="© 2026 Daniel Grasso",
                    developer_name="Daniel Grasso",
                    developers=["Daniel Grasso"],
                )
                about.add_legal_section("PDFium, pypdfium2", None,
                                        Gtk.License.BSD_3, None)
                about.present(self)
                return
            if self._about is None:  # built once, hidden on close
                self._about = Gtk.AboutDialog(
                    transient_for=self, modal=True, hide_on_close=True,
                    program_name=APP_NAME, version=__version__,
                    comments=_("Crops the white margins of PDF pages, many "
                               "files at once."),
                    website=f"https://github.com/{GITHUB_REPO}",
                    website_label=_("Project on GitHub"),
                    license_type=Gtk.License.MIT_X11,
                    copyright="© 2026 Daniel Grasso",
                    authors=["Daniel Grasso"],
                    logo_icon_name=APP_ID,
                )
                self._about.add_credit_section(_("PDF engine"), ["PDFium (pypdfium2)"])
            self._about.present()

        # --- update check (Windows) -----------------------------------------
        def check_updates(self, manual):
            if manual:
                self.show_banner(_("Checking for updates…"))

            def work():
                try:
                    rel = fetch_latest_release()
                except UpdateError as e:
                    GLib.idle_add(self._update_result, None, str(e), manual)
                    return
                GLib.idle_add(self._update_result, rel, None, manual)
            threading.Thread(target=work, daemon=True).start()

        def _update_result(self, rel, err, manual):
            if err:
                if manual:
                    self.show_banner(_("Update check failed: {error}").format(error=err))
                return False
            if is_newer(rel["version"]):
                if manual:
                    self.banner.set_reveal_child(False)
                    UpdateDialog(self, rel).present()
                else:
                    self.show_banner(
                        _("Version {version} of {app} is available.").format(
                            version=rel["version"], app=APP_NAME),
                        _("Update…"), lambda: UpdateDialog(self, rel).present())
            elif manual:
                self.show_banner(_("{app} {version} is up to date: no newer version.").format(
                    app=APP_NAME, version=__version__))
            return False

        # --- signal handlers ----------------------------------------------
        def _on_key_pressed(self, _ctl, keyval, _code, _state):
            # remember whether Alt is being pressed on its own
            self._alt_solo = keyval in (Gdk.KEY_Alt_L, Gdk.KEY_Alt_R)
            return False

        def _on_key_released(self, _ctl, keyval, _code, _state):
            if keyval in (Gdk.KEY_Alt_L, Gdk.KEY_Alt_R) and self._alt_solo:
                self._alt_solo = False
                if self.menu_btn.get_active():
                    self.menu_btn.popdown()
                else:
                    self.menu_btn.popup()

        def on_overwrite_toggled(self, check):
            self._overwrite = check.get_active()

        def on_margin_changed(self, spin):
            self._margin = clamp_margin(spin.get_value())
            self.settings.set("margin_mm", f"{self._margin:.1f}")

        def _on_row_activated(self, _listbox, row):
            """Open the cropped document with its default application."""
            dest = getattr(row, "dest", None)
            if dest is None:
                return
            try:
                if is_win:
                    os.startfile(str(dest))
                elif has_filedialog:  # GTK >= 4.10
                    Gtk.FileLauncher.new(Gio.File.new_for_path(str(dest))).launch(
                        self, None, None)
                else:
                    Gio.AppInfo.launch_default_for_uri(dest.as_uri(), None)
            except (OSError, GLib.Error) as e:
                self.show_banner(_("Cannot open {name}: {error}").format(
                    name=dest.name, error=e))

        def on_drop_enter(self, _target, _x, _y):
            self.dropzone.add_css_class("hover")
            return Gdk.DragAction.COPY

        def on_drop_motion(self, _target, _x, _y):
            return Gdk.DragAction.COPY

        def on_drop_leave(self, _target):
            self.dropzone.remove_css_class("hover")

        def on_drop(self, _target, value, _x, _y):
            self.dropzone.remove_css_class("hover")
            self.enqueue([f.get_path() for f in value.get_files()])
            return True

        def on_pick_files(self):
            if has_filedialog:
                dlg = Gtk.FileDialog(title=_("Choose PDF files"))
                f_pdf = Gtk.FileFilter()
                f_pdf.set_name(_("PDF documents (*.pdf)"))
                f_pdf.add_pattern("*.pdf")
                f_pdf.add_pattern("*.PDF")
                f_all = Gtk.FileFilter()
                f_all.set_name(_("All files"))
                f_all.add_pattern("*")
                store = Gio.ListStore.new(Gtk.FileFilter)
                store.append(f_pdf)
                store.append(f_all)
                dlg.set_filters(store)
                dlg.set_default_filter(f_pdf)
                dlg.open_multiple(self, None, self._files_chosen)
            else:
                self._native = Gtk.FileChooserNative.new(
                    _("Choose PDF files"), self, Gtk.FileChooserAction.OPEN,
                    _("Open"), _("Cancel"))
                self._native.set_select_multiple(True)
                self._native.connect("response", self._native_response)
                self._native.show()

        def on_pick_folder(self):
            if has_filedialog:
                dlg = Gtk.FileDialog(title=_("Choose a folder"))
                dlg.select_folder(self, None, self._folder_chosen)
            else:
                self._native = Gtk.FileChooserNative.new(
                    _("Choose a folder"), self,
                    Gtk.FileChooserAction.SELECT_FOLDER, _("Open"), _("Cancel"))
                self._native.connect("response", self._native_response)
                self._native.show()

        def _files_chosen(self, dlg, res):
            try:
                files = dlg.open_multiple_finish(res)
            except GLib.Error:
                return
            self.enqueue([files.get_item(i).get_path()
                          for i in range(files.get_n_items())])

        def _folder_chosen(self, dlg, res):
            try:
                folder = dlg.select_folder_finish(res)
            except GLib.Error:
                return
            if folder:
                self.enqueue([folder.get_path()])

        def _native_response(self, native, response):
            if response == Gtk.ResponseType.ACCEPT:
                files = native.get_files()
                self.enqueue([files.get_item(i).get_path()
                              for i in range(files.get_n_items())])
            self._native = None

        # --- worker thread ------------------------------------------------
        def _worker(self):
            # The only thread that talks to PDFium, which is not thread-safe.
            while True:
                batch = self._jobs.get()
                files = iter_pdf(host_paths(batch))
                if not files:
                    GLib.idle_add(self._set_summary, _("No PDF files found."))
                keys = [next(self._seq) for _f in files]
                for key, f in zip(keys, files):  # every file shows up at once, queued
                    GLib.idle_add(self._row_add, key, f)
                for key, f in zip(keys, files):
                    GLib.idle_add(self._row_call, key, "start")
                    try:
                        result = crop_file(f, self._margin, self._overwrite,
                                           progress=self._progress_reporter(key))
                        GLib.idle_add(self._row_done, key, result, None)
                    except FileExistsError:
                        GLib.idle_add(self._row_done, key, None, "exists")
                    except NothingToCrop:
                        GLib.idle_add(self._row_done, key, None, "nothing")
                    except (CropError, OSError) as e:
                        GLib.idle_add(self._row_done, key, None, str(e))
                    except Exception as e:  # a page PDFium chokes on must not stop the queue
                        GLib.idle_add(self._row_done, key, None,
                                      f"{type(e).__name__}: {e}")
                GLib.idle_add(self._batch_done)

        def _progress_reporter(self, key):
            """progress(fraction) callback for crop_file, throttled so the
            main loop is not flooded on documents with many pages."""
            last = [-1.0, 0.0]  # fraction, monotonic time

            def report(fraction):
                now = time.monotonic()
                if fraction - last[0] >= 0.02 and now - last[1] >= 0.05:
                    last[0], last[1] = fraction, now
                    GLib.idle_add(self._row_call, key, "progress", fraction)
            return report

        # --- UI updates (main thread) -------------------------------------
        def _batch_done(self):
            self._pending = max(0, self._pending - 1)
            if self._pending == 0:
                self.spinner.stop()
                if any(self._counts):
                    self._set_counts()
            return False

        def _row_add(self, key, src):
            row = ResultRow(src, self._reveal)
            self._rows[key] = row
            self.listbox.append(row)
            if use_adw:  # first row: swap the empty state for the card
                self.list_stack.set_visible_child_name("list")
            return False  # one-shot GLib.idle_add

        def _row_call(self, key, method, *args):
            row = self._rows.get(key)
            if row is not None:
                getattr(row, method)(*args)
            return False

        def _row_done(self, key, result, err):
            row = self._rows.pop(key, None)
            if row is not None:
                self._counts[row.finish(result, err)] += 1
                self._set_counts()
            return False

        def _set_counts(self):
            ok, skip, errn = self._counts
            self._set_summary(_("{ok} cropped · {skipped} skipped · {errors} errors").format(
                ok=ok, skipped=skip, errors=errn))

        def _set_summary(self, text):
            self.summary.set_label(text)
            return False

        def _reveal(self, dest):
            """Show the cropped file in the platform file manager.

            Gio.AppInfo.launch_default_for_uri silently fails for file://
            URIs on Windows, hence the per-platform paths.
            """
            if is_win:
                # Explorer wants exactly  /select,"<path>" : passing a list
                # lets Popen quote the whole switch, which Explorer ignores
                # (it then opens Documents). Drive letters are kept as they
                # are (no resolve()), so mapped network drives stay mapped.
                try:
                    subprocess.Popen(f'explorer /select,"{dest}"')
                except OSError:
                    os.startfile(dest.parent)
            elif has_filedialog:  # GTK >= 4.10
                launcher = Gtk.FileLauncher.new(
                    Gio.File.new_for_path(str(dest)))
                launcher.open_containing_folder(self, None, None)
            else:
                Gio.AppInfo.launch_default_for_uri(
                    dest.parent.as_uri(), None)

    # --- update dialog (Windows) --------------------------------------------

    class UpdateDialog(Gtk.Window):
        def __init__(self, parent, rel):
            super().__init__(transient_for=parent, modal=True, resizable=False,
                             title=_("Update available"), default_width=480)
            init_dialog(self)
            self._app = parent.get_application()
            self._rel = rel
            self._cancel = threading.Event()
            self.connect("close-request", self._on_close)

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            set_margins(box, 24)
            heading = Gtk.Label(label=_("{app} {version} is available").format(
                app=APP_NAME, version=rel["version"]), xalign=0.0, wrap=True)
            heading.add_css_class("title-2")
            sub = Gtk.Label(label=_("You are using version {version}.").format(
                version=__version__), xalign=0.0)
            sub.add_css_class("dim-label")
            box.append(heading)
            box.append(sub)
            notes = rel["notes"].strip()
            if notes:
                lbl = Gtk.Label(label=notes[:4000], xalign=0.0, yalign=0.0,
                                wrap=True, selectable=True, max_width_chars=56)
                set_margins(lbl, 8)
                sc = Gtk.ScrolledWindow(child=lbl, min_content_height=120,
                                        max_content_height=240,
                                        propagate_natural_height=True)
                sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
                box.append(Gtk.Frame(child=sc))
            self.progress = Gtk.ProgressBar(show_text=True, visible=False)
            self.status = Gtk.Label(xalign=0.0, wrap=True)
            self.status.add_css_class("dim-label")
            box.append(self.progress)
            box.append(self.status)

            later = Gtk.Button(label=_("Later"))
            later.connect("clicked", lambda _b: self.close())
            self.asset = pick_asset(rel["assets"], is_installed_build()) if is_win else None
            if self.asset and is_installed_build():
                self.action = Gtk.Button(label=_("Download and install"))
                self.action.connect("clicked", self._download)
                self.status.set_label(_("The installation replaces the current "
                                        "version; the app will close."))
            else:
                self.action = Gtk.Button(label=_("Open the download page"))
                self.action.connect("clicked", self._open_page)
            self.action.add_css_class("suggested-action")
            box.append(button_row(later, self.action))
            self.set_child(box)
            self.set_default_widget(self.action)

        def _on_close(self, _win):
            self._cancel.set()
            return False

        def _open_page(self, _btn):
            open_uri(self._rel["url"], self)
            self.close()

        def _download(self, _btn):
            import tempfile
            self.action.set_sensitive(False)
            self.progress.set_visible(True)
            self.progress.set_fraction(0)
            self.status.set_label(_("Downloading…"))
            dest = Path(tempfile.gettempdir()) / self.asset["name"]

            def progress(done, total):
                GLib.idle_add(self._on_progress, done, total)

            def work():
                try:
                    download_file(self.asset["url"], dest, progress, self._cancel)
                except UpdateError as e:
                    GLib.idle_add(self._on_error, str(e))
                    return
                GLib.idle_add(self._on_downloaded, dest)
            threading.Thread(target=work, daemon=True).start()

        def _on_progress(self, done, total):
            if total:
                self.progress.set_fraction(min(done / total, 1.0))
                self.progress.set_text(f"{done / 1e6:.1f} / {total / 1e6:.1f} MB")
            else:
                self.progress.pulse()
            return False

        def _on_error(self, msg):
            self.progress.set_visible(False)
            self.status.set_label(_("Download failed: {error}").format(error=msg))
            self.action.set_sensitive(True)
            return False

        def _on_downloaded(self, dest):
            self.status.set_label(_("Starting the installer…"))
            try:
                os.startfile(str(dest))
            except OSError as e:
                self.status.set_label(_("Cannot start the installer: {error}").format(error=e))
                self.action.set_sensitive(True)
                return False
            self._app.quit()  # let the installer replace the files in peace
            return False

    # --- preferences --------------------------------------------------------

    class PreferencesWindow(Gtk.Window):
        def __init__(self, parent):
            super().__init__(transient_for=parent, modal=True, resizable=False,
                             title=_("Preferences"), default_width=540)
            init_dialog(self)
            self._parent = parent
            self.shell_label = None
            self._schemes = (("auto", _("Automatic (follow the system)")),
                             ("light", _("Light")), ("dark", _("Dark")))
            self._languages = (("auto", _("Automatic (system language)")),
                               ("it", "Italiano"), ("en", "English"))
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            set_margins(box, 24)

            box.append(self._section(_("Appearance")))
            look = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            look.append(self._row(
                _("Theme"), _("“Automatic” follows the system settings"),
                self._dropdown(self._schemes, "color_scheme", self._on_scheme)))
            look.append(self._row(
                _("Language"), _("Takes effect at the next start"),
                self._dropdown(self._languages, "language", self._on_language)))
            if is_win:
                look.append(self._row(
                    _("Native Windows decorations"),
                    _("System title bar instead of GTK's (takes effect at the next start)"),
                    self._switch("native_decorations")))
            box.append(Gtk.Frame(child=look))

            if is_win:
                box.append(self._section(_("General")))
                general = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
                general.append(self._row(
                    _("Check for updates at start-up"),
                    _("Once a day, from the project's GitHub Releases"),
                    self._switch("check_updates")))
                box.append(Gtk.Frame(child=general))

                box.append(self._section(_("File Explorer")))
                shell = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
                reg = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
                b_reg = Gtk.Button(label=_("Add"))
                b_reg.connect("clicked", self._on_register)
                b_unreg = Gtk.Button(label=_("Remove"))
                b_unreg.connect("clicked", self._on_unregister)
                reg.append(b_reg)
                reg.append(b_unreg)
                row, self.shell_label = self._row(
                    _("“Crop white margins” in the context menu of PDF files"),
                    self._shell_text(), reg, return_subtitle=True)
                shell.append(row)
                box.append(Gtk.Frame(child=shell))
            self.set_child(box)

        @staticmethod
        def _dropdown(options, key, callback):
            keys = [k for k, _label in options]
            drop = Gtk.DropDown.new_from_strings([label for _k, label in options])
            drop.set_valign(Gtk.Align.CENTER)
            current = settings.get(key)
            drop.set_selected(keys.index(current) if current in keys else 0)
            drop.connect("notify::selected", callback)
            return drop

        def _on_scheme(self, drop, _pspec):
            settings.set("color_scheme", self._schemes[drop.get_selected()][0])
            theme.apply()

        def _on_language(self, drop, _pspec):
            settings.set("language", self._languages[drop.get_selected()][0])
            self._parent.show_banner(_("The new language will be used at the next start."))

        @staticmethod
        def _section(text):
            lbl = Gtk.Label(label=text, xalign=0.0)
            lbl.add_css_class("heading")
            lbl.set_margin_top(8)
            return lbl

        @staticmethod
        def _switch(key):
            sw = Gtk.Switch(active=settings.get_bool(key), valign=Gtk.Align.CENTER)
            sw.connect("state-set",
                       lambda _s, state: (settings.set(key, bool(state)), False)[1])
            return sw

        @staticmethod
        def _row(title, subtitle, widget, return_subtitle=False):
            row = Gtk.ListBoxRow(activatable=False)
            hbox = Gtk.Box(spacing=12)
            set_margins(hbox, top=8, bottom=8, start=12, end=12)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                            valign=Gtk.Align.CENTER)
            t = Gtk.Label(label=title, xalign=0.0, wrap=True)
            st = Gtk.Label(label=subtitle, xalign=0.0, wrap=True)
            st.add_css_class("dim-label")
            st.add_css_class("caption")
            texts.append(t)
            texts.append(st)
            hbox.append(texts)
            hbox.append(widget)
            row.set_child(hbox)
            return (row, st) if return_subtitle else row

        @staticmethod
        def _shell_text():
            try:
                registered = win_is_registered()
            except OSError:
                registered = False
            if registered:
                return _("Currently: present (on Windows 11 under “Show more options”)")
            return _("Currently: not present")

        def _refresh(self, message=None):
            self.shell_label.set_label(self._shell_text())
            if message:
                self._parent.show_banner(message)

        def _on_register(self, _btn):
            try:
                win_register()
                self._refresh(_("Context-menu entry added."))
            except OSError as e:
                self._refresh(_("Registration failed: {error}").format(error=e))

        def _on_unregister(self, _btn):
            try:
                win_unregister()
                self._refresh(_("Context-menu entry removed for the current user."))
            except OSError as e:
                self._refresh(_("Removal failed: {error}").format(error=e))

    def adw_preferences(parent):
        """Preferences as an AdwPreferencesDialog: HIG boxed list, combo rows.
        The plain-GTK PreferencesWindow above serves Windows."""
        schemes = (("auto", _("Automatic (follow the system)")),
                   ("light", _("Light")), ("dark", _("Dark")))
        languages = (("auto", _("Automatic (system language)")),
                     ("it", "Italiano"), ("en", "English"))

        def on_scheme(value):
            settings.set("color_scheme", value)
            theme.apply()

        def on_language(value):
            settings.set("language", value)
            parent.show_banner(_("The new language will be used at the next start."))

        def combo(title, subtitle, options, key, callback):
            keys = [k for k, _label in options]
            row = Adw.ComboRow(
                title=title, subtitle=subtitle,
                model=Gtk.StringList.new([label for _k, label in options]))
            current = settings.get(key)
            row.set_selected(keys.index(current) if current in keys else 0)
            row.connect("notify::selected",
                        lambda r, _p: callback(keys[r.get_selected()]))
            return row

        group = Adw.PreferencesGroup(title=_("Appearance"))
        group.add(combo(_("Theme"), _("“Automatic” follows the system settings"),
                        schemes, "color_scheme", on_scheme))
        group.add(combo(_("Language"), _("Takes effect at the next start"),
                        languages, "language", on_language))
        page = Adw.PreferencesPage()
        page.add(group)
        dialog = Adw.PreferencesDialog(title=_("Preferences"))
        dialog.add(page)
        dialog.present(parent)

    # --- application --------------------------------------------------------

    AppBase = Adw.Application if use_adw else Gtk.Application

    class App(AppBase):
        """Single-instance application: a second launch (context-menu entry,
        "Open with", several files selected at once) forwards its command
        line to the running window instead of opening another one."""

        def __init__(self):
            super().__init__(application_id=APP_ID,
                             flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
            self.settings = settings
            self.window = None

        def do_startup(self):
            nonlocal theme
            AppBase.do_startup(self)
            _mark("startup")
            display = Gdk.Display.get_default()
            bundle = getattr(sys, "_MEIPASS", None)
            if bundle:  # icons shipped inside the PyInstaller bundle
                Gtk.IconTheme.get_for_display(display).add_search_path(
                    os.path.join(bundle, "share", "icons"))
            else:  # source checkout: data/icons next to this file (no-op when installed)
                icons = Path(__file__).resolve().parent / "data" / "icons"
                if icons.is_dir():
                    Gtk.IconTheme.get_for_display(display).add_search_path(str(icons))
            Gtk.Window.set_default_icon_name(APP_ID)

            css = Gtk.CssProvider()
            css_data = _CSS_ADW if use_adw else _CSS
            try:  # GTK >= 4.12
                css.load_from_string(css_data.decode())
            except AttributeError:  # older GTK 4, PyGObject signature varies
                try:
                    css.load_from_data(css_data)
                except TypeError:
                    css.load_from_data(css_data, len(css_data))
            Gtk.StyleContext.add_provider_for_display(
                display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            theme = ThemeManager()

            self._action("quit", lambda _a, _p: self.quit(), ["<Control>q"])
            self._action("about", lambda _a, _p: self._win().show_about())
            self._action("open-files", lambda _a, _p: self._win().on_pick_files(),
                         ["<Control>o"])
            self._action("open-folder", lambda _a, _p: self._win().on_pick_folder(),
                         ["<Control><Shift>o"])
            self._action("preferences", lambda _a, _p: (
                adw_preferences(self._win()) if use_adw
                else PreferencesWindow(self._win()).present()),
                ["<Control>comma"])
            self._action("banner-action",
                         lambda _a, _p: self._win().run_banner_action())
            if is_win:
                self._action("check-updates",
                             lambda _a, _p: self._win().check_updates(manual=True))

        def _action(self, name, callback, accels=()):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", list(accels))

        def _win(self):
            if self.window is None:
                self.window = Window(self)
            return self.window

        def do_activate(self):
            self._show(())

        def do_open(self, files, _n_files, _hint):  # D-Bus activation
            self._show([f.get_path() for f in files])

        def do_command_line(self, cmdline):
            # Runs in the primary instance, also for command lines forwarded
            # by later launches (their cwd may differ from ours).
            _mark("command-line")
            args = list(cmdline.get_arguments())[1:]
            try:
                ns, _unknown = build_parser().parse_known_args(args)
            except SystemExit:
                return 1
            cwd = cmdline.get_cwd() or os.getcwd()
            self._show([os.path.normpath(os.path.join(cwd, p)) for p in ns.paths])
            return 0

        def _show(self, paths):
            first = self.window is None
            win = self._win()
            win.present()
            if is_win and not first:  # files handed over by a later launch
                win_bring_to_front(APP_NAME)
            if paths:
                win.enqueue(list(paths))
            if first:
                GLib.idle_add(win.first_shown)

    if is_win:
        win_allow_foreground()
    return App().run(argv)


# ---------------------------------------------------------------------------


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

    settings = Settings()
    setup_i18n(settings.get("language"))
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
    return run_gui(sys.argv, settings)


if __name__ == "__main__":
    sys.exit(main())
